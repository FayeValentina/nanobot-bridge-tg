# 任务 P3-c：`db/judge.py` 与 `db/deliveries.py`（纯搬迁，行为不变）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-2。Phase 3 已完成：P3-a（`cf1a492e`，Scopes/Messages/Views/StickerLibrary）、
P3-b（`c2668524`，`db/wakes.py` 状态机 + `db/connection.transaction()`）。请先读这两个提交的 `social/db/` 代码，沿用同样的写法。
本任务把 judge 的三张表和投递日志的三张表的 SQL 搬进两个新仓储。**纯搬迁**：SQL 文本、参数、判断逻辑和执行顺序保持原样，
不修 bug，不改行为。投递日志两张表的合并和发送策略的统一是 Phase 5 的工作，本任务**不做**。
事实来源：`.agent/tasks/P3-0.report.md` 第 3、4 节（行号是 P3-a 之前的，以当前代码为准）。

## 设计（已定，不得改变）

### 1. 表归属
| 仓储 | 文件 | 拥有的表 |
|---|---|---|
| `Judge` | `social/db/judge.py` | `judge_state`、`judge_batches`、`judge_attempts` |
| `Deliveries` | `social/db/deliveries.py` | `actions`、`send_attempts`、`message_deliveries` |

规则与 P3-b 相同：仓储拥有表的全部 SQL（读和写）；仓储的 SQL 可以在**只读子查询/JOIN** 中引用别的表，但不得写别的表。
**唯一保留的例外**：`Wakes._check_trigger_source` 中 `SELECT input_meta,decision FROM judge_batches WHERE handoff_job=?` 留在 wakes.py 不动
（`Judge` 依赖 `Wakes`，反过来会形成循环）。在该行上方加一行注释：`# Read-only exception: Judge depends on Wakes, not the reverse.`

### 2. 依赖方向与构造
```
Judge(db, now, scopes: Scopes, messages: Messages, views: Views, wakes: Wakes)
Deliveries(db, now, scopes: Scopes)
```
`SocialStore.__init__` 在 `self.wakes` 之后依次创建 `self.judge`、`self.deliveries`。`db/` 内不允许反向 import（wakes 不 import judge）。
仓储**不得** import 或调用 `followups`、`learning`、`activity` 等 db/ 之外的模块。

### 3. judge 方法去向
- `SocialStore` 的以下方法**整体**移入 `Judge`（调用方改为 `store.judge.<名>`）：
  `judge_through`→`through`、`judge_record`→`record`、`_judge_current`→`_current`、`open_judge_batch`→`open_batch`、
  `claim_judge_attempt`→`claim_attempt`、`finish_judge_attempt`→`finish_attempt`、`judge_fallback`→`fallback`、
  `recover_judge_batches`→`recover`、`queue_judgement`→`queue`、`_judge_metadata`→`_metadata`、`_judge_input_matches`→`_input_matches`、
  `_handoff_judgement`→`_handoff`。`_handoff` 中对 wakes 的调用照旧经 `self.wakes`。
  `judge_runtime.py` 目前直接调用 `store._judge_current`（私有访问），改为调用 `store.judge.current`（把 `_current` 公开命名为 `current`，
  内部调用也用 `current`）。
- `SocialStore` 中仍保留的编排方法里属于 judge 表的 SQL，改为调用 `Judge` 的新方法：
  - `bind`：`judge_batches` 取消其他版本 + `judge_state` UPSERT → `Judge.reset_binding(key, version)`（两条 SQL 原顺序）；
  - `pause`：`judge_state` 清 pending/batch + `judge_batches` 取消 → `Judge.pause(key)`；
  - `wait_for_quiet`：`judge_state` 的 pending_due 收紧 → `Judge.shorten_pending(key, quiet, total)`；
  - `view`：`SELECT * FROM judge_batches WHERE handoff_job=? ORDER BY created DESC LIMIT 1` → `Judge.latest_for_job(job)`。
- `judge_runtime.py` 中对三张 judge 表的全部 SQL（`configure`、`_restore`、`_snapshot`、`start_due`、`_run` 及其 finally 块）改为调用 `Judge` 方法：
  一条不同的 SQL 一个方法，名称描述其作用，文本相同的只建一个；`with self.store.db:` 改为 `with transaction(self.store.db):`，位置不变。
- `proactive.py` 中读 `judge_state` 的两处（`valid`、`tick`）→ `Judge` 方法。
- `expressions.py:busy` 的三段 UNION 查询拆成两次调用：`Wakes.any_active()`（`SELECT 1 FROM scopes WHERE mode='active' UNION ALL SELECT 1 FROM wakes WHERE status IN ('scheduled','queued','running') LIMIT 1`，mode/status 用参数传入）
  和 `Judge.any_pending()`（`SELECT 1 FROM judge_state WHERE inflight=1 OR pending_due IS NOT NULL LIMIT 1`），`busy` 中用 `or` 连接，短路顺序与原 UNION 的含义一致。

