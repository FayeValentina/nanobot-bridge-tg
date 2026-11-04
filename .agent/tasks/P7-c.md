# 任务 P7-c：LearningWorker——学习批次生命周期合并为一份；供应商错误不消耗重试次数【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P1-5；你的 P7-0 清单第 3 节（两个 run 的逐段对照、start_due/busy/peer、疑点 16、17）。
现在 `ExpressionLearner.run` 与 `JargonLearner.run` 的生命周期（取批次、scope/binding/busy/attempts 检查、异常分类、finally 失败记账）各写一份；
`JargonLearner` 继承 `ExpressionLearner` 却覆盖了 `run`；两者靠 `peer` 互查实现单批次互斥；`run_due` 里有一段按最早批次排序的内联逻辑。

**线上事故（2026-11-04 19:37–19:43）**：主模型供应商返回 HTTP 402（欠费）期间，表达学习批次 `54c6bbb2…` 两次尝试都失败（`invalid_or_changed_input: invalid extraction response`），重试次数耗尽，状态 failed。
`SocialLearning.prepare` 把 failed 视为活动批次，因此该 scope 的表达学习从此不会再建新批次。根因是供应商错误响应（`finish_reason == "error"`）被当作“模型输出不合法”，消耗了重试次数。

## 设计（已定，不得改变）

### 1. 新文件 `social/learning_worker.py`
```python
class LearningProviderError(Exception):
    """The provider returned an error response (billing, rate limit, outage). The attempt is refunded."""

class LearningWorker:
    """One in-flight learning batch at a time: selection, common checks, failure bookkeeping and logs."""
    def __init__(self, store: SocialStore, bindings: ScopeBindings,
                 processors: tuple[ExpressionLearner, JargonLearner], judge_busy: Callable[[], bool]) -> None
    main_busy: Callable[[], bool]          # 默认 lambda: False；wiring.connect_agent 设置
    task: asyncio.Task[None] | None
    def busy(self) -> bool                 # = main_busy() or judge_busy() or store.wakes.any_active()（与现在 ExpressionLearner.busy 相同）
    def cancel(self) -> None
    def start_due(self, scopes: list[str]) -> None
    async def _run(self, processor: ExpressionLearner | JargonLearner, batch: str) -> None
```
- `start_due`：合并现在 `runtime.run_due` 中的排序（`runtime.py:276–278`）与 `ExpressionLearner.start_due`：
  若 scopes 为空、学习未启用、`self.task` 未结束、或 `busy()` → 返回；全局 `last_batch_due` 未到 → 返回；
  按 `earliest_batch(kind, 1 + max_retries)` 对 processors 排序（并列时 expression 在前，与现在相同），对每个 provider 不为 None 的 processor，
  扫描 `pending_batches(kind)`，找到第一个 scope 在 scopes 内且 attempts < 1 + max_retries 的批次 → 创建 `_run` 任务并返回。
- `_run`：
  1. 取 row；row 为 None 或 processor.provider 为 None → 返回。
  2. scope 无效、binding 为 None、paused、`busy()`、attempts ≥ 1 + max_retries → 返回（两个 run 中现有的相同检查合并于此）。
  3. `await processor.process(row, scope)`。
  4. 异常分类与现在完全相同：CancelledError → `interrupted` 并重抛；TimeoutError → `timeout`；ValueError → `invalid_or_changed_input`；其他 → `provider_exception`；日志格式不变。
     **新增**：`LearningProviderError` → 调用 `store.learning.refund_attempt(batch)`，`logger.warning("Social learning batch={} kind={} provider error={}; attempt refunded", ...)`，不记失败。
  5. finally（与现在两份 finally 的公共部分相同）：status 为 running 且有 error → `fail_batch` + `count(scope, f"{kind}:failed", ...)`；status 为 succeeded → `clear_batch_error`。
- 用量与时延记账**不在 worker 里**：expression 的 `record_expression_usage`、jargon 的 `record_jargon_latency` 移进各自 `process` 内的 try/finally，保持现在的记账条件和时机（疑点 16：jargon 每次 ask 单独记 usage 的行为不变）。

### 2. `SocialLearning.refund_attempt(batch)`（`social/learning.py`）
`UPDATE learning_batches SET status='ready', attempts=attempts-1, updated=? WHERE id=? AND status='running'`。不改 due（全局 `start_*_call` 已把下次间隔推后）。

