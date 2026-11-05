"""Read and refresh the OAuth document owned by the host Antigravity CLI."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

DEFAULT_DIR = Path.home() / ".gemini" / "antigravity-cli"
TOKEN_FILE = "antigravity-oauth-token"
CLIENT_FILE = "nanobot-oauth-client.json"
LOCK_FILE = ".nanobot-antigravity.lock"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REFRESH_SKEW_SECONDS = 60


class AntigravityAuthError(RuntimeError):
    """Antigravity login is missing or its token could not be refreshed."""


@dataclass(frozen=True)
class AntigravityToken:
    access_token: str
    refresh_token: str
    expiry: datetime
    email: str | None


def antigravity_dir() -> Path:
    """Resolve the shared CLI credential directory without reading it."""
    directory = os.environ.get("ANTIGRAVITY_DIR")
    return Path(directory).expanduser() if directory else DEFAULT_DIR


def read_token(directory: Path) -> AntigravityToken:
    """Read CLI credentials, truncating RFC3339 nanoseconds to microseconds."""
    try:
        document: dict[str, Any] = json.loads((directory / TOKEN_FILE).read_text("utf-8"))
    except FileNotFoundError as exc:
        raise AntigravityAuthError("Run agy on the host to log in first.") from exc
    token = document["token"]
    if not token.get("access_token"):
        raise AntigravityAuthError("Run agy on the host to log in first.")
    expiry = re.sub(r"(\.\d{6})\d+", r"\1", token["expiry"])
    return AntigravityToken(
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token", ""),
        expiry=datetime.fromisoformat(expiry).astimezone(timezone.utc),
        email=document.get("id_token_email"),
    )


def get_access_token(
    directory: Path, *, rejected: str | None = None, client: httpx.Client | None = None
) -> str:
    """Reuse a fresh token or refresh and atomically replace it under a file lock."""
    with FileLock(directory / LOCK_FILE, timeout=15):
        token = read_token(directory)
        fresh = token.expiry > datetime.now(timezone.utc) + timedelta(seconds=REFRESH_SKEW_SECONDS)
        if fresh and (rejected is None or token.access_token != rejected):
            return token.access_token
        try:
            oauth_client: dict[str, Any] = json.loads((directory / CLIENT_FILE).read_text("utf-8"))
        except FileNotFoundError as exc:
            raise AntigravityAuthError(f"Antigravity OAuth client file is missing: {CLIENT_FILE}") from exc
        document: dict[str, Any] = json.loads((directory / TOKEN_FILE).read_text("utf-8"))
        with httpx.Client() if client is None else nullcontext(client) as http_client:
            response = http_client.post(
                TOKEN_ENDPOINT,
                data={
                    "client_id": oauth_client["client_id"],
                    "client_secret": oauth_client["client_secret"],
                    "refresh_token": token.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
        if response.status_code != 200:
            try:
                error = response.json().get("error", "unknown")
            except ValueError:
                error = "unknown"
            raise AntigravityAuthError(
                f"Antigravity token refresh failed: HTTP {response.status_code} {error}"
            )
        payload = response.json()
        document["token"].update(
            access_token=payload["access_token"],
            expiry=(datetime.now(timezone.utc) + timedelta(seconds=payload["expires_in"])).isoformat(),
            expires_in=payload["expires_in"],
        )
        if "refresh_token" in payload:
            document["token"]["refresh_token"] = payload["refresh_token"]
        fd, temporary = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / TOKEN_FILE)
        return payload["access_token"]


def login_status(directory: Path) -> dict[str, Any]:
    """Report local login metadata without making a network request."""
    try:
        token = read_token(directory)
    except AntigravityAuthError:
        return {"configured": False, "account": None, "expires_at": None}
    return {
        "configured": bool(token.refresh_token),
        "account": token.email,
        "expires_at": int(token.expiry.timestamp() * 1000),
    }
