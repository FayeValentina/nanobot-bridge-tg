"""Model failure notifications use the normal outbound message bus."""

import asyncio
from dataclasses import replace

import pytest
from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ModelFailureAlertConfig
from nanobot.llm_usage.alerts import ModelFailureAlert
from nanobot.llm_usage.models import LLMCallRecord

FAILURE = LLMCallRecord(
    started_at_ms=0, duration_ms=1, provider="openai", model="main-model",
    source="user", stream=False, finish_reason="error",
    error_status_code=402, error_kind="http_status",
)
SUCCESS = replace(FAILURE, finish_reason="stop", error_status_code=None, error_kind=None)


async def test_alert_once_at_threshold_and_no_more_on_continued_failure() -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(bus, ModelFailureAlertConfig(chat_id="123"))

    for _ in range(2):
        alert.observe(FAILURE)
    await asyncio.sleep(0)
    assert bus.outbound_size == 0
    alert.observe(FAILURE)
    await asyncio.sleep(0)
    assert bus.outbound_size == 1
    message = await bus.consume_outbound()
    assert message.channel == "telegram"
    assert message.chat_id == "123"
    assert message.content == (
        "⚠️ 主模型连续 3 次调用失败：openai/main-model，HTTP 402（http_status）。"
        "机器人暂时无法正常回复，请检查账户余额或模型配置。"
    )

    for _ in range(2):
        alert.observe(FAILURE)
    await asyncio.sleep(0)
    assert bus.outbound_size == 0


async def test_recovery_then_new_failure_streak_alerts_again() -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(bus, ModelFailureAlertConfig(chat_id="123"))
    for _ in range(3):
        alert.observe(FAILURE)
    await asyncio.sleep(0)
    await bus.consume_outbound()

    alert.observe(SUCCESS)
    alert.observe(SUCCESS)
    await asyncio.sleep(0)
    assert bus.outbound_size == 1
    recovery = await bus.consume_outbound()
    assert recovery.content == "✅ 主模型已恢复：openai/main-model。"
    assert recovery.chat_id == "123"

    for _ in range(3):
        alert.observe(FAILURE)
    await asyncio.sleep(0)
    assert bus.outbound_size == 1
    assert (await bus.consume_outbound()).content.startswith("⚠️ 主模型连续 3 次调用失败")


async def test_success_resets_failure_count_before_threshold() -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(bus, ModelFailureAlertConfig(chat_id="123"))

    for call in [FAILURE, FAILURE, SUCCESS, FAILURE, FAILURE]:
        alert.observe(call)
    await asyncio.sleep(0)
    assert bus.outbound_size == 0


async def test_cancelled_neither_counts_nor_resets_or_recovers() -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(bus, ModelFailureAlertConfig(chat_id="123"))
    cancelled = replace(FAILURE, finish_reason="cancelled")

    for call in [FAILURE, cancelled, FAILURE]:
        alert.observe(call)
    await asyncio.sleep(0)
    assert bus.outbound_size == 0
    alert.observe(cancelled)
    alert.observe(FAILURE)
    await asyncio.sleep(0)
    assert bus.outbound_size == 1
    await bus.consume_outbound()
    alert.observe(cancelled)
    alert.observe(FAILURE)
    await asyncio.sleep(0)
    assert bus.outbound_size == 0


async def test_provider_model_pairs_count_independently() -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(bus, ModelFailureAlertConfig(chat_id="123"))
    other_provider = replace(FAILURE, provider="other")
    other_model = replace(FAILURE, model="other-model")

    for call in [FAILURE, other_provider, other_model] * 2:
        alert.observe(call)
    await asyncio.sleep(0)
    assert bus.outbound_size == 0

    for call in [FAILURE, other_provider, other_model]:
        alert.observe(call)
        await asyncio.sleep(0)
        assert bus.outbound_size == 1
        assert f"{call.provider}/{call.model}" in (await bus.consume_outbound()).content


async def test_empty_chat_id_logs_once_without_direct_message() -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(bus, ModelFailureAlertConfig())
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR", format="{message}")
    try:
        for _ in range(5):
            alert.observe(FAILURE)
        await asyncio.sleep(0)
        assert bus.outbound_size == 0
        assert messages == [
            "Model openai/main-model failed 3 consecutive calls (status=402, kind=http_status)\n",
        ]
    finally:
        logger.remove(sink)


@pytest.mark.parametrize(
    ("finish_reason", "status", "kind", "expected"),
    [("error", None, None, "HTTP 无（未知）"), ("stop", 402, "http_status", "HTTP 402（http_status）")],
)
async def test_failure_conditions_and_configured_threshold_and_channel(
    finish_reason: str, status: int | None, kind: str | None, expected: str,
) -> None:
    bus = MessageBus()
    alert = ModelFailureAlert(
        bus, ModelFailureAlertConfig(channel="test", chat_id="123", consecutive_failures=1),
    )
    alert.observe(replace(FAILURE, finish_reason=finish_reason, error_status_code=status, error_kind=kind))
    await asyncio.sleep(0)
    assert bus.outbound_size == 1
    message = await bus.consume_outbound()
    assert message.channel == "test"
    assert expected in message.content
