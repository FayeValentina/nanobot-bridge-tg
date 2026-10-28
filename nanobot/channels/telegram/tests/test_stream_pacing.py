"""Slow Telegram calls must not replay every queued model fragment."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter, TimedOut

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import runtime as telegram_runtime
from nanobot.channels.telegram.runtime import TelegramChannel, TelegramConfig


@pytest.fixture
def paced(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(telegram_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    channel = TelegramChannel(TelegramConfig(stream_edit_interval=0.1), MessageBus())
    channel._app = SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        edit_message_text=AsyncMock(),
    ))
    channel._app_ready.set()
    return channel, clock


async def test_slow_initial_send_coalesces_already_queued_fragments(paced):
    channel, clock = paced

    async def slow_send(**kwargs):
        clock[0] += 2
        return SimpleNamespace(message_id=1)

    channel._app.bot.send_message.side_effect = slow_send
    async def slow_edit(**kwargs):
        clock[0] += 2

    channel._app.bot.edit_message_text.side_effect = slow_edit
    await channel.send_delta("-100123", "first", stream_id="s")
    for _ in range(40):
        await channel.send_delta("-100123", "x", stream_id="s")
    channel._app.bot.edit_message_text.assert_not_awaited()
    await channel.send_delta("-100123", "", stream_id="s", stream_end=True)
    channel._app.bot.edit_message_text.assert_awaited_once()
    assert channel._app.bot.edit_message_text.await_args.kwargs["text"] == "first" + "x" * 40


async def test_slow_preview_edit_does_not_trigger_immediate_next_edit(paced):
    channel, clock = paced
    await channel.send_delta("123", "first", stream_id="s")

    async def slow_edit(**kwargs):
        clock[0] += 2

    channel._app.bot.edit_message_text.side_effect = slow_edit
    clock[0] += 4
    await channel.send_delta("123", " second", stream_id="s")
    await channel.send_delta("123", " third", stream_id="s")
    assert channel._app.bot.edit_message_text.await_count == 1
    assert channel._stream_bufs["123"].text == "first second third"


async def test_group_preview_has_a_safe_floor_even_with_fast_config(paced):
    channel, clock = paced
    await channel.send_delta("-100123", "first", stream_id="s")
    clock[0] += 1
    await channel.send_delta("-100123", " second", stream_id="s")
    channel._app.bot.edit_message_text.assert_not_awaited()
    clock[0] += 3
    await channel.send_delta("-100123", " third", stream_id="s")
    channel._app.bot.edit_message_text.assert_awaited_once()


async def test_preview_flood_wait_buffers_latest_text_without_sleeping(paced, monkeypatch):
    channel, clock = paced
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_runtime.asyncio, "sleep", sleep)
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    await channel.send_delta("-100123", "first", stream_id="s")
    channel._app.bot.edit_message_text.side_effect = [RetryAfter(timedelta(seconds=34)), None]
    clock[0] += 4
    await channel.send_delta("-100123", " second", stream_id="s")
    sleep.assert_not_awaited()
    assert channel._app.bot.edit_message_text.await_count == 1
    clock[0] += 1
    await channel.send_delta("-100123", " third", stream_id="s")
    assert channel._app.bot.edit_message_text.await_count == 1
    await channel.send_delta("-100123", "", stream_id="s", stream_end=True)
    sleep.assert_awaited_once_with(33)
    assert channel._app.bot.edit_message_text.await_count == 2
    assert channel._app.bot.edit_message_text.await_args.kwargs["text"] == "first second third"


async def test_preview_timeout_does_not_sleep_or_duplicate_delta(paced, monkeypatch):
    channel, clock = paced
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_runtime.asyncio, "sleep", sleep)
    await channel.send_delta("123", "first", stream_id="s")
    channel._app.bot.edit_message_text.side_effect = [TimedOut(), None]
    clock[0] += 4
    await channel.send_delta("123", " second", stream_id="s")
    sleep.assert_not_awaited()
    assert channel._app.bot.edit_message_text.await_count == 1
    await channel.send_delta("123", "", stream_id="s", stream_end=True)
    assert channel._app.bot.edit_message_text.await_args.kwargs["text"] == "first second"
