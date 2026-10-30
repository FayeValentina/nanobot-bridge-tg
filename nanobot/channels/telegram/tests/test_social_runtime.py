"""Social observation/delivery acceptance with shared ordinary assistant permissions."""

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from telegram import Update
from telegram.error import TimedOut

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.context_governance import ContextCompactionState, ContextGovernor
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.outbound_events import StreamDeltaEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.runtime import TelegramChannel, TelegramConfig
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import (
    SOCIAL_TOOLS,
    TelegramSocialRuntime,
    encode_view,
)
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import Config, TelegramSocialConfig
from nanobot.events import ContextCompactionEvent
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.runtime_context import RuntimeContextBlock
from nanobot.session.manager import ISOLATED_CONTEXT_KEY, SessionManager
from nanobot.webui.settings_system import WebUISettingsError, save_channel_config_values

A = ConversationScope("telegram", -1001, 42)
B = ConversationScope("telegram", -1002, 42)


@pytest.fixture
def social(tmp_path):
    root = tmp_path / "data" / "telegram-social"
    (root / "roles").mkdir(parents=True)
    (root / "roles" / "a.md").write_text("PERSONA ALPHA", encoding="utf-8")
    (root / "roles" / "b.md").write_text("PERSONA BETA", encoding="utf-8")
    config = TelegramSocialConfig.model_validate({
        "enabled": True,
        "repairUnsentFinal": False,
        "coalesceSeconds": 0,
        "cooldownSeconds": 0,
        "sendCooldownSeconds": 0,
        "judge": {"minIntervalSeconds": 0},
        "scopes": [
            {"chatId": -1001, "threadId": 42, "roleId": "a", "background": "ALPHA BACKGROUND"},
            {"chatId": -1002, "threadId": 42, "roleId": "b"},
        ],
    })
    runtime = TelegramSocialRuntime(config, root, MessageBus())
    yield runtime
    runtime.close()


def message(scope=A, *, seq=1, text="member text", **kwargs):
    return ObservedMessage(
        scope=scope, update_id=seq, message_id=seq, sender_id="123",
        display_name="Member", timestamp=time.time(), text=text, **kwargs,
    )


def agent(tmp_path, social, responses, *, tools_config=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "SOUL.md").write_text("PRIVATE SOUL", encoding="utf-8")
    (workspace / "USER.md").write_text("PRIVATE USER", encoding="utf-8")
    provider = MagicMock()
    provider.get_default_model.return_value = "m1-offline"
    provider.generation = GenerationSettings(max_tokens=128)
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    tools = ToolRegistry()
    for name in SOCIAL_TOOLS:
        tools.register(TelegramSocialTool(social, name))
    loop = AgentLoop(
        bus=social.bus, provider=provider, workspace=workspace,
        context_window_tokens=20000, model="m1-offline", cron_service=MagicMock(),
        session_manager=SessionManager(workspace, sessions_root=tmp_path / "sessions"),
        session_policy_resolver=social.policy_for_session, request_admission=social.admit_request,
        hook_factories=[social.hook_factory], tool_registry=tools,
        tools_config=tools_config,
    )
    loop.context.memory.write_memory("PRIVATE LONG TERM MEMORY")
    return loop


async def wake(social, scope=A, *, seq=1, text="@bot hello"):
    await social.observe(message(scope, seq=seq, text=text), directed=True)
    return await asyncio.wait_for(social.bus.consume_inbound(), 1)


def context(social, scope=A, *, turn="test-turn"):
    _, version = social._persona(scope)
    job = social.store.wake(scope.session_key, 1, 100, now=time.time())
    ctx = RequestContext(
        channel=scope.channel, chat_id=str(scope.chat_id), session_key=scope.session_key,
        turn_id=turn, metadata={"message_thread_id": scope.thread_id, "_social_job": job},
        attributes={"context_version": version},
    )
    social.admit_request(ctx)
    return ctx


def telegram_update(*, chat=-1001, thread=42, seq=1, text="hello", reply=False, edited=False, user=123, is_bot=False, timestamp=None):
    msg = {
        "message_id": seq, "date": int(time.time() if timestamp is None else timestamp), "message_thread_id": thread,
        "chat": {"id": chat, "type": "supergroup", "title": "Group"},
        "from": {"id": user, "is_bot": is_bot, "first_name": "Member"}, "text": text,
    }
    if reply:
        msg["reply_to_message"] = {
            "message_id": 99, "date": int(time.time()), "chat": msg["chat"],
            "from": {"id": 999, "is_bot": True, "first_name": "Bot"}, "text": "earlier",
        }
    return Update.de_json({"update_id": seq, "edited_message" if edited else "message": msg}, None)


