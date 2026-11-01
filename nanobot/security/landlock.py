"""Standalone Linux exec sandbox. Invoked with Python -I before untrusted code.

Uses the Landlock filesystem ABI (v3+) without namespaces or capabilities.
https://docs.kernel.org/userspace-api/landlock.html
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import stat
import sys
from pathlib import Path


class _Ruleset(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def restrict_filesystem(read_roots: list[Path], write_roots: list[Path]) -> None:
    if sys.platform != "linux" or platform.machine() not in {"aarch64", "x86_64"}:
        raise RuntimeError("Landlock requires Linux on aarch64 or x86_64")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def checked(result: int) -> int:
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    abi = checked(libc.syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), 1))
    if abi < 3:
        raise RuntimeError("Landlock ABI 3 or newer is required; refusing unsandboxed exec")
    # ABI 3 includes rename/link confinement and file truncation. ABI 5 adds
    # device ioctls; ABI 6 can scope signals and abstract Unix sockets too.
    rights = (1 << (16 if abi >= 5 else 15)) - 1
    readonly = (1 << 0) | (1 << 2) | (1 << 3)
    file_rights = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14)
    attr = _Ruleset(rights, 0, 3 if abi >= 6 else 0)
    size = ctypes.sizeof(attr) if abi >= 6 else ctypes.sizeof(ctypes.c_uint64)
    ruleset = checked(libc.syscall(444, ctypes.byref(attr), ctypes.c_size_t(size), 0))
    try:
        for roots, allowed in ((read_roots, readonly), (write_roots, rights)):
            for path in roots:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    mode = os.fstat(descriptor).st_mode
                    access = allowed
                    if not stat.S_ISDIR(mode):
                        mask = file_rights
                        if abi >= 5 and (stat.S_ISCHR(mode) or stat.S_ISBLK(mode)):
                            mask |= 1 << 15
                        access &= mask
                    rule = _PathRule(access, descriptor)
                    checked(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0))
                finally:
                    os.close(descriptor)
        checked(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        checked(libc.syscall(446, ruleset, 0))
    finally:
        os.close(ruleset)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--read", action="append", default=[])
    parser.add_argument("--write", action="append", default=[])
    parser.add_argument("--protect", action="append", default=[])
    parser.add_argument("--command", required=True)
    args = parser.parse_args()
    protected = [Path(p).resolve() for p in args.protect]

    def root(value: str) -> Path:
        path = Path(value).resolve(strict=True)
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
            raise PermissionError("Sandbox roots must not contain runtime credentials")
        return path

    workspace = root(args.workspace)
    reads = [root(p) for p in args.read]
    writes = [workspace, *(root(p) for p in args.write)]
    cwd = Path(args.cwd).resolve(strict=True)
    if not cwd.is_relative_to(workspace):
        raise PermissionError("Sandbox working directory must be inside the workspace")
    restrict_filesystem(reads, writes)
    os.chdir(cwd)
    os.environ["HOME"] = str(workspace)
    os.environ["TMPDIR"] = str(workspace)
    os.execv("/bin/sh", ["sh", "-c", args.command])


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(f"Sandbox could not start: {exc}", file=sys.stderr)
        raise SystemExit(126) from None
