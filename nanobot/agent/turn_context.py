"""Typed turn inputs and the mutable state carried between execution stages."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from nanobot.agent.context import TranscriptInput
from nanobot.agent.hook import AgentHook, AgentTurnHookFactory
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.turn_delivery import TurnDelivery
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.events import NO_EVENTS, EventSink
from nanobot.providers.base import LLMUsage, ProviderConversationState
from nanobot.runtime_context import RuntimeContextBlock
from nanobot.session.manager import Session
from nanobot.session.summary import SessionSummary, SessionSummaryCheckpoint
from nanobot.utils.llm_runtime import LLMRuntime


@dataclass(frozen=True, kw_only=True)
class TurnOptions:
    """One request's optional behavior; effective ephemerality is resolved by policy."""

    session_key: str | None = None
    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    pending_queue: asyncio.Queue[InboundMessage] | None = None
    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
    hooks: Sequence[AgentHook] | None = None
    hook_factories: Sequence[AgentTurnHookFactory] | None = None
    tools: ToolRegistry | None = None
    runtime: LLMRuntime | None = None
    delivery: TurnDelivery | None = None
    on_runtime_admitted: Callable[[LLMRuntime], Awaitable[None]] | None = None
    attributes: Mapping[str, Any] | None = None


class TurnKind(Enum):
    USER = auto()
    SYSTEM = auto()


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    turn_id: str
    runtime: LLMRuntime | None
    kind: TurnKind
    delivery: TurnDelivery
    original_user_text: str | None = None
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    transcript_input: TranscriptInput | None = None
    provider_state: ProviderConversationState | None = field(default=None, repr=False)
    request_context: RequestContext | None = None
    runtime_context_blocks: list[RuntimeContextBlock] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    final_content: str | None = None
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    failure_error_kind: str | None = None
    streamed_content: bool = False

    input_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None
    suppress_response: bool = False

    events: EventSink = NO_EVENTS
    streaming: bool = False
    on_runtime_admitted: Callable[[LLMRuntime], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue[InboundMessage] | None = None
    pending_summary: SessionSummary | None = None
    summary_checkpoint: SessionSummaryCheckpoint | None = None
    provider_compaction_applied: bool = False

    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
    hooks: list[AgentHook] = field(default_factory=list)
    hook_factories: list[AgentTurnHookFactory] = field(default_factory=list)
    turn_scopes: list[AbstractContextManager[Any]] = field(default_factory=list)
    tools: ToolRegistry | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    visible_run_started_at: float | None = None
    turn_latency_ms: int | None = None
    usage: LLMUsage | None = None

    def require_runtime(self) -> LLMRuntime:
        """Return the runtime established by the BUILD stage."""
        if self.runtime is None:
            raise RuntimeError("turn runtime is not initialized; BUILD must run before this stage")
        return self.runtime

    def require_session(self) -> Session:
        """Return the session established by the RESTORE stage."""
        if self.session is None:
            raise RuntimeError("turn session is not initialized; RESTORE must run before this stage")
        return self.session

