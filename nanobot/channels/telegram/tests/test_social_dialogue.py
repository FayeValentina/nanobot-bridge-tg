"""V2-A conversation lifecycles with a manual clock, SQLite and offline sends."""

import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.events import DeliveryReceipt
from nanobot.channels.telegram.social.models import SocialDeliveryUnknownError
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent
from nanobot.channels.telegram.tests.test_social_scheduler import (
    clock,
    context,
    finish,
    observed,
    social,
)
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


@pytest.fixture
def dialogue(social):
    social.config.dynamic_wait.enabled = True
    social.config.reply_attention.enabled = True
    social.config.sleep_observation.enabled = True
    social.config.cooldown_seconds = 0
    social.config.send_cooldown_seconds = 0
    social.configure(social.config)
    return social


def expose(runtime, *, text="我明天去台湾", turn="first", seq=1):
    runtime.store.observe(observed(runtime, seq, text=text), 500, fresh=True)
    ctx = context(runtime, turn=turn)
    view = runtime.store.view(A.session_key, count=30, chars=12000, turn_id=turn)
    runtime.store.note_seen(A.session_key, turn, encode_view(view))
    return ctx


def take(runtime):
    msg = runtime.bus.inbound.get_nowait()
    return msg, json.loads(msg.content.split("\n", 1)[1])


def admit(runtime, msg, turn="followup"):
    ctx = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                         turn_id=turn, metadata=dict(msg.metadata))
    runtime.admit_request(ctx)
    return ctx


async def test_split_sentence_waits_for_last_arrival_and_answers_once(dialogue):
    await dialogue.observe(observed(dialogue, text="@bot 我明天去台湾"), directed=True)
    clock(dialogue).advance(1)
    await dialogue.observe(observed(dialogue, 2, text="但是"), directed=False)
    clock(dialogue).advance(2)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    await dialogue.observe(observed(dialogue, 3, text="护照找不到了"), directed=False)
    clock(dialogue).advance(4.1)
    await dialogue.run_due()
    _, view = take(dialogue)
    assert [m["text"] for m in view["messages"]] == ["@bot 我明天去台湾", "但是", "护照找不到了"]
    assert view["dialogue"]["quiet"] is True
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()


async def test_continuous_messages_hit_total_cap_and_report_not_quiet(dialogue):
    await dialogue.observe(observed(dialogue, text="@bot 我跟你讲"), directed=True)
    for seq in range(2, 14):
        clock(dialogue).advance(1)
        await dialogue.observe(observed(dialogue, seq, text="然后"), directed=False)
        await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1
    _, view = take(dialogue)
    assert view["dialogue"]["quiet"] is False
    assert len(view["messages"]) == 13


async def test_finishing_another_turn_does_not_shorten_quiet_deadline(dialogue):
    await dialogue.observe(observed(dialogue, text="@bot 但是"), directed=True)
    dialogue.store.release_pending(A.session_key)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    clock(dialogue).advance(4)
    await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1


async def test_urgent_request_uses_short_path_and_duplicate_update_does_not_delay(dialogue):
    msg = observed(dialogue, text="@bot 马上回答")
    await dialogue.observe(msg, directed=True)
    clock(dialogue).advance(0.4)
    await dialogue.observe(msg, directed=True)
    clock(dialogue).advance(0.2)
    await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1


async def test_reply_continuation_survives_read_ack_and_is_consumed_once(dialogue):
    ctx = expose(dialogue)
    result = dialogue.wait(ctx, 12, purpose="reply", quiet_seconds=3)
    assert result["deferred"]
    # mark_read remains independent of waiting; use the store as the old turn has yielded.
    dialogue.store.mark_read(A.session_key, ctx.turn_id, 1)
    finish(dialogue, ctx)
    clock(dialogue).advance(3)
    await dialogue.run_due()
    msg, view = take(dialogue)
    assert view["wake_reason"] == "wait_reply"
    assert view["messages"][0]["seq"] == 1
    next_ctx = admit(dialogue, msg)
    finish(dialogue, next_ctx)
    clock(dialogue).advance(30)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()