def channel(social):
    result = TelegramChannel(
        TelegramConfig(social=social.config, group_policy="open", allow_from=["99999"]),
        social.bus, social=social,
    )
    result._bot_user_id = 999
    result._bot_username = "bot"
    result._app = SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
    ))
    return result


async def test_plain_messages_are_observed_without_llm_or_media_download(tmp_path, social):
    loop = agent(tmp_path, social, [])
    tg = channel(social)
    tg._download_message_media = AsyncMock(side_effect=AssertionError("must not download"))
    await tg._process_message_update(telegram_update(text="ordinary group conversation"), None)
    assert social.store.state(A.session_key)["next_seq"] == 2
    assert social.bus.inbound.empty()
    loop.provider.chat_stream_with_retry.assert_not_awaited()
    tg._download_message_media.assert_not_awaited()
    assert tg.config.allow_from == ["99999"]


@pytest.mark.parametrize("command", ["/start", "/help", "/dream@bot", "/new@bot"])
async def test_social_commands_use_ordinary_handlers_and_sender_authorization(tmp_path, social, command):
    loop = agent(tmp_path, social, [LLMResponse(content="internal")])
    loop.commands.dispatch = AsyncMock(return_value=None)
    tg = channel(social)
    tg.config.allow_from = ["123"]
    update = telegram_update(text=command)
    assert not await tg._observe_social(update)
    if command == "/start":
        with patch("telegram.Message.reply_text", new_callable=AsyncMock) as reply:
            await tg._on_start(update, None)
            reply.assert_awaited_once()
    elif command == "/help":
        with patch("telegram.Message.reply_text", new_callable=AsyncMock) as reply:
            await tg._on_help(update, None)
            reply.assert_awaited_once()
    else:
        await tg._process_forward_command(update, None)
        await loop._dispatch(social.bus.inbound.get_nowait())
        loop.commands.dispatch.assert_awaited_once()
    assert social.bus.outbound.empty()


async def test_observer_authorization_does_not_fall_back_to_ordinary_tools(social):
    config = social.config.model_copy(deep=True)
    config.scopes[0].observe_from = ["555"]
    social.configure(config)
    tg = channel(social)
    await tg._process_message_update(telegram_update(text="@bot forbidden observer"), None)
    assert social.store.state(A.session_key)["next_seq"] == 1
    assert social.bus.inbound.empty()


async def test_unconfigured_topic_keeps_ordinary_routing(social):
    tg = channel(social)
    tg.config.allow_from = ["123"]
    tg._handle_message = AsyncMock()
    tg._download_message_media = AsyncMock(return_value=([], []))
    tg._extract_reply_context = AsyncMock(return_value=None)
    tg._add_reaction = AsyncMock()
    tg._start_typing = MagicMock()
    await tg._process_message_update(telegram_update(thread=43), None)
    assert tg._handle_message.await_args.kwargs["session_key"] == "telegram:-1001:topic:43"
    assert social.store.state(A.session_key)["next_seq"] == 1


async def test_request_budget_and_stale_mentions_do_not_call_model(social):
    social.config.max_requests_per_hour = 1
    await social.observe(message(A), directed=True)
    await social.observe(message(A, seq=2), directed=True)
    await social.observe(replace(message(A, seq=3), timestamp=time.time() - 300), directed=True)
    assert social.bus.inbound.qsize() == 1
    assert social.store.state(A.session_key)["next_seq"] == 4


@pytest.mark.parametrize("reply", [False, True])
async def test_mentions_and_replies_wake_even_when_group_policy_is_open(tmp_path, social, reply):
    loop = agent(tmp_path, social, [LLMResponse(content="internal final")])
    tg = channel(social)
    await tg._process_message_update(telegram_update(text="hello" if reply else "@bot hello", reply=reply), None)
    inbound = social.bus.inbound.get_nowait()
    assert inbound.session_key == A.session_key
    await loop._dispatch(inbound)
    assert loop.provider.chat_stream_with_retry.await_count == 1
    assert social.bus.outbound.empty()


async def test_groups_keep_roles_and_history_with_ordinary_profile_and_tools(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content="internal A"), LLMResponse(content="internal B")])
    await social.observe(message(A, text="ALPHA GROUP SECRET"), directed=False)
    await loop._dispatch(await wake(social, A, seq=2))
    await loop._dispatch(await wake(social, B, seq=3, text="BETA GROUP SECRET"))
    calls = loop.provider.chat_stream_with_retry.await_args_list
    for call, own, other in ((calls[0], "ALPHA", "BETA"), (calls[1], "BETA", "ALPHA")):
        text = str(call.kwargs["messages"])
        assert f"PERSONA {own}" in text and other not in text
        assert "PRIVATE SOUL" in text and "PRIVATE USER" in text
        assert "PRIVATE LONG TERM MEMORY" in text
        assert {tool["function"]["name"] for tool in call.kwargs["tools"]} == set(loop.tools.tool_names)
    assert loop.sessions.read_session_file(A.session_key) is not None
    assert loop.sessions.read_session_file(B.session_key) is not None
    assert social.bus.outbound.empty()


