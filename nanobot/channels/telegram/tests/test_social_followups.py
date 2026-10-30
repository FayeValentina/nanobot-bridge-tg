"""V4-B intentions, linked receipts and background reply continuity."""

import json
import sqlite3
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from telegram.error import TimedOut

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.context_governance import ContextCompactionState, ContextGovernor
from nanobot.agent.hook import AgentHookContext, AgentTurnHookContext
from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
from nanobot.agent.tools.message import MessageTool
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.channels.telegram.social.models import SocialDeliveryUnknownError
from nanobot.channels.telegram.social.runtime import SOCIAL_PROTOCOL, TelegramSocialRuntime
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_judge_runtime import decide
from nanobot.channels.telegram.tests.test_social_proactive import enable, tick
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent, channel
from nanobot.channels.telegram.tests.test_social_scheduler import (
    clock,
    context,
    observed,
    remove_v4b_tables,
    social,
)
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


def own(social, **fields):
    return social.memory.apply(A.session_key, "idea-origin", MemoryRequest(
        action="record", kind="pending", origin="self", content="想了解旅行的实际体验",
        pending_thought="想听听旅行回来后最难忘的事", motivation="关心近况", **fields,
    ))["memory"]


def ordinary(turn="ordinary", scope=A):
    return RequestContext(channel="telegram", chat_id=str(scope.chat_id), session_key=scope.session_key,
                          turn_id=turn, metadata={"message_thread_id": scope.thread_id})


def edit(social, memory_id, **fields):
    return social.memory.apply(A.session_key, "editing", MemoryRequest(action="followup", memory_id=memory_id, **fields))["memory"]


def test_own_intentions_have_turn_provenance_without_inventing_member_evidence(social):
    item = own(social)
    assert item["origin"] == "self" and item["origin_turn"] == "idea-origin"
    assert item["evidence"] == [] and item["member_ids"] == []
    assert item["remaining_attempts"] == 2
    live = json.loads(social.memory.context(A.session_key))["memories"]
    assert live[0]["pending_thought"] == item["pending_thought"]
    with pytest.raises(ValidationError, match="message_ids"):
        MemoryRequest(action="record", kind="preference", content="A member loves tea")
    with pytest.raises(ValidationError, match="own intentions"):
        MemoryRequest(action="record", kind="preference", origin="self", content="A member loves tea")


def test_topic_question_keeps_member_evidence_and_marks_question_as_own_intention(social):
    social.store.observe(observed(social, text="周末去旅行"), 500)
    view = social.store.view(A.session_key, count=20, chars=10000, turn_id="source")
    from nanobot.channels.telegram.social.runtime import encode_view
    social.store.note_seen(A.session_key, "source", encode_view(view))
    item = social.memory.apply(A.session_key, "source", MemoryRequest(
        action="record", kind="topic", content="成员计划周末旅行", message_ids=[1], basis="direct"))["memory"]
    changed = edit(social, item["id"], pending_question="回来后最喜欢哪一段？", followup_at=social.now()+3600)
    assert changed["id"] == item["id"] and changed["evidence"] == item["evidence"]
    assert changed["origin"] == "evidence" and changed["origin_turn"] == "editing"
    assert changed["followup_at"] == social.now()+3600


@pytest.mark.parametrize("state", ["completed", "dropped", "responded"])
def test_finished_intentions_leave_live_suggestions_but_can_be_inspected(social, state):
    item = own(social)
    edit(social, item["id"], followup_state=state, state_reason="conversation moved on")
    assert not social.store.followups.has_pending_thought(A.session_key)
    assert item["id"] not in social.memory.context(A.session_key)
    result = social.memory.apply(A.session_key, "inspect", MemoryRequest(action="list", memory_id=item["id"], include_inactive=True))
    assert result["memories"][0]["followup_state"] == state


