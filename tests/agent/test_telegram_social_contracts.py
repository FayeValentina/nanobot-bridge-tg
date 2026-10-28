"""M0 characterization probes, NOT acceptance tests for the future social runtime.

Several assertions deliberately expose limitations of the ordinary agent path.
Replace those probes with social acceptance coverage when M1/M2 add the seams
described in docs/M0_CONTRACTS.md. All providers, data and sends are local fakes.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.context_governance import ContextCompactionState, ContextGovernor
from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import (
    RequestContext,
    current_request_context,
    request_context,
)
from nanobot.agent.tools.message import MessageTool, capture_message_deliveries
from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.bus.events import InboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.runtime_context import RuntimeContextBlock
from nanobot.session.manager import SessionPolicy
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

PERSONA = "M0 trusted group persona"


def _loop(tmp_path: Path, responses: list[LLMResponse], **kwargs) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "m0-offline"
    provider.generation = GenerationSettings()
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    return AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path,
        model="m0-offline", cron_service=MagicMock(), **kwargs,
    )


def _message(*, background: bool = False, chat_id: str = "-1001") -> InboundMessage:
    key = f"telegram:{chat_id}:topic:42"
    return InboundMessage(
        channel="system" if background else "telegram",
        sender_id="social-wake" if background else "123",
        chat_id=key if background else chat_id,
        content="M0 wake snapshot",
        session_key_override=key,
        metadata={"message_id": "7", "message_thread_id": 42},
    )


class _PersonaHook(AgentHook):
    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage

    async def before_run(self, context: AgentRunHookContext) -> None:
        if self.stage == "before_run":
            context.messages[0]["content"] += f"\n{PERSONA}"

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self.stage == "before_iteration":
            context.messages[0]["content"] += f"\n{PERSONA}"


@pytest.mark.parametrize("stage", ["before_run", "before_iteration"])
@pytest.mark.parametrize("background", [False, True])
async def test_hook_system_edits_do_not_reach_durable_model_prefix(
    tmp_path: Path, stage: str, background: bool,
) -> None:
    loop = _loop(tmp_path, [LLMResponse(content="ordinary reply")])
    await loop._process_message(_message(background=background), hooks=[_PersonaHook(stage)])
    messages = loop.provider.chat_stream_with_retry.await_args.kwargs["messages"]
    assert PERSONA not in str(messages)


async def test_iteration_hook_edit_reaches_noncompacting_runner(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [LLMResponse(content="ordinary reply")])
    await loop._run_agent_loop(
        TranscriptInput(history=[], current_message="wake"),
        runtime=loop.llm_runtime(), hooks=[_PersonaHook("before_iteration")],
    )
    messages = loop.provider.chat_stream_with_retry.await_args.kwargs["messages"]
    assert PERSONA in messages[0]["content"]


def test_compaction_rebuilds_system_and_drops_runtime_material(tmp_path: Path) -> None:
    builder = ContextBuilder(tmp_path)
    raw, compaction = ContextCompactionState.from_transcript(
        TranscriptInput(history=[], current_message="wake"),
        builder.build_transcript, AsyncMock(return_value="summary"), None,
    )
    assert compaction is not None
    raw[0]["content"] += f"\n{PERSONA}"
    rebuilt = ContextGovernor._summary_transcript(compaction, "M0 compacted history")
    assert PERSONA not in rebuilt[0]["content"]
    assert "M0 compacted history" in rebuilt[0]["content"]


def test_project_and_memory_switches_do_not_isolate_global_profile(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("M0 PRIVATE SOUL", encoding="utf-8")
    (tmp_path / "USER.md").write_text("M0 PRIVATE USER", encoding="utf-8")
    group = tmp_path / "group-project"
    group.mkdir()
    (group / "SOUL.md").write_text(PERSONA, encoding="utf-8")
    builder = ContextBuilder(tmp_path)
    builder.memory.write_memory("M0 PRIVATE MEMORY")
    prompt = builder.build_system_prompt(workspace=group, include_memory=False)
    assert "M0 PRIVATE MEMORY" not in prompt
    assert "M0 PRIVATE SOUL" in prompt
    assert "M0 PRIVATE USER" in prompt
    assert PERSONA not in prompt


async def test_group_compaction_writes_to_shared_dream_journal(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [LLMResponse(content="M0 GROUP SUMMARY")])
    key = _message().session_key
    summary = await loop.consolidator.summarize_transcript(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "group"}],
        None, runtime=loop.llm_runtime(), session_key=key, tools=[],
    )
    assert summary == "M0 GROUP SUMMARY"
    records = [json.loads(line) for line in loop.context.memory.history_file.read_text().splitlines()]
    assert any(row["session_key"] == key and row["content"] == summary for row in records)
    assert any(row["content"] == summary for row in loop.context.memory.read_unprocessed_history(0))


@pytest.mark.parametrize("background", [False, True])
async def test_finalize_hook_alone_is_not_a_complete_silence_policy(
    tmp_path: Path, background: bool,
) -> None:
    class SilenceHook(AgentHook):
        def finalize_content(self, context, content):
            return ""

    loop = _loop(tmp_path, [LLMResponse(content="internal deliberation") for _ in range(3)])
    outbound = await loop._process_message(_message(background=background), hooks=[SilenceHook()])
    assert loop.provider.chat_stream_with_retry.await_count == 3
    assert outbound is not None
    assert outbound.content == EMPTY_FINAL_RESPONSE_MESSAGE


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_message_tool_dedup_depends_on_turn_kind_and_injections(
    tmp_path: Path, background: bool, injected: bool,
) -> None:
    loop = _loop(tmp_path, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="m0-send", name="message", arguments={"content": "public tool message"},
        )]),
        LLMResponse(content="internal final text"),
    ])
    registry = ToolRegistry()
    registry.register(MessageTool(send_callback=loop.bus.publish_outbound, workspace=tmp_path))
    pending = asyncio.Queue()
    if injected:
        pending.put_nowait(_message())
    outbound = await loop._process_message(
        _message(background=background), tools=registry, pending_queue=pending,
    )
    messages = []
    while not loop.bus.outbound.empty():
        messages.append(loop.bus.outbound.get_nowait())
    # Ordinary user turns also expose tool hints, another social output to gate.
    assert any(isinstance(msg.event, ProgressEvent) for msg in messages) is (not background)
    queued, = [msg for msg in messages if msg.event is None]
    assert queued.content == "public tool message"
    assert queued.metadata["message_thread_id"] == 42
    if not background and not injected:
        assert outbound is None
    else:
        assert outbound is not None
        assert outbound.content == "internal final text"


async def test_restricted_registry_rejects_unadvertised_execution(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="m0-forbidden", name="exec", arguments={"command": "must never execute"},
        )]),
        LLMResponse(content="done"),
    ])
    forbidden = loop.tools.get("exec")
    assert forbidden is not None
    forbidden.execute = AsyncMock(side_effect=AssertionError("forbidden tool executed"))
    registry = ToolRegistry()
    registry.register(MessageTool(send_callback=loop.bus.publish_outbound, workspace=tmp_path))
    await loop._process_message(_message(), tools=registry)
    definitions = loop.provider.chat_stream_with_retry.await_args_list[0].kwargs["tools"]
    assert [item["function"]["name"] for item in definitions] == ["message"]
    assert "not found" in str(loop.provider.chat_stream_with_retry.await_args.kwargs["messages"])
    forbidden.execute.assert_not_awaited()


@pytest.mark.parametrize("entry", ["turn", "runner", "disabled_policy"])
async def test_explicit_empty_registry_does_not_restore_default_tools(
    tmp_path: Path, entry: str,
) -> None:
    loop = _loop(tmp_path, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="m0-no-tools", name="exec", arguments={"command": "must never execute"},
        )]),
        LLMResponse(content="done"),
    ])
    forbidden = loop.tools.get("exec")
    assert forbidden is not None
    forbidden.execute = AsyncMock(side_effect=AssertionError("forbidden tool executed"))
    default_context = AsyncMock(return_value=RuntimeContextBlock(
        source="m0-forbidden", content="M0 forbidden tool context",
    ))
    forbidden.runtime_context_provider = MagicMock(return_value=default_context)
    registry = ToolRegistry()
    if entry == "runner":
        await loop._run_agent_loop(
            TranscriptInput(history=[], current_message="wake"),
            runtime=loop.llm_runtime(), tools=registry,
        )
    elif entry == "turn":
        await loop._process_message(_message(), tools=registry)
    else:
        session = loop.sessions.get_or_create(_message().session_key)
        session.policy = SessionPolicy(disabled_tools=frozenset(loop.tools.tool_names))
        await loop._process_message(_message())
    first_request = loop.provider.chat_stream_with_retry.await_args_list[0].kwargs
    assert not first_request["tools"]
    assert "M0 forbidden tool context" not in str(first_request["messages"])
    forbidden.execute.assert_not_awaited()
    default_context.assert_not_awaited()


async def test_factory_sees_authoritative_scope_for_concurrent_group_turns(tmp_path: Path) -> None:
    seen: list[RequestContext] = []
    barrier = asyncio.Event()

    class ScopeTool(MessageTool):
        async def execute(self, content: str, **kwargs) -> str:
            ctx = current_request_context()
            assert ctx is not None
            seen.append(ctx)
            if len(seen) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=2)
            assert current_request_context() is ctx
            return await super().execute(content=content, **kwargs)

    factory_contexts = []

    def factory(ctx):
        factory_contexts.append(ctx)
        return None

    loop = _loop(tmp_path, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id=f"m0-scope-{index}", name="message", arguments={"content": "scoped message"},
        )]) for index in range(2)
    ] + [LLMResponse(content="done") for _ in range(2)])
    registry = ToolRegistry()
    registry.register(ScopeTool(send_callback=loop.bus.publish_outbound, workspace=tmp_path))
    await asyncio.gather(*(
        loop._process_message(_message(chat_id=chat), tools=registry, hook_factories=[factory])
        for chat in ("-1001", "-1002")
    ))
    assert {ctx.session_key for ctx in seen} == {
        "telegram:-1001:topic:42", "telegram:-1002:topic:42",
    }
    assert all(ctx.sender_id == "123" and ctx.metadata["message_thread_id"] == 42 for ctx in seen)
    assert {ctx.session_key for ctx in factory_contexts} == {ctx.session_key for ctx in seen}
    assert current_request_context() is None
    sends = []
    while not loop.bus.outbound.empty():
        msg = loop.bus.outbound.get_nowait()
        if msg.event is None:
            sends.append(msg)
    assert {msg.chat_id for msg in sends} == {"-1001", "-1002"}
    assert all(msg.metadata["message_thread_id"] == 42 for msg in sends)


async def test_queue_success_precedes_any_platform_confirmation(tmp_path: Path) -> None:
    bus = MessageBus()
    tool = MessageTool(send_callback=bus.publish_outbound, workspace=tmp_path)
    msg = _message()
    with request_context(RequestContext(
        channel=msg.channel, chat_id=msg.chat_id, session_key=msg.session_key,
        metadata=msg.metadata,
    )), capture_message_deliveries() as deliveries:
        result = await tool.execute(content="public message")
    assert not is_tool_error_result(result)
    assert json.loads(result)["status"] == "queued"
    assert json.loads(result)["message_ids"] == []
    assert deliveries == {("telegram", "-1001")}
    # No channel consumer exists: this is queue acceptance, not a Telegram receipt.
    assert bus.outbound_size == 1
    queued = bus.outbound.get_nowait()
    assert queued.metadata["message_thread_id"] == 42
    assert "telegram_message_id" not in queued.metadata


async def test_enqueue_failure_does_not_record_success(tmp_path: Path) -> None:
    tool = MessageTool(
        send_callback=AsyncMock(side_effect=OSError("queue unavailable")),
        default_channel="telegram", default_chat_id="-1001", workspace=tmp_path,
    )
    with capture_message_deliveries() as deliveries:
        result = await tool.execute(content="public message")
    assert is_tool_error_result(result)
    assert not deliveries


async def test_generic_message_tool_does_not_enforce_group_scope(tmp_path: Path) -> None:
    bus = MessageBus()
    tool = MessageTool(send_callback=bus.publish_outbound, workspace=tmp_path)
    with request_context(RequestContext(channel="telegram", chat_id="-1001")):
        result = await tool.execute(content="cross-group", chat_id="-1002")
    assert not is_tool_error_result(result)
    assert bus.outbound.get_nowait().chat_id == "-1002"


async def test_dispatch_exception_has_public_fallback(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [])
    loop._process_message = AsyncMock(side_effect=RuntimeError("M0 internal failure"))
    await loop._dispatch(_message())
    outbound = loop.bus.outbound.get_nowait()
    assert outbound.content == "Sorry, I encountered an error."


async def test_explicit_session_key_prevents_unified_group_routing(tmp_path: Path) -> None:
    loop = _loop(tmp_path, [LLMResponse(content="reply")], unified_session=True)
    msg = _message()
    assert loop._effective_session_key(msg) == msg.session_key
    await loop._dispatch(msg)
    assert loop.sessions.get_cached(msg.session_key) is not None
    assert loop.sessions.get_cached("unified:default") is None
