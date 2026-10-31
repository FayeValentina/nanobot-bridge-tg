"""Replay reviewed V5 historical extraction sources; never open a Telegram receiver/send.

Use --config for one actual-provider extraction per historical scope. The offline
corpus has fewer than ten human messages per scope, so this explicit replay uses
min_messages=1; deployed scheduling retains the default ten-message threshold.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram.social.expressions import (
    ExpressionCandidate,
    ExpressionEdit,
    ExtractionResult,
    make_expression_provider,
)
from nanobot.channels.telegram.social.models import ConversationScope, ObservedMessage
from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime
from nanobot.config.loader import load_config
from nanobot.config.schema import TelegramSocialConfig


async def replay(config_path: Path, output: Path) -> None:
    corpus_path = Path(__file__).resolve().parents[1] / "nanobot/channels/telegram/tests/fixtures/social_v5_baseline.json"
    corpus = json.loads(corpus_path.read_text())
    config = load_config(config_path)
    settings = config.channels.model_dump(by_alias=True).get("telegram", {}).get("social", {}).get("learning", {})
    scopes = {"main": ConversationScope("telegram", -1001, 0), "topic": ConversationScope("telegram", -1001, 42)}
    clock = [1000.]
    with tempfile.TemporaryDirectory(prefix="v5b-replay-") as directory:
        root = Path(directory)
        (root / "roles").mkdir()
        (root / "roles" / "guest.md").write_text("Offline historical extraction. No sender attached.")
        social = TelegramSocialConfig.model_validate({"enabled": True, "learning": {**settings, "minMessages": 1},
            "scopes": [{"chatId": scope.chat_id, "threadId": scope.thread_id, "roleId": "guest"} for scope in scopes.values()]})
        # The intermediary is never run; only the independent expression provider is called.
        runtime = TelegramSocialRuntime(social, root, MessageBus(), now=lambda: clock[0])
        provider, model = make_expression_provider(config, social.learning.expression)
        runtime.expressions.set_provider(provider, model)
        try:
            rows = sorted(((segment["scope"], msg) for segment in corpus["extraction"] for msg in segment["messages"]), key=lambda item: item[1]["at"])
            for alias, msg in rows:
                clock[0] = float(msg["at"])
                human = msg["learnable"] and msg["source"] == "reviewed_human_text"
                runtime.store.observe(ObservedMessage(scopes[alias], msg["message_id"], msg["message_id"], msg["sender"], msg["sender"], clock[0], msg["text"],
                    reply_to=msg["reply_to"], is_self=msg["source"] == "self", is_bot=msg["source"] == "bot",
                    learning_text=msg["text"] if human else None), 100)
            clock[0] += 1
            results = []
            for alias, scope in scopes.items():
                batch = runtime.store.learning.prepare(scope.session_key, "expression")
                if batch:
                    # Explicit offline extraction, not a scheduler timing probe.
                    await runtime.expressions.run(batch)
                entries = runtime.expressions.library.list(scope.session_key)
                results.append({"scope": alias, "batch": dict(runtime.store.db.execute("SELECT * FROM learning_batches WHERE id=?", (batch,)).fetchone()) if batch else None,
                    "entries": [runtime.expressions.library.get(scope.session_key, str(entry["id"])) for entry in entries]})
            checks = {"evidence_lookup": 0, "self_exclusion": True, "disable_no_revival": 0, "similar_no_revival": 0}
            for result in results:
                scope = scopes[result["scope"]]
                known = {msg["message_id"]: msg for alias, msg in rows if alias == result["scope"]}
                for entry in result["entries"]:
                    for evidence in entry["evidence"]:
                        original = known[evidence["message_id"]]
                        assert original["learnable"] and original["source"] == "reviewed_human_text"
                        assert original["revision"] == evidence["revision"] and original["sender"] == evidence["sender_id"]
                        assert evidence["excerpt"] in original["text"] and entry["expression"] in evidence["excerpt"]
                        checks["evidence_lookup"] += 1
                    runtime.expressions.library.change(scope.session_key, ExpressionEdit(action="disable", entry_id=entry["id"], expected_revision=entry["revision"]))
                    evidence = entry["evidence"][0]
                    # Replay an old real source as a new revision, then a deliberately
                    # constructed close variant under a fresh ID. No additional LLM calls.
                    for variant in (False, True):
                        clock[0] += 301
                        mid = evidence["message_id"] + (900000 if variant else 0)
                        text = entry["expression"] + ("啊" if variant else "")
                        runtime.store.observe(ObservedMessage(scope, 1000000 + mid, mid, evidence["sender_id"], evidence["sender_id"], clock[0], text,
                            learning_text=text, is_edit=not variant), 100)
                        batch = runtime.store.learning.prepare(scope.session_key, "expression")
                        assert batch
                        runtime.store.learning.start(batch)
                        candidate = ExpressionCandidate.model_validate({**{field: entry[field] for field in ("situation", "tone", "intent_category", "suitable", "unsuitable")},
                            "message_id": mid, "revision": 1 if variant else 2, "sender_id": evidence["sender_id"], "expression": text, "excerpt": text,
                            "scene_tags": json.loads(entry["scene_tags"]), "confidence": "clear", "reusable": True, "concrete_scene": True,
                            "private_or_named": False, "abusive_or_mechanical": False, "top_level": True})
                        assert runtime.expressions.library.commit(batch, ExtractionResult(expressions=[candidate]), runtime.expressions.library.generation(scope.session_key)) == []
                        checks["similar_no_revival" if variant else "disable_no_revival"] += 1
            assert runtime.bus.inbound_size == runtime.bus.outbound_size == 0
            report = {"model": model, "corpus": str(corpus_path.relative_to(Path(__file__).resolve().parents[1])),
                "mode": "actual_provider_offline_historical_extraction", "sent_messages": 0,
                "reviewed_human_messages": sum(bool(msg["learnable"]) for _, msg in rows), "checks": checks, "results": results}
            # Exclude frozen SELF/context copies from the report; the input fixture is authoritative.
            for result in results:
                if result["batch"]:
                    result["batch"].pop("sources", None)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"model": model, "expressions": sum(len(result["entries"]) for result in results), "batches": [result["batch"]["status"] for result in results], "report": str(output)}, ensure_ascii=False))
        finally:
            runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(replay(args.config, args.output))


if __name__ == "__main__":
    main()
