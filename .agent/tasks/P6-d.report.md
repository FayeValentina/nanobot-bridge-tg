1. 改动摘要（按文件）

- `nanobot/channels/telegram/social/wiring.py`：新增 69 行，集中配置解析、OAuth 默认目录、runtime/provider/工具装配及连接、刷新、关闭生命周期。
- `nanobot/cli/gateway_runtime.py`：净减少 30 行，通过 wiring 接入 social；保留 `_active_tasks` 私有访问及原有 pyright 注释。
- `tests/cli/test_gateway_runtime.py`：新增指定的 4 条测试，覆盖两种关闭条件、全部 12 个工具及 OAuth 默认目录。未修改既有测试替身，无机械适配项。

2. 实际运行的命令及结果（原样贴最后几行）

以下 pytest 均继承已有环境 `PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`。

改动前：

```text
$ .venv/bin/python -m pytest tests/cli/test_commands.py -q -p no:cacheprovider
```

首次运行复现原有 9 个失败；保存日志的改动前复跑额外出现 Dream 提交测试失败，完整失败名单为：

```text
FAILED tests/cli/test_commands.py::test_commit_dream_changes_commits_real_edits
FAILED tests/cli/test_commands.py::test_heartbeat_empty_response_is_not_evaluated
FAILED tests/cli/test_commands.py::test_gateway_unbound_agent_cron_is_skipped
FAILED tests/cli/test_commands.py::test_gateway_bound_cron_runs_as_session_turn
FAILED tests/cli/test_commands.py::test_gateway_local_trigger_queue_submits_agent_turns[None]
FAILED tests/cli/test_commands.py::test_gateway_local_trigger_queue_submits_agent_turns[No API key configured]
FAILED tests/cli/test_commands.py::test_gateway_health_endpoint_binds_and_serves_expected_responses[127.0.0.1-http://127.0.0.1:18791/health-False]
FAILED tests/cli/test_commands.py::test_gateway_health_endpoint_binds_and_serves_expected_responses[0.0.0.0-http://127.0.0.1:18791/health-True]
FAILED tests/cli/test_commands.py::test_gateway_agent_task_owns_initial_mcp_provider_close
FAILED tests/cli/test_commands.py::test_gateway_shutdown_event_exits_forever_runtime_tasks
10 failed, 147 passed in 4.80s
```

改动后，原有 9 个失败全部消失：

```text
$ .venv/bin/python -m pytest tests/cli/test_commands.py -q -p no:cacheprovider
FAILED tests/cli/test_commands.py::test_commit_dream_changes_commits_real_edits
1 failed, 156 passed in 3.74s
```

```text
$ .venv/bin/python -m pytest tests/cli/test_gateway_runtime.py -q -p no:cacheprovider
14 passed in 1.02s
```

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
958 passed, 13 warnings in 152.48s (0:02:32)
```

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
22 passed in 14.13s
```

```text
$ .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway tests/cli -q -p no:cacheprovider
FAILED tests/cli/test_commands.py::test_commit_dream_changes_commits_real_edits
FAILED tests/cli/test_desktop_target.py::test_unix_request_revalidates_instance_identity
FAILED tests/cli/test_desktop_target.py::test_socket_rejects_truncated_or_oversized_reply[{}]
FAILED tests/cli/test_desktop_target.py::test_socket_rejects_truncated_or_oversized_reply[xxxxxxxxxxxxxxxxx]
FAILED tests/cli/test_desktop_tui.py::test_resolver_output_accepts_only_anonymous_ipc
5 failed, 589 passed, 4 skipped in 16.38s
```

```text
$ .venv/bin/ruff check nanobot/ tests/cli
All checks passed!
```

默认类型检查命令，改动前后分别为：

```text
$ .venv/bin/basedpyright nanobot/channels/telegram/social nanobot/cli/gateway_runtime.py
1131 errors, 0 warnings, 0 notes
```

```text
$ .venv/bin/basedpyright nanobot/channels/telegram/social nanobot/cli/gateway_runtime.py
1124 errors, 0 warnings, 0 notes
```

默认环境未解析 `.venv` 依赖。使用 `/tmp/p6d-pyright.json` 保持 strict、Python 3.11，并显式指定项目 `.venv`；原 gateway 与改动后 gateway 运行同一命令均为：

```text
$ .venv/bin/basedpyright -p /tmp/p6d-pyright.json nanobot/channels/telegram/social nanobot/cli/gateway_runtime.py
0 errors, 0 warnings, 0 notes
```

```text
$ grep -n "TelegramSocialConfig\|make_judge_provider\|SOCIAL_TOOL\|TelegramSocialRuntime" nanobot/cli/gateway_runtime.py
```

无输出，退出码 1，即 0 命中。`git diff --check` 亦无输出、退出码 0。

3. 与简报的偏差及原因

- 实现无设计偏差；仅修改允许的三个文件，未提交、推送或部署。
- 组合测试未全部通过：Dream 测试因 `/tmp/.git` 存在而跳过临时仓库初始化；另外 4 个测试在 Unix socket 操作处报 `PermissionError: [Errno 1] Operation not permitted`。未修改代码、测试或沙箱插件绕过这些环境问题。
- 默认类型检查未达到零错误；显式配置虚拟环境后，前后均为零错误。

4. 未解决的问题 / 需要编排者决定的事项

- 需编排者在允许临时 Git 初始化及 Unix socket 操作的环境复跑上述 5 个失败，当前不能声称全部验收通过。
- 启动时 social 关闭，之后通过 WebUI 开启需要重启，符合简报指定行为。
- 文件 watcher 不刷新 social、channel 构造时覆盖 social 配置、热刷新 judge 配置两次，均保持原状。