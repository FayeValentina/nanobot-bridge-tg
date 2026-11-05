**1. 改动摘要（按文件）**

- `nanobot/providers/antigravity_provider.py`：新增 provider，共 174 行；实现模型映射、缓存、401 刷新、SSE 回调及 HTTP 异常。
- `nanobot/providers/registry.py`：登记 provider、三个内置模型及 backend 注释。
- `nanobot/providers/factory.py`：新增构建分支。
- `nanobot/config/schema.py`：新增排除序列化的 OAuth 配置字段。
- `nanobot/webui/settings_models.py`：接入本地登录状态。
- `nanobot/security/credentials.py`：保护整个凭据目录。
- `deploy/compose.local.yml`：新增 `/agy` 可读写挂载和环境变量。
- `scripts/bridge_release.py`：生成 `BRIDGE_AGY_DIR`。
- `tests/providers/test_antigravity_provider.py`：新增 23 项测试。

未修改已有测试。已有文件合计新增 35 行；工作区中的 `docs/SOCIAL_REFACTOR_PROGRESS.md` 改动非本次操作产生。

**2. 实际运行的命令及结果**

测试均设置：

```bash
ANTIGRAVITY_DIR=/tmp/agy-b-empty-credentials
PYTHONPATH=.agent
PYTEST_ADDOPTS='-p codex_pytest_env'
```

```bash
.venv/bin/python -m pytest tests/providers/test_antigravity_provider.py tests/providers/test_antigravity_auth.py tests/providers/test_gemini_wire.py tests/providers/test_xai_grok_provider.py tests/providers/test_providers_init.py tests/deploy tests/webui/test_settings_api.py tests/config -q -p no:cacheprovider
```

```text
10 failed, 295 passed in 8.83s
```

10 个失败全部位于 `tests/deploy/test_bridge_release.py`：

```text
test_release_rejects_missing_payload_before_loading
test_legacy_release_is_usable_without_its_old_manifest
test_failed_upgrade_restores_data_and_previous_running_version
test_explicit_rollback_restores_snapshot_and_release_state
test_same_release_is_health_checked_without_restarting
test_release_export_roundtrip
test_snapshot_failure_restarts_previous_release
test_failed_explicit_rollback_recovers_current_release
test_sudo_compose_receives_an_explicit_non_secret_env_file
test_snapshot_failure_during_rollback_restarts_current_release
```

均为 `zipfile.BadZipFile`。通过 `git archive HEAD` 建立 `/tmp/agy-b-baseline`，在该目录运行：

```bash
/home/ubuntu/nanobot-bridge-tg/.venv/bin/python -m pytest tests/deploy -q -p no:cacheprovider
```

复现相同失败：

```text
10 failed, 3 passed in 1.93s
```

```bash
.venv/bin/python -m pytest tests/providers/test_antigravity_provider.py -q -p no:cacheprovider
```

```text
23 passed in 1.41s
```

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob='*evaluate_*'
```

```text
944 passed, 13 warnings in 83.01s (0:01:23)
```

```bash
.venv/bin/ruff check nanobot/providers/antigravity_provider.py nanobot/providers/registry.py nanobot/providers/factory.py nanobot/config/schema.py nanobot/webui/settings_models.py nanobot/security/credentials.py scripts/bridge_release.py tests/providers/test_antigravity_provider.py
```

```text
All checks passed!
```

类型检查使用 `/tmp` 临时配置补充虚拟环境 `extraPaths`，保持 strict；测试检查配置另设 `exclude: []`。

```bash
.venv/bin/basedpyright --project /tmp/agy-b-pyright-tests.json nanobot/providers/antigravity_provider.py nanobot/providers/registry.py nanobot/providers/factory.py nanobot/config/schema.py nanobot/webui/settings_models.py nanobot/security/credentials.py tests/providers/test_antigravity_provider.py
```

```text
0 errors, 0 warnings, 0 notes
```

包含 `scripts/bridge_release.py` 的全改动检查，以及 HEAD 副本中该脚本的检查，均为：

```text
152 errors, 0 warnings, 0 notes
```

直接比较诊断并调整新增一行造成的行号偏移：

```text
baseline diagnostics: 152; current diagnostics: 152
direct comparison after inserted-line adjustment: True
```

`git diff --check`：退出码 0，无输出。

**3. 与简报的偏差及原因**

- 未达到指定测试全部通过、所有改动 Python 文件类型检查 0 errors：存在上述已复现的基线失败，未越界修复。
- 尝试对允许路径执行 `git stash push -u`，退出码 1、无输出；当前 `.git` 只读，改用 HEAD 临时副本对比。
- 沙箱未自动发现类型检查依赖，使用临时配置补齐路径，未修改项目配置或环境插件。
- 未提交、推送、部署，未读取真实凭据或发送真实网络请求。

**4. 未解决的问题 / 需要编排者决定的事项**

- 需编排者决定如何处理既有的 10 个部署测试失败及 `bridge_release.py` 的 152 个类型错误；本任务未新增对应诊断。