### 4. 投递日志方法去向
- `SocialStore` 中只碰投递表的方法**整体**移入 `Deliveries`：`uncertain_send`、`last_send`、`queue_retry`、`ready_sends`、
  `send_predecessor`、`postpone_send`、`claim_retry`。
- 以下方法**留在 SocialStore**（因为要联动 `followups`/`stickers`），其中投递表的 SQL 改为调用 `Deliveries` 的新方法，followups/stickers 调用位置不变：
  `reserve_send`、`delivery`、`enqueue_parts`、`recover`（三条投递 UPDATE → `Deliveries.recover()`）、
  `cancel_opportunity`（actions 的 failed 更新 → `Deliveries.fail_queued_for_turn(scope, turn, error)`）、
  `pause`（→ `Deliveries.fail_queued(scope, error)`）、`bind`（configuration changed 的 actions 更新 → `Deliveries` 方法）。
- `social/runtime.py` 中对三张投递表的全部 SQL（`reserve_message_send`、`send`（含 `UPDATE actions SET reply_to=?,expires=(SELECT MAX(expires) FROM wakes ...)`，
  这条整体搬进 `Deliveries`，wakes 子查询属于允许的只读引用）、`send_parts`、`_deliver`、`_confirmed_send`、`keep_reply_intent`、
  `record_message_receipt`、`get_state` 中的 send_attempts 计数）改为调用 `Deliveries` 方法；
  `record_message_receipt` 中的 `with self.store.db:` 改为 `with transaction(self.store.db):`。
- 其他模块对投递表的读取：`proactive.py`（`status`、`tick` 的 send_attempts 计数）、`followups.py:sync_receipts`（`FROM followup_sends s LEFT JOIN actions ... LEFT JOIN message_deliveries`：
  主表是 followup_sends，**不搬**，留给 P3-d）、`learning.py:confirm_use`、`selection.py:recent_usage` → `Deliveries` 方法；
  `Messages.participation`（FROM messages JOIN actions/message_deliveries）**不动**。
- `next_due` 中对 actions 的 UNION 子查询不动（只读引用）。

### 5. 新方法的命名与返回值
同 P3-a：一条不同的 SQL 一个方法，名称描述返回内容或作用，返回原代码使用的类型（`sqlite3.Row`、`list[sqlite3.Row]`、标量），
不新建 dataclass/TypedDict。报告中列出每个新方法及其替代的原位置。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得“顺手修正”任何 SQL 或逻辑（包括 P3-0 第 7 节的 Q6 等疑点）；写进报告的“建议”。
- 不改 schema、不改 `SCHEMA_VERSION`、不改提示词和工具 schema。
- 预期规模：store.py 再减少约 250 行；judge.py 约 250 行、deliveries.py 约 200 行；生产代码净增不超过 120 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含 `db/`）
- `nanobot/channels/telegram/runtime.py`、`nanobot/agent/tools/telegram_social.py`（仅随方法改名）
- `nanobot/channels/telegram/tests/`、`tests/agent/test_telegram_social_contracts.py`、`scripts/evaluate_social_*.py`：仅机械性改名

## 不要做
- 不要合并 actions 与 message_deliveries，不要统一冷却/预算等发送策略（Phase 5）。
- 不要搬 followups、memory、jargon、learning、expressions、activity、proactive_state 自己的表的 SQL（P3-d）。
- 不要给 SocialStore 留转发方法。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（基线 981 passed）；`test_social_scenarios.py` 不得修改。
- `.venv/bin/ruff check nanobot/ scripts/` 无错误；basedpyright 对 `nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py nanobot/agent/tools/telegram_social.py` 0 errors
  （沙箱找不到依赖时按 P3-a 的方式补 extraPaths，原样说明）。
- 贴出以下搜索结果并逐条说明保留原因：
  - `grep -rnE "(UPDATE|INTO|FROM|JOIN) (judge_state|judge_batches|judge_attempts|actions|send_attempts|message_deliveries)\b" nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py --include=*.py | grep -vE "/db/(judge|deliveries|schema)\.py"`
    （预期只剩：wakes.py 中的只读子查询与上述例外、`Messages.participation`、`followups.sync_receipts`）
  - 每个被移走的 `SocialStore` 方法名的全仓库 grep（预期 0 处旧调用），以及 `_judge_current` 的全仓库 grep（预期 0）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 新建方法清单（仓储.方法 → 替代的原位置）
3. 实际运行的命令及结果（原样贴最后几行）
4. 修改过的测试（逐条：文件:行号，改了什么）
5. 与简报的偏差及原因
6. 未解决的问题 / 建议
