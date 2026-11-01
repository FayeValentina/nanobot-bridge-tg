"""Keep runtime configuration and credential stores outside ordinary file tools."""

from pathlib import Path

from nanobot.config.paths import get_config_path


def protected_runtime_paths() -> tuple[Path, ...]:
    config = get_config_path().expanduser().absolute()
    return tuple(path.resolve(strict=False) for path in (
        config,
        config.parent / "auth",
        config.parent / "telegram-social" / "oauth",
        Path.home() / ".local" / "share" / "oauth-cli-kit" / "auth",
        Path.home() / ".codex" / "auth.json",
    ))


def require_noncredential_path(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    for protected in protected_runtime_paths():
        if resolved == protected or resolved.is_relative_to(protected):
            raise PermissionError("Runtime credentials are not accessible through file tools")
    return resolved


def require_noncredential_root(path: Path) -> Path:
    """An allowed hierarchy must neither contain nor sit inside a credential root."""
    resolved = require_noncredential_path(path)
    if any(secret.is_relative_to(resolved) for secret in protected_runtime_paths()):
        raise PermissionError("Sandbox roots must not contain runtime credentials")
    return resolved
