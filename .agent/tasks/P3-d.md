# 任务 P3-d：Phase 3 收尾：表的单一归属、schema 集中、事务写法统一、SQL 归属测试

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
Phase 3（docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-2/P0-3）已完成三个任务：P3-a `cf1a492e`、P3-b `c2668524`、P3-c `7ec03fa3`。
核心表（scopes/messages/updates/views/stickers/wakes/requests/dialogue_*/judge_*/actions/send_attempts/message_deliveries/runtime_settings）
的 SQL 已全部在 `social/db/` 的仓储里。本任务收尾：
1. 把 schema 升级函数集中到 `db/schema.py`；
2. 解开 memory ↔ jargon 的循环（目前用函数内延迟导入规避）；
3. 让 proactive_state 和学习批次等表“只有拥有者模块执行 SQL”；
4. 把 social 包里剩余的旧式 `with self.db:` 统一成 `transaction()`；
5. 加一个测试，固定“哪些文件可以执行 SQL”。

除第 4 项可能带来的事务边界变化（见该节）外，**行为不变**；SQL 文本、参数和判断逻辑原样搬迁。

## 设计（已定，不得改变）

### 1. schema 升级函数并入 `db/schema.py`
`learning.py` 中的 `upgrade_jargon_schema`、`upgrade_expression_schema`、`upgrade_learning_schema`，`activity.py` 中的 `upgrade_activity_schema`，
原样（函数体逐字不变）移到 `db/schema.py`，作为该文件内的私有函数（名字前加 `_`）；`db/schema.py` 不再 import learning/activity。
这四个函数只有 `db/schema.py` 调用（已核实）。

### 2. memory ↔ jargon
- `MemoryEvidence`（`memory.py`）原样移到 `models.py`；`memory.py` 及其他引用处改为从 models 导入。
- `displayed_evidence(store, scope, turn_id)`（`memory.py`）改为 `Views.displayed_evidence(key, turn_id) -> dict[int, MemoryEvidence]`，函数体逻辑和注释原样；
  调用方（`memory.py`、`jargon.py`）改为 `self.store.views.displayed_evidence(...)`。
- 之后 `jargon.py` 不再 import memory；`memory.py` 在文件顶部 `from nanobot.channels.telegram.social.jargon import JargonLibrary`，
  删除两处函数内的延迟导入。
- `memory.py` 中 `apply` 里查询 jargon 表计数的那段 UNION（`... UNION ALL SELECT COUNT(*) n FROM jargon ...`）：jargon 部分改为调用
  `JargonLibrary` 的新方法（返回同一计数），memories 部分留在 memory.py；两者相加的语义与原 SQL 一致。

### 3. 表归属（本任务涉及的部分）
| 拥有者 | 表 |
|---|---|
| 新建 `db/proactive_state.py` 的 `ProactiveState(db, now)`，挂在 `store.proactive_state` | `proactive_state` |
| `SocialLearning`（learning.py） | `learning_message_revisions`、`learning_events`、`learning_cursors`、`learning_batches`、`learning_counts`、`learning_changes`、`expression_turns`、`expression_selections` |
| `JargonLibrary`（jargon.py） | `jargon`、`jargon_evidence`、`jargon_suppressions`、`jargon_resets` |
| `SocialMemory`（memory.py） | `memories`、`memory_evidence`、`memory_suppressions`、`memory_resets` |
| `SocialFollowups`（followups.py） | `memory_followups`、`followup_attempts`、`followup_sends` |
| 表达库类（expressions.py） | `expressions`、`expression_evidence`、`expression_guards` |
| `SocialActivity`（activity.py） | `activities`、`activity_steps` |

**规则**：对他人表的**独立查询和任何写入**都必须改为调用拥有者的方法。一条 SQL 以自有表为主体、只在 JOIN/子查询中**读**他人表的，可以留在原处
（例如 followups.py 中 `FROM memories m JOIN memory_followups f` 这类以跟进为主体的查询、`followups.sync_receipts` 对 actions/message_deliveries 的 JOIN、
`expressions.py:commit` 中 `expression_evidence JOIN learning_changes`、jargon.py `create` 中同时计数 jargon 与 memories 的子查询）。

### 4. 需要改的具体位置（以当前代码为准逐一核对，不要遗漏同类位置）
- `proactive.py` 中全部 proactive_state SQL → `ProactiveState` 方法；`followups.py:_live` 的 `SELECT stopped FROM proactive_state` → `store.proactive_state` 方法；
  `social/runtime.py:set_wake` 的 `UPDATE proactive_state SET stopped=?` → `store.proactive_state` 方法。`ProactiveState` 只含原 SQL，不含 tick/valid 等判断逻辑（逻辑留在 proactive.py）。
- `expressions.py`：learning_changes 的 INSERT 与 generation 查询、learning_batches 的全部 SQL → `SocialLearning` 方法（经 `self.store.learning`）。
- `jargon.py`：learning_changes 的 INSERT 与 generation 查询、`apply` 中 `UPDATE learning_batches ... explicit_recheck` → `SocialLearning` 方法。
- `jargon_learning.py`：learning_batches/learning_cursors 的全部 SQL → `SocialLearning` 方法；jargon/jargon_evidence 的 SQL → `JargonLibrary` 方法。
  完成后 jargon_learning.py 中不得再有 `.execute(`。
