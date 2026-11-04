# 任务 P9-a：live memory 每 turn 只注入一次、活动注入改为索引、工具 schema 去掉 title 注释【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
`.agent/tasks/P9-T0.report.md`、`.agent/tasks/P9-0.report.md` 第 1、2、4 节（必读）。已确认：
- `SocialExposureHook.before_iteration` 每次迭代都 build 并注入 live memory 块；runner 在恢复原文之前已把请求深拷贝进 accepted/H 前缀，所以第 n 次请求带 n 份块（线上每份约 5.6k token）。
- live memory 块中最大项是无条件注入的最近 5 条**完整**活动记录（topic 53 约 5.5k cl100k token）。
- 工具 schema 中 pydantic 生成的 `title` 注释每次请求约 399 token（注意：`social_activity` 的 `properties.title` 是真实参数，不能删）。

owner 已定方针：live memory 每 turn 只注入一次，turn 内状态变化依靠工具结果，**不补发更新**；普通 turn 只注入活动索引；去掉 schema title 注释。

## 设计（已定，不得改变）

### T1 `nanobot/channels/telegram/social/hook.py`
- `before_iteration`：保留开头的 `self._restore_memory_context()`；其后仅当 `context.iteration == 0` 时才执行 build 与注入（现有的 scope/binding/turn_id 检查与注入代码不变，只是放在这个条件之后）。`iteration > 0` 时直接返回。
- 恢复逻辑（`_restore_memory_context`、`after_iteration`、finally 中的调用）**不改**。
- 依据（无需额外代码）：social 会话都有 `ContextCompactionState`（P9-0 §1.1），第一次请求被 accept 进 H 后，后续请求从 H 开头自然携带这一份块。
- 已知且接受的限制（不要为此加代码，只在报告中确认行为）：turn 中途发生 compaction 时，该块会进入摘要输入、之后的迭代不再携带块。

### T2 活动注入：`social/activity.py`、`social/context.py`
- `SocialActivity` 新增两个方法（SQL 留在 activity.py）：
  - `index(self, scope: str) -> list[dict[str, object]]`：与 `context()` 同样的筛选与排序（`ORDER BY status='active' DESC, updated DESC LIMIT 5`），只返回 `id, status, title, next_step` 四列。
  - `current(self, scope: str) -> dict[str, object] | None`：该 scope 中 `status='active'` 的那一条完整记录（与 `context()` 单条的形状相同，含解析后的 `artifacts`），没有则 None。
- `context()` 保持不变（`social_activity(action="list")` 仍用它返回完整记录）。
- `SocialContextBuilder.build`：`body_data["autonomous_activities"] = self.store.activities.index(key)`；在已有的 `running_activity` 分支中（activity turn）再加 `body_data["current_activity"] = self.store.activities.current(key)`——注意 body_data 必须在序列化之前设置，所以把 `running_activity` 的查询移到 `json.dumps(body_data)` 之前；`telegram_autonomous_work` 模板块的追加位置与条件不变。

### T4 `nanobot/agent/tools/base.py` `Tool.to_schema`
- 返回的 `parameters` 改为 `_strip_schema_titles(self.parameters)` 的结果（模块级私有函数，就放在 base.py）。**不修改** `self.parameters`（本地 `validate_params` 仍用原 schema）。
- `_strip_schema_titles(node)`：返回去掉注释后的深拷贝。规则：
  - dict 视为 schema 节点：删除其值为 `str` 的 `"title"` 键；其余键递归处理。
  - 但 `"properties"`、`"$defs"`、`"definitions"`、`"patternProperties"` 这几个键的值是“名字 → schema”的映射：对映射本身**不删任何键**，只对每个值递归。
  - list：逐项递归。其他值原样返回。
- 例：`{"properties": {"title": {"type": "string", "title": "Title"}}, "title": "ActivityRequest"}` → `{"properties": {"title": {"type": "string"}}}`。

## 要做的事
1. 按 T1、T2、T4 实施。
2. 测试（加在最贴近的现有文件，不新建测试文件）：
   - T1：用现有 runtime + AgentLoop 测试模式（参考 `test_social_runtime.py` 中构造 loop 并检查 `chat_stream_with_retry` 请求的测试），跑一个至少 3 次模型请求的 social turn（例如 social_sticker list → social_send → 结束），断言**每一次**请求的 messages 中 `Live group memory index` 恰好出现 1 次；turn 结束后持久化的会话消息中出现 0 次。
   - T2：普通 turn 的块中 `autonomous_activities` 每项只有四个键；activity turn 的块中有 `current_activity` 且为 active 那条完整记录；无 active 活动的 activity turn 中 `current_activity` 为 None。
   - T4：`social_activity` 的 schema 经 `to_schema()` 后仍有 `properties.title`；所有已注册工具的 `to_schema()` 中不再出现值为 str 的 schema 节点 `title`（按上面的规则遍历检查）；`validate_params` 行为不变（用 social_memory 一个非法参数举例）。
   - 依赖旧行为的现有断言（例如断言 live memory 块含完整活动字段、断言每次迭代重新注入）：改为新行为，逐条列出。
3. 场景测试 `test_social_scenarios.py` 不得修改；若失败，停下报告。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不为“无 H 路径”或 compaction 后补注入加任何代码。
- 预期规模：生产代码新增不超过 40 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/{hook,activity,context}.py`、`nanobot/agent/tools/base.py`
- `nanobot/channels/telegram/tests/`（不含 evaluate_*、fixtures、test_social_scenarios.py）、`tests/agent/`、`tests/tools/`（若存在 to_schema 测试）

## 不要做
- 不改 runner、context_governance、runtime_context、摘要逻辑、provider。
- 不改 `SocialActivity.context()`、`social_activity` 工具、活动表结构。
- 不改 protocol 与模板。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/python -m pytest tests/agent tests/tools tests/providers tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider`（目录不存在的跳过并注明）
- `.venv/bin/ruff check nanobot/`；`.venv/bin/basedpyright -p <你上次使用的显式 venv 配置> nanobot/channels/telegram/social nanobot/agent/tools/base.py` 0 errors。
- 报告中给出：用 `/tmp/p9/work` 中 P9-0 的回放脚本（复制一份到 `/tmp/p9/work/p9a/` 再改为调用新代码，不要 monkeypatch）重跑第二次唤醒 4 次请求，列出每次请求的块份数与 cl100k 估算，与 P9-0 §1.1 表对比。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 回放对比表
4. 与简报的偏差及原因
5. 未解决的问题 / 需要编排者决定的事项