### 3. 处理器
- `ExpressionLearner`：删除 `run`、`start_due`、`busy`、`peer`、`task`、`main_busy`、`judge_busy`；新增 `async def process(self, row, scope) -> None`，内容为现有 run 中 checks 之后、finally 之前的业务段（generation、payload、180000 字符预算、`learning.start`、`start_expression_call`、provider 调用、解析、binding 复查、`library.commit`），
  外层 try/finally 记录 usage（条件与现在 finally 中相同）。provider 返回 `finish_reason == "error"` 时 `raise LearningProviderError(f"status={response.error_status_code}")`，**在** 现有 “invalid extraction response” 判断之前。
  构造改为 `ExpressionLearner(store, bindings)`。
- `JargonLearner`：不再继承 `ExpressionLearner`。保留 `kind`、`provider/model/configure_provider/set_provider`（与 ExpressionLearner 现有实现相同的代码，可各写一份，不要为此新建基类或 helper）、`jargon`、`tools`、prompts、`prepare_reviews`、`ask`、`read_public`；
  `run` 改为 `process(row, scope)`，内容为现有 run 的业务段，外层 try/finally 记录 latency（条件与现在相同）。`ask` 中 `finish_reason == "error"` 时 `raise LearningProviderError(...)`，在 “invalid jargon response” 判断之前（usage 记账仍在判断之前完成，与现在相同）。
  构造改为 `JargonLearner(store, bindings)`。注意它原先通过继承得到 `self.library = ExpressionLibrary(...)`；全仓库确认 jargon 是否用到 `self.library`，没用到就不要再创建。
- `ExpressionLearner.cancel`/`JargonLearner.cancel` 若只用于取消 task，则删除，由 worker.cancel 负责。

### 4. runtime 与 wiring
- runtime：`self.learning = LearningWorker(self.store, self.bindings, (self.expressions, self.jargon), lambda: self.judging.busy())`；删除 peer 设置；
  `run_due` 中 `workers = sorted(...)` 与循环改为 `self.learning.start_due(scopes)`；`close` 中取消改为 `self.learning.cancel()`；`get_state` 中引用保持语义。
- wiring `connect_agent`：`self.runtime.learning.main_busy = main_busy`；`jargon.tools` 不变。

## 要做的事
1. 按设计实现。
2. 测试：
   - 新增：(a) expression provider 返回 `finish_reason="error"` → 批次回到 ready、attempts 不变（相对开始前）、`expression:failed` 计数不增加；jargon 同理；
     (b) 两个 kind 都有就绪批次时同一时间只运行一个；(c) 排序：jargon 批次更早时先运行 jargon；(d) 现有 timeout/ValueError/其他异常的失败记账不变（若已有测试覆盖，指出位置即可）。
   - 现有测试机械适配（构造参数、`runtime.expressions.task` → `runtime.learning.task`、`start_due` 调用位置等），逐文件计数；语义性断言修改逐条列出。
3. **只报告**：线上卡住的批次 `54c6bbb2aea24b1b8a1acc8e96cdc17d`（expression，failed，attempts=2）——确认 `prepare` 会因为它永久不再为该 scope 创建表达批次；
   给出若要让它重新排队需要执行的 SQL（只写出，不执行），以及这样做对游标和来源的影响。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件、基类。
- 预期规模：生产代码净减少（两份生命周期合并为一份）。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（learning_worker.py（新建）、expressions.py、jargon_learning.py、learning.py、runtime.py、wiring.py）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`、`nanobot/channels/telegram/tests/evaluate_*.py`（仅机械适配，不运行）

## 不要做
- 不改批次准备（prepare/collect_due）、游标、证据、ExpressionLibrary/JargonLibrary 的业务规则、提示词。
- 不改 selection（P7-d）。
- 不执行任何对线上库的写操作。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway tests/cli/test_gateway_runtime.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净；basedpyright（social/）0 errors
- `grep -n "peer\|learning_task" nanobot/channels/telegram/social/*.py` 0 命中
- 报告 `wc -l`：expressions.py、jargon_learning.py、learning_worker.py、runtime.py 前后对比

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 卡住批次的分析与 SQL（只报告）
5. 未解决的问题 / 需要编排者决定的事项