@pytest.mark.parametrize("action", ["delete", "clear", "expire"])
def test_retired_intentions_do_not_reappear_in_context_or_probability(social, action):
    item = own(social, ttl_seconds=2)
    if action == "expire":
        clock(social).advance(3)
    else:
        social.memory.apply(A.session_key, "retire", MemoryRequest(action=action, memory_id=item["id"] if action == "delete" else None))
    assert not social.store.followups.has_pending_thought(A.session_key)
    assert item["id"] not in social.memory.context(A.session_key)


async def test_compaction_rebuild_keeps_trusted_protocol_and_injects_only_live_intentions(tmp_path, social):
    item = own(social)
    ctx = context(social)
    persona, _ = social._persona(A)
    builder = ContextBuilder(tmp_path)
    raw, compaction = ContextCompactionState.from_transcript(
        TranscriptInput(history=[], current_message="Synthetic idle observation"),
        partial(builder.build_transcript, trusted_persona=persona, include_skills=False),
        AsyncMock(return_value="Earlier chat summary"), None,
    )
    assert compaction is not None
    raw[0]["content"] += "\nOBSOLETE_RUNTIME_MATERIAL"
    rebuilt = ContextGovernor._summary_transcript(compaction, "Earlier chat summary")
    rebuilt.append({"role": "user", "content": "Synthetic idle observation"})
    assert SOCIAL_PROTOCOL in rebuilt[0]["content"]
    assert "OBSOLETE_RUNTIME_MATERIAL" not in str(rebuilt)
    hook = social.hook_factory(AgentTurnHookContext(session_key=A.session_key))
    assert hook is not None
    iteration = AgentHookContext(iteration=0, messages=rebuilt)
    with request_context(ctx):
        await hook.before_iteration(iteration)
        assert item["pending_thought"] in str(iteration.messages)
        edit(social, item["id"], followup_state="dropped", state_reason="not relevant now")
        # Same hook/turn after a live update must withdraw the stale suggestion.
        await hook.before_iteration(iteration)
        assert item["pending_thought"] not in str(iteration.messages)
        assert SOCIAL_PROTOCOL in iteration.messages[0]["content"]


async def test_pending_thought_multiplier_is_first_and_clamped(social):
    binding = enable(social)
    binding.proactive.probability = 0.9
    binding.proactive.timezone = "Etc/GMT-12"
    social.configure(social.config)
    item = own(social)
    await tick(social)
    assert social.proactive.status(A.session_key)["probability"] == pytest.approx(1 * 0.3)
    social.store.cancel_opportunity(social.bus.inbound.get_nowait().metadata["_social_job"], "test consumed")
    social.store.db.commit()
    edit(social, item["id"], followup_state="completed")
    await tick(social)
    assert social.proactive.status(A.session_key)["probability"] == pytest.approx(0.9 * 0.3)


async def test_two_confirmed_attempts_survive_rewording_and_restart(social):
    item = own(social)
    social.set_sender(AsyncMock(side_effect=[501, 502]))
    await social.send(ordinary("attempt-one"), "旅途有什么惊喜？", None, followup_id=item["id"])
    updated = edit(social, item["id"], pending_thought="换个问法，最喜欢什么？")
    assert updated["confirmed_attempts"] == 1 and updated["remaining_attempts"] == 1
    clock(social).advance(3)
    await social.send(ordinary("attempt-two"), "最喜欢哪段旅程？", None, followup_id=item["id"])
    info = social.store.followups.describe(item["id"])
    assert info.confirmed_attempts == 2 and info.followup_state == "dropped"
    trace = social.get_state(ordinary("inspect"))["followups"][0]
    assert trace["state_reason"] == "attempt_limit"
    assert {attempt["turn_id"] for attempt in trace["attempts"]} == {"attempt-one", "attempt-two"}
    assert not social.store.followups.has_pending_thought(A.session_key)
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    restored.set_sender(AsyncMock(return_value=503))
    try:
        clock(restored).advance(3)
        with pytest.raises(PermissionError, match="dropped"):
            await restored.send(ordinary("attempt-three"), "再问一次？", None, followup_id=item["id"])
        restored._sender.assert_not_awaited()
        # A member's actual reply resumes normal conversation without proactive limits.
        edit(restored, item["id"], followup_state="responded")
        assert (await restored.send(ordinary("answer"), "听起来很棒！", None))["status"] == "sent"
        assert restored.store.followups.describe(item["id"]).confirmed_attempts == 2
    finally:
        restored.close()


