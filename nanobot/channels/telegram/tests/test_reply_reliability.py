"""Observed reply omissions, selective waiting, exposure and actual platform receipts."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

from nanobot.agent.tools.context import RequestContext, ToolContext, request_context
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent, channel, message
from nanobot.channels.telegram.tests.test_social_scheduler import clock, context, observed, social
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


async def wake(social):
    await social.observe(observed(social, text="@bot hello"), directed=True)
    return social.bus.inbound.get_nowait()


async def test_unsent_answer_gets_one_delivery_check_and_explicit_send(tmp_path, social):
    social.config.repair_unsent_final = True
    social.config.coalesce_seconds = 0
    sender = AsyncMock(return_value=800)
    social.set_sender(sender)
    loop = agent(tmp_path, social, [
        LLMResponse(content="A public answer the model forgot to publish"),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="send", name="social_send", arguments={"text": "published answer"})]),
        LLMResponse(content="internal final"),
    ])
    await loop._dispatch(await wake(social))
    sender.assert_awaited_once_with(A, "published answer", None)
    assert loop.provider.chat_stream_with_retry.await_count == 3
    assert "Runtime delivery check" in str(loop.provider.chat_stream_with_retry.call_args_list[1].kwargs["messages"])
    assert social.store.state(A.session_key)["read_through"] == 1
    assert social.bus.outbound.empty()


@pytest.mark.parametrize("repair_result", ["", "still internal"])
async def test_delivery_check_never_publishes_raw_text_and_is_bounded(tmp_path, social, repair_result):
    social.config.repair_unsent_final = True
    social.config.coalesce_seconds = 0
    sender = AsyncMock(return_value=800)
    social.set_sender(sender)
    loop = agent(tmp_path, social, [LLMResponse(content="internal note"), LLMResponse(content=repair_result)])
    await loop._dispatch(await wake(social))
    assert loop.provider.chat_stream_with_retry.await_count == 2
    sender.assert_not_awaited()
    assert social.bus.outbound.empty()
    status = social.store.db.execute("SELECT status FROM wakes ORDER BY created DESC LIMIT 1").fetchone()[0]
    assert status == ("unsent" if repair_result else "completed")


async def test_explicit_silence_never_gets_a_delivery_check(tmp_path, social):
    social.config.repair_unsent_final = True
    social.config.coalesce_seconds = 0
    loop = agent(tmp_path, social, [LLMResponse(content="")])
    await loop._dispatch(await wake(social))
    assert loop.provider.chat_stream_with_retry.await_count == 1
    assert social.store.state(A.session_key)["read_through"] == 1


async def test_conditional_wait_filters_numbers_and_consumes_keyword_once(social):
    ctx = context(social)
    social.set_wake(ctx, keywords=["5", "五"], senders=[], on_messages=False)
    social.wait(ctx, 3600)
    social.store.finish(ctx.turn_id, "completed")
    for i, text in enumerate(["1", "2", "unrelated"], 1):
        await social.observe(observed(social, i, text=text), directed=False)
        clock(social).advance(11)
        await social.run_due()
        assert social.bus.inbound.empty()
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1  # setup only
    await social.observe(observed(social, 4, text="5"), directed=False)
    clock(social).advance(2)
    await social.run_due()
    inbound = social.bus.inbound.get_nowait()
    assert json.loads(inbound.content.split("\n", 1)[1])["wake_reason"] == "judge"
    # A delayed candidate must not consume the condition before model admission.
    assert json.loads(social.store.state(A.session_key)["wake_config"])["keywords"] == ["5", "五"]
    social.admit_request(RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                                       turn_id="consume-keyword", metadata=dict(inbound.metadata)))
    assert social.store.state(A.session_key)["wake_config"] == "{}"
    assert social.store.state(B.session_key)["wake_config"] == "{}"


async def test_conditional_wait_direct_mention_interrupts_without_consuming_condition(social):
    ctx = context(social)
    social.set_wake(ctx, keywords=["5"], senders=[], on_messages=False)
    social.wait(ctx, 3600)
    social.store.finish(ctx.turn_id, "completed")
    await social.observe(observed(social, text="@bot help"), directed=True)
    clock(social).advance(11)
    await social.run_due()
    assert social.bus.inbound.qsize() == 1
    assert json.loads(social.store.state(A.session_key)["wake_config"])["keywords"] == ["5"]


async def test_condition_deadline_survives_restart_and_expiry_clears_it(social):
    ctx = context(social)
    social.set_wake(ctx, keywords=["5"], senders=[], on_messages=False, ttl_seconds=5, one_shot=False)
    social.wait(ctx, 100)
    social.store.finish(ctx.turn_id, "completed")
    root, config, bus, now = social.root, social.config, social.bus, social.now
    social.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        assert json.loads(restored.store.state(A.session_key)["wake_config"])["expires_at"] == now() + 5
        clock(social).advance(6)
        await restored.observe(replace(observed(social), text="ordinary after expiry"), directed=False)
        assert restored.store.state(A.session_key)["wake_config"] == "{}"
    finally:
        restored.close()


async def test_turn_ack_does_not_ack_new_arrivals_during_send(tmp_path, social):
    social.config.coalesce_seconds = 0
    async def sender(scope, text, reply_to):
        social.store.observe(message(A, seq=2, text="arrived after provider exposure"), 500)
        return 800
    social.set_sender(sender)
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="send", name="social_send", arguments={"text": "answer"})]),
        LLMResponse(content="internal"),
    ])
    await loop._dispatch(await wake(social))
    assert social.store.state(A.session_key)["read_through"] == 1
    assert social.store.state(A.session_key)["next_seq"] == 4


def test_auto_ack_stops_at_a_gap_in_actual_exposure(social):
    for seq in range(1, 4):
        social.store.observe(observed(social, seq), 500)
    view = social.store.view(A.session_key, count=2, chars=4000, turn_id="reader", trigger_seq=3)
    assert [m["seq"] for m in view["messages"]] == [1, 3]
    social.store.note_seen(A.session_key, "reader", encode_view(view))
    assert social.store.acknowledge_seen(A.session_key, "reader") == 1


@pytest.mark.parametrize("attachment", [False, True])
async def test_ordinary_message_gets_platform_ids_and_a_durable_receipt(tmp_path, social, attachment):
    tg = channel(social)
    tg._app_ready.set()
    tg._app.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=802))
    tg.bus.set_delivery_handler("telegram", tg.send_confirmed)
    tool = MessageTool.create(ToolContext(config=ToolsConfig(), workspace=str(tmp_path), bus=tg.bus))
    kwargs = {"content": "ordinary public answer"}
    if attachment:
        path = tmp_path / "image.png"
        path.write_bytes(b"offline-image")
        kwargs["media"] = [str(path)]
    with request_context(RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                                       turn_id="normal-send", metadata={"message_thread_id": A.thread_id})):
        result = json.loads(await tool.execute(**kwargs))
    assert result["status"] == "sent"
    assert result["message_ids"] == ([802, 501] if attachment else [501])
    assert tg.bus.outbound.empty()
    row = social.store.db.execute("SELECT status,message_ids,turn_id FROM message_deliveries").fetchone()
    assert row[0] == "sent" and json.loads(row[1]) == result["message_ids"] and row[2] == "normal-send"
    assert social.store.db.execute("SELECT COUNT(*) FROM send_attempts").fetchone()[0] == 0
    assert social.store.db.execute("SELECT text FROM messages WHERE is_self=1").fetchone()[0] == "ordinary public answer"
    assert tg._app.bot.send_message.call_args.kwargs["message_thread_id"] == A.thread_id


async def test_ordinary_timeout_is_unknown_and_never_retried(social):
    tg = channel(social)
    tg._app_ready.set()
    tg._app.bot.send_message.side_effect = TimedOut("response lost")
    receipt = await tg.send_confirmed(OutboundMessage("telegram", str(A.chat_id), "one attempt", metadata={"message_thread_id": A.thread_id}))
    assert receipt.status == "unknown" and receipt.message_ids == ()
    assert tg._app.bot.send_message.await_count == 1
    assert social.store.db.execute("SELECT status FROM message_deliveries").fetchone()[0] == "unknown"


async def test_partial_ordinary_send_reports_confirmed_prefix(social):
    tg = channel(social)
    tg._app_ready.set()
    tg._app.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=802))
    tg._app.bot.send_message.side_effect = BadRequest("chat write forbidden")
    # URL is handed to the platform; no real network request is made by the fake bot.
    receipt = await tg.send_confirmed(OutboundMessage("telegram", str(A.chat_id), "caption", media=["https://example.com/image.png"], metadata={"message_thread_id": A.thread_id}))
    assert receipt.status == "failed" and receipt.message_ids == (802,)
    assert tg._app.bot.send_photo.await_count == 1
    assert tg._app.bot.send_message.await_count == 1


async def test_channels_without_receipt_support_report_queued(social):
    receipt = await social.bus.deliver_outbound(OutboundMessage("other", "123", "queued"))
    assert receipt.status == "queued" and receipt.message_ids == ()
    assert social.bus.outbound.qsize() == 1


def test_schema_five_upgrade_adds_receipts_without_losing_history(social):
    social.store.observe(observed(social), 500)
    root = social.root
    social.close()
    import sqlite3
    with sqlite3.connect(root / "social.sqlite3") as db:
        db.execute("DROP TABLE message_deliveries")
        for table in ("wake_samples", "dialogue_jobs", "dialogue_attention", "dialogue_waits", "dialogue_state"):
            db.execute(f"DROP TABLE {table}")
        db.execute("ALTER TABLE scopes DROP COLUMN wait_infinite")
        db.execute("ALTER TABLE wakes DROP COLUMN source_seq")
        db.execute("ALTER TABLE wakes DROP COLUMN condition_config")
        db.execute("PRAGMA user_version=5")
    upgraded = SocialStore(root, now=social.now)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 11
        assert upgraded.state(A.session_key)["next_seq"] == 2
        assert upgraded.db.execute("SELECT COUNT(*) FROM message_deliveries").fetchone()[0] == 0
    finally:
        upgraded.close()


async def test_queued_keyword_cannot_outlive_condition_ttl(social):
    ctx = context(social)
    social.set_wake(ctx, keywords=["5"], senders=[], on_messages=False, ttl_seconds=3)
    social.wait(ctx, 100)
    social.store.finish(ctx.turn_id, "completed")
    await social.observe(observed(social, text="5"), directed=False)
    clock(social).advance(4)
    await social.run_due()
    assert social.bus.inbound.empty()


@pytest.mark.parametrize("chat,thread", [(123, 0), (-9999, 0), (-9999, 77)])
async def test_confirmed_ordinary_delivery_keeps_cross_chat_permissions(social, chat, thread):
    tg = channel(social)
    tg._app_ready.set()
    receipt = await tg.send_confirmed(OutboundMessage("telegram", str(chat), "cross chat", metadata={"message_thread_id": thread}))
    assert receipt.status == "sent"
    kwargs = tg._app.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == chat
    assert kwargs.get("message_thread_id", 0) == thread
    assert social.store.db.execute("SELECT COUNT(*) FROM message_deliveries").fetchone()[0] == 0


async def test_interrupted_ordinary_send_is_not_reawakened_after_restart(social):
    class ProcessInterrupted(BaseException):
        pass
    ctx = context(social, turn="interrupted-turn")
    tg = channel(social)
    tg._app_ready.set()
    tg._app.bot.send_message.side_effect = ProcessInterrupted()
    with pytest.raises(ProcessInterrupted):
        await tg.send_confirmed(OutboundMessage("telegram", str(A.chat_id), "accepted before process exit",
                                                metadata={"message_thread_id": A.thread_id, "_delivery_turn_id": ctx.turn_id}))
    assert social.store.db.execute("SELECT status FROM message_deliveries").fetchone()[0] == "queued"
    root, config, bus, now = social.root, social.config, social.bus, social.now
    social.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        restored.store.recovery_jobs()
        assert restored.store.db.execute("SELECT status FROM message_deliveries").fetchone()[0] == "unknown"
        assert restored.store.db.execute("SELECT status FROM wakes WHERE turn_id=?", (ctx.turn_id,)).fetchone()[0] == "cancelled"
        await restored.run_due()
        assert bus.inbound.empty()
    finally:
        restored.close()
