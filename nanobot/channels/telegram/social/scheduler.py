"""One event-driven worker; SQLite owns deadlines, never an agent turn."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from loguru import logger

from nanobot.channels.telegram.social.store import SocialStore


class SocialScheduler:
    def __init__(self, store: SocialStore, run_due: Callable[[], Awaitable[None]]) -> None:
        self.store = store
        self.run_due = run_due
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="telegram-social-scheduler")

    def notify(self) -> None:
        self.changed.set()

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    async def stop(self) -> None:
        self.cancel()
        if self.task is not None:
            with suppress(asyncio.CancelledError):
                await self.task
        self.task = None

    async def _run(self) -> None:
        while True:
            self.changed.clear()
            try:
                await self.run_due()
                due = self.store.next_due()
                delay = max(0.01, min(60, due - self.store.now())) if due is not None else 60
            except Exception:
                logger.exception("Telegram social scheduler cycle failed")
                delay = 5
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=delay)
