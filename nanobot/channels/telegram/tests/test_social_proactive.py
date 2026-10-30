"""V4-A idle opportunities, quiet hours and single-queue recovery acceptance."""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.events import DeliveryReceipt
from nanobot.channels.telegram.manifest import SETUP_SPEC
from nanobot.channels.telegram.runtime import TelegramConfig
from nanobot.channels.telegram.social.memory import MemoryRequest
from nanobot.channels.telegram.social.models import ConversationScope, ProactiveCandidate
from nanobot.channels.telegram.social.proactive import is_quiet
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime, encode_view
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_judge_runtime import decide
from nanobot.channels.telegram.tests.test_social_runtime import A, B, agent
from nanobot.channels.telegram.tests.test_social_scheduler import (
    clock,
    observed,
    remove_v4_columns,
    social,
)
from nanobot.config.schema import (
    TelegramSocialConfig,
    TelegramSocialProactiveConfig,
    TelegramSocialQuietHoursConfig,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


@pytest.mark.parametrize("fail", [False, True])
def test_schema_eleven_upgrade_preserves_source_values_and_rolls_back(social, monkeypatch, fail):
    social.store.observe(observed(social), 500, fresh=True)
    db = social.store.db
    remove_v4_columns(db)
    db.execute("PRAGMA user_version=11")
    db.commit()
    messages = [tuple(row) for row in db.execute("SELECT * FROM messages")]
    scopes = [tuple(row) for row in db.execute("SELECT * FROM scopes")]
    social.close()
    if fail:
        connect = sqlite3.connect

        class InterruptedMigration(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "ADD COLUMN sample" in sql:
                    raise sqlite3.OperationalError("synthetic migration failure")
                return super().execute(sql, parameters)

        monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=InterruptedMigration))
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            SocialStore(social.root, now=social.now)
        monkeypatch.setattr(sqlite3, "connect", connect)
        with connect(social.root / "social.sqlite3") as original:
            assert original.execute("PRAGMA user_version").fetchone()[0] == 11
            assert "last_member_at" not in {row[1] for row in original.execute("PRAGMA table_info(scopes)")}
            assert [tuple(row) for row in original.execute("SELECT * FROM scopes")] == scopes
    restored = SocialStore(social.root, now=social.now)
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert [tuple(row) for row in restored.db.execute("SELECT * FROM messages")] == messages
        assert [tuple(row)[:-1] for row in restored.db.execute("SELECT * FROM scopes")] == scopes
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()


@pytest.mark.parametrize("sample,interval", [(0, 1800), (1, 5400), (0.5, 3600)])
def test_default_random_interval_boundaries_and_restart_preserves_deadline(social, sample, interval):
    social.proactive.random_value = lambda: sample
    binding = social.config.scopes[0]
    binding.proactive.enabled = True
    social.configure(social.config)
    due = social.proactive.status(A.session_key)["next_check"]
    assert due == social.now() + interval
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    try:
        restored.proactive.random_value = lambda: pytest.fail("restart must not resample")
        restored.configure(social.config)
        assert restored.proactive.status(A.session_key)["next_check"] == due
    finally:
        restored.close()


@pytest.mark.parametrize("elapsed,decision", [(899, "member_active"), (900, "scheduled")])
async def test_idle_boundary_ignores_edits_duplicates_and_bot_sends(social, elapsed, decision):
    binding = enable(social)
    binding.proactive.idle_seconds = 900
    social.configure(social.config)
    original = observed(social)
    social.store.observe(original, 500)
    started = social.now()
    clock(social).advance(600)
    social.store.observe(replace(original, update_id=200, is_edit=True, timestamp=social.now(), text="edit"), 500)
    social.store.observe(original, 500)
    social.store.observe(observed(social, 2, is_self=True), 500)
    assert social.store.db.execute("SELECT last_member_at FROM scopes WHERE key=?", (A.session_key,)).fetchone()[0] == started
    social.store.db.execute("UPDATE proactive_state SET next_check=? WHERE scope=?", (started + elapsed, A.session_key))
    social.store.db.commit()
    await tick(social, elapsed - 600)
    assert social.proactive.status(A.session_key)["decision"] == decision


