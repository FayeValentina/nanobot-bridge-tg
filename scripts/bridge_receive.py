#!/usr/bin/env python3
"""Restricted SSH receiver: accept a verified release bundle, never a shell command."""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

if __package__:
    from .bridge_release import DEFAULT_ROOT, WHEEL, ReleaseManager, digest, verify_release
else:
    from bridge_release import DEFAULT_ROOT, WHEEL, ReleaseManager, digest, verify_release


def unpack(stream, target: Path) -> None:
    seen = set()
    total = 0
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for entry in archive:
            name = entry.name
            if name in seen or not entry.isfile() or name != Path(name).name:
                raise ValueError("release archive contains an invalid entry")
            if name not in {"release.json", "SHA256SUMS", "image.tar.gz"} and not WHEEL.fullmatch(name):
                raise ValueError("release archive contains an unexpected file")
            total += entry.size
            if total > 3 * 1024**3 or entry.size < 0:
                raise ValueError("release archive exceeds the size limit")
            if name in {"release.json", "SHA256SUMS"} and entry.size > 64 * 1024:
                raise ValueError("release metadata exceeds the size limit")
            seen.add(name)
            source = archive.extractfile(entry)
            if source is None:
                raise ValueError("missing release archive content")
            with source, (target / name).open("xb") as destination:
                shutil.copyfileobj(source, destination)
    metadata = verify_release(target)
    if seen != {"release.json", "SHA256SUMS", "image.tar.gz", metadata["wheel"]}:
        raise ValueError("release archive inventory mismatch")


def receive(stream, root: Path = DEFAULT_ROOT) -> None:
    manager = ReleaseManager(root)
    with manager.lock(), tempfile.TemporaryDirectory(prefix="incoming-", dir=manager.root) as scratch:
        staging = Path(scratch)
        unpack(stream, staging)
        metadata = verify_release(staging)
        revision = metadata["revision"]
        target = manager.root / "releases" / revision
        if target.exists():
            verify_release(target)
            if digest(target / "SHA256SUMS") != digest(staging / "SHA256SUMS"):
                raise ValueError("an immutable release already exists with different artifacts")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(target)
        manager.deploy(revision)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        receive(sys.stdin.buffer)
    except Exception as error:
        raise SystemExit(f"Deployment rejected: {type(error).__name__}: {error}") from None
