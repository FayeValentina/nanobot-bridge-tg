"""V5-A data isolation, durable restart, source lifecycle and jargon regressions."""
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pydantic import ValidationError

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.db.schema import SCHEMA_VERSION
from nanobot.channels.telegram.social.expressions import ExpressionLibrary
from nanobot.channels.telegram.social.jargon import JargonLibrary, JargonRequest
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialLearningConfig
from nanobot.providers.base import LLMProvider, LLMResponse

A = ConversationScope("telegram", -1001, 42)
B = ConversationScope("telegram", -1002, 42)


@pytest.fixture
def store(tmp_path):
    instance = SocialStore(tmp_path, now=lambda: 1000.)
    instance.learning.attach_libraries(ExpressionLibrary(instance), JargonLibrary(instance))
    instance.bind(A, "1")
    instance.bind(B, "1")
    instance.learning.configure(TelegramSocialLearningConfig(min_messages=2))
    yield instance
    if not instance.closed:
        instance.close()


def observe(store, message_id=1, **kwargs):
    message = ObservedMessage(A, message_id, message_id, "human-1", "Member", 1000.,
                              kwargs.pop("text", f"闲聊 {message_id}"), learning_text=f"闲聊 {message_id}")
    message = replace(message, **kwargs)
    store.observe(message, 100)
    return message


def batches(store):
    observe(store, 1)
    observe(store, 2)
    return store.learning.prepare(A.session_key, "expression"), store.learning.prepare(A.session_key, "jargon")


def test_collection_without_main_exposure_and_independent_success_cursors(store):
    before = store.scopes.state(A.session_key)
    expression, jargon = batches(store)
    assert expression and jargon
    assert store.db.execute("SELECT COUNT(*) FROM views").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM wakes").fetchone()[0] == 0
    store.learning.finish(expression, success=True, result_count=0)
    cursors = {row["kind"]: dict(row) for row in store.db.execute("SELECT * FROM learning_cursors WHERE scope=?", (A.session_key,))}
    assert cursors["expression"]["message_id"] == 2 and cursors["expression"]["revision"] == 1
    assert cursors["jargon"]["event_id"] == 0
    assert store.judge.through(A.session_key) == 0
    assert store.scopes.state(A.session_key)["seen_through"] == before["seen_through"] == 0
    assert store.scopes.state(A.session_key)["read_through"] == before["read_through"] == 0
    assert store.learning.status(B.session_key)["buffered_materials"] == 0


@pytest.mark.parametrize("kwargs,reason", [
    ({"is_self": True}, "self"), ({"is_bot": True}, "bot"),
    ({"is_forwarded": True}, "forwarded_or_via_bot"),
    ({"learning_text": None}, "unknown_or_nontext"), ({"learning_text": " "}, "nontext"),
    ({"timestamp": 100.}, "stale_event"),
])
def test_nonhuman_nontext_and_stale_events_are_not_templates(store, kwargs, reason):
    observe(store, **kwargs)
    assert store.learning.status(A.session_key)["buffered_materials"] == 0
    assert store.db.execute("SELECT count FROM learning_counts WHERE reason=?", (reason,)).fetchone()[0] == 1


def test_edit_cancels_frozen_batch_and_keeps_new_revision(store):
    expression, _ = batches(store)
    observe(store, 3)  # Arrived while old batch was pending.
    observe(store, 1, update_id=101, is_edit=True, text="改过的闲聊", learning_text="改过的闲聊")
    assert store.db.execute("SELECT status FROM learning_batches WHERE id=?", (expression,)).fetchone()[0] == "cancelled"
    with pytest.raises(ValueError, match="no longer active"):
        store.learning.finish(expression, success=True)
    replacement = store.learning.prepare(A.session_key, "expression")
    sources = json.loads(store.db.execute("SELECT sources FROM learning_batches WHERE id=?", (replacement,)).fetchone()[0])
    assert {(item["message_id"], item["revision"]) for item in sources} == {(1, 2), (2, 1), (3, 1)}
    assert store.scopes.state(A.session_key)["seen_through"] == store.scopes.state(A.session_key)["read_through"] == 0