async def test_failed_send_does_not_count_and_unknown_send_blocks_resending(social):
    item = own(social)
    social.set_sender(AsyncMock(side_effect=RuntimeError("rejected")))
    assert (await social.send(ordinary("failed"), "第一问", None, followup_id=item["id"]))["status"] == "failed"
    assert social.store.followups.describe(item["id"]).remaining_attempts == 2
    clock(social).advance(3)
    social.set_sender(AsyncMock(side_effect=SocialDeliveryUnknownError("maybe accepted")))
    result = await social.send(ordinary("unknown"), "第二问", None, followup_id=item["id"])
    assert result["status"] == "unknown"
    info = social.store.followups.describe(item["id"])
    assert info.confirmed_attempts == 0 and info.reserved_attempts == 1 and info.unresolved_send
    clock(social).advance(3)
    with pytest.raises(PermissionError, match="unknown send"):
        await social.send(ordinary("no-resend"), "换写法也不能重发", None, followup_id=item["id"])
    social.store.delivery(result["action_id"], "sent", message_id=501)
    social.store.delivery(result["action_id"], "sent", message_id=501)
    assert social.store.followups.describe(item["id"]).confirmed_attempts == 1


async def test_reworded_unknown_matter_stays_blocked_after_process_restart(social):
    item = own(social)
    social.set_sender(AsyncMock(side_effect=SocialDeliveryUnknownError("lost receipt")))
    result = await social.send(ordinary("before-restart"), "旅行后来怎么样？", None, followup_id=item["id"])
    changed = edit(social, item["id"], pending_question="换个说法：旅途顺利吗？")
    assert changed["id"] == item["id"] and changed["reserved_attempts"] == 1
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    sender = AsyncMock(return_value=501)
    restored.set_sender(sender)
    try:
        clock(restored).advance(3)
        with pytest.raises(PermissionError, match="unknown send"):
            await restored.send(ordinary("after-restart"), changed["pending_question"], None, followup_id=item["id"])
        sender.assert_not_awaited()
        restored.store.delivery(result["action_id"], "sent", message_id=501)
        restored.store.delivery(result["action_id"], "sent", message_id=501)
        info = restored.store.followups.describe(item["id"])
        assert info.confirmed_attempts == 1 and info.remaining_attempts == 1
    finally:
        restored.close()


async def test_two_paced_groups_count_once_each_and_second_group_finishes(social):
    item = own(social)
    sender = AsyncMock(side_effect=[501, 502, 503, 504])
    social.set_sender(sender)
    for index in range(2):
        ctx = context(social, turn=f"group-{index}")
        rows = await social.send_parts(ctx, [f"开头 {index}", f"问题 {index}"], None, followup_id=item["id"])
        assert {row["status"] for row in rows} == {"queued"}
        await social.run_due()
        clock(social).advance(3)
        await social.run_due()
        social.store.finish(ctx.turn_id, "completed")
        clock(social).advance(3)
    assert sender.await_count == 4
    assert social.store.followups.describe(item["id"]).confirmed_attempts == 2
    assert social.store.followups.describe(item["id"]).followup_state == "dropped"


