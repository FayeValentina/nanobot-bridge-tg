"""Pytest shims for Codex's bwrap sandbox only. Never import from project code or conftest.

Loaded via PYTEST_ADDOPTS="-p codex_pytest_env" with PYTHONPATH=.agent when Claude dispatches
`codex exec` (see .agent/orchestration.md). Normal runs and the release pipeline never load it.

- The sandbox has no external DNS; one existing attachment test resolves example.com.
- The sandbox delays asyncio's cross-thread selector wakeups, stalling to_thread completions.
"""

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest


@pytest.fixture(autouse=True)
def codex_offline_example_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = socket.getaddrinfo

    def lookup(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        return resolve("93.184.216.34" if host == "example.com" else host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)


@pytest.fixture(autouse=True)
async def codex_responsive_event_loop() -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    timer: asyncio.TimerHandle | None = None

    def pulse() -> None:
        nonlocal timer
        timer = loop.call_later(0.05, pulse)

    pulse()
    try:
        yield
    finally:
        await loop.shutdown_default_executor()
        if timer is not None:
            timer.cancel()
