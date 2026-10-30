"""Opt-in real-model evaluation, synthetic input and temporary SQLite only.

Run with python -m nanobot.channels.telegram.tests.evaluate_social_judge
--oauth-dir <independent Bot OAuth directory> --output <report.json>.
Never starts a Telegram receiver, sends public messages or changes live read state.
"""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from loguru import logger

from nanobot.agent.tools.context import RequestContext
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.judge import make_judge_provider
from nanobot.channels.telegram.social.judge_models import JUDGE_PROMPT_VERSION
from nanobot.channels.telegram.social.models import (
    ConversationScope,
    ObservedMessage,
    QuotedMessage,
)
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.config.schema import (
    Config,
    TelegramSocialConfig,
    TelegramSocialJudgeConfig,
    TelegramSocialScopeConfig,
)


async def evaluate(args):
    settings = TelegramSocialJudgeConfig(oauth_storage_dir=args.oauth_dir, max_retries=0,
                                         max_fallbacks_per_hour=0, min_interval_seconds=0)
    provider, version = make_judge_provider(Config(), settings)
    cases = json.loads(args.cases.read_text())
    results = []
    for case in cases:
        if args.case and case["name"] not in args.case:
            continue
        with tempfile.TemporaryDirectory(prefix="social-judge-eval-") as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/friend.md").write_text(args.persona.read_text() if args.persona else "你是肥鱼，作为群友自然地参与群聊。")
            scope = ConversationScope("telegram", -10001, 53)
            config = TelegramSocialConfig(enabled=True, coalesce_seconds=0, cooldown_seconds=0, judge=settings,
                scopes=[TelegramSocialScopeConfig(chat_id=scope.chat_id, thread_id=53, role_id="friend",
                    nicknames=["肥鱼", "肥魚"], wake_on_question=case.get("standing_questions", False))])
            config.sleep_observation.enabled = case.get("observe_sleep", False)
            runtime = TelegramSocialRuntime(config, root, MessageBus(), judge_provider=provider, judge_provider_version=version)
            try:
                for seq, (sender, text) in enumerate(case["history"], 1):
                    runtime.store.observe(ObservedMessage(scope, seq, seq, sender, sender, time.time()-10, text,
                                                          is_self=sender == "bot"), 500)
                if case.get("wait") or case.get("observe_sleep"):
                    job = runtime.store.wake(scope.session_key, 1, 100, now=runtime.now())
                    ctx = RequestContext(channel="telegram", chat_id=str(scope.chat_id), session_key=scope.session_key,
                        turn_id="setup", metadata={"message_thread_id": 53, "_social_job": job},
                        attributes={"context_version": runtime.store.state(scope.session_key)["version"]})
                    runtime.admit_request(ctx)
                    if case.get("wait"):
                        runtime.set_wake(ctx, keywords=["到家"], senders=["456"], on_messages=False, ttl_seconds=60)
                        runtime.wait(ctx, infinite=True, purpose="messages", quiet_seconds=0.1)
                    else:
                        runtime.wait(ctx, 30, sleeping=True)
                    runtime.store.finish("setup", "completed")
                elif case.get("sleeping"):
                    with runtime.store.db:
                        runtime.store.db.execute("UPDATE scopes SET mode='sleeping',wait_infinite=1 WHERE key=?", (scope.session_key,))
                seq = len(case["history"]) + 1
                quote = QuotedMessage(message_id=53, sender_id="creator", name="Creator", timestamp=time.time()-1000,
                                      text="", is_topic_root=True) if case.get("topic_root") else None
                message = ObservedMessage(scope, seq, seq, case.get("sender", "123"), "Member", time.time(), case["text"],
                    reply_to=53 if quote else None, reply=quote, bot_username="fish_bot")
                started = time.monotonic()
                await runtime.observe(message, directed=False)
                await asyncio.sleep(0.15)  # Explicit message-wait coalescing, never a held agent lock.
                await runtime.run_due()
                await asyncio.gather(*list(runtime.judging.tasks.values()))
                await runtime.run_due()
                row = runtime.store.db.execute("SELECT * FROM judge_batches ORDER BY created DESC LIMIT 1").fetchone()
                decision = json.loads(row["decision"]) if row and row["decision"] else {}
                attempt = runtime.store.db.execute("SELECT latency_ms,input_tokens,output_tokens,total_tokens,error_code "
                                                   "FROM judge_attempts ORDER BY rowid DESC LIMIT 1").fetchone()
                snapshot = runtime.judging._restore(row["id"]) if row else None
                result = {"case": case["name"], "expected": case["expected"], "decision": decision,
                    "matched": decision.get("decision") == case["expected"] and decision.get("fulfills_intent") == case.get("fulfills", False),
                    "elapsed_ms": round((time.monotonic()-started)*1000), "attempt": dict(attempt) if attempt else None,
                    "full_chars": len(snapshot.model_dump_json()) if snapshot else 0,
                    "model_chars": len(snapshot.prompt_json()) if snapshot else 0,
                    "main_candidates": runtime.bus.inbound.qsize(), "status": row["status"] if row else "missing"}
                assert runtime.store.state(scope.session_key)["read_through"] == 0
                assert runtime.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
                # A setup wake is historical; it must not be delivered again.
                assert result["main_candidates"] == int(decision.get("decision") == "wake")
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                runtime.close()
    report = {"model": settings.model, "reasoning_effort": settings.reasoning_effort,
              "prompt_version": JUDGE_PROMPT_VERSION, "results": results}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    if not results or not all(result["matched"] for result in results):
        raise SystemExit("Semantic evaluation has mismatches; inspect report")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oauth-dir", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("fixtures") / "social_judge_v3c.json")
    parser.add_argument("--case", action="append")
    parser.add_argument("--persona", type=Path, help="Use the reviewed role file in the actual 600-character summary")
    logger.remove()
    asyncio.run(evaluate(parser.parse_args()))