async def test_tool_send_is_scope_checked_confirmed_and_final_is_silent(tmp_path, social):
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="read", name="social_mark_read", arguments={"through": 1}),
            ToolCallRequest(id="send", name="social_send", arguments={"text": "public hello", "reply_to": 1}),
        ]),
        LLMResponse(content=""),
    ])
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    await loop._dispatch(await wake(social))
    sender.assert_awaited_once_with(A, "public hello", 1)
    assert loop.provider.chat_stream_with_retry.await_count == 2
    assert social.store.state(A.session_key)["read_through"] == 1
    row = social.store.db.execute("SELECT status,message_id FROM actions").fetchone()
    assert tuple(row) == ("sent", 501)
    assert social.store.has_message(A.session_key, 501)
    assert social.bus.outbound.empty()


async def test_exec_is_available_and_social_send_keeps_its_current_topic_interface(tmp_path, social):
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="exec", name="exec", arguments={"command": "forbidden"}),
            ToolCallRequest(id="cross", name="social_send", arguments={"text": "bad", "chat_id": "-1002"}),
        ]), LLMResponse(content="internal"),
    ])
    command = loop.tools.get("exec")
    command.execute = AsyncMock(return_value="ordinary command result")
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    await loop._dispatch(await wake(social))
    command.execute.assert_awaited_once_with(command="forbidden")
    sender.assert_not_awaited()
    assert social.bus.outbound.empty()


@pytest.mark.parametrize("kind", ["empty", "model_error", "exception", "budget"])
async def test_all_internal_endings_are_quiet(tmp_path, social, kind):
    responses = [LLMResponse(content="")]
    if kind == "model_error":
        responses = [LLMResponse(content="provider failed", finish_reason="error")]
    elif kind == "exception":
        responses = [RuntimeError("provider broke")]
    elif kind == "budget":
        responses = [LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="state", name="social_get_state", arguments={}),
        ])]
    loop = agent(tmp_path, social, responses)
    loop.max_iterations = 1
    await loop._dispatch(await wake(social))
    assert loop.provider.chat_stream_with_retry.await_count == 1
    assert social.bus.outbound.empty()


async def test_private_chat_keeps_personal_profile_and_normal_reply(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content="private answer")])
    await loop._dispatch(InboundMessage(channel="telegram", sender_id="123", chat_id="123", content="hello"))
    request = loop.provider.chat_stream_with_retry.await_args.kwargs
    assert "PRIVATE USER" in str(request["messages"])
    assert "PRIVATE LONG TERM MEMORY" in str(request["messages"])
    assert {tool["function"]["name"] for tool in request["tools"]} == set(loop.tools.tool_names)
    assert social.bus.outbound.get_nowait().content == "private answer"


@pytest.mark.parametrize("scope,role_id", [(A, "a"), (B, "b")])
async def test_persona_source_is_available_in_scoped_state_tool(tmp_path, social, scope, role_id):
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="state", name="social_get_state", arguments={})]),
        LLMResponse(content="internal"),
    ])
    await loop._dispatch(await wake(social, scope, text="Where is your personality configuration file?"))
    request = loop.provider.chat_stream_with_retry.await_args.kwargs
    result = next(m for m in request["messages"] if m.get("tool_call_id") == "state")
    state = json.loads(result["content"])
    assert state["role_id"] == role_id
    assert state["role_file"] == str((social.roles / f"{role_id}.md").resolve())
    assert state["tool_permissions"] == "shared_with_normal_chat"
    assert "social_get_state for the actual role_id and role_file" in request["messages"][0]["content"]
    names = {tool["function"]["name"] for tool in request["tools"]}
    assert names == set(loop.tools.tool_names)
    assert "PRIVATE USER" in str(request["messages"])
    assert social.bus.outbound.empty()


