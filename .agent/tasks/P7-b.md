# 任务 P7-b：judge 内部重写——内存合并窗口、单次调用、删除批次表（schema 22）【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md §5.1（含 owner 补充决定）；你的 P7-0 清单（`.agent/tasks/P7-0.report.md`，第 1 节与第 6 节疑点是本简报的依据）；
P7-a（`f6d0dd50`）已上线：`JudgeCoordinator.local_admission` 决定 B（本地 wake）/C（调用 judge）。
本任务把 judge 的持久化批次机制换成 §5.1 的简单实现。owner 已在群里确认 P7-a 后的行为正常；本任务对外可见的行为应与 P7-a 一致，只有下面“行为变化”一节列出的差异。

## 设计（已定，不得改变）

### 1. 新的 `social/judge.py`（取代 judge.py、judge_runtime.py、judge_models.py 三个文件）
文件内只包含以下内容（judge_runtime.py、judge_models.py 删除；其中仍需要的部分按下面的定义合并进来）：

```python
JUDGE_PROMPT_VERSION = "social-judge-v9"
BATCH_TTL_SECONDS = 120          # A 层：超过 120 秒（按 judge_facts.received_at）的消息不再进入窗口

@dataclass
class PendingBatch:
    first_at: float              # 窗口内第一条消息的 received_at
    last_at: float               # 最后一条的 received_at
    through_seq: int             # 窗口内最大 seq

class PendingWindows:
    """In-memory coalescing windows, one per scope. Persisted state is only judge_state.assessed_through."""
    def add(self, key: str, seq: int, at: float) -> None
    def due(self, key: str, delay: float, maximum: float) -> float          # min(last_at+delay, first_at+maximum)
    def shorten(self, key: str, quiet: float, total: float | None) -> None   # 取代 db judge.shorten_pending 的语义
    def take(self, key: str) -> PendingBatch | None                          # 取走并删除
    def discard(self, key: str) -> None
    def has(self, key: str) -> bool
    def any(self) -> bool
    def next_due(self, delay_for: Callable[[str], float], maximum: float) -> float | None
    def keys(self) -> list[str]
```
（`shorten` 的语义必须与现在 `db/judge.py::shorten_pending` 对 pending_due 的效果一致；如无法用三个字段表达，在 PendingBatch 中增加**一个** `due_cap: float | None` 字段，并在报告里说明。）

