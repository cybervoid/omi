"""Best-effort convergence helpers for the self-host Typesense projection.

Firestore remains authoritative. These helpers refresh search projections after
post-finalization mutations while keeping every product write fail-open when
Typesense is unavailable. A periodic full reconcile is still the crash-recovery
safety net for writes that commit immediately before a process exit.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

import database.conversations as conversations_db
from utils.conversations.search import index_conversation

logger = logging.getLogger(__name__)


def refresh_conversation_index(uid: str, conversation_id: str) -> None:
    """Re-read one authoritative conversation and upsert its search projection."""
    if not os.getenv("TYPESENSE_API_KEY"):
        return
    try:
        conversation = conversations_db.get_conversation(uid, conversation_id)
        if conversation is None:
            return
        index_conversation(uid, conversation)
    except Exception as exc:
        logger.warning(
            "refresh_conversation_index failed uid=%s id=%s: %s",
            uid,
            conversation_id,
            exc,
        )


def refresh_conversation_indexes(uid: str, conversation_ids: Iterable[str]) -> None:
    """Refresh a bounded set once each, preserving fail-open mutation semantics."""
    seen: set[str] = set()
    for conversation_id in conversation_ids:
        normalized = str(conversation_id)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        refresh_conversation_index(uid, normalized)


def refresh_all_user_conversation_indexes(uid: str) -> None:
    """Refresh a user's corpus after a batch mutation such as subscription unlock."""
    if not os.getenv("TYPESENSE_API_KEY"):
        return
    try:
        for conversation in conversations_db.iter_all_conversations(uid, include_discarded=True):
            index_conversation(uid, conversation)
    except Exception as exc:
        logger.warning("refresh_all_user_conversation_indexes failed uid=%s: %s", uid, exc)