def test_restart_resumes_success_cursor_and_retains_pending_batch(store, tmp_path):
    expression, jargon = batches(store)
    store.learning.finish(expression, success=True)
    observe(store, 3)
    observe(store, 4)
    store.close()
    restored = SocialStore(tmp_path, now=lambda: 1400.)
    try:
        restored.learning.configure(TelegramSocialLearningConfig(min_messages=2))
        restored.learning.recover()
        assert restored.learning.prepare(A.session_key, "jargon") == jargon
        new = restored.learning.prepare(A.session_key, "expression")
        sources = json.loads(restored.db.execute("SELECT sources FROM learning_batches WHERE id=?", (new,)).fetchone()[0])
        assert [item["message_id"] for item in sources] == [3, 4]
    finally:
        restored.close()


def test_failure_keeps_cursor_and_attempt_budget_survives_restart(store, tmp_path):
    expression, _ = batches(store)
    store.learning.finish(expression, success=False, reason="timeout")
    assert store.db.execute("SELECT event_id FROM learning_cursors WHERE kind='expression' AND scope=?", (A.session_key,)).fetchone()[0] == 0
    store.close()
    restored = SocialStore(tmp_path, now=lambda: 1001.)
    try:
        restored.learning.configure(TelegramSocialLearningConfig(min_messages=2))
        restored.learning.recover()
        assert restored.learning.prepare(A.session_key, "expression") == expression
        restored.learning.finish(expression, success=False, reason="timeout")
        with pytest.raises(ValueError, match="budget exhausted"):
            restored.learning.finish(expression, success=True)
    finally:
        restored.close()


def test_atomic_result_and_cursor_rollback(store):
    expression, _ = batches(store)
    with pytest.raises(RuntimeError):
        with store.db:
            store.db.execute("INSERT INTO expressions(id,scope,situation,expression,created,updated) VALUES('new',?,'s','e',1000,1000)", (A.session_key,))
            store.learning.finish(expression, success=True, result_count=1)
            raise RuntimeError("crash before commit")
    assert store.db.execute("SELECT COUNT(*) FROM expressions").fetchone()[0] == 0
    assert store.db.execute("SELECT status FROM learning_batches WHERE id=?", (expression,)).fetchone()[0] == "ready"
    assert store.db.execute("SELECT MAX(event_id) FROM learning_cursors").fetchone()[0] == 0


def test_window_overflow_is_bounded_and_frozen_sources_survive_retention(store):
    store.learning.configure(TelegramSocialLearningConfig(window_messages=10, min_messages=2))
    expression, _ = batches(store)
    for message_id in range(3, 115):
        observe(store, message_id)
    status = store.learning.status(A.session_key)
    assert status["buffered_materials"] == 10
    assert {row["reason"] for row in status["counts"]} == {"unbatched_overflow:expression", "unbatched_overflow:jargon"}
    assert store.db.execute("SELECT 1 FROM messages WHERE message_id=1").fetchone() is None
    store.learning.finish(expression, success=True)
    assert store.scopes.state(A.session_key)["seen_through"] == 0


def test_config_pause_and_scope_revocation_do_not_consume_cursor(store):
    expression, _ = batches(store)
    store.learning.configure(TelegramSocialLearningConfig(enabled=False, min_messages=2))
    assert store.db.execute("SELECT status FROM learning_batches WHERE id=?", (expression,)).fetchone()[0] == "cancelled"
    assert store.learning.prepare(A.session_key, "expression") is None
    store.learning.configure(TelegramSocialLearningConfig(min_messages=2))
    store.pause(A.session_key)
    assert store.learning.prepare(A.session_key, "expression") is None
    store.learning.collect_due([])
    assert store.db.execute("SELECT MAX(event_id) FROM learning_cursors").fetchone()[0] == 0