async def test_probability_modifiers_and_confirmed_message_groups(social):
    binding = enable(social)
    binding.proactive.probability = 0.3
    # Noon UTC is midnight in this explicitly selected zone.
    binding.proactive.timezone = "Etc/GMT-12"
    social.configure(social.config)
    receipt = DeliveryReceipt("sent", (501, 502, 503))
    social.record_message_receipt(A, receipt, "ordinary", [(i, "confirmed") for i in receipt.message_ids])
    for i in (601, 602):
        action = social.store.reserve_send(A.session_key, social.store.state(A.session_key)["version"], f"part {i}", 60)
        social.store.db.execute("UPDATE actions SET group_id='burst' WHERE id=?", (action,))
        social.store.delivery(action, "sent", message_id=i)
        social.store.observe(replace(observed(social, i, is_self=True), message_id=i), 500)
    for status in ("queued", "failed", "unknown"):
        social.record_message_receipt(A, DeliveryReceipt(status), "ordinary", [])
    social.proactive.random_value = lambda: 0.02
    await tick(social)
    result = social.proactive.status(A.session_key)
    assert result["probability"] == pytest.approx(0.3 * 0.5 * 0.3 * 0.3)
    assert result["decision"] == "probability_miss"
    assert result["metrics"]["confirmed_messages_hour"] == 5
    assert result["metrics"]["confirmed_groups_hour"] == 2
    assert result["metrics"]["history_complete"] is False
    assert social.bus.inbound.empty()


async def test_stop_proactive_contact_survives_sleep_clear_and_restart(social):
    enable(social)
    ctx = RequestContext(channel="telegram", chat_id=str(A.chat_id), session_key=A.session_key,
                         turn_id="ordinary", metadata={"message_thread_id": A.thread_id})
    social.set_wake(ctx, keywords=[], senders=[], on_messages=False, proactive_paused=True)
    social.store.set_wake_config(A.session_key, "{}")
    social.store.db.execute("UPDATE scopes SET mode='sleeping' WHERE key=?", (A.session_key,))
    social.store.db.commit()
    await tick(social)
    assert social.proactive.status(A.session_key)["decision"] == "stopped"
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    restored.proactive.random_value = lambda: 0
    try:
        assert restored.proactive.status(A.session_key)["stopped"] == 1
        restored.set_wake(ctx, keywords=[], senders=[], on_messages=False, proactive_paused=False)
        await tick(restored)
        assert view(restored.bus.inbound.get_nowait())["wake_reason"] == "proactive"
    finally:
        restored.close()


async def test_claimed_idle_is_consumed_after_restart(social):
    enable(social)
    await tick(social)
    claim(social, social.bus.inbound.get_nowait())
    old = social.proactive.status(A.session_key)
    social.close()
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=social.now)
    try:
        await restored.recover()
        assert restored.bus.inbound.empty()
        assert restored.proactive.status(A.session_key)["next_check"] == old["next_check"]
        assert restored.proactive.status(A.session_key)["sample"] == old["sample"]
    finally:
        await restored.scheduler.stop()
        restored.close()


def enable(social, *, require_context=True, quiet=None):
    clock(social).value = datetime.fromisoformat("2026-10-28T12:00:00+00:00").timestamp()
    binding = social.config.scopes[0]
    binding.proactive = TelegramSocialProactiveConfig(enabled=True, ttl_seconds=30,
                                                     min_interval_seconds=60, max_interval_seconds=60,
                                                     idle_seconds=0, probability=1,
                                                     context_max_age_seconds=300, require_context=require_context)
    social.proactive.random_value = lambda: 0
    if quiet is not None:
        binding.quiet_hours = TelegramSocialQuietHoursConfig.model_validate(quiet)
    social.configure(social.config)
    social.store.db.execute("UPDATE proactive_state SET tracked_since=?", (social.now(),))
    social.store.db.commit()
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


async def test_probability_miss_means_zero_requests_and_next_deadline_persisted(social):
    binding = enable(social)
    binding.proactive.probability = 0
    social.configure(social.config)
    await tick(social)
    assert social.bus.inbound.empty()
    status = social.proactive.status(A.session_key)
    assert status["decision"] == "probability_miss"
    assert status["sample"] == 0
    assert status["selected"] == 0
    assert status["next_check"] == social.now() + 60
    assert social.store.next_due() == status["next_check"]
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


