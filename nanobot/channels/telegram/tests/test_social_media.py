"""M3 acceptance with real SQLite/Pillow and offline Telegram/provider fakes."""

import asyncio
import base64
import io
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from pydantic import ValidationError
from telegram import Update
from telegram.error import BadRequest, RetryAfter, TimedOut

from nanobot.agent.tools.context import request_context
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.channels.telegram.social.media import SocialMediaCache
from nanobot.channels.telegram.social.models import MediaReference
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.channels.telegram.tests.test_social_runtime import (
    A,
    B,
    agent,
    channel,
    context,
    message,
    social,
    telegram_update,
)
from nanobot.config.schema import TelegramSocialConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest

# Reuse the two-scope runtime fixture rather than duplicating its authorization setup.
__all__ = ["social"]


def picture(size=(40, 20), *, fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, "red").save(out, format=fmt)
    return out.getvalue()


def reference(*, kind="photo", file_id="download-file", unique_id="unique", **kwargs):
    return MediaReference(kind=kind, file_id=file_id, unique_id=unique_id, **kwargs)


def update_with_media(kind="photo", *, seq=1, reply=None, **values):
    raw = telegram_update(seq=seq).to_dict()
    msg = raw["message"]
    item = {"file_id": "download-file", "file_unique_id": "unique", "width": 40, "height": 20,
            "file_size": 200, **values}
    if kind == "sticker":
        item.update(type="regular", is_animated=values.get("is_animated", False),
                    is_video=values.get("is_video", False), emoji="🙂", set_name="local_set")
    if kind in {"video", "animation"}:
        item["duration"] = 1
    msg[kind] = [item] if kind == "photo" else item
    msg.pop("text", None)
    msg["caption"] = "hello"
    if reply:
        msg["reply_to_message"] = reply
    return Update.de_json(raw, None)


def loader_for(tg, raw=None, *, error=None):
    raw = picture() if raw is None else raw
    download = AsyncMock(return_value=bytearray(raw), side_effect=error)
    tg._app.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_size=len(raw), download_as_bytearray=download))
    tg._app.bot.send_sticker = AsyncMock(return_value=SimpleNamespace(message_id=601))
    return download


async def observe_sticker(social, *, seq=1, unique_id="unique", file_id="download-file", **values):
    ref = reference(kind="sticker", unique_id=unique_id, file_id=file_id, emoji="🙂", **values)
    await social.observe(message(seq=seq, media=("sticker",), media_refs=(ref,)), directed=False)
    return ref


@pytest.mark.parametrize("kind", ["photo", "sticker", "animation", "video"])
async def test_observation_saves_references_without_download_or_model(social, kind):
    tg = channel(social)
    loader_for(tg)
    update = update_with_media(kind)
    await tg._observe_social(update)
    tg._app.bot.get_file.assert_not_awaited()
    assert social.bus.inbound.empty()
    ref = social.store.media_for_message(A.session_key, 1)[0]
    assert (ref.kind, ref.file_id, ref.unique_id) == (kind, "download-file", "unique")
    view = json.loads(social.read_messages(context(social)))
    assert view["messages"][0]["media_details"]
    assert "download-file" not in json.dumps(view)


