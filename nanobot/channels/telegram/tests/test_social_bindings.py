"""Scope resolution caches and the read-only social validation boundary."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.bindings import SOCIAL_PROTOCOL, ScopeBindings
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import ProviderConversationState
from nanobot.session.manager import Session

SCOPE = ConversationScope("telegram", -1001, 42)


@pytest.fixture
def runtime(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "friend.md").write_text("PERSONA ALPHA", encoding="utf-8")
    config = TelegramSocialConfig.model_validate({
        "enabled": True, "learning": {"enabled": False},
        "scopes": [{"chatId": SCOPE.chat_id, "threadId": SCOPE.thread_id,
                    "roleId": "friend", "background": "GROUP BACKGROUND"}],
    })
    instance = TelegramSocialRuntime(config, tmp_path, MessageBus())
    yield instance
    instance.close()


def test_role_text_is_cached_until_stat_changes(runtime, monkeypatch):
    bindings = ScopeBindings(runtime.store, runtime.roles, "telegram")
    bindings.configure(runtime.config)
    read_text = Mock(side_effect=Path.read_text)
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs: read_text(path, **kwargs))
    first = bindings.resolve(SCOPE)
    assert first.persona == (
        f"{SOCIAL_PROTOCOL}\n\n# Operator persona\nPERSONA ALPHA"
        "\n\n# Operator group background\nGROUP BACKGROUND"
    )
    assert bindings.resolve(SCOPE) == first
    assert read_text.call_count == 1
    (runtime.roles / "friend.md").write_text("PERSONA BETA, EDITED", encoding="utf-8")
    edited = bindings.resolve(SCOPE)
    assert "PERSONA BETA, EDITED" in edited.persona
    assert edited.persona_revision == first.persona_revision + 1
    assert edited.version == first.version
    assert read_text.call_count == 2


def test_revisions_only_follow_persona_text_and_role_id(runtime, monkeypatch):
    revisions = Mock(wraps=runtime.store.setting_revision)
    monkeypatch.setattr(runtime.store, "setting_revision", revisions)
    first = runtime.bindings.resolve(SCOPE)
    assert runtime.bindings.resolve(SCOPE) == first
    revisions.assert_not_called()

    runtime.config.scopes[0].background = "NEW BACKGROUND"
    runtime.bindings.configure(runtime.config)
    background = runtime.bindings.resolve(SCOPE)
    assert background.version == first.version
    assert background.persona_revision == first.persona_revision + 1
    revisions.assert_called_once_with(f"persona:{SCOPE.session_key}", background.persona)

    revisions.reset_mock()
    runtime.config.judge.timeout_seconds += 1
    runtime.bindings.configure(runtime.config)
    assert runtime.bindings.resolve(SCOPE) == background
    revisions.assert_not_called()

    (runtime.roles / "other.md").write_text("OTHER PERSONA", encoding="utf-8")
    runtime.config.scopes[0].role_id = "other"
    changed = runtime.bindings.resolve(SCOPE)
    assert changed.version == str(int(first.version) + 1)
    assert changed.persona_revision == background.persona_revision + 1
    revisions.assert_any_call(
        f"scope:{SCOPE.session_key}", json.dumps({"role_id": "other", "paused": False}, sort_keys=True),
    )
    assert revisions.call_count == 2

    runtime.config.scopes[0].paused = True
    paused = runtime.bindings.resolve(SCOPE)
    assert paused.version != changed.version
    assert paused.persona_revision == changed.persona_revision
    runtime.config.scopes[0].paused = False
    resumed = runtime.bindings.resolve(SCOPE)
    assert resumed.version != paused.version
    assert resumed.persona_revision == changed.persona_revision


def test_legacy_context_version_adoption_keeps_history_across_repeated_resolution(runtime):
    session = Session(SCOPE.session_key, metadata={"_context_version": "legacy-social-version"})
    session.add_message("user", "EARLIER GROUP HISTORY")
    runtime.policy_for_session(session)
    assert session.last_archived == 0
    assert session.get_history()[0]["content"] == "EARLIER GROUP HISTORY"
    assert session.metadata["_social_mode"] == "social"
    assert "_context_version" not in session.metadata
    runtime.policy_for_session(session)
    assert session.last_archived == 0
    assert session.get_history()[0]["content"] == "EARLIER GROUP HISTORY"


def test_resolve_rejects_role_outside_authorized_directory_without_binding(runtime, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE PERSONA", encoding="utf-8")
    role = runtime.roles / "friend.md"
    role.unlink()
    role.symlink_to(outside)
    changes = runtime.store.db.total_changes
    with pytest.raises(PermissionError, match="role file is outside the authorized directory"):
        runtime.bindings.resolve(SCOPE)
    assert runtime.store.db.total_changes == changes


def test_resolve_rejects_unauthorized_scope(runtime):
    with pytest.raises(PermissionError, match="group/topic is not authorized for social mode"):
        runtime.bindings.resolve(ConversationScope("telegram", -999, 42))


@pytest.mark.parametrize("content", [None, "", " " * 32001, b"\xff"])
def test_unavailable_role_keeps_original_placeholder(runtime, content):
    role = runtime.roles / "friend.md"
    if content is None:
        role.unlink()
    elif isinstance(content, bytes):
        role.write_bytes(content)
    else:
        role.write_text(content, encoding="utf-8")
    resolved = runtime.bindings.resolve(SCOPE)
    assert (
        f"Role file unavailable: {role.resolve()}. Use ordinary file tools to inspect or repair it when requested."
    ) in resolved.persona


@pytest.mark.parametrize("require_claim", [False, True])
def test_validate_context_does_not_write_database(runtime, require_claim):
    runtime.sync_scope(SCOPE)
    ctx = RequestContext(
        channel=SCOPE.channel, chat_id=str(SCOPE.chat_id), session_key=SCOPE.session_key,
        turn_id="test-turn", metadata={"message_thread_id": SCOPE.thread_id},
    )
    if require_claim:
        with runtime.store.db:
            runtime.store.db.execute(
                "INSERT INTO wakes(id,scope,version,turn_id,trigger_seq,status,created,expires,due,requested) "
                "VALUES(?,?,?,?,1,'running',?,?,?,?)",
                ("claimed-wake", SCOPE.session_key, runtime.store.state(SCOPE.session_key)["version"],
                 ctx.turn_id, runtime.now(), runtime.now() + 120, runtime.now(), runtime.now()),
            )
        ctx.metadata["_social_job"] = "claimed-wake"
    # Validation must not resolve edited personas, even when checking a claimed wake.
    (runtime.roles / "friend.md").write_text("CHANGED PERSONA", encoding="utf-8")
    changes = runtime.store.db.total_changes
    assert runtime.validate_context(ctx, require_claim=require_claim) == SCOPE
    assert runtime.validate_context(ctx, require_claim=require_claim) == SCOPE
    assert runtime.store.db.total_changes == changes


@pytest.mark.parametrize("change", ["config", "role_text", "background", "role_id"])
def test_policy_changes_preserve_history_and_pending_metadata(runtime, change):
    session = Session(SCOPE.session_key)
    first = runtime.policy_for_session(session)
    session.add_message("user", "EARLIER GROUP HISTORY")
    session.provider_state = ProviderConversationState(
        kind="openai_responses", provider="test", model="offline", version=1, payload={},
    )
    provider_state = session.provider_state
    pending = {"_last_summary": {"text": "EARLIER SUMMARY"},
               "runtime_checkpoint": {"phase": "tool_calls"},
               "pending_user_turn": {"text": "pending"}, "goal": {"objective": "pending"}}
    session.metadata.update(pending)
    if change == "config":
        runtime.config.judge.timeout_seconds += 1
    elif change == "role_text":
        (runtime.roles / "friend.md").write_text("CHANGED PERSONA", encoding="utf-8")
    elif change == "background":
        runtime.config.scopes[0].background = "CHANGED BACKGROUND"
    else:
        (runtime.roles / "other.md").write_text("OTHER PERSONA", encoding="utf-8")
        runtime.config.scopes[0].role_id = "other"
    runtime.configure(runtime.config)
    policy = runtime.policy_for_session(session)
    assert policy.context_version == first.context_version == f"social:{SCOPE.session_key}"
    assert session.get_history()[0]["content"] == "EARLIER GROUP HISTORY"
    assert session.last_archived == 0
    assert all(session.metadata[key] == value for key, value in pending.items())
    assert session.provider_state is (provider_state if change == "config" else None)


def test_only_social_ordinary_mode_switch_truncates_history(runtime):
    session = Session(SCOPE.session_key)
    runtime.policy_for_session(session)
    session.add_message("user", "SOCIAL HISTORY")
    runtime.config.enabled = False
    runtime.configure(runtime.config)
    policy = runtime.policy_for_session(session)
    assert not policy.tool_only
    assert session.get_history() == []
    assert session.metadata["_social_mode"] == "ordinary"
    session.add_message("user", "ORDINARY HISTORY")
    runtime.policy_for_session(session)
    assert session.get_history()[0]["content"] == "ORDINARY HISTORY"
    runtime.config.enabled = True
    runtime.configure(runtime.config)
    assert runtime.policy_for_session(session).tool_only
    assert session.get_history() == []
    assert session.metadata["_social_mode"] == "social"
