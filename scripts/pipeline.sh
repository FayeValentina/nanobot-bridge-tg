#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
case "${1:-help}" in
  setup)
    if [[ ! -x .venv/bin/python ]]; then
      uv venv --python 3.11 .venv
    fi
    uv pip install --python .venv/bin/python -e '.[dev]'
    .venv/bin/python -m ensurepip --upgrade
    .venv/bin/python -m scripts.install_channel_dependencies telegram
    npm --prefix webui ci
    ;;
  test)
    .venv/bin/python -m ruff check scripts/bridge_release.py scripts/bridge_receive.py deploy/smoke_probe.py tests/deploy
    .venv/bin/python -m pytest -q tests/deploy nanobot/channels/telegram/tests tests/agent/test_runtime_context.py tests/agent/test_loop_session_policy.py tests/triggers/test_local_triggers.py
    npm --prefix webui run lint
    npm --prefix webui run test
    npm --prefix webui run build
    ;;
  build|smoke|deploy|migrate-systemd|rollback|status|logs|stop|export)
    exec "$repo_dir/scripts/deploy-local.sh" "$@"
    ;;
  *)
    echo 'Usage: scripts/pipeline.sh {setup|test|build|smoke|deploy|migrate-systemd|rollback|status|logs|stop|export}'
    ;;
esac