async def test_on_demand_image_is_native_bounded_and_cached(social):
    tg = channel(social)
    download = loader_for(tg, picture((1800, 900)))
    await tg._observe_social(update_with_media())
    ctx = context(social)
    blocks = await social.get_media(ctx, 1)
    assert blocks[0]["type"] == "image_url"
    raw = base64.b64decode(blocks[0]["image_url"]["url"].split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (1536, 768) and image.format == "JPEG"
    assert Path(blocks[0]["_meta"]["path"]).is_file()
    assert len(list(social.media.root.glob("*.original"))) == 1
    await social.get_media(ctx, 1)
    download.assert_awaited_once()


async def test_media_cannot_read_other_group_or_topic(social):
    tg = channel(social)
    loader_for(tg)
    await tg._observe_social(update_with_media())
    with pytest.raises(PermissionError, match="not observed"):
        await social.get_media(context(social, B, turn="other"), 1)
    tg._app.bot.get_file.assert_not_awaited()
    ctx = replace(context(social, turn="wrong-topic"), metadata={"message_thread_id": 43})
    with pytest.raises(PermissionError):
        await social.get_media(ctx, 1)


@pytest.mark.parametrize("failure", ["invalid", "large_bytes", "large_pixels", "download", "missing_size"])
async def test_media_failure_is_explicit_and_text_still_sends(social, failure):
    tg = channel(social)
    raw = b"broken" if failure == "invalid" else picture()
    loader_for(tg, raw, error=RuntimeError("offline") if failure == "download" else None)
    update = update_with_media(file_size=social.config.media_max_bytes + 1 if failure == "large_bytes" else 200,
                               width=100000 if failure == "large_pixels" else 40,
                               height=100000 if failure == "large_pixels" else 20)
    if failure == "missing_size":
        raw_update = update.to_dict()
        raw_update["message"]["photo"][0].pop("file_size")
        update = Update.de_json(raw_update, None)
        tg._app.bot.get_file.return_value.file_size = None
    await tg._observe_social(update)
    ctx = context(social)
    blocks = await social.get_media(ctx, 1)
    assert all(block["type"] == "text" for block in blocks)
    assert "unavailable" in blocks[0]["text"]
    assert (await social.send(ctx, "text still works", None))["status"] == "sent"
    if failure in {"large_bytes", "large_pixels"}:
        tg._app.bot.get_file.assert_not_awaited()


@pytest.mark.parametrize("kind,values", [
    ("animation", {}), ("video", {}), ("sticker", {"is_animated": True}),
    ("sticker", {"is_video": True}),
])
async def test_motion_uses_thumbnail_without_decoding_original(social, kind, values):
    tg = channel(social)
    loader_for(tg)
    update = update_with_media(kind, thumbnail={"file_id": "thumb", "file_unique_id": "thumb-unique", "width": 40, "height": 20}, **values)
    await tg._observe_social(update)
    blocks = await social.get_media(context(social), 1)
    tg._app.bot.get_file.assert_awaited_once_with("thumb")
    assert "thumbnail only" in blocks[1]["text"]


async def test_tgs_without_thumbnail_uses_metadata(social):
    tg = channel(social)
    loader_for(tg)
    await tg._observe_social(update_with_media("sticker", is_animated=True))
    blocks = await social.get_media(context(social), 1)
    assert blocks[0]["type"] == "text" and "no thumbnail" in blocks[0]["text"]
    tg._app.bot.get_file.assert_not_awaited()


async def test_image_budget_includes_concurrent_calls_and_cached_reads(social):
    tg = channel(social)
    loader_for(tg)
    await tg._observe_social(update_with_media())
    ctx = context(social)
    results = await asyncio.gather(*(social.get_media(ctx, 1) for _ in range(7)))
    assert sum(block["type"] == "image_url" for result in results for block in result) == 4


async def test_pause_during_download_discards_media_result(social):
    tg = channel(social)
    loader_for(tg)
    await tg._observe_social(update_with_media())
    ctx = context(social)

    async def interrupted_download():
        social.store.pause(A.session_key)
        return bytearray(picture())

    tg._app.bot.get_file.return_value.download_as_bytearray.side_effect = interrupted_download
    with pytest.raises(PermissionError, match="paused"):
        await social.get_media(ctx, 1)
    assert not list(social.media.root.iterdir())


async def test_reply_identifies_member_and_can_view_old_quoted_photo(social):
    tg = channel(social)
    loader_for(tg)
    reply = update_with_media(seq=99).to_dict()["message"]
    reply["from"] = {"id": 456, "is_bot": False, "first_name": "Other"}
    await tg._observe_social(update_with_media(reply=reply))
    assert social.bus.inbound.empty()  # Member-to-member replies are not direct addresses.
    ctx = context(social)
    view = json.loads(social.read_messages(ctx))
    quoted = view["messages"][0]["reply"]
    assert quoted["sender_id"] == "456" and not quoted["is_bot"]
    assert (await social.get_media(ctx, 99))[0]["type"] == "image_url"
    result = await social.send(ctx, "reply to the right member", 99)
    assert result["status"] == "sent"
    kwargs = tg._app.bot.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 42 and kwargs["reply_parameters"].message_id == 99


async def test_cross_topic_quoted_message_does_not_grant_media_or_reply(social):
    tg = channel(social)
    loader_for(tg)
    reply = update_with_media(seq=99).to_dict()["message"]
    reply["message_thread_id"] = 43
    await tg._observe_social(update_with_media(reply=reply))
    ctx = context(social)
    with pytest.raises(PermissionError):
        await social.get_media(ctx, 99)
    with pytest.raises(PermissionError):
        await social.send(ctx, "wrong topic", 99)


async def test_long_quote_fits_small_snapshot_without_losing_current_message(social):
    social.config.snapshot_chars = 2000
    tg = channel(social)
    reply = update_with_media(seq=99).to_dict()["message"]
    reply["caption"] = "old context " * 200
    reply["from"]["first_name"] = "R" * 120
    raw = update_with_media(reply=reply).to_dict()
    raw["message"]["caption"] = "new context " * 200
    raw["message"]["from"]["first_name"] = "N" * 120
    await tg._observe_social(Update.de_json(raw, None))
    view_text = social.read_messages(context(social))
    view = json.loads(view_text)
    assert len(view_text) <= 2000
    assert len(view["messages"]) == 1
    assert view["messages"][0]["text"].startswith("new context")
    assert view["messages"][0]["reply"]["text"].startswith("old context")


async def test_album_items_keep_identity_and_share_one_wake(social):
    social.config.coalesce_seconds = 1
    social.config.scopes[0].wake_on_messages = True
    tg = channel(social)
    loader_for(tg)
    for seq in (1, 2):
        raw = update_with_media(seq=seq).to_dict()
        raw["message"]["media_group_id"] = "album-1"
        await tg._observe_social(Update.de_json(raw, None))
    assert social.store.db.execute("SELECT COUNT(*) FROM wakes WHERE status='scheduled'").fetchone()[0] == 1
    ctx = context(social)
    view = json.loads(social.read_messages(ctx))
    assert [m["album_id"] for m in view["messages"]] == ["album-1", "album-1"]
    assert [m["message_id"] for m in view["messages"]] == [1, 2]


async def test_static_sticker_collect_search_dedup_and_receipt(social):
    tg = channel(social)
    loader_for(tg)
    await observe_sticker(social)
    ctx = context(social)
    sticker_id = social.collect_sticker(ctx, 1, "happy greeting")
    await observe_sticker(social, seq=2, file_id="fresh-file-id")
    assert social.collect_sticker(ctx, 2, None) == sticker_id
    assert social.list_stickers(ctx, "greeting") == [{"sticker_id": "unique", "note": "happy greeting", "uses": 0, "emoji": "🙂"}]
    result = await social.send_sticker(ctx, sticker_id, 1)
    assert result["status"] == "sent"
    repeated = await social.send_sticker(ctx, sticker_id, 1)
    assert repeated["action_id"] == result["action_id"] and repeated["status"] == "sent"
    kwargs = tg._app.bot.send_sticker.call_args.kwargs
    assert kwargs["sticker"] == "fresh-file-id"  # file_unique_id is only the collection key.
    assert kwargs["chat_id"] == A.chat_id and kwargs["message_thread_id"] == A.thread_id
    assert kwargs["reply_parameters"].message_id == 1
    assert social.list_stickers(ctx, "")[0]["uses"] == 1
    tg._app.bot.send_sticker.assert_awaited_once()
    tg._app.bot.send_message.assert_not_awaited()


async def test_sticker_library_and_notes_do_not_cross_scopes(social):
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, "private group note")
    other = context(social, B, turn="other-group")
    assert social.list_stickers(other, "") == []
    with pytest.raises(PermissionError):
        await social.send_sticker(other, "unique", None)
    with pytest.raises(PermissionError):
        social.collect_sticker(other, 1, "cross group")


