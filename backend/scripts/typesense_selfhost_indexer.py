#!/usr/bin/env python3
"""Self-host Typesense indexer for Omi conversations.

Runs INSIDE the backend container (working dir /app) so it can reuse the app's
database layer. Upstream Omi keeps the Typesense `conversations` collection in
sync via a Firebase "firestore-typesense-search" extension; the single-VM
self-host does not run that, so this script is the indexer:

  ensure-collection          Create the `conversations` collection if missing.
  backfill [--uid UID]       Index every conversation (all users, or one uid).
  sync --since-min N [--uid] Index only conversations created in the last N min.

The document shape matches what utils/conversations/search.py reads back:
timestamps are stored as unix ints (started_at/finished_at fall back to
created_at so no hit is skipped), plus userId and a curated `structured`.
transcript_segments are intentionally NOT stored (the app would reject partial
segments and full transcripts would bloat the index); the speaker-filter fields
are still declared optional so a speaker-filtered search returns empty instead
of erroring.
"""
import argparse
import json
import logging
import os

import typesense

import database.conversations as conversations_db
from database._client import get_users_uid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ts-indexer")

COLLECTION = "conversations"
PAGE_SIZE = 200

SCHEMA = {
    "name": COLLECTION,
    "enable_nested_fields": True,
    "fields": [
        {"name": "userId", "type": "string"},
        {"name": "created_at", "type": "int64"},
        {"name": "started_at", "type": "int64", "optional": True},
        {"name": "finished_at", "type": "int64", "optional": True},
        {"name": "discarded", "type": "bool", "optional": True},
        {"name": "is_locked", "type": "bool", "optional": True},
        {"name": "structured.title", "type": "string", "optional": True},
        {"name": "structured.overview", "type": "string", "optional": True},
        # Declared so speaker-filtered searches don't error; never populated here.
        {"name": "transcript_segments.is_user", "type": "bool[]", "optional": True},
        {"name": "transcript_segments.person_id", "type": "string[]", "optional": True},
        # Store any remaining scalar fields so the app can render results.
        {"name": ".*", "type": "auto", "optional": True},
    ],
}


def get_client() -> typesense.Client:
    return typesense.Client(
        {
            "nodes": [
                {
                    "host": os.getenv("TYPESENSE_HOST", "typesense"),
                    "port": os.getenv("TYPESENSE_HOST_PORT", "8108"),
                    "protocol": os.getenv("TYPESENSE_PROTOCOL", "http"),
                }
            ],
            "api_key": os.getenv("TYPESENSE_API_KEY", ""),
            "connection_timeout_seconds": 30,
        }
    )


def ensure_collection(client: typesense.Client) -> None:
    try:
        client.collections[COLLECTION].retrieve()
        log.info("collection '%s' already exists", COLLECTION)
        return
    except Exception:
        pass
    try:
        client.collections.create(SCHEMA)
        log.info("created collection '%s'", COLLECTION)
    except Exception as e:
        log.info("ensure_collection: create returned '%s' (assuming it now exists)", e)


def _to_int_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(v.timestamp())
    except Exception:
        return None


def to_document(uid: str, c: dict):
    cid = c.get("id")
    created = _to_int_ts(c.get("created_at"))
    if not cid or created is None:
        return None
    started = _to_int_ts(c.get("started_at")) or created
    finished = _to_int_ts(c.get("finished_at")) or created
    s = c.get("structured") or {}
    doc = {
        "id": str(cid),
        "userId": uid,
        "created_at": created,
        "started_at": started,
        "finished_at": finished,
        "discarded": bool(c.get("discarded", False)),
        "is_locked": bool(c.get("is_locked", False)),
        "starred": bool(c.get("starred", False)),
        "source": c.get("source") or "omi",
        "status": c.get("status") or "completed",
        "visibility": c.get("visibility") or "private",
        "language": c.get("language"),
        "folder_id": c.get("folder_id"),
        "structured": {
            "title": s.get("title") or "",
            "overview": s.get("overview") or "",
            "category": s.get("category") or "other",
            "emoji": s.get("emoji") or "",
        },
    }
    return {k: v for k, v in doc.items() if v is not None}


def _count_failures(results):
    fails = []
    for r in results or []:
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except Exception:
                continue
        if isinstance(r, dict) and not r.get("success", True):
            fails.append(r)
    return fails


def index_uid(client: typesense.Client, uid: str, start_date=None) -> int:
    total = 0
    offset = 0
    while True:
        convs = conversations_db.get_conversations_without_photos(
            uid, limit=PAGE_SIZE, offset=offset, include_discarded=True, start_date=start_date
        )
        if not convs:
            break
        docs = [d for d in (to_document(uid, c) for c in convs) if d]
        if docs:
            results = client.collections[COLLECTION].documents.import_(docs, {"action": "upsert"})
            fails = _count_failures(results)
            if fails:
                log.warning("uid=%s: %d import failures (first: %s)", uid, len(fails), fails[0])
            total += len(docs) - len(fails)
        got = len(convs)
        offset += got
        if got < PAGE_SIZE:
            break
    return total


def run(cmd: str, uid=None, since_min=None) -> None:
    client = get_client()
    ensure_collection(client)
    if cmd == "ensure-collection":
        return

    start_date = None
    if since_min:
        from datetime import datetime, timezone, timedelta

        start_date = datetime.now(timezone.utc) - timedelta(minutes=since_min)

    uids = [uid] if uid else get_users_uid()
    grand = 0
    for u in uids:
        try:
            n = index_uid(client, u, start_date=start_date)
            grand += n
            log.info("indexed uid=%s count=%d (running=%d)", u, n, grand)
        except Exception as e:
            log.error("failed uid=%s: %s", u, e)
    log.info("DONE total_indexed=%d users=%d", grand, len(uids))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ensure-collection", "backfill", "sync"])
    ap.add_argument("--uid", default=None)
    ap.add_argument("--since-min", type=int, default=None)
    args = ap.parse_args()
    if args.cmd == "sync" and not args.since_min:
        args.since_min = 60
    run(args.cmd, uid=args.uid, since_min=args.since_min)
