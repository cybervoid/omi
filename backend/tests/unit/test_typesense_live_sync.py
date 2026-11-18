from utils.conversations import index_sync


def test_refresh_conversation_index_upserts_authoritative_document(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key")
    conversation = {"id": "c1", "created_at": 1}
    calls = []
    monkeypatch.setattr(index_sync.conversations_db, "get_conversation", lambda uid, cid: conversation)
    monkeypatch.setattr(index_sync, "index_conversation", lambda uid, doc: calls.append((uid, doc)))

    index_sync.refresh_conversation_index("u1", "c1")

    assert calls == [("u1", conversation)]


def test_refresh_conversation_index_missing_source_is_noop(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(index_sync.conversations_db, "get_conversation", lambda uid, cid: None)
    monkeypatch.setattr(index_sync, "index_conversation", lambda uid, doc: calls.append((uid, doc)))

    index_sync.refresh_conversation_index("u1", "missing")

    assert calls == []


def test_refresh_conversation_index_is_fail_open(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key")

    def fail(_uid, _conversation_id):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(index_sync.conversations_db, "get_conversation", fail)
    index_sync.refresh_conversation_index("u1", "c1")


def test_refresh_conversation_indexes_deduplicates_ids(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(
        index_sync,
        "refresh_conversation_index",
        lambda uid, conversation_id: calls.append((uid, conversation_id)),
    )

    index_sync.refresh_conversation_indexes("u1", ["c1", "c1", "c2", ""])

    assert calls == [("u1", "c1"), ("u1", "c2")]


def test_refresh_all_user_conversation_indexes_includes_discarded(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key")
    conversations = [
        {"id": "c1", "created_at": 1, "discarded": False},
        {"id": "c2", "created_at": 2, "discarded": True},
    ]
    observed = {}
    calls = []

    def iter_all(uid, include_discarded):
        observed.update(uid=uid, include_discarded=include_discarded)
        return iter(conversations)

    monkeypatch.setattr(index_sync.conversations_db, "iter_all_conversations", iter_all)
    monkeypatch.setattr(index_sync, "index_conversation", lambda uid, doc: calls.append((uid, doc["id"])))

    index_sync.refresh_all_user_conversation_indexes("u1")

    assert observed == {"uid": "u1", "include_discarded": True}
    assert calls == [("u1", "c1"), ("u1", "c2")]


def test_refresh_all_user_conversation_indexes_is_fail_open(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key")

    def fail(_uid, include_discarded):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(index_sync.conversations_db, "iter_all_conversations", fail)
    index_sync.refresh_all_user_conversation_indexes("u1")