@pytest.mark.parametrize("source", ["summary", "failure", "raw", "idle"])
async def test_group_compaction_uses_ordinary_memory_archiving(tmp_path, social, source):
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, finish_reason="error") if source == "failure"
        else LLMResponse(content="GROUP CHECKPOINT"),
    ])
    session = loop.sessions.get_or_create(A.session_key)
    session.add_message("user", "group source evidence")
    session.add_message("assistant", "group response")
    loop.sessions.save(session)
    if source == "raw":
        summary = await loop.consolidator.archiver.archive_session(
            session, archive_end=len(session.messages), runtime=loop.llm_runtime(), input_token_budget=0,
        )
        loop.provider.chat_stream_with_retry.assert_not_awaited()
    elif source == "idle":
        summary = await loop.consolidator.compact_idle_session(A.session_key, runtime=loop.llm_runtime())
    else:
        summary = await loop.consolidator.archive_session(
            session, archive_end=len(session.messages), runtime=loop.llm_runtime(),
        )
    assert summary
    assert loop.context.memory.history_file.exists()
    if source != "raw":
        prompts = str(loop.provider.chat_stream_with_retry.await_args.kwargs["messages"])
        assert "PERSONA ALPHA" in prompts
        assert "PRIVATE USER" in prompts


def test_persona_survives_rebuild_with_the_common_profile_and_memory(tmp_path, social):
    loop = agent(tmp_path, social, [])
    policy = loop.sessions.get_or_create(A.session_key).policy
    builder = ContextBuilder(loop.workspace)
    def build(transcript):
        return builder.build_transcript(
            transcript, include_profile=policy.include_profile, include_memory=policy.include_memory,
            include_skills=policy.include_skills, trusted_persona=policy.persona,
        )
    _, state = ContextCompactionState.from_transcript(
        TranscriptInput(history=[], current_message="group snapshot"), build, AsyncMock(), None,
    )
    rebuilt = ContextGovernor._summary_transcript(state, "GROUP CHECKPOINT")
    assert "PERSONA ALPHA" in rebuilt[0]["content"]
    assert "GROUP CHECKPOINT" in rebuilt[0]["content"]
    assert "PRIVATE USER" in str(rebuilt)


def test_token_estimation_uses_the_common_tool_registry(tmp_path, social, monkeypatch):
    loop = agent(tmp_path, social, [])
    session = loop.sessions.get_or_create(A.session_key)
    estimate = MagicMock(return_value=(100, "offline"))
    monkeypatch.setattr("nanobot.agent.memory.estimate_prompt_tokens_chain", estimate)
    loop.consolidator.estimate_session_prompt_tokens(session, runtime=loop.llm_runtime())
    tools = estimate.call_args.args[3]
    assert {tool["function"]["name"] for tool in tools} == set(loop.tools.tool_names)


async def test_restart_restores_common_permissions_and_current_role(tmp_path, social):
    loop = agent(tmp_path, social, [])
    session = loop.sessions.get_or_create(A.session_key)
    session.add_message("user", "GROUP HISTORY")
    loop.sessions.save(session)
    restored = SessionManager(loop.workspace, sessions_root=tmp_path / "sessions")
    closed = restored.get_or_create(A.session_key)
    assert ISOLATED_CONTEXT_KEY not in closed.metadata
    assert closed.policy.enabled and closed.policy.archive_memory
    restored.set_policy_resolver(social.policy_for_session)
    opened = restored.get_or_create(A.session_key)
    assert opened.policy.enabled and opened.policy.persona == session.policy.persona
    assert opened.get_history()[0]["content"] == "GROUP HISTORY"


async def test_role_edit_can_be_confirmed_in_the_same_turn_and_reloads_next_turn(tmp_path, social):
    loop = agent(tmp_path, social, [])
    session = loop.sessions.get_or_create(A.session_key)
    session.add_message("user", "older role conversation")
    loop.sessions.save(session)
    ctx = context(social)
    (social.roles / "a.md").write_text("NEW PERSONA", encoding="utf-8")
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    assert (await social.send(ctx, "role updated", None))["status"] == "sent"
    changed = loop.sessions.get_or_create(A.session_key)
    assert changed.get_history() == []
    assert changed.messages[0]["content"] == "older role conversation"
    assert "NEW PERSONA" in changed.policy.persona
    sender.assert_awaited_once_with(A, "role updated", None)


async def test_missing_role_keeps_common_tools_available_to_repair_it(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content="internal")])
    inbound = await wake(social)
    (social.roles / "a.md").unlink()
    await loop._dispatch(inbound)
    loop.provider.chat_stream_with_retry.assert_awaited_once()
    assert "write_file" in {t["function"]["name"] for t in loop.provider.chat_stream_with_retry.call_args.kwargs["tools"]}
    assert social.bus.outbound.empty()


