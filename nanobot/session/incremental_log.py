"""Append session messages and atomically commit their visible length and state.

The JSONL remains the message log. A small state file replaces the logical header
and provider state and bounds readers to committed bytes. Rewriting old messages
uses the existing full atomic replacement instead. Callers hold the store lock.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

_PROVIDER_LINE = re.compile(rb'^\s*\{\s*"_type"\s*:\s*"provider_state"\s*(?:,|\})')
_STAMP = tuple[int, int, int, int]


def state_path(path: Path) -> Path:
    return path.with_suffix(".state.json")


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("session state must be a JSON object")
    return cast(dict[str, Any], value)


def _stamp(path: Path) -> _STAMP:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _fsync_directory(path: Path) -> None:
    with suppress(PermissionError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno != errno.EINVAL:
                    raise
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class _Commit:
    header: dict[str, Any]
    provider_state: dict[str, Any] | None
    offset: int
    message_count: int
    revision: int
    save_revision: int = 0


def _read_commit(path: Path, handle: BinaryIO, base: dict[str, Any]) -> _Commit | None:
    target = state_path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("session state file must be a regular file")
    raw = _object(json.loads(target.read_text(encoding="utf-8")))
    revision = raw.get("storage_revision")
    if type(revision) is not int:
        raise ValueError("invalid session storage revision")
    # An atomic full replacement supersedes the older generation's state file.
    if revision != base.get("_storage_revision"):
        return None
    header = _object(raw.get("header"))
    offset, count = raw.get("committed_bytes"), raw.get("message_count")
    provider = raw.get("provider_state")
    save_revision = raw.get("save_revision", 0)
    if (
        raw.get("version") != 1
        or type(revision) is not int
        or type(offset) is not int
        or type(count) is not int
        or type(save_revision) is not int
        or save_revision < 0
        or count < 0
        or not 0 < offset <= os.fstat(handle.fileno()).st_size
        or header.get("_type") != "metadata"
        or header.get("key") != base.get("key")
        or header.get("_storage_revision") != revision
        or header.get("_message_count") != count
        or (provider is not None and not isinstance(provider, dict))
    ):
        raise ValueError("invalid committed session state; restore from backup")
    position = handle.tell()
    handle.seek(offset - 1)
    terminated = handle.read(1) == b"\n"
    handle.seek(position)
    if not terminated:
        raise ValueError("session commit does not end at a complete JSONL record")
    return _Commit(header, None if provider is None else _object(provider), offset, count, revision, save_revision)


class SessionLogReader:
    """Stream the logical JSONL view, including the latest committed header."""

    def __init__(self, path: Path):
        self._handle = path.open("rb")
        try:
            first = self._handle.readline()
            try:
                base = _object(json.loads(first))
            except (ValueError, TypeError):
                if state_path(path).exists():
                    raise ValueError("cannot recover session state without its base header") from None
                base = {}
            self.base_header = base
            self.commit = _read_commit(path, self._handle, base)
            self._lines = self._iter_lines(first)
        except BaseException:
            self._handle.close()
            raise

    def _iter_lines(self, first: bytes) -> Iterator[str]:
        commit = self.commit
        if commit is None:
            yield first.decode("utf-8")
            for line in self._handle:
                yield line.decode("utf-8")
            return
        yield json.dumps(commit.header, ensure_ascii=False) + "\n"
        if commit.provider_state is not None:
            yield json.dumps({"_type": "provider_state", "state": commit.provider_state}, ensure_ascii=False) + "\n"
        count = 0
        while self._handle.tell() < commit.offset:
            remaining = commit.offset - self._handle.tell()
            line = self._handle.readline(remaining)
            if not line:
                raise ValueError("session log is shorter than its committed length")
            if _PROVIDER_LINE.match(line):
                continue
            if line.strip():
                count += 1
            yield line.decode("utf-8")
        if count != commit.message_count:
            raise ValueError("session message count does not match its commit")

    def readline(self) -> str:
        return next(self._lines, "")

    def __iter__(self) -> Iterator[str]:
        return self._lines

    def __enter__(self) -> SessionLogReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self._lines.close()
        self._handle.close()


def committed_mtime(path: Path) -> int:
    with SessionLogReader(path) as reader:
        return (state_path(path) if reader.commit is not None else path).stat().st_mtime_ns


def committed_revision(path: Path) -> tuple[int | None, int]:
    with SessionLogReader(path) as reader:
        if reader.commit is not None:
            return reader.commit.revision, reader.commit.save_revision
        revision = reader.base_header.get("_storage_revision")
        return (revision if type(revision) is int else None), 0


def _write_commit(path: Path, commit: _Commit, *, fsync: bool) -> None:
    target = state_path(path)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    payload = {
        "version": 1, "storage_revision": commit.revision, "save_revision": commit.save_revision,
        "committed_bytes": commit.offset, "message_count": commit.message_count,
        "header": commit.header, "provider_state": commit.provider_state,
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, target)
        if fsync:
            _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class _Baseline:
    commit: _Commit
    messages: tuple[dict[str, Any], ...]
    stamp: _STAMP


class IncrementalSessionLog:
    """A bounded direct-comparison cache; no hashes or content fingerprints."""

    def __init__(self, max_sessions: int = 16):
        self._baselines: OrderedDict[Path, _Baseline] = OrderedDict()
        self._max_sessions = max_sessions

    def discard(self, path: Path) -> None:
        self._baselines.pop(path, None)

    def remember(
        self, path: Path, header: dict[str, Any], provider: dict[str, Any] | None,
        messages: list[dict[str, Any]],
    ) -> None:
        self.discard(path)
        commit = _Commit(deepcopy(header), deepcopy(provider), path.stat().st_size,
                         len(messages), header["_storage_revision"])
        self._baselines[path] = _Baseline(commit, tuple(deepcopy(messages)), _stamp(path))
        while len(self._baselines) > self._max_sessions:
            self._baselines.popitem(last=False)

    def append(
        self, path: Path, header: dict[str, Any], provider: dict[str, Any] | None,
        messages: list[dict[str, Any]], *, fsync: bool,
    ) -> bool:
        baseline = self._baselines.get(path)
        if baseline is None:
            return False
        try:
            if _stamp(path) != baseline.stamp:
                return False
        except FileNotFoundError:
            return False
        count = len(baseline.messages)
        if len(messages) < count or any(old != messages[i] for i, old in enumerate(baseline.messages)):
            return False
        updated = dict(header, _storage_revision=baseline.commit.revision, _message_count=len(messages))
        try:
            with path.open("r+b") as handle:
                base = _object(json.loads(handle.readline()))
                existing = _read_commit(path, handle, base)
                previous = existing or baseline.commit
                if previous.offset != baseline.stamp[2] or previous.message_count != count:
                    return False
                delta = messages[count:]
                if not delta and updated == previous.header and provider == previous.provider_state:
                    if fsync:
                        os.fsync(handle.fileno())
                        if state_path(path).exists():
                            with state_path(path).open("rb") as state:
                                os.fsync(state.fileno())
                        _fsync_directory(path.parent)
                    return True
                # Install the old commit before touching the log. A process crash
                # during append must leave a valid bound for all future readers.
                if existing is None:
                    _write_commit(path, previous, fsync=fsync)
                handle.seek(previous.offset)
                for message in delta:
                    handle.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                handle.flush()
                if fsync:
                    os.fsync(handle.fileno())
                commit = _Commit(updated, provider, handle.tell(), len(messages), previous.revision,
                                 previous.save_revision + 1)
                _write_commit(path, commit, fsync=fsync)
            baseline.commit = _Commit(deepcopy(updated), deepcopy(provider), commit.offset,
                                      len(messages), commit.revision, commit.save_revision)
            baseline.messages += tuple(deepcopy(delta))
            baseline.stamp = _stamp(path)
            self._baselines.move_to_end(path)
            return True
        except BaseException:
            # Leave any tail intact: the old or newly published commit determines
            # visibility even if publication/fsync failed. The next save replaces it.
            self.discard(path)
            raise

    def update_metadata(self, path: Path, updates: dict[str, Any], *, fsync: bool) -> bool:
        with path.open("rb") as handle:
            base = _object(json.loads(handle.readline()))
            previous = _read_commit(path, handle, base)
            if previous is None:
                revision, count = base.get("_storage_revision"), base.get("_message_count")
                if type(revision) is not int or type(count) is not int:
                    return False  # Legacy files establish a full baseline once.
                provider = None
                line = handle.readline()
                if _PROVIDER_LINE.match(line):
                    record = _object(json.loads(line))
                    value = record.get("state")
                    provider = None if value is None else _object(value)
                previous = _Commit(base, provider, os.fstat(handle.fileno()).st_size, count, revision)
            header = deepcopy(previous.header)
            metadata = _object(header.get("metadata", {}))
            metadata.update(deepcopy(updates))
            header["metadata"] = metadata
            _write_commit(path, _Commit(header, previous.provider_state, previous.offset,
                                       previous.message_count, previous.revision, previous.save_revision), fsync=fsync)
        return True


    def materialize(self, path: Path) -> None:
        if not state_path(path).exists():
            return
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("refusing to materialize a non-regular session log")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with SessionLogReader(path) as source:
                header = _object(json.loads(source.readline()))
                header.pop("_storage_revision", None)
                header.pop("_message_count", None)
                with temporary.open("x", encoding="utf-8") as target:
                    temporary.chmod(0o600)
                    target.write(json.dumps(header, ensure_ascii=False) + "\n")
                    for line in source:
                        target.write(line)
                    target.flush()
                    os.fsync(target.fileno())
            os.replace(temporary, path)
            state_path(path).unlink(missing_ok=True)
            self.discard(path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
