"""Implementation adapter for social scenarios; only this file knows runtime internals.

The clock controls runtime/store deadlines. No scheduler runs in the background:
queue_due settles offline judge work, and tick also lets the real AgentLoop claim
every queued wake. Network boundaries are fakes, not the admission/delivery logic.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.runtime import TelegramChannel, TelegramConfig
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    MediaReference,
    ObservedMessage,
    SocialDeliveryUnknownError,
    SocialRateLimitError,
    StickerSet,
)
from nanobot.channels.telegram.social.runtime import SOCIAL_TOOLS, TelegramSocialRuntime
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialProactiveConfig
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager


class SocialHarness:
    """One isolated group, scripted main model, real runtime/store/agent/tools.

    Config overrides use TelegramSocialConfig's JSON names. The existing autouse
    offline_intermediary fixture supplies the judge; wakeOnMessages makes that
    fake admit ordinary chat. Main-model scripts default to a silent final after
    their last tool call. model_calls counts provider requests, including tool
    continuations. All observations are copies taken at external boundaries.
    """

    def __init__(self, directory: Path, **config: Any) -> None:
        self._directory = directory
        self._root = directory / "telegram-social"
        self._role = self._root / "roles" / "friend.md"
        self._role.parent.mkdir(parents=True)
        self._role.write_text("PERSONA ALPHA", encoding="utf-8")
        self._workspace = directory / "workspace"
        self._workspace.mkdir()
        self._scope = ConversationScope("telegram", -1001, 42)
        self._time = 1793188800.0  # Fixed noon UTC, independent of machine time.
        self._config = TelegramSocialConfig.model_validate({
            "enabled": True, "repairUnsentFinal": False,
            "coalesceSeconds": 0, "cooldownSeconds": 0, "sendCooldownSeconds": 0,
            "maxRequestsPerHour": 120, "maxSendsPerHour": 60,
            "dynamicWait": {"quietSeconds": 0.1},
            "judge": {"minIntervalSeconds": 0, "maxRetries": 0},
            "learning": {"enabled": False},
            "scopes": [{"chatId": -1001, "threadId": 42, "roleId": "friend",
                        "wakeOnMessages": True}],
            **config,
        })
        self._responses: deque[LLMResponse] = deque()
        self._inputs: list[list[dict[str, Any]]] = []
        self._results: dict[str, Any] = {}
        self._wakes: list[dict[str, Any]] = []
        self._sent: list[dict[str, Any]] = []
        self._outcomes: dict[int, tuple[str, float]] = {}
        self._send_calls = 0
        self._next_member_id = 0
        self._tool_id = 0
        self._provider = MagicMock()
        self._provider.get_default_model.return_value = "scenario-offline"
        self._provider.generation = GenerationSettings(max_tokens=128)
        self._provider.chat_stream_with_retry = AsyncMock(side_effect=self._respond)
        self._build()

    def _now(self) -> float:
        return self._time

    def _build(self) -> None:
        self._bus = MessageBus()
        self._bus.publish_inbound = AsyncMock(side_effect=self._publish_wake)
        self._runtime = TelegramSocialRuntime(
            self._config, self._root, self._bus, now=self._now,
        )
        # The real Telegram adapter owns ordinary message-tool quota accounting.
        self._channel = TelegramChannel(
            TelegramConfig(social=self._config), self._bus, social=self._runtime,
        )
        self._channel._app = SimpleNamespace(bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=self._telegram_text),
        ))
        self._channel._app_ready.set()
        self._bus.set_delivery_handler("telegram", self._channel.send_confirmed)
        self._runtime.set_sender(self._send_text)
        self._runtime.set_media_services(self._load_image, self._send_sticker, self._load_pack)
        registry = ToolRegistry()
        for name in SOCIAL_TOOLS:
            registry.register(TelegramSocialTool(self._runtime, name))
        results = self._results

        class Observer(AgentHook):
            async def after_execute_tool(
                self, context: AgentHookContext, tool_call: ToolCallRequest,
                tool: Any, params: Any, result: Any,
            ) -> None:
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except ValueError:
                        pass
                results[tool_call.name] = deepcopy(result)

            async def on_execute_tool_error(
                self, context: AgentHookContext, tool_call: ToolCallRequest,
                tool: Any, params: Any, error: Any,
            ) -> None:
                results[tool_call.name] = {"error": str(error)}

        observer = Observer(reraise=True)
        self._loop = AgentLoop(
            bus=self._bus, provider=self._provider, workspace=self._workspace,
            context_window_tokens=32000, model="scenario-offline", cron_service=MagicMock(),
            session_manager=SessionManager(
                self._workspace, sessions_root=self._directory / "sessions",
            ),
            session_policy_resolver=self._runtime.policy_for_session,
            request_admission=self._runtime.admit_request,
            hook_factories=[self._runtime.hook_factory, lambda turn: observer], tool_registry=registry,
        )
        self._loop.set_runtime_context_window(32000)

    async def _respond(self, **kwargs: Any) -> LLMResponse:
        self._inputs.append(deepcopy(kwargs["messages"]))
        return self._responses.popleft() if self._responses else LLMResponse(content="")

    async def _publish_wake(self, message: InboundMessage) -> None:
        snapshot = json.loads(message.content.split("\n", 1)[1])
        opportunity = snapshot.get("opportunity") or {}
        self._wakes.append({"reason": snapshot["wake_reason"], "kind": opportunity.get("kind"),
                            "content": message.content})
        await self._bus.inbound.put(message)

    async def _send(self, kind: str, value: str, reply_to: int | None) -> int:
        self._send_calls += 1
        outcome, retry_after = self._outcomes.get(self._send_calls, ("success", 0))
        if outcome == "rate_limit":
            raise SocialRateLimitError(retry_after)
        message_id = 500 + self._send_calls
        self._sent.append({"kind": kind, "text" if kind == "text" else "sticker": value,
                           "message_id": message_id, "reply_to": reply_to})
        if outcome == "unknown":
            # Telegram accepted it, but the client never received the receipt.
            raise SocialDeliveryUnknownError("receipt lost")
        return message_id

    async def _send_text(self, scope: ConversationScope, text: str, reply_to: int | None) -> int:
        assert scope == self._scope
        return await self._send("text", text, reply_to)

    async def _send_sticker(self, scope: ConversationScope, file_id: str, reply_to: int | None) -> int:
        assert scope == self._scope
        return await self._send("sticker", file_id, reply_to)

    async def _telegram_text(self, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["chat_id"] == self._scope.chat_id
        assert kwargs["message_thread_id"] == self._scope.thread_id
        return SimpleNamespace(message_id=await self._send("text", kwargs["text"], None))

    async def _load_image(self, reference: MediaReference, limit: int) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (40, 40), "red").save(output, format="PNG")
        return output.getvalue()

    async def _load_pack(self, name: str) -> StickerSet:
        return StickerSet(name=name, title="Scenario stickers", stickers=(MediaReference(
            kind="sticker", file_id="scenario-sticker-file", unique_id="scenario-sticker",
            emoji="🙂", sticker_set=name,
        ),))

    async def member_says(self, text: str, *, sender: str = "123", directed: bool = False,
                          reply_to: int | None = None) -> int:
        self._next_member_id += 1
        identity = self._next_member_id
        await self._runtime.observe(ObservedMessage(
            scope=self._scope, update_id=identity, message_id=identity, sender_id=sender,
            display_name=f"Member {sender}", timestamp=self._now(), text=text,
            learning_text=text, reply_to=reply_to, bot_username="bot",
        ), directed=directed)
        return identity

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self._time += seconds

    async def queue_due(self) -> None:
        """Settle due scheduling and judge tasks without claiming queued wakes."""
        for iteration in range(10):
            await self._runtime.run_due()
            tasks = list(self._runtime.judging.tasks.values())
            if not tasks:
                return
            await asyncio.gather(*tasks)
        raise AssertionError("offline judge did not settle")

    async def tick(self) -> None:
        await self.queue_due()
        while not self._bus.inbound.empty():
            await self._loop._dispatch(self._bus.inbound.get_nowait())
            await self.queue_due()
        if self._responses:
            raise AssertionError("scripted model responses were not consumed")

    def script_model(self, *responses: LLMResponse) -> None:
        self._responses.extend(responses)

    def tool(self, name: str, **arguments: Any) -> LLMResponse:
        """Create one scripted tool-call response with a unique ordinary call ID."""
        self._tool_id += 1
        return LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id=f"scenario-tool-{self._tool_id}", name=name, arguments=arguments,
        )])

    def edit_role(self, text: str) -> None:
        self._role.write_text(text, encoding="utf-8")

    def set_proactive(self, **settings: Any) -> None:
        self._config.scopes[0].proactive = TelegramSocialProactiveConfig.model_validate({
            "enabled": True, "minIntervalSeconds": 60, "maxIntervalSeconds": 60,
            "idleSeconds": 60, "probability": 1, "requireContext": False,
            "nightMultiplier": 1, "recentSendMultiplier": 1, "frequentSendMultiplier": 1,
            "activityIntervalSeconds": 60, **settings,
        })
        self._runtime.configure(self._config)

    def sender_outcome(self, call: int, outcome: Literal["success", "rate_limit", "unknown"],
                       *, retry_after: float = 2) -> None:
        """Program the absolute Nth platform send, shared by text and stickers."""
        self._outcomes[call] = (outcome, retry_after)

    def fail_judge(self, error: Exception) -> None:
        """Fail the existing offline judge at its provider boundary."""
        self._runtime.judging.provider.chat.side_effect = error

    def judge_calls(self) -> int:
        return self._runtime.judging.provider.chat.await_count

    async def interrupt_send(self, text: str) -> None:
        """Inject a hard-crash cut point after platform acceptance, before receipt.

        Cancelling a coroutine normally journals unknown immediately, so it
        cannot represent process death with a durable sending reservation. Set
        up that exact cut point here; recovery itself remains unmodified.
        """
        version = self._runtime.store.state(self._scope.session_key)["version"]
        self._runtime.store.reserve_send(
            self._scope.session_key, version, text, self._config.max_sends_per_hour,
            turn="interrupted-scenario-turn",
        )
        await self._send_text(self._scope, text, None)

    async def restart(self) -> None:
        await self.close()
        self._build()
        await self._runtime.recover()
        # recover starts a wall-clock scheduler. Stop it before yielding so all
        # future deadlines are exclusively driven by advance/queue_due/tick.
        await self._runtime.scheduler.stop()

    async def close(self) -> None:
        await self._runtime.scheduler.stop()
        tasks = list(self._runtime.judging.tasks.values())
        self._runtime.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def sent(self) -> list[dict[str, Any]]:
        return deepcopy(self._sent)

    def wakes(self) -> list[dict[str, Any]]:
        return deepcopy(self._wakes)

    def last_tool_result(self, name: str) -> Any:
        return deepcopy(self._results[name])

    def model_calls(self) -> int:
        return len(self._inputs)

    def model_saw(self, text: str) -> bool:
        return bool(self._inputs and text in str(self._inputs[-1]))