```python
class JudgeMessage(BaseModel)   # 字段：seq, message_id, sender_id, name, timestamp, text, is_self, is_bot, mentions_bot, reply(JudgeReply|None), media(tuple[str,...])
class JudgeReply(BaseModel)     # 沿用现有字段
class JudgeIntent(BaseModel)    # 字段：description, keywords, senders, on_question, on_messages, one_shot, expires_at
class JudgeInput(BaseModel)     # 字段：bot_username, bot_name, nicknames, intent(JudgeIntent|None), context(tuple[JudgeMessage,...]), new(tuple[JudgeMessage,...])
    def prompt_json(self) -> str
class JudgeVerdict(BaseModel)   # 字段：decision Literal["wake","observe"], fulfills_intent: bool = False, reason: str (max_length=120)；extra="forbid"

def make_judge_provider(...)    # 原样保留（wiring 使用）

class SocialJudge:
    """One intermediary call: no retry, no budget, no persistence. Failure means wake."""
    def __init__(self, settings: TelegramSocialJudgeConfig, provider: LLMProvider, provider_version: JudgeProviderSettings) -> None
    async def assess(self, data: JudgeInput) -> JudgeVerdict | None
        # 一次 provider.chat（tools=None、tool_choice="none"、temperature=0、settings.max_tokens、settings.reasoning_effort），
        # asyncio.timeout(settings.timeout_seconds)；超时/异常/非 stop/有 tool_calls/内容为空或 >4096/JSON 解析失败 → logger.warning(原因, 脱敏) 并返回 None。
        # 每次调用（含失败）都用 nanobot.llm_usage.record_llm_call 记录一条 LLMCallRecord，source="system"；
        # 字段的填法与 gateway 观察器一致（参照 nanobot/providers/base.py:1262–1295 中构造 LLMCallRecord 的方式）。

class JudgeCoordinator:
    """Admission: A host guards (in observe), B platform facts wake directly, C asks the judge. Unsure means wake."""
    def __init__(self, store, bindings, waiting, windows: PendingWindows, notify, config, now, provider=None, provider_version="") -> None
    def configure(self) -> None                 # 根据配置重建 SocialJudge（provider 为 None 时按 make_judge_provider 构造，与现在相同）
    def set_provider(self, provider, version) -> None
    def cancel(self) -> None
    def coalesce_delay(self, scope) -> float    # 原样保留
    def note(self, scope: ConversationScope, seq: int, received_at: float) -> None   # windows.add + notify
    def recover(self) -> None                   # 每个 scope：assessed_through 之后、eligible、received_at+BATCH_TTL_SECONDS>now 的消息重新 add 进窗口
    def next_due(self) -> float | None
    def busy(self) -> bool                      # 有在途任务或 windows.any()（供 learner busy 使用）
    async def start_due(self) -> None           # 对到期且无在途任务的 scope 创建任务 _run
    def local_admission(self, data: JudgeInput, key: str) -> str | None   # P7-a 的规则原样保留，输入改为 JudgeInput
    def intent(self, key: str) -> JudgeIntent | None
    async def _run(self, scope, batch: PendingBatch) -> None
```
`_run` 的步骤（按顺序）：
1. 读 `assessed_through` 之后到 `batch.through_seq` 的消息；丢掉 not eligible 或 received_at 超过 BATCH_TTL_SECONDS 的。没有剩余新消息 → 推进水位到 `batch.through_seq`，结束。
2. 构造 `JudgeInput`：`new` 为剩余新消息；`context` 为它们之前的最近 `settings.context_messages - len(new)` 条（下限 0）；不发图片、不发 followups/persona/background/participation/opportunity。
3. `reason = local_admission(...)`；为 None 时 `verdict = await judge.assess(data)`；verdict 为 None（失败）→ 视为 `wake, fulfills_intent=False, reason="judge unavailable"`。
4. 回到事务：若 scope 已暂停或 binding 为 None → 只推进水位，丢弃结果。否则推进水位到 through；decision=wake 时调用 `store.wakes.admit_batch(...)`（见第 3 节）。
5. 若窗口在运行期间又收到更新的消息，`windows` 中会有新的 PendingBatch，照常等待下次到期。

`intent(key)`：由 `waiting.conditions(key)` 与 dialogue_wait 生成；description 保持现有宿主生成文本的含义（P7-0 第 1.6 节表格），字段如上；只有存在 keywords/senders/on_question 时返回非 None（与 local_admission 的选择性等待判定一致）。

### 2. 新提示词
`nanobot/templates/telegram/social_judge.md` 的内容**整体替换**为 `.agent/tasks/P7-b.social_judge.md` 的原文（逐字）。`system_prompt` 仍在末尾追加 `Runtime target bot identity: {...}`。

### 3. 交接移到 `db/wakes.py`
```python
def admit_batch(self, key: str, new_seqs: Sequence[int], through_seq: int, *, admission: dict[str, str],
                fulfills_intent: bool, one_shot: bool, condition_config: str, condition_expires: float | None,
                expires: float, purpose: str, quiet_seconds: float, quiet_due: float, total_due: float, after_seq: int) -> None:
    """Hand an admitted batch to the wake state machine; reuse a turn that already showed it."""
```
- 复用规则与现在 `Judge._handoff` 完全相同：扫描 `recent_turns(key)`（completed 或未过期 running），若**整批 new_seqs** 都在该 turn 的 `views.shown_sequences` 中 → 不新建 wake；
  若 fulfills_intent 且 one_shot 且 condition_config 等于当前 wake_config → 清除 wake_config。返回。
- 否则 `schedule(key, through_seq, WakeReason.JUDGE, 0)`（reason 枚举**不变**，directed/conversation/judge 的区别只记在 admission 里），
  取得 scheduled wake，`attach_judgement(wake_id, expires, condition_config if fulfills_intent else "{}", None)`，写 `wakes.admission = json.dumps(admission)`，
  并 `add_dialogue_job(...)`（参数含义同现在 `_handoff`）。
