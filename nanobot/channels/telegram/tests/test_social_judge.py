"""One-call intermediary wire contracts and provider configuration."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.channels.telegram.manifest import SETUP_SPEC
from nanobot.channels.telegram.social.db.schema import SCHEMA_VERSION
from nanobot.channels.telegram.social.expressions import ExpressionLibrary
from nanobot.channels.telegram.social.jargon import JargonLibrary
from nanobot.channels.telegram.social.judge import (
    JudgeInput,
    JudgeMessage,
    SocialJudge,
    make_judge_provider,
)
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_scheduler import remove_v4_columns
from nanobot.config.schema import (
    Config,
    InlineFallbackConfig,
    TelegramSocialConfig,
    TelegramSocialJudgeConfig,
)
from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage, ToolCallRequest
from nanobot.providers.openai_codex_provider import OpenAICodexProvider

A = ConversationScope("telegram", -1001, 53)


def decide(runtime, decision="wake", *, fulfills=False, error=False):
    provider = AsyncMock(spec=LLMProvider)
    provider.provider_name = "integration-provider"

    async def respond(**kwargs):
        if error:
            return LLMResponse(content=None, finish_reason="error")
        return LLMResponse(content=json.dumps({"decision": decision,
            "reason": "canned integration outcome", "fulfills_intent": fulfills}))

    provider.chat.side_effect = respond
    runtime.judging.set_provider(provider, "integration-provider")
    return provider


@pytest.fixture
def case():
    provider = AsyncMock(spec=LLMProvider)
    provider.provider_name = "unit-provider"
    provider.chat.return_value = LLMResponse(content='{"decision":"wake","reason":"called"}',
        usage=LLMUsage.estimated(input_tokens=30, output_tokens=10))
    settings = TelegramSocialJudgeConfig()
    data = JudgeInput(bot_name="肥鱼", bot_username="Deep_Fat_Fish_bot", nicknames=("肥鱼",),
        new=(JudgeMessage(seq=1, message_id=1, sender_id="member", name="Member", timestamp=1000, text="hello"),))
    return provider, SocialJudge(settings, provider, "unit"), data


def test_judge_defaults_roundtrip_through_existing_json_settings():
    config = TelegramSocialConfig()
    assert config.judge.model == "openai-codex/gpt-6-luna"
    assert config.judge.provider == "openai_codex"
    assert config.judge.reasoning_effort == "low"
    body = config.model_dump(by_alias=True)
    assert TelegramSocialConfig.model_validate(body) == config
    assert TelegramSocialConfig.model_validate(SETUP_SPEC.fields["social"].default).judge == config.judge
    assert "enabled" not in body["judge"]


@pytest.mark.parametrize("values", [
    {"maxRetries": 2}, {"maxRequestsPerHour": 0}, {"maxFallbacksPerHour": -1},
    {"timeoutSeconds": float("nan")}, {"timeoutSeconds": float("inf")},
    {"contextMessages": 101}, {"contextChars": 100}, {"model": "gpt-6-luna "},
    {"provider": ""}, {"reasoningEffort": "ultra"}, {"enabled": True},
    {"mediaMaxImages": 0}, {"mediaMaxImages": 9}, {"mediaMaxEdge": 63},
    {"mediaMaxEdge": 1537}, {"mediaTimeoutSeconds": float("nan")},
])
def test_judge_config_rejects_invalid_or_unsupported_settings(values):
    with pytest.raises(ValidationError):
        TelegramSocialJudgeConfig.model_validate(values)


def test_provider_reuses_oauth_without_main_fallback_or_config_mutation():
    config = Config()
    config.agents.defaults.fallback_models = [InlineFallbackConfig(model="main-secret", provider="openai")]
    before = config.model_dump()
    provider, values = make_judge_provider(config, TelegramSocialJudgeConfig())
    assert isinstance(provider, OpenAICodexProvider)
    assert provider.default_model == "openai-codex/gpt-6-luna"
    assert provider.generation.reasoning_effort == "low"
    assert provider._extra_body["text"]["format"]["strict"]
    assert isinstance(values, tuple)
    assert config.model_dump() == before


def test_judge_provider_uses_independent_persistent_oauth(tmp_path):
    provider, _ = make_judge_provider(Config(), TelegramSocialJudgeConfig(oauth_storage_dir=str(tmp_path)))
    assert isinstance(provider, OpenAICodexProvider)
    assert provider.token_storage is not None
    assert provider.token_storage._import_codex_cli is False
    assert provider.token_storage.get_token_path() == tmp_path / "auth" / "codex.json"


def test_public_defaults_do_not_advertise_retired_attention_settings():
    default = SETUP_SPEC.fields["social"].default
    assert "replyAttention" not in default
    assert default == {"enabled": False, "scopes": []}
    assert TelegramSocialConfig.model_validate(default).enabled is False


def test_v3b_judge_settings_change_preserves_the_active_scope_version_and_wait(tmp_path):
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
    from nanobot.config.schema import TelegramSocialScopeConfig

    config = TelegramSocialConfig(enabled=True, scopes=[TelegramSocialScopeConfig(chat_id=-1001, thread_id=53, role_id="friend")])
    runtime = TelegramSocialRuntime(config, tmp_path, MessageBus())
    try:
        before = runtime.store.scopes.state(A.session_key)["version"]
        with runtime.store.db:
            runtime.store.db.execute("UPDATE scopes SET mode='waiting',wake_at=2000,wake_config=? WHERE key=?",
                                     ('{"keywords":["ready"]}', A.session_key))
        changed = config.model_copy(deep=True)
        changed.judge.model = "openai-codex/gpt-6.1-sol"
        runtime.configure(changed)
        assert runtime.store.scopes.state(A.session_key)["version"] == before
        assert runtime.store.scopes.state(A.session_key)["mode"] == "waiting"
        assert runtime.store.scopes.state(A.session_key)["wake_at"] == 2000
        assert runtime.store.scopes.state(A.session_key)["wake_config"] == '{"keywords":["ready"]}'
    finally:
        runtime.close()



@pytest.mark.parametrize("content", ["not JSON", "{}", "[]", '```json\n{}\n```',
    '{"decision":"reply","reason":"no"}', '{"decision":"wake","reason":"ok","extra":true}',
    json.dumps({"decision":"wake","reason":"x"*121}), "", "x"*4097])
async def test_bad_decisions_are_errors_not_observe(case, content):
    provider, judge, data = case
    provider.chat.return_value = LLMResponse(content=content)
    assert await judge.assess(data) is None
    provider.chat.assert_awaited_once()


@pytest.mark.parametrize("response", [LLMResponse(content="{}", finish_reason="length"),
    LLMResponse(content="{}", tool_calls=[ToolCallRequest(id="x", name="tool", arguments={})])])
async def test_nonfinal_and_tool_responses_are_rejected(case, response):
    provider, judge, data = case
    provider.chat.return_value = response
    assert await judge.assess(data) is None
    provider.chat.assert_awaited_once()


async def test_context_topic_metadata_and_intent_are_shown_without_main_session(case):
    provider, judge, data = case
    result = await judge.assess(data)
    assert result.decision == "wake"
    request = provider.chat.call_args.kwargs
    assert json.loads(request["messages"][1]["content"]) == json.loads(data.prompt_json())
    assert request["tools"] is None and request["tool_choice"] == "none"
    assert request["temperature"] == 0 and request["reasoning_effort"] == "low"
    assert request["max_tokens"] == 512
    provider.chat_with_retry.assert_not_called()


@pytest.mark.parametrize("username,name", [("Deep_Fat_Fish_bot", "肥鱼"), ("another_bot", "群友")])
async def test_judge_system_identity_comes_from_target_not_peer_history(case, username, name):
    provider, judge, data = case
    data = data.model_copy(update={"bot_username": username, "bot_name": name})
    await judge.assess(data)
    identity = json.loads(provider.chat.call_args.kwargs["messages"][0]["content"].split("Runtime target bot identity: ")[1])
    assert identity["bot_username"] == username and identity["bot_name"] == name


async def test_cancelled_request_is_recorded(case, monkeypatch):
    provider, judge, data = case
    records = []
    monkeypatch.setattr("nanobot.channels.telegram.social.judge.record_llm_call", records.append)
    provider.chat.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await judge.assess(data)
    assert len(records) == 1 and records[0].finish_reason == "cancelled"


def test_schema_eight_upgrade_preserves_main_data(tmp_path):
    store = SocialStore(tmp_path)
    store.learning.attach_libraries(ExpressionLibrary(store), JargonLibrary(store))
    store.bind(A, "version1")
    for seq in range(1, 4):
        store.observe(ObservedMessage(A, seq, seq+10, "1", "Alice", 990.+seq, "text"), 500)
    remove_v4_columns(store.db)
    with store.db:
        for table in ("judge_attempts", "judge_batches", "judge_state"):
            store.db.execute(f"DROP TABLE {table}")
        store.db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
        store.db.execute("UPDATE actions SET message_id=json_extract(message_ids,'$[0]')")
        store.db.execute("ALTER TABLE actions DROP COLUMN message_ids")
        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
        store.db.execute("PRAGMA user_version=8")
    store.close()
    upgraded = SocialStore(tmp_path)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert upgraded.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert upgraded.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert upgraded.scopes.state(A.session_key)["read_through"] == 0
        assert upgraded.judge.through(A.session_key) == 3
    finally:
        upgraded.close()


async def test_model_payload_omits_internal_identity_and_revisions(case):
    provider, judge, data = case
    await judge.assess(data)
    body = json.loads(provider.chat.call_args.kwargs["messages"][1]["content"])
    assert not {"batch_id", "version", "scope", "expires", "quiet_due", "total_due"} & body.keys()
    assert all("revision" not in message for message in body["context"] + body["new"])
