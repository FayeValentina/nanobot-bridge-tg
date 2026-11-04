"""Expose social context and settle each agent turn."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.tools.context import current_request_context
from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.context import SocialContextBuilder, transcript_text
from nanobot.channels.telegram.social.db.wakes import WakeStatus
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.social.waiting import SocialWaiting
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import ToolCallRequest
from nanobot.runtime_context import append_runtime_context


class SocialExposureHook(AgentHook):
    def __init__(self, store: SocialStore, bindings: ScopeBindings,
                 context_builder: SocialContextBuilder, waiting: SocialWaiting,
                 notify: Callable[[], None], config: Callable[[], TelegramSocialConfig]) -> None:
        super().__init__(reraise=True)
        self.store = store
        self.bindings = bindings
        self.context_builder = context_builder
        self.waiting = waiting
        self.notify = notify
        self.config = config
        self._memory_target: dict[str, Any] | None = None
        self._original_content: Any = None
        self._publication_attempted = False
        self._repair_requested = False
        self._unsent_final = False

    def _restore_memory_context(self) -> None:
        if self._memory_target is not None:
            self._memory_target["content"] = self._original_content
            self._memory_target = None
            self._original_content = None

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._restore_memory_context()
        if context.iteration > 0:
            return
        ctx = current_request_context()
        if ctx is None or ctx.session_key is None or not self.store.scopes.owns(ctx.session_key):
            return
        scope = ConversationScope.from_session_key(ctx.session_key)
        if scope is None or self.bindings.binding(scope) is None:
            return
        if ctx.turn_id is None:
            return
        # Compare actual content, not JSON's escaped representation of the view.
        blocks = await self.context_builder.build(
            ctx.session_key, ctx.turn_id, transcript_text(context.messages),
        )
        target = next((message for message in reversed(context.messages) if message.get("role") in {"user", "tool"}), None)
        if target is None:
            return
        # Enrich the newest user input/tool result, which is still in the unsent
        # delta. Rewriting an earlier user message would leave the accepted H
        # prefix stale on subsequent iterations. Do not change raw transcript
        # boundaries used by compaction. Restore the content before persistence
        # and archiving, so this automatic snapshot is never replayed as old data.
        self._memory_target = target
        self._original_content = target.get("content")
        target["content"], _ = append_runtime_context(self._original_content, blocks)

    def _note(self, context: AgentHookContext) -> None:
        ctx = current_request_context()
        if ctx is None or ctx.session_key is None or ctx.turn_id is None:
            return
        for job in self.store.note_seen(ctx.session_key, ctx.turn_id, transcript_text(context.model_messages)):
            self.waiting.consume(ctx.session_key, job)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self._note(context)

    async def before_execute_tool(self, context: AgentHookContext, tool_call: ToolCallRequest,
                                  tool: Any, params: Any) -> None:
        ctx = current_request_context()
        if ctx is None or not isinstance(params, dict):
            return
        values = cast(dict[str, Any], params)
        if tool_call.name == "social_send":
            self._publication_attempted = True
        elif tool_call.name == "message":
            channel = values.get("channel") or ctx.channel
            target = str(values.get("chat_id") or ctx.chat_id)
            if channel == ctx.channel and target in {ctx.chat_id, (ctx.session_key or "").removeprefix(ctx.channel + ":")}:
                self._publication_attempted = True

    async def after_execute_tool(self, context: AgentHookContext, tool_call: ToolCallRequest,
                                 tool: Any, params: Any, result: Any) -> None:
        ctx = current_request_context()
        if ctx is not None and ctx.attributes.get("social_deferred"):
            context.end_turn = True

    async def after_iteration(self, context: AgentHookContext) -> None:
        self._note(context)
        self._restore_memory_context()
        ctx = current_request_context()
        if ctx is not None and ctx.session_key is not None and self.store.wakes.is_activity_turn(ctx.session_key, ctx.turn_id):
            scope = ConversationScope.from_session_key(ctx.session_key)
            binding = self.bindings.binding(scope) if scope is not None else None
            if binding is not None and context.iteration + 1 >= binding.proactive.activity_max_iterations:
                # Bound free-time resource use without requiring output or a progress report.
                context.end_turn = True
        if ctx is not None and ctx.attributes.get("social_deferred"):
            context.end_turn = True
        elif (ctx is not None and ctx.metadata.get("_social_job") is not None
              and self.config().repair_unsent_final and context.final_content
              and not context.error and not self._publication_attempted and not context.end_turn):
            self._unsent_final = True
            if not self._repair_requested:
                self._repair_requested = True
                context.messages.append({"role": "user", "content": (
                    "Runtime delivery check: your previous final text was NOT sent to the group. "
                    "If it is an intended reply, publish it with social_send or message now. "
                    "If you intend silence, call social_wait or return an empty response. "
                    "Do not publish internal notes. This is one delivery check, not a request to repeat sent text."
                )})
                context.continue_turn = True
        if self._publication_attempted or (ctx is not None and ctx.attributes.get("social_deferred")) or not context.final_content:
            self._unsent_final = False

    async def on_finally(self, context: AgentRunHookContext) -> None:
        self._restore_memory_context()
        ctx = current_request_context()
        if ctx is not None and ctx.turn_id is not None:
            if ctx.session_key is not None:
                self.context_builder.selector.forget(ctx.session_key, ctx.turn_id)
            failed = context.error is not None or context.exception is not None
            status = WakeStatus.FAILED if failed else WakeStatus.UNSENT if self._unsent_final else WakeStatus.COMPLETED
            if ctx.session_key is not None and not failed and not self._unsent_final:
                self.store.acknowledge_seen(ctx.session_key, ctx.turn_id)
            self.store.wakes.finish(ctx.turn_id, status)
            if ctx.session_key is not None and not failed and not self._unsent_final:
                scope = ConversationScope.from_session_key(ctx.session_key)
                if scope is not None:
                    self.waiting.keep_reply_intent(scope, ctx.turn_id)
            if ctx.session_key is not None:
                self.store.wakes.release_pending(ctx.session_key)
            self.notify()