def jargon_record(store):
    observe(store, 1)
    view = store.view(A.session_key, count=30, chars=12000, turn_id="turn")
    store.note_seen(A.session_key, "turn", encode_view(view))
    return JargonLibrary(store).apply(A.session_key, "turn", JargonRequest(
        action="submit", term="闲聊", message_ids=[1]))["jargon"]


def test_jargon_edit_disable_and_no_legacy_rows(store):
    entry = jargon_record(store)
    assert store.db.execute("SELECT COUNT(*) FROM memories WHERE kind='slang'").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM jargon").fetchone()[0] == 1
    assert store.db.execute("SELECT revision,source FROM jargon_evidence").fetchone()[:] == (1, "human_text")
    assert JargonLibrary(store).apply(A.session_key, "turn", JargonRequest(action="list"))["jargon"][0]["id"] == entry["id"]
    observe(store, 1, update_id=101, is_edit=True, text="新闲聊", learning_text="新闲聊")
    assert JargonLibrary(store).apply(A.session_key, "turn", JargonRequest(action="list"))["jargon"] == []
    assert store.db.execute("SELECT status,revision FROM jargon").fetchone()[:] == ("disabled", 2)
    JargonLibrary(store).apply(A.session_key, "turn", JargonRequest(action="disable", entry_id=entry["id"], expected_revision=2))
    assert store.db.execute("SELECT status FROM jargon").fetchone()[0] == "disabled"


def test_slang_rejects_bot_and_stale_view_sources(store):
    observe(store, 1, is_bot=True)
    view = store.view(A.session_key, count=30, chars=12000, turn_id="turn")
    store.note_seen(A.session_key, "turn", encode_view(view))
    request = JargonRequest(action="submit", term="闲聊", message_ids=[1])
    with pytest.raises(ValueError, match="human top-level"):
        JargonLibrary(store).apply(A.session_key, "turn", request)
    observe(store, 1, update_id=101, is_edit=True)
    with pytest.raises(ValueError, match="human top-level"):
        JargonLibrary(store).apply(A.session_key, "turn", request)


def test_selection_details_ring_and_sqlite_only_selected_ids(store):
    with store.db:
        store.db.execute("INSERT INTO expressions(id,scope,situation,expression,revision,created,updated) VALUES('chosen',?,'s','e',2,1000,1000)", (A.session_key,))
    for index in range(30):
        store.learning.record_selection(A.session_key, f"turn-{index}", target_message_id=index,
                                        candidates=13, selected=[("expression", "chosen", 2)],
                                        explanation={"rejected": [{"id": "not-chosen", "reason": "unrelated"}]})
    status = store.learning.status(A.session_key)
    assert len(status["recent_selections"]) == 25
    assert status["recent_selections"][0]["turn_id"] == "turn-5"
    assert store.learning.status(B.session_key)["recent_selections"] == []
    assert store.db.execute("SELECT COUNT(*) FROM expression_selections").fetchone()[0] == 30
    assert store.db.execute("SELECT calls,candidate_count FROM expression_turns LIMIT 1").fetchone()[:] == (1, 13)
    assert "not-chosen" not in " ".join(store.db.iterdump())


def test_config_contract_limits_aliases_and_independent_model_slots():
    config = TelegramSocialLearningConfig.model_validate({"expression": {"model": "learner"}, "selection": {"provider": "local"}})
    wire = config.model_dump(by_alias=True)
    assert wire["intervalSeconds"] == 300 and wire["selectionHistoryTurns"] == 25
    assert config.jargon.model is None
    for values in ({"concurrency": 2}, {"selectionHistoryTurns": 100}, {"minMessages": 30, "windowMessages": 10},
                   {"expression": {"maxRetries": 2}}, {"intervalSeconds": float("nan")}, {"leaseSeconds": 20}):
        with pytest.raises(ValidationError):
            TelegramSocialLearningConfig.model_validate(values)


