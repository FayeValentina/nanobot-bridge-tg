# 任务 P6-c：learner/selector 去反向引用；工具一个类一个（纯重构）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-1、P2-1、§4；P6-0 清单（`.agent/tasks/P6-0.report.md` 第 2、5 节）。
P6-a（`8d50aa77`）、P6-b（`8bd39b18`）已完成：waiting/media/delivery/hook 已拆出。本任务完成剩余的反向引用清理和工具层拆类。
**行为完全不变**：工具名、description、参数 schema、返回值、错误文案都不变。

## 设计（已定，不得改变）

### A. 学习器与选择器
1. `ExpressionLearner`（`social/expressions.py`）构造改为
   `ExpressionLearner(store: SocialStore, bindings: ScopeBindings, judge_busy: Callable[[], bool])`，删除 `self.runtime`。
   - 新增实例属性 `self.peer: ExpressionLearner | None = None`（另一个学习器；两者共享“同一时间只有一个学习批次在跑”的约束）。
   - `busy()` 中 `self.runtime.judging.tasks` → `self.judge_busy()`。
   - `start_due()` 中 `(self.runtime.learning_task is not None and not self.runtime.learning_task.done())` 改为
     `any(task is not None and not task.done() for task in (self.task, self.peer.task if self.peer else None))`；
     删除 `self.runtime.learning_task = self.task` 这一行。
   - `self.runtime.binding(scope)` → `self.bindings.binding(scope)`（`expressions.py:312,336`，`jargon_learning.py:158,194`）。
2. `JargonLearner`（`social/jargon_learning.py`）构造同父类签名，调用 `super().__init__(store, bindings, judge_busy)`。
3. `ContextSelector`（`social/selection.py`）构造改为 `ContextSelector(store: SocialStore, config: Callable[[], TelegramSocialConfig], now: Callable[[], float])`，删除 `self.runtime`：
   `self.runtime.store` → `self.store`；`self.runtime.config` → `self.config()`；`self.runtime.now()` → `self.now()`。`selection.py:204` 的局部别名 `store = self.runtime.store` 改为 `store = self.store`。
4. runtime（`social/runtime.py`）：
   - 删除 `self.learning_task`。
   - `self.expressions = ExpressionLearner(self.store, self.bindings, lambda: bool(self.judging.tasks))`；`self.jargon = JargonLearner(...同参数...)`；
     紧接着 `self.expressions.peer = self.jargon`、`self.jargon.peer = self.expressions`。
   - `self.selector = ContextSelector(self.store, lambda: self.config, self.now)`。
   - 新增两个与 `memory_action` 同形的方法（取代工具层对 store/library 的直接访问；**不**调用 notify，与当前工具行为一致）：
     ```python
     def activity_action(self, ctx: RequestContext, request: ActivityRequest) -> dict[str, object]:
         scope = self.bindings.validate_context(ctx)
         assert ctx.turn_id is not None
         return self.store.activities.apply(scope.session_key, ctx.turn_id, request)
     def expression_action(self, ctx: RequestContext, edit: ExpressionEdit) -> dict[str, object]:
         scope = self.bindings.validate_context(ctx)
         return self.expressions.library.change(scope.session_key, edit)
     ```
     返回类型以被调用方法的实际返回类型为准。
5. `SocialDelivery` 新增 `def followup_status(self, followup_id: str) -> dict[str, object]: return self.store.followups.describe(followup_id).model_dump()`。

### B. 工具一个类一个（`nanobot/agent/tools/telegram_social.py`，同一文件）
- 基类：
  ```python
  class _SocialTool(Tool):
      _plugin_discoverable = False
      tool_name: ClassVar[str]
      tool_description: ClassVar[str]
      def __init__(self, runtime: TelegramSocialRuntime) -> None
      name / description 属性返回 tool_name / tool_description
      parameters 属性：子类覆盖（返回与现在逐字相同的 dict）
      async def run(self, ctx: RequestContext, **kwargs: Any) -> object   # 子类实现
      async def execute(self, **kwargs: Any) -> str | list[dict[str, Any]]:
          # 与现在相同：取 current_request_context()，None 时返回原错误；
          # result = await self.run(ctx, **kwargs)；str 或 list 原样返回，其他 json.dumps(result, ensure_ascii=False)；
          # 捕获 (PermissionError, ValueError, RuntimeError) → ToolResult.error(str(exc))（保留 RuntimeError）
  ```
