#!/usr/bin/env python3
"""Backfill verbatim transcript-chunk vectors (Pinecone ``ns_tchunks``) for existing conversations.

Runs INSIDE the backend container (working dir /app) so it can reuse the app's database layer and
its decryption (transcripts are encrypted at rest in Firestore). Conversation vectors (ns1) embed
only the structured SUMMARY, so specific spoken details (names, numbers, one-off mentions) are not
findable semantically. ``TRANSCRIPT_CHUNK_INDEXING_ENABLED=true`` starts indexing NEW conversations
on the fly; this script populates chunks for conversations that already exist.

  backfill [--uid UID]   Index chunks for every conversation (all users, or one uid).

Each conversation is re-chunked deterministically (utils.conversations.transcript_chunks) and
upserted; existing chunk vectors for a conversation are deleted first so a re-run cannot leave stale
higher-index chunks behind. Chunk TEXT is embedded but never stored in Pinecone metadata — readers
re-hydrate text from Firestore. Fail-open per conversation: one failure never aborts the run.
"""

import argparse
import logging

import database.conversations as conversations_db
import database.vector_db as vector_db
from database._client import get_users_uid
from utils.conversations.transcript_chunks import build_transcript_chunks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tchunks-backfill")


def index_uid(uid: str) -> int:
    """Backfill transcript-chunk vectors for a single user. Returns the number of chunks upserted."""
    total_chunks = 0
    total_convs = 0
    for c in conversations_db.iter_all_conversations(uid, include_discarded=True):
        cid = c.get("id")
        if not cid:
            continue
        total_convs += 1
        try:
            segments = c.get("transcript_segments") or []
            chunks = build_transcript_chunks(segments, c.get("started_at") or c.get("created_at"))
            # Clear existing chunk vectors first so a re-run can't leave stale chunks behind.
            vector_db.delete_transcript_chunk_vectors(uid, cid)
            if chunks:
                total_chunks += vector_db.upsert_transcript_chunk_vectors(uid, cid, chunks)
        except Exception as e:
            log.warning("uid=%s conversation=%s failed: %s", uid, cid, e)
    log.info("uid=%s conversations=%d chunks_upserted=%d", uid, total_convs, total_chunks)
    return total_chunks


def run(uid=None) -> None:
    uids = [uid] if uid else get_users_uid()
    grand = 0
    for u in uids:
        try:
            grand += index_uid(u)
        except Exception as e:
            log.error("failed uid=%s: %s", u, e)
    log.info("DONE total_chunks=%d users=%d", grand, len(uids))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill"])
    ap.add_argument("--uid", default=None)
    args = ap.parse_args()
    run(uid=args.uid)