def test_reply_already_quiet_returns_without_yielding(dialogue):
    ctx = expose(dialogue)
    clock(dialogue).advance(5)
    assert dialogue.wait(ctx, 12, purpose="reply") == {"deferred": False, "purpose": "reply", "quiet": True}
    assert not ctx.attributes["social_deferred"]
    assert dialogue.get_state(ctx)["dialogue"]["wait"] is None


async def test_messages_wait_has_distinct_quiet_and_total_timeouts(dialogue):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 60, purpose="messages", quiet_seconds=3)
    finish(dialogue, ctx)
    clock(dialogue).advance(10)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    await dialogue.observe(observed(dialogue, 2, text="下午三点"), directed=False)
    clock(dialogue).advance(2)
    await dialogue.observe(observed(dialogue, 3, text="从上海走"), directed=False)
    clock(dialogue).advance(2)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    clock(dialogue).advance(1)
    await dialogue.run_due()
    _, view = take(dialogue)
    assert view["wake_reason"] == "wait_messages" and view["dialogue"]["quiet"]


async def test_messages_arriving_between_model_input_and_wait_tool_are_not_lost(dialogue):
    ctx = expose(dialogue)
    await dialogue.observe(observed(dialogue, 2, text="下午三点"), directed=False)
    dialogue.wait(ctx, 60, purpose="messages", quiet_seconds=1)
    finish(dialogue, ctx)
    clock(dialogue).advance(1)
    await dialogue.run_due()
    _, view = take(dialogue)
    assert view["wake_reason"] == "wait_messages"
    assert any(m["seq"] == 2 for m in view["messages"])


async def test_selective_messages_wait_ignores_unmatched_chatter(dialogue):
    ctx = expose(dialogue)
    dialogue.set_wake(ctx, keywords=["出发"], senders=[], on_messages=False)
    dialogue.wait(ctx, 60, purpose="messages")
    finish(dialogue, ctx)
    await dialogue.observe(observed(dialogue, 2, text="无关聊天"), directed=False)
    clock(dialogue).advance(10)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    await dialogue.observe(observed(dialogue, 3, text="准备出发"), directed=False)
    clock(dialogue).advance(2)
    await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1
    msg, _ = take(dialogue)
    admit(dialogue, msg)
    assert dialogue.store.state(A.session_key)["wake_config"] == "{}"


async def test_reply_at_automatic_total_cap_cannot_start_another_full_wait(dialogue):
    await dialogue.observe(observed(dialogue, text="@bot 但是"), directed=True)
    for seq in range(2, 14):
        clock(dialogue).advance(1)
        await dialogue.observe(observed(dialogue, seq, text="然后"), directed=False)
    await dialogue.run_due()
    msg, view = take(dialogue)
    ctx = admit(dialogue, msg)
    dialogue.store.note_seen(A.session_key, ctx.turn_id, encode_view(view))
    result = dialogue.wait(ctx, 12, purpose="reply", quiet_seconds=4)
    assert result == {"deferred": False, "purpose": "reply", "quiet": False}


async def test_wait_consumption_and_continuation_insert_roll_back_together(dialogue):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 12, purpose="reply", quiet_seconds=3)
    finish(dialogue, ctx)
    dialogue.store.db.execute("CREATE TEMP TRIGGER reject_continuation BEFORE INSERT ON dialogue_jobs "
                              "BEGIN SELECT RAISE(ABORT, 'test transaction failure'); END")
    clock(dialogue).advance(3)
    with pytest.raises(sqlite3.IntegrityError):
        dialogue.store.consume_timers()
    assert dialogue.store.dialogue_status(A.session_key)["wait"] is not None
    assert dialogue.store.state(A.session_key)["mode"] == "waiting"
    assert not dialogue.store.db.execute("SELECT 1 FROM wakes WHERE status='scheduled'").fetchone()
    dialogue.store.db.execute("DROP TRIGGER reject_continuation")
    await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1


