"""Bounded, on-demand image copies. No SDK, URLs or model-provided filesystem paths."""

from __future__ import annotations

import io
import os
import re
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from nanobot.channels.telegram.social.models import ConversationScope, MediaReference
from nanobot.config.schema import TelegramSocialConfig


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
