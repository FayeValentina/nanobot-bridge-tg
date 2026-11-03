**1. 改动摘要（按文件）**

- `social/db/judge.py`：新增 Judge，接管三张 judge 表；完整搬迁 12 个方法，封装其他模块的相关 SQL。
- `social/db/deliveries.py`：新增 Deliveries，接管三张投递表；完整搬迁 7 个方法，保留现有发送策略。
- `social/db/wakes.py`：新增 `any_active`；为指定的 judge 只读例外添加要求的注释。
- `social/store.py`：依次构造 `judge`、`deliveries`；移除旧方法，替换目标 SQL；followups/stickers 调用顺序保持不变。减少 302 行。
- `social/judge.py`：更新仓储调用。
- `social/judge_runtime.py`：替换 judge SQL；原事务位置改用 `transaction`；调用公开的 `judge.current`。
- `social/runtime.py`：替换投递 SQL和旧调用；回执事务改用 `transaction`。
- `social/proactive.py`、`learning.py`、`selection.py`：替换相关查询。
- `social/expressions.py`：busy 查询按要求拆分，保留短路顺序。
- 六个测试文件：仅机械改名。

生产代码净增 **120 行**。未修改 schema、提示词、工具 schema 或 `test_social_scenarios.py`；未提交、推送或部署。

**2. 新建方法清单（仓储.方法 → 替代的原位置）**

下表省略路径前缀 `nanobot/channels/telegram/social/`。

| 仓储.方法 | 原位置 |
|---|---|
| Judge.__init__ | store.py：构造仓储依赖 |
| Judge.through | SocialStore.judge_through |
| Judge.record | SocialStore.judge_record |
| Judge.current | SocialStore._judge_current |
| Judge.open_batch | SocialStore.open_judge_batch |
| Judge.claim_attempt | SocialStore.claim_judge_attempt |
| Judge.finish_attempt | SocialStore.finish_judge_attempt |
| Judge.fallback | SocialStore.judge_fallback |
| Judge.recover | SocialStore.recover_judge_batches |
| Judge.queue | SocialStore.queue_judgement |
| Judge._metadata | SocialStore._judge_metadata |
| Judge._input_matches | SocialStore._judge_input_matches |
| Judge._handoff | SocialStore._handoff_judgement |
| Judge.reset_binding | store.py：bind 的两条 judge SQL |
| Judge.pause | store.py：pause 的两条 judge SQL |
| Judge.shorten_pending | store.py：wait_for_quiet |
| Judge.latest_for_job | store.py：view |
| Judge.cancel_other_revisions | judge_runtime.py：configure |
| Judge.requeue_provider_changes | judge_runtime.py：configure |
| Judge.set_through | judge_runtime.py：_snapshot |
| Judge.input_metadata | judge_runtime.py：_restore；搬迁后的 _input_matches 共用 |
| Judge.state | judge_runtime.py：_snapshot、_run.finally；搬迁后的 queue 共用 |
| Judge.due_scopes | judge_runtime.py：start_due |
| Judge.claim_scope | judge_runtime.py：start_due |
| Judge.attach_batch | judge_runtime.py：_run |
| Judge.finish_failed_commit | judge_runtime.py：_run 的 SQLite 异常分支 |
| Judge.retry_failed_commit | judge_runtime.py：_run 的 SQLite 异常分支 |
| Judge.cancel_invalid_input | judge_runtime.py：_run 的普通异常分支 |
| Judge.reschedule_batch | judge_runtime.py：_run.finally |
| Judge.release_scope | judge_runtime.py：_run.finally |
| Judge.advance_through | judge_runtime.py：_run.finally；搬迁后的 _handoff 共用 |
| Judge.discard_pending | judge_runtime.py：_run.finally |
| Judge.pending | proactive.py：valid、tick |
| Judge.any_pending | expressions.py：busy 的 judge UNION 分支 |
| Deliveries.__init__ | store.py：构造仓储依赖 |
| Deliveries.uncertain_send | SocialStore.uncertain_send |
| Deliveries.last_send | SocialStore.last_send |
| Deliveries.queue_retry | SocialStore.queue_retry |
| Deliveries.ready_sends | SocialStore.ready_sends |
| Deliveries.send_predecessor | SocialStore.send_predecessor |
| Deliveries.postpone_send | SocialStore.postpone_send |
| Deliveries.claim_retry | SocialStore.claim_retry |
| Deliveries.recover | store.py：recover 的三条投递 UPDATE |
| Deliveries.fail_other_versions | store.py：bind |
| Deliveries.insert_action | store.py：reserve_send |
| Deliveries.prune_actions | store.py：reserve_send |
| Deliveries.charge_attempts | store.py：reserve_send |
| Deliveries.prune_attempts | store.py：reserve_send |
| Deliveries.record_delivery | store.py：delivery |
| Deliveries.fail_queued_for_turn | store.py：cancel_opportunity |
| Deliveries.fail_later_parts | store.py：delivery |
| Deliveries.failed_parts | store.py：delivery |
| Deliveries.insert_part | store.py：enqueue_parts |
| Deliveries.group_parts | store.py：enqueue_parts；runtime.py：send_parts |
| Deliveries.unknown_content | store.py：reserve_send |
| Deliveries.get | store.py：delivery |
| Deliveries.attempt_count | store.py：reserve_send、enqueue_parts；runtime.py：get_state；proactive.py：tick |
| Deliveries.unknown_part | store.py：enqueue_parts |
| Deliveries.queued | runtime.py：reserve_message_send、send、send_parts |
| Deliveries.set_reply_expiry | runtime.py：send，含 wakes 只读子查询 |
| Deliveries.action_media | runtime.py：_deliver |
| Deliveries.part_position | runtime.py：_deliver |
| Deliveries.sent_sticker | runtime.py：_confirmed_send |
| Deliveries.confirmed_turn | runtime.py：keep_reply_intent |
| Deliveries.insert_message_delivery | runtime.py：record_message_receipt |
| Deliveries.update_message_delivery | runtime.py：record_message_receipt |
| Deliveries.attempted_content | runtime.py：reserve_message_send |
| Deliveries.latest_content | runtime.py：send |
| Deliveries.turn_groups | runtime.py：send_parts |
| Deliveries.message_receipt | runtime.py：record_message_receipt |
| Deliveries.later_part | runtime.py：_deliver |
| Deliveries.turn_actions | proactive.py：status |
| Deliveries.turn_messages | proactive.py：status |
| Deliveries.confirmed_receipt | learning.py：confirm_use |
| Deliveries.recent_usage | selection.py：recent_usage |
| Deliveries.fail_queued | store.py：pause |
| Wakes.any_active | expressions.py：busy 的 scopes/wakes UNION 分支 |

