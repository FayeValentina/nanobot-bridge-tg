"""M0 probes of ordinary Telegram delivery limits; see docs/M0_CONTRACTS.md."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TimedOut

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.runtime import TelegramChannel, TelegramConfig


async def test_text_send_does_not_return_platform_receipt() -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=81)))
    channel._app = SimpleNamespace(bot=bot)
    channel._wait_for_app = AsyncMock(return_value=channel._app)
    outbound = OutboundMessage(
        channel="telegram", chat_id="-1001", content="M0 public message",
        metadata={"message_thread_id": 42},
    )
    result = await channel.send(outbound)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["message_thread_id"] == 42
    assert result is None
    assert "telegram_message_id" not in outbound.metadata


async def test_ordinary_timeout_retry_can_repeat_an_accepted_send(monkeypatch) -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    accepted = []

    async def send_message(**kwargs):
        # The platform accepts the first send, but its response is lost.
        accepted.append(kwargs)
        if len(accepted) == 1:
            raise TimedOut("response lost after acceptance")
        return SimpleNamespace(message_id=len(accepted))

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    monkeypatch.setattr("nanobot.channels.telegram.runtime._SEND_RETRY_BASE_DELAY", 0)
    await channel._send_text(-1001, "M0 public message", thread_kwargs={"message_thread_id": 42})
    assert len(accepted) == 2
    assert accepted[0] == accepted[1]
