"""In-memory alerts for consecutive model call failures."""

import asyncio
from dataclasses import dataclass

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ModelFailureAlertConfig
from nanobot.llm_usage.models import LLMCallRecord


@dataclass
class _State:
    failures: int = 0
    alerted: bool = False
    last_status: int | None = None
    last_kind: str | None = None


class ModelFailureAlert:
    def __init__(self, bus: MessageBus, config: ModelFailureAlertConfig) -> None:
        self._bus = bus
        self._config = config
        self._states: dict[tuple[str, str], _State] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def observe(self, call: LLMCallRecord) -> None:
        if call.finish_reason == "cancelled":
            return
        key = (call.provider, call.model)
        state = self._states.setdefault(key, _State())
        if call.error_status_code is not None or call.finish_reason == "error":
            state.failures += 1
            state.last_status = call.error_status_code
            state.last_kind = call.error_kind
            if state.failures != self._config.consecutive_failures or state.alerted:
                return
            logger.error(
                "Model {provider}/{model} failed {n} consecutive calls (status={status}, kind={kind})",
                provider=call.provider, model=call.model, n=state.failures,
                status=state.last_status, kind=state.last_kind,
            )
            text = (
                f"⚠️ 主模型连续 {state.failures} 次调用失败：{call.provider}/{call.model}，"
                f"HTTP {state.last_status if state.last_status is not None else '无'}"
                f"（{state.last_kind or '未知'}）。机器人暂时无法正常回复，请检查账户余额或模型配置。"
            )
            state.alerted = True
        else:
            del self._states[key]
            if not state.alerted:
                return
            logger.info("Model {provider}/{model} recovered", provider=call.provider, model=call.model)
            text = f"✅ 主模型已恢复：{call.provider}/{call.model}。"
        if self._config.chat_id:
            task = asyncio.get_running_loop().create_task(self._bus.publish_outbound(
                OutboundMessage(
                    channel=self._config.channel, chat_id=self._config.chat_id, content=text,
                ),
            ))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
