# Deployment

This fork runs on the local server using the [local release pipeline](./LOCAL_PIPELINE.md).
The `deploy/` directory contains its Dockerfile and Compose configuration. Deployment keeps
WebUI, chat channels, Dream, cron jobs, and sessions available. The Linux and macOS service
sections below remain references for separate installations.

## Before You Deploy

Check these before deploying a new instance:

| Check | Why it matters |
|---|---|
| `nanobot status` shows the expected config and workspace | Confirms the process will read the instance you meant to run |
| `nanobot agent -m "Hello!"` works | Proves install, config, provider, model, and workspace writes before adding a service layer |
| Secrets are in environment variables or protected config files | API keys, bot tokens, OAuth state, and chat credentials should not be world-readable |
| The active config directory (including `sessions/`) and workspace are persistent | Sessions follow `--config`; memory, generated artifacts, and the workspace identity marker follow the workspace |
| Channel access control is intentional | Use `allowFrom`, pairing, WebSocket `token`/`tokenIssueSecret`, or private test channels before exposing the bot |
| Ports are planned | Gateway health defaults to local-only `127.0.0.1:18790`; WebUI/WebSocket defaults to `8765`; `nanobot serve` defaults to `8900` |
| Logs are easy to reach | Use `docker compose logs`, `journalctl`, LaunchAgent log files, or `nanobot gateway --verbose` while diagnosing startup |

Restart the deployed process after editing `config.json`. Long-running processes read config at startup.

## Choose a Runtime

| Runtime | Use it for | State location | Useful first command |
|---|---|---|---|
| Local release pipeline | This server: tested builds, data backups, deployment, and rollback | `~/.local/share/nanobot-bridge-tg-deploy/shared/data` mounted at `/data` | `./scripts/deploy-local.sh status` |
| systemd user service | Linux user-level gateway that restarts automatically | Host user's `~/.nanobot` unless you pass explicit paths | `systemctl --user status nanobot-gateway` |
| macOS LaunchAgent | macOS gateway that starts after login | Host user's `~/.nanobot` unless the plist passes explicit paths | `launchctl list | grep ai.nanobot.gateway` |

## Docker

This fork uses `deploy/Dockerfile`, `deploy/compose.local.yml`, and the release manager.
After committing changes, run from the repository root:

```bash
./scripts/pipeline.sh release
./scripts/deploy-local.sh status
./scripts/deploy-local.sh logs
```

The pipeline builds the WebUI and Python wheel, installs the wheel into the image,
verifies the image, backs up instance data, and deploys the new version. Failed upgrades
restore the preceding deployment. Use `./scripts/deploy-local.sh rollback` to restore
the previous version and its data snapshot.

The container runs as the host UID/GID (currently `1001:1001`), drops all Linux capabilities,
and uses `no-new-privileges`. Data is mounted at `/data`; the config is `/data/config.json`.
Host ports are published on loopback only: `127.0.0.1:8765` for WebUI/WebSocket and
`127.0.0.1:18790` for health. Container listeners bind to `0.0.0.0` for port forwarding;
WebUI bootstrap remains authenticated. Telegram dependencies are preinstalled.

The image does not include bubblewrap or an override for nested sandbox namespaces.
See the [local pipeline guide](./LOCAL_PIPELINE.md) for private data paths, first-instance
setup, production migration, image exports, and recovery.

### Cloudflare Tunnel + Cloudflare Access

For a local `cloudflared` process in front of nanobot, Cloudflare Access can
authenticate the user before forwarding the request and add
`Cf-Access-Jwt-Assertion`. Opt in to trusted-proxy no-token mode only when the
direct TCP peer is the tunnel process and the assertion is non-empty:

```json
{
  "gateway": { "host": "127.0.0.1" },
  "channels": {
    "websocket": {
      "host": "127.0.0.1",
      "port": 8765,
      "publicWsUrl": "wss://nanobot.example.com/",
      "trustedProxyAuth": {
        "trustedPeerCidrs": ["127.0.0.1/32", "::1/128"],
        "assertionHeader": "Cf-Access-Jwt-Assertion"
      }
    }
  }
}
```

This is two-part authorization: a trusted direct loopback peer **and** a
non-empty Cloudflare Access assertion. A trusted CIDR alone is not a bypass.
For this flow `/webui/bootstrap` returns connection metadata without a
bootstrap token or REST API token; the proxy assertion authorizes the WebSocket
handshake and REST requests directly.

Set `publicWsUrl` to the browser-facing `wss://` endpoint when the tunnel sends
the origin host header (such as `127.0.0.1:8765`); otherwise the WebUI could
attempt to open its WebSocket directly against the loopback address.
The assertion header must be generated
by Cloudflare Access after authentication; routing/client metadata headers such
as `Host`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, and `CF-Connecting-IP`
are rejected as `assertionHeader` values. Nanobot trusts the assertion but does
not cryptographically validate the JWT, so configure the tunnel and Access
policy carefully and do not expose the nanobot listener directly to untrusted
clients. Forwarded client headers do not establish proxy trust.

## Linux Service

Run the gateway as a systemd user service so it starts automatically and restarts on failure.

Preview the generated unit first:

```bash
nanobot gateway install-service --manager systemd --dry-run
```

Install, enable, and start it:

```bash
nanobot gateway install-service --manager systemd
```

For a custom instance, pass the same config/workspace selector you use to run the gateway:

```bash
nanobot gateway install-service \
  --manager systemd \
  --name nanobot-telegram \
  --config ~/.nanobot-telegram/config.json \
  --workspace ~/.nanobot-telegram/workspace
```

Common operations:

```bash
systemctl --user status nanobot-gateway        # check status
systemctl --user restart nanobot-gateway       # restart after config changes
journalctl --user -u nanobot-gateway -f        # follow logs
nanobot gateway uninstall-service --manager systemd
```

The installer writes `~/.config/systemd/user/nanobot-gateway.service`, runs
`systemctl --user daemon-reload`, enables the unit, and restarts it. It uses the
current Python executable with `python -m nanobot gateway --foreground`, so the
service runs in the same environment you used to install nanobot.

> **Note:** User services only run while you are logged in. To keep the gateway running after logout, enable lingering:
>
> ```bash
> loginctl enable-linger $USER
> ```

## macOS LaunchAgent

Use a LaunchAgent when you want `nanobot gateway` to stay online after you log in, without keeping a terminal open.

Preview the generated plist first:

```bash
nanobot gateway install-service --manager launchd --dry-run
```

Install, load, enable, and start it:

```bash
nanobot gateway install-service --manager launchd
```

For a custom instance:

```bash
nanobot gateway install-service \
  --manager launchd \
  --name nanobot-telegram \
  --config ~/.nanobot-telegram/config.json \
  --workspace ~/.nanobot-telegram/workspace
```

Common operations:

```bash
launchctl list | grep ai.nanobot.gateway
launchctl kickstart -k gui/$(id -u)/ai.nanobot.gateway
nanobot gateway uninstall-service --manager launchd
```

The installer writes `~/Library/LaunchAgents/ai.nanobot.gateway.plist`, uses the
current Python executable with `python -m nanobot gateway --foreground`, and
writes LaunchAgent logs under `~/.nanobot/logs/`.

> **Note:** if startup fails with "address already in use", stop the manually started `nanobot gateway` process first.