async def test_collection_is_bounded_but_existing_entries_can_be_updated(social):
    social.config.sticker_limit = 1
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, "first")
    social.collect_sticker(ctx, 1, "updated")
    await observe_sticker(social, seq=2, unique_id="second")
    with pytest.raises(PermissionError, match="limit"):
        social.collect_sticker(ctx, 2, "too many")
    assert social.list_stickers(ctx, "")[0]["note"] == "updated"


@pytest.mark.parametrize("values", [{"animated": True}, {"video": True}])
async def test_animated_stickers_cannot_be_collected_or_sent(social, values):
    await observe_sticker(social, **values)
    with pytest.raises(ValueError, match="static"):
        social.collect_sticker(context(social), 1, None)


@pytest.mark.parametrize("failure,status", [(TimedOut("timeout"), "unknown"), (BadRequest("invalid file id"), "failed")])
async def test_sticker_failure_does_not_count_use_or_block_later_text(social, failure, status):
    tg = channel(social)
    loader_for(tg)
    tg._app.bot.send_sticker.side_effect = failure
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, None)
    assert (await social.send_sticker(ctx, "unique", None))["status"] == status
    assert social.list_stickers(ctx, "")[0]["uses"] == 0
    assert (await social.send(ctx, "still here", None))["status"] == "sent"
    if status == "unknown":
        social.store.finish(ctx.turn_id, "completed")
        with pytest.raises(PermissionError, match="unknown"):
            await social.send_sticker(context(social, turn="next"), "unique", None)
    tg._app.bot.send_sticker.assert_awaited_once()