async def test_runtime_collects_when_main_silent_and_learning_config_preserves_v3_version(tmp_path):
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "guest.md").write_text("Guest")
    config = TelegramSocialConfig.model_validate({"enabled": True, "learning": {"minMessages": 2},
                    "scopes": [{"chatId": A.chat_id, "threadId": A.thread_id, "roleId": "guest"}]})
    runtime = TelegramSocialRuntime(config, tmp_path, MessageBus(), now=lambda: 1000.)
    try:
        version = runtime.store.scopes.state(A.session_key)["version"]
        for message_id in (1, 2):
            await runtime.observe(ObservedMessage(A, message_id, message_id, "human", "Member", 1000.,
                                                  "闲聊", learning_text="闲聊"), directed=False)
        await runtime.run_due()
        assert runtime.store.learning.status(A.session_key)["batches"][0]["status"] == "ready"
        assert runtime.store.scopes.state(A.session_key)["seen_through"] == runtime.store.scopes.state(A.session_key)["read_through"] == 0
        config.learning.expression.model = "future-learner"
        runtime.configure(config)
        assert runtime.store.scopes.state(A.session_key)["version"] == version
    finally:
        runtime.close()


def test_upgrade_schema_thirteen_preserves_all_existing_source_values(store, tmp_path):
    observe(store)
    before = {table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table}")]
              for table in ("messages", "scopes", "judge_state", "memories", "memory_evidence", "stickers")}
    # Reconstruct the actual old layout, rather than merely changing user_version.
    with store.db:
        store.db.execute("ALTER TABLE messages DROP COLUMN learning_source")
        store.db.execute("ALTER TABLE messages DROP COLUMN learning_text")
        tables = [row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0].startswith(("learning_", "expression", "jargon"))]
        for table in sorted(tables, key=lambda name: name in {"expressions", "jargon", "expression_turns"}):
            store.db.execute(f"DROP TABLE {table}")
        store.db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
        store.db.execute("UPDATE actions SET message_id=json_extract(message_ids,'$[0]')")
        store.db.execute("ALTER TABLE actions DROP COLUMN message_ids")
        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
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
        store.db.execute("PRAGMA user_version=13")
    store.close()
    restored = SocialStore(tmp_path, now=lambda: 1000.)
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        for table, values in before.items():
            actual = [tuple(row) for row in restored.db.execute(f"SELECT * FROM {table}")]
            if table == "messages":
                assert [row[:-2] for row in actual] == [row[:-2] for row in values]
                assert actual[0][-2:] == ("unknown", "")
            else:
                assert actual == values
        assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()


def test_real_chat_fixture_has_disjoint_sources_and_no_self_templates():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "social_v5_baseline.json").read_text())
    extraction = {(item["scope"], message["message_id"]) for item in fixture["extraction"] for message in item["messages"]}
    evaluation = {(item["scope"], message["message_id"]) for item in fixture["evaluation"] for message in item["messages"]}
    assert extraction and evaluation and not extraction & evaluation
    for item in fixture["extraction"]:
        for message in item["messages"]:
            if message["learnable"]:
                assert message["source"] == "reviewed_human_text" and message["text"] != "[non-text message]"
    assert len(fixture["evaluation"]) >= 8


def test_collector_failure_does_not_lose_main_observation(store):
    with store.db:
        store.db.execute("CREATE TRIGGER fail_capture BEFORE INSERT ON learning_events BEGIN SELECT RAISE(ABORT,'capture fault'); END")
    observe(store)
    assert store.db.execute("SELECT text FROM messages WHERE message_id=1").fetchone()[0] == "闲聊 1"
    assert store.db.execute("SELECT count FROM learning_counts WHERE reason='capture_error'").fetchone()[0] == 1
    assert store.scopes.state(A.session_key)["seen_through"] == store.scopes.state(A.session_key)["read_through"] == 0


