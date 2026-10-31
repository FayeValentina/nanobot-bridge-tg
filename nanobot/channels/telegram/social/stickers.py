"""Sticker-pack import and visual learning through the current group model's tools."""

# This channel-owned helper shares the runtime/cache's media lifecycle.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import io
import json
import math
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from PIL import Image, ImageDraw

from nanobot.agent.tools.context import RequestContext
from nanobot.channels.telegram.social.models import MediaReference
from nanobot.utils.helpers import build_image_content_blocks

if TYPE_CHECKING:
    from nanobot.channels.telegram.social.runtime import TelegramSocialRuntime


def sticker_set_name(value: str) -> str:
    """Extract only a Telegram set name; never fetch a user-supplied URL."""
    value = value.strip()
    if "://" in value:
        url = urlsplit(value)
        if url.scheme in {"https", "http"} and url.netloc.lower() in {"t.me", "telegram.me"}:
            match = re.fullmatch(r"/addstickers/([A-Za-z][A-Za-z0-9_]{0,63})/?", url.path)
            value = match[1] if match else ""
        elif url.scheme == "tg" and url.netloc == "addstickers" and not url.path:
            values = parse_qs(url.query).get("set", [])
            value = values[0] if len(values) == 1 else ""
        else:
            value = ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
        raise ValueError("provide a Telegram addstickers link or sticker set name")
    return value


class SocialStickers:
    def __init__(self, runtime: TelegramSocialRuntime) -> None:
        self.runtime = runtime

    async def import_set(self, ctx: RequestContext, value: str) -> dict[str, object]:
        runtime = self.runtime
        scope = runtime.validate_context(ctx)
        name = sticker_set_name(value)
        if runtime._sticker_set_loader is None:
            raise RuntimeError("Telegram sticker-set loader is offline")
        try:
            pack = await asyncio.wait_for(runtime._sticker_set_loader(name), timeout=30)
        except TimeoutError as exc:
            raise RuntimeError("Telegram sticker-set lookup timed out; nothing imported") from exc
        except (ValueError, PermissionError):
            raise
        except Exception as exc:
            raise RuntimeError("Telegram could not read this sticker set; check its name or availability") from exc
        runtime.validate_context(ctx)
        if pack.name.casefold() != name.casefold():
            raise ValueError("Telegram returned a different sticker set")
        refs = tuple(ref for ref in pack.stickers if not ref.animated and not ref.video)
        if not refs:
            raise ValueError("this release can only import static stickers; set contains none")
        imported = runtime.store.import_stickers(scope.session_key, refs, runtime.config.sticker_limit)
        catalog = runtime.store.sticker_catalog(scope.session_key, pack.name)
        return {"set_name": pack.name, "title": pack.title, "total": len(pack.stickers),
                "imported": imported, "available": len(catalog), "skipped_motion": len(pack.stickers) - len(refs),
                "unlearned": sum(not row["note"] for row in catalog),
                "next": "view this set's unlearned stickers, then annotate visible text, emotion and suitable uses"}

    async def view(self, ctx: RequestContext, *, set_name: str = "", sticker_id: str = "",
                   offset: int = 0, limit: int = 30, unlearned_only: bool = True) -> list[dict[str, Any]]:
        runtime = self.runtime
        scope = runtime.validate_context(ctx)
        if offset < 0 or not 1 <= limit <= 30:
            raise ValueError("sticker view requires offset >= 0 and limit between 1 and 30")
        name = sticker_set_name(set_name) if set_name else ""
        rows = runtime.store.sticker_catalog(scope.session_key, name)
        if sticker_id:
            rows = [row for row in rows if row["sticker_id"] == sticker_id]
            if not rows:
                raise PermissionError("sticker is not collected in this group/topic")
        elif unlearned_only:
            rows = [row for row in rows if not row["note"]]
        selected = rows[offset:offset + limit]
        if not selected:
            return [{"type": "text", "text": json.dumps({"stickers": [], "remaining": len(rows)}, ensure_ascii=False)}]
        async with runtime._media_lock:
            used = int(ctx.attributes.get("social_images", 0))
            if used >= runtime.config.media_max_images_per_turn:
                return [{"type": "text", "text": "Per-turn media image limit reached; stickers remain unlearned."}]
            ctx.attributes["social_images"] = used + 1
            columns = min(6, len(selected))
            sheet = Image.new("RGB", (columns * 320, math.ceil(len(selected) / columns) * 348), "white")
            draw = ImageDraw.Draw(sheet)
            viewed: set[str] = ctx.attributes.setdefault("social_stickers_viewed", set())
            successful: list[str] = []
            metadata: list[dict[str, object]] = []
            for index, row in enumerate(selected):
                runtime.validate_context(ctx)
                identity = str(row["sticker_id"])
                ref = runtime.store.sticker(scope.session_key, identity)
                blocks = await runtime.prepare_media(scope, ref, f"Sticker {index + 1}",
                    validate=lambda: runtime.validate_context(ctx), max_edge=320)
                x, y = index % columns * 320, index // columns * 348
                visible = blocks[0].get("type") == "image_url"
                if visible:
                    with Image.open(blocks[0]["_meta"]["path"]) as image:
                        sheet.paste(image, (x + (320 - image.width) // 2, y + (320 - image.height) // 2))
                    successful.append(identity)
                draw.text((x + 8, y + 326), f"{index + 1}" + (" unavailable" if not visible else ""), fill="black")
                metadata.append({**row, "number": index + 1, "image_available": visible})
            runtime.validate_context(ctx)
            if not successful:
                return [{"type": "text", "text": "Sticker images unavailable; nothing was visually learned."}]
            output = io.BytesIO()
            sheet.save(output, format="JPEG", quality=80)
            raw = output.getvalue()
            if len(raw) > 1024 * 1024:
                raise ValueError("sticker preview exceeds one MiB; request a smaller batch")
            preview = MediaReference(kind="photo", file_id="local-preview", unique_id="preview-" + uuid4().hex)
            runtime.media._save_pair(scope, preview, raw, raw, runtime.config.media_cache_bytes)
            viewed.update(successful)
            label = json.dumps({"stickers": metadata, "remaining": len(rows) - offset - len(selected),
                "instructions": "Numbers identify image tiles. Save accurate visual notes with annotate; never describe unavailable images. After annotating, view unlearned_only=true at offset=0 for the next batch."}, ensure_ascii=False)
            return build_image_content_blocks(raw, "image/jpeg", str(runtime.media._path(scope, preview, ".jpg")), label)

    def annotate(self, ctx: RequestContext, notes: list[dict[str, str]]) -> dict[str, object]:
        runtime = self.runtime
        scope = runtime.validate_context(ctx)
        if not 1 <= len(notes) <= 30:
            raise ValueError("provide 1–30 sticker notes")
        viewed: set[str] = ctx.attributes.get("social_stickers_viewed", set())
        updates: dict[str, str] = {}
        for item in notes:
            identity, note = item["sticker_id"], " ".join(item["note"].split())
            if not note or len(note) > 400:
                raise ValueError("sticker notes must contain 1–400 characters")
            if identity not in viewed:
                raise PermissionError("view this sticker image in the current turn before annotating it")
            runtime.store.sticker(scope.session_key, identity)
            if identity in updates:
                raise ValueError("duplicate sticker note")
            updates[identity] = note
        runtime.store.annotate_stickers(scope.session_key, updates)
        return {"annotated": len(updates), "unlearned": sum(
            not row["note"] for row in runtime.store.sticker_catalog(scope.session_key))}