@pytest.mark.parametrize("change", ["completed", "delete", "edit", "expire", "configuration"])
async def test_queued_parts_recheck_live_intention_before_sending(social, change):
    item = own(social, ttl_seconds=2 if change == "expire" else 3600)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    ctx = context(social)
    await social.send_parts(ctx, ["准备提问", "后续问题"], None, followup_id=item["id"])
    if change == "delete":
        social.memory.apply(A.session_key, "delete", MemoryRequest(action="delete", memory_id=item["id"]))
    elif change == "edit":
        edit(social, item["id"], pending_thought="已改成另一个问题")
    elif change == "expire":
        clock(social).advance(3)
    elif change == "configuration":
        social.config.scopes[0].background = "updated operator background"
        social.configure(social.config)
        assert social.store.followups.describe(item["id"]).reserved_attempts == 0
    else:
        edit(social, item["id"], followup_state="completed")
    await social.run_due()
    sender.assert_not_awaited()
    assert social.store.followups.describe(item["id"]).confirmed_attempts == 0


async def test_cannot_queue_unlinked_parts_when_followup_is_invalid(social):
    item = own(social)
    edit(social, item["id"], followup_state="dropped")
    social.set_sender(AsyncMock(return_value=501))
    with pytest.raises(PermissionError):
        await social.send_parts(context(social), ["第一条", "第二条"], None, followup_id=item["id"])
    assert social.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


async def test_ordinary_message_shares_attempts_and_preserves_regular_delivery(tmp_path, social):
    item = own(social)
    social.set_sender(AsyncMock(return_value=700))
    await social.send(ordinary("social-first"), "先主动关心一次", None, followup_id=item["id"])
    tg = channel(social)
    tg._app_ready.set()
    tg._app.bot.send_message = AsyncMock(side_effect=[SimpleNamespace(message_id=501), SimpleNamespace(message_id=502)])
    tg.bus.set_delivery_handler("telegram", tg.send_confirmed)
    tool = MessageTool.create(ToolContext(config=ToolsConfig(), workspace=str(tmp_path), bus=tg.bus))
    clock(social).advance(3)
    with request_context(ordinary("ordinary-second")):
        result = json.loads(await tool.execute(content="再关心一次旅行", followup_id=item["id"]))
    assert result["status"] == "sent"
    assert "remaining_attempts=0" in result["detail"]
    clock(social).advance(3)
    with request_context(ordinary("third")):
        rejected = json.loads(await tool.execute(content="第三次主动追问", followup_id=item["id"]))
        clock(social).advance(3)
        regular = json.loads(await tool.execute(content="普通答复"))
    assert rejected["status"] == "failed" and regular["status"] == "sent"
    assert tg._app.bot.send_message.await_count == 2
    assert social.store.followups.describe(item["id"]).confirmed_attempts == 2


async def test_partial_ordinary_unknown_counts_confirmed_prefix_and_blocks_retry(tmp_path, social):
    item = own(social)
    tg = channel(social)
    tg._app_ready.set()
    tg._app.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=501))
    tg._app.bot.send_message = AsyncMock(side_effect=TimedOut())
    tg.bus.set_delivery_handler("telegram", tg.send_confirmed)
    tool = MessageTool.create(ToolContext(config=ToolsConfig(), workspace=str(tmp_path), bus=tg.bus))
    image = tmp_path / "photo.png"
    image.write_bytes(b"test image")
    with request_context(ordinary("partial")):
        result = json.loads(await tool.execute(content="旅行问题", media=[str(image)], followup_id=item["id"]))
    assert result["status"] == "unknown" and result["message_ids"] == [501]
    info = social.store.followups.describe(item["id"])
    assert info.confirmed_attempts == 1 and info.unresolved_send
    social.store.recovery_jobs()
    assert social.store.followups.describe(item["id"]).confirmed_attempts == 1
    assert social.store.followups.describe(item["id"]).unresolved_send