- `expires` = 批次 first_at + BATCH_TTL_SECONDS；fulfills_intent 且有 condition_expires 时取二者较小（与现在相同）。
- `admission` = `{"by": "directed"|"conversation"|"judge", "reason": <文本>}`。
- `_check_trigger_source` 删除读取 judge_batches 的整段（§5.1：不做调用后复核）；保留 condition_config 检查。

### 4. 快照字段
`SocialStore.view` 中 `"judgement": [...]` 改为 `"admission": json.loads(wake["admission"]) if job_id and wake["admission"] else None`。
`bindings.py` SOCIAL_PROTOCOL 第 16 行附近 “Its judgement gives …” 一句与第 138 行附近 “matching judgement” 的措辞改为指向 `admission`，其余协议文本不动；报告中贴出改前改后原文。

### 5. 状态与调用点
- `db/judge.py` 改名为 `db/judge_state.py`，只剩 `JudgeState`：`through(key)`、`set_through(key, seq)`、`reset_binding(key)`（版本变化时水位设为 next_seq-1，与现在相同）。
- 删除所有对 judge_batches、judge_attempts、judge_state.pending_*/batch_id/inflight/opportunity 的读写。P7-0 第 1.2、1.10 节列出的每一个读取方都要改到新位置，
  **在报告中逐条列出“原调用 → 新调用”**，至少包括：`store.observe` 中 `judge.queue`（改为 runtime.observe 在 `store.observe` 返回新消息且 eligible 时调用 `judging.note`）、
  `store.wait_for_quiet` 中 `shorten_pending`（改为 `windows.shorten`，由持有 windows 的一方调用；`SocialWaiting` 构造时接收 `windows`）、
  `store.pause`/`store.bind` 中的 `judge.pause`/`reset_binding`（DB 部分留在 JudgeState；窗口用 `windows.discard`，由 runtime.sync_scope 在暂停或版本变化时调用）、
  `wakes.next_due` 的 judge_state 子查询（删除；scheduler 另取 `judging.next_due()`，`SocialScheduler` 构造增加 `extra_due: Callable[[], float | None]`）、
  learner `busy()` 中的 `judge.any_pending()`（改为 runtime 传入的 `judge_busy` 回调调用 `judging.busy()`）、`proactive.py:133,168` 的 `store.judge.pending(key)`（改为 `windows.has(key)`，`SocialProactive` 构造增加 `windows`）、
  `get_state`、`latest_for_job`、评估脚本。
- `PendingWindows` 由 runtime 构造一次，传给 JudgeCoordinator、SocialWaiting、SocialProactive。
- `runtime.recover` 中 judge 恢复改为 `judging.recover()`。

### 6. 配置（`TelegramSocialJudgeConfig`）
只保留：`model`、`provider`、`reasoning_effort`、`max_tokens`、`timeout_seconds`（默认改为 8）、`context_messages`（默认改为 12）、`oauth_storage_dir`。
删除其余字段（max_retries、retry_delay_seconds、min_interval_seconds、max_requests_per_hour、max_fallbacks_per_hour、context_chars、media_*、batch_ttl_seconds）。线上实例配置中 judge 段为 null，不受影响。

### 7. schema 22（`db/schema.py`）
- `SCHEMA_VERSION = 22`。升级函数（21→22）在一个事务中：
  1. 把 `judge_batches` 与 `judge_attempts` 全部行导出为 JSON（`{"judge_batches": [...], "judge_attempts": [...]}`，每行是列名→值的字典）写到 `<social 根目录>/backups/judge-history-schema21.json`（目录不存在则创建，权限 0o700）；
     导出在 DROP 之前完成。若该文件已存在，覆盖写入。
  2. `DROP TABLE judge_attempts`、`DROP TABLE judge_batches`。
  3. 重建 `judge_state` 为 `(scope TEXT PRIMARY KEY, assessed_through INTEGER NOT NULL DEFAULT 0)`，保留每个 scope 原有的 assessed_through。
  4. `ALTER TABLE wakes ADD COLUMN admission TEXT`。