async def test_wait_deadline_and_new_mention_merge_into_one_directed_turn(dialogue):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 12, purpose="reply", quiet_seconds=3)
    finish(dialogue, ctx)
    clock(dialogue).advance(3)
    await dialogue.observe(observed(dialogue, 2, text="@bot 马上回答"), directed=True)
    await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1
    _, view = take(dialogue)
    assert view["wake_reason"] == "directed"


async def test_messages_timeout_cannot_rearm_without_new_human_content(dialogue):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 10, purpose="messages", on_timeout=True)
    finish(dialogue, ctx)
    clock(dialogue).advance(10)
    await dialogue.run_due()
    msg, view = take(dialogue)
    assert view["dialogue"]["quiet"] is False
    ctx = admit(dialogue, msg)
    dialogue.store.note_seen(A.session_key, ctx.turn_id, encode_view(view))
    dialogue.wait(ctx, 10, purpose="messages", on_timeout=True)
    finish(dialogue, ctx)
    clock(dialogue).advance(10)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()


@pytest.mark.parametrize("admission_delay", [0, 10])
async def test_selective_wait_ttl_limits_continuation_even_after_queueing(dialogue, admission_delay):
    ctx = expose(dialogue)
    dialogue.set_wake(ctx, keywords=["出发"], senders=[], on_messages=False, ttl_seconds=5)
    dialogue.wait(ctx, 60, purpose="messages", quiet_seconds=1)
    finish(dialogue, ctx)
    await dialogue.observe(observed(dialogue, 2, text="出发"), directed=False)
    clock(dialogue).advance(1 if admission_delay else 6)
    await dialogue.run_due()
    if admission_delay:
        msg, _ = take(dialogue)
        clock(dialogue).advance(admission_delay)
        with pytest.raises(PermissionError):
            admit(dialogue, msg)
    else:
        assert dialogue.bus.inbound.empty()
    assert dialogue.store.dialogue_status(A.session_key)["wait"] is None


@pytest.mark.parametrize("queued", [False, True])
async def test_removed_source_cannot_create_a_contextless_reply(dialogue, queued):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 12, purpose="reply", quiet_seconds=1)
    finish(dialogue, ctx)
    clock(dialogue).advance(1)
    if queued:
        await dialogue.run_due()
        msg, _ = take(dialogue)
    dialogue.store.db.execute("DELETE FROM messages")
    dialogue.store.db.commit()
    if queued:
        with pytest.raises(PermissionError, match="source"):
            admit(dialogue, msg)
    else:
        await dialogue.run_due()
        assert dialogue.bus.inbound.empty()


async def test_attention_catches_plain_answer_after_check_without_idle_model_calls(dialogue):
    ctx = expose(dialogue)
    dialogue.set_sender(AsyncMock(return_value=800))
    await dialogue.send(ctx, "你什么时候出发？", None)
    finish(dialogue, ctx)
    clock(dialogue).advance(30)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    focus = dialogue.store.dialogue_status(A.session_key)["attention"]
    assert focus["check_at"] is None
    await dialogue.observe(observed(dialogue, 2, text="下午三点"), directed=False)
    clock(dialogue).advance(4)
    await dialogue.run_due()
    msg, view = take(dialogue)
    assert view["wake_reason"] == "attention"
    ctx2 = admit(dialogue, msg)
    finish(dialogue, ctx2)
    clock(dialogue).advance(600)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()


async def test_attention_preserves_selective_conditions_and_topic_scope(dialogue):
    ctx = expose(dialogue)
    dialogue.set_sender(AsyncMock(return_value=800))
    await dialogue.send(ctx, "你什么时候出发？", None)
    dialogue.set_wake(ctx, keywords=["出发"], senders=[], on_messages=False)
    finish(dialogue, ctx)
    await dialogue.observe(observed(dialogue, 2, text="下午三点"), directed=False)
    await dialogue.observe(observed(dialogue, 3, scope=B, text="下午三点"), directed=False)
    clock(dialogue).advance(10)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()


