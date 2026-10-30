"""Owner-requested common permissions, with social observation/delivery unchanged."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.channels.telegram.tests.test_social_runtime import (
    A,
    agent,
    channel,
    message,
    social,
    wake,
)
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import ISOLATED_CONTEXT_KEY

__all__ = ["social"]


@pytest.mark.parametrize("disable_files", [False, True])
async def test_private_and_social_turns_have_identical_tools_and_global_switches(tmp_path, social, disable_files):
    config = ToolsConfig()
    config.file.enable = not disable_files
    config.exec.enable = not disable_files
    loop = agent(tmp_path, social, [LLMResponse(content="normal answer"), LLMResponse(content="internal")], tools_config=config)
    await loop._dispatch(InboundMessage(channel="telegram", sender_id="123", chat_id="123", content="hello"))
    await loop._dispatch(await wake(social))
    normal, group = loop.provider.chat_stream_with_retry.call_args_list
    assert normal.kwargs["tools"] == group.kwargs["tools"]
    names = {tool["function"]["name"] for tool in group.kwargs["tools"]}
    assert {"message", "cron", "spawn", "create_goal", "social_wait"} <= names
    assert ("write_file" in names) is (not disable_files)
    assert ("exec" in names) is (not disable_files)
    normal_policy = loop.sessions.get_or_create("telegram:123").policy
    group_policy = loop.sessions.get_or_create(A.session_key).policy
    for key in ("allowed_tools", "disabled_tools", "include_profile", "include_memory", "include_skills", "archive_memory", "allow_commands", "enabled"):
        assert getattr(normal_policy, key) == getattr(group_policy, key)
    assert group_policy.tool_only  # Delivery remains explicit; capability grants match.


async def test_real_file_and_shell_tools_can_edit_role_and_confirm_before_next_turn(tmp_path, social):
    social.config.dynamic_wait.quiet_seconds = 0.1
    role = social.roles / "a.md"
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="read", name="read_file", arguments={"path": str(role)}),
            ToolCallRequest(id="write", name="write_file", arguments={"path": str(role), "content": "UPDATED GROUP PERSONA"}),
            ToolCallRequest(id="shell", name="exec", arguments={"command": "printf shared-command"}),
        ]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="send", name="social_send", arguments={"text": "role updated"})]),
        LLMResponse(content="internal"),
        LLMResponse(content="next turn internal"),
    ])
    await loop._dispatch(await wake(social, text="please edit your role"))
    assert role.read_text() == "UPDATED GROUP PERSONA"
    sender.assert_awaited_once_with(A, "role updated", None)
    calls = loop.provider.chat_stream_with_retry.call_args_list
    results = {m["tool_call_id"]: m["content"] for m in calls[1].kwargs["messages"] if m["role"] == "tool"}
    assert "PERSONA ALPHA" in str(results["read"])
    assert "shared-command" in str(results["shell"])
    await social.observe(message(seq=2, text="use your updated role"), directed=True)
    await asyncio.sleep(0.15)
    await social.run_due()
    await loop._dispatch(await asyncio.wait_for(social.bus.consume_inbound(), 1))
    system = loop.provider.chat_stream_with_retry.call_args.kwargs["messages"][0]["content"]
    assert "UPDATED GROUP PERSONA" in system and "PERSONA ALPHA" not in system
    assert social.bus.outbound.empty()


@pytest.mark.parametrize("attachment", [False, True])
async def test_ordinary_message_tool_reaches_social_topic_and_does_not_duplicate_final(tmp_path, social, attachment):
    tg = channel(social)
    tg._app_ready.set()
    for name in ("send_photo", "send_video", "send_audio", "send_voice", "send_document", "send_animation"):
        setattr(tg._app.bot, name, AsyncMock(return_value=SimpleNamespace(message_id=502)))
    arguments = {"content": "ordinary message delivery"}
    if attachment:
        photo = tmp_path / "photo.png"
        photo.write_bytes(b"offline-photo")
        arguments["media"] = [str(photo)]
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="message", name="message", arguments=arguments)]),
        LLMResponse(content="internal final must stay quiet"),
    ])
    await loop._dispatch(await wake(social))
    outbound = social.bus.outbound.get_nowait()
    assert outbound.content == "ordinary message delivery"
    assert outbound.metadata["message_thread_id"] == A.thread_id
    await tg.send(outbound)
    sent = tg._app.bot.send_photo if attachment else tg._app.bot.send_message
    sent.assert_awaited_once()
    assert sent.call_args.kwargs["chat_id"] == A.chat_id
    assert sent.call_args.kwargs["message_thread_id"] == A.thread_id
    assert social.bus.outbound.empty()


async def test_ordinary_message_tool_keeps_cross_chat_delivery(tmp_path, social):
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="cross", name="message", arguments={"content": "cross chat", "channel": "telegram", "chat_id": "-1002"})]),
        LLMResponse(content="internal"),
    ])
    await loop._dispatch(await wake(social))
    outbound = social.bus.outbound.get_nowait()
    assert outbound.chat_id == "-1002" and outbound.content == "cross chat"
    assert "message_thread_id" not in outbound.metadata
    assert social.bus.outbound.empty()


async def test_background_topic_destination_is_sent_to_its_original_thread(social):
    tg = channel(social)
    tg._app_ready.set()
    await tg.send(OutboundMessage(channel="telegram", chat_id=f"{A.chat_id}:topic:{A.thread_id}", content="background reply"))
    kwargs = tg._app.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == A.chat_id and kwargs["message_thread_id"] == A.thread_id


class ExampleMCPTool(Tool):
    name = "mcp_example_echo"
    description = "Offline MCP-like dynamically registered tool"
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    async def execute(self, **kwargs):
        return "MCP result: " + kwargs["value"]


async def test_newly_registered_mcp_tools_need_no_social_allowlist(tmp_path, social):
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="mcp", name="mcp_example_echo", arguments={"value": "shared"})]),
        LLMResponse(content="internal"),
    ])
    loop.tools.register(ExampleMCPTool())
    await loop._dispatch(await wake(social))
    result = next(m for m in loop.provider.chat_stream_with_retry.call_args.kwargs["messages"] if m.get("tool_call_id") == "mcp")
    assert result["content"] == "MCP result: shared"


def test_legacy_isolation_marker_is_removed_when_existing_session_is_loaded(tmp_path, social):
    loop = agent(tmp_path, social, [])
    session = loop.sessions.get_or_create(A.session_key)
    session.metadata[ISOLATED_CONTEXT_KEY] = True
    session.add_message("user", "kept group history")
    loop.sessions.save(session)
    restored = loop.sessions.get_or_create(A.session_key)
    assert ISOLATED_CONTEXT_KEY not in restored.metadata
    assert restored.policy.allowed_tools is None
    assert restored.policy.include_skills and restored.policy.archive_memory
    assert "kept group history" in json.dumps(restored.messages)


async def test_wait_stops_ordinary_tools_later_in_the_same_batch(tmp_path, social):
    file = tmp_path / "must-not-be-written.txt"
    loop = agent(tmp_path, social, [LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="wait", name="social_wait", arguments={"seconds": 60, "sleeping": True}),
        ToolCallRequest(id="write", name="write_file", arguments={"path": str(file), "content": "too late"}),
    ])])
    await loop._dispatch(await wake(social))
    assert not file.exists()
    assert social.store.state(A.session_key)["mode"] == "sleeping"
    assert loop.provider.chat_stream_with_retry.await_count == 1
    assert social.bus.outbound.empty()


async def test_group_goal_command_can_use_normal_tools_and_continue_until_complete(tmp_path, social):
    file = tmp_path / "goal-result.txt"
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="goal", name="create_goal", arguments={"objective": "Write the result file and complete this goal"})]),
        LLMResponse(content="still working"),
        LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="write", name="write_file", arguments={"path": str(file), "content": "goal completed"}),
            ToolCallRequest(id="complete", name="update_goal", arguments={"action": "complete", "recap": "file written"}),
        ]),
        LLMResponse(content="internal final"),
    ])
    await loop._dispatch(InboundMessage(
        channel="telegram", sender_id="123", chat_id=str(A.chat_id),
        content="/goal Write the result file and complete this goal",
        session_key_override=A.session_key, metadata={"message_thread_id": A.thread_id},
    ))
    assert file.read_text() == "goal completed"
    assert loop.provider.chat_stream_with_retry.await_count == 4
    assert social.bus.outbound.empty()