**3. 实际运行的命令及结果**

测试使用编排文档已有的沙箱插件，未修改插件或项目测试环境：

```bash
PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env' \
.venv/bin/python -m pytest nanobot/channels/telegram/tests \
tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py \
-q -p no:cacheprovider --ignore-glob='*evaluate_*'
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
981 passed, 13 warnings in 149.21s (0:02:29)
```

```bash
.venv/bin/ruff check nanobot/ scripts/
```

```text
All checks passed!
```

原始类型检查：

```bash
.venv/bin/basedpyright nanobot/channels/telegram/social \
nanobot/channels/telegram/runtime.py nanobot/agent/tools/telegram_social.py
```

```text
1904 errors, 0 warnings, 0 notes
```

与 P3-a 相同，沙箱未自动发现虚拟环境依赖。临时配置 `/tmp/p3c-pyright.json`：

```json
{
  "extends": "/home/ubuntu/nanobot-bridge-tg/pyproject.toml",
  "extraPaths": [
    "/home/ubuntu/nanobot-bridge-tg",
    "/home/ubuntu/nanobot-bridge-tg/.venv/lib/python3.11/site-packages"
  ]
}
```

```bash
.venv/bin/basedpyright --project /tmp/p3c-pyright.json \
nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py \
nanobot/agent/tools/telegram_social.py
```

```text
0 errors, 0 warnings, 0 notes
```

```bash
git diff --check
```

无输出，退出码 0。

临时 AST/SQL 对照：

```bash
.venv/bin/python /tmp/p3c_audit.py
```

19 个整体搬迁方法均输出 `AST equivalent = True`；对照仅归一化指定改名及共用查询调用。末尾：

```text
Deliveries.send_predecessor: AST equivalent = True
Deliveries.postpone_send: AST equivalent = True
Deliveries.claim_retry: AST equivalent = True
Production net lines: 120
```

指定 SQL 搜索：

```bash
grep -rnE "(UPDATE|INTO|FROM|JOIN) (judge_state|judge_batches|judge_attempts|actions|send_attempts|message_deliveries)\b" \
nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py \
--include=*.py | grep -vE "/db/(judge|deliveries|schema)\.py"
```

