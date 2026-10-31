"""Pack import, image learning, scope isolation and ordinary sticker delivery."""

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from nanobot.agent.tools.context import request_context
from nanobot.agent.tools.telegram_social import TelegramSocialTool
from nanobot.channels.telegram.social.models import StickerSet
from nanobot.channels.telegram.social.stickers import sticker_set_name
from nanobot.channels.telegram.tests.test_social_media import loader_for, reference
from nanobot.channels.telegram.tests.test_social_runtime import (
    A,
    B,
    agent,
    channel,
    context,
    message,
    social,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest

__all__ = ["social"]


def pack(count=3):
    return StickerSet(name="LLM_Moe", title="LLM小妹", stickers=tuple(
        reference(kind="sticker", unique_id=f"sticker-{i}", file_id=f"file-{i}",
                  emoji="🙂", sticker_set="LLM_Moe") for i in range(count)))


@pytest.mark.parametrize("value", ["LLM_Moe", "https://t.me/addstickers/LLM_Moe",
    "https://telegram.me/addstickers/LLM_Moe/", "tg://addstickers?set=LLM_Moe"])
def test_resolves_only_telegram_pack_names(value):
    assert sticker_set_name(value) == "LLM_Moe"


@pytest.mark.parametrize("value", ["https://evil.test/addstickers/LLM_Moe", "https://t.me@evil.test/addstickers/LLM_Moe",
    "https://t.me/other/LLM_Moe", "tg://addstickers?set=one&set=two", "../private", "", "a" * 65])
def test_pack_parser_rejects_unrelated_urls_and_paths(value):
    with pytest.raises(ValueError):
        sticker_set_name(value)


async def test_full_pack_import_is_idempotent_persistent_and_preserves_learning(social):
    tg = channel(social)
    loader_for(tg)
    social._sticker_set_loader = AsyncMock(return_value=pack(120))
    ctx = context(social)
    result = await social.stickers.import_set(ctx, "https://t.me/addstickers/LLM_Moe")
    assert (result["imported"], result["unlearned"]) == (120, 120)
    tg._app.bot.get_file.assert_not_awaited()
    assert social.store.sticker_catalog(B.session_key) == []
    social.store.annotate_stickers(A.session_key, {"sticker-0": "开心问好"})
    with social.store.db:
        social.store.db.execute("UPDATE stickers SET uses=3 WHERE id='sticker-0'")
    again = await social.stickers.import_set(ctx, "LLM_Moe")
    assert (again["imported"], again["unlearned"]) == (0, 119)
    assert social.store.list_stickers(A.session_key, "问好")[0]["uses"] == 3
    with pytest.raises(PermissionError):
        await social.stickers.view(context(social, B, turn="other"), sticker_id="sticker-0")
    assert social.store.db.execute("SELECT count(*) FROM stickers").fetchone()[0] == 120


async def test_pack_limit_is_atomic_and_pause_during_lookup_discards_result(social):
    loader = AsyncMock(return_value=pack())
    social._sticker_set_loader = loader
    social.config.sticker_limit = 2
    ctx = context(social)
    with pytest.raises(PermissionError, match="no stickers imported"):
        await social.stickers.import_set(ctx, "LLM_Moe")
    assert social.store.sticker_catalog(A.session_key) == []

    async def paused(_):
        social.store.pause(A.session_key)
        return pack()

    loader.side_effect = paused
    with pytest.raises(PermissionError, match="paused"):
        await social.stickers.import_set(ctx, "LLM_Moe")
    assert social.store.sticker_catalog(A.session_key) == []


async def test_view_annotate_search_and_send_through_tool(social):
    tg = channel(social)
    loader_for(tg)
    social._sticker_set_loader = AsyncMock(return_value=pack())
    tool = TelegramSocialTool(social, "social_sticker")
    ctx = context(social)
    with request_context(ctx):
        assert json.loads(await tool.execute(action="import_set", set_name="LLM_Moe"))["imported"] == 3
        result = await tool.execute(action="view", set_name="LLM_Moe", limit=2)
        with Image.open(io.BytesIO(base64.b64decode(result[0]["image_url"]["url"].split(",")[1]))) as image:
            assert image.size == (640, 348)
        rows = json.loads(result[1]["text"])["stickers"]
        assert [row["number"] for row in rows] == [1, 2]
        annotations = [{"sticker_id": row["sticker_id"], "note": "开心：接梗、得意地问好"} for row in rows]
        saved = json.loads(await tool.execute(action="annotate", notes=annotations))
        assert saved == {"annotated": 2, "unlearned": 1}
        next_batch = await tool.execute(action="view", set_name="LLM_Moe")
        assert json.loads(next_batch[1]["text"])["stickers"][0]["sticker_id"] == "sticker-2"
        found = json.loads(await tool.execute(action="list", query="接梗"))["stickers"]
        assert len(found) == 2
        receipt = json.loads(await tool.execute(action="send", sticker_id=found[0]["sticker_id"]))
        assert receipt["status"] == "sent"
    tg._app.bot.send_sticker.assert_awaited_once()


async def test_annotations_require_view_and_validate_whole_batch_before_writing(social):
    channel(social)
    social._sticker_set_loader = AsyncMock(return_value=pack())
    ctx = context(social)
    await social.stickers.import_set(ctx, "LLM_Moe")
    with pytest.raises(PermissionError, match="view"):
        social.stickers.annotate(ctx, [{"sticker_id": "sticker-0", "note": "invented"}])
    ctx.attributes["social_stickers_viewed"] = {"sticker-0"}
    with pytest.raises(PermissionError):
        social.stickers.annotate(ctx, [{"sticker_id": "sticker-0", "note": "ok"},
                                     {"sticker_id": "sticker-1", "note": "unseen"}])
    assert all(not row["note"] for row in social.store.sticker_catalog(A.session_key))


async def test_preview_budget_is_shared_and_failed_images_cannot_be_annotated(social):
    tg = channel(social)
    loader_for(tg, error=RuntimeError("offline"))
    social._sticker_set_loader = AsyncMock(return_value=pack())
    social.config.media_max_images_per_turn = 2
    ctx = context(social)
    await social.stickers.import_set(ctx, "LLM_Moe")
    result = await social.stickers.view(ctx)
    assert "unavailable" in result[0]["text"]
    assert not ctx.attributes.get("social_stickers_viewed")
    loader_for(tg)
    results = await asyncio.gather(*(social.stickers.view(ctx, limit=1) for _ in range(3)))
    assert sum(block["type"] == "image_url" for result in results for block in result) == 1


async def test_channel_adapter_normalizes_real_sdk_sticker_set(social):
    tg = channel(social)
    item = SimpleNamespace(file_id="telegram-file", file_unique_id="telegram-unique", file_size=400,
        width=512, height=512, emoji="🙂", is_animated=False, is_video=False, thumbnail=None)
    tg._app.bot.get_sticker_set = AsyncMock(return_value=SimpleNamespace(
        name="LLM_Moe", title="LLM小妹", sticker_type="regular", stickers=[item]))
    result = await social.stickers.import_set(context(social), "tg://addstickers?set=LLM_Moe")
    assert result["imported"] == 1
    assert social.store.sticker(A.session_key, "telegram-unique").file_id == "telegram-file"
    tg._app.bot.get_sticker_set.assert_awaited_once_with("LLM_Moe")


async def test_real_agent_learns_native_sheet_and_sends_in_same_turn(tmp_path, social):
    tg = channel(social)
    loader_for(tg)
    social._sticker_set_loader = AsyncMock(return_value=pack(1))

    def call(identity, **arguments):
        return LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id=identity, name="social_sticker", arguments=arguments)])

    loop = agent(tmp_path, social, [
        call("import", action="import_set", set_name="https://t.me/addstickers/LLM_Moe"),
        call("view", action="view", set_name="LLM_Moe"),
        call("annotate", action="annotate", notes=[{"sticker_id": "sticker-0", "note": "得意接梗"}]),
        call("send", action="send", sticker_id="sticker-0"),
        LLMResponse(content=""),
    ])
    await social.observe(message(text="@bot 学一下这包，以后合适就用"), directed=True)
    await asyncio.gather(*list(social.judging.tasks.values()))
    await social.run_due()
    await asyncio.gather(*list(social.judging.tasks.values()))
    await social.run_due()
    await loop._dispatch(social.bus.inbound.get_nowait())
    messages = loop.provider.chat_stream_with_retry.call_args.kwargs["messages"]
    assert any(block.get("type") == "image_url" for m in messages if m["role"] == "tool"
               and isinstance(m["content"], list) for block in m["content"])
    assert social.store.list_stickers(A.session_key, "接梗")[0]["uses"] == 1
    tg._app.bot.send_sticker.assert_awaited_once()