async def test_main_send_registers_local_wait_then_short_reply_reaches_v3(tmp_path, social):
    social.config.coalesce_seconds = 0
    social.configure(social.config)
    item = own(social)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="ask", name="social_send", arguments={"text": "你什么时候出发？", "followup_id": item["id"]})]),
        LLMResponse(content=""),
    ])
    await social.observe(observed(social, text="@bot 明天出去玩"), directed=True)
    await loop._dispatch(social.bus.inbound.get_nowait())
    wait = social.store.dialogue_status(A.session_key)["wait"]
    assert wait["purpose"] == "messages" and wait["total_due"] == social.now()+30
    assert not wait["on_timeout"]
    provider = decide(social)
    await social.observe(observed(social, 2, text="下午三点"), directed=False)
    clock(social).advance(2)
    await social.run_due()
    task = social.judging.tasks.get(A.session_key)
    if task is not None:
        await task
    await social.run_due()
    prompt = json.loads(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert prompt["intent"]["purpose"] == "messages"
    assert prompt["followups"][0]["id"] == item["id"]
    assert any(message["is_self"] and "什么时候" in message["text"] for message in prompt["messages"])
    clock(social).advance(social.config.cooldown_seconds)
    await social.run_due()
    assert not social.bus.inbound.empty()


async def test_reply_wait_timeout_stays_local_and_other_topics_do_not_share_intentions(tmp_path, social):
    item = own(social)
    social.set_sender(AsyncMock(return_value=501))
    await social.send(ordinary("ask"), "旅行怎么样？", None, followup_id=item["id"])
    assert social.store.state(A.session_key)["mode"] == "waiting"
    clock(social).advance(31)
    await social.run_due()
    assert social.store.state(A.session_key)["mode"] == "observing"
    assert social.bus.inbound.empty()
    assert social.store.followups.summary(B.session_key) == []
    with pytest.raises(PermissionError, match="missing"):
        await social.send(ordinary("wrong-scope", B), "跨话题追问", None, followup_id=item["id"])


def test_schema_twelve_upgrade_preserves_sources(social):
    social.store.observe(observed(social), 500)
    remove_v4b_tables(social.store.db)
    social.store.db.execute("PRAGMA user_version=12")
    social.store.db.commit()
    messages = [tuple(row) for row in social.store.db.execute("SELECT * FROM messages")]
    state = social.store.state(A.session_key)
    social.close()
    restored = SocialStore(social.root, now=social.now)
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 13
        assert [tuple(row) for row in restored.db.execute("SELECT * FROM messages")] == messages
        assert restored.state(A.session_key) == state
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()


def test_schema_twelve_upgrade_failure_rolls_back(social, monkeypatch):
    remove_v4b_tables(social.store.db)
    social.store.db.execute("PRAGMA user_version=12")
    social.store.db.commit()
    social.close()
    connect = sqlite3.connect

    class InterruptedMigration(sqlite3.Connection):
        def executescript(self, sql):
            # A real SQL error after the first CREATE must roll back that table too.
            return super().executescript(sql.replace("CREATE TABLE followup_attempts", "INVALID CREATE TABLE followup_attempts"))

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=InterruptedMigration))
    with pytest.raises(sqlite3.OperationalError):
        SocialStore(social.root, now=social.now)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with connect(social.root / "social.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_followups'").fetchone() is None


async def test_future_intention_is_not_weighted_or_sent_early(social):
    item = own(social, followup_at=social.now()+3600)
    social.set_sender(AsyncMock(return_value=501))
    assert not social.store.followups.has_pending_thought(A.session_key)
    with pytest.raises(PermissionError, match="not due"):
        await social.send(ordinary("too-early"), "提前追问", None, followup_id=item["id"])
    assert social.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert social.store.followups.describe(item["id"]).remaining_attempts == 2


async def test_stop_contact_prevents_linked_proactive_send_but_allows_normal_answer(social):
    item = own(social)
    social.set_sender(AsyncMock(return_value=501))
    social.set_wake(ordinary(), keywords=[], senders=[], on_messages=False, proactive_paused=True)
    with pytest.raises(PermissionError, match="stopped"):
        await social.send(ordinary("proactive"), "追问近况", None, followup_id=item["id"])
    assert (await social.send(ordinary("answer"), "回答你的新问题", None))["status"] == "sent"
    assert social.store.followups.describe(item["id"]).confirmed_attempts == 0