async def test_reply_arriving_during_platform_send_is_not_lost(dialogue):
    ctx = expose(dialogue)
    async def sender(scope, text, reply_to):
        await dialogue.observe(observed(dialogue, 2, text="下午三点"), directed=False)
        return 800
    dialogue.set_sender(sender)
    await dialogue.send(ctx, "你什么时候出发？", None)
    finish(dialogue, ctx)
    clock(dialogue).advance(2)
    await dialogue.run_due()
    _, view = take(dialogue)
    assert view["wake_reason"] == "attention"
    assert any(m["text"] == "下午三点" for m in view["messages"])


async def test_unrelated_participant_does_not_trigger_attention(dialogue):
    ctx = expose(dialogue)
    dialogue.set_sender(AsyncMock(return_value=800))
    await dialogue.send(ctx, "你什么时候出发？", None)
    finish(dialogue, ctx)
    await dialogue.observe(replace(observed(dialogue, 2, text="跟别人讨论"), sender_id="456"), directed=False)
    clock(dialogue).advance(35)
    await dialogue.run_due()
    assert dialogue.bus.inbound.empty()
    await dialogue.observe(replace(observed(dialogue, 3, text="下午三点", reply_to=800), sender_id="456"), directed=False)
    clock(dialogue).advance(4)
    await dialogue.run_due()
    assert dialogue.bus.inbound.qsize() == 1


async def test_attention_survives_restart_and_does_not_repeat_idle_checks(dialogue):
    ctx = expose(dialogue)
    dialogue.set_sender(AsyncMock(return_value=800))
    await dialogue.send(ctx, "什么时候出发？", None)
    finish(dialogue, ctx)
    root, config, now, bus = dialogue.root, dialogue.config, dialogue.now, dialogue.bus
    dialogue.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        restored.store.recovery_jobs()
        now.advance(31)
        await restored.run_due()
        assert bus.inbound.empty()
        await restored.observe(observed(restored, 2, text="下午三点"), directed=False)
        now.advance(4)
        await restored.run_due()
        assert bus.inbound.qsize() == 1
    finally:
        restored.close()


async def test_send_confirmed_after_configuration_change_does_not_create_attention(dialogue):
    ctx = expose(dialogue)
    async def sender(scope, text, reply_to):
        dialogue.config.scopes[0].background = "new revision"
        dialogue.configure(dialogue.config)
        return 800
    dialogue.set_sender(sender)
    assert (await dialogue.send(ctx, "几点出发？", None))["status"] == "sent"
    assert dialogue.store.dialogue_status(A.session_key)["attention"] is None


def test_ordinary_receipt_from_old_configuration_does_not_create_attention(dialogue):
    ctx = expose(dialogue)
    delivery = dialogue.record_message_receipt(A, DeliveryReceipt(status="queued"), ctx.turn_id, [])
    dialogue.config.scopes[0].background = "new revision"
    dialogue.configure(dialogue.config)
    dialogue.record_message_receipt(A, DeliveryReceipt(status="sent", message_ids=(800,)), ctx.turn_id, [(800, "几点出发？")], delivery)
    assert dialogue.store.dialogue_status(A.session_key)["attention"] is None


@pytest.mark.parametrize("kind", ["unknown", "failed", "queued"])
async def test_only_confirmed_sends_establish_attention(dialogue, kind):
    ctx = expose(dialogue)
    error = {"unknown": SocialDeliveryUnknownError(), "failed": ValueError(), "queued": None}[kind]
    if kind == "queued":
        dialogue.record_message_receipt(A, DeliveryReceipt(status="queued"), ctx.turn_id, [])
    else:
        dialogue.set_sender(AsyncMock(side_effect=error))
        result = await dialogue.send(ctx, "你什么时候出发？", None)
        assert result["status"] == kind
    assert dialogue.store.dialogue_status(A.session_key)["attention"] is None


