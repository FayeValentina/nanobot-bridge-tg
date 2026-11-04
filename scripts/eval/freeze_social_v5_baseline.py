"""Freeze reviewed real chat by explicit message IDs, read-only and without model calls.

Scope/sender aliases remove account identifiers; SELF and peer bots remain context
only. This exports an offline corpus and never feeds historical rows to live learning.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

EXTRACTION = [
    ("guess_after_sharing", "main", 6838, 6845, [6844]),
    ("self_deprecating_transport", "main", 6846, 6852, [6850]),
    ("identity_tease", "topic", 6874, 6879, [6878]),
    ("correct_date_and_photo", "main", 6891, 6908, [6895, 6902, 6906]),
]
EVALUATION = [
    ("identity_food_and_peer_correction", "main", 6913, 6921, [6913, 6916, 6919]),
    ("sharing_then_factual_question", "topic", 6922, 6927, [6922, 6923]),
    ("tease_with_old_itinerary", "topic", 6931, 6936, [6934]),
    ("sticker_banter_without_request", "topic", 6937, 6939, [6937]),
    ("praise_sticker_without_request", "topic", 6942, 6944, [6942]),
    ("token_is_actual_topic", "topic", 6956, 6958, [6956]),
    ("pacific_bath_and_retort", "topic", 6978, 6989, [6981, 6984, 6987]),
    ("explicit_help", "topic", 6994, 6996, [6994]),
    ("cancel_old_plan_then_sticker", "main", 6997, 7004, [6999, 7002]),
    ("question_mark_and_sticker_exchange", "topic", 7005, 7012, [7006, 7007, 7010]),
]


def freeze(database: Path, scopes: dict[str, str], humans: set[str], peer_bots: set[str],
           replacements: dict[str, str]) -> dict[str, object]:
    db = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    turn_aliases: dict[str, str] = {}
    sender_aliases: dict[str, str] = {}
    try:
        db.execute("BEGIN")
        corpus: dict[str, object] = {"version": 1, "source_schema": db.execute("PRAGMA user_version").fetchone()[0],
            "source_policy": "Explicit historical human roster reviewed for this offline corpus only; legacy DB source metadata is unknown. SELF/peer bots/media are context, never templates. No automatic historical backfill.",
            "split_policy": "Disjoint (scope alias, message_id), including ALL context and baseline outputs. Extraction < 6910; evaluation >= 6913.",
            "replay_policy": "For each target, pass messages through target only; later observed SELF messages are baseline outputs, never expected answers or learning evidence. Media markers alone do not supply image pixels.",
            "arms": ["frozen_baseline", "persona_only_without_library", "v5_with_library"],
            "metrics": ["context_fit", "naturalness", "unrequested_advice", "unrelated_catchphrases", "sticker_choice", "factual_target_accuracy", "requests_tokens_latency"],
            "extraction": [], "evaluation": []}
        split_keys: dict[str, set[tuple[str, int]]] = {}
        for split, segments in (("extraction", EXTRACTION), ("evaluation", EVALUATION)):
            episodes = []
            keys: set[tuple[str, int]] = set()
            for name, scope_alias, start, end, targets in segments:
                scope = scopes[scope_alias]
                messages = []
                for row in db.execute("SELECT * FROM messages WHERE scope=? AND message_id BETWEEN ? AND ? ORDER BY seq", (scope, start, end)):
                    message_id, sender = int(row["message_id"]), str(row["sender_id"])
                    source = "self" if row["is_self"] else "peer_bot" if sender in peer_bots else "reviewed_human_text" if sender in humans else "unknown"
                    text = str(row["text"])
                    media = json.loads(row["media"])
                    if text == "[non-text message]":
                        source = "human_media" if sender in humans else source
                    for original, replacement in replacements.items():
                        text = text.replace(original, replacement)
                    sender_aliases.setdefault(sender, f"participant-{len(sender_aliases)+1}")
                    action = db.execute("SELECT turn_id,status FROM actions WHERE scope=? AND EXISTS (SELECT 1 FROM json_each(actions.message_ids) WHERE value=?) AND status='sent'", (scope, message_id)).fetchone()
                    turn = None
                    if action and action["turn_id"]:
                        raw_turn = str(action["turn_id"])
                        turn_aliases.setdefault(raw_turn, f"turn-{len(turn_aliases)+1}")
                        turn = turn_aliases[raw_turn]
                    messages.append({"message_id": message_id, "revision": int(row["revision"]), "seq": int(row["seq"]),
                                     "at": row["at"], "sender": sender_aliases[sender], "source": source,
                                     "text": text, "media": media, "reply_to": row["reply_to"],
                                     "learnable": split == "extraction" and source == "reviewed_human_text" and not text.startswith("/"),
                                     "confirmed_turn": turn})
                    keys.add((scope_alias, message_id))
                if not set(targets).issubset({item["message_id"] for item in messages}):
                    raise ValueError(f"source target missing from {name}; frozen corpus must not silently shrink")
                episodes.append({"id": name, "scope": scope_alias, "turn_targets": targets, "messages": messages})
            corpus[split] = episodes
            split_keys[split] = keys
        if split_keys["extraction"] & split_keys["evaluation"]:
            raise ValueError("extraction/evaluation source overlap")
        corpus["counts"] = {split: len(keys) for split, keys in split_keys.items()}
        return corpus
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--main-scope", required=True)
    parser.add_argument("--topic-scope", required=True)
    parser.add_argument("--human-id", action="append", required=True)
    parser.add_argument("--peer-bot-id", action="append", default=[])
    parser.add_argument("--replace", action="append", default=[], metavar="ORIGINAL=ALIAS")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = freeze(args.database, {"main": args.main_scope, "topic": args.topic_scope},
                    set(args.human_id), set(args.peer_bot_id), dict(item.split("=", 1) for item in args.replace))
    # Explicit export only; never overwrite a previously frozen evaluation by accident.
    with args.output.open("x", encoding="utf-8") as file:
        file.write(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(corpus["counts"]))


if __name__ == "__main__":
    main()