async def test_consumed_messages_do_not_block_later_idle_opportunity(social):
    enable(social)
    decide(social)
    await social.observe(observed(social), directed=False)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["wake_reason"] == "judge"
    ctx = claim(social, inbound)
    social.store.finish(ctx.turn_id, "completed")
    await tick(social)
    assert view(social.bus.inbound.get_nowait())["opportunity"]["kind"] == "idle"
    assert social.store.state(A.session_key)["read_through"] == 0


async def test_context_free_opportunity_ignores_legacy_require_context(social):
    enable(social)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["messages"] == []
    assert view(inbound)["opportunity"]["kind"] == "idle"
    assert view(inbound)["opportunity"]["seq"] == 0
    assert view(inbound)["judgement"] == []
    assert social.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


async def test_opportunity_configuration_and_budget_are_per_conversation(social):
    enable(social, require_context=False)
    await tick(social)
    assert social.bus.inbound.qsize() == 1
    assert social.bus.inbound.get_nowait().session_key == A.session_key
    assert social.proactive.status(B.session_key)["decision"] == "disabled"
    assert social.proactive.status(B.session_key)["next_check"] is None


@pytest.mark.parametrize("kind", ["pending", "topic"])
async def test_idle_is_independent_of_memory_revision_and_middle_model(social, kind):
    enable(social)
    provider = decide(social)
    learn(social, kind=kind)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    assert view(inbound)["opportunity"]["kind"] == "idle"
    provider.chat.assert_not_awaited()
    ctx = claim(social, inbound)
    social.store.finish(ctx.turn_id, "completed")
    await tick(social)
    assert view(social.bus.inbound.get_nowait())["opportunity"]["opportunity_id"] == 2


async def test_legacy_pending_is_not_mistaken_for_eligible_pending_thought(social):
    enable(social)
    learn(social, kind="topic", content="旧话题")
    learn(social, kind="pending", content="待说想法", seq=2)
    await tick(social)
    assert view(social.bus.inbound.get_nowait())["opportunity"]["kind"] == "idle"
    assert social.proactive.status(A.session_key)["metrics"]["pending_thought_available"] is False


async def test_stale_and_read_context_do_not_prevent_idle_opportunity(social):
    enable(social)
    await social.observe(replace(observed(social), timestamp=social.now() - 301), directed=False)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    social.store.finish(ctx.turn_id, "completed")
    social.store.observe(observed(social, 2), 500)
    with social.store.db:
        social.store.db.execute("UPDATE scopes SET read_through=2 WHERE key=?", (A.session_key,))
    await tick(social)
    assert view(social.bus.inbound.get_nowait())["opportunity"]["kind"] == "idle"


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
    assert view(inbound)["wake_reason"] == "judge"
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
async def test_explicit_wait_is_deferred_but_ordinary_sleep_allows_idle(social, mode):
    enable(social, require_context=False)
    with social.store.db:
        social.store.db.execute("UPDATE scopes SET mode=?,wake_at=? WHERE key=?", (mode, social.now() + 300, A.session_key))
    await tick(social)
    assert social.bus.inbound.empty() is (mode == "waiting")
    assert social.proactive.status(A.session_key)["decision"] == ("deferred" if mode == "waiting" else "scheduled")


@pytest.mark.parametrize("change", ["delete", "clear", "correct", "expire"])
async def test_memory_changes_cancel_queued_opportunity_before_model_admission(social, change):
    enable(social)
    memory_id = learn(social)
    await tick(social)
    inbound = social.bus.inbound.get_nowait()
    # Legacy memory-sourced events retain their source revision checks.
    source = ProactiveCandidate(kind="memory", seq=1, memory_id=memory_id, revision=1,
                                expires_at=social.now() + 30)
    social.store.db.execute("UPDATE wakes SET proactive_source=? WHERE id=?", (source.model_dump_json(), inbound.metadata["_social_job"]))
    social.store.db.commit()
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
    social.store.observe(observed(social), 500)
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
    assert view(social.bus.inbound.get_nowait())["wake_reason"] == "judge"
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
    assert view(direct)["wake_reason"] == "judge"
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