def test_ordinary_message_receipt_establishes_attention_once(dialogue):
    ctx = expose(dialogue)
    delivery = dialogue.record_message_receipt(A, DeliveryReceipt(status="queued"), ctx.turn_id, [])
    receipt = DeliveryReceipt(status="sent", message_ids=(800,))
    dialogue.record_message_receipt(A, receipt, ctx.turn_id, [(800, "何时出发？")], delivery)
    expires = dialogue.store.dialogue_status(A.session_key)["attention"]["expires"]
    clock(dialogue).advance(3)
    dialogue.record_message_receipt(A, receipt, ctx.turn_id, [(800, "何时出发？")], delivery)
    assert dialogue.store.dialogue_status(A.session_key)["attention"]["expires"] == expires


async def test_paced_group_establishes_one_attention_after_last_child(dialogue):
    ctx = expose(dialogue)
    dialogue.config.send_cooldown_seconds = 2
    dialogue.configure(dialogue.config)
    # Re-admit after changing configuration.
    ctx = expose(dialogue, turn="group", seq=2)
    dialogue.set_sender(AsyncMock(side_effect=[800, 801, 802]))
    await dialogue.send_parts(ctx, ["第一条", "第二条", "几点出发？"], None)
    await dialogue.run_due()
    assert dialogue.store.dialogue_status(A.session_key)["attention"] is None
    for _ in range(2):
        clock(dialogue).advance(2)
        await dialogue.run_due()
    focus = dialogue.store.dialogue_status(A.session_key)["attention"]
    assert focus is not None, [dict(row) for row in dialogue.store.db.execute("SELECT ordinal,status,error,due FROM actions")]
    assert dialogue.store.db.execute("SELECT ordinal FROM actions WHERE id=?", (focus["action_id"],)).fetchone()[0] == 2


async def test_sleep_observation_transfers_to_sleep_locally_and_mark_read_stays_pure(dialogue):
    ctx = expose(dialogue)
    assert dialogue.mark_read(ctx, 1) == 1
    result = dialogue.wait(ctx, 600, sleeping=True)
    assert result["purpose"] == "observe" and result["deferred"]
    finish(dialogue, ctx)
    clock(dialogue).advance(299)
    await dialogue.run_due()
    assert dialogue.store.state(A.session_key)["mode"] == "waiting"
    clock(dialogue).advance(1)
    await dialogue.run_due()
    state = dialogue.store.state(A.session_key)
    assert state["mode"] == "sleeping" and state["wake_at"] == dialogue.now() + 600
    assert dialogue.bus.inbound.empty() and dialogue.bus.outbound.empty()


async def test_sleep_observation_new_content_is_shown_before_sleep(dialogue):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 600, sleeping=True)
    finish(dialogue, ctx)
    clock(dialogue).advance(30)
    await dialogue.observe(observed(dialogue, 2, text="还有一件事"), directed=False)
    clock(dialogue).advance(4)
    await dialogue.run_due()
    msg, view = take(dialogue)
    assert [m["seq"] for m in view["messages"]] == [1, 2]
    assert dialogue.store.state(A.session_key)["read_through"] == 0
    admit(dialogue, msg)
    assert dialogue.store.dialogue_status(A.session_key)["wait"] is None


async def test_unseen_arrival_before_sleep_tool_gets_a_fresh_snapshot(dialogue):
    ctx = expose(dialogue)
    await dialogue.observe(observed(dialogue, 2, text="还没说完"), directed=False)
    dialogue.wait(ctx, 600, sleeping=True)
    finish(dialogue, ctx)
    await dialogue.run_due()
    _, view = take(dialogue)
    assert any(m["seq"] == 2 for m in view["messages"])
    assert dialogue.store.state(A.session_key)["seen_through"] == 1


def test_received_but_unexposed_content_never_counts_as_observed(dialogue):
    dialogue.store.observe(observed(dialogue), 500, fresh=True)
    row = dialogue.store.db.execute("SELECT observed_at,observed_seq FROM dialogue_state").fetchone()
    assert tuple(row) == (None, 0)
    assert dialogue.store.sleep_observation_due(A.session_key, 300) == dialogue.now() + 300


