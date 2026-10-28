"""M4b local opportunities, quiet hours and single-queue recovery acceptance."""

import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import RequestContext
from nanobot.channels.telegram.manifest import SETUP_SPEC
from nanobot.channels.telegram.runtime import TelegramConfig
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.channels.telegram.social.proactive import is_quiet
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent
from nanobot.channels.telegram.tests.test_social_scheduler import clock, observed, social
from nanobot.config.schema import (
    TelegramSocialConfig,
    TelegramSocialProactiveConfig,
    TelegramSocialQuietHoursConfig,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


def enable(social, *, require_context=True, quiet=None):
    clock(social).value = datetime.fromisoformat("2026-10-28T12:00:00+00:00").timestamp()
    binding = social.config.scopes[0]
    binding.proactive = TelegramSocialProactiveConfig(enabled=True, interval_seconds=60, ttl_seconds=30,
                                                     context_max_age_seconds=300, require_context=require_context)
    if quiet is not None:
        binding.quiet_hours = TelegramSocialQuietHoursConfig.model_validate(quiet)
    social.configure(social.config)
    return binding


async def tick(social, seconds=60):
    clock(social).advance(seconds)
    await social.run_due()


def view(inbound):
    return json.loads(inbound.content.split("\n", 1)[1])


def claim(social, inbound, turn="proactive-turn"):
    ctx = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                         turn_id=turn, metadata=dict(inbound.metadata))
    social.admit_request(ctx)
    social.store.note_seen(A.session_key, turn, encode_view(view(inbound)))
    return ctx


def learn(social, *, kind="pending", content="稍后继续讨论乌龙茶", seq=1):
    social.store.observe(observed(social, seq, text=content), 500)
    snapshot = social.store.view(A.session_key, count=30, chars=12000, turn_id="learning")
    social.store.note_seen(A.session_key, "learning", encode_view(snapshot))
    result = social.memory.apply(A.session_key, "learning", MemoryRequest(
        action="record", kind=kind, content=content, message_ids=[seq], basis="direct",
    ))
    return result["memory"]["id"]


async def test_upgrade_defaults_keep_ordinary_observation_zero_calls(social):
    await social.observe(observed(social, text="ordinary chat"), directed=False)
    for _ in range(3):
        await tick(social, 3600)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["next_check"] is None
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert not social.config.scopes[0].proactive.enabled


async def test_no_candidate_means_no_model_request_and_next_deadline_persisted(social):
    enable(social)
    await tick(social)
    assert social.bus.inbound.empty()
    status = social.proactive.status(A.session_key)
    assert status["decision"] == "no_context"
    assert status["next_check"] == social.now() + 60
    assert social.store.next_due() == status["next_check"]
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


async def test_recent_unread_is_considered_only_once_without_new_activity(social):
    enable(social)
    await social.observe(observed(social), directed=False)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["wake_reason"] == "proactive"
    assert view(inbound)["opportunity"]["kind"] == "unread"
    ctx = claim(social, inbound)
    social.store.finish(ctx.turn_id, "completed")
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "no_context"


async def test_explicit_context_free_opportunity_is_opt_in(social):
    enable(social, require_context=False)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["messages"] == []
    assert view(inbound)["opportunity"]["kind"] == "configured"
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


async def test_opportunity_configuration_and_budget_are_per_conversation(social):
    enable(social, require_context=False)
    await tick(social)
    assert social.bus.inbound.qsize() == 1
    assert social.bus.inbound.get_nowait().session_key == A.session_key
    assert social.proactive.status(B.session_key)["decision"] == "disabled"
    assert social.proactive.status(B.session_key)["next_check"] is None


@pytest.mark.parametrize("kind", ["pending", "topic"])
async def test_memory_opportunity_is_bounded_by_source_revision(social, kind):
    enable(social)
    memory_id = learn(social, kind=kind)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["opportunity"]["memory_id"] == memory_id
    assert view(inbound)["opportunity"]["revision"] == 1
    # IDs in queued snapshots do not cache a second copy of memory content.
    assert "content" not in view(inbound)["opportunity"]
    ctx = claim(social, inbound)
    social.store.finish(ctx.turn_id, "completed")
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "no_context"


async def test_pending_has_priority_and_preference_is_not_an_idle_topic(social):
    enable(social)
    learn(social, kind="topic", content="旧话题")
    pending = learn(social, kind="pending", content="待说想法", seq=2)
    await tick(social)
    assert view(social.bus.inbound.get_nowait())["opportunity"]["memory_id"] == pending


