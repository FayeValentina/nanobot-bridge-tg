# 任务 P1-b：黑话只保留 `social_jargon` 一套接口，拆掉 SocialMemory 的自嵌套

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode” 和 `.agent/orchestration.md`）。

## 背景

`docs/SOCIAL_ARCHITECTURE_REVIEW.md` P1-1 最后一项。黑话现在有两套写入路径：
- 新接口：`social_jargon` 工具 → `JargonLibrary.apply`；
- 旧接口：`social_memory(kind="slang")` → `SocialMemory.apply` → 内部嵌套的第二个 `SocialMemory(jargon=True)` → `JargonLibrary.legacy`。

`SocialMemory` 通过 `jargon=True` 切换表名，把自己当成黑话库用，并持有另一个自己（`self.jargon`），到处是 `if self.is_jargon`。
`JargonLibrary.apply/sources` 还需要传入这个“黑话模式的 SocialMemory”，只为了复用它的 `_evidence`。

已核实：线上 `memories` 表没有 `kind='slang'` 的行（v14 迁移已把它们移进 `jargon` 表）；线上 `jargon` 表有 1 条 usable 记录，不受本任务影响。

## 设计（已定，不得改变）

1. `nanobot/channels/telegram/social/memory.py`
   - 新增模块级函数（放在 `SocialMemory` 类之前）：
     ```python
     def displayed_evidence(store: SocialStore, scope: str, turn_id: str) -> dict[int, MemoryEvidence]:
     ```
     内容就是现在 `SocialMemory._evidence` 中读取 `views` 并构建 `shown` 字典的那段循环（只收 `is_self` 为假的消息），原样搬过来。
   - `SocialMemory.__init__(self, store)`：删除 `jargon` 参数，删除 `is_jargon`、`self.jargon`，以及 `table/evidence_table/suppressions_table/resets_table` 四个属性；
     所有 SQL 直接写 `memories`、`memory_evidence`、`memory_suppressions`、`memory_resets` 这些表名（去掉 f-string 中的表名插值）。
   - `_evidence`：调用 `displayed_evidence(...)` 得到 `shown`，其余检查（reset cutoff、suppression）保留；删除 `if self.is_jargon:` 整个分支。
   - `_entry`：删除 `is_jargon` 的两处条件，`revision` 固定为 `1`，followup 字段固定合并。
   - `_expire`：删除 `if self.is_jargon:` 分支。
   - `apply`：删除开头把 slang / 黑话 id 转给 `self.jargon` 的分支、`if self.is_jargon and ...` 校验、转给 `JargonLibrary.legacy` 的分支，以及 `kind == "slang"` 的检查。
     `clear` 动作中原来调用 `self.jargon.apply(...)["cleared"]` 的地方改为调用 `JargonLibrary(self.store).clear(scope)`（见第 2 条），
     返回值语义不变：`cleared` = 删除的记忆条数 + 删除的黑话条数，且只在 `request.kind is None` 时清除黑话。
   - `list`：删除合并 `self.jargon` 结果的逻辑。把 `_list_local` 的实现直接合并进 `list`，删除 `_list_local`；保持 `list(scope, request, *, all_rows=False)` 签名和语义。
     `active_filter` 中去掉 `'understand_only','usable'`。
   - `has_records`：只查 `memories`。
   - `context`：`if include_jargon and self.jargon is not None:` 改为 `if include_jargon:`，过滤 `item.kind != "slang"` 的列表推导改为不过滤。
   - `MemoryKind` 去掉 `"slang"`；`MemoryRequest` 删除字段 `term` 和 `jargon_status`；`MemoryEntry.status` 的 Literal 去掉 `understand_only`、`usable`、`disabled`。
     Pydantic 会因此自动拒绝 `kind="slang"`，**不要**另加检查或错误提示。