```text
nanobot/channels/telegram/social/db/wakes.py:291:        for batch in self.db.execute("SELECT input_meta,decision FROM judge_batches WHERE handoff_job=?", (job,)):
nanobot/channels/telegram/social/db/wakes.py:310:                              "UNION ALL SELECT MIN(MIN(due,expires)) FROM actions WHERE status='queued' "
nanobot/channels/telegram/social/db/wakes.py:314:                              "UNION ALL SELECT MIN(pending_due) FROM judge_state WHERE inflight=0)").fetchone()
nanobot/channels/telegram/social/db/wakes.py:324:            self._move(WakeStatus.CANCELLED, "status IN ('scheduled','queued','running') AND EXISTS (SELECT 1 FROM message_deliveries d WHERE d.scope=wakes.scope AND d.turn_id=wakes.turn_id AND d.status='unknown')", ())
nanobot/channels/telegram/social/db/wakes.py:338:                       "AND EXISTS (SELECT 1 FROM actions WHERE actions.scope=wakes.scope "
nanobot/channels/telegram/social/db/messages.py:119:                               "FROM messages m LEFT JOIN actions a ON a.scope=m.scope AND a.message_id=m.message_id AND a.status='sent' "
nanobot/channels/telegram/social/db/messages.py:120:                               "LEFT JOIN message_deliveries d ON d.scope=m.scope AND d.status='sent' "
nanobot/channels/telegram/social/followups.py:151:                 "FROM followup_sends s LEFT JOIN actions a ON a.id=s.delivery_id "
nanobot/channels/telegram/social/followups.py:152:                 "LEFT JOIN message_deliveries d ON d.id=s.delivery_id "
```

逐条保留原因：

| 位置 | 原因 |
|---|---|
| wakes.py:291 | 简报唯一指定的直接读取例外，已添加要求的注释。 |
| wakes.py:310 | next_due 的 actions 只读 UNION，简报明确保留。 |
| wakes.py:314 | next_due 的 judge_state 只读 UNION，保留现有调度查询。 |
| wakes.py:324 | wakes 恢复更新中的只读 message_deliveries 子查询。 |
| wakes.py:338 | wakes 恢复更新中的只读 actions 子查询。 |
| messages.py:119 | Messages.participation 的 actions JOIN，简报明确不动。 |
| messages.py:120 | 同上，message_deliveries JOIN。 |
| followups.py:151 | sync_receipts 以 followup_sends 为主表，留给 P3-d。 |
| followups.py:152 | 同上，message_deliveries JOIN。 |

全仓库 Python 源码搜索排除 `.git`、`.venv`、`node_modules`、`__pycache__`：judge 旧名使用 `grep -rnw "$name" . --include='*.py'`；保留名称的投递方法使用 `grep -rnP "(?<!deliveries)\.${name}\(" ...` 排除新仓储调用。实际输出：

```text
judge_through: 0 matches
judge_record: 0 matches
_judge_current: 0 matches
open_judge_batch: 0 matches
claim_judge_attempt: 0 matches
finish_judge_attempt: 0 matches
judge_fallback: 0 matches
recover_judge_batches: 0 matches
queue_judgement: 0 matches
_judge_metadata: 0 matches
_judge_input_matches: 0 matches
_handoff_judgement: 0 matches
old calls to uncertain_send: 0 matches
old calls to last_send: 0 matches
old calls to queue_retry: 0 matches
old calls to ready_sends: 0 matches
old calls to send_predecessor: 0 matches
old calls to postpone_send: 0 matches
old calls to claim_retry: 0 matches
```

**4. 修改过的测试**

路径均相对于 `nanobot/channels/telegram/tests/`。全部仅替换方法调用位置或名称，断言语义不变。

| 文件:行号 | 修改 |
|---|---|
| `test_reply_reliability.py:234` | uncertain_send → deliveries.uncertain_send |
| `test_social_judge.py:145,154,241,271,281,282,283,313,349,376,378,383,384,386,387,388,394,395,399,402,403,413,415,435,445,457,525,527,528` | judge 方法改为对应仓储调用 |
| `test_social_judge_runtime.py:84,197,245,289,290,419,420,509` | through、record 调用改名 |
| `test_social_learning.py:59` | judge_through → judge.through |
| `test_social_revisions.py:17,20,21,32,33,82,83` | open_batch、claim_attempt、record 调用改名 |
| `test_social_v5e_replay.py:90` | judge_through → judge.through |

`test_social_scenarios.py` 未修改，并包含在 981 项通过结果中。

**5. 与简报的偏差及原因**

- 原始 basedpyright 命令未通过依赖发现；按 P3-a 方法补充临时 extraPaths 后通过，未修改项目配置或关闭诊断。
- 按指定 `fail_queued(scope, error)` 接口，原 SQL 的 `'paused'` 字面量改为绑定参数，调用值仍为 `"paused"`。其余 SQL 文本变化仅为简报要求的 busy UNION 拆分；相邻字符串字面量合并不改变实际 SQL。
- 文件规模为 Judge 293 行、Deliveries 164 行；生产代码净增 120 行，满足上限。

**6. 未解决的问题 / 建议**

- 无已知搬迁遗留问题。
- P3-0 第 7 节历史疑点（包括 Q6）保持原状；投递表合并和发送策略统一仍留给 Phase 5。
- 建议编排者在正常环境复跑原始 basedpyright 命令。
- 按编排模式未执行构建、启动、提交、推送或部署。