async def test_stale_or_read_context_does_not_create_an_opportunity(social):
    enable(social)
    await social.observe(replace(observed(social), timestamp=social.now() - 301), directed=False)
    await tick(social)
    assert social.bus.inbound.empty()
    await social.observe(observed(social, 2), directed=False)
    with social.store.db:
        social.store.db.execute("UPDATE scopes SET read_through=2 WHERE key=?", (A.session_key,))
    await tick(social)
    assert social.bus.inbound.empty()


@pytest.mark.parametrize("instant,quiet", [
    ("2026-10-28T13:59:00+00:00", False),  # Tokyo 22:59
    ("2026-10-28T14:00:00+00:00", True),
    ("2026-10-28T23:00:00+00:00", False),  # Tokyo 08:00
    ("2026-10-28T22:59:00+00:00", True),
])
def test_cross_midnight_quiet_window_uses_configured_timezone(social, instant, quiet):
    binding = enable(social, quiet={"enabled": True, "timezone": "Asia/Tokyo", "start": "23:00", "end": "08:00"})
    assert is_quiet(binding, datetime.fromisoformat(instant).timestamp()) is quiet


@pytest.mark.parametrize("instant", ["2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00"])
def test_quiet_window_covers_both_repeated_dst_hours(social, instant):
    binding = enable(social, quiet={"enabled": True, "timezone": "America/New_York", "start": "01:00", "end": "02:00"})
    assert is_quiet(binding, datetime.fromisoformat(instant).timestamp())


async def test_quiet_hours_skip_opportunities_but_direct_mention_and_reply_path_survives(social):
    enable(social, require_context=False, quiet={"enabled": True, "timezone": "UTC", "start": "12:00", "end": "13:00"})
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "quiet_hours"
    await social.observe(observed(social), directed=True)
    await tick(social, 2)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["wake_reason"] == "directed"
    claim(social, inbound)


async def test_quiet_ending_does_not_burst_replay_missed_slots(social):
    enable(social, require_context=False, quiet={"enabled": True, "timezone": "UTC", "start": "12:00", "end": "13:00"})
    await tick(social)
    await tick(social, 3599)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "missed"
    await tick(social)
    assert social.bus.inbound.qsize() == 1


@pytest.mark.parametrize("mode", ["waiting", "sleeping"])
async def test_wait_and_sleep_are_not_interrupted_by_opportunities(social, mode):
    enable(social, require_context=False)
    with social.store.db:
        social.store.db.execute("UPDATE scopes SET mode=?,wake_at=? WHERE key=?", (mode, social.now() + 300, A.session_key))
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "deferred"


@pytest.mark.parametrize("change", ["delete", "clear", "correct", "expire"])
async def test_memory_changes_cancel_queued_opportunity_before_model_admission(social, change):
    enable(social)
    memory_id = learn(social)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    if change == "delete":
        social.memory.apply(A.session_key, "learning", MemoryRequest(action="delete", memory_id=memory_id))
    elif change == "clear":
        social.memory.apply(A.session_key, "learning", MemoryRequest(action="clear"))
    elif change == "correct":
        social.store.observe(observed(social, 2, text="改为讨论咖啡"), 500)
        snapshot = social.store.view(A.session_key, count=30, chars=12000, turn_id="learning")
        social.store.note_seen(A.session_key, "learning", encode_view(snapshot))
        social.memory.apply(A.session_key, "learning", MemoryRequest(action="correct", memory_id=memory_id,
                            content="讨论咖啡", message_ids=[2], basis="direct"))
    else:
        with social.store.db:
            social.store.db.execute("UPDATE memories SET expires=? WHERE id=?", (social.now(), memory_id))
    with pytest.raises(PermissionError, match="memory_changed"):
        claim(social, inbound)
    assert social.store.db.execute("SELECT status FROM wakes WHERE id=?", (inbound.metadata["_social_job"],)).fetchone()[0] == "cancelled"


async def test_expired_unread_queue_is_revalidated_before_admission(social):
    enable(social)
    await social.observe(observed(social), directed=False)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    clock(social).advance(31)
    with pytest.raises(PermissionError, match="expired"):
        claim(social, inbound)


async def test_direct_event_wins_when_timer_and_message_are_due_together(social):
    enable(social, require_context=False)
    clock(social).advance(60)
    await social.observe(observed(social), directed=True)
    await tick(social, 2)
    assert social.bus.inbound.qsize() == 1
    assert view(social.bus.inbound.get_nowait())["wake_reason"] == "directed"
    assert social.proactive.status(A.session_key)["decision"] == "busy"


async def test_new_direct_message_replaces_unclaimed_queued_opportunity(social):
    enable(social, require_context=False)
    social.config.max_requests_per_hour = 1
    social.configure(social.config)
    await tick(social)
    opportunity = social.bus.inbound.get_nowait()
    await social.observe(observed(social), directed=True)
    await tick(social, 2)
    with pytest.raises(PermissionError):
        claim(social, opportunity)
    direct = social.bus.inbound.get_nowait()
    assert view(direct)["wake_reason"] == "directed"
    assert claim(social, direct)
    assert social.bus.inbound.empty()