def test_already_quiet_conversation_does_not_wait_another_five_minutes(dialogue):
    ctx = expose(dialogue)
    # Keep an authorized test turn live while advancing the manual clock.
    dialogue.store.db.execute("UPDATE wakes SET expires=expires+600")
    clock(dialogue).advance(301)
    assert dialogue.wait(ctx, 600, sleeping=True)["mode"] == "sleeping"


async def test_model_error_does_not_leave_a_running_lock(tmp_path, dialogue):
    loop = agent(tmp_path, dialogue, [RuntimeError("offline model failure")])
    await dialogue.observe(observed(dialogue, text="@bot hello"), directed=True)
    clock(dialogue).advance(2)
    await dialogue.run_due()
    await loop._dispatch(dialogue.bus.inbound.get_nowait())
    lock = loop._session_locks.get(A.session_key)
    assert lock is None or not lock.locked()
    assert dialogue.store.dialogue_status(A.session_key)["wait"] is None
    assert dialogue.store.state(A.session_key)["mode"] == "observing"


@pytest.mark.parametrize("text", ["晚安", "再见！", "good night"])
def test_explicit_farewell_can_sleep_directly(dialogue, text):
    ctx = expose(dialogue, text=text)
    assert dialogue.wait(ctx, 600, sleeping=True)["mode"] == "sleeping"


async def test_confirmed_publication_resets_observation_evidence(dialogue):
    ctx = expose(dialogue)
    dialogue.set_sender(AsyncMock(return_value=800))
    await dialogue.send(ctx, "好的", None)
    row = dialogue.store.db.execute("SELECT observed_at,observed_seq FROM dialogue_state WHERE scope=?", (A.session_key,)).fetchone()
    assert tuple(row) == (None, 0)
    # Replaying the same old view cannot restore the pre-send observation credential.
    view = dialogue.store.db.execute("SELECT body FROM views WHERE turn_id=?", (ctx.turn_id,)).fetchone()[0]
    dialogue.store.note_seen(A.session_key, ctx.turn_id, view)
    assert dialogue.store.db.execute("SELECT observed_at FROM dialogue_state WHERE scope=?", (A.session_key,)).fetchone()[0] is None


async def test_new_send_resets_even_a_previously_quiet_sleep_credential(dialogue):
    ctx = expose(dialogue)
    dialogue.store.db.execute("UPDATE wakes SET expires=expires+600")
    clock(dialogue).advance(301)
    dialogue.set_sender(AsyncMock(return_value=800))
    await dialogue.send(ctx, "什么时候出发？", None)
    result = dialogue.wait(ctx, 600, sleeping=True)
    assert result["purpose"] == "observe" and result["total_due"] == dialogue.now() + 300


@pytest.mark.parametrize("purpose", ["reply", "messages", "observe"])
async def test_persisted_wait_survives_restart_without_duplicate_continuations(dialogue, purpose):
    ctx = expose(dialogue)
    if purpose == "observe":
        dialogue.wait(ctx, 600, sleeping=True)
    else:
        dialogue.wait(ctx, 30, purpose=purpose, on_timeout=True)
    root, config, now, bus = dialogue.root, dialogue.config, dialogue.now, dialogue.bus
    dialogue.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        restored.store.recovery_jobs()
        assert restored.bus.inbound.empty()
        clock(dialogue).advance(300 if purpose == "observe" else 30)
        await restored.run_due()
        assert restored.bus.inbound.qsize() == int(purpose != "observe")
        await restored.run_due()
        assert restored.bus.inbound.qsize() == int(purpose != "observe")
    finally:
        restored.close()


async def test_restart_preserves_automatic_quiet_deadline(dialogue):
    await dialogue.observe(observed(dialogue, text="@bot 但是"), directed=True)
    root, config, now, bus = dialogue.root, dialogue.config, dialogue.now, dialogue.bus
    dialogue.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        restored.store.recovery_jobs()
        await restored.run_due()
        assert bus.inbound.empty()
        now.advance(4)
        await restored.run_due()
        assert bus.inbound.qsize() == 1
    finally:
        restored.close()


