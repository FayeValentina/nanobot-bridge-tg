**1. 改动摘要（按文件）**

- `social/judge.py`：合并内存窗口、输入/输出模型、单次 judge 调用、用量记录和协调器；保留 P7-a 的 B/C 规则。
- 删除 `judge_runtime.py`、`judge_models.py`；`db/judge.py` 替换为仅保存水位的 `db/judge_state.py`。
- `db/wakes.py`：新增 `admit_batch`，保存 admission；删除交接后的 judge 来源复核及 pending deadline 子查询。
- `store.py`、`models.py`：快照改为 admission；移除持久化 judge 入队与窗口操作。
- `runtime.py`、`waiting.py`、`proactive.py`、`scheduler.py`、`expressions.py`：接入共享窗口、恢复、deadline 和 busy 回调。
- `db/schema.py`：新增 schema 22；升级前导出历史，新库直接建立新的 judge 状态结构。
- `config/schema.py`：judge 配置缩减至指定字段，默认超时 8 秒、上下文 12 条。
- `social_judge.md`：与指定提示词原文逐字一致。
- 测试及三个评估脚本完成适配；未运行评估脚本。

`wc -l`：

| 文件 | 修改前 | 修改后 |
|---|---:|---:|
| judge.py | 178 | 372 |
| db/judge.py → db/judge_state.py | 314 | 20 |
| db/wakes.py | 540 | 546 |
| store.py | 329 | 320 |
| runtime.py | 352 | 363 |

judge 相关四文件合计 **1043→392 行**；全部改动生产代码净减少 **627 行**。

**2. 实际运行的命令及结果**

pytest 均使用现有沙箱插件环境：`PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env'`。

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
```

```text
919 passed, 13 warnings in 111.80s (0:01:51)
```

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
```

```text
22 passed in 9.19s
```

```bash
.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway tests/config -q -p no:cacheprovider
```

```text
189 passed, 2 skipped in 7.55s
```

```bash
.venv/bin/ruff check nanobot/ scripts/
```

```text
All checks passed!
```

```bash
.venv/bin/basedpyright -p /tmp/p7b/pyrightconfig.json
```

```text
0 errors, 0 warnings, 0 notes
```

临时 pyright 配置沿用工程 strict 设置，明确指定 `.venv`、工程路径和 social 检查范围。[配置文件](/tmp/p7b/pyrightconfig.json)

`cmp` 提示词、`git diff --check` 均退出 0。

指定 grep **尚不满足字面验收要求**：

```text
TOTAL 66
RUNTIME_MATCHES 0
```

命中仅位于历史 schema 升级和迁移测试。[完整 grep 输出](/tmp/p7b/grep-final.log)

首轮完整目录曾为 `12 failed, 905 passed`，修复后得到上述最终结果。[首轮日志](/tmp/p7b/all-tests.log) · [最终日志](/tmp/p7b/all-tests-final2.log)

**3. 与简报的偏差及原因**

使用了简报允许的一个 `due_cap` 字段：保留旧 `shorten_pending` 覆盖 deadline 的效果；新增消息会清除此覆盖。`judge.py` 为 372 行，高于约 300 行的预估，相关生产代码合计仍低于 400 行。

历史迁移引用暂时保留，原因及待决定事项见第 5 节。

调用点映射：

| 原调用/读取 | 新调用/位置 |
|---|---|
| `store.observe → judge.queue` | `runtime.observe → judging.note`，使用持久化事实中的 received_at |
| `store.wait_for_quiet → shorten_pending` | `SocialWaiting.wait/keep_reply_intent → windows.shorten` |
| `store.pause → judge.pause` | 删除 DB pending 操作；runtime 同步时 discard 窗口 |
| `store.bind → judge.reset_binding(key, version)` | `JudgeState.reset_binding(key)`；runtime 在版本变化时 discard |
| `wakes.next_due` 的 judge 子查询 | scheduler 的 `extra_due → judging.next_due()` |
| learner 的 `judge.any_pending()` | 注入的 `judge_busy → judging.busy()` |
| proactive 两处 `judge.pending(key)` | `windows.has(key)` |
| `runtime.recover → store.judge.recover` | `judging.recover()` |
| `get_state → intent(scope)` | `intent(key)`；水位仍由 JudgeState 读取 |
| `latest_for_job → judgement` | `wakes.admission → snapshot.admission` |
| `Judge._handoff` | `Wakes.admit_batch` |
| `_check_trigger_source` 的批次复核 | 删除；保留 condition_config 检查 |
| provider revision 取消、重排 | 删除；重建无持久化 SocialJudge |
| 三个评估脚本的批次/attempt 查询 | admission、JudgeInput/Verdict、内存 deadline |

**删除测试清单、逐文件计数及全部断言原文/新文/行号/理由，列于本节完整附件：[测试审计](/tmp/p7b/test-audit.md)。** 附件包含 37 组断言变化，区分删除与改名；schema-8 升级测试已保留并适配。

场景与 harness 仅删除已退休的 judge 配置字段：21 个场景函数、全部场景断言保持不变；参数化展开后 22 项通过。

协议改前：

```text
Its judgement gives
source messages and a short reason, not a command to speak.
```

改后：

```text
Its admission gives
the admission source and a short reason, not a command to speak.
```

另一处改前：

```text
One-shot conditions are consumed only when a matching judgement is
admitted or actually displayed;
```

改后：

```text
One-shot conditions are consumed only when a matching admission is
admitted or actually displayed;
```

其余协议文本保持不变。

**4. 线上库副本演练结果**

使用 SQLite backup API，源库 URI 带 `?mode=ro`，复制到 `/tmp/p7b/social.sqlite3`，随后仅迁移副本。

| 项目 | 迁移前 | 导出文件 | 迁移后 |
|---|---:|---:|---|
| schema | 21 | — | 22 |
| judge_batches | 59 | 59 | 已删除 |
| judge_attempts | 41 | 41 | 已删除 |

各 scope 水位完全一致：

| scope | 前→后 |
|---|---:|
| `telegram:-1003760551468:topic:53` | 508→508 |
| `telegram:-1003760551468` | 148→148 |
| `telegram:-1003760551468:topic:6532` | 1→1 |

其余全部表行数保持一致，包括 judge_state 的 3 行。[逐表前后对比](/tmp/p7b/rehearsal.md) · [原始结果 JSON](/tmp/p7b/rehearsal.json)

```text
history exported: {'judge_batches': 59, 'judge_attempts': 41}
watermarks unchanged: True
other tables unchanged: True
```

```text
quick_check: [('ok',)]
foreign_key_check: []
```

**5. 未解决的问题 / 需要编排者决定的事项**

唯一待决定事项是 grep 例外范围：现有 8→9、9→10、10→11 升级以及要求保留/新增的迁移测试必须引用旧表名。目前保留这些历史升级与测试，生产运行时零引用，因此未宣称全部验收标准满足。

建议允许“schema.py 历史升级和迁移测试”作为例外。此前提交的澄清尚未收到答复，未自行删除历史升级兼容性或规避字符串检查。

未提交、推送、部署，未写入线上原库。