"""Finalize abandoned ``in_progress`` conversations.

Why this exists
---------------
During a listen session the backend keeps the active conversation in
``status == in_progress`` and only finalizes it when the *next* session connects
(``routers/transcribe.py`` -> ``_prepare_in_progess_conversations`` ->
``_process_conversation``): if more than ``conversation_creation_timeout`` (>=120s)
has elapsed since the last segment, the old conversation is pushed to processing
and a new stub is created.

If the user never starts another session (app killed, device off), that
conversation stays ``in_progress`` forever and never gets a summary/memories.
This script is the missing periodic sweep: it finds ``in_progress`` conversations
whose ``finished_at`` is older than a safety threshold and finalizes them the same
way the live path does -- run the full ``process_conversation`` pipeline when there
is content, or delete the empty stub.

Safety
------
The default threshold (900s / 15 min) is far larger than the live session
inactivity timeout (120s) and the WebSocket no-data timeout (300s), so this will
never race an active or recently-active session. Run it as a cron, e.g. every
15 minutes:

    cd ~/omi-deploy && docker compose exec -T backend python scripts/finalize_stale_conversations.py

Use ``--dry-run`` to log what would happen without mutating anything.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from google.cloud.firestore_v1 import FieldFilter

import database.conversations as conversations_db
from database import redis_db
from database._client import db
from models.conversation_enums import ConversationStatus
from utils.conversations.factory import deserialize_conversation
from utils.conversations.process_conversation import process_conversation
from utils.executors import (
    critical_executor,
    db_executor,
    llm_executor,
    postprocess_executor,
    storage_executor,
    stripe_executor,
    sync_executor,
)

logger = logging.getLogger("finalize_stale_conversations")

DEFAULT_THRESHOLD_SECONDS = int(os.getenv("OMI_FINALIZER_THRESHOLD_SECONDS", "900"))
conversations_collection = "conversations"


def _all_user_ids() -> List[str]:
    """Stream every user document id without reading any fields."""
    return [doc.id for doc in db.collection("users").select([]).stream()]


def _stale_in_progress_ids(uid: str, cutoff: datetime) -> List[str]:
    """Return ids of this user's in_progress conversations last touched before ``cutoff``.

    ``finished_at`` is a plain (unencrypted) top-level timestamp, so we read it from
    the raw document and avoid decrypting anything during discovery.
    """
    coll = db.collection("users").document(uid).collection(conversations_collection)
    query = coll.where(filter=FieldFilter("status", "==", ConversationStatus.in_progress.value))
    stale: List[str] = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        finished_at = data.get("finished_at")
        # No timestamp -> treat as stale. Otherwise only finalize if it is older than the cutoff.
        if finished_at is None or finished_at < cutoff:
            stale.append(doc.id)
    return stale


def _finalize_one(uid: str, conversation_id: str, dry_run: bool) -> str:
    """Finalize a single conversation. Returns a short outcome label for logging."""
    full = conversations_db.get_conversation(uid, conversation_id)
    if not full:
        return "missing"

    has_content = bool(full.get("transcript_segments")) or bool(full.get("photos"))

    if not has_content:
        if dry_run:
            return "would_delete_empty"
        conversations_db.delete_conversation(uid, conversation_id)
        if redis_db.get_in_progress_conversation_id(uid) == conversation_id:
            redis_db.remove_in_progress_conversation_id(uid)
        return "deleted_empty"

    if dry_run:
        return "would_process"

    conversations_db.update_conversation_status(uid, conversation_id, ConversationStatus.processing)
    language = full.get("language") or "en"
    try:
        conversation = deserialize_conversation(full)
        process_conversation(uid, language, conversation)
        outcome = "processed"
    except Exception as e:
        logger.error(f"Failed to process stale conversation {conversation_id} for {uid}: {e}")
        conversations_db.set_conversation_as_discarded(uid, conversation_id)
        outcome = "errored_discarded"

    if redis_db.get_in_progress_conversation_id(uid) == conversation_id:
        redis_db.remove_in_progress_conversation_id(uid)
    return outcome


def _drain_executors() -> None:
    """Wait for fan-out background work (memories, trends, action items, vectors,
    webhooks) submitted by process_conversation before the process exits.

    Drain postprocess first -- its tasks call into the llm/db/storage pools and block
    on their results, so those must stay alive until postprocess is empty.
    """
    postprocess_executor.shutdown(wait=True)
    for executor in (llm_executor, db_executor, storage_executor, sync_executor, critical_executor, stripe_executor):
        executor.shutdown(wait=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize abandoned in_progress conversations.")
    parser.add_argument(
        "--threshold-seconds",
        type=int,
        default=DEFAULT_THRESHOLD_SECONDS,
        help="Only finalize conversations idle (finished_at) for at least this long. Default 900s.",
    )
    parser.add_argument(
        "--uid",
        default=None,
        help="Limit the sweep to a single user id (default: all users).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be finalized without mutating anything.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=args.threshold_seconds)
    uids = [args.uid] if args.uid else _all_user_ids()
    logger.info(
        f"Sweep start: users={len(uids)} threshold={args.threshold_seconds}s "
        f"cutoff={cutoff.isoformat()} dry_run={args.dry_run}"
    )

    scanned = 0
    finalized = 0
    try:
        for uid in uids:
            try:
                stale_ids = _stale_in_progress_ids(uid, cutoff)
            except Exception as e:
                logger.error(f"Failed to query in_progress conversations for {uid}: {e}")
                continue
            for conversation_id in stale_ids:
                scanned += 1
                try:
                    outcome = _finalize_one(uid, conversation_id, args.dry_run)
                except Exception as e:
                    logger.error(f"Unexpected error finalizing {conversation_id} for {uid}: {e}")
                    continue
                if outcome not in ("missing",):
                    finalized += 1
                logger.info(f"{uid} {conversation_id} -> {outcome}")
    finally:
        if not args.dry_run:
            _drain_executors()

    logger.info(f"Sweep done: scanned={scanned} finalized={finalized} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
