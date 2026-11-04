"""Legacy schema upgrades preserve journals and export retired judge history."""

import json
import stat

import pytest

from nanobot.channels.telegram.social.db.schema import SCHEMA_VERSION
from nanobot.channels.telegram.social.expressions import ExpressionLibrary
from nanobot.channels.telegram.social.jargon import JargonLibrary
from nanobot.channels.telegram.social.models import ObservedMessage
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_runtime import A
from nanobot.channels.telegram.tests.test_social_scheduler import remove_v4_columns


def test_schema_ten_upgrade_removes_keys_and_preserves_journals(tmp_path):
    now = [1000.]
    store = SocialStore(tmp_path, now=lambda: now[0])
    store.learning.attach_libraries(ExpressionLibrary(store), JargonLibrary(store))
    store.bind(A, "version1")
    for seq in range(1, 4):
        store.observe(ObservedMessage(A, seq, seq+10, "1", "Alice", 990.+seq, "text"), 500)
    old_version = "a" * 64  # Synthetic legacy value; no hash calculation.
    store.bind(A, old_version)
    remove_v4_columns(store.db)
    with store.db:
        store.db.execute("UPDATE judge_state SET assessed_through=0 WHERE scope=?", (A.session_key,))
        store.db.execute("INSERT INTO judge_batches(id,scope,version,judge_revision,model,prompt_version,after_seq,through_seq,created,expires,due,status,attempts,input_meta) VALUES('batch',?,?,1,'model','old',0,3,1000,1120,1000,'running',1,?)",
                         (A.session_key, old_version, json.dumps({"batch_id": "batch", "scope": A.session_key, "version": old_version,
                          "excerpts": [{"seq":1,"revision":1}]})))
        store.db.execute("INSERT INTO judge_attempts(batch_id,attempt,started) VALUES('batch',1,1000)")
    with store.db:
        store.db.execute("UPDATE scopes SET read_through=1,seen_through=2,mode='waiting',wake_at=1070 WHERE key=?", (A.session_key,))
        store.db.execute("UPDATE judge_state SET batch_id=?,pending_seq=3 WHERE scope=?", ("batch", A.session_key))
        store.db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("retired", A.session_key, "preference", " OLD   FACT ", "[]", "deleted", "direct", 1, 990., 995., None))
        store.db.execute("INSERT INTO memory_evidence VALUES(?,?,?,?,?,?)", ("retired", 11, 1, "1", 990., "original evidence"))
        store.db.execute("INSERT INTO memory_suppressions VALUES(?,?,?)", (A.session_key, "content", "opaque-legacy-value"))
        store.db.execute("ALTER TABLE memory_suppressions RENAME COLUMN value TO digest")
        store.db.execute("ALTER TABLE judge_batches DROP COLUMN judge_revision")
        store.db.execute("ALTER TABLE judge_batches ADD COLUMN signature TEXT NOT NULL DEFAULT 'old-key'")
        store.db.execute("ALTER TABLE judge_batches ADD COLUMN input_hash TEXT NOT NULL DEFAULT 'old-input'")
        store.db.execute("CREATE UNIQUE INDEX legacy_keys ON judge_batches(scope,version,signature,input_hash)")
        data = json.loads(store.db.execute("SELECT input_meta FROM judge_batches WHERE id=?", ("batch",)).fetchone()[0])
        data.pop("batch_id")
        for item in data["excerpts"]:
            item.pop("revision")
            item["content_hash"] = "legacy-content-key"
        store.db.execute("UPDATE judge_batches SET input_meta=? WHERE id=?", (json.dumps(data), "batch"))
        store.db.execute("ALTER TABLE messages DROP COLUMN revision")
        store.db.execute("DROP TABLE runtime_settings")
        store.db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
        store.db.execute("UPDATE actions SET message_id=json_extract(message_ids,'$[0]')")
        store.db.execute("ALTER TABLE actions DROP COLUMN message_ids")
        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
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
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert restored.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(row)[:-1] for row in restored.db.execute("SELECT * FROM messages ORDER BY seq")] == messages
        assert tuple(restored.db.execute("SELECT * FROM memories").fetchone()) == memory
        state = restored.scopes.state(A.session_key)
        assert (state["read_through"], state["seen_through"], state["next_seq"]) == (1, 2, 4)
        assert state["version"] == "1" and state["mode"] == "waiting" and state["wake_at"] == 1070
        assert restored.judge.through(A.session_key) == 0
        assert restored.db.execute("SELECT through_seq FROM memory_resets").fetchone()[0] == 3
        assert {row[2] for row in restored.db.execute("SELECT * FROM memory_suppressions")} == {"old fact", "11"}
        history = json.loads((tmp_path / "backups/judge-history-schema21.json").read_text())
        assert len(history["judge_batches"]) == len(history["judge_attempts"]) == 1
        assert not {"signature", "input_hash"} & history["judge_batches"][0].keys()
        assert "digest" not in {row[1] for row in restored.db.execute("PRAGMA table_info(memory_suppressions)")}
        data = history["judge_batches"][0]["input_meta"]
        assert "content_hash" not in data and old_version not in data
    finally:
        restored.close()


