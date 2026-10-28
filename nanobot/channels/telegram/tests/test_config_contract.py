"""Keep settings choices compatible with the Telegram runtime configuration."""

from typing import get_args

import pytest
from pydantic import ValidationError

from nanobot.channels.telegram.manifest import SETUP_SPEC
from nanobot.channels.telegram.runtime import TelegramConfig


def test_group_policy_public_contract_matches_runtime() -> None:
    field = SETUP_SPEC.fields["groupPolicy"]
    runtime_field = TelegramConfig.model_fields["group_policy"]
    assert field.choices == frozenset(get_args(runtime_field.annotation))
    assert field.default == runtime_field.default == "mention"
    public_field = next(
        row for row in SETUP_SPEC.to_public_dict("telegram")["fields"]
        if row["field"] == "groupPolicy"
    )
    assert set(public_field["choices"]) == {"mention", "open"}
    for choice in field.choices:
        config = TelegramConfig.model_validate({"groupPolicy": choice})
        assert config.group_policy == choice
        assert config.model_dump(by_alias=True)["groupPolicy"] == choice


@pytest.mark.parametrize("policy", ["allowlist", "invalid", "", None])
def test_group_policy_rejects_unsupported_values(policy: object) -> None:
    with pytest.raises(ValidationError):
        TelegramConfig.model_validate({"groupPolicy": policy})


def test_social_configuration_is_opt_in_and_has_one_json_settings_contract() -> None:
    config = TelegramConfig()
    assert config.social.enabled is False and config.social.scopes == []
    assert SETUP_SPEC.fields["social"].kind == "json"
    assert SETUP_SPEC.route_field_types["social"] == "json"
    assert config.model_dump(by_alias=True)["social"]["enabled"] is False


def test_group_wide_social_binding_round_trips_and_legacy_topics_stay_exact() -> None:
    base = {"chatId": -1001, "roleId": "groupmate"}
    config = TelegramConfig.model_validate({"social": {"enabled": True, "scopes": [
        {**base, "allTopics": True}, {**base, "threadId": 53},
    ]}})
    group, topic = config.social.scopes
    assert group.all_topics and group.thread_id == 0
    assert not topic.all_topics and topic.thread_id == 53
    restored = TelegramConfig.model_validate(config.model_dump(by_alias=True))
    assert restored.social == config.social
