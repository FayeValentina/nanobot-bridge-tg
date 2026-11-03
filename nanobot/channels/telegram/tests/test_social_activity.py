"""Self-directed work scheduling, durable checkpoints and actual runner/tool delivery."""

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.channels.telegram.social.activity import ActivityRequest
from nanobot.channels.telegram.social.db.schema import SCHEMA_VERSION
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_proactive import claim, enable, tick, view
from nanobot.channels.telegram.tests.test_social_runtime import A, agent
from nanobot.channels.telegram.tests.test_social_scheduler import clock, social
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


def activity_mode(runtime):
    binding = enable(runtime)
    binding.proactive.activity_enabled = True
    binding.proactive.activity_interval_seconds = 60
    binding.proactive.activity_ttl_seconds = 300
    binding.proactive.probability = 0
    runtime.configure(runtime.config)
    return binding


def start(runtime, scope=A):
    return runtime.store.activities.apply(scope.session_key, "work-1", ActivityRequest(
        action="start", title="试做一份小谜题", goal="制作并核对一个三线索谜题，保存谜底",
        motivation="自己喜欢推理，群里也聊过谜题", next_step="写下三个线索并检查是否有唯一解",
    ))["activity"]


async def test_work_runs_without_speech_probability_or_send_budget(social):
    activity_mode(social)
    social.config.max_sends_per_hour = 1
    with social.store.db:
        social.store.db.execute("INSERT INTO send_attempts VALUES(?,?)", (A.session_key, social.now()))
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["opportunity"]["kind"] == "activity"
    assert view(inbound)["messages"] == []
    assert view(inbound)["opportunity"]["expires_at"] == social.now() + 300
    assert social.proactive.status(A.session_key)["probability"] is None
    ctx = claim(social, inbound)
    social.set_sender(AsyncMock(return_value=601))
    with pytest.raises(PermissionError):
        await social.send(ctx, "发言额度仍然有效", None)


async def test_work_still_respects_request_budget_stop_and_new_member_priority(social):
    activity_mode(social)
    social.config.max_requests_per_hour = 1
    with social.store.db:
        social.store.db.execute("INSERT INTO requests VALUES(?,?)", (A.session_key, social.now()))
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "idle:request_budget;activity:request_budget"
    with social.store.db:
        social.store.db.execute("DELETE FROM requests")
        social.store.db.execute("UPDATE proactive_state SET stopped=1 WHERE scope=?", (A.session_key,))
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "idle:stopped;activity:stopped"
    with social.store.db:
        social.store.db.execute("UPDATE proactive_state SET stopped=0 WHERE scope=?", (A.session_key,))
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    with social.store.db:
        social.store.db.execute("UPDATE scopes SET last_member_at=? WHERE key=?", (social.now(), A.session_key))
    # idleSeconds=0 in this fixture, so an in-flight judge batch is the priority guard.
    with social.store.db:
        social.store.db.execute("UPDATE judge_state SET inflight=1 WHERE scope=?", (A.session_key,))
    with pytest.raises(PermissionError, match="conversation_arrived"):
        claim(social, inbound)


async def test_all_topics_work_uses_most_recent_conversation(social):
    binding = activity_mode(social)
    binding.all_topics = True
    social.configure(social.config)
    recent = ConversationScope(A.channel, A.chat_id, 99)
    social.sync_scope(recent)
    with social.store.db:
        social.store.db.execute("UPDATE scopes SET last_member_at=? WHERE key=?", (social.now() - 10, recent.session_key))
    await tick(social)
    assert social.bus.inbound.qsize() == 1
    assert social.bus.inbound.get_nowait().session_key == recent.session_key
    assert social.proactive.status(A.session_key)["decision"] == "idle:probability_miss;activity:other_topic_active"


async def test_free_time_can_end_on_resource_budget_without_notes_or_public_output(tmp_path, social):
    binding = activity_mode(social)
    binding.proactive.activity_max_iterations = 2
    social.configure(social.config)
    loop = agent(tmp_path, social, [LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id=f"list-{index}", name="social_activity", arguments={"action": "list"})]) for index in range(2)])
    await tick(social)
    await loop._process_message(social.bus.inbound.get_nowait())
    assert loop.provider.chat_stream_with_retry.await_count == 2
    assert social.store.activities.context(A.session_key) == []
    assert social.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert social.store.db.execute("SELECT status FROM wakes").fetchone()[0] == "completed"


def test_checkpoint_scope_revision_and_restart_preserve_real_progress(social):
    activity_mode(social)
    entry = start(social)
    request = ActivityRequest(action="progress", activity_id=entry["id"], expected_revision=1,
        checkpoint="写入三条线索，手工发现还存在第二个解", next_step="加入一条排除第二个解的线索",
        artifacts=["activities/puzzle.md"])
    entry = social.store.activities.apply(A.session_key, "work-1", request)["activity"]
    with pytest.raises(ValueError, match="changed"):
        social.store.activities.apply(A.session_key, "work-2", request)
    with pytest.raises(ValueError, match="missing"):
        social.store.activities.apply("telegram:-1002:topic:42", "work-2", request)
    due = social.proactive.status(A.session_key)["next_check"]
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    try:
        assert restored.store.activities.context(A.session_key) == [entry]
        assert restored.proactive.status(A.session_key)["next_check"] == due
        assert restored.store.db.execute("SELECT COUNT(*) FROM activity_steps").fetchone()[0] == 1
    finally:
        restored.close()


