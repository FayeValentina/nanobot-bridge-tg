# 任务 P6-b：拆出 delivery.py 与 hook.py（纯重构）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-1、§4；P6-0 清单（`.agent/tasks/P6-0.report.md`）；P6-a 已完成（`8d50aa77`：waiting/media 拆出、validate_context 在 ScopeBindings）。
本任务把发送路径和 hook 从 `social/runtime.py`（当前 727 行）移出。**行为完全不变**：只移动代码、改依赖注入、改调用方名字。

## 设计（已定，不得改变）

### 通用约定（与 P6-a 相同）
- 热配置用 `config: Callable[[], TelegramSocialConfig]`，时钟 `now: Callable[[], float]`，调度通知 `notify: Callable[[], None]`。
- runtime **不保留**被移走方法的转发；所有调用方（生产、工具、Telegram channel、测试、评估脚本）改为新位置。
- 只用于类型注解的导入若造成循环，放进 `if TYPE_CHECKING:`。

### 1. `context.py`：快照文本
- 把 `encode_view`（`runtime.py:70–71`）移到 `social/context.py`。runtime.py 改为从 context 导入它（runtime 内部仍使用；测试和脚本 `from ...runtime import encode_view` 因此照常可用，**不需要改这些导入**）。
- 在 context.py 新增模块函数（取代 `runtime.read_messages_for`）：
  ```python
  def snapshot_text(store: SocialStore, config: TelegramSocialConfig, scope: ConversationScope, turn: str | None) -> str:
      return encode_view(store.view(scope.session_key, count=config.snapshot_messages, chars=config.snapshot_chars, turn_id=turn))
  ```
- runtime 删除 `read_messages_for`；`runtime.read_messages(ctx)` 保留（工具入口，本任务不动），内部改为调用 `snapshot_text(self.store, self.config, scope, ctx.turn_id)`。

### 2. `SocialDelivery`（新文件 `social/delivery.py`）
```python
SocialSender = Callable[[ConversationScope, str, int | None], Awaitable[int]]   # 从 runtime 移来
NEW_MESSAGES_NOTICE = ...                                                        # 从 runtime 移来，原文不变

class SocialDelivery:
    """Every public send: permission check, journal, Telegram delivery, retries and confirmed-send effects."""
    def __init__(self, store: SocialStore, bindings: ScopeBindings, proactive: SocialProactive,
                 waiting: SocialWaiting, selector: ContextSelector, notify: Callable[[], None],
                 config: Callable[[], TelegramSocialConfig], now: Callable[[], float]) -> None: ...
    sender: SocialSender | None            # 原 runtime._sender
    sticker_sender: SocialSender | None    # 原 runtime._sticker_sender
```
原样移入以下 runtime 方法（名字变化见括号；私有的保持私有）：
`send_lock`、`_check_send_allowed`、`_collect_new_messages`、`begin_message`、`send`、`send_sticker`、`_public_text`、`_action_result`、`send_parts`、
`_deliver`、`_confirmed_send`、`_retry_sends`（改名为公开的 `retry_due`）、`finish_message`、`followup_note`；以及实例状态 `_send_locks`、`_send_rechecked`。
- 方法体内替换：`self.binding` → `self.bindings.binding`；`self.config.` → `self.config().`；`self.scheduler.notify()` → `self.notify()`；
  `self.read_messages_for(scope, turn)` → `snapshot_text(self.store, self.config(), scope, turn)`；`self._sender`/`self._sticker_sender` → `self.sender`/`self.sticker_sender`。
- runtime：`run_due` 中 `await self._retry_sends()` → `await self.delivery.retry_due()`；`set_sender` 改为设置 `self.delivery.sender`；
  `set_media_services` 中 sticker sender 改为设置 `self.delivery.sticker_sender`。`set_sender`、`set_media_services` 留在 runtime（channel 接入点）。
- runtime `__init__`：在 `self.selector` 构造之后构造 `self.delivery`。

