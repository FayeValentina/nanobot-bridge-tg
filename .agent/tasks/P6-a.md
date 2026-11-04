# 任务 P6-a：拆出 waiting / media，judge 与 stickers 不再持有 runtime（纯重构）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-1、§4。你在 P6-0（`.agent/tasks/P6-0.report.md`）列出的事实是本简报的依据。
Phase 6 拆为：**P6-a（本任务）** waiting/media 拆出 + judge/stickers 去反向引用；P6-b delivery/hook 拆出；
P6-c learner/selector 去反向引用 + 工具一类一个；P6-d wiring.py + gateway + test_commands。
本任务**行为完全不变**：只移动代码、改依赖注入方式、改调用方名字。

## 设计（已定，不得改变）

### 通用约定
- 需要热更新配置的组件接收 `config: Callable[[], TelegramSocialConfig]`（runtime 传 `lambda: self.config`），每次使用时调用 `self.config()`；不保存配置对象。
- 需要时钟的组件接收 `now: Callable[[], float]`（runtime 传 `self.now`）。
- 需要唤醒调度器的组件接收 `notify: Callable[[], None]`（runtime 传 `self.scheduler.notify`）。
- 只用于类型注解的导入若造成循环导入，放进 `if TYPE_CHECKING:`（与 judge_runtime.py 现有写法一致）。
- 新组件都是 runtime 的公开属性；**runtime 不保留被移走方法的转发方法**（下表“runtime 中删除”一栏全部删除），所有调用方（生产、工具、channel、测试、评估脚本）改为调用新位置。

### 1. `ScopeBindings.validate_context`（`social/bindings.py`）
- 把 `TelegramSocialRuntime.validate_context`（`runtime.py:297–313`）原样移为 `ScopeBindings.validate_context(self, ctx, *, require_claim=True) -> ConversationScope`。
  其中 `self.binding(scope)`、`self.store.scopes`、`self.store.wakes` 改为 bindings 自己的 `self.binding` 和 `self.store`（bindings 已持有 store）。
- runtime 中删除 `validate_context`；`runtime.binding()` **保留**（facade 入口，本任务不动）。

### 2. `SocialMedia`（`social/media.py`，与 `SocialMediaCache` 同文件）
```python
class SocialMedia:
    """Image download, preparation and the per-turn image budget shared by main, judge and sticker views."""
    def __init__(self, cache: SocialMediaCache, store: SocialStore, bindings: ScopeBindings,
                 config: Callable[[], TelegramSocialConfig]) -> None: ...
    lock: asyncio.Lock                 # 原 runtime._media_lock
    loader: SocialMediaLoader | None   # 原 runtime._media_loader
    async def get(self, ctx: RequestContext, message_id: int) -> list[dict[str, Any]]      # 原 runtime.get_media
    async def prepare(self, scope, ref, label, *, validate, max_edge=None) -> list[dict[str, Any]]  # 原 runtime.prepare_media
    def save_preview(self, scope: ConversationScope, raw: bytes) -> str
        # 原 stickers.py:126–131 中构造 preview MediaReference、调用 cache._save_pair、取 cache._path(..., ".jpg") 的部分；返回路径字符串
```
- `SocialMediaLoader` 类型别名移到 media.py；runtime.py 从 media 导入（`set_media_services` 仍用）。
- runtime 中删除：`get_media`、`prepare_media`、`_media_lock`、`_media_loader`；`self.media` 改为 `SocialMedia` 实例（cache 作为其 `cache` 属性）。
  其他读 `runtime.media`（cache）的地方改为 `runtime.media.cache`。

### 3. `SocialWaiting`（新文件 `social/waiting.py`）
```python
class SocialWaiting:
    """wait / sleep / set_wake semantics and model-configured wake conditions."""
    def __init__(self, store: SocialStore, bindings: ScopeBindings, proactive: SocialProactive,
                 notify: Callable[[], None], config: Callable[[], TelegramSocialConfig],
                 now: Callable[[], float]) -> None: ...
    def conditions(self, key: str) -> tuple[TelegramSocialWakeConfig, bool]          # 原 _wake_conditions
    def effective(self, key: str, binding: TelegramSocialScopeConfig) -> tuple[...]  # 原 _effective_wake
    def consume(self, key: str, job: str) -> None                                    # 原 consume_wake_conditions
    def admit_request(self, ctx: RequestContext) -> None                             # 原 admit_request
    def wait(self, ctx, seconds=None, *, ...) -> dict[str, Any]                      # 原 wait，签名不变
    def set_wake(self, ctx, *, ...) -> dict[str, Any]                                # 原 set_wake，签名不变
    def keep_reply_intent(self, scope: ConversationScope, turn: str) -> None         # 原 keep_reply_intent
```
- 方法体原样移动，只把 `self.validate_context` → `self.bindings.validate_context`、`self.binding` → `self.bindings.binding`、
  `self.config.` → `self.config().`、`self.scheduler.notify()` → `self.notify()`。