def test_leisure_needs_no_goal_deliverable_or_verification_and_can_switch_interests(social):
    activity_mode(social)
    entries = social.store.activities
    first = entries.apply(A.session_key, "idle-1", ActivityRequest(action="start", title="随便逛逛"))["activity"]
    second = entries.apply(A.session_key, "idle-2", ActivityRequest(action="start", title="忽然想玩个猜谜"))["activity"]
    old = next(item for item in entries.context(A.session_key) if item["id"] == first["id"])
    assert old["status"] == "paused"
    resumed = entries.apply(A.session_key, "idle-3", ActivityRequest(action="resume", activity_id=old["id"],
        expected_revision=old["revision"]))["activity"]
    note = entries.apply(A.session_key, "idle-3", ActivityRequest(action="progress", activity_id=resumed["id"],
        expected_revision=resumed["revision"], checkpoint="看到个奇怪的梗，还没弄明白，先记着"))["activity"]
    assert note["artifacts"] == [] and note["next_step"] == ""
    finished = entries.apply(A.session_key, "idle-4", ActivityRequest(action="complete", activity_id=note["id"],
        expected_revision=note["revision"]))["activity"]
    assert finished["status"] == "completed" and finished["artifacts"] == []
    other = next(item for item in entries.context(A.session_key) if item["id"] == second["id"])
    assert other["status"] == "paused"


async def test_claimed_activity_is_not_replayed_after_restart(social):
    activity_mode(social)
    entry = start(social)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    claim(social, inbound)
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    try:
        restored.store.recovery_jobs()
        assert restored.store.activities.context(A.session_key)[0]["id"] == entry["id"]
        assert restored.store.db.execute("SELECT status FROM wakes WHERE id=?", (inbound.metadata["_social_job"],)).fetchone()[0] == "cancelled"
        assert restored.bus.inbound.empty()
    finally:
        restored.close()


@pytest.mark.parametrize("fail", [False, True])
def test_schema_sixteen_migration_preserves_data_and_rolls_back(social, monkeypatch, fail):
    db = social.store.db
    with db:
        db.execute("DROP TABLE activity_steps")
        db.execute("DROP TABLE activities")
        db.execute("PRAGMA user_version=16")
    scopes = [tuple(row) for row in db.execute("SELECT * FROM scopes")]
    social.close()
    connect = sqlite3.connect
    if fail:
        class Interrupted(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "CREATE TABLE IF NOT EXISTS activity_steps" in sql:
                    raise sqlite3.OperationalError("activity migration interrupted")
                return super().execute(sql, parameters)
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=Interrupted))
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            SocialStore(social.root, now=social.now)
        monkeypatch.setattr(sqlite3, "connect", connect)
        with connect(social.root / "social.sqlite3") as original:
            assert original.execute("PRAGMA user_version").fetchone()[0] == 16
            assert not original.execute("SELECT 1 FROM sqlite_master WHERE name='activities'").fetchone()
    restored = SocialStore(social.root, now=social.now)
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [tuple(row) for row in restored.db.execute("SELECT * FROM scopes")] == scopes
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()


async def test_real_runner_writes_artifact_checkpoints_and_shares_mid_work(tmp_path, social):
    activity_mode(social)
    entry = start(social)
    path = tmp_path / "workspace" / "activities" / "puzzle.md"
    calls = [
        ToolCallRequest(id="write", name="write_file", arguments={"path": str(path), "content": "线索一：答案是偶数\n候选：2、4\n"}),
        ToolCallRequest(id="checkpoint", name="social_activity", arguments={"action": "progress", "activity_id": entry["id"],
            "expected_revision": 1, "checkpoint": "保存首条线索，目前有两个候选解", "next_step": "增加线索排除4", "artifacts": [str(path)]}),
        ToolCallRequest(id="share", name="social_send", arguments={"text": "自己出的谜题居然能有两个答案，先把出题人拖出去。"}),
    ]
    loop = agent(tmp_path, social, [*(LLMResponse(content=None, tool_calls=[call]) for call in calls), LLMResponse(content="")])
    loop.tools.register(WriteFileTool(workspace=path.parent.parent, allowed_dir=path.parent.parent))
    loop.tools.register(ReadFileTool(workspace=path.parent.parent, allowed_dir=path.parent.parent))
    sender = AsyncMock(return_value=601)
    social.set_sender(sender)
    await tick(social)
    result = await loop._process_message(social.bus.inbound.get_nowait())
    assert result is None
    assert path.read_text().endswith("候选：2、4\n")
    checkpoint = social.store.activities.context(A.session_key)[0]
    assert checkpoint["id"] == entry["id"] and checkpoint["status"] == "active"
    assert checkpoint["next_step"] == "增加线索排除4"
    sender.assert_awaited_once()
    assert social.store.db.execute("SELECT status FROM actions").fetchone()[0] == "sent"
    initial = loop.provider.chat_stream_with_retry.call_args_list[0].kwargs["messages"]
    assert "your free time" in json.dumps(initial, ensure_ascii=False)
    assert clock(social)() < float(social.proactive.status(A.session_key)["next_check"])
