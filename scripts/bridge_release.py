"""Build immutable local releases and deploy them with recoverable data snapshots."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path.home() / ".local/share/nanobot-bridge-tg-deploy"
REVISION = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
WHEEL = re.compile(r"nanobot_ai-[A-Za-z0-9_.+!-]+\.whl\Z")


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def output(args: list[str], **kwargs) -> str:
    return run(args, capture_output=True, text=True, **kwargs).stdout.strip()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    staging.chmod(0o600)
    staging.replace(path)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_release(directory: Path) -> dict:
    """Verify all payload bytes before reading their metadata or loading an image."""
    checksums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split("  ", 1)
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("invalid release checksum")
        if name in checksums or name != Path(name).name:
            raise ValueError("invalid release filename")
        checksums[name] = checksum
    metadata = json.loads((directory / "release.json").read_text())
    revision = metadata["revision"]
    wheel = metadata["wheel"]
    if not REVISION.fullmatch(revision) or not WHEEL.fullmatch(wheel):
        raise ValueError("invalid revision or wheel filename")
    expected = {"release.json", "image.tar.gz", wheel}
    if checksums.keys() != expected:
        raise ValueError("release checksum inventory does not match payload")
    for name, checksum in checksums.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != checksum:
            raise ValueError(f"release integrity check failed: {name}")
    if metadata.get("schema") != 1:
        raise ValueError("unsupported release schema")
    if metadata["image"] != f"nanobot-bridge-tg:sha-{revision}":
        raise ValueError("unexpected image name")
    if not IMAGE_ID.fullmatch(metadata["image_id"]):
        raise ValueError("invalid image identity")
    if metadata["architecture"] not in {"arm64", "amd64"}:
        raise ValueError("unsupported architecture")
    return metadata


def initial_config() -> dict:
    return {
        "agents": {"defaults": {"workspace": "/data/workspace", "timezone": "Asia/Tokyo"}},
        "gateway": {"host": "0.0.0.0", "port": 18790},
        "channels": {"websocket": {
            "enabled": True, "host": "0.0.0.0", "port": 8765,
            "tokenIssueSecret": secrets.token_urlsafe(32),
        }},
    }


def container_config(config: dict) -> dict:
    config.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = "/data/workspace"
    config.setdefault("gateway", {}).update(host="0.0.0.0", port=18790)
    ws = config.setdefault("channels", {}).setdefault("websocket", {})
    ws.update(enabled=True, host="0.0.0.0", port=8765)
    if not (ws.get("tokenIssueSecret") or ws.get("token")):
        ws["tokenIssueSecret"] = secrets.token_urlsafe(32)
    return config


class ReleaseManager:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root.expanduser().resolve()
        if self.root == REPO or REPO in self.root.parents:
            raise ValueError("deployment data must be outside the Git checkout")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.data = self.root / "shared/data"
        self.state_path = self.root / "state.json"
        self._docker_prefix: list[str] | None = None
        settings_path = self.root / "settings.json"
        if not settings_path.exists():
            suffix = hashlib.sha256(str(self.root).encode()).hexdigest()[:8]
            settings = {
                "project": "nanobot-bridge-tg" if self.root == DEFAULT_ROOT.resolve() else f"nanobot-bridge-tg-{suffix}",
                "web_port": int(os.environ.get("BRIDGE_WEB_PORT", "8765")),
                "health_port": int(os.environ.get("BRIDGE_HEALTH_PORT", "18790")),
            }
            if not all(1 <= settings[key] <= 65535 for key in ("web_port", "health_port")):
                raise ValueError("invalid host port")
            write_json(settings_path, settings)
        self.settings = json.loads(settings_path.read_text())

    @contextlib.contextmanager
    def lock(self):
        with (self.root / "deploy.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    @property
    def docker(self) -> list[str]:
        if self._docker_prefix is None:
            if shutil.which("docker") is None:
                raise RuntimeError("Docker is not installed")
            accessible = subprocess.run(
                ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
            self._docker_prefix = ["docker"] if accessible else ["sudo", "-n", "docker"]
            output([*self._docker_prefix, "info", "--format", "{{.Architecture}}"])
        return self._docker_prefix

    def state(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def release(self, revision: str) -> tuple[Path, dict]:
        if not REVISION.fullmatch(revision):
            raise ValueError("release must be a full 40-character Git commit")
        directory = self.root / "releases" / revision
        return directory, verify_release(directory)

    def _load_image(self, directory: Path, metadata: dict) -> None:
        expected_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
        if metadata["architecture"] != expected_arch:
            raise ValueError("release architecture does not match this server")
        found = subprocess.run(
            [*self.docker, "image", "inspect", metadata["image"], "--format", "{{.Id}}"],
            capture_output=True, text=True,
        )
        if found.returncode or found.stdout.strip() != metadata["image_id"]:
            run([*self.docker, "load", "--input", str(directory / "image.tar.gz")])
        identity = json.loads(output([*self.docker, "image", "inspect", metadata["image"]]))[0]
        if identity["Id"] != metadata["image_id"]:
            raise ValueError("loaded image identity does not match release")
        if identity["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != metadata["revision"]:
            raise ValueError("loaded image revision does not match release")

    def _compose(self, revision: str, *args: str) -> str:
        _, metadata = self.release(revision)
        env = dict(os.environ, BRIDGE_IMAGE=metadata["image"], BRIDGE_DATA_DIR=str(self.data),
                   BRIDGE_UID=str(os.getuid()), BRIDGE_GID=str(os.getgid()),
                   BRIDGE_WEB_PORT=str(self.settings["web_port"]),
                   BRIDGE_HEALTH_PORT=str(self.settings["health_port"]))
        return output([
            *self.docker, "compose", "--env-file", "/dev/null", "--project-name", self.settings["project"],
            "-f", str(REPO / "deploy/compose.local.yml"), *args,
        ], env=env)

    def _wait(self, revision: str) -> None:
        cid = self._compose(revision, "ps", "-q", "gateway")
        if not cid:
            raise RuntimeError("gateway container did not start")
        self._wait_container(cid)

    def _wait_container(self, cid: str) -> None:
        for _ in range(90):
            status = output([*self.docker, "inspect", cid, "--format", "{{.State.Status}}"])
            if status != "running":
                raise RuntimeError(f"gateway container is {status}")
            health = output([*self.docker, "inspect", cid, "--format", "{{.State.Health.Status}}"])
            if health == "healthy":
                run([*self.docker, "exec", cid, "python", "/opt/smoke_probe.py"])
                return
            time.sleep(1)
        raise RuntimeError("gateway health check timed out")

    def init(self) -> None:
        if self.data.exists():
            raise ValueError("data directory already exists; refusing to overwrite")
        self.data.mkdir(parents=True, mode=0o700)
        write_json(self.data / "config.json", initial_config())
        print("Initialized private data. Configure the model through the authenticated WebUI.")

    def build(self) -> str:
        if output(["git", "status", "--porcelain"], cwd=REPO):
            raise ValueError("commit changes before building an immutable release")
        revision = output(["git", "rev-parse", "HEAD"], cwd=REPO)
        target = self.root / "releases" / revision
        if target.exists():
            verify_release(target)
            self.smoke(revision)
            print(f"Existing verified release: {revision}")
            return revision
        target.parent.mkdir(parents=True, exist_ok=True)
        image = f"nanobot-bridge-tg:sha-{revision}"
        run([*self.docker, "build", "-f", str(REPO / "deploy/Dockerfile"),
             "--build-arg", f"SOURCE_REVISION={revision}", "-t", image, str(REPO)])
        with tempfile.TemporaryDirectory(prefix=".build-", dir=target.parent) as scratch:
            staging = Path(scratch)
            cid = output([*self.docker, "create", image])
            try:
                run([*self.docker, "cp", f"{cid}:/opt/artifacts/.", str(staging)])
            finally:
                run([*self.docker, "rm", cid], stdout=subprocess.DEVNULL)
            wheels = list(staging.glob("*.whl"))
            if len(wheels) != 1:
                raise ValueError("image must contain exactly one built wheel")
            with (staging / "image.tar.gz").open("wb") as archive:
                producer = subprocess.Popen([*self.docker, "save", image], stdout=subprocess.PIPE)
                assert producer.stdout is not None
                try:
                    with gzip.GzipFile(fileobj=archive, mode="wb", compresslevel=1, mtime=0) as compressed:
                        shutil.copyfileobj(producer.stdout, compressed)
                finally:
                    producer.stdout.close()
                if producer.wait() != 0:
                    raise RuntimeError("docker save failed")
            metadata = {
                "schema": 1, "revision": revision, "image": image,
                "image_id": output([*self.docker, "image", "inspect", image, "--format", "{{.Id}}"]),
                "architecture": output([*self.docker, "image", "inspect", image, "--format", "{{.Architecture}}"]),
                "wheel": wheels[0].name,
            }
            write_json(staging / "release.json", metadata)
            names = ["release.json", "image.tar.gz", wheels[0].name]
            (staging / "SHA256SUMS").write_text("".join(f"{digest(staging / n)}  {n}\n" for n in names))
            verify_release(staging)
            shutil.copytree(staging, target)
        self.smoke(revision)
        print(f"Built and smoke-tested release: {revision}")
        return revision

    def smoke(self, revision: str) -> None:
        directory, metadata = self.release(revision)
        self._load_image(directory, metadata)
        with tempfile.TemporaryDirectory(prefix="smoke-", dir=self.root) as scratch:
            data = Path(scratch)
            write_json(data / "config.json", initial_config())
            cid = output([*self.docker, "run", "-d", "--network", "none",
                          "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
                          "--security-opt=no-new-privileges:true", "-v", f"{data}:/data", metadata["image"]])
            try:
                self._wait_container(cid)
            finally:
                run([*self.docker, "rm", "-f", cid], stdout=subprocess.DEVNULL)

    def _backup(self, old_state: dict) -> Path:
        backup = self.root / "backups" / f"{time.time_ns()}"
        backup.mkdir(parents=True, mode=0o700)
        shutil.copytree(self.data, backup / "data", symlinks=True)
        write_json(backup / "state.json", old_state)
        return backup

    def _restore(self, backup: Path) -> None:
        # Preserve failed candidate data rather than deleting potentially useful evidence.
        if self.data.exists():
            self.data.rename(self.data.with_name(f"failed-data-{time.time_ns()}"))
        shutil.copytree(backup / "data", self.data, symlinks=True)

    def deploy(self, revision: str, previous: dict | None = None) -> None:
        self.smoke(revision)
        if not (self.data / "config.json").is_file():
            raise ValueError("initialize data or use migrate-systemd before deploying")
        old_state = self.state()
        old = old_state.get("current")
        if old == revision:
            self._wait(revision)
            print("Release already deployed and healthy")
            return
        if old:
            self._compose(old, "stop", "gateway")
        backup = None
        try:
            backup = self._backup(old_state)
            self._compose(revision, "up", "-d", "--no-deps", "--force-recreate", "gateway")
            self._wait(revision)
            predecessor = previous or ({"kind": "container", "revision": old, "backup": str(backup)} if old else None)
            write_json(self.state_path, {"current": revision, "previous": predecessor})
        except BaseException:
            self._compose(revision, "down")
            if backup:
                self._restore(backup)
            if old:
                self._compose(old, "up", "-d", "--no-deps", "gateway")
                self._wait(old)
            raise
        print(f"Deployed healthy release: {revision}")

    def migrate_systemd(self, revision: str, source: Path) -> None:
        self.smoke(revision)
        if self.state().get("current") or self.data.exists():
            raise ValueError("migration requires a new deployment data directory")
        source = source.expanduser().resolve()
        config = json.loads((source / "config.json").read_text())
        workspace = Path(config.get("agents", {}).get("defaults", {}).get("workspace", str(source / "workspace"))).expanduser().resolve()
        if workspace != source / "workspace":
            raise ValueError("migration expects the existing workspace under the source data directory")
        service = "nanobot-gateway.service"
        enabled = subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", service]).returncode == 0
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", service]).returncode == 0
        backup = self.root / "backups" / f"systemd-{time.time_ns()}"
        backup.mkdir(parents=True, mode=0o700)
        unit = Path.home() / ".config/systemd/user" / service
        shutil.copy2(unit, backup / service)
        run(["systemctl", "--user", "stop", service])
        try:
            run(["systemctl", "--user", "disable", service])
            shutil.copytree(source, backup / "original-data", symlinks=True)
            shutil.copytree(source, self.data, symlinks=True)
            write_json(self.data / "config.json", container_config(config))
            self.deploy(revision, previous={
                "kind": "systemd", "backup": str(backup), "enabled": enabled, "active": active,
            })
        except BaseException:
            if enabled:
                run(["systemctl", "--user", "enable", service])
            if active:
                run(["systemctl", "--user", "start", service])
            if self.data.exists():
                self.data.rename(self.data.with_name(f"failed-migration-{time.time_ns()}"))
            raise
        print("Original systemd service stopped and disabled; its data was not modified.")

    def rollback(self) -> None:
        state = self.state()
        previous = state.get("previous")
        if not state.get("current") or not previous:
            raise ValueError("no previous deployment is available")
        backup = Path(previous["backup"]).resolve()
        if self.root / "backups" not in backup.parents:
            raise ValueError("invalid backup path")
        self._compose(state["current"], "down")
        safety = self._backup(state)
        try:
            if previous["kind"] == "systemd":
                unit = Path.home() / ".config/systemd/user/nanobot-gateway.service"
                shutil.copy2(backup / "nanobot-gateway.service", unit)
                run(["systemctl", "--user", "daemon-reload"])
                if previous["enabled"]:
                    run(["systemctl", "--user", "enable", "nanobot-gateway.service"])
                if previous["active"]:
                    run(["systemctl", "--user", "start", "nanobot-gateway.service"])
                write_json(self.state_path, {"current": None, "previous": None})
            else:
                self._restore(backup)
                restored = json.loads((backup / "state.json").read_text())
                self._compose(previous["revision"], "up", "-d", "--no-deps", "gateway")
                self._wait(previous["revision"])
                write_json(self.state_path, restored)
        except BaseException:
            if previous["kind"] == "systemd":
                run(["systemctl", "--user", "stop", "nanobot-gateway.service"])
                run(["systemctl", "--user", "disable", "nanobot-gateway.service"])
            self._restore(safety)
            self._compose(state["current"], "up", "-d", "--no-deps", "gateway")
            self._wait(state["current"])
            raise
        print("Rollback completed; previous data and deployment state restored.")

    def export(self, revision: str, destination: Path) -> None:
        directory, metadata = self.release(revision)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination, "w") as archive:
            for name in ["release.json", "SHA256SUMS", "image.tar.gz", metadata["wheel"]]:
                archive.add(directory / name, arcname=name, recursive=False)
        print(f"Release transfer bundle: {destination}")

    def status(self) -> None:
        state = self.state()
        print(json.dumps(state, indent=2))
        if state.get("current"):
            print(self._compose(state["current"], "ps"))

    def logs(self) -> None:
        revision = self.state().get("current")
        if not revision:
            raise ValueError("no container deployment")
        text = self._compose(revision, "logs", "--no-color", "--tail", "100", "gateway")
        config = json.loads((self.data / "config.json").read_text())

        def redact(value):
            nonlocal text
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and len(item) > 5 and any(part in key.lower() for part in ("token", "secret", "apikey", "password")):
                        text = text.replace(item, "<redacted>")
                    else:
                        redact(item)
            elif isinstance(value, list):
                for item in value:
                    redact(item)

        redact(config)
        print(re.sub(r"(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}", "<telegram-token>", text))


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("BRIDGE_DEPLOY_ROOT", DEFAULT_ROOT)))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "build", "rollback", "status", "logs", "stop"):
        commands.add_parser(name)
    for name in ("smoke", "deploy", "migrate-systemd", "export"):
        command = commands.add_parser(name)
        command.add_argument("revision")
        if name == "migrate-systemd":
            command.add_argument("--source", type=Path, default=Path.home() / ".nanobot")
        if name == "export":
            command.add_argument("destination", type=Path)
    args = parser.parse_args()
    manager = ReleaseManager(args.root)
    with manager.lock():
        if args.command in ("smoke", "deploy"):
            getattr(manager, args.command)(args.revision)
        elif args.command == "migrate-systemd":
            manager.migrate_systemd(args.revision, args.source)
        elif args.command == "export":
            manager.export(args.revision, args.destination)
        elif args.command == "stop":
            revision = manager.state().get("current")
            if revision:
                print(manager._compose(revision, "stop", "gateway"))
        else:
            getattr(manager, args.command)()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        # Avoid printing subprocess payloads: build/deploy input may include credentials.
        raise SystemExit(f"Release operation failed: {type(error).__name__}: {error}") from None
