"""Antigravity credential tests using temporary files and mock HTTP only."""

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from nanobot.providers.antigravity_auth import (
    CLIENT_FILE,
    DEFAULT_DIR,
    LOCK_FILE,
    REFRESH_SKEW_SECONDS,
    TOKEN_ENDPOINT,
    TOKEN_FILE,
    AntigravityAuthError,
    antigravity_dir,
    get_access_token,
    login_status,
    read_token,
)


def _write_token(directory: Path, seconds: int = 3600) -> dict:
    document = {
        "auth_method": "consumer",
        "id_token_email": "user@example.com",
        "other": {"preserve": True},
        "token": {
            "access_token": "old-access",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expiry": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
            "expires_in": 3600,
            "extra": "preserve-token-field",
        },
    }
    (directory / TOKEN_FILE).write_text(json.dumps(document), encoding="utf-8")
    return document


def _write_client(directory: Path) -> None:
    (directory / CLIENT_FILE).write_text(
        json.dumps({"client_id": "test-client", "client_secret": "client-secret"}),
        encoding="utf-8",
    )


def _no_request(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"Unexpected request: {request.method} {request.url}")


def test_directory_override_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTIGRAVITY_DIR", raising=False)
    assert antigravity_dir() == DEFAULT_DIR
    monkeypatch.setenv("ANTIGRAVITY_DIR", "~/test-agy")
    assert antigravity_dir() == Path.home() / "test-agy"


@pytest.mark.parametrize("rejected", [None, "previous-access"])
def test_fresh_token_reused_without_http(tmp_path: Path, rejected: str | None) -> None:
    original = _write_token(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(_no_request)) as client:
        assert get_access_token(tmp_path, rejected=rejected, client=client) == "old-access"
        assert not client.is_closed
    assert json.loads((tmp_path / TOKEN_FILE).read_text()) == original


@pytest.mark.parametrize(
    ("seconds", "rejected"),
    [(-10, None), (REFRESH_SKEW_SECONDS - 10, None), (3600, "old-access"), (-10, "previous-access")],
)
@pytest.mark.parametrize("rotate", [False, True])
def test_refresh_form_and_atomic_document_preservation(
    tmp_path: Path, seconds: int, rejected: str | None, rotate: bool
) -> None:
    original = _write_token(tmp_path, seconds)
    _write_client(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == TOKEN_ENDPOINT
        assert parse_qs(request.content.decode()) == {
            "client_id": ["test-client"],
            "client_secret": ["client-secret"],
            "refresh_token": ["refresh-secret"],
            "grant_type": ["refresh_token"],
        }
        assert request.extensions["timeout"] == dict.fromkeys(["connect", "read", "write", "pool"], 30)
        assert (tmp_path / LOCK_FILE).exists()
        assert json.loads((tmp_path / TOKEN_FILE).read_text()) == original
        payload = {"access_token": "new-access", "expires_in": 1800}
        if rotate:
            payload["refresh_token"] = "new-refresh"
        return httpx.Response(200, json=payload)

    before = datetime.now(timezone.utc)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert get_access_token(tmp_path, rejected=rejected, client=client) == "new-access"
        assert not client.is_closed
    after = datetime.now(timezone.utc)

    assert len(requests) == 1
    saved = json.loads((tmp_path / TOKEN_FILE).read_text())
    token = read_token(tmp_path)
    assert before + timedelta(seconds=1800) <= token.expiry <= after + timedelta(seconds=1800)
    assert token.expiry.tzinfo == timezone.utc
    assert token.access_token == "new-access"
    assert token.refresh_token == ("new-refresh" if rotate else "refresh-secret")
    expected = original
    expected["token"].update(
        access_token="new-access", expiry=token.expiry.isoformat(), expires_in=1800,
        refresh_token=token.refresh_token,
    )
    assert saved == expected
    assert stat.S_IMODE((tmp_path / TOKEN_FILE).stat().st_mode) == 0o600
    assert {path.name for path in tmp_path.iterdir()} == {TOKEN_FILE, CLIENT_FILE, LOCK_FILE}


def test_owned_http_client_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_token(tmp_path, -10)
    _write_client(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    ))
    monkeypatch.setattr("nanobot.providers.antigravity_auth.httpx.Client", lambda: client)
    assert get_access_token(tmp_path) == "new"
    assert client.is_closed


@pytest.mark.parametrize("json_error", [True, False])
def test_refresh_error_does_not_disclose_secrets(tmp_path: Path, json_error: bool) -> None:
    original = _write_token(tmp_path, -10)
    _write_client(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if json_error:
            return httpx.Response(400, json={
                "error": "invalid_grant",
                "error_description": "refresh-secret client-secret old-access",
            })
        return httpx.Response(400, text="refresh-secret client-secret old-access")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AntigravityAuthError) as caught:
            get_access_token(tmp_path, client=client)
    assert str(caught.value) == (
        "Antigravity token refresh failed: HTTP 400 " + ("invalid_grant" if json_error else "unknown")
    )
    for secret in ["refresh-secret", "client-secret", "old-access"]:
        assert secret not in str(caught.value)
    assert json.loads((tmp_path / TOKEN_FILE).read_text()) == original


def test_expired_token_without_client_file(tmp_path: Path) -> None:
    _write_token(tmp_path, -10)
    with httpx.Client(transport=httpx.MockTransport(_no_request)) as client:
        with pytest.raises(AntigravityAuthError, match=CLIENT_FILE):
            get_access_token(tmp_path, client=client)


def test_nanosecond_expiry_is_truncated_and_converted_to_utc(tmp_path: Path) -> None:
    document = _write_token(tmp_path)
    document["token"]["expiry"] = "2026-11-05T11:35:30.400902766+09:00"
    (tmp_path / TOKEN_FILE).write_text(json.dumps(document))
    token = read_token(tmp_path)
    assert token.expiry == datetime(2026, 11, 5, 2, 35, 30, 400902, tzinfo=timezone.utc)
    assert token.email == "user@example.com"


@pytest.mark.parametrize("missing_file", [False, True])
def test_missing_login_prompts_host_agy_and_reports_unconfigured(tmp_path: Path, missing_file: bool) -> None:
    if not missing_file:
        document = _write_token(tmp_path)
        document["token"]["access_token"] = ""
        (tmp_path / TOKEN_FILE).write_text(json.dumps(document))
    with pytest.raises(AntigravityAuthError, match="agy.*host|host.*agy"):
        read_token(tmp_path)
    assert login_status(tmp_path) == {"configured": False, "account": None, "expires_at": None}


@pytest.mark.parametrize("refresh", ["refresh-secret", ""])
def test_login_status_uses_refresh_token_and_local_metadata(tmp_path: Path, refresh: str) -> None:
    document = _write_token(tmp_path, -10)
    document["token"]["refresh_token"] = refresh
    (tmp_path / TOKEN_FILE).write_text(json.dumps(document))
    assert login_status(tmp_path) == {
        "configured": bool(refresh),
        "account": "user@example.com",
        "expires_at": int(read_token(tmp_path).expiry.timestamp() * 1000),
    }
    assert not (tmp_path / LOCK_FILE).exists()
