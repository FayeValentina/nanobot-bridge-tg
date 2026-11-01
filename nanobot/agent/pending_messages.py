"""Bounded pending-message draining and waiting for background completions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.agent.runner import _MAX_INJECTIONS_PER_TURN
from nanobot.bus.events import InboundMessage


class PendingMessageBuffer:
    def __init__(
        self, queue: asyncio.Queue[InboundMessage] | None, *,
        convert: Callable[[InboundMessage], Awaitable[dict[str, Any]]],
        has_running_work: Callable[[], bool], session_key: str | None,
        terminal_wait_seconds: float,
    ) -> None:
        self._queue = queue
        self._convert = convert
        self._has_running_work = has_running_work
        self._session_key = session_key
        self._terminal_wait_seconds = terminal_wait_seconds
        self._deadline: float | None = None

    async def drain(
        self, *, limit: int = _MAX_INJECTIONS_PER_TURN,
        first_msg: InboundMessage | None = None,
    ) -> list[dict[str, Any]]:
        if self._queue is None:
            return []
        items: list[dict[str, Any]] = []
        if first_msg is not None:
            items.append(await self._convert(first_msg))
        while len(items) < limit:
            try:
                message = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            items.append(await self._convert(message))
        return items

    async def wait(self, *, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
        items = await self.drain(limit=limit)
        if items or self._queue is None or not self._has_running_work():
            return items
        now = asyncio.get_running_loop().time()
        if self._deadline is None:
            self._deadline = now + self._terminal_wait_seconds
        remaining = self._deadline - now
        if remaining <= 0:
            return []
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for sub-agent completion before session {} exits", self._session_key)
            return []
        return await self.drain(limit=limit, first_msg=message)