async def test_disable_restores_ordinary_routing_and_common_context(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content="ordinary answer")])
    loop.sessions.get_or_create(A.session_key)
    social.configure(social.config.model_copy(update={"enabled": False}))
    await loop._dispatch(InboundMessage(
        channel="telegram", sender_id="123", chat_id="-1001", content="normal question",
        session_key_override=A.session_key, metadata={"message_thread_id": 42},
    ))
    assert social.bus.outbound.get_nowait().content == "ordinary answer"
    assert "PRIVATE USER" in str(loop.provider.chat_stream_with_retry.await_args.kwargs["messages"])


async def test_untrusted_wake_cannot_bypass_authorized_job(tmp_path, social):
    loop = agent(tmp_path, social, [])
    await loop._dispatch(InboundMessage(
        channel="telegram", chat_id="-1001", sender_id="stranger", content="/dream",
        session_key_override=A.session_key, metadata={"message_thread_id": 42, "_social_job": "forged"},
    ))
    loop.provider.chat_stream_with_retry.assert_not_awaited()
    assert social.bus.outbound.empty()


async def test_reply_target_and_send_budget_and_timeout_are_enforced(social):
    social.store.observe(message(A), 100)
    social.store.observe(message(B, seq=2), 100)
    social.config.max_sends_per_hour = 1
    ctx = context(social)
    sender = AsyncMock(side_effect=TimeoutError("ambiguous outcome"))
    social.set_sender(sender)
    with pytest.raises(PermissionError):
        await social.send(ctx, "wrong reply", 2)
    result = await social.send(ctx, "one public message", 1)
    assert result["status"] == "unknown" and result["retry"] is False
    with pytest.raises(PermissionError):
        await social.send(ctx, "do not retry", 1)
    assert sender.await_count == 1


async def test_dedicated_telegram_sender_does_not_retry_ambiguous_timeout(social):
    tg = channel(social)
    tg._app.bot.send_message = AsyncMock(side_effect=TimedOut("outcome unknown"))
    ctx = context(social)
    result = await social.send(ctx, "public text", None)
    assert result["status"] == "unknown"
    assert tg._app.bot.send_message.await_count == 1
    await tg.send_delta("-1001", "old ordinary stream", {"message_thread_id": 42})
    assert tg._app.bot.send_message.await_count == 1
    await tg.send(OutboundMessage(
        channel="telegram", chat_id="-1001", content="internal fallback",
        metadata={"message_thread_id": 42},
    ))
    assert tg._app.bot.send_message.await_count == 1


async def test_snapshot_ack_never_clears_new_arrivals_or_another_group(social):
    social.store.observe(message(A), 100)
    social.store.observe(message(A, seq=2), 100)
    ctx = context(social)
    view = social.store.view(A.session_key, count=2, chars=4000, turn_id=ctx.turn_id)
    with pytest.raises(ValueError):
        social.mark_read(ctx, 2)
    social.store.note_seen(A.session_key, ctx.turn_id, encode_view(view))
    social.store.observe(message(A, seq=3), 100)
    assert social.mark_read(ctx, 2) == 2
    with pytest.raises(ValueError):
        social.mark_read(ctx, 3)
    assert social.store.state(B.session_key)["read_through"] == 0
    other = replace(ctx, chat_id=str(B.chat_id))
    with pytest.raises(PermissionError):
        social.read_messages(other)


async def test_unseen_truncated_snapshot_does_not_grant_read_ack(social):
    social.store.observe(message(A), 100)
    ctx = context(social)
    view = social.store.view(A.session_key, count=2, chars=4000, turn_id=ctx.turn_id)
    social.store.note_seen(A.session_key, ctx.turn_id, encode_view(view)[:30])
    with pytest.raises(ValueError):
        social.mark_read(ctx, 1)


async def test_duplicates_edits_and_media_markers_do_not_produce_extra_wakes(social):
    original = message(A, text="original", media=("photo",))
    await social.observe(original, directed=True)
    await social.observe(original, directed=True)
    await social.observe(replace(original, update_id=2, text="edited"), directed=True)
    assert social.bus.inbound.qsize() == 1
    assert social.store.state(A.session_key)["next_seq"] == 2
    view = social.store.view(A.session_key, count=2, chars=4000)
    assert view["messages"][0]["text"] == "edited"
    assert view["messages"][0]["media"] == ["photo"]


