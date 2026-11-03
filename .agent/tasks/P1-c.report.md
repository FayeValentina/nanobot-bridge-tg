1. **改动摘要（验收尚未全部通过）**

按设计完成清理。生产代码净减少 **108 行**，测试及评估代码净减少 **19 行**。未提交、推送或部署。

| 文件 | 改动 | 净行数 |
|---|---|---:|
| `nanobot/session/manager.py` | 删除隔离字段、常量及标记分支，保留三个社交扩展点 | -19 |
| `nanobot/agent/loop.py` | 恢复工具过滤、命令、上下文和输出流程 | -23 |
| `nanobot/agent/context.py` | 删除 profile/skills 参数链，恢复默认行为 | -18 |
| `nanobot/agent/memory.py` | 删除策略代理，恢复上游归档写法；与指定上游完全一致 | -37 |
| `nanobot/agent/autocompact.py` | 仅保留 context_version 条件 | 0 |
| `nanobot/agent/tools/context.py` | 删除 RequestContext 隔离字段 | -1 |
| `nanobot/agent/tools/base.py` | 删除显式白名单属性及注释 | -3 |
| `nanobot/channels/telegram/social/runtime.py` | 删除隔离标记处理 | -7 |
| `nanobot/channels/telegram/tests/evaluate_social_v4.py` | 删除废弃参数 | 0 |
| `nanobot/channels/telegram/tests/test_harness_prompts.py` | 删除废弃参数 | 0 |
| `nanobot/channels/telegram/tests/test_shared_tools.py` | 删除隔离测试及废弃字段断言 | -15 |
| `nanobot/channels/telegram/tests/test_social_followups.py` | 删除废弃参数 | 0 |
| `nanobot/channels/telegram/tests/test_social_memory.py` | 删除废弃属性断言 | -1 |
| `nanobot/channels/telegram/tests/test_social_runtime.py` | 删除废弃参数和字段断言 | -3 |

`git diff --stat`：

```text
 nanobot/agent/autocompact.py                       |  2 +-
 nanobot/agent/context.py                           | 30 +++-----------
 nanobot/agent/loop.py                              | 33 +++------------
 nanobot/agent/memory.py                            | 47 +++-------------------
 nanobot/agent/tools/base.py                        |  3 --
 nanobot/agent/tools/context.py                     |  1 -
 nanobot/channels/telegram/social/runtime.py        | 11 +----
 .../channels/telegram/tests/evaluate_social_v4.py  |  4 +-
 .../telegram/tests/test_harness_prompts.py         |  4 +-
 .../channels/telegram/tests/test_shared_tools.py   | 17 +-------
 .../telegram/tests/test_social_followups.py        |  2 +-
 .../channels/telegram/tests/test_social_memory.py  |  1 -
 .../channels/telegram/tests/test_social_runtime.py | 11 ++---
 nanobot/session/manager.py                         | 19 ---------
 14 files changed, 29 insertions(+), 156 deletions(-)
```

以下命令的改动前后结果：

```bash
git diff 1bb712d3 --stat -- nanobot/session/manager.py nanobot/agent/loop.py nanobot/agent/context.py nanobot/agent/memory.py nanobot/agent/autocompact.py
```

改动前：

```text
 nanobot/agent/autocompact.py |   4 +
 nanobot/agent/context.py     |  40 +++-
 nanobot/agent/loop.py        | 441 +++++++++++++++++--------------------------
 nanobot/agent/memory.py      |  47 ++++-
 nanobot/session/manager.py   | 195 +++++++++++--------
 5 files changed, 373 insertions(+), 354 deletions(-)
```

改动后：

```text
 nanobot/agent/autocompact.py |   4 +
 nanobot/agent/context.py     |  10 ++
 nanobot/agent/loop.py        | 408 +++++++++++++++----------------------------
 nanobot/session/manager.py   | 176 +++++++++++--------
 4 files changed, 260 insertions(+), 338 deletions(-)
```

上游差异总行数从 727 减至 598，减少 **129 行**。

2. **实际运行的命令及结果**

核心完整检查：

```bash
.venv/bin/python -m pytest tests/agent tests/session tests/tools tests/cli/test_gateway_runtime.py tests/test_tool_contextvars.py -q -p no:cacheprovider
```

运行中已有失败、错误，约 65% 处连续两分多钟无进展，手动中止，退出码 130。末尾原文：

```text
.................................................F......FFFF............ [ 63%]
.................................
```

追加相同范围、限制前两个失败的诊断运行：

```bash
.venv/bin/python -m pytest tests/agent tests/session tests/tools tests/cli/test_gateway_runtime.py tests/test_tool_contextvars.py -q -p no:cacheprovider --maxfail=2
```

```text
FAILED tests/agent/test_auto_compact.py::TestProactiveAutoCompact::test_idle_tick_survives_session_removed_during_listing
ERROR tests/agent/test_auto_compact.py::TestIdleScanThrottling::test_configured_idle_scan_interval_throttles_checks
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 2 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 55 passed, 1 warning, 1 error in 3.69s
```

在 `/tmp/p1c-baseline` 解包改动前 HEAD（含根目录 conftest），复跑上述两个测试：

```bash
PYTHONPATH=/tmp/p1c-baseline/.agent /home/ubuntu/nanobot-bridge-tg/.venv/bin/python -m pytest tests/agent/test_auto_compact.py::TestIdleScanThrottling::test_configured_idle_scan_interval_throttles_checks tests/agent/test_auto_compact.py::TestProactiveAutoCompact::test_idle_tick_survives_session_removed_during_listing -q -p no:cacheprovider
```

