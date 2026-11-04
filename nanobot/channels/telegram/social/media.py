"""Bounded, on-demand image copies. No SDK, URLs or model-provided filesystem paths."""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, ImageOps

from nanobot.agent.tools.context import RequestContext
from nanobot.channels.telegram.social.bindings import ScopeBindings
from nanobot.channels.telegram.social.models import ConversationScope, MediaReference
from nanobot.channels.telegram.social.store import SocialStore
from nanobot.config.schema import TelegramSocialConfig
from nanobot.utils.helpers import build_image_content_blocks

SocialMediaLoader = Callable[[MediaReference, int], Awaitable[bytes]]


class SocialMediaCache:
    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise ValueError("social media cache must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        for path in self.root.iterdir():
            if re.fullmatch(r"[0-9a-f]{64}\.(original|jpg)", path.name) and path.is_file() and not path.is_symlink():
                path.unlink()  # Disposable legacy entries; Telegram can supply them again.

    def _path(self, scope: ConversationScope, ref: MediaReference, suffix: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref.unique_id):
            raise ValueError("invalid Telegram media identity")
        path = self.root / f"{scope.chat_id}_{scope.thread_id}_{ref.unique_id}{suffix}"
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("invalid media cache path")
        return path

    def original(self, scope: ConversationScope, ref: MediaReference, limit: int) -> bytes | None:
        path = self._path(scope, ref, ".original")
        if not path.exists():
            return None
        if path.stat().st_size > limit:
            return None
        return path.read_bytes()

    def prepare(self, scope: ConversationScope, ref: MediaReference, raw: bytes,
                config: TelegramSocialConfig) -> tuple[bytes, str]:
        if not raw or len(raw) > config.media_max_bytes:
            raise ValueError("media exceeds the download byte limit or is empty")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.width * original.height > config.media_max_pixels:
                    raise ValueError("image exceeds the pixel limit")
                if original.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise ValueError("unsupported image format")
                if getattr(original, "is_animated", False):
                    raise ValueError("animated originals require a thumbnail in this release")
                image = ImageOps.exif_transpose(original)
                image.thumbnail((config.media_max_edge, config.media_max_edge))
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=80)
        normalized = output.getvalue()
        if len(normalized) > 1024 * 1024:
            raise ValueError("model image exceeds one MiB")
        self._save_pair(scope, ref, raw, normalized, config.media_cache_bytes)
        return normalized, str(self._path(scope, ref, ".jpg"))

    def save_preview(self, scope: ConversationScope, raw: bytes, limit: int) -> str:
        """Store a locally rendered sheet; returns its JPEG path."""
        preview = MediaReference(kind="photo", file_id="local-preview", unique_id="preview-" + uuid4().hex)
        self._save_pair(scope, preview, raw, raw, limit)
        return str(self._path(scope, preview, ".jpg"))

    def _save_pair(self, scope: ConversationScope, ref: MediaReference, original: bytes,
                   normalized: bytes, budget: int) -> None:
        paths = (self._path(scope, ref, ".original"), self._path(scope, ref, ".jpg"))
        needed = len(original) + len(normalized)
        if needed > budget:
            raise ValueError("media exceeds the cache budget")
        files = [p for p in self.root.iterdir() if p.suffix in {".original", ".jpg"} and not p.is_symlink()]
        total = sum(p.stat().st_size for p in files if p not in paths)
        for old in sorted((p for p in files if p not in paths), key=lambda p: p.stat().st_mtime):
            if total + needed <= budget:
                break
            total -= old.stat().st_size
            old.unlink()
        for path, payload in zip(paths, (original, normalized), strict=True):
            fd, temporary = tempfile.mkstemp(prefix=".download-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


class SocialMedia:
    """Image download, preparation and the per-turn image budget shared by main, judge and sticker views."""

    def __init__(self, cache: SocialMediaCache, store: SocialStore, bindings: ScopeBindings,
                 config: Callable[[], TelegramSocialConfig]) -> None:
        self.cache = cache
        self.store = store
        self.bindings = bindings
        self.config = config
        self.lock = asyncio.Lock()
        self.loader: SocialMediaLoader | None = None

    async def get(self, ctx: RequestContext, message_id: int) -> list[dict[str, Any]]:
        scope = self.bindings.validate_context(ctx)
        refs = self.store.messages.media_for_message(scope.session_key, message_id)
        if self.loader is None:
            return [{"type": "text", "text": "Media unavailable: Telegram downloader is offline."}]
        blocks: list[dict[str, Any]] = []
        # One lock bounds concurrent downloads and the per-turn model image total.
        async with self.lock:
            for original in refs:
                self.bindings.validate_context(ctx)
                used = int(ctx.attributes.get("social_images", 0))
                if used >= self.config().media_max_images_per_turn:
                    blocks.append({"type": "text", "text": "Per-turn media image limit reached."})
                    break
                ref = original
                thumbnail = original.animated or original.video or original.kind in {"video", "animation"}
                if thumbnail:
                    if not original.thumbnail_id:
                        blocks.append({"type": "text", "text": f"{original.summary()}: no thumbnail; motion not decoded."})
                        continue
                    ref = original.model_copy(update={"file_id": original.thumbnail_id,
                                                      "size": None, "width": 0, "height": 0})
                ctx.attributes["social_images"] = used + 1
                blocks.extend(await self.prepare(scope, ref,
                    f"Message {message_id}: {original.summary()}" + ("; thumbnail only, motion not decoded" if thumbnail else ""),
                    validate=lambda: self.bindings.validate_context(ctx)))
        return blocks or [{"type": "text", "text": "This message has no supported visual media."}]

    async def prepare(self, scope: ConversationScope, ref: MediaReference, label: str, *,
                      validate: Callable[[], object], max_edge: int | None = None) -> list[dict[str, Any]]:
        """Shared main/judge image preparation; callers hold the media lock and validate their own turn/batch."""
        validate()
        if self.loader is None:
            return [{"type": "text", "text": f"{label}: media unavailable; downloader is offline."}]
        try:
            if ref.size is not None and ref.size > self.config().media_max_bytes:
                raise ValueError("media exceeds the byte limit")
            if ref.width * ref.height > self.config().media_max_pixels:
                raise ValueError("media exceeds the pixel limit")
            raw = self.cache.original(scope, ref, self.config().media_max_bytes)
            if raw is None:
                raw = await asyncio.wait_for(self.loader(ref, self.config().media_max_bytes), timeout=20)
            validate()
            config = self.config().model_copy(update={"media_max_edge": min(max_edge, self.config().media_max_edge)}) if max_edge else self.config()
            image, path = await asyncio.to_thread(self.cache.prepare, scope, ref, raw, config)
            validate()
            return build_image_content_blocks(image, "image/jpeg", path, label)
        except (asyncio.CancelledError, PermissionError):
            raise
        except Exception:
            return [{"type": "text", "text": f"{label}: media unavailable, invalid or exceeds limits; do not infer image content."}]