@pytest.mark.parametrize("change", ["pause", "disable", "role"])
async def test_operator_changes_invalidate_pending_observation(dialogue, change):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 600, sleeping=True)
    finish(dialogue, ctx)
    if change == "pause":
        dialogue.config.scopes[0].paused = True
    elif change == "disable":
        dialogue.config.enabled = False
    else:
        dialogue.config.scopes[0].background = "different binding revision"
    dialogue.configure(dialogue.config)
    clock(dialogue).advance(301)
    await dialogue.run_due()
    assert dialogue.store.dialogue_status(A.session_key)["wait"] is None
    assert dialogue.bus.inbound.empty()
    assert dialogue.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


async def test_background_wait_releases_real_agent_lock(tmp_path, dialogue):
    loop = agent(tmp_path, dialogue, [LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="wait", name="social_wait", arguments={"seconds": 60, "purpose": "messages"}),
    ])])
    await dialogue.observe(observed(dialogue, text="@bot hello"), directed=True)
    clock(dialogue).advance(2)
    await dialogue.run_due()
    await loop._dispatch(dialogue.bus.inbound.get_nowait())
    lock = loop._session_locks.get(A.session_key)
    assert lock is None or not lock.locked()
    assert loop.provider.chat_stream_with_retry.await_count == 1
    assert dialogue.bus.outbound.empty()


@pytest.mark.parametrize("delta", [-100, 1000])
async def test_clock_jump_does_not_replay_old_wait_or_spin(dialogue, delta):
    ctx = expose(dialogue)
    dialogue.wait(ctx, 30, purpose="messages", on_timeout=True)
    finish(dialogue, ctx)
    clock(dialogue).advance(delta)
    for _ in range(3):
        await dialogue.run_due()
    assert dialogue.store.dialogue_status(A.session_key)["wait"] is None
    assert dialogue.bus.inbound.empty()


def test_schema_six_upgrade_preserves_data_and_unknown_versions_are_rejected(dialogue):
    ctx = expose(dialogue)
    dialogue.store.mark_read(A.session_key, ctx.turn_id, 1)
    root = dialogue.root
    for table in ("wake_samples", "dialogue_jobs", "dialogue_attention", "dialogue_waits", "dialogue_state"):
        dialogue.store.db.execute(f"DROP TABLE {table}")
    dialogue.store.db.execute("ALTER TABLE scopes DROP COLUMN wait_infinite")
    dialogue.store.db.execute("ALTER TABLE wakes DROP COLUMN source_seq")
    dialogue.store.db.execute("ALTER TABLE wakes DROP COLUMN condition_config")
    dialogue.store.db.execute("ALTER TABLE message_deliveries DROP COLUMN version")
    dialogue.store.db.execute("PRAGMA user_version=6")
    dialogue.store.db.commit()
    dialogue.close()
    upgraded = SocialStore(root, now=dialogue.now)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert upgraded.state(A.session_key)["read_through"] == 1
        assert upgraded.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        upgraded.db.execute("PRAGMA user_version=99")
    finally:
        upgraded.close()
    with pytest.raises(ValueError, match="unsupported"):
        SocialStore(root)


@pytest.mark.parametrize("section", [
    {"dynamicWait": {"quietSeconds": 4, "unfinishedSeconds": 1}},
    {"dynamicWait": {"maxSeconds": float("nan")}},
    {"replyAttention": {"checkSeconds": 300, "windowSeconds": 30}},
    {"sleepObservation": {"windowSeconds": 0}},
])
def test_dialogue_configuration_rejects_invalid_windows(section):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate(section)


def test_dialogue_config_round_trips_in_public_manifest():
    from nanobot.channels.telegram.manifest import SETUP_SPEC
    default = SETUP_SPEC.fields["social"].default
    config = TelegramSocialConfig.model_validate(default)
    assert not config.dynamic_wait.enabled and not config.reply_attention.enabled and not config.sleep_observation.enabled
    config.dynamic_wait.enabled = True
    assert TelegramSocialConfig.model_validate(config.model_dump(by_alias=True)) == config
