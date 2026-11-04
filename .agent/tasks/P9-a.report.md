## 1. 改动摘要（按文件）

- `social/hook.py`：恢复原文后，仅 iteration 0 构建并注入 live memory；恢复逻辑未改。
- `social/activity.py`：新增 `index()`、`current()`；`context()` 保持不变。
- `social/context.py`：普通 turn 注入四字段活动索引；activity turn 另含完整 `current_activity`，无 active 时为 `None`。
- `agent/tools/base.py`：新增 `_strip_schema_titles()`，仅清理展示副本，保留真实 `properties.title` 和本地验证 schema。
- `test_social_runtime.py`：新增三请求 turn 的单块注入、持久化零块、全部注册工具 schema 检查。
- `test_social_activity.py`：覆盖普通/activity turn、完整 active 记录及无 active 情况。
- `test_tool_validation.py`：覆盖四类名称映射、嵌套列表、深拷贝及非字符串 title。
- 调整旧断言，逐条如下：
  - `test_social_memory.py`：record 后检查工具结果；初始块保持原状态，下一次唤醒才更新。
  - 同文件：disable 后检查工具结果和每次请求只有一块，不再要求重新注入。
  - `test_social_followups.py`：iteration 1 恢复原文且不重新注入。

生产代码新增 33 行；`test_social_scenarios.py` 未修改。

## 2. 实际运行的命令及结果（原样贴最后几行）

pytest 均继承 `PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`。指定目录全部存在。

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
963 passed, 13 warnings in 126.25s (0:02:06)
```

包含未修改的场景测试，全部通过。

```text
$ .venv/bin/python -m pytest tests/agent tests/tools tests/providers tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider
........................................................................ [ 41%]
.................................................................
```

在 `test_apply_patch_edits_add_new_file` 长时间停滞后中断，退出码 130，无最终汇总。[完整日志](/tmp/p9/work/p9a/core.log)。

随后分组取得结果：

```text
$ .venv/bin/python -m pytest tests/agent -q -p no:cacheprovider
ERROR tests/agent/test_auto_compact.py::TestIdleScanThrottling::test_configured_idle_scan_interval_throttles_checks
ERROR tests/agent/test_mcp_reconnect_crash.py::test_mcp_reconnect_after_session_timeout
ERROR tests/agent/test_mcp_reconnect_crash.py::test_mcp_reconnect_during_shutdown_does_not_crash
38 failed, 1608 passed, 1 warning, 3 errors in 68.32s (0:01:08)
```

[失败详情](/tmp/p9/work/p9a/agent.log)。

```text
$ .venv/bin/python -m pytest tests/providers tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider
FAILED tests/providers/test_xai_oauth.py::test_callback_server_accepts_only_matching_state_and_allows_accounts_origin
FAILED tests/providers/test_xai_oauth.py::test_login_uses_random_loopback_callback_and_saves_separate_credentials
FAILED tests/providers/test_xai_oauth.py::test_pending_login_accepts_authorization_code_from_remote_browser
3 failed, 1202 passed in 40.77s
```

三项均为创建 socket 时 `PermissionError: [Errno 1] Operation not permitted`。[日志](/tmp/p9/work/p9a/providers-cli-config.log)。

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_runtime.py nanobot/channels/telegram/tests/test_social_activity.py nanobot/channels/telegram/tests/test_social_memory.py nanobot/channels/telegram/tests/test_social_followups.py tests/tools/test_tool_validation.py -q -p no:cacheprovider
FAILED tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls[curl -s -o /dev/null -w "%{http_code}" https://www.google.com]
1 failed, 238 passed in 26.60s
```

唯一失败对应 DNS 解析失败。[日志](/tmp/p9/work/p9a/targeted.log)。首轮曾有新增测试读取错误返回键的失败，已修正并复跑。

```text
$ .venv/bin/ruff check nanobot/
All checks passed!

$ .venv/bin/basedpyright -p /tmp/p8-a-pyright.json nanobot/channels/telegram/social nanobot/agent/tools/base.py
0 errors, 0 warnings, 0 notes

$ .venv/bin/ruff check tests/tools/test_tool_validation.py
All checks passed!

$ git diff --check
```

最后一项无输出，退出码 0。

停滞诊断命令：

```text
$ timeout -s INT 20s .venv/bin/python -m pytest tests/tools/test_apply_patch_tool.py::test_apply_patch_edits_add_new_file -q -p no:cacheprovider -o faulthandler_timeout=10
Timeout (0:00:10)!
```

栈显示主线程停在 `asyncio.run()` 的 selector，工作线程空闲；随后人工中断。[诊断日志](/tmp/p9/work/p9a/stall.log)。

## 3. 回放对比表

复制并修改 [replay.py](/tmp/p9/work/p9a/replay.py)，移除原有行为 monkeypatch，直接调用新代码：

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -u /tmp/p9/work/p9a/replay.py
request live_blocks cl100k_estimate
1 1 34344
2 1 36691
3 1 37119
4 1 38150
```

单元格为“块份数 / cl100k 输入估算”：

| 第二次唤醒 | 请求 1 | 请求 2 | 请求 3 | 请求 4 |
|---|---:|---:|---:|---:|
| P9-0 原代码，有 H | 1 / 39,638 | 2 / 49,153 | 3 / 56,748 | 4 / 65,206 |
| P9-0 仅一次注入，有 H | 1 / 39,638 | 1 / 41,985 | 1 / 42,416 | 1 / 43,441 |
| P9-a T1＋T2＋T4 | 1 / 34,344 | 1 / 36,691 | 1 / 37,119 | 1 / 38,150 |

合计从 **210,745** 降至 **146,304**，减少 **64,441，约 30.6%**。工具 schema 单请求减少 399 cl100k token。回放 `error=None`，结束后原始 transcript 的 14 条消息均为零块。

以上为 cl100k 估算，存在随机 ID 的微小浮动，并非服务端计费数据。[回放日志](/tmp/p9/work/p9a/replay.log)。

## 4. 与简报的偏差及原因

实现无设计偏差；没有修改禁改路径、提交、推送或部署。

验收未全部达标：第二组完整检查因停滞被中断，分组检查仍有失败。未为环境问题修改工程、conftest、测试或沙箱插件。

确认接受既定限制：turn 中途 compaction 会将该块送入摘要输入，之后的迭代不再携带或补发该块；本次未增加处理代码。

## 5. 未解决的问题 / 需要编排者决定的事项

- 需在具备正常 DNS、socket 和线程唤醒条件的环境复跑第二组完整检查。
- Agent 失败还涉及 `/tmp/.git` 导致 GitStore 跳过初始化、旧提示文本断言、测试替身签名不匹配及计时测试问题；详情已保留，未扩大本次修改范围。
- 本次相关测试、场景测试、ruff、指定范围类型检查均已通过；不能据此声称全部完成标准已通过。