### 3. `SocialExposureHook`（新文件 `social/hook.py`）
- 把 `_SocialExposureHook`（`runtime.py:612–727`）原样移到 hook.py，类名改为 `SocialExposureHook`（公开，因为它跨模块使用）。
- 构造改为
  `SocialExposureHook(store: SocialStore, bindings: ScopeBindings, context_builder: SocialContextBuilder, waiting: SocialWaiting, notify: Callable[[], None], config: Callable[[], TelegramSocialConfig])`，删除 `self.runtime`。
- 替换：`self.runtime.store` → `self.store`；`self.runtime.binding` → `self.bindings.binding`；`self.runtime.context_builder` → `self.context_builder`；
  `self.runtime.waiting` → `self.waiting`；`self.runtime.config.repair_unsent_final` → `self.config().repair_unsent_final`；`self.runtime.scheduler.notify()` → `self.notify()`；
  `self.runtime.is_activity_turn(ctx)` → `self.store.wakes.is_activity_turn(ctx.session_key, ctx.turn_id)`。
- runtime 删除 `is_activity_turn`（唯一调用者是 hook）。`hook_factory` 留在 runtime，返回
  `SocialExposureHook(self.store, self.bindings, self.context_builder, self.waiting, self.scheduler.notify, lambda: self.config)`。

## 要做的事
1. 按设计修改 `runtime.py`、`context.py`，新建 `delivery.py`、`hook.py`。
2. 调用方改名（机械）：
   - `nanobot/channels/telegram/runtime.py`：`self.social.send_lock/begin_message/finish_message/followup_note` → `self.social.delivery.xxx`（`TG:1109,1137,1184,1192` 附近）。
   - `nanobot/agent/tools/telegram_social.py`：`send`、`send_parts`、`send_sticker` → `self.runtime.delivery.xxx`。
   - 测试（`nanobot/channels/telegram/tests/` 下 test_*.py 与 `social_harness.py`）：`runtime.send/send_parts/send_sticker/begin_message/finish_message` → `runtime.delivery.xxx`；
     私有字段 `runtime._sender`/`runtime._sticker_sender` → `runtime.delivery.sender`/`runtime.delivery.sticker_sender`；
     `_SocialExposureHook` → 从 `social.hook` 导入 `SocialExposureHook`，构造参数按新签名传入 runtime 的对应属性。
     harness 中 `store.reserve_send` 等直接仓储调用不动。
   - 评估脚本（`nanobot/channels/telegram/tests/evaluate_*.py`、`scripts/evaluate_*.py`）：同样机械改名（`_retry_sends` → `delivery.retry_due`、`_sticker_sender` 等），**不运行**它们。
     `scripts/evaluate_social_v5.py` 中 `ReplayHook` 继承的基类改为 `SocialExposureHook`，并按新构造签名调用 `super().__init__`；
     它用 `vars(runtime_module)` 编译冻结 hook 代码的部分**不要改**，若因此无法静态对应，写进报告。
   - **不要修改** `nanobot/channels/telegram/tests/fixtures/` 下任何文件。
3. 修改测试只允许机械改名（方法位置、构造参数），不得改断言；逐文件列出改名数量。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件、转发方法。
- 预期规模：生产代码净增不超过 40 行；runtime.py 应降到约 370 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（runtime.py、context.py、delivery.py（新建）、hook.py（新建））
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/channels/telegram/runtime.py`（仅改调用位置）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`

## 不要做
- 不动 learner、selector、工具类结构、gateway、wiring（后续任务）。
- 不改任何方法体的逻辑、文案、异常类型、检查顺序。
- 不处理 P6-0 第 7 节疑点（例如 `_confirmed_send` 的未使用参数 `text`/`final`，保持原样）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过（场景文件不改）
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净
- `.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py nanobot/channels/telegram/runtime.py` 0 errors（telegram/runtime.py 若有既有错误，贴出改动前后对比证明没有新增）
- `grep -n "self.runtime" nanobot/channels/telegram/social/hook.py nanobot/channels/telegram/social/delivery.py` 0 命中
- 报告 `wc -l` 前后对比：social/runtime.py、delivery.py、hook.py、context.py

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