- 新库直接按 schema 22 建表。升级函数需要知道 social 根目录：按 schema.py 现有升级函数获取路径的方式；若现有接口拿不到路径，停下并在报告中说明，不要自行引入全局变量。

### 行为变化（本任务唯一允许的）
- judge 调用失败（超时/异常/格式错误）不再重试、不再走 fallback 预算，直接按 wake 交接。
- 不再向 judge 发送图片、followups、persona/background；judge 只看新消息与最近 12 条上下文。
- judge 超时 15→8 秒。
- 交接后不再复核消息是否被编辑或变为 ineligible（`_check_trigger_source` 的 judge_batches 段删除）。
- 合并窗口在进程重启时按 received_at 120 秒内重建（原来恢复依赖持久化的 pending_due）。

## 要做的事
1. 按设计实现；删除 `judge_runtime.py`、`judge_models.py`。
2. 测试：
   - `test_social_judge.py`、`test_social_judge_runtime.py` 中针对已删除机制的测试（批次表、attempts、重试、fallback 预算、冻结输入、图片输入、provider 变更取消等）**删除**；在报告中按文件列出删除的测试名。
   - 新增（放进 `test_social_judge_runtime.py`）：(a) PendingWindows 的 add/due/shorten/take；(b) C 层 judge 返回 observe → 不 wake，水位推进；(c) judge 抛异常/超时 → wake 且 admission.reason="judge unavailable"；
     (d) 重启：水位后 120 秒内的 eligible 消息重新进入窗口并被处理，超过 120 秒的不处理；(e) 快照含 `admission`，`by` 分别为 directed/conversation/judge；(f) judge 调用产生一条 llm_usage 记录（source=system）。
   - schema 迁移测试：21→22 后 judge_batches/judge_attempts 不存在、导出文件包含原行数、judge_state 只剩两列且水位不变、wakes 有 admission 列。
   - 其余测试机械适配（改调用位置、构造参数），逐文件计数；语义性断言修改逐条列出（文件:行号、原、新、理由）。
   - 场景测试 `test_social_scenarios.py` 与 harness 允许适配，逐条列出。
3. **线上库副本演练**：把 `/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/social.sqlite3` 用 sqlite backup API 复制到 `/tmp/p7b/social.sqlite3`（只读打开源库，`?mode=ro`），
   在副本目录上运行迁移，报告：迁移前 judge_batches/judge_attempts 行数、导出文件中的行数、迁移后各 scope 的 assessed_through 与迁移前是否一致、其余表行数前后对比、`PRAGMA quick_check`、`PRAGMA foreign_key_check`。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件（除简报列出的 judge.py 中的类、`db/judge_state.py`）。
- 预期规模：judge.py 约 300 行以内；judge 相关生产代码（judge.py+judge_runtime.py+judge_models.py+db/judge.py）合计从约 1000 行降到 400 行以内；全部生产代码净减少。

## 允许改动的路径
- `nanobot/channels/telegram/social/`
- `nanobot/templates/telegram/social_judge.md`
- `nanobot/config/schema.py`（仅 TelegramSocialJudgeConfig）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`、`nanobot/channels/telegram/tests/evaluate_*.py`（机械适配或删除对已删除机制的引用；不运行）
- `/tmp/p7b/`（演练）

## 不要做
- 不改 directed 识别、B/C 规则（P7-a）、wake 状态机的其他部分、发送路径、learner、selector。
- 不打开线上原库写入；不部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway tests/config -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净；basedpyright（social/）0 errors
- `grep -rn "judge_batches\|judge_attempts\|pending_due\|judge_runtime\|judge_models" nanobot scripts --include=*.py` 只允许出现在 schema.py 的 21→22 升级函数中
- 报告 `wc -l`：judge.py、db/judge_state.py、db/wakes.py、store.py、runtime.py 前后对比

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因（含调用点映射表、删除的测试清单、语义性断言修改清单）
4. 线上库副本演练结果
5. 未解决的问题 / 需要编排者决定的事项
