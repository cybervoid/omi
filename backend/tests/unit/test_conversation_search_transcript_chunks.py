"""Unit tests for transcript-chunk retrieval in conversation search.

Covers:
  * ``interleave_conversation_search_ids`` — the pure round-robin merge that keeps each retrieval
    layer's top hits near the front (so a verbatim transcript-chunk hit is not starved by a full
    page of fuzzy keyword matches once the ``limit`` cap is applied).
  * ``search_conversations_tool`` — that the transcript-chunk layer's conversation ids are merged
    into the candidate set and its verbatim excerpts are surfaced, and that a chunk-search failure
    fails open (keyword + vector results still returned).

No network or real provider calls.
"""

import os

os.environ.setdefault(
    "ENCRYPTION_SECRET",
    "omi_ZwB2ZNqB2HHpMK6wStk7sTpavJiPTFg7gXUHnc4tFABPU6pZ2c2DKgehtfgi4RZv",
)
# conversation_tools -> utils.conversations.search constructs a Typesense client at import time;
# give it dummy config so import succeeds (client validates config only, never connects here).
os.environ.setdefault("TYPESENSE_API_KEY", "test-typesense-key")
os.environ.setdefault("TYPESENSE_HOST", "localhost")
os.environ.setdefault("TYPESENSE_HOST_PORT", "8108")

from utils.conversations.search import interleave_conversation_search_ids
from utils.retrieval.tools import conversation_tools

# --------------------------------------------------------------------------- #
# interleave_conversation_search_ids (pure)
# --------------------------------------------------------------------------- #


def test_interleave_round_robin_order():
    # First tier is the top hit of each list, in argument order, then the second tier, etc.
    assert interleave_conversation_search_ids(["k1", "k2"], ["c1", "c2"], ["v1", "v2"]) == [
        "k1",
        "c1",
        "v1",
        "k2",
        "c2",
        "v2",
    ]


def test_interleave_dedupes_keeping_first_occurrence():
    # A shared id keeps its earliest (highest-priority) position and is not repeated.
    assert interleave_conversation_search_ids(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_interleave_handles_uneven_and_empty_lists():
    assert interleave_conversation_search_ids(["k1"], [], ["v1", "v2", "v3"]) == ["k1", "v1", "v2", "v3"]
    assert interleave_conversation_search_ids([], []) == []


def test_interleave_keeps_chunk_hit_within_small_cap():
    # The key property: even when keyword returns a full page, the top transcript-chunk hit lands
    # early enough to survive a `limit`-sized cap.
    keyword = ["k1", "k2", "k3", "k4", "k5"]
    chunk = ["real-hit"]
    vector = ["v1"]
    merged = interleave_conversation_search_ids(keyword, chunk, vector)
    assert merged[:3] == ["k1", "real-hit", "v1"]
    assert "real-hit" in merged[:5]


# --------------------------------------------------------------------------- #
# search_conversations_tool transcript-chunk wiring
# --------------------------------------------------------------------------- #


class _FakeConversation:
    """Minimal stand-in for a deserialized Conversation used by the tool."""

    def __init__(self, cid):
        self.id = cid
        self.transcript_segments = []

    def model_dump(self):
        return {"id": self.id}


def _config():
    return {"configurable": {"user_id": "u-1", "safety_guard": None, "conversations_collected": []}}


def _patch_common(monkeypatch):
    monkeypatch.setattr(conversation_tools, "deserialize_conversation", lambda data: _FakeConversation(data["id"]))
    monkeypatch.setattr(conversation_tools, "conversations_to_string", lambda *a, **k: "CONV_SUMMARIES")
    monkeypatch.setattr(conversation_tools.notification_db, "get_user_time_zone", lambda uid: "UTC")
    monkeypatch.setattr(conversation_tools.users_db, "get_people_by_ids", lambda uid, ids: [])
    monkeypatch.setattr(
        conversation_tools.conversations_db,
        "get_conversations_by_id",
        lambda uid, ids: [{"id": cid, "is_locked": False, "transcript_segments": []} for cid in ids],
    )


def test_transcript_chunk_ids_merged_and_excerpts_surfaced(monkeypatch):
    _patch_common(monkeypatch)

    captured = {}

    def _capture_get_conversations_by_id(uid, ids):
        # Record the (interleaved) candidate ids the tool asked to load, then return the matching
        # conversation dicts. NB: don't use ``setdefault(...) or [...]`` here — a non-empty ``ids``
        # list is truthy, so the ``or`` would short-circuit and return the id strings instead of dicts.
        captured["ids"] = ids
        return [{"id": cid, "is_locked": False, "transcript_segments": []} for cid in ids]

    monkeypatch.setattr(
        conversation_tools.conversations_db,
        "get_conversations_by_id",
        _capture_get_conversations_by_id,
    )
    monkeypatch.setattr(conversation_tools, "keyword_search_conversation_ids", lambda **kw: ["kw-1"])
    monkeypatch.setattr(conversation_tools.vector_db, "query_vectors", lambda **kw: ["vec-1"])
    # Two chunks from the same conversation -> de-duped to one conversation id.
    monkeypatch.setattr(
        conversation_tools.vector_db,
        "search_transcript_chunks",
        lambda uid, query, limit, starts_at, ends_at: [
            {"conversation_id": "chunk-conv", "chunk_index": 0, "created_at": 1, "score": 0.91},
            {"conversation_id": "chunk-conv", "chunk_index": 1, "created_at": 1, "score": 0.80},
        ],
    )
    monkeypatch.setattr(
        conversation_tools,
        "hydrate_chunk_texts",
        lambda uid, rows: [{**rows[0], "text": "User: had lunch with Lisa on Friday"}],
    )

    result = conversation_tools.search_conversations_tool.func(query="lunch with Lisa", config=_config())

    # The transcript-chunk conversation is in the loaded candidate set, ordered right after keyword.
    assert "chunk-conv" in captured["ids"]
    assert captured["ids"][:3] == ["kw-1", "chunk-conv", "vec-1"]
    # Verbatim excerpt is surfaced so the model sees the spoken evidence.
    assert "VERBATIM TRANSCRIPT MATCHES" in result
    assert "lunch with Lisa" in result


def test_transcript_chunk_search_failure_fails_open(monkeypatch):
    _patch_common(monkeypatch)

    monkeypatch.setattr(conversation_tools, "keyword_search_conversation_ids", lambda **kw: ["kw-1"])
    monkeypatch.setattr(conversation_tools.vector_db, "query_vectors", lambda **kw: ["vec-1"])

    def _boom(*a, **k):
        raise RuntimeError("pinecone down")

    monkeypatch.setattr(conversation_tools.vector_db, "search_transcript_chunks", _boom)

    # Must not raise; keyword + vector results still produce an answer, no excerpts section.
    result = conversation_tools.search_conversations_tool.func(query="lunch with Lisa", config=_config())
    assert "CONV_SUMMARIES" in result
    assert "VERBATIM TRANSCRIPT MATCHES" not in result