async def test_stickers_share_text_budget_and_pause_gate(social):
    tg = channel(social)
    loader_for(tg)
    social.config.max_sends_per_hour = 1
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, None)
    await social.send(ctx, "one send", None)
    with pytest.raises(PermissionError, match="budget"):
        await social.send_sticker(ctx, "unique", None)
    social.store.pause(A.session_key)
    with pytest.raises(PermissionError):
        await social.send_sticker(ctx, "unique", None)
    tg._app.bot.send_sticker.assert_not_awaited()


async def test_sticker_429_reuses_action_and_counts_only_success(social):
    tg = channel(social)
    loader_for(tg)
    tg._app.bot.send_sticker.side_effect = [RetryAfter(1), SimpleNamespace(message_id=601)]
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, None)
    result = await social.send_sticker(ctx, "unique", 1)
    assert result["status"] == "queued" and social.list_stickers(ctx, "")[0]["uses"] == 0
    with social.store.db:
        social.store.db.execute("UPDATE actions SET due=? WHERE id=?", (social.now() - 1, result["action_id"]))
    await social.run_due()
    row = social.store.db.execute("SELECT * FROM actions WHERE id=?", (result["action_id"],)).fetchone()
    assert row["status"] == "sent" and row["attempts"] == 2
    assert social.list_stickers(ctx, "")[0]["uses"] == 1
    assert tg._app.bot.send_sticker.await_count == 2
    assert social.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1


async def test_restart_cancels_sticker_retry_without_resending(social):
    tg = channel(social)
    loader_for(tg)
    tg._app.bot.send_sticker.side_effect = RetryAfter(10)
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, None)
    action = await social.send_sticker(ctx, "unique", None)
    social.store.recovery_jobs()
    row = social.store.db.execute("SELECT status FROM actions WHERE id=?", (action["action_id"],)).fetchone()
    assert row[0] == "unknown"
    await social.run_due()
    tg._app.bot.send_sticker.assert_awaited_once()


async def test_pause_cancels_queued_sticker_without_use_count(social):
    tg = channel(social)
    loader_for(tg)
    tg._app.bot.send_sticker.side_effect = RetryAfter(10)
    await observe_sticker(social)
    ctx = context(social)
    social.collect_sticker(ctx, 1, None)
    result = await social.send_sticker(ctx, "unique", None)
    social.store.pause(A.session_key)
    await social.run_due()
    assert social.store.db.execute("SELECT status FROM actions WHERE id=?", (result["action_id"],)).fetchone()[0] == "failed"
    assert social.store.db.execute("SELECT uses FROM stickers").fetchone()[0] == 0
    tg._app.bot.send_sticker.assert_awaited_once()


async def test_real_agent_receives_image_tool_result_without_public_leak(tmp_path, social):
    tg = channel(social)
    loader_for(tg)
    await tg._observe_social(update_with_media())
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="image", name="social_get_media", arguments={"message_id": 1})]),
        LLMResponse(content="internal result"),
    ])
    await social.observe(message(seq=2, text="@bot look"), directed=True)
    await loop._dispatch(social.bus.inbound.get_nowait())
    calls = loop.provider.chat_stream_with_retry.call_args_list
    tools = [m for m in calls[-1].kwargs["messages"] if m["role"] == "tool"]
    assert any(b.get("type") == "image_url" for m in tools for b in m["content"])
    assert social.bus.outbound.empty()