async def test_busy_turn_does_not_get_a_second_proactive_run(social):
    enable(social, require_context=False)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    # Keep a real lease alive through the next opportunity deadline.
    with social.store.db:
        social.store.db.execute("UPDATE wakes SET expires=?,proactive_source=json_set(proactive_source,'$.expires_at',?) WHERE turn_id=?",
                                (social.now() + 300, social.now() + 300, ctx.turn_id))
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == "busy"


@pytest.mark.parametrize("budget", ["requests", "send_attempts"])
async def test_known_budget_exhaustion_skips_model_calls(social, budget):
    enable(social, require_context=False)
    limit = social.config.max_requests_per_hour if budget == "requests" else social.config.max_sends_per_hour
    with social.store.db:
        social.store.db.executemany(f"INSERT INTO {budget} VALUES(?,?)", [(A.session_key, social.now())] * limit)
    await tick(social)
    assert social.bus.inbound.empty()
    assert social.proactive.status(A.session_key)["decision"] == ("request_budget" if budget == "requests" else "send_budget")


@pytest.mark.parametrize("change", ["paused", "scope", "global", "role_binding"])
async def test_configuration_changes_cancel_old_candidates(social, change):
    enable(social, require_context=False)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    if change == "paused":
        social.config.scopes[0].paused = True
    elif change == "scope":
        social.config.scopes[0].enabled = False
    elif change == "global":
        social.config.enabled = False
    else:
        (social.roles / "other.md").write_text("new role")
        social.config.scopes[0].role_id = "other"
    social.configure(social.config)
    with pytest.raises(PermissionError):
        claim(social, inbound)
    await social.run_due()
    assert social.bus.inbound.empty()


async def test_role_text_edit_does_not_cancel_current_opportunity(social):
    enable(social, require_context=False)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    (social.roles / "guest.md").write_text("edited role text")
    assert claim(social, inbound)


async def test_quiet_beginning_cancels_active_proactive_send_but_not_ordinary_sender(social):
    binding = enable(social, require_context=False, quiet={"enabled": True, "timezone": "UTC", "start": "12:02", "end": "13:00"})
    binding.proactive.ttl_seconds = 120
    social.configure(social.config)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    clock(social).advance(60)
    with pytest.raises(PermissionError, match="quiet_hours"):
        await social.send(ctx, "主动发言", None)
    sender.assert_not_awaited()
    # An ordinary/background turn is not subject to proactive tool restrictions.
    ordinary = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                              turn_id="ordinary", metadata={"message_thread_id": A.thread_id})
    result = await social.send(ordinary, "明确请求的消息", None)
    assert result["status"] == "sent"


async def test_quiet_beginning_cancels_unattempted_proactive_parts(social):
    binding = enable(social, require_context=False, quiet={"enabled": True, "timezone": "UTC", "start": "12:02", "end": "13:00"})
    binding.proactive.ttl_seconds = 120
    social.configure(social.config)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    await social.send_parts(ctx, ["第一条", "第二条"], None)
    await tick(social)
    sender.assert_not_awaited()
    assert {row[0] for row in social.store.db.execute("SELECT status FROM actions")} == {"failed"}


async def test_acknowledging_unread_before_reply_does_not_invalidate_claimed_turn(social):
    enable(social)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    await social.observe(observed(social), directed=False)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    social.mark_read(ctx, 1)
    assert (await social.send(ctx, "参与这段聊天", None))["status"] == "sent"


async def test_wait_after_queuing_parts_keeps_existing_delivery_without_new_wake(social):
    enable(social, require_context=False)
    sender = AsyncMock(side_effect=[501, 502])
    social.set_sender(sender)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    await social.send_parts(ctx, ["第一条", "第二条"], None)
    social.wait(ctx, 300)
    social.store.finish(ctx.turn_id, "completed")
    await social.run_due()
    await tick(social, 2)
    assert sender.await_count == 2
    assert social.store.state(A.session_key)["mode"] == "waiting"
    assert social.bus.inbound.empty()


async def test_expired_unclaimed_restart_refunds_unused_request_slot(social):
    enable(social, require_context=False)
    social.config.max_requests_per_hour = 1
    social.configure(social.config)
    await tick(social)
    social.bus.inbound.get_nowait()
    current_clock = clock(social)
    social.close()
    current_clock.advance(31)
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=current_clock)
    try:
        await restored.recover()
        assert restored.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        await restored.observe(observed(restored), directed=True)
        current_clock.advance(2)
        await restored.run_due()
        assert view(restored.bus.inbound.get_nowait())["wake_reason"] == "directed"
    finally:
        await restored.scheduler.stop()
        restored.close()