2. `nanobot/channels/telegram/social/jargon.py`
   - 删除 `legacy` 方法。
   - 新增 `clear(self, scope: str) -> int`：内容就是 `legacy` 中 `request.action in {"delete","clear"}` 且 `request.action == "clear"` 时执行的那段逻辑
     （对所有条目做 retire / 加 term suppression / 清空字段并 disabled / 删除 evidence / change_log，然后写 `jargon_resets`），返回处理的条目数。
     调用方已经在事务中（`SocialMemory.apply` 的 `with self.db`），`clear` 内部不要再开事务。
   - `sources(self, scope, turn_id, ids)`：删除 `memory` 参数。实现改为：`shown = displayed_evidence(self.store, scope, turn_id)`，然后对每个 id 依次执行原来
     `SocialMemory._evidence` 中适用于黑话的检查，读的是黑话的表：不在 shown 中 → 报错；`jargon_resets` cutoff → 报错；
     `jargon_suppressions` 中 kind='message' → 报错；原 `is_jargon` 分支中的 human_text / revision 检查 → 报错。
     错误文案沿用原文案。最后像现在一样构建 `ExpressionSource` 列表。
   - `apply(self, scope, turn_id, request)`：删除 `memory` 参数，内部调用 `self.sources(scope, turn_id, ...)`。
   - 删除因此不再需要的 import（例如 `MemoryRequest`、`SocialMemory` 的 TYPE_CHECKING 导入、`re`）。
3. `nanobot/channels/telegram/social/runtime.py`
   - `jargon_action`：删除 `self.memory.jargon is not None` 断言，调用改为 `self.jargon.jargon.apply(scope.session_key, ctx.turn_id, request)`。
   - `SOCIAL_PROTOCOL` 中把 `experiences and slang. Supply message_ids` 改为 `experiences; use social_jargon for slang. Supply message_ids`，其余文字不变。
4. `nanobot/channels/telegram/social/learning.py` 中 v14 迁移对 `kind='slang'` 的处理**保留不动**（它负责迁移历史数据）。

## 测试的处理

- 通过 `social_memory(kind="slang")` 或 `JargonLibrary.legacy` 测试黑话行为的测试：如果相同行为可以用 `social_jargon` / `JargonLibrary.apply` 表达，就改写为新接口；
  如果测试的是旧接口特有的语义（例如从自由文本里切出 term、`jargon_status` 字段），就删除。
- `social_memory` 的 `clear` 同时清除黑话的行为必须有一个测试覆盖（现有就保留，没有就新增一个）。
- 新增一个测试：`social_memory(action="record", kind="slang", ...)` 返回参数校验错误。
- 逐条列出每个修改或删除的测试：文件、测试名、处理方式、原因。
- `test_social_scenarios.py` / `social_harness.py` 不应受影响；受影响就停下报告。

## 允许改动的路径

- `nanobot/channels/telegram/social/memory.py`、`jargon.py`、`runtime.py`
- `nanobot/channels/telegram/tests/test_social_memory.py`、`test_social_jargon.py`、`test_social_learning.py`，以及确实引用了被删内容的其他社交测试文件
- `tests/agent/test_telegram_social_contracts.py`（仅当它引用了被删内容时）

## 防御性代码规则

- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项。
- 不要为“万一有 slang 记忆残留”加兼容处理，背景中已核实不存在。
- 不要重构 `SocialMemory.apply` 的其余逻辑，也不要动 `followups`。
- 预期规模：生产代码净减少约 80–130 行。

## 完成标准

- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过（1 个 xfail）。
- 全仓库搜索：`grep -rnE "is_jargon|jargon=True|\.legacy\(|_list_local|jargon_status" nanobot tests` 无输出（v14 迁移中的历史字符串除外，如有请在报告中说明）。
- `.venv/bin/ruff check nanobot/channels/telegram/` 通过；`.venv/bin/basedpyright nanobot/channels/telegram/social/` 为 0 errors。
- 报告给出 `git diff --stat` 和生产代码净行数。

## 报告格式（最终回复只包含以下内容）

1. 改动摘要（按文件，含净行数）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因；测试改动逐条列出
4. 未解决的问题 / 建议（不要自己实现）
