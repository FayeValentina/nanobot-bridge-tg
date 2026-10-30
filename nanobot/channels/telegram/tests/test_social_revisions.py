"""Legacy-key retirement, source revisions and frozen-request identity."""

import json

import pytest

from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_judge import A, case, updated
from nanobot.channels.telegram.tests.test_social_scheduler import remove_v4_columns

__all__ = ["case"]


def test_batch_id_cannot_be_reused_with_changed_frozen_intent(case):
    store, settings, provider, judge, snapshot, _ = case
    record = store.open_judge_batch(snapshot, judge.revision, settings.model)
    changed = updated(snapshot, role_summary="changed after the request was frozen")
    with pytest.raises(PermissionError, match="input changed"):
        store.claim_judge_attempt(record.id, changed, limit=240, interval=0, max_attempts=2)
    assert store.judge_record(record.id).attempts == 0
    provider.chat.assert_not_called()


def test_schema_ten_upgrade_removes_keys_and_preserves_journals(case, tmp_path):
    store, settings, _, judge, snapshot, now = case
    old_version = "a" * 64  # Synthetic legacy value; no hash calculation.
    store.bind(A, old_version)
    with store.db:
        store.db.execute("UPDATE judge_state SET assessed_through=0 WHERE scope=?", (A.session_key,))
    snapshot = updated(snapshot, version=old_version)
    batch = store.open_judge_batch(snapshot, judge.revision, settings.model)
    store.claim_judge_attempt(batch.id, snapshot, limit=240, interval=0, max_attempts=2)
    with store.db:
        store.db.execute("UPDATE scopes SET read_through=1,seen_through=2,mode='waiting',wake_at=1070 WHERE key=?", (A.session_key,))
        store.db.execute("UPDATE judge_state SET batch_id=?,pending_seq=3 WHERE scope=?", (batch.id, A.session_key))
        store.db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("retired", A.session_key, "preference", " OLD   FACT ", "[]", "deleted", "direct", 1, 990., 995., None))
        store.db.execute("INSERT INTO memory_evidence VALUES(?,?,?,?,?,?)", ("retired", 11, 1, "1", 990., "original evidence"))
        store.db.execute("INSERT INTO memory_suppressions VALUES(?,?,?)", (A.session_key, "content", "opaque-legacy-value"))
        store.db.execute("ALTER TABLE memory_suppressions RENAME COLUMN value TO digest")
        store.db.execute("ALTER TABLE judge_batches DROP COLUMN judge_revision")
        store.db.execute("ALTER TABLE judge_batches ADD COLUMN signature TEXT NOT NULL DEFAULT 'old-key'")
        store.db.execute("ALTER TABLE judge_batches ADD COLUMN input_hash TEXT NOT NULL DEFAULT 'old-input'")
        store.db.execute("CREATE UNIQUE INDEX legacy_keys ON judge_batches(scope,version,signature,input_hash)")
        data = json.loads(store.db.execute("SELECT input_meta FROM judge_batches WHERE id=?", (batch.id,)).fetchone()[0])
        data.pop("batch_id")
        for item in data["excerpts"]:
            item.pop("revision")
            item["content_hash"] = "legacy-content-key"
        store.db.execute("UPDATE judge_batches SET input_meta=? WHERE id=?", (json.dumps(data), batch.id))
        store.db.execute("ALTER TABLE messages DROP COLUMN revision")
        store.db.execute("DROP TABLE runtime_settings")
        remove_v4_columns(store.db)
        store.db.execute("PRAGMA user_version=10")
    messages = [tuple(row) for row in store.db.execute("SELECT * FROM messages ORDER BY seq")]
    memory = tuple(store.db.execute("SELECT * FROM memories").fetchone())
    store.close()

    import sqlite3

    with sqlite3.connect(tmp_path / "social.sqlite3") as db:
        db.execute("UPDATE judge_batches SET input_meta='invalid'")
    with pytest.raises(json.JSONDecodeError):
        SocialStore(tmp_path, now=lambda: now[0])
    with sqlite3.connect(tmp_path / "social.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert "signature" in {row[1] for row in db.execute("PRAGMA table_info(judge_batches)")}
        assert "revision" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        db.execute("UPDATE judge_batches SET input_meta=?", (json.dumps(data),))

    restored = SocialStore(tmp_path, now=lambda: now[0])
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert restored.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(row)[:-1] for row in restored.db.execute("SELECT * FROM messages ORDER BY seq")] == messages
        assert tuple(restored.db.execute("SELECT * FROM memories").fetchone()) == memory
        state = restored.state(A.session_key)
        assert (state["read_through"], state["seen_through"], state["next_seq"]) == (1, 2, 4)
        assert state["version"] == "1" and state["mode"] == "waiting" and state["wake_at"] == 1070
        assert restored.judge_record(batch.id).status == "cancelled"
        assert restored.judge_record(batch.id).attempts == 1
        assert restored.db.execute("SELECT pending_due FROM judge_state").fetchone()[0] == now[0]
        assert restored.db.execute("SELECT through_seq FROM memory_resets").fetchone()[0] == 3
        assert {row[2] for row in restored.db.execute("SELECT * FROM memory_suppressions")} == {"old fact", "11"}
        assert not {"signature", "input_hash"} & {row[1] for row in restored.db.execute("PRAGMA table_info(judge_batches)")}
        assert "digest" not in {row[1] for row in restored.db.execute("PRAGMA table_info(memory_suppressions)")}
        data = restored.db.execute("SELECT input_meta FROM judge_batches").fetchone()[0]
        assert "content_hash" not in data and old_version not in data
    finally:
        restored.close()