- 12 个子类，类名 = 工具名去下划线的驼峰 + `Tool`：`SocialActivityTool`、`SocialJargonTool`、`SocialExpressionTool`、`SocialGetStateTool`、`SocialReadMessagesTool`、
  `SocialMarkReadTool`、`SocialSendTool`、`SocialWaitTool`、`SocialSetWakeTool`、`SocialGetMediaTool`、`SocialStickerTool`、`SocialMemoryTool`。
  每个子类：`tool_name`、`tool_description`（现有字典中的原文）、`parameters`（现有分支原样）、`run`（现有 execute 分支原样）。
  - 原先直接访问 store/library 的三处改为：`runtime.activity_action`、`runtime.expression_action`、`runtime.delivery.followup_status`。
  - `validate_params` 与 `_send_shape_error` 只属于 `SocialSendTool`；`social_sticker` 的 action 二级分发保留在 `SocialStickerTool.run` 内。
- 模块末尾：`SOCIAL_TOOL_CLASSES: dict[str, type[_SocialTool]] = {cls.tool_name: cls for cls in (...12 个类...)}`。
- 删除 `TelegramSocialTool`。不新增其他 helper。
- gateway（`G:508–510`）改为 `for name in sorted(SOCIAL_TOOLS): tools.register(SOCIAL_TOOL_CLASSES[name](telegram_social))`；`SOCIAL_TOOLS` 保持在 runtime.py 不变。
- 新增一条测试（放进 `test_social_runtime.py` 中现有构造全部工具的测试附近）：`set(SOCIAL_TOOL_CLASSES) == SOCIAL_TOOLS`。

### 逐字一致性验证（必须做）
改动前，用一个临时脚本（放在 `/tmp`，不入库）把 12 个工具的 `name`、`description`、`parameters` 以 `json.dumps(..., sort_keys=False)` 导出到 `/tmp/p6c-tools-before.json`；
改动后同样导出 `/tmp/p6c-tools-after.json`；报告中贴出两者 `cmp` 的结果（必须相同）。

## 要做的事
1. 按设计修改 `expressions.py`、`jargon_learning.py`、`selection.py`、`runtime.py`、`delivery.py`、`telegram_social.py`、`gateway_runtime.py`（仅工具注册三行）。
2. 调用方机械改名：
   - 测试与 harness 中 `TelegramSocialTool(runtime, name)` → `SOCIAL_TOOL_CLASSES[name](runtime)`（P6-0 第 5 节列出 11 处）；导入相应调整。
   - `test_social_jargon.py:296,298` 中 `runtime.learning_task` → `runtime.expressions.task`（断言含义不变：第一个任务仍在跑、jargon 未启动）。
   - 其他测试若构造 `ExpressionLearner/JargonLearner/ContextSelector(runtime)`，按新签名传入 runtime 的对应属性。
   - 评估脚本（`nanobot/channels/telegram/tests/evaluate_*.py`、`scripts/evaluate_*.py`）同样机械改名，**不运行**。
   - **不要修改** `nanobot/channels/telegram/tests/fixtures/`。
3. 修改测试只允许机械改名，不得改断言；逐文件列出改名数量。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件、转发方法。
- 预期规模：telegram_social.py 行数变化在 ±40 行内；其余生产代码净变化不超过 +20 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（expressions.py、jargon_learning.py、selection.py、runtime.py、delivery.py）
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/cli/gateway_runtime.py`（仅工具注册）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`

## 不要做
- 不合并、不改名任何工具，不改 description/schema 文本，不把手写 schema 改成 pydantic 模型（Phase 8）。
- 不动 wiring、gateway 的其他部分、test_commands（P6-d）。
- 不改 learner 的批次逻辑与 selection 的 SQL（Phase 7）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过（场景文件不改）
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净
- `.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py` 0 errors
- `grep -rn "self.runtime" nanobot/channels/telegram/social/` 0 命中
- `grep -n "runtime.store\|\.library\." nanobot/agent/tools/telegram_social.py` 0 命中
- 工具导出 `cmp` 相同
- 报告 `wc -l`：runtime.py、telegram_social.py、expressions.py、selection.py

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
