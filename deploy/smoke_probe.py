"""Credential-safe packaged WebUI and authentication probe; never calls an LLM."""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def probe(health_only: bool = False) -> None:
    with urlopen("http://127.0.0.1:18790/health", timeout=3) as response:
        assert json.load(response).get("ready") is True, "gateway is not ready"
    if health_only:
        return
    with urlopen("http://127.0.0.1:8765/", timeout=3) as response:
        assert response.status == 200 and b"<html" in response.read().lower()
    endpoint = "http://127.0.0.1:8765/webui/bootstrap"
    try:
        with urlopen(endpoint, timeout=3):
            raise AssertionError("bootstrap accepted an unauthenticated request")
    except HTTPError as error:
        assert error.code in (401, 403), "unexpected unauthenticated response"
    config = json.loads(Path("/data/config.json").read_text())
    ws = config["channels"]["websocket"]
    secret = ws.get("tokenIssueSecret") or ws.get("token")
    assert secret, "WebUI authentication is missing"
    request = Request(endpoint, headers={"Authorization": "Bearer " + secret})
    with urlopen(request, timeout=3) as response:
        assert response.status == 200
        assert json.load(response).get("token"), "bootstrap did not issue a token"
    import telegram  # noqa: F401

    import nanobot

    assert "/opt/venv/" in str(nanobot.__file__), "runtime is importing the source checkout"
    print("PASS: wheel import, Telegram dependency, WebUI, authentication and health")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--health-only", action="store_true")
    probe(parser.parse_args().health_only)
