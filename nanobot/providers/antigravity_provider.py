"""Gemini native chat through the host Antigravity CLI OAuth credentials."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx

from nanobot.providers.antigravity_auth import antigravity_dir, get_access_token
from nanobot.providers.base import LLMProvider, LLMResponse, resolve_stream_idle_timeout_s
from nanobot.providers.gemini_wire import GeminiResponseAccumulator, build_request
from nanobot.providers.openai_responses.parsing import iter_sse

BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"
USER_AGENT = "antigravity/1.5.0 linux/arm64"
CLIENT_METADATA = {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
THINKING_LEVELS = ("low", "medium", "high")
NO_THINKING_EFFORTS = ("none", "minimal")
THINKING_MAX_OUTPUT_TOKENS = 65536

_PROJECTS: dict[str, str] = {}
_CATALOGS: dict[str, frozenset[str]] = {}


class AntigravityHTTPError(RuntimeError):
    def __init__(self, status_code: int, error_type: str | None, message: str):
        super().__init__(f"Antigravity HTTP {status_code}: {message}")
        self.status_code = status_code
        self.error_type = error_type

    @classmethod
    def from_response(cls, response: httpx.Response) -> AntigravityHTTPError:
        try:
            error = response.json()["error"]
            error_type, message = error["status"], error["message"]
        except (ValueError, KeyError, TypeError):
            error_type, message = None, response.text[:300]
        return cls(response.status_code, error_type, message)


def resolve_model(
    requested: str, catalog: frozenset[str], reasoning_effort: str | None
) -> tuple[str, str | None]:
    effort_level = (
        reasoning_effort if reasoning_effort in THINKING_LEVELS
        else "low" if reasoning_effort in NO_THINKING_EFFORTS else None
    )
    if requested in catalog:
        return requested, effort_level
    match = re.fullmatch(r"(gemini-[0-9.]+-flash)-(low|medium|high)", requested)
    if match and f"{match[1]}-tiered" in catalog:
        return f"{match[1]}-tiered", effort_level or match[2]
    return requested, None


class AntigravityProvider(LLMProvider):
    def __init__(self, default_model: str, *, provider_name: str = "antigravity"):
        super().__init__(api_key=None, api_base=None, provider_name=provider_name)
        self.default_model = default_model
        self._directory = antigravity_dir()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._call(
            messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice, None, None
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        return await self._call(
            messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice,
            on_content_delta, on_thinking_delta,
        )

    def get_default_model(self) -> str:
        return self.default_model

    async def _post_json(
        self, client: httpx.AsyncClient, token: str, method: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        headers = {
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        response = await client.post(f"{BASE_URL}:{method}", json=body, headers=headers)
        if response.status_code == 401:
            token = await asyncio.to_thread(get_access_token, self._directory, rejected=token)
            headers["Authorization"] = f"Bearer {token}"
            response = await client.post(f"{BASE_URL}:{method}", json=body, headers=headers)
        if response.status_code != 200:
            raise AntigravityHTTPError.from_response(response)
        return response.json(), token

    async def _call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        on_content_delta: Callable[[str], Awaitable[None]] | None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        requested = (model or self.default_model).removeprefix("antigravity/")
        async with httpx.AsyncClient(timeout=resolve_stream_idle_timeout_s()) as client:
            token = await asyncio.to_thread(get_access_token, self._directory)
            key = str(self._directory)
            if key not in _PROJECTS:
                payload, token = await self._post_json(
                    client, token, "loadCodeAssist", {"metadata": CLIENT_METADATA}
                )
                _PROJECTS[key] = payload["cloudaicompanionProject"]
            project = _PROJECTS[key]
            if key not in _CATALOGS:
                payload, token = await self._post_json(
                    client, token, "fetchAvailableModels", {"project": project}
                )
                _CATALOGS[key] = frozenset(payload["models"])
            wire_model, level = resolve_model(requested, _CATALOGS[key], reasoning_effort)
            request = build_request(
                self._sanitize_empty_content(messages), tools, tool_choice,
                max_output_tokens=max(max_tokens, THINKING_MAX_OUTPUT_TOKENS) if level else max_tokens,
                temperature=temperature, thinking_level=level,
            )
            body = {
                "project": project, "model": wire_model, "user_prompt_id": str(uuid4()),
                "requestType": "CHAT", "userAgent": "antigravity", "requestId": str(uuid4()),
                "request": request,
            }
            accumulator = GeminiResponseAccumulator()
            for attempt in range(2):
                async with client.stream(
                    "POST", f"{BASE_URL}:streamGenerateContent?alt=sse", json=body, headers={
                        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                        "User-Agent": USER_AGENT, "Accept": "text/event-stream",
                    },
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        token = await asyncio.to_thread(
                            get_access_token, self._directory, rejected=token
                        )
                        continue
                    if response.status_code != 200:
                        await response.aread()
                        raise AntigravityHTTPError.from_response(response)
                    async for event in iter_sse(response):
                        for kind, delta in accumulator.feed(event["response"]):
                            if kind == "content" and on_content_delta is not None:
                                await on_content_delta(delta)
                            elif kind == "thinking" and on_thinking_delta is not None:
                                await on_thinking_delta(delta)
                    break
            return accumulator.response()