def test_store_transaction_failure_does_not_advance_watermark(social):
    social.store.db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON messages
        BEGIN SELECT RAISE(ABORT,'simulated write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        social.store.observe(message(A), 100)
    assert social.store.state(A.session_key)["next_seq"] == 1
    social.store.db.execute("DROP TRIGGER fail_insert")
    assert social.store.observe(message(A), 100) == (1, True)


def test_retention_is_bounded_and_history_gap_is_not_acknowledged(social):
    for seq in range(1, 106):
        social.store.observe(message(A, seq=seq, text="x" * 4000), 100)
    view = social.store.view(A.session_key, count=100, chars=4000, turn_id="reader")
    assert len(encode_view(view)) <= 4000
    assert view["history_gap"] == [1, 5]
    assert social.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 100
    social.store.note_seen(A.session_key, "reader", encode_view(view))
    with pytest.raises(ValueError):
        social.store.mark_read(A.session_key, "reader", view["messages"][0]["seq"])


async def test_recovery_coalesces_pending_wakes_and_does_not_resend_actions(social):
    await social.observe(message(A), directed=True)
    await social.observe(message(A, seq=2), directed=True)
    while not social.bus.inbound.empty():
        social.bus.inbound.get_nowait()
    await social.recover()
    assert social.bus.inbound.qsize() == 1
    await social.recover()
    assert social.bus.inbound.qsize() == 1


async def test_uncertain_send_cancels_recovery_of_the_old_question(social):
    await social.observe(message(A), directed=True)
    social.bus.inbound.get_nowait()
    version = social.store.state(A.session_key)["version"]
    social.store.reserve_send(A.session_key, version, "interrupted", 6)
    await social.recover()
    assert social.bus.inbound.empty()
    assert social.store.db.execute("SELECT status FROM actions").fetchone()[0] == "unknown"
    view = social.store.view(A.session_key, count=2, chars=4000)
    assert view["uncertain_send"]["text"] == "interrupted"


async def test_uncertain_text_cannot_be_blindly_repeated_in_a_new_action(social):
    sender = AsyncMock(side_effect=TimeoutError())
    social.set_sender(sender)
    ctx = context(social)
    first = await social.send(ctx, "uncertain public text", None)
    assert first["status"] == "unknown"
    assert (await social.send(ctx, "uncertain public text", None))["action_id"] == first["action_id"]
    new_ctx = context(social, turn="new-attempt")
    with pytest.raises(PermissionError, match="blindly repeat"):
        await social.send(new_ctx, "uncertain public text", None)
    assert sender.await_count == 1


async def test_budget_configuration_change_invalidates_an_admitted_turn(social):
    ctx = context(social)
    social.config.max_sends_per_hour = 1
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    with pytest.raises(PermissionError):
        await social.send(ctx, "old turn", None)
    sender.assert_not_awaited()


async def test_skills_and_runtime_providers_are_loaded_as_in_normal_chat(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content="internal")])
    skill = loop.workspace / "skills" / "private"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: private\ndescription: PRIVATE SKILL DESCRIPTION\n---\nPRIVATE SKILL CONTENT", encoding="utf-8",
    )
    provider = AsyncMock(return_value=RuntimeContextBlock(source="private", content="PRIVATE RUNTIME DATA"))
    loop.register_runtime_context_provider(provider)
    await loop._dispatch(await wake(social, text="@bot use $private"))
    assert "PRIVATE RUNTIME DATA" in str(loop.provider.chat_stream_with_retry.await_args.kwargs["messages"])
    assert "PRIVATE SKILL" in str(loop.provider.chat_stream_with_retry.await_args.kwargs["messages"])
    provider.assert_awaited()


def test_new_runtime_drops_opaque_inflight_state_without_losing_committed_history(tmp_path, social):
    from nanobot.providers.base import ProviderConversationState

    loop = agent(tmp_path, social, [])
    session = loop.sessions.get_or_create(A.session_key)
    session.add_message("user", "COMMITTED GROUP HISTORY")
    session.provider_state = ProviderConversationState(
        kind="openai_responses", provider="test", model="m1-offline", version=1, payload={},
    )
    session.metadata["runtime_checkpoint"] = {"phase": "tool_calls"}
    second = TelegramSocialRuntime(social.config, social.root, MessageBus())
    try:
        second.policy_for_session(session)
        assert session.provider_state is None
        assert "runtime_checkpoint" not in session.metadata
        assert session.get_history()[0]["content"] == "COMMITTED GROUP HISTORY"
    finally:
        second.close()