async def test_restart_does_not_replay_missed_slots_or_repeat_consumed_memory(social):
    enable(social)
    learn(social)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    social.store.finish(ctx.turn_id, "completed")
    current_clock = clock(social)
    social.close()
    current_clock.advance(3600)
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=current_clock)
    try:
        await restored.recover()
        assert restored.bus.inbound.empty()
        assert restored.proactive.status(A.session_key)["decision"] == "missed"
        current_clock.advance(60)
        await restored.run_due()
        assert restored.bus.inbound.empty()
        assert restored.proactive.status(A.session_key)["decision"] == "no_context"
    finally:
        await restored.scheduler.stop()
        restored.close()


async def test_restart_recovers_at_most_one_still_valid_candidate(social):
    enable(social, require_context=False)
    await tick(social)
    social.bus.inbound.get_nowait()
    current_clock = clock(social)
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=current_clock)
    try:
        await restored.recover()
        await restored.recover()
        await restored.run_due()
        assert restored.bus.inbound.qsize() == 1
        assert view(restored.bus.inbound.get_nowait())["wake_reason"] == "proactive"
    finally:
        await restored.scheduler.stop()
        restored.close()


async def test_model_can_silently_decline_opportunity_and_return_to_observing(tmp_path, social):
    enable(social, require_context=False)
    loop = agent(tmp_path, social, [LLMResponse(content="internal decision to stay quiet")])
    await tick(social)
    await loop._dispatch(social.bus.inbound.get_nowait())
    loop.provider.chat_stream_with_retry.assert_awaited_once()
    assert social.bus.outbound.empty()
    assert social.store.state(A.session_key)["mode"] == "observing"


async def test_real_tool_turn_can_read_candidate_by_id_and_publish_once(tmp_path, social):
    enable(social)
    memory_id = learn(social)
    sender = AsyncMock(return_value=501)
    social.set_sender(sender)
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="read", name="social_memory", arguments={"action": "list", "memory_id": memory_id})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="send", name="social_send", arguments={"text": "接着聊茶？"})]),
        LLMResponse(content="internal"),
    ])
    await tick(social)
    await loop._dispatch(social.bus.inbound.get_nowait())
    sender.assert_awaited_once_with(A, "接着聊茶？", None)
    assert social.bus.outbound.empty()
    assert social.store.state(A.session_key)["mode"] == "observing"


def test_scope_config_and_settings_json_contract_round_trip():
    config = TelegramConfig.model_validate({"social": {"enabled": True, "scopes": [{
        "chatId": -1001, "roleId": "guest", "proactive": {"enabled": True, "intervalSeconds": 900},
        "quietHours": {"enabled": True, "timezone": "Asia/Tokyo", "start": "23:30", "end": "08:30"},
    }]}})
    public = config.model_dump(by_alias=True)
    assert TelegramConfig.model_validate(public) == config
    assert public["social"]["scopes"][0]["proactive"]["intervalSeconds"] == 900
    assert SETUP_SPEC.fields["social"].default == TelegramSocialConfig().model_dump(by_alias=True)
    assert SETUP_SPEC.fields["social"].kind == "json"


@pytest.mark.parametrize("section", [
    {"proactive": {"intervalSeconds": 0}}, {"proactive": {"ttlSeconds": 1000}},
    {"proactive": {"unknown": True}}, {"quietHours": {"timezone": "Mars/Base"}},
    {"quietHours": {"start": "24:00"}}, {"quietHours": {"start": "23:00", "end": "23:00"}},
])
def test_invalid_scheduling_fields_are_rejected_at_config_boundary(section):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate({"scopes": [{"chatId": -1001, "roleId": "guest", **section}]})


def test_schema_four_upgrade_preserves_memory_watermarks_and_history(tmp_path):
    root = tmp_path / "upgrade"
    store = SocialStore(root)
    store.bind(A, "v4")
    store.db.execute("INSERT INTO memories VALUES('id',?,'topic','remember me','[]','confirmed','direct',1,1,1,NULL)", (A.session_key,))
    store.db.execute("UPDATE scopes SET next_seq=7,read_through=4,seen_through=5 WHERE key=?", (A.session_key,))
    store.db.execute("DROP TABLE proactive_state")
    store.db.execute("ALTER TABLE wakes DROP COLUMN proactive_source")
    store.db.execute("PRAGMA user_version=4")
    store.db.commit()
    store.close()
    upgraded = SocialStore(root)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert upgraded.db.execute("SELECT content FROM memories WHERE id='id'").fetchone()[0] == "remember me"
        assert upgraded.state(A.session_key)["read_through"] == 4
        assert upgraded.state(A.session_key)["seen_through"] == 5
        assert upgraded.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        upgraded.close()