def test_interrupted_attempt_is_charged_before_restart(store, tmp_path):
    expression, _ = batches(store)
    store.learning.start(expression)
    store.close()
    restored = SocialStore(tmp_path, now=lambda: 1001.)
    try:
        restored.learning.configure(TelegramSocialLearningConfig(min_messages=2))
        restored.learning.recover()
        assert restored.db.execute("SELECT status,attempts FROM learning_batches WHERE id=?", (expression,)).fetchone()[:] == ("ready", 1)
        restored.learning.start(expression)
        restored.learning.recover()
        assert restored.db.execute("SELECT status,attempts FROM learning_batches WHERE id=?", (expression,)).fetchone()[:] == ("failed", 2)
        with pytest.raises(ValueError, match="budget exhausted"):
            restored.learning.start(expression)
    finally:
        restored.close()


@pytest.mark.parametrize("ordinary", [False, True])
@pytest.mark.parametrize("status,ids,confirmed", [("queued", [], False), ("unknown", [], False), ("failed", [], False), ("sent", [123], True)])
def test_usage_requires_real_receipt_and_keeps_human_occurrences_unchanged(store, ordinary, status, ids, confirmed):
    with store.db:
        store.db.execute("INSERT INTO expressions(id,scope,situation,expression,revision,created,updated) VALUES('chosen',?,'s','e',2,1000,1000)", (A.session_key,))
        store.db.execute("INSERT INTO actions(id,scope,version,content,status,created,turn_id,message_ids,kind) VALUES('delivery',?,'1','text',?,1000,'turn',?,?)", (A.session_key, "sending" if ordinary and status == "queued" else status, json.dumps(ids), "message" if ordinary else "text"))
    store.learning.record_selection(A.session_key, "turn", target_message_id=1, candidates=1,
                                    selected=[("expression", "chosen", 2)], explanation={})
    if not confirmed:
        with pytest.raises(ValueError, match="confirmed delivery"):
            store.learning.confirm_use(A.session_key, "turn", "expression", "chosen", 2, "delivery")
    else:
        store.learning.confirm_use(A.session_key, "turn", "expression", "chosen", 2, "delivery")
    assert store.db.execute("SELECT occurrences FROM expressions").fetchone()[0] == 0
    assert store.db.execute("SELECT delivery_id FROM expression_selections").fetchone()[0] == ("delivery" if confirmed else None)


def test_coarse_filter_uses_scene_index(store):
    for table in ("expressions", "jargon"):
        plan = store.db.execute(f"EXPLAIN QUERY PLAN SELECT id FROM {table} WHERE scope=? AND status=? AND intent_category=? ORDER BY updated", (A.session_key, "candidate", "反讽")).fetchall()
        assert any(f"{table}_scene" in row[3] for row in plan)


def test_late_edit_after_observation_and_material_eviction_keeps_monotonic_revision(store):
    entry = jargon_record(store)
    observe(store, 2)
    batch = store.learning.prepare(A.session_key, "expression")
    for message_id in range(3, 115):
        observe(store, message_id)
    assert store.db.execute("SELECT 1 FROM messages WHERE message_id=1").fetchone() is None
    assert store.db.execute("SELECT 1 FROM learning_events WHERE message_id=1").fetchone() is None
    observe(store, 1, update_id=1001, is_edit=True, text="晚来的编辑", learning_text="晚来的编辑")
    assert store.db.execute("SELECT revision FROM messages WHERE message_id=1").fetchone()[0] == 2
    assert store.db.execute("SELECT status FROM learning_batches WHERE id=?", (batch,)).fetchone()[0] == "cancelled"
    assert store.db.execute("SELECT status FROM jargon WHERE id=?", (entry["id"],)).fetchone()[0] == "disabled"
    assert store.db.execute("SELECT revision FROM learning_events WHERE message_id=1").fetchone()[0] == 2


