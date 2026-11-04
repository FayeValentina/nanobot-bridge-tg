"""Gateway assembly and lifecycle for Telegram social mode."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.telegram_social import SOCIAL_TOOL_CLASSES
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.judge import make_judge_provider
from nanobot.channels.telegram.social.runtime import SOCIAL_TOOLS, TelegramSocialRuntime
from nanobot.config.schema import Config, TelegramSocialConfig


def social_config(config: Config, config_path: Path) -> TelegramSocialConfig:
    """Parse channels.telegram.social; default the Codex judge OAuth directory beside the instance config."""
    telegram = config.channels.model_dump(by_alias=True).get("telegram", {})
    social = TelegramSocialConfig.model_validate(
        cast(dict[str, Any], telegram).get("social", {}) if isinstance(telegram, dict) else {},
    )
    if social.judge.provider == "openai_codex" and social.judge.oauth_storage_dir is None:
        social.judge.oauth_storage_dir = str(config_path.parent / "telegram-social" / "oauth")
    return social


class SocialWiring:
    """Gateway-facing handle: the runtime plus its refresh/close lifecycle."""

    def __init__(self, runtime: TelegramSocialRuntime, tools: ToolRegistry, config_path: Path) -> None:
        self.runtime = runtime
        self.tools = tools
        self.config_path = config_path

    def connect_agent(self, main_busy: Callable[[], bool]) -> None:
        self.runtime.expressions.main_busy = main_busy
        self.runtime.jargon.main_busy = main_busy
        self.runtime.jargon.tools = self.tools

    def refresh(self, config: Config) -> None:
        self.runtime.configure(social_config(config, self.config_path))
        provider, version = make_judge_provider(config, self.runtime.config.judge)
        self.runtime.judging.set_provider(provider, version)
        self.runtime.expressions.configure_provider(config)
        self.runtime.jargon.configure_provider(config)
        self.runtime.selector.configure_provider(config)

    def close(self) -> None:
        self.runtime.close()


def build_social_runtime(
    config: Config, config_path: Path, bus: MessageBus, tools: ToolRegistry,
) -> SocialWiring | None:
    """None unless channels.telegram.enabled and its social.enabled are both true."""
    telegram = config.channels.model_dump(by_alias=True).get("telegram", {})
    social = social_config(config, config_path)
    if not telegram.get("enabled") or not social.enabled:
        return None
    provider, version = make_judge_provider(config, social.judge)
    runtime = TelegramSocialRuntime(
        social, config_path.parent / "telegram-social", bus,
        judge_provider=provider, judge_provider_version=version,
    )
    runtime.expressions.configure_provider(config)
    runtime.jargon.configure_provider(config)
    runtime.selector.configure_provider(config)
    for name in sorted(SOCIAL_TOOLS):
        tools.register(SOCIAL_TOOL_CLASSES[name](runtime))
    return SocialWiring(runtime, tools, config_path)
