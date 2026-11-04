"""Offline intermediary defaults for channel tests; semantic outcomes are explicit fakes."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse


@pytest.fixture(autouse=True)
def offline_intermediary(monkeypatch):
    def factory(config, settings):
        provider = AsyncMock(spec=LLMProvider)
        provider.provider_name = "offline-test-provider"

        async def decide(**kwargs):
            content = kwargs["messages"][1]["content"]
            data = json.loads(content[0]["text"] if isinstance(content, list) else content)
            fresh = data["new"]
            intent = data.get("intent") or {}
            preferences = intent
            keys = preferences.get("keywords", [])
            senders = preferences.get("senders", [])
            selected = [message for message in fresh if message["mentions_bot"] or any(
                name.casefold() in message["text"].casefold() for name in data["nicknames"])]
            matches = [message for message in fresh if any(key.casefold() in message["text"].casefold() for key in keys)
                       or message["sender_id"] in senders]
            if not keys and not senders and intent:
                matches = fresh
            selected = selected or matches
            return LLMResponse(content=json.dumps({
                "decision": "wake" if selected else "observe", "reason": "offline canned decision",
                "fulfills_intent": bool(matches and intent),
            }))

        provider.chat.side_effect = decide
        return provider, "offline-test-provider"

    monkeypatch.setattr("nanobot.channels.telegram.social.judge.make_judge_provider", factory)


@pytest.fixture(autouse=True)
def isolated_judge_usage(monkeypatch):
    monkeypatch.setattr("nanobot.channels.telegram.social.judge.record_llm_call", Mock())
