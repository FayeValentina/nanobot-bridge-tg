"""Offline intermediary defaults for channel tests; semantic outcomes are explicit fakes."""

import json
from unittest.mock import AsyncMock

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse


@pytest.fixture(autouse=True)
def offline_intermediary(monkeypatch):
    def factory(config, settings):
        provider = AsyncMock(spec=LLMProvider)

        async def decide(**kwargs):
            content = kwargs["messages"][1]["content"]
            data = json.loads(content[0]["text"] if isinstance(content, list) else content)
            fresh = [message for message in data["messages"] if message["seq"] in data["new_seqs"]]
            intent = data.get("intent") or {}
            preferences = intent or data.get("participation", {})
            keys = preferences.get("keywords", [])
            senders = preferences.get("senders", [])
            selected = [message for message in fresh if message["mentions_bot"] or any(
                name.casefold() in message["text"].casefold() for name in data["nicknames"])]
            matches = [message for message in fresh if any(key.casefold() in message["text"].casefold() for key in keys)
                       or message["sender_id"] in senders]
            if not keys and not senders and (intent or preferences.get("on_messages")) and data["mode"] != "sleeping":
                matches = fresh
            selected = selected or matches
            if data.get("opportunity"):
                selected = [message for message in data["messages"] if not message["is_self"]][-1:]
            sources = [message["seq"] for message in selected]
            return LLMResponse(content=json.dumps({
                "decision": "wake" if sources or data.get("opportunity") else "observe", "through_seq": data["through_seq"],
                "source_seqs": sources, "reason": "offline canned decision", "fulfills_intent": bool(matches and intent),
            }))

        provider.chat.side_effect = decide
        return provider, "offline-test-provider"

    monkeypatch.setattr("nanobot.channels.telegram.social.judge_runtime.make_judge_provider", factory)
