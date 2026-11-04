# 任务 P7-d：selection 简化与容错；候选 SQL 与 learning.py 跨拥有者 SQL 迁回各 Library【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P1-2（selection 部分）、Phase 3 遗留；你的 P7-0 清单第 3.3、4 节与疑点 12–15。
线上 selector 经常整次失败：(1) 模型把 `jargon` 返回成对象 `[{"id":"j1","note":"codex"}]`，`Choice` 严格要求字符串 → ValidationError；
(2) 模型引用了本次没有提供的候选 ID，或类别不符 → `ValueError("unknown or stale candidate")` / `invalid selection`。任一项出错都会让整次选择作废（连可用的贴纸一起丢）。
这是一个 2 秒、可选、失败即空的提示增强，不需要这么严格。

## 设计（已定，不得改变）

### 1. 解析容错（`social/selection.py`）
- `Choice` 去掉 `strict=True` 与 `extra="forbid"`（改为 `extra="ignore"`）；`expressions/jargon/stickers` 的元素类型改为 `str | dict[str, object]`，并新增方法
  `def ids(self, field: str) -> list[str]`：字符串原样；字典取 `"id"` 键且其值为字符串时取该值；其余元素丢弃。`max_length` 约束去掉（数量上限由下面的 limit 截断）。
- 选择循环中：未知 ID、类别不符、`current()` 为 False 的候选**跳过**（不再抛错），其余照常收集并按 limit 截断。
  被跳过的 ID 数写进 `explanation` 的新键 `"dropped": <int>`（record_selection 已有 explanation 字典，只加这一个键）。
- `response.finish_reason != "stop"`、有 tool_calls、内容为空或超长、JSON 无法解析 → 仍按现在的方式记为 error（这些是整体无效）。

### 2. 一个 turn 只选一次、缓存到 turn 结束、调用后不复核
- 缓存命中时**直接返回** `previous.selected`，删除命中时对 source/version/config_revision/messages_current/current 的比较，以及 `reuse_status` 写入。
- 删除 LLM 返回后的输入复核（`self.input(...) != source or not self.messages_current(...) or version/config_revision 变化 → raise ValueError("input changed")`）。
- 新增 `def forget(self, scope: str, turn: str) -> None`：从 cache 删除该键。`SocialExposureHook.on_finally` 在 turn 结束时调用（hook 构造增加 `selector` 参数或通过 context_builder 取得——二选一，选不新增依赖的那种，在报告里说明）。
  保留 100 项上限与 `has_selection` 重启保护。
- `messages_current` 若删除后无调用者则删除。`record_selection` 中的业务校验（条目存在、revision/status）保留不动（疑点 13：这是写入约束）。

### 3. 候选 SQL 迁回拥有者
- `selection.py` 中拼接表名的候选查询（`selection.py:152–179` 附近）与 `current()` 中的 `SELECT * FROM {table} WHERE scope=? AND id=?`，按 P7-0 第 4.2 节的表格迁到：
  - `ExpressionLibrary.candidates(scope, needles, limit) -> list[sqlite3.Row]`、`ExpressionLibrary.row(scope, entry_id) -> sqlite3.Row | None`
  - `JargonLibrary.candidates(scope, needles, now, limit)`、`JargonLibrary.row(scope, entry_id)`
  - `db/stickers.py` 的贴纸仓储：`candidates(scope, needles, limit)`、`row(scope, entry_id)`
  SQL 文本与参数**原样**搬过去（包括 relevance/variety 排序），只是每个类别一份固定表名。selection.py 中不再有 `db.execute`。
  ContextSelector 构造增加它需要的 library 引用（ExpressionLibrary、JargonLibrary）；贴纸仓储通过 `store.stickers` 取得。

### 4. learning.py 的跨拥有者 SQL（Phase 3 遗留，P7-0 第 3.3 节表格 7 处）
逐处迁到对应拥有者的方法，SQL 原样：
- `learning.py:114–125`（invalidate 中 evidence JOIN、expression expired / jargon disabled、revision+1）→ `ExpressionLibrary.invalidate_source(...)`、`JargonLibrary.invalidate_source(...)`；`learning_changes`/批次取消/events 仍由 SocialLearning 写。
- `233–237`（selection 条目合法性）、`245–248`（last_selected）、`261–262`（usable jargon 确认）、`270–272`（last_confirmed）、`284`（usable expression 计数）、`285–286`（jargon 数量与状态分布）
  → ExpressionLibrary / JargonLibrary 的方法（方法名自定，与已有命名风格一致，在报告中列出）。
- SocialLearning 需要这两个 library 的引用：`SocialLearning` 构造时拿不到（它在 store 内创建）——改为在 runtime 构造 learner 后调用 `store.learning.attach_libraries(expression_library, jargon_library)` 设置。
  （若有循环导入，类型注解放进 TYPE_CHECKING。）
- 迁移完成后 `test_social_sql_ownership.py` 的允许清单相应收紧：learning.py 不再执行 expressions/expression_evidence/jargon/jargon_evidence 表的 SQL；把这一点写成该测试中的断言。

## 要做的事
1. 按设计实现。
2. 测试：
   - 新增：(a) 模型返回 `{"jargon":[{"id":"j1","note":"x"}],"stickers":["s2"]}` 且 j1、s2 都是本次候选 → 两个都选中；
     (b) 返回中含未提供的 ID → 跳过它，其余选中，explanation.dropped=1；(c) 同一 turn 第二次 `select` 不再调用 provider，即使消息有变化也返回首次结果；
     (d) turn 结束后缓存键被删除；(e) 内容不是 JSON → status=error、selected 为空（保持现状）。
   - 原先断言“输入变化返回空”“调用后复核”的测试按新语义修改或删除，逐条列出（文件:行号、原、新/删除、理由）。
   - 其余机械适配逐文件计数。
3. 用 `/tmp/p7/container-72h.log` 中两次 ValidationError 的原始模型输出（P7-0 第 4.4 节）各写一个测试用例，证明现在能解析出贴纸选择。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、文件；新增方法限于简报列出的和第 4 节要求的 Library 方法。
- 预期规模：selection.py 净减少；全部生产代码净变化不超过 +30 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（selection.py、expressions.py、jargon.py、learning.py、db/stickers.py、hook.py、context.py、runtime.py）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`、`nanobot/channels/telegram/tests/evaluate_*.py`（仅机械适配，不运行）

## 不要做
- 不改选择提示词 `social_expression_selection.md`、候选召回的打分规则、`record_selection` 的写入内容（除 explanation.dropped）、贴纸/表达/黑话的业务规则。
- 不处理 P0-6 的角色字面量（SCENE_HINTS、口头禅列表；Phase 8）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净；basedpyright（social/）0 errors
- `grep -n "db.execute" nanobot/channels/telegram/social/selection.py` 0 命中
- 报告 `wc -l`：selection.py、learning.py、expressions.py、jargon.py 前后对比

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因（含语义性测试修改清单、新增 Library 方法清单）
4. 未解决的问题 / 需要编排者决定的事项
