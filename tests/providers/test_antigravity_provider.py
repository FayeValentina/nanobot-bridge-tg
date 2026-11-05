"""Antigravity integration with temporary credentials and mock HTTP only."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.providers import antigravity_provider as agy
from nanobot.providers.antigravity_auth import TOKEN_FILE
from nanobot.providers.base import LLMUsage, ToolCallRequest, resolve_stream_idle_timeout_s
from nanobot.providers.factory import make_provider
from nanobot.providers.registry import find_by_name
from nanobot.security.credentials import protected_runtime_paths
from nanobot.webui.settings_models import oauth_provider_status

MODEL = "antigravity/gemini-3.8-flash-medium"
WIRE_MODEL = "gemini-3.8-flash-tiered"
METHODS = ["loadCodeAssist", "fetchAvailableModels", "streamGenerateContent"]


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agy._PROJECTS.clear()  # pyright: ignore[reportPrivateUsage] -- required cache isolation
    agy._CATALOGS.clear()  # pyright: ignore[reportPrivateUsage] -- required cache isolation
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path))

    def token(directory: Path, *, rejected: str | None = None) -> str:
        return "old-token"

    monkeypatch.setattr(agy, "get_access_token", token)


def _mock_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    original_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs == {"timeout": resolve_stream_idle_timeout_s()}
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(agy.httpx, "AsyncClient", fake_client)


def _method(request: httpx.Request) -> str:
    return request.url.path.rsplit(":", 1)[1]


def _success(request: httpx.Request) -> httpx.Response:
    if _method(request) == "loadCodeAssist":
        return httpx.Response(200, json={"cloudaicompanionProject": "project-1"})
    if _method(request) == "fetchAvailableModels":
        return httpx.Response(200, json={"models": {WIRE_MODEL: {"displayName": "Flash"}}})
    events = [
        {"response": {"candidates": [{"content": {"parts": [
            {"text": "think", "thought": True}, {"text": "hello "},
        ]}}]}, "metadata": {}, "traceId": "trace-1"},
        {"response": {
            "candidates": [{"content": {"parts": [
                {"text": "world"},
                {"functionCall": {"name": "search", "args": {"q": "cats"}, "id": "call-1"},
                 "thoughtSignature": "sig-1"},
            ]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4,
                              "thoughtsTokenCount": 2, "totalTokenCount": 16,
                              "cachedContentTokenCount": 3},
        }},
    ]
    return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))


@pytest.mark.parametrize(("requested", "catalog", "effort", "expected"), [
    (WIRE_MODEL, {WIRE_MODEL}, "high", (WIRE_MODEL, "high")),
    (WIRE_MODEL, {WIRE_MODEL}, "none", (WIRE_MODEL, "low")),
    (WIRE_MODEL, {WIRE_MODEL}, "minimal", (WIRE_MODEL, "low")),
    (WIRE_MODEL, {WIRE_MODEL}, "invalid", (WIRE_MODEL, None)),
    (WIRE_MODEL, {WIRE_MODEL}, None, (WIRE_MODEL, None)),
    ("gemini-3.8-flash-medium", {WIRE_MODEL}, "high", (WIRE_MODEL, "high")),
    ("gemini-3.8-flash-medium", {WIRE_MODEL}, "low", (WIRE_MODEL, "low")),
    ("gemini-3.8-flash-medium", {WIRE_MODEL}, "none", (WIRE_MODEL, "low")),
    ("gemini-3.8-flash-medium", {WIRE_MODEL}, "minimal", (WIRE_MODEL, "low")),
    ("gemini-3.8-flash-medium", {WIRE_MODEL}, None, (WIRE_MODEL, "medium")),
    ("gemini-3.8-flash-medium", {WIRE_MODEL}, "invalid", (WIRE_MODEL, "medium")),
    ("gemini-3.8-flash-low", {WIRE_MODEL}, None, (WIRE_MODEL, "low")),
    ("gemini-3.8-flash-high", {WIRE_MODEL}, None, (WIRE_MODEL, "high")),
    ("gemini-3.8-flash-high", {WIRE_MODEL}, "low", (WIRE_MODEL, "low")),
    ("gemini-3.8-flash-medium", {"gemini-3.8-flash-medium", WIRE_MODEL}, "low",
     ("gemini-3.8-flash-medium", "low")),
    ("gemini-3.8-flash-medium", set[str](), "high", ("gemini-3.8-flash-medium", None)),
    ("unknown", {WIRE_MODEL}, "high", ("unknown", None)),
])
def test_resolve_model(
    requested: str, catalog: set[str], effort: str | None, expected: tuple[str, str | None]
) -> None:
    assert agy.resolve_model(requested, frozenset(catalog), effort) == expected


async def test_full_stream_and_cached_second_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer old-token"
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["User-Agent"] == agy.USER_AGENT
        return _success(request)

    _mock_http(monkeypatch, handler)
    deltas: list[tuple[str, str]] = []

    async def content(delta: str) -> None:
        deltas.append(("content", delta))

    async def thinking(delta: str) -> None:
        deltas.append(("thinking", delta))

    provider = agy.AntigravityProvider("unused")
    result = await provider.chat_stream(
        [{"role": "user", "content": "hi"}], model=MODEL, max_tokens=1000,
        temperature=0.2, reasoning_effort="high", on_content_delta=content,
        on_thinking_delta=thinking,
    )
    assert [_method(r) for r in requests] == METHODS
    assert json.loads(requests[0].content) == {"metadata": agy.CLIENT_METADATA}
    assert json.loads(requests[1].content) == {"project": "project-1"}
    stream = requests[2]
    assert str(stream.url) == f"{agy.BASE_URL}:streamGenerateContent?alt=sse"
    assert stream.headers["Accept"] == "text/event-stream"
    body = json.loads(stream.content)
    assert body["model"] == WIRE_MODEL
    assert body["project"] == "project-1"
    assert UUID(body["user_prompt_id"]).version == 4
    assert UUID(body["requestId"]).version == 4
    assert body["requestType"] == "CHAT"
    assert body["userAgent"] == "antigravity"
    assert body["request"]["generationConfig"] == {
        "maxOutputTokens": 65536, "temperature": 0.2,
        "thinkingConfig": {"thinkingLevel": "high", "includeThoughts": True},
    }
    assert deltas == [("thinking", "think"), ("content", "hello "), ("content", "world")]
    assert result.content == "hello world"
    assert result.reasoning_content == "think"
    assert result.tool_calls == [ToolCallRequest(
        id="call-1", name="search", arguments={"q": "cats"},
        extra_content={"google": {"thought_signature": "sig-1"}},
    )]
    assert result.usage == LLMUsage.reported(
        input_tokens=10, output_tokens=6, total_tokens=16, cache_read_tokens=3,
    )
    second = await agy.AntigravityProvider(MODEL).chat([{"role": "user", "content": "again"}])
    assert second.content == result.content
    assert [_method(r) for r in requests] == [*METHODS, "streamGenerateContent"]


@pytest.mark.parametrize("method", METHODS)
async def test_401_refresh_propagates_token(
    method: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens: list[str | None] = []
    requests: list[httpx.Request] = []

    def token(directory: Path, *, rejected: str | None = None) -> str:
        assert directory == tmp_path
        tokens.append(rejected)
        return "new-token" if rejected else "old-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if _method(request) == method and request.headers["Authorization"] == "Bearer old-token":
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED", "message": "expired"}})
        return _success(request)

    monkeypatch.setattr(agy, "get_access_token", token)
    _mock_http(monkeypatch, handler)
    result = await agy.AntigravityProvider(MODEL).chat([])
    assert result.content == "hello world"
    assert tokens == [None, "old-token"]
    start = METHODS.index(method)
    assert [_method(r) for r in requests] == [*METHODS[:start], method, *METHODS[start:]]
    assert all(r.headers["Authorization"] == "Bearer new-token" for r in requests[start + 1:])


@pytest.mark.parametrize("method", METHODS)
async def test_quota_error_converted_by_base(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _method(request) == method:
            return httpx.Response(429, json={"error": {
                "code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
            }})
        return _success(request)

    _mock_http(monkeypatch, handler)
    # Use the brief's no-retry base entry point to verify exception conversion once.
    result = await agy.AntigravityProvider(MODEL)._safe_chat_stream(messages=[])  # pyright: ignore[reportPrivateUsage]
    assert result.finish_reason == "error"
    assert result.error_status_code == 429
    assert result.error_type == "RESOURCE_EXHAUSTED"
    assert result.content == "Error calling LLM: Antigravity HTTP 429: quota"


@pytest.mark.parametrize("method", METHODS)
async def test_non_json_http_error_body_bounded(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="x" * 400) if _method(request) == method else _success(request)

    _mock_http(monkeypatch, handler)
    with pytest.raises(agy.AntigravityHTTPError) as caught:
        await agy.AntigravityProvider(MODEL).chat([])
    assert caught.value.status_code == 404
    assert caught.value.error_type is None
    assert str(caught.value) == f"Antigravity HTTP 404: {'x' * 300}"


def test_factory_auto_provider() -> None:
    preset = ModelPresetConfig(model=MODEL, provider="auto")
    provider = make_provider(Config(), preset=preset)
    assert isinstance(provider, agy.AntigravityProvider)
    assert provider.get_default_model() == MODEL
    assert provider.provider_name == "antigravity"


@pytest.mark.parametrize("configured", [False, True])
def test_oauth_status(tmp_path: Path, configured: bool) -> None:
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    if configured:
        (tmp_path / TOKEN_FILE).write_text(json.dumps({
            "id_token_email": "user@example.com",
            "token": {"access_token": "access", "refresh_token": "refresh",
                      "expiry": expiry.isoformat()},
        }))
    assert oauth_provider_status(find_by_name("antigravity")) == {
        "configured": configured,
        "account": "user@example.com" if configured else None,
        "expires_at": int(expiry.timestamp() * 1000) if configured else None,
        "login_supported": False,
    }


def test_credential_directory_protected(tmp_path: Path) -> None:
    assert tmp_path.resolve() in protected_runtime_paths()