- `social/runtime.py:run_due` 中按 kind 取最早 learning_batches 的排序查询 → `SocialLearning` 方法。完成后 social/runtime.py 中不得再有 `.execute(`。
- `proactive.py:valid` 中 `SELECT status,revision,expires FROM memories ...` → `SocialMemory` 方法（proactive 已持有 memory）。完成后 proactive.py 中不得再有 `.execute(`。
- `selection.py`：`expression_turns`、`expression_selections` 的查询 → `SocialLearning` 方法。
- `store.py:_collect_learning` 中的 `SAVEPOINT`/`ROLLBACK TO`/`RELEASE` 改用 `db/connection.py` 新增的
  ```python
  @contextmanager
  def savepoint(db: sqlite3.Connection, name: str) -> Generator[None, None, None]:
      """Roll back only this block on error, then re-raise."""
  ```
  语义必须与原代码完全一致：原代码在异常时 `ROLLBACK TO` 并**吞掉异常**、记录日志和计数，最后总是 `RELEASE`。因此 `savepoint` 只负责
  “进入时 SAVEPOINT、异常时 ROLLBACK TO、退出时 RELEASE”，并**重新抛出**异常；`_collect_learning` 里保留原来的 `try/except Exception` 记录日志和计数
  （位置在 `with savepoint(...)` 外面）。完成后 store.py 中不得再有 `.execute(`。
- 有意**保留到 Phase 7**（不要改）：`learning.py` 中 `invalidate`、`record_selection`、`confirm_use`、`status` 对 expressions/jargon/jargon_evidence 表的读写，
  以及 `selection.py` 中 `candidates`/`current` 的动态多表查询（它们要等学习流水线重组时一起处理，否则会形成 learning ↔ expressions/jargon 的循环依赖）。

### 5. 事务写法统一
`nanobot/channels/telegram/social/` 下（db/ 之外）所有 `with self.db:`、`with self.store.db:`、`with store.db:`、`with db:`、`with self.runtime.store.db:`
改为 `with transaction(<同一个连接>):`；learning.py 中 `nullcontext() if self.db.in_transaction else self.db` 这类写法也改为 `transaction(self.db)`。
**改之前必须先做一项分析并写进报告**：旧式 `with db:` 退出时会提交**当前连接上任何未提交的事务**，包括块外更早执行的 DML；`transaction()` 在已有事务时只是加入、不提交。
因此请找出 social 包中所有**不在任何事务块内执行的写语句**（INSERT/UPDATE/DELETE/REPLACE），对每一处说明它在生产路径上由哪个调用方的事务块包住；
若存在“不在任何事务块内、依赖后面某个旧式 `with db:` 顺带提交”的写语句，**停下来不要转换**，把这些位置列在报告里由编排者决定。

### 6. SQL 归属测试
新增 `nanobot/channels/telegram/tests/test_social_sql_ownership.py`，一个测试函数：扫描 `nanobot/channels/telegram/social/` 下所有 `.py`
以及 `nanobot/channels/telegram/runtime.py`、`nanobot/agent/tools/telegram_social.py`，找出包含 `.execute(`、`.executemany(`、`.executescript(` 的文件，
断言其集合是下面允许集合的子集：
```python
ALLOWED = {
    "social/db/connection.py", "social/db/schema.py", "social/db/scopes.py", "social/db/messages.py", "social/db/views.py",
    "social/db/stickers.py", "social/db/wakes.py", "social/db/judge.py", "social/db/deliveries.py", "social/db/proactive_state.py",
    # Owner modules of knowledge/learning tables; move under db/ when Phase 6/7 regroups them.
    "social/memory.py", "social/jargon.py", "social/followups.py", "social/activity.py", "social/learning.py", "social/expressions.py",
    # Dynamic candidate queries across expressions/jargon/stickers; Phase 7.
    "social/selection.py",
}
```
路径相对 `nanobot/channels/telegram/`。只用 `pathlib` 读文件内容做子串判断，不引入新依赖，测试不超过 30 行。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得“顺手修正”任何 SQL 或逻辑；写进报告的“建议”。
- 不改 schema 内容、不改 `SCHEMA_VERSION`、不改提示词和工具 schema。
- 预期规模：生产代码净增不超过 80 行（新方法的签名开销）；`db/proactive_state.py` 约 60 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含 `db/`）
- `nanobot/channels/telegram/tests/`：新增上述测试；其余仅机械性改名/改 import
- `tests/agent/test_telegram_social_contracts.py`、`scripts/evaluate_social_*.py`：仅机械性改名

## 不要做
- 不要处理第 4 节中明确“保留到 Phase 7”的部分。
- 不要把 memory/jargon/learning 等模块搬进 `db/`，不要拆分或重命名这些模块里的类。
- 不要给 SocialStore 留转发方法。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（基线 981 passed，加上新测试应为 982）；`test_social_scenarios.py` 不得修改。
- `.venv/bin/ruff check nanobot/ scripts/` 无错误；basedpyright 对 `nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py nanobot/agent/tools/telegram_social.py` 0 errors。
- 贴出：`grep -rn "\.execute(\|\.executemany(\|\.executescript(" nanobot/channels/telegram/social --include=*.py -l`；
  `grep -rn "^\s*from nanobot.channels.telegram.social" nanobot/channels/telegram/social/memory.py nanobot/channels/telegram/social/jargon.py`；
  `grep -rn "with self.db:\|with self.store.db:\|with store.db:\|with db:\|with self.runtime.store.db:\|nullcontext" nanobot/channels/telegram/social --include=*.py`（预期 0）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 新建方法清单（拥有者.方法 → 替代的原位置）
3. 第 5 节要求的“块外写语句”分析结果（逐条：文件:行号、写语句、生产路径上包住它的事务块）
4. 实际运行的命令及结果（原样贴最后几行）
5. 修改过的测试（逐条）
6. 与简报的偏差及原因
7. 未解决的问题 / 建议