def test_corrupt_store_is_not_replaced_with_empty_database(tmp_path):
    path = tmp_path / "social.sqlite3"
    path.write_bytes(b"broken sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        SocialStore(tmp_path)
    assert path.read_bytes() == b"broken sqlite database"


@pytest.mark.parametrize("values", [
    {"enabled": True, "scopes": [{"chatId": 123, "roleId": "a"}]},
    {"scopes": [{"chatId": -1, "roleId": "../private"}]},
    {"scopes": [{"chatId": -1, "roleId": "a", "observeFrom": ["admin"]}]},
    {"unknown": True},
    {"scopes": [{"chatId": -1, "roleId": "a"}, {"chatId": -1, "roleId": "b"}]},
])
def test_invalid_social_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate(values)


def test_role_symlink_cannot_read_private_files(tmp_path, social):
    outside = tmp_path / "private.md"
    outside.write_text("must not load", encoding="utf-8")
    (social.roles / "a.md").unlink()
    (social.roles / "a.md").symlink_to(outside)
    with pytest.raises(PermissionError):
        social._persona(A)


def test_replacing_role_directory_with_a_symlink_cannot_escape_root(tmp_path, social):
    outside = tmp_path / "other-roles"
    outside.mkdir()
    (outside / "a.md").write_text("PRIVATE FILE", encoding="utf-8")
    social.roles.rename(social.root / "original-roles")
    social.roles.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        social._persona(A)


@pytest.mark.parametrize("route", ["user", "background", "idle"])
async def test_internal_notifications_cannot_bypass_silence(tmp_path, social, route):
    loop = agent(tmp_path, social, [])
    msg = InboundMessage(
        channel="system" if route == "background" else "telegram", sender_id="test",
        chat_id=A.session_key if route == "background" else str(A.chat_id), content="",
        session_key_override=A.session_key, metadata={"message_thread_id": 42},
    )
    delivery = loop.turn_delivery_factory.create(msg, A.session_key, enable_stream=True)
    sink = loop.turn_delivery_factory.session_events(A.session_key, {}) if route == "idle" else delivery.events
    await sink.emit(ContextCompactionEvent("test", "started"))
    await sink.emit(StreamDeltaEvent(content="private reasoning"))
    await delivery.fail(publish_completion=True)
    assert social.bus.outbound.empty()


async def test_concurrent_sends_reserve_one_shared_budget(social):
    social.config.max_sends_per_hour = 1
    first = context(social, turn="one")
    second = context(social, turn="two")
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    results = await asyncio.gather(
        social.send(first, "one", None), social.send(second, "two", None), return_exceptions=True,
    )
    assert sum(isinstance(result, PermissionError) for result in results) == 1
    assert sender.await_count == 1


async def test_busy_turn_defers_new_wake_without_losing_watermark(tmp_path, social):
    loop = agent(tmp_path, social, [])
    first_request = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(**kwargs):
        calls.append(str(kwargs["messages"]))
        if len(calls) == 1:
            first_request.set()
            await release.wait()
        return LLMResponse(content="")

    loop.provider.chat_stream_with_retry = AsyncMock(side_effect=respond)
    await social.recover()
    runner = asyncio.create_task(loop.run())
    try:
        await social.observe(message(A, text="FIRST SNAPSHOT"), directed=True)
        await asyncio.wait_for(first_request.wait(), 2)
        await social.observe(message(A, seq=2, text="NEW MESSAGE DURING TURN"), directed=True)
        assert social.store.db.execute("SELECT COUNT(*) FROM wakes WHERE status='scheduled'").fetchone()[0] == 1
        assert loop._pending_queues[A.session_key].empty()
        release.set()

        async def wait_completed():
            while social.store.db.execute("SELECT COUNT(*) FROM wakes WHERE status<>'completed'").fetchone()[0]:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_completed(), 2)
        assert len(calls) == 2
        assert "NEW MESSAGE DURING TURN" in calls[1]
        assert social.store.state(A.session_key)["read_through"] == 2
        assert social.bus.outbound.empty()
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


def test_cached_old_summary_is_not_replayed_after_role_switch(tmp_path, social):
    loop = agent(tmp_path, social, [])
    session = loop.sessions.get_or_create(A.session_key)
    loop.auto_compact._summaries[A.session_key] = {"text": "OLD ROLE DATA", "last_active": "2026-10-28"}
    (social.roles / "a.md").write_text("CHANGED ROLE", encoding="utf-8")
    current = loop.sessions.get_or_create(A.session_key)
    _, summary = loop.auto_compact.prepare_session(current, A.session_key)
    assert summary is None
    assert "CHANGED ROLE" in session.policy.persona


def test_webui_saves_typed_social_config_and_rejects_invalid_authorization():
    from nanobot.channels.registry import load_channel_plugin

    config = Config()
    saved = save_channel_config_values(
        config, "telegram", {"channels.telegram.social": json.dumps({"enabled": False, "scopes": []})},
        load_channel_plugin=load_channel_plugin,
    )
    assert saved == ["channels.telegram.social"]
    with pytest.raises(WebUISettingsError):
        save_channel_config_values(
            config, "telegram", {"channels.telegram.social": json.dumps({
                "enabled": True, "scopes": [{"chatId": 1, "roleId": "a"}],
            })}, load_channel_plugin=load_channel_plugin,
        )


