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
    .venv/bin/python -m ruff check scripts/bridge_release.py deploy/smoke_probe.py tests/deploy \
      nanobot/agent/autocompact.py nanobot/agent/context.py nanobot/agent/hook.py \
      nanobot/agent/loop.py nanobot/agent/memory.py nanobot/agent/runner.py \
      nanobot/agent/tools/base.py nanobot/agent/tools/context.py nanobot/agent/tools/telegram_social.py \
      nanobot/agent/turn_delivery.py nanobot/session/manager.py nanobot/config/schema.py \
      nanobot/channels/manager.py nanobot/channels/telegram/runtime.py \
      nanobot/channels/telegram/manifest.py nanobot/channels/telegram/social \
      nanobot/channels/telegram/tests/test_config_contract.py \
      nanobot/channels/telegram/tests/test_delivery_contract.py \
      nanobot/channels/telegram/tests/test_social_runtime.py \
      nanobot/channels/telegram/tests/test_social_scheduler.py \
      nanobot/channels/telegram/tests/test_stream_pacing.py \
      nanobot/cli/gateway_runtime.py nanobot/webui/settings_system.py \
      tests/agent/test_telegram_social_contracts.py tests/agent/test_dream.py \
      tests/agent/test_task_cancel.py tests/channels/test_channel_setup.py
    .venv/bin/python -m pytest -q tests/deploy nanobot/channels/telegram/tests \
      tests/agent/test_runtime_context.py tests/agent/test_loop_session_policy.py \
      tests/agent/test_telegram_social_contracts.py tests/agent/test_context_builder.py \
      tests/agent/test_consolidator.py tests/agent/test_turn_delivery.py \
      tests/agent/test_loop_tool_context.py tests/agent/test_turn_hooks.py \
      tests/agent/test_dream.py tests/agent/test_task_cancel.py \
      tests/webui/test_settings_system.py tests/triggers/test_local_triggers.py
    npm --prefix webui run lint
    npm --prefix webui run test
    npm --prefix webui run build
    ;;
  release)
    revision="$(git rev-parse HEAD)"
    if [[ -n "$(git status --porcelain)" ]]; then
      echo 'Commit changes before running the release pipeline.' >&2
      exit 1
    fi
    "$repo_dir/scripts/pipeline.sh" test
    if [[ "$(git rev-parse HEAD)" != "$revision" ]]; then
      echo 'HEAD changed during tests; run the pipeline again.' >&2
      exit 1
    fi
    "$repo_dir/scripts/deploy-local.sh" build
    exec "$repo_dir/scripts/deploy-local.sh" deploy "$revision"
    ;;
  build|smoke|deploy|migrate-systemd|rollback|status|logs|stop|export)
    exec "$repo_dir/scripts/deploy-local.sh" "$@"
    ;;
  *)
    echo 'Usage: scripts/pipeline.sh {setup|test|release|build|smoke|deploy|migrate-systemd|rollback|status|logs|stop|export}'
    ;;
esac