@pytest.mark.parametrize("budget", ["requests", "send_attempts", "confirmed"])
async def test_known_budget_exhaustion_skips_model_calls(social, budget):
    enable(social, require_context=False)
    limit = social.config.max_requests_per_hour if budget == "requests" else social.config.max_sends_per_hour
    if budget == "confirmed":
        ids = tuple(range(501, 501 + limit))
        social.record_message_receipt(A, DeliveryReceipt("sent", ids), "ordinary", [(i, "sent") for i in ids])
    else:
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
    decide(social)
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
        assert view(restored.bus.inbound.get_nowait())["wake_reason"] == "judge"
    finally:
        await restored.scheduler.stop()
        restored.close()


async def test_restart_skips_missed_slots_then_allows_new_idle_without_new_memory(social):
    enable(social)
    learn(social)
    await tick(social)
    ctx = claim(social, social.bus.inbound.get_nowait())
    social.store.finish(ctx.turn_id, "completed")
    current_clock = clock(social)
    social.close()
    current_clock.advance(3600)
    restored = TelegramSocialRuntime(social.config, social.root, social.bus, now=current_clock)
    restored.proactive.random_value = lambda: 0
    try:
        await restored.recover()
        assert restored.bus.inbound.empty()
        assert restored.proactive.status(A.session_key)["decision"] == "missed"
        current_clock.advance(60)
        await restored.run_due()
        assert view(restored.bus.inbound.get_nowait())["opportunity"]["kind"] == "idle"
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
    proactive = public["social"]["scopes"][0]["proactive"]
    assert "intervalSeconds" not in proactive
    assert proactive["minIntervalSeconds"] == 1800
    assert proactive["maxIntervalSeconds"] == 5400
    assert TelegramSocialConfig.model_validate(SETUP_SPEC.fields["social"].default) == TelegramSocialConfig()
    assert SETUP_SPEC.fields["social"].kind == "json"


@pytest.mark.parametrize("section", [
    {"proactive": {"intervalSeconds": 0}}, {"proactive": {"ttlSeconds": 1000}},
    {"proactive": {"minIntervalSeconds": 5400, "maxIntervalSeconds": 1800}},
    {"proactive": {"probability": -0.1}}, {"proactive": {"probability": 1.1}},
    {"proactive": {"timezone": "Mars/Base"}},
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
    store.db.execute("DROP TABLE message_deliveries")
    for table in ("wake_samples", "dialogue_jobs", "dialogue_attention", "dialogue_waits", "dialogue_state"):
        store.db.execute(f"DROP TABLE {table}")
    store.db.execute("ALTER TABLE scopes DROP COLUMN wait_infinite")
    store.db.execute("ALTER TABLE wakes DROP COLUMN source_seq")
    store.db.execute("ALTER TABLE wakes DROP COLUMN condition_config")
    remove_v4_columns(store.db)
    store.db.execute("PRAGMA user_version=4")
    store.db.commit()
    store.close()
    upgraded = SocialStore(root)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 12
        assert upgraded.db.execute("SELECT content FROM memories WHERE id='id'").fetchone()[0] == "remember me"
        assert upgraded.state(A.session_key)["read_through"] == 4
        assert upgraded.state(A.session_key)["seen_through"] == 5
        assert upgraded.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        upgraded.close()


async def test_group_wide_opportunities_use_discovered_topic_bindings_after_restart(social):
    binding = enable(social)
    binding.all_topics = True
    social.configure(social.config)
    target = ConversationScope("telegram", A.chat_id, 999)
    await social.observe(replace(observed(social), scope=target), directed=False)
    assert social.proactive.status(target.session_key)["next_check"] is not None
    root, config, bus, now = social.root, social.config, social.bus, social.now
    social.close()
    restored = TelegramSocialRuntime(config, root, bus, now=now)
    try:
        decide(restored)
        clock(social).advance(60)
        await restored.run_due()
        inbounds = [bus.inbound.get_nowait() for _ in range(bus.inbound.qsize())]
        assert {item.session_key for item in inbounds} == {A.session_key, target.session_key}
        inbound = next(item for item in inbounds if item.session_key == target.session_key)
        assert view(inbound)["wake_reason"] == "judge"
        job = inbound.metadata["_social_job"]
        await restored.run_due()
        assert restored.store.db.execute("SELECT status FROM wakes WHERE id=?", (job,)).fetchone()[0] == "queued"
        binding.all_topics = False
        restored.configure(config)
        await restored.run_due()
        assert restored.store.db.execute("SELECT status FROM wakes WHERE id=?", (job,)).fetchone()[0] == "cancelled"
    finally:
        restored.close()