@pytest.mark.parametrize("unified", [False, True])
def test_personal_heartbeat_can_choose_a_social_group(social, unified):
    from nanobot.cli.gateway_runtime import _pick_heartbeat_target_from_sessions
    from nanobot.session.keys import UNIFIED_SESSION_KEY, remember_last_channel

    metadata = {}
    remember_last_channel(metadata, "telegram", str(A.chat_id))
    target = _pick_heartbeat_target_from_sessions(
        enabled_channels=["telegram"], archived_keys=[],
        sessions=[{"key": UNIFIED_SESSION_KEY if unified else A.session_key}, {"key": "telegram:123"}],
        unified_session_metadata=metadata,
        target_allowed=social.allows_personal_delivery,
    )
    assert target == ("telegram", str(A.chat_id) if unified else f"{A.chat_id}:topic:{A.thread_id}")


async def test_background_sessions_keep_ordinary_permissions_for_social_targets(tmp_path, social):
    loop = agent(tmp_path, social, [LLMResponse(content="background response")])
    await loop._process_message(InboundMessage(
            channel="telegram", sender_id="background", chat_id=str(A.chat_id),
            content="personal background task", session_key_override="heartbeat",
            metadata={"message_thread_id": 42},
    ))
    loop.provider.chat_stream_with_retry.assert_awaited_once()
    assert "PRIVATE USER" in str(loop.provider.chat_stream_with_retry.call_args.kwargs["messages"])


def test_tools_require_current_request_and_do_not_import_telegram_sdk():
    import subprocess
    import sys

    result = subprocess.run([
        sys.executable, "-c",
        "import sys; import nanobot.agent.tools.telegram_social; assert 'telegram' not in sys.modules",
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("thread", [0, 43, 999])
async def test_group_wide_binding_observes_and_replies_in_each_actual_topic(tmp_path, social, thread):
    social.config.scopes[0].all_topics = True
    social.configure(social.config)
    target = ConversationScope("telegram", A.chat_id, thread)
    tg = channel(social)
    tg._handle_message = AsyncMock(side_effect=AssertionError("must use social routing"))
    await tg._process_message_update(telegram_update(thread=thread, text="ordinary conversation"), None)
    assert social.bus.inbound.empty()
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="send", name="social_send", arguments={"text": "received"},
        )]),
        LLMResponse(content="internal"),
    ])
    await tg._process_message_update(telegram_update(thread=thread, seq=2, text="@bot hello", reply=True), None)
    inbound = social.bus.inbound.get_nowait()
    assert inbound.session_key == target.session_key
    await loop._dispatch(inbound)
    sent = tg._app.bot.send_message.call_args.kwargs
    assert sent["chat_id"] == A.chat_id
    assert sent.get("message_thread_id", 0) == thread
    system = str(loop.provider.chat_stream_with_retry.call_args.kwargs["messages"][0]["content"])
    assert "For a member-message wake, an intermediary selected the batch" in system
    tg._handle_message.assert_not_awaited()
    if target != A:
        assert social.store.state(A.session_key)["next_seq"] == 1


@pytest.mark.parametrize("override_first", [False, True])
def test_topic_override_can_disable_group_wide_binding_without_affecting_other_groups(social, override_first):
    group = social.config.scopes[0]
    group.all_topics = True
    override = group.model_copy(update={"all_topics": False, "thread_id": 43, "enabled": False})
    social.config.scopes.insert(0 if override_first else len(social.config.scopes), override)
    social.configure(social.config)
    assert social.binding(ConversationScope("telegram", A.chat_id, 43)) is None
    assert social.binding(ConversationScope("telegram", A.chat_id, 999)) == group
    assert social.binding(B).role_id == "b"
    assert social.binding(ConversationScope("telegram", B.chat_id, 999)) is None
    assert social.binding(ConversationScope("telegram", -9999, 43)) is None


def test_group_wide_role_and_existing_topic_history_survive_reconfiguration(social):
    social.store.observe(message(A, text="keep this topic history"), 500)
    group = social.config.scopes[0]
    group.all_topics = True
    social.configure(social.config)
    other = ConversationScope("telegram", A.chat_id, 43)
    social._persona(other)
    assert social.store.state(A.session_key)["next_seq"] == 2
    assert social.store.state(other.session_key)["next_seq"] == 1
    assert "PERSONA ALPHA" in social._persona(other)[0]
    group.all_topics = False
    social.configure(social.config)
    assert social.binding(other) is None
    assert social.store.state(other.session_key)["version"] == "disabled"
    assert social.store.state(A.session_key)["next_seq"] == 2
