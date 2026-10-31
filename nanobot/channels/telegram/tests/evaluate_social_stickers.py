"""Opt-in actual-model sticker learning; Telegram reads only, all sends are fakes."""

import argparse
import asyncio
import inspect
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from loguru import logger
from telegram import Bot

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.runtime import TelegramChannel, TelegramConfig
from nanobot.channels.telegram.social.models import ConversationScope
from nanobot.channels.telegram.social.runtime import SOCIAL_PROTOCOL, TelegramSocialRuntime
from nanobot.config.loader import load_config
from nanobot.config.schema import TelegramSocialConfig, TelegramSocialScopeConfig
from nanobot.providers.factory import make_provider


async def evaluate(args):
    config = load_config(args.config)
    config.agents.defaults.fallback_models = []
    preset = config.resolve_preset()
    provider = make_provider(config, preset=preset)
    scope = ConversationScope("telegram", -10001, 53)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sticker-learning-") as directory:
        root = Path(directory)
        runtime = TelegramSocialRuntime(TelegramSocialConfig(enabled=True, send_cooldown_seconds=0,
            scopes=[TelegramSocialScopeConfig(chat_id=scope.chat_id, thread_id=scope.thread_id, role_id="friend")]),
            root, MessageBus())
        runtime.roles.joinpath("friend.md").write_text(Path("docs/harness/groupmate.md").read_text())
        runtime._persona(scope)
        publications = []

        async def sender(target, text, reply_to):
            publications.append({"scope": target.session_key, "content": text})
            return 9000 + len(publications)

        telegram_config = TelegramConfig.model_validate(config.channels.telegram)
        async with Bot(telegram_config.token) as bot:
            channel = TelegramChannel(TelegramConfig(social=runtime.config), runtime.bus, social=runtime)
            channel._app = SimpleNamespace(bot=bot)
            # No updater, polling, message handler or real send endpoint is started.
            runtime.set_sender(sender)
            runtime.set_media_services(channel._download_social_media, sender, channel._load_social_sticker_set)
            tools = {name: TelegramSocialTool(runtime, name) for name in ("social_sticker", "social_send", "social_wait")}
            schemas = [{"type": "function", "function": {"name": tool.name,
                "description": tool.description, "parameters": tool.parameters}} for tool in tools.values()]
            messages = [{"role": "system", "content": SOCIAL_PROTOCOL + "\n" + runtime.roles.joinpath("friend.md").read_text()},
                {"role": "user", "content": f"群友 Al：{args.pack} 自己看，这个系列也是你自己的表情包，你自己学一下，以后合适就用。"}]
            calls = []
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            started = time.monotonic()

            async def run_turn(turn):
                ctx = RequestContext(channel="telegram", chat_id=str(scope.chat_id), session_key=scope.session_key,
                    turn_id=turn, metadata={"message_thread_id": scope.thread_id})
                with request_context(ctx):
                    for _ in range(24):
                        response = await provider.chat(messages=messages, tools=schemas, model=preset.model,
                            max_tokens=min(preset.max_tokens, 8192), temperature=preset.temperature,
                            reasoning_effort=preset.reasoning_effort)
                        if response.usage:
                            for key in usage:
                                usage[key] += getattr(response.usage, key)
                        if response.finish_reason == "error":
                            raise RuntimeError(f"model failed: {response.error_kind or 'provider_error'}")
                        messages.append({"role": "assistant", "content": response.content or "",
                            "tool_calls": [call.to_openai_tool_call() for call in response.tool_calls]})
                        for call in response.tool_calls:
                            output = await tools[call.name].execute(**call.arguments)
                            summary = [{"type": block["type"], "text": block.get("text", "")} for block in output] if isinstance(output, list) else str(output)
                            calls.append({"name": call.name, "arguments": call.arguments, "result": summary})
                            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                            print(json.dumps({"tool": call.name, "action": call.arguments.get("action"),
                                "learned": sum(bool(row["note"]) for row in runtime.store.sticker_catalog(scope.session_key))}, ensure_ascii=False), flush=True)
                        if not response.tool_calls or ctx.attributes.get("social_deferred"):
                            break

            try:
                await run_turn("learn-request")
                learning_catalog = runtime.store.sticker_catalog(scope.session_key)
                messages.append({"role": "user", "content": "学得不错，本大爷现在得意得很，来张合适的表情，不用文字。"})
                await run_turn("sticker-request")
                catalog = runtime.store.sticker_catalog(scope.session_key)
                report = {"model": preset.model, "available": len(catalog),
                    "learned_after_request": sum(bool(row["note"]) for row in learning_catalog),
                    "learned": sum(bool(row["note"]) for row in catalog),
                    "confirmed_sticker_sends": runtime.store.db.execute("SELECT count(*) FROM actions WHERE kind='sticker' AND status='sent'").fetchone()[0],
                    "elapsed_seconds": round(time.monotonic() - started, 2), "usage": usage,
                    "tools": calls, "publications": publications, "real_telegram_sends": 0}
                (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                # Private verification artifact for seeding the owner's requested pack after deployment.
                exported = [dict(row) for row in runtime.store.db.execute("SELECT id,media_ref,note FROM stickers")]
                (args.output / "learned-stickers.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({key: value for key, value in report.items() if key not in {"tools", "publications"}}, ensure_ascii=False), flush=True)
                assert report["available"] == 120 and report["learned_after_request"] == 120
                assert report["confirmed_sticker_sends"] >= 1
            finally:
                runtime.close()
                close = getattr(provider, "close", None)
                if close:
                    result = close()
                    if inspect.isawaitable(result):
                        await result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pack", default="https://t.me/addstickers/LLM_Moe")
    logger.remove()
    asyncio.run(evaluate(parser.parse_args()))
