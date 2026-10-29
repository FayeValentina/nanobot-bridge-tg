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
      nanobot/agent/tools/message.py nanobot/bus/events.py nanobot/bus/queue.py \
      tests/tools/test_message_tool.py tests/test_tool_contextvars.py \
      nanobot/agent/tools/execution.py \
      nanobot/agent/turn_delivery.py nanobot/session/manager.py nanobot/config/schema.py \
      nanobot/channels/manager.py nanobot/channels/telegram/runtime.py \
      nanobot/channels/telegram/manifest.py nanobot/channels/telegram/social \
      nanobot/channels/telegram/tests/test_config_contract.py \
      nanobot/channels/telegram/tests/test_delivery_contract.py \
      nanobot/channels/telegram/tests/test_social_runtime.py \
      nanobot/channels/telegram/tests/test_social_scheduler.py \
      nanobot/channels/telegram/tests/test_social_media.py \
      nanobot/channels/telegram/tests/test_social_memory.py \
      nanobot/channels/telegram/tests/test_social_proactive.py \
      nanobot/channels/telegram/tests/test_shared_tools.py \
      nanobot/channels/telegram/tests/test_reply_reliability.py \
      nanobot/channels/telegram/tests/test_social_dialogue.py \
      nanobot/channels/telegram/tests/test_social_wake_v2.py \
      nanobot/channels/telegram/tests/test_social_judge.py \
      nanobot/channels/telegram/tests/test_social_judge_runtime.py \
      nanobot/channels/telegram/tests/conftest.py \
      nanobot/channels/telegram/tests/test_stream_pacing.py \
      nanobot/cli/gateway_runtime.py nanobot/webui/settings_system.py \
      nanobot/providers/openai_compat_provider.py tests/providers/test_litellm_kwargs.py \
      nanobot/providers/openai_codex_provider.py tests/providers/test_openai_codex_provider.py \
      tests/agent/test_telegram_social_contracts.py tests/agent/test_dream.py \
      tests/agent/test_task_cancel.py tests/channels/test_channel_setup.py
    .venv/bin/python -m pytest -q tests/deploy nanobot/channels/telegram/tests \
      tests/agent/test_runtime_context.py tests/agent/test_loop_session_policy.py \
      tests/agent/test_telegram_social_contracts.py tests/agent/test_context_builder.py \
      tests/agent/test_consolidator.py tests/agent/test_turn_delivery.py \
      tests/agent/test_loop_tool_context.py tests/agent/test_turn_hooks.py \
      tests/agent/test_dream.py tests/agent/test_task_cancel.py tests/tools/test_message_tool.py tests/test_tool_contextvars.py \
      tests/agent/test_autocompact_unit.py tests/agent/test_runner_tool_execution.py \
      tests/agent/test_runner_goal_continue.py tests/agent/test_loop_runner_integration.py \
      tests/cli/test_gateway_runtime.py \
      tests/webui/test_settings_system.py tests/triggers/test_local_triggers.py
    .venv/bin/python -m pytest -q tests/providers/test_litellm_kwargs.py \
      tests/providers/test_openai_codex_provider.py tests/providers/test_openai_codex_oauth.py
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
