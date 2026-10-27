"""Deployment integrity and failed-upgrade recovery, without Docker or credentials."""

import io
import json
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="local deployment scripts target Linux")

from scripts.bridge_receive import unpack
from scripts.bridge_release import (
    ReleaseManager,
    container_config,
    digest,
    verify_release,
    write_json,
)

OLD = "a" * 40
NEW = "b" * 40


def release(root: Path, revision: str) -> Path:
    directory = root / "releases" / revision
    directory.mkdir(parents=True)
    wheel = "nanobot_ai-0.3.5-py3-none-any.whl"
    (directory / wheel).write_bytes(b"test wheel")
    (directory / "image.tar.gz").write_bytes(b"test image")
    write_json(directory / "release.json", {
        "schema": 1, "revision": revision, "image": f"nanobot-bridge-tg:sha-{revision}",
        "image_id": "sha256:" + "c" * 64, "architecture": "arm64", "wheel": wheel,
    })
    names = ["release.json", "image.tar.gz", wheel]
    (directory / "SHA256SUMS").write_text("".join(f"{digest(directory / n)}  {n}\n" for n in names))
    return directory


def test_release_detects_changed_image_before_loading(tmp_path):
    directory = release(tmp_path, OLD)
    assert verify_release(directory)["revision"] == OLD
    (directory / "image.tar.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        verify_release(directory)


@pytest.mark.parametrize("name,kind", [("../config.json", "file"), ("release.json", "symlink"), ("evil.sh", "file")])
def test_receiver_rejects_unsafe_archive_members(tmp_path, name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
        archive.addfile(member)
    stream.seek(0)
    with pytest.raises(ValueError):
        unpack(stream, tmp_path)


def test_receiver_accepts_only_a_complete_verified_bundle(tmp_path):
    source = release(tmp_path, OLD)
    target = tmp_path / "incoming"
    target.mkdir()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for path in source.iterdir():
            archive.add(path, arcname=path.name)
    stream.seek(0)
    unpack(stream, target)
    assert verify_release(target)["revision"] == OLD


def test_container_config_preserves_credentials_and_source_values():
    config = {"providers": {"deepseek": {"apiKey": "private"}},
              "channels": {"telegram": {"token": "bot-secret"},
                           "websocket": {"tokenIssueSecret": "web-secret"}}}
    result = container_config(config)
    assert result["providers"]["deepseek"]["apiKey"] == "private"
    assert result["channels"]["telegram"]["token"] == "bot-secret"
    assert result["channels"]["websocket"]["tokenIssueSecret"] == "web-secret"
    assert result["agents"]["defaults"]["workspace"] == "/data/workspace"
    assert result["gateway"]["host"] == "0.0.0.0"


class FakeManager(ReleaseManager):
    fail_candidate = False

    def smoke(self, revision):
        self.release(revision)

    def _compose(self, revision, *args):
        self.calls.append((revision, args))
        if revision == NEW and args[0] == "up":
            (self.data / "application-state").write_text("candidate changed data")
        return "fake-container"

    def _wait(self, revision):
        if revision == NEW and self.fail_candidate:
            raise RuntimeError("candidate unhealthy")


def manager(tmp_path):
    instance = FakeManager(tmp_path / "deployment")
    instance.calls = []
    instance.data.mkdir(parents=True)
    write_json(instance.data / "config.json", {})
    (instance.data / "application-state").write_text("old data")
    write_json(instance.state_path, {"current": OLD, "previous": None})
    release(instance.root, OLD)
    release(instance.root, NEW)
    return instance


def test_failed_upgrade_restores_data_and_previous_running_version(tmp_path):
    instance = manager(tmp_path)
    instance.fail_candidate = True
    with pytest.raises(RuntimeError, match="unhealthy"):
        instance.deploy(NEW)
    assert instance.state()["current"] == OLD
    assert (instance.data / "application-state").read_text() == "old data"
    assert (OLD, ("up", "-d", "--no-deps", "gateway")) in instance.calls
    assert list(instance.data.parent.glob("failed-data-*"))


def test_explicit_rollback_restores_snapshot_and_release_state(tmp_path):
    instance = manager(tmp_path)
    instance.deploy(NEW)
    assert instance.state()["current"] == NEW
    instance.rollback()
    assert instance.state()["current"] == OLD
    assert (instance.data / "application-state").read_text() == "old data"


def test_same_release_is_health_checked_without_restarting(tmp_path):
    instance = manager(tmp_path)
    instance.deploy(OLD)
    assert instance.calls == []


def test_release_export_roundtrip(tmp_path):
    instance = manager(tmp_path)
    bundle = tmp_path / "release.tar"
    instance.export(OLD, bundle)
    target = tmp_path / "received"
    target.mkdir()
    with bundle.open("rb") as stream:
        unpack(stream, target)
    assert json.loads((target / "release.json").read_text())["revision"] == OLD


def test_snapshot_failure_restarts_previous_release(tmp_path, monkeypatch):
    instance = manager(tmp_path)

    def fail_backup(_state):
        raise OSError("disk full")

    monkeypatch.setattr(instance, "_backup", fail_backup)
    with pytest.raises(OSError, match="disk full"):
        instance.deploy(NEW)
    assert instance.state()["current"] == OLD
    assert (OLD, ("up", "-d", "--no-deps", "gateway")) in instance.calls


def test_failed_explicit_rollback_recovers_current_release(tmp_path, monkeypatch):
    instance = manager(tmp_path)
    instance.deploy(NEW)

    def wait(revision):
        if revision == OLD:
            raise RuntimeError("previous release cannot start")

    monkeypatch.setattr(instance, "_wait", wait)
    with pytest.raises(RuntimeError, match="cannot start"):
        instance.rollback()
    assert instance.state()["current"] == NEW
    assert (instance.data / "application-state").read_text() == "candidate changed data"
    assert (NEW, ("up", "-d", "--no-deps", "gateway")) in instance.calls


def test_separate_roots_have_separate_projects_and_persist_ports(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_WEB_PORT", "8766")
    first = ReleaseManager(tmp_path / "first")
    monkeypatch.setenv("BRIDGE_WEB_PORT", "8767")
    second = ReleaseManager(tmp_path / "second")
    assert first.settings["project"] != second.settings["project"]
    assert ReleaseManager(tmp_path / "first").settings["web_port"] == 8766
    assert second.settings["web_port"] == 8767
