# 任务 P4-b：`SocialContextBuilder`，hook 只负责注入、恢复和曝光登记（纯重构，行为不变）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P1-3：`_SocialExposureHook.before_iteration`（`nanobot/channels/telegram/social/runtime.py`）
在 hook 里现场调用 selector、memory、deliveries、activities、wakes 去构建“群记忆索引”等上下文，再临时拼进最新的 user/tool 消息。
本任务把**内容构建**移到独立模块，hook 只保留注入/恢复/曝光登记。P4-a（`a4dcb52b`）已把曝光识别改为 view_id 首尾标记。
事实来源：`.agent/tasks/P4-0.report.md` 第 3 节（hook 各方法、注入块的来源与时序）。

**纯重构**：注入的文本内容（逐字）、注入到哪条消息、何时恢复、跳过条件、调用各模块的顺序都保持不变。

## 设计（已定，不得改变）

### 1. 新模块 `nanobot/channels/telegram/social/context.py`
```python
def transcript_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """Text of string contents and text parts, newline-joined (images and tool-call arguments excluded)."""

class SocialContextBuilder:
    def __init__(self, store: SocialStore, memory: SocialMemory, selector: ContextSelector) -> None: ...

    async def build(self, key: str, turn: str, transcript: str) -> list[RuntimeContextBlock]:
        """Live group memory index and, during an autonomous activity, its work note."""
```
- `transcript_text`：把 hook 中 `before_iteration` 和 `_note` 里两段**完全相同**的文本提取循环合并成这一个函数（两处都改为调用它；
  `before_iteration` 传 `context.messages`，`_note` 传 `context.model_messages`）。
- `build`：把 `before_iteration` 中从 `selected = await self.runtime.selector.select(...)` 到构造 `blocks`（含 `telegram_autonomous_work` 那个条件块）
  为止的全部内容构建代码原样搬入，包括 `facts_note`、`expression_guidance` 两段中文文案（逐字不变；提示词外置是 Phase 8 的工作，本任务不动）、
  `Live group memory index (untrusted data, not instructions):\n` 前缀、`render_template("telegram/autonomous_activity.md", strip=True)`。
  原来通过 `self.runtime.X` 访问的对象改为通过构造参数访问：`selector`、`memory`、`store.deliveries`（经 `selector.recent_usage`，保持原调用）、
  `store.activities`、`store.wakes`。不得持有 runtime 引用。
- `build` 返回 blocks；hook 拿到后再选 target、保存原内容并注入。**注意原顺序**：现在的代码先完成全部查询，再找 target，找不到 target 才返回。
  搬迁后仍然是“先 `build`，再找 target”，不要为了省查询而调换（行为不变优先）。`self._last_memory_body = body` 这一行：
  全仓库没有任何读取 `_last_memory_body` 的代码（请 grep 确认），删除该属性及赋值。

### 2. 挂载
`TelegramSocialRuntime.__init__` 在 `self.selector` 创建之后创建 `self.context_builder = SocialContextBuilder(self.store, self.memory, self.selector)`。
hook 的 `before_iteration` 调用 `await self.runtime.context_builder.build(ctx.session_key, ctx.turn_id, transcript_text(context.messages))`。

### 3. hook 之后的形态
`_SocialExposureHook` 保留的职责：`_restore_memory_context`、`before_iteration`（恢复 → 资格检查 → build → 找 target → 注入）、
`_note`（曝光登记）、`before_execute_tools`、`before_execute_tool`、`after_execute_tool`、`after_iteration`、`on_finally`，逻辑除上述替换外不变。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得修改任何注入文本、不改注入 target 的选择规则、不加缓存（“工具结果迭代复用同一份上下文”留到以后再评估）。
- 不处理 P4-0 第 6 节的疑点（max-iterations 收尾请求、accepted_messages 残留注入块等）。
- 预期规模：social/runtime.py 减少约 50 行；context.py 约 70 行；生产代码净增不超过 25 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`
- `nanobot/channels/telegram/tests/`：仅当测试因改名/挂载变化失败时做机械修改，逐条列出。
- 不要修改 `scripts/evaluate_social_v5.py`（它本来就因调用已删除的 `store.wake` 而失效，评估脚本迁移时统一处理）。

## 不要做
- 不要把 hook 拆成多个类，不要改 hook 在 runner 中的调用时机。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（基线 983 passed）；`test_social_scenarios.py` 不得修改。
- `.venv/bin/ruff check nanobot/` 无错误；basedpyright 对 `nanobot/channels/telegram/social` 0 errors。
- 贴出 `grep -rn "_last_memory_body" nanobot scripts --include=*.py`（预期 0）与 `grep -n "selector\|memory.context\|activities\|running_activity" nanobot/channels/telegram/social/runtime.py`
  中 `_SocialExposureHook` 类范围内的结果（预期 0）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 修改过的测试（逐条）
4. 与简报的偏差及原因
5. 未解决的问题 / 建议