@pytest.mark.parametrize("kind", ["expression", "jargon"])
@pytest.mark.parametrize("exception,code", [
    (RuntimeError, "provider_exception"), (ValueError, "invalid_or_changed_input"),
    (TimeoutError, "timeout"),
])
async def test_worker_error_detail_is_redacted_and_cleared_on_success(tmp_path, kind, exception, code):
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "guest.md").write_text("Guest")
    config = TelegramSocialConfig.model_validate({"enabled": True, "learning": {"minMessages": 2},
        "scopes": [{"chatId": A.chat_id, "threadId": A.thread_id, "roleId": "guest"}]})
    runtime = TelegramSocialRuntime(config, tmp_path, MessageBus(), now=lambda: 1000.)
    try:
        worker = runtime.expressions if kind == "expression" else runtime.jargon
        provider = AsyncMock(spec=LLMProvider)
        provider.chat.side_effect = exception(
            "boom sk-abc123 Bearer secret-auth api_key=secret-key token=secret-token " + "x" * 400)
        worker.set_provider(provider, "test-learner")
        observe(runtime.store, 1)
        observe(runtime.store, 2)
        batch = runtime.store.learning.prepare(A.session_key, kind)
        assert batch
        records = []
        sink = logger.add(lambda message: records.append(message.record))
        try:
            await runtime.learning._run(worker, batch)
        finally:
            logger.remove(sink)
        record = next(r for r in records if f"batch={batch} kind={kind} error={code}" in r["message"])
        assert record["level"].name == "WARNING"
        assert (record["exception"] is not None) == (exception is not TimeoutError)
        row = runtime.store.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()
        assert row["status"] == "failed" and row["reason"] == code and row["attempts"] == 1
        assert runtime.store.db.execute(
            "SELECT count FROM learning_counts WHERE scope=? AND reason=?",
            (A.session_key, f"{kind}:failed")).fetchone()[0] == 1
        detail = row["error_detail"]
        assert detail.startswith(f"{exception.__name__}: boom") and len(detail) == 300
        assert all(secret not in detail for secret in ("sk-abc123", "secret-auth", "secret-key", "secret-token"))
        state_detail = runtime.store.learning.status(A.session_key)["batches"][0]["error_detail"]
        assert state_detail == detail[:120]
        cursor = runtime.store.db.execute("SELECT event_id FROM learning_cursors WHERE scope=? AND kind=?",
                                          (A.session_key, kind)).fetchone()[0]
        assert cursor == 0
        provider.chat.side_effect = None
        provider.chat.return_value = LLMResponse(content=json.dumps({"expressions" if kind == "expression" else "jargon": []}))
        await runtime.learning._run(worker, batch)
        row = runtime.store.db.execute("SELECT status,error_detail,attempts FROM learning_batches WHERE id=?", (batch,)).fetchone()
        assert tuple(row) == ("succeeded", None, 2)
        assert runtime.store.learning.status(A.session_key)["batches"][0]["error_detail"] is None
    finally:
        runtime.close()


@pytest.mark.parametrize("column_exists", [False, True])
def test_upgrade_seventeen_retains_learning_batches_and_adds_nullable_detail(store, tmp_path, column_exists):
    batches(store)
    columns = [row[1] for row in store.db.execute("PRAGMA table_info(learning_batches)") if row[1] != "error_detail"]
    before = [tuple(row) for row in store.db.execute(f"SELECT {','.join(columns)} FROM learning_batches ORDER BY id")]
    with store.db:
        if not column_exists:
            store.db.execute("ALTER TABLE learning_batches DROP COLUMN error_detail")
        store.db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
        store.db.execute("UPDATE actions SET message_id=json_extract(message_ids,'$[0]')")
        store.db.execute("ALTER TABLE actions DROP COLUMN message_ids")
        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
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
        store.db.execute("PRAGMA user_version=17")
    store.close()
    restored = SocialStore(tmp_path, now=lambda: 1000.)
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [tuple(row) for row in restored.db.execute(f"SELECT {','.join(columns)} FROM learning_batches ORDER BY id")] == before
        assert [row[0] for row in restored.db.execute("SELECT error_detail FROM learning_batches")] == [None, None]
        assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()