```text
FAILED tests/agent/test_auto_compact.py::TestProactiveAutoCompact::test_idle_tick_survives_session_removed_during_listing
ERROR tests/agent/test_auto_compact.py::TestIdleScanThrottling::test_configured_idle_scan_interval_throttles_checks
1 failed, 1 passed, 1 warning, 1 error in 0.60s
```

关闭沙箱插件后，时钟 mock 定点测试：

```bash
PYTEST_ADDOPTS= .venv/bin/python -m pytest tests/agent/test_auto_compact.py::TestIdleScanThrottling::test_configured_idle_scan_interval_throttles_checks -q -p no:cacheprovider
```

```text
.                                                                        [100%]
1 passed in 0.45s
```

Telegram 完整检查：

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
```

```text
FAILED nanobot/channels/telegram/tests/test_social_runtime.py::test_group_compaction_uses_ordinary_memory_archiving[summary]
FAILED nanobot/channels/telegram/tests/test_social_runtime.py::test_group_compaction_uses_ordinary_memory_archiving[failure]
FAILED nanobot/channels/telegram/tests/test_social_runtime.py::test_group_compaction_uses_ordinary_memory_archiving[idle]
3 failed, 924 passed, 1 xfailed, 13 warnings in 149.71s (0:02:29)
```

归档定点检查：

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_runtime.py::test_group_compaction_uses_ordinary_memory_archiving -q -p no:cacheprovider
```

```text
3 failed, 1 passed in 3.09s
```

黑盒场景检查：

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
```

```text
...............x....                                                     [100%]
19 passed, 1 xfailed in 10.23s
```

Ruff：

```bash
.venv/bin/ruff check nanobot/ tests/
```

另用 `--output-format concise` 复核：

```text
nanobot/agent/loop.py:5:1: I001 [*] Import block is un-sorted or un-formatted
nanobot/agent/tools/filesystem.py:5:1: I001 [*] Import block is un-sorted or un-formatted
nanobot/session/manager.py:10:8: F401 [*] `shutil` imported but unused
Found 3 errors.
[*] 3 fixable with `--fix`.
```

三个错误均用 `git show HEAD:<path>` 的原始内容确认改动前已存在。

类型检查在改动前、改动后均运行：

```bash
.venv/bin/basedpyright nanobot/agent/loop.py nanobot/agent/context.py nanobot/agent/memory.py nanobot/session/manager.py nanobot/agent/autocompact.py nanobot/agent/tools/context.py nanobot/agent/tools/base.py nanobot/channels/telegram/social/runtime.py
```

两次末行均为：

```text
245 errors, 0 warnings, 0 notes
```

忽略行号变化，按文件、诊断文本和数量比较：

```text
Added diagnostics: 0
Removed diagnostics: 0
```

残留搜索：

```bash
grep -rnE "allowed_tools|include_profile|include_skills|archive_memory|allow_commands|requires_explicit_allowlist|ISOLATED_|allow_private_context|prompt_policy" nanobot tests --include=*.py
```

无输出，退出码 1。

```bash
grep -rn "policy.enabled" nanobot --include=*.py
```

```text
nanobot/channels/telegram/social/proactive.py:170:        elif binding.paused or not policy.enabled:
```

该处 `policy = binding.proactive`，与 SessionPolicy 无关。`git diff --check` 无输出、通过。

3. **与简报的偏差及原因；测试改动逐条列出**

简报未单列 `loop.py` 的 `suppress_output` 中 `policy_for_key(...).enabled` 引用；同步删除了该条件，否则删除字段后会产生属性错误。未增加其他结构或防御代码。

未修复范围外的既有 Ruff、核心测试和沙箱问题。`test_social_scenarios.py`、`social_harness.py` 均未改动。

以下文件均位于 `nanobot/channels/telegram/tests/`：

| 文件 | 测试名 | 处理方式及原因 |
|---|---|---|
| `test_shared_tools.py` | `test_legacy_isolation_marker_is_removed_when_existing_session_is_loaded` | 整个删除；专测已撤回的隔离标记迁移 |
| `test_shared_tools.py` | `test_private_and_social_turns_have_identical_tools_and_global_switches` | 删除废弃字段的比较，保留工具和全局开关断言 |
| `test_social_runtime.py` | `test_persona_survives_rebuild_with_the_common_profile_and_memory` | 删除 profile/skills 参数，memory 改用 persist；断言不变 |
| `test_social_runtime.py` | `test_restart_restores_common_permissions_and_current_role` | 删除隔离标记、enabled、archive_memory 断言；保留角色和历史断言 |
| `test_social_memory.py` | `test_memory_tool_is_in_shared_catalog_and_errors_are_clear` | 删除 requires_explicit_allowlist 断言，其他断言不变 |
| `test_harness_prompts.py` | `test_conversation_persona_does_not_leak_into_ordinary_mode` | 删除两处废弃 skills 参数；断言不变 |
| `test_social_followups.py` | `test_compaction_rebuild_keeps_trusted_protocol_and_injects_only_live_intentions` | 删除废弃 skills 参数；断言不变 |

另对 `evaluate_social_v4.py::evaluate_case` 内的 `run_main` 删除两处废弃 skills 参数，评估断言不变。

4. **未解决的问题 / 建议**

- **归档 persona 与简报存在冲突。** 删除 `prompt_policy()` 后，归档不再收到 `trusted_persona`，导致三个既有测试的 `PERSONA ALPHA` 断言失败。已保留断言；需编排者决定修订设计以保留 persona，或明确允许调整这些断言。
- **核心完整检查未完成。** 已确认前两个失败/错误在改动前也存在；其余失败尚未逐项定位。停滞疑与同步测试内部 `asyncio.run()` 的跨线程唤醒有关，未修改沙箱插件。
- **Ruff 未通过。** 三个既有错误需另行安排清理；本次类型检查无新增错误。