def test_schema_twenty_one_exports_judge_history_and_preserves_watermarks(tmp_path):
    store = SocialStore(tmp_path)
    store.bind(A, "v1")
    store.db.executescript("""
ALTER TABLE wakes DROP COLUMN admission;
ALTER TABLE judge_state ADD COLUMN pending_first REAL;
ALTER TABLE judge_state ADD COLUMN pending_last REAL;
ALTER TABLE judge_state ADD COLUMN pending_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE judge_state ADD COLUMN pending_due REAL;
ALTER TABLE judge_state ADD COLUMN pending_max REAL;
ALTER TABLE judge_state ADD COLUMN inflight INTEGER NOT NULL DEFAULT 0;
ALTER TABLE judge_state ADD COLUMN batch_id TEXT;
ALTER TABLE judge_state ADD COLUMN opportunity TEXT;
CREATE TABLE judge_batches (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES scopes(key), version TEXT NOT NULL,
    judge_revision INTEGER NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
    after_seq INTEGER NOT NULL, through_seq INTEGER NOT NULL, created REAL NOT NULL,
    expires REAL NOT NULL, due REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    decision TEXT, error_code TEXT, fallback_at REAL, input_meta TEXT, handoff_job TEXT,
    condition_config TEXT NOT NULL DEFAULT '{}');
CREATE TABLE judge_attempts (batch_id TEXT NOT NULL REFERENCES judge_batches(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL, started REAL NOT NULL, finished REAL, error_code TEXT, latency_ms INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, PRIMARY KEY(batch_id,attempt));
    """)
    with store.db:
        store.judge.set_through(A.session_key, 27)
        store.db.execute("INSERT INTO judge_batches(id,scope,version,judge_revision,model,prompt_version,after_seq,through_seq,created,expires,due,status) VALUES('old',?,'v1',1,'model','v8',0,27,1000,1120,1000,'decided')", (A.session_key,))
        store.db.execute("INSERT INTO judge_attempts(batch_id,attempt,started) VALUES('old',1,1000)")
        store.db.execute("PRAGMA user_version=21")
    store.close()
    backup = tmp_path / "backups/judge-history-schema21.json"
    backup.parent.mkdir(mode=0o700)
    backup.write_text("overwrite me")
    restored = SocialStore(tmp_path)
    try:
        history = json.loads(backup.read_text())
        assert {key:len(value) for key,value in history.items()} == {"judge_batches":1, "judge_attempts":1}
        assert history["judge_batches"][0]["through_seq"] == 27
        assert restored.db.execute("SELECT name FROM sqlite_master WHERE name IN ('judge_batches','judge_attempts')").fetchall() == []
        assert [row[1] for row in restored.db.execute("PRAGMA table_info(judge_state)")] == ["scope", "assessed_through"]
        assert restored.judge.through(A.session_key) == 27
        assert "admission" in [row[1] for row in restored.db.execute("PRAGMA table_info(wakes)")]
        assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 22
    finally:
        restored.close()