async def test_real_agent_cannot_reset_image_budget_between_tool_rounds(tmp_path, social):
    social.config.media_max_images_per_turn = 1
    tg = channel(social)
    loader_for(tg)
    await tg._observe_social(update_with_media())
    loop = agent(tmp_path, social, [
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="first", name="social_get_media", arguments={"message_id": 1})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(id="second", name="social_get_media", arguments={"message_id": 1})]),
        LLMResponse(content="internal"),
    ])
    await social.observe(message(seq=2, text="@bot look"), directed=True)
    await loop._dispatch(social.bus.inbound.get_nowait())
    messages = loop.provider.chat_stream_with_retry.call_args.kwargs["messages"]
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert sum(b.get("type") == "image_url" for m in tool_messages for b in m["content"]) == 1
    assert "limit reached" in tool_messages[-1]["content"][0]["text"]


async def test_tools_reject_arbitrary_file_ids_targets_and_missing_inputs(social):
    tool = TelegramSocialTool(social, "social_sticker")
    assert tool.validate_params({"action": "send", "file_id": "arbitrary", "chat_id": -99})
    assert TelegramSocialTool(social, "social_get_media").validate_params({"message_id": 1, "path": "/tmp/private"})
    with request_context(context(social)):
        result = await tool.execute(action="send")
    assert result.is_error


def test_cache_preserves_original_and_evicts_under_budget(tmp_path):
    cache = SocialMediaCache(tmp_path / "cache")
    config = TelegramSocialConfig(media_cache_bytes=1024 * 1024)
    raw = picture() + b"\0" * 300000
    for i in range(20):
        cache.prepare(A, reference(unique_id=str(i)), raw, config)
    assert sum(p.stat().st_size for p in cache.root.iterdir()) <= config.media_cache_bytes
    assert cache.original(A, reference(unique_id="0"), config.media_max_bytes) is None
    assert cache.original(A, reference(unique_id="19"), config.media_max_bytes) == raw
    assert cache.original(B, reference(unique_id="19"), config.media_max_bytes) is None


def test_cache_rejects_symlink_and_oversized_pixel_payload(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        SocialMediaCache(link)
    cache = SocialMediaCache(tmp_path / "cache")
    with pytest.raises(ValueError, match="pixel"):
        cache.prepare(A, reference(), picture((40, 40)), TelegramSocialConfig(media_max_pixels=1024))
    assert not list(cache.root.iterdir())


def test_schema_v2_upgrade_preserves_messages_actions_and_watermarks(tmp_path):
    root = tmp_path / "v2"
    store = SocialStore(root)
    store.bind(A, "version")
    store.observe(message(text="old photo", media=("photo",)), 100)
    action = store.reserve_send(A.session_key, "version", "old text", 6)
    store.delivery(action, "sent", message_id=501)
    # Recreate the actual schema-2 shape, retaining existing data.
    for table in ("proactive_state", "memory_evidence", "memories", "memory_suppressions", "memory_resets"):
        store.db.execute(f"DROP TABLE {table}")
    store.db.execute("ALTER TABLE wakes DROP COLUMN proactive_source")
    store.db.execute("DROP TABLE stickers")
    for column in ("media_refs", "reply_context", "album_id"):
        store.db.execute(f"ALTER TABLE messages DROP COLUMN {column}")
    for column in ("kind", "media_ref"):
        store.db.execute(f"ALTER TABLE actions DROP COLUMN {column}")
    store.db.execute("UPDATE scopes SET read_through=1,seen_through=1")
    store.db.execute("PRAGMA user_version=2")
    store.db.commit()
    store.close()
    reopened = SocialStore(root)
    try:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert reopened.state(A.session_key)["read_through"] == 1
        row = reopened.db.execute("SELECT content,status,message_id,kind FROM actions").fetchone()
        assert tuple(row) == ("old text", "sent", 501, "text")
        assert reopened.media_for_message(A.session_key, 1) == []
        assert reopened.db.execute("SELECT text FROM messages").fetchone()[0] == "old photo"
    finally:
        reopened.close()


@pytest.mark.parametrize("values", [{"mediaMaxBytes": 0}, {"mediaMaxPixels": 1}, {"mediaMaxImagesPerTurn": 9}, {"stickerLimit": 0}])
def test_media_configuration_rejects_invalid_limits(values):
    with pytest.raises(ValidationError):
        TelegramSocialConfig.model_validate(values)


def test_damaged_sticker_database_is_not_recreated(tmp_path):
    root = tmp_path / "damaged"
    root.mkdir()
    raw = b"invalid sqlite database"
    (root / "social.sqlite3").write_bytes(raw)
    with pytest.raises(sqlite3.DatabaseError):
        SocialStore(root)
    assert (root / "social.sqlite3").read_bytes() == raw