- runtime 中删除以上 7 个方法；runtime 内部调用方（`run_due`、`get_state`、`_confirmed_send`、hook 等）改为 `self.waiting.xxx`。
- gateway `G:538` 注册的 request admission 改为 `telegram_social.waiting.admit_request`。

### 4. `SocialStickers`（`social/stickers.py`）
- 构造改为 `SocialStickers(store: SocialStore, bindings: ScopeBindings, media: SocialMedia, config: Callable[[], TelegramSocialConfig])`，删除 `self.runtime`。
- 新增公开属性 `loader: SocialStickerSetLoader | None = None`（原 `runtime._sticker_set_loader`）；`SocialStickerSetLoader` 别名移到 stickers.py。
- 把 runtime 的 `collect_sticker` 移为 `SocialStickers.collect(ctx, message_id, note)`，`list_stickers` 移为 `SocialStickers.search(ctx, query)`，方法体原样。
- `view` 中 `runtime._media_lock` → `self.media.lock`，`runtime.prepare_media` → `self.media.prepare`，preview 保存改用 `self.media.save_preview`。
- `send_sticker` **留在 runtime**（P6-b 移到 delivery），改为通过 `self.store.stickers.get` 取引用（与现在相同）。
- 删除文件顶部的 `# pyright: reportPrivateUsage=false` 及其说明注释。

### 5. `JudgeCoordinator`（`social/judge_runtime.py`）
- 构造改为
  `JudgeCoordinator(store, bindings, waiting, media, notify, config, now, provider=None, provider_version="")`，删除 `self.runtime` 及 TYPE_CHECKING 导入 runtime。
- 替换：`runtime.binding` → `self.bindings.binding`；`runtime.bindings.resolve` → `self.bindings.resolve`；`runtime._wake_conditions` → `self.waiting.conditions`；
  `runtime._media_lock` → `self.media.lock`；`runtime.prepare_media` → `self.media.prepare`；`runtime.config` → `self.config()`；`runtime.now` → `self.now`；`runtime.scheduler.notify` → `self.notify`。
- 删除无调用者的 `enqueue`（`judge_runtime.py:83–89`，P6-0 已全仓确认）。
- 删除文件顶部的 `# pyright: reportPrivateUsage=false`。

### 6. 其他
- `social/jargon.py:2–3` 已过时的注释和 `# pyright: reportPrivateUsage=false` 删除（P6-0 确认无私有访问）。删除后 basedpyright 若报错，停下并在报告中列出，不要改代码绕过。
- `set_media_services` 保留在 runtime，内部改为设置 `self.media.loader`、`self._sticker_sender`、`self.stickers.loader`。
- runtime `__init__` 的构造顺序：store → bindings → memory → proactive → now → scheduler → media → waiting → stickers → judging → 其余不变。

## 要做的事
1. 按设计修改 `runtime.py`、`bindings.py`、`media.py`、新建 `waiting.py`、`stickers.py`、`judge_runtime.py`、`jargon.py`。
2. 调用方改名（机械）：
   - `nanobot/agent/tools/telegram_social.py`：get_media/collect/list/wait/set_wake/validate_context 等改到新位置。
   - `nanobot/cli/gateway_runtime.py`：只改 admission 注册一处。
   - `nanobot/channels/telegram/runtime.py`：若引用了被移动的方法，改到新位置。
   - 测试（`nanobot/channels/telegram/tests/` 下 test_*.py 与 `social_harness.py`）：按 P6-0 第 6 节的引用表机械改名；
     私有字段访问 `runtime._sticker_set_loader` → `runtime.stickers.loader`。
   - 评估脚本（`nanobot/channels/telegram/tests/evaluate_*.py`、`scripts/evaluate_*.py`）：同样机械改名，**不运行**它们。
   - **不要修改** `nanobot/channels/telegram/tests/fixtures/` 下的任何文件（冻结基线）。
3. 修改测试只允许机械改名（方法位置、构造参数），不得改断言；逐文件列出改名数量。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件、转发方法。
- 预期规模：生产代码净增不超过 40 行（新类的构造与签名开销）；runtime.py 应减少约 200 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（runtime.py、bindings.py、media.py、waiting.py（新建）、stickers.py、judge_runtime.py、jargon.py）
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/cli/gateway_runtime.py`（仅 admission 一处）
- `nanobot/channels/telegram/runtime.py`（仅改调用位置）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`

## 不要做
- 不拆 delivery、hook、learner、selector、工具类、wiring（后续任务）。
- 不改任何方法体的逻辑、文案、异常类型、检查顺序。
- 不改 P6-0 第 7 节列出的疑点（recover 标记、set_wake 通知、close 不等待等）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过（场景文件不改）
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净
- `.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py` 0 errors
- `grep -rn "self.runtime\|runtime\._" nanobot/channels/telegram/social/judge_runtime.py nanobot/channels/telegram/social/stickers.py` 0 命中
- `grep -rn "reportPrivateUsage" nanobot/channels/telegram/social` 0 命中
- 报告 `wc -l` 前后对比：runtime.py、waiting.py、media.py、stickers.py、judge_runtime.py、bindings.py

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
