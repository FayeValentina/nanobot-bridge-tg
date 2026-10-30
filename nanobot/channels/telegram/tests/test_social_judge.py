"""V3-A intermediary contracts, durable budgets and isolated provider calls."""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.channels.telegram.manifest import SETUP_SPEC
from nanobot.channels.telegram.social.judge import SocialJudge, make_judge_provider
from nanobot.channels.telegram.social.judge_models import (
    JudgeDecision,
    JudgeIntent,
    JudgeMessage,
    JudgeReply,
    JudgeSnapshot,
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
B = ConversationScope("telegram", -1001, 54)


def output(*, decision="wake", through=3, sources=None, reason="Directly calls the bot"):
    return json.dumps({"decision": decision, "through_seq": through,
                       "source_seqs": ([3] if decision == "wake" else []) if sources is None else sources,
                       "reason": reason}, ensure_ascii=False)


@pytest.fixture
def case(tmp_path):
    now = [1000.0]
    store = SocialStore(tmp_path, now=lambda: now[0])
    store.bind(A, "version1")
    messages = (
        JudgeMessage(seq=1, message_id=11, sender_id="1", name="Alice", timestamp=990.0, text="准备好了"),
        JudgeMessage(seq=2, message_id=12, sender_id="bot", name="肥鱼", timestamp=991.0,
                     text="准备了什么呀？", is_self=True),
        JudgeMessage(seq=3, message_id=13, sender_id="1", name="Alice", timestamp=999.0,
                     text="你猜猜我给你准备了什么", reply=JudgeReply(kind="topic_root", message_id=53)),
    )
    for message in messages:
        store.observe(ObservedMessage(A, message.seq, message.message_id, message.sender_id, message.name,
                                      message.timestamp, message.text, is_self=message.is_self), 500)
    snapshot = JudgeSnapshot(scope=A.session_key, version="version1", after_seq=0, through_seq=3,
                             new_seqs=(1, 3), messages=messages, created=1000.0, expires=1120.0,
                             bot_name="肥鱼", nicknames=("肥鱼",), main_running=False)
    settings = TelegramSocialJudgeConfig(min_interval_seconds=0, retry_delay_seconds=0)
    provider = AsyncMock(spec=LLMProvider)
    provider.chat.return_value = LLMResponse(content=output(), usage=LLMUsage.estimated(input_tokens=30, output_tokens=10))
    judge = SocialJudge(store, settings, provider)
    yield store, settings, provider, judge, snapshot, now
    store.close()


def updated(snapshot, **values):
    data = {**snapshot.model_dump(), **values}
    if any(key in values for key in ("scope", "after_seq", "through_seq")):
        data.pop("batch_id")
    return JudgeSnapshot.model_validate(data)


def test_judge_defaults_roundtrip_through_existing_json_settings():
    config = TelegramSocialConfig()
    assert config.judge.model == "openai-codex/gpt-6-luna"
    assert config.judge.provider == "openai_codex"
    assert config.judge.reasoning_effort == "low"
    body = config.model_dump(by_alias=True)
    assert TelegramSocialConfig.model_validate(body) == config
    assert SETUP_SPEC.fields["social"].default["judge"] == body["judge"]
    assert "enabled" not in body["judge"]


@pytest.mark.parametrize("values", [
    {"maxRetries": 2}, {"maxRequestsPerHour": 0}, {"maxFallbacksPerHour": -1},
    {"timeoutSeconds": float("nan")}, {"timeoutSeconds": float("inf")},
    {"contextMessages": 101}, {"contextChars": 100}, {"model": "gpt-6-luna "},
    {"provider": ""}, {"reasoningEffort": "ultra"}, {"enabled": True},
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


@pytest.mark.parametrize("values", [
    {"scope": "telegram:123"}, {"scope": "slack:-1001"},
    {"new_seqs": (3, 1)}, {"new_seqs": (1, 1, 3)}, {"new_seqs": (4,)},
    {"through_seq": 2}, {"after_seq": 3}, {"expires": 1000.0},
    {"new_seqs": (1, 2, 3)}, {"created": float("nan")},
])
def test_snapshot_rejects_invalid_ranges_and_scope(case, values):
    with pytest.raises(ValidationError):
        updated(case[4], **values)


@pytest.mark.parametrize("body", [
    "not JSON", "```json\n{}\n```", "{}", "[]",
    output(through=4), output(sources=[99]), output(sources=[2]), output(sources=[]),
    output(sources=[3, 1]), output(sources=[3, 3]), output(reason=" "),
    output(reason="x" * 241), output(decision="reply"), output(decision="observe", sources=[3]),
    output().replace('"through_seq": 3', '"through_seq": "3"'),
    output()[:-1] + ',"public_reply":"hello"}',
])
async def test_bad_decisions_are_errors_not_observe(case, body):
    store, _, provider, judge, snapshot, _ = case
    provider.chat.return_value = LLMResponse(content=body)
    record = await judge.assess(snapshot)
    assert record.status == "pending" and record.error_code == "invalid_decision"
    assert store.judge_through(A.session_key) == store.state(A.session_key)["read_through"] == 0


@pytest.mark.parametrize("decision", ["wake", "observe"])
async def test_decision_advances_only_assessment_and_duplicate_does_not_recall(case, decision):
    store, _, provider, judge, snapshot, _ = case
    provider.chat.return_value = LLMResponse(content=output(decision=decision))
    record = await judge.assess(snapshot)
    assert record.status == "decided" and record.decision.decision == decision
    assert store.judge_through(A.session_key) == 3
    assert store.state(A.session_key)["seen_through"] == store.state(A.session_key)["read_through"] == 0
    assert not store.db.execute("SELECT 1 FROM wakes").fetchone()
    assert not store.db.execute("SELECT 1 FROM actions").fetchone()
    assert await judge.assess(snapshot) == record
    provider.chat.assert_awaited_once()


async def test_context_topic_metadata_and_intent_are_shown_without_main_session(case):
    _, _, provider, judge, snapshot, _ = case
    snapshot = updated(snapshot, mode="sleeping", intent=JudgeIntent(
        purpose="messages", description="等 Alice 回答准备了什么", senders=("1",),
        infinite=True, condition_expires=1100.0,
    ))
    await judge.assess(snapshot)
    kwargs = provider.chat.call_args.kwargs
    body = json.loads(kwargs["messages"][1]["content"])
    assert body["messages"][2]["reply"]["kind"] == "topic_root"
    assert body["messages"][1]["is_self"]
    assert body["mode"] == "sleeping" and body["intent"]["senders"] == ["1"]
    assert kwargs["tools"] is None and kwargs["tool_choice"] == "none"
    assert kwargs["reasoning_effort"] == "low"
    provider.chat_with_retry.assert_not_called()


async def test_model_payload_omits_internal_identity_and_revisions(case):
    _, _, provider, judge, snapshot, _ = case
    await judge.assess(snapshot)
    payload = provider.chat.call_args.kwargs["messages"][1]["content"]
    body = json.loads(payload)
    assert not {"batch_id", "version", "scope", "expires", "quiet_due", "total_due"} & body.keys()
    assert all("revision" not in message for message in body["messages"])
    assert len(payload) < len(snapshot.model_dump_json())


def test_public_defaults_do_not_advertise_retired_attention_settings():
    default = SETUP_SPEC.fields["social"].default
    assert "replyAttention" not in default
    assert "enabled" not in default["dynamicWait"]
    assert "unfinishedSeconds" not in default["dynamicWait"]
    assert TelegramSocialConfig.model_validate(default).reply_attention.enabled is False


async def test_older_displayed_member_message_can_be_a_source(case):
    _, _, provider, judge, snapshot, _ = case
    provider.chat.return_value = LLMResponse(content=output(sources=[1]))
    assert (await judge.assess(snapshot)).decision.source_seqs == (1,)


@pytest.mark.parametrize("response", [
    LLMResponse(content=output(), finish_reason="length"),
    LLMResponse(content=None), LLMResponse(content="x" * 4097),
    LLMResponse(content=output(), tool_calls=[ToolCallRequest("t", "message", {})]),
])
async def test_nonfinal_and_tool_responses_are_rejected(case, response):
    _, _, provider, judge, snapshot, _ = case
    provider.chat.return_value = response
    assert (await judge.assess(snapshot)).status == "pending"


async def test_two_failures_yield_one_durable_fallback_without_main_execution(case):
    store, _, provider, judge, snapshot, _ = case
    provider.chat.side_effect = RuntimeError("sensitive bearer token")
    first = await judge.assess(snapshot)
    final = await judge.assess(snapshot)
    assert first.status == "pending" and first.attempts == 1
    assert final.status == "fallback" and final.attempts == 2
    assert final.decision is None and final.error_code == "provider_exception"
    assert await judge.assess(snapshot) == final
    assert provider.chat.await_count == 2
    assert store.judge_through(A.session_key) == 0
    assert not store.db.execute("SELECT 1 FROM wakes").fetchone()
    assert "sensitive" not in str([tuple(row) for row in store.db.execute("SELECT * FROM judge_attempts")])


async def test_retry_after_is_persisted_without_sleeping_or_hidden_retries(case):
    store, _, provider, judge, snapshot, now = case
    provider.chat.return_value = LLMResponse(content=None, finish_reason="error", error_retry_after_s=10)
    first = await judge.assess(snapshot)
    assert first.due == now[0] + 10
    assert (await judge.assess(snapshot)).attempts == 1
    now[0] += 10
    provider.chat.return_value = LLMResponse(content=output())
    assert (await judge.assess(snapshot)).status == "decided"
    assert store.db.execute("SELECT COUNT(*) FROM judge_attempts").fetchone()[0] == 2


async def test_global_budget_covers_other_topics_and_retries(case):
    store, settings, provider, _, snapshot, now = case
    settings.max_requests_per_hour = 1
    judge = SocialJudge(store, settings, provider)
    await judge.assess(snapshot)
    store.bind(B, "version1")
    other = snapshot.messages[0]
    store.observe(ObservedMessage(B, 99, other.message_id, other.sender_id, other.name, other.timestamp, other.text), 500)
    second = updated(snapshot, scope=B.session_key, through_seq=1, new_seqs=(1,), messages=(other,))
    record = await judge.assess(second)
    assert record.status == "pending" and record.attempts == 0
    assert record.due == now[0] + 3600
    provider.chat.assert_awaited_once()
    assert store.judge_through(B.session_key) == 0


async def test_zero_fallback_budget_keeps_failed_batch_pending_until_expiry(case):
    store, settings, provider, _, snapshot, now = case
    settings.max_retries = settings.max_fallbacks_per_hour = 0
    provider.chat.return_value = LLMResponse(content=None, finish_reason="error")
    record = await SocialJudge(store, settings, provider).assess(snapshot)
    assert record.status == "pending" and record.due == snapshot.expires
    now[0] = snapshot.expires
    store.recover_judge_batches()
    assert store.judge_record(record.id).status == "expired"
    assert store.judge_through(A.session_key) == 0


async def test_concurrent_same_scope_sends_one_request(case):
    store, _, provider, judge, snapshot, _ = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        entered.set()
        await release.wait()
        return LLMResponse(content=output())

    provider.chat.side_effect = slow
    task = asyncio.create_task(judge.assess(snapshot))
    await entered.wait()
    assert (await judge.assess(snapshot)).status == "running"
    release.set()
    assert (await task).status == "decided"
    assert store.db.execute("SELECT COUNT(*) FROM judge_attempts").fetchone()[0] == 1


async def test_arrivals_during_judgement_are_not_assessed_or_marked_read(case):
    store, _, provider, judge, snapshot, _ = case

    async def arrive(**kwargs):
        store.observe(ObservedMessage(A, 4, 14, "1", "Alice", 1000.0, "还有一件事"), 500)
        return LLMResponse(content=output(decision="observe"))

    provider.chat.side_effect = arrive
    assert (await judge.assess(snapshot)).status == "decided"
    assert store.judge_through(A.session_key) == 3
    assert store.state(A.session_key)["next_seq"] == 5
    assert store.state(A.session_key)["seen_through"] == 0


@pytest.mark.parametrize("change", ["role", "pause", "expire", "delete", "edit"])
async def test_late_results_do_not_revive_invalid_snapshots(case, change):
    store, _, provider, judge, snapshot, now = case

    async def invalidate(**kwargs):
        if change == "role":
            store.bind(A, "version2")
        elif change == "pause":
            with store.db:
                store.db.execute("UPDATE scopes SET mode='paused' WHERE key=?", (A.session_key,))
        elif change == "expire":
            now[0] = snapshot.expires
        elif change == "delete":
            with store.db:
                store.db.execute("DELETE FROM messages WHERE seq=3")
        else:
            with store.db:
                store.db.execute("UPDATE messages SET text='changed' WHERE seq=3")
        return LLMResponse(content=output())

    provider.chat.side_effect = invalidate
    assert (await judge.assess(snapshot)).status in {"cancelled", "expired"}
    assert store.state(A.session_key)["read_through"] == 0


async def test_missing_context_cannot_advance_past_retained_unshown_messages(case):
    store, _, provider, judge, snapshot, _ = case
    snapshot = updated(snapshot, messages=(snapshot.messages[0], snapshot.messages[2]))
    with pytest.raises(PermissionError, match="not retained"):
        await judge.assess(snapshot)
    provider.chat.assert_not_called()
    assert store.judge_through(A.session_key) == 0


async def test_context_and_ttl_limits_precede_network(case):
    _, settings, provider, _, snapshot, _ = case
    settings.context_messages = 2
    with pytest.raises(ValueError, match="context"):
        await SocialJudge(case[0], settings, provider).assess(snapshot)
    settings.context_messages = 30
    with pytest.raises(ValueError, match="TTL"):
        await SocialJudge(case[0], settings, provider).assess(updated(snapshot, expires=1600.0))
    provider.chat.assert_not_called()


async def test_usage_is_recorded_for_invalid_json(case):
    store, _, provider, judge, snapshot, _ = case
    provider.chat.return_value = LLMResponse(content="bad JSON", usage=LLMUsage.estimated(input_tokens=31, output_tokens=11))
    await judge.assess(snapshot)
    row = store.db.execute("SELECT input_tokens,output_tokens,total_tokens FROM judge_attempts").fetchone()
    assert tuple(row) == (31, 11, 42)


async def test_cancelled_request_is_charged_and_does_not_mark_read(case):
    store, _, provider, judge, snapshot, _ = case
    provider.chat.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await judge.assess(snapshot)
    record = store.judge_record(store.db.execute("SELECT id FROM judge_batches").fetchone()[0])
    assert record.status == "pending" and record.attempts == 1
    assert store.judge_through(A.session_key) == 0


def test_startup_recovery_does_not_refund_an_incomplete_attempt(case):
    store, settings, _, judge, snapshot, _ = case
    record = store.open_judge_batch(snapshot, judge.revision, settings.model)
    running = store.claim_judge_attempt(record.id, snapshot, limit=240, interval=0, max_attempts=2)
    assert running.attempts == 1
    store.recover_judge_batches()
    assert store.judge_record(record.id).status == "pending"
    assert store.judge_record(record.id).attempts == 1
    assert store.db.execute("SELECT error_code FROM judge_attempts").fetchone()[0] == "interrupted"


def test_decision_and_assessment_watermark_rollback_together(case):
    store, settings, _, judge, snapshot, _ = case
    record = store.open_judge_batch(snapshot, judge.revision, settings.model)
    running = store.claim_judge_attempt(record.id, snapshot, limit=240, interval=0, max_attempts=2)
    store.db.execute("CREATE TRIGGER fail_judge_cursor BEFORE INSERT ON judge_state BEGIN SELECT RAISE(ABORT,'test'); END")
    store.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.finish_judge_attempt(record.id, snapshot, attempt=running.attempts,
                                   decision=JudgeDecision.model_validate_json(output()), error_code=None,
                                   delay=0, latency_ms=1, input_tokens=1, output_tokens=1, total_tokens=2)
    assert store.judge_record(record.id).status == "running"
    assert store.judge_through(A.session_key) == 0
    assert store.db.execute("SELECT finished FROM judge_attempts").fetchone()[0] is None


async def test_restart_preserves_decision_without_recalling_provider(case, tmp_path):
    store, settings, provider, judge, snapshot, now = case
    expected = await judge.assess(snapshot)
    store.close()
    restored = SocialStore(tmp_path, now=lambda: now[0])
    try:
        restored.recover_judge_batches()
        assert await SocialJudge(restored, settings, provider).assess(snapshot) == expected
        assert restored.judge_through(A.session_key) == 3
        provider.chat.assert_awaited_once()
    finally:
        restored.close()


def test_schema_eight_upgrade_preserves_main_data(case, tmp_path):
    store = case[0]
    with store.db:
        for table in ("judge_attempts", "judge_batches", "judge_state"):
            store.db.execute(f"DROP TABLE {table}")
        remove_v4_columns(store.db)
        store.db.execute("PRAGMA user_version=8")
    store.close()
    upgraded = SocialStore(tmp_path)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 13
        assert upgraded.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert upgraded.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert upgraded.state(A.session_key)["read_through"] == 0
        assert upgraded.judge_through(A.session_key) == 3
    finally:
        upgraded.close()


async def test_timeout_is_a_charged_failure_with_no_observe_watermark(case):
    store, _, provider, judge, snapshot, _ = case
    provider.chat.side_effect = TimeoutError
    record = await judge.assess(snapshot)
    assert record.error_code == "timeout" and record.attempts == 1
    assert store.judge_through(A.session_key) == 0


async def test_single_attempt_mode_never_silently_retries(case):
    store, settings, provider, _, snapshot, _ = case
    settings.max_retries = 0
    provider.chat.return_value = LLMResponse(content="invalid")
    judge = SocialJudge(store, settings, provider)
    record = await judge.assess(snapshot)
    assert record.status == "fallback" and record.attempts == 1
    assert (await judge.assess(snapshot)).id == record.id
    provider.chat.assert_awaited_once()
    assert store.judge_through(A.session_key) == 0


async def test_fallback_allowance_is_global_and_persistent(case):
    store, settings, provider, _, snapshot, now = case
    settings.max_retries = 0
    settings.max_fallbacks_per_hour = 1
    provider.chat.return_value = LLMResponse(content="invalid")
    judge = SocialJudge(store, settings, provider)
    assert (await judge.assess(snapshot)).status == "fallback"
    store.bind(B, "version1")
    message = snapshot.messages[0]
    store.observe(ObservedMessage(B, 99, 11, message.sender_id, message.name, message.timestamp, message.text), 500)
    second = updated(snapshot, scope=B.session_key, through_seq=1, new_seqs=(1,), messages=(message,))
    record = await judge.assess(second)
    assert record.status == "pending" and record.attempts == 1
    assert record.due == now[0] + 3600
    assert (await judge.assess(second)).attempts == 1
    assert provider.chat.await_count == 2


async def test_minimum_interval_is_persisted_across_attempts(case):
    _, settings, provider, _, snapshot, now = case
    settings.min_interval_seconds = 3
    provider.chat.return_value = LLMResponse(content="invalid")
    judge = SocialJudge(case[0], settings, provider)
    await judge.assess(snapshot)
    second = await judge.assess(snapshot)
    assert second.attempts == 1 and second.due == now[0] + 3
    provider.chat.assert_awaited_once()


async def test_late_cancelled_response_usage_is_not_lost(case):
    store, _, provider, judge, snapshot, _ = case

    async def changed(**kwargs):
        store.bind(A, "version2")
        return LLMResponse(content=output(), usage=LLMUsage.estimated(input_tokens=30, output_tokens=10))

    provider.chat.side_effect = changed
    assert (await judge.assess(snapshot)).status == "cancelled"
    assert store.db.execute("SELECT total_tokens FROM judge_attempts").fetchone()[0] == 40


async def test_same_scope_different_batch_cannot_overlap_network_requests(case):
    store, _, provider, judge, snapshot, _ = case
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        entered.set()
        await release.wait()
        return LLMResponse(content=output())

    provider.chat.side_effect = slow
    first = asyncio.create_task(judge.assess(snapshot))
    await entered.wait()
    store.observe(ObservedMessage(A, 4, 14, "1", "Alice", 1000.0, "补充"), 500)
    fourth = JudgeMessage(seq=4, message_id=14, sender_id="1", name="Alice", timestamp=1000.0, text="补充")
    second = updated(snapshot, through_seq=4, new_seqs=(1, 3, 4), messages=(*snapshot.messages, fourth))
    pending = await judge.assess(second)
    assert pending.status == "pending" and pending.attempts == 0
    release.set()
    assert (await first).status == "decided"
    provider.chat.assert_awaited_once()


async def test_recovery_cannot_replenish_exhausted_attempts(case):
    store, settings, provider, judge, snapshot, _ = case
    record = store.open_judge_batch(snapshot, judge.revision, settings.model)
    for _ in range(2):
        store.claim_judge_attempt(record.id, snapshot, limit=240, interval=0, max_attempts=2)
        store.recover_judge_batches()
    assert (await judge.assess(snapshot)).status == "fallback"
    provider.chat.assert_not_called()


@pytest.mark.parametrize("values", [
    {"infinite": True, "until": 1100.0}, {"infinite": True, "purpose": "reply"},
])
def test_wait_intent_rejects_impossible_combinations(values):
    with pytest.raises(ValidationError):
        JudgeIntent.model_validate(values)


async def test_forged_self_identity_is_rejected_before_calling(case):
    _, _, provider, judge, snapshot, _ = case
    fake = snapshot.messages[1].model_copy(update={"is_self": False})
    snapshot = updated(snapshot, messages=(snapshot.messages[0], fake, snapshot.messages[2]))
    with pytest.raises(PermissionError):
        await judge.assess(snapshot)
    provider.chat.assert_not_called()


def test_v3b_judge_settings_change_invalidates_the_active_scope_version(tmp_path):
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
    from nanobot.config.schema import TelegramSocialScopeConfig

    config = TelegramSocialConfig(enabled=True, scopes=[TelegramSocialScopeConfig(chat_id=-1001, thread_id=53, role_id="friend")])
    runtime = TelegramSocialRuntime(config, tmp_path, MessageBus())
    try:
        before = runtime.store.state(A.session_key)["version"]
        with runtime.store.db:
            runtime.store.db.execute("UPDATE scopes SET mode='waiting',wake_at=2000,wake_config=? WHERE key=?",
                                     ('{"keywords":["ready"]}', A.session_key))
        changed = config.model_copy(deep=True)
        changed.judge.model = "openai-codex/gpt-6.1-sol"
        runtime.configure(changed)
        assert runtime.store.state(A.session_key)["version"] != before
        assert runtime.store.state(A.session_key)["mode"] == "observing"
        assert runtime.store.state(A.session_key)["wake_config"] == '{}'
    finally:
        runtime.close()
