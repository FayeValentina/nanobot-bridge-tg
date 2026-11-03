# 任务 P3-b：`db/wakes.py`：显式的 wake 状态机

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-3：wake/等待状态机是隐式的，`wakes.status` 和 `scopes.mode` 的转换散落在
store.py、runtime.py、proactive.py 等文件里。P3-a（已提交 `cf1a492e`）建立了 `social/db/` 包和 Scopes/Messages/Views/StickerLibrary 仓储。
本任务把 wake 状态机集中到一个仓储 `db/wakes.py`，让每个状态转换都有唯一的入口和明确的前置状态。
事实来源：`.agent/tasks/P3-0.report.md` 第 1–3 节（状态转换表、写入点、疑点 Q1–Q15）。行号是 P3-a 之前的，以当前代码为准。

本任务**以搬迁为主**，SQL 和判断逻辑原样保留；唯一允许的行为变化是下面“行为修正”一节列出的四项。

## 设计（已定，不得改变）

### 1. `db/connection.py` 新增 `transaction`
```python
@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    """Join the caller's transaction, or open one and commit/rollback it."""
    if db.in_transaction:
        yield
        return
    db.execute("BEGIN")
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    db.commit()
```
`db/wakes.py` 和 `store.py` 中所有 `with self.db:` 一律改为 `with transaction(self.db):`，位置不变。
其他模块（proactive/learning/memory/...）里的 `with self.db:` **不动**。

### 2. `db/wakes.py` 的归属
`Wakes` 拥有以下表/列的**全部** SQL（读和写）：
- `wakes`、`requests`、`dialogue_waits`、`dialogue_jobs`、`dialogue_state` 五张表；
- `scopes` 表的调度列：`mode`、`wake_at`、`wake_on_timeout`、`wait_after_seq`、`wait_infinite`、`wake_config`、`timeout_through`。

wakes.py 中的 SQL 可以在**只读的子查询或 JOIN** 中引用其他表（例如 `recovery` 中 `EXISTS (SELECT 1 FROM actions ...)`、`next_due` 的 UNION、
`_check_trigger_source` 读 `judge_batches`），但**不得写**其他表。读 `judge_batches` 的那条查询暂留 wakes.py，P3-c 移到 `db/judge.py`。

### 3. 枚举与转换表（放在 wakes.py 顶部，原样采用）
```python
class WakeStatus(StrEnum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNSENT = "unsent"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

ACTIVE = (WakeStatus.SCHEDULED, WakeStatus.QUEUED, WakeStatus.RUNNING)

# Every status write goes through Wakes._move; terminal states have no exits.
TRANSITIONS: dict[WakeStatus, frozenset[WakeStatus]] = {
    WakeStatus.SCHEDULED: frozenset({WakeStatus.QUEUED, WakeStatus.RUNNING, WakeStatus.CANCELLED, WakeStatus.EXPIRED}),
    WakeStatus.QUEUED: frozenset({WakeStatus.RUNNING, WakeStatus.SCHEDULED, WakeStatus.CANCELLED, WakeStatus.EXPIRED}),
    WakeStatus.RUNNING: frozenset({WakeStatus.COMPLETED, WakeStatus.FAILED, WakeStatus.UNSENT, WakeStatus.SCHEDULED,
                                   WakeStatus.CANCELLED, WakeStatus.EXPIRED}),
}

class WakeReason(StrEnum):
    JUDGE = "judge"
    PROACTIVE = "proactive"
    WAIT_REPLY = "wait_reply"
    WAIT_MESSAGES = "wait_messages"
    TIMEOUT = "timeout"

RANK = {WakeReason.JUDGE: 4, WakeReason.WAIT_REPLY: 1, WakeReason.WAIT_MESSAGES: 1,
        WakeReason.TIMEOUT: 0, WakeReason.PROACTIVE: -1}

class ScopeMode(StrEnum):
    OBSERVING = "observing"
    ACTIVE = "active"
    WAITING = "waiting"
    SLEEPING = "sleeping"
    PAUSED = "paused"
```
`RANK` 取代 `_schedule` 里的局部 `rank` 字典（原字典只含这五个值，见 P0-3 报告 reason 表）。

### 4. 唯一的 status 写入口
```python
def _move(self, to: WakeStatus, where: str, params: Sequence[object], **columns: object) -> int:
    """UPDATE wakes SET status=<to>[, <columns>] WHERE (<where>) AND status IN <every state that may reach `to`>.
    Returns rowcount."""
```
- 允许的源状态由 `TRANSITIONS` 反查得到（`{s for s, targets in TRANSITIONS.items() if to in targets}`）。
- wakes.py 中**每一条**改 `status` 的 UPDATE 都必须改写为 `_move(...)` 调用；原 WHERE 条件原样作为 `where`
  （原条件里已有的 `status IN (...)`/`status='...'` 可以保留，它与 `_move` 追加的条件取交集，结果相同）。
- 插入新行只在 `_insert_scheduled(...)` 一个私有方法里（`status` 固定为 scheduled），供 `schedule`、timer 和主动机会三处使用；
  三处原 INSERT 的列和值保持原样（timeout 行原来不写 source_seq，现在也不写）。
- 不改 status、只改 due/expires/condition_config/proactive_source 等列的 UPDATE，照原样写普通 SQL。

### 5. `scopes.mode` 等调度列
不建通用转换函数。凡写 mode 的 SQL 都留在 wakes.py 的具名方法里，模式值改为用 `ScopeMode` 作参数传入
（例如 `"UPDATE scopes SET mode=?,wake_at=NULL,wait_infinite=0 WHERE key=?", (ScopeMode.ACTIVE, key)`）。
WHERE 中比较 mode 的地方同样用参数。

### 6. 构造与挂载
`Wakes(db, now, scopes: Scopes, messages: Messages, views: Views)`。`SocialStore.__init__` 在 P3-a 的四个仓储之后创建 `self.wakes`。

### 7. 方法去向
`SocialStore` 中以下方法**整体**移入 `Wakes`（调用方改为 `store.wakes.<方法>`），内部对 scopes/messages/views 的访问用注入的仓储：
`claim`、`active_turn`、`finish(turn, status: WakeStatus)`、`dialogue_job`、`sleep_observation_due`、`consume_dialogue`、
`dialogue_status`、`defer`、`set_wake_config`、`_schedule`（改名为公开的 `schedule`）、`consume_timers`、`ready`、
`_check_trigger_source`、`release_pending`、`next_due`、`_refund_unclaimed_opportunity`（改名 `_refund`）。

以下方法**留在 SocialStore**，作为跨仓储的编排（用 `with transaction(self.db):` 包住），其中属于 wakes 的 SQL 改为调用 `Wakes` 的新方法：

| SocialStore 方法 | 拆分方式 |
|---|---|
| `bind` | 取消其他版本活跃 wake、`scopes` 调度列复位、清 dialogue_state/dialogue_waits → `Wakes.reset_binding(key, version)`；judge/actions/followups 部分不动 |
| `pause` | 清 dialogue、mode=paused、取消活跃 wake → `Wakes.pause(key)`；actions/followups/judge 部分不动。删除 `_clear_dialogue`（并入 wakes） |
| `observe` | `fresh` 分支中 dialogue_state 的 last_at 写入和 reply wait 的 quiet_due 延长 → `Wakes.note_member_activity(key, seq)` |
| `view` | 读 wake 的 reason / proactive_source → `Wakes.get(job)`（返回 `sqlite3.Row \| None`）；`dialogue_job` 改调 `self.wakes.dialogue_job` |
| `note_seen` | 视图 job 的 queued→running → `Wakes.admit_view_job(key, version, job, turn) -> bool`；按展示序号收取 scheduled wake 的循环（含来源校验失败即取消）和随后的批量 running 更新 → `Wakes.admit_shown(key, version, seqs, turn) -> list[str]`；dialogue_state 的 observed 写入 → `Wakes.note_observed(key, seq)` |
| `recovery_jobs` | **改名 `recover()`，返回 None**（Q12）。顺序：message_deliveries queued→unknown、actions 的两条 UPDATE（这三条留在 store.py，P3-c 再搬）→ `Wakes.recover()`（原函数中其余全部 wakes/requests/scopes SQL，保持原相对顺序）→ `followups.sync_receipts()`。actions 两条提前到 wakes 步骤之前是安全的：其前面的 wakes 步骤不读 actions |
| `cancel_opportunity(job, reason)` | `row = self.wakes.cancel_opportunity(job)`（返回取消前读到的 proactive 行，非 proactive 返回 None；内部退款 + `_move(CANCELLED, ...)`）；若 row 非 None，原样执行 actions 的 failed 更新和 `followups.sync_receipts(scope)`。**actions/followups 部分不依赖 wake 是否真的被改动** |
| `wait_for_quiet` | 计算和 dialogue_waits/scopes/wakes 写入 → `Wakes.wait_for_quiet(...)`（参数同原函数，返回原 dict）；`judge_state` 的 pending_due 更新留在 SocialStore，在同一个 `transaction` 内、仅当返回 `deferred` 为真且 purpose ∈ {reply, messages} 时执行，用返回的 `total_due` 作为原 `total` |
| `_handoff_judgement` | 读最近 completed/running wake → `Wakes.recent_turns(key)`；`self.wakes.schedule(...)`；取 scheduled 行 → `Wakes.scheduled(key)`；`UPDATE wakes SET expires=MIN(...),condition_config,proactive_source` → `Wakes.attach_judgement(job, expires, condition_config, proactive_source)`；dialogue_jobs INSERT → `Wakes.add_dialogue_job(...)`；`UPDATE scopes SET wake_config='{}'` → `Wakes.clear_wake_config(key)`；judge_batches/judge_state 部分不动 |
| `enqueue_parts` | 读本轮 running wake 的 expires → `Wakes.turn_expires(turn)` |

`Wakes.schedule` 内部原来调用 `self.cancel_opportunity(...)` 取消排队中的主动机会：改为调用 `Wakes.cancel_opportunity`（只做 wakes 部分）。
这与原行为等价：被取消的是 queued 且未被 claim 的行，turn_id 为 NULL，原 actions 更新 `turn_id=?` 绑定 NULL 不匹配任何行，
`sync_receipts` 因此也无事可做。

### 8. store.py 之外的 wake 相关 SQL
全部改为调用 `Wakes` 方法（一条不同的 SQL 一个方法，名称描述返回内容，文本完全相同的只建一个；返回原代码使用的类型）：
- `proactive.py`：`status`（:75）、`_deferred`（:91，dialogue_waits）、`tick`（:172 活跃 wake 存在性、:190/:225 requests 计数、:241 INSERT → `Wakes.schedule_opportunity(key, version, candidate, now)`，内部调 `_insert_scheduled`）、`cancel_invalid`（:247）、`check_job`（:260）、`check_send`（:271）。
- `social/runtime.py`：:324、:329、:399（见行为修正 Q2）、:446（requests 统计）、:754（dialogue_state last_sent → `Wakes.note_sent(key)`）、:932。
- `followups.py`：:113；`memory.py`：:367；`judge_runtime.py`：:80 与 :98 改用同一个 `Wakes.wait(key) -> sqlite3.Row | None`（`SELECT * FROM dialogue_waits WHERE scope=?`；`dialogue_status` 也改用它）。
- **不搬**：`expressions.py:285` 的忙碌联合查询、`runtime.py:639` 对 actions 的 UPDATE（含 wakes 子查询），它们归 P3-c。

## 行为修正（只有这四项，报告中逐条说明改了哪里、哪些测试受影响）
- **终态不可变**（含 Q3）：由 `_move` 保证。效果之一：`cancel_opportunity` 不再把 completed/failed/unsent/expired 改成 cancelled
  （原 `store.py` 中前置查询不限 status）。`SocialStore.cancel_opportunity` 中 actions/followups 的部分照旧执行。
- **Q2 发布失败回退**：`runtime.run_due` 中 `_publish_wake` 抛异常后，改为调用 `Wakes.requeue(job)`：
  `_move(SCHEDULED, "id=?", ..., due=now+1)`（只对仍为 queued 的行生效），并在同一事务内对该行执行 `_refund`（退回 `ready` 记的 requests 预算）。
  `_refund` 的判定条件不变（queued、turn_id 为 NULL、requested 非空）：先退款再 `_move`。
- **Q9 终态清理**：删除 `schedule` 末尾按 scope 的 `DELETE FROM wakes ... created<now-86400 AND status NOT IN (活跃三态)`；
  改为在 `ready()` 的事务开头对**所有 scope** 执行同样条件的 DELETE（`dialogue_jobs` 经外键级联删除）。
- **Q12**：`recover()` 返回 None，删除构造 `WakeJob` 列表的代码；若 `WakeJob` 因此不再被任何代码使用，不要删除模型（P3-c 统一清理）。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- `_move` 不检查 rowcount、不抛异常；调用方原来怎么用 rowcount 就怎么用。
- 不得修 P3-0 第 7 节中上面四项以外的疑点（例如 Q4/Q5/Q6/Q10/Q11），哪怕你看到了；写在报告“建议”里。
- 不改 schema、不改 `SCHEMA_VERSION`。
- 预期规模：wakes.py 约 400–500 行；store.py 减少约 400 行；生产代码净增不超过 120 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含 `db/`）
- `nanobot/channels/telegram/runtime.py`、`nanobot/agent/tools/telegram_social.py`（仅随方法改名）
- `nanobot/channels/telegram/tests/`：机械性改名（`store.X(` → `store.wakes.X(`、`recovery_jobs()` → `recover()`）；
  另外，**只**在某个测试断言了上面四项修正前的旧行为时，允许修改该断言以符合新行为——逐条列出文件:行号、原断言、新断言和理由。
- `scripts/evaluate_social_*.py`（仅机械性改名）

## 不要做
- 不要搬 judge_*、actions、send_attempts、message_deliveries、proactive_state 以及知识/学习表的 SQL（P3-c）。
- 不要给 SocialStore 留转发方法。
- 不要改提示词、工具 schema、配置。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（P3-a 后基线 981 passed）；场景测试 `test_social_scenarios.py` 必须全部通过且**不得修改**。
- `.venv/bin/ruff check nanobot/ scripts/` 无错误；basedpyright 对 `nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py nanobot/agent/tools/telegram_social.py` 0 errors
  （如沙箱找不到依赖，按 P3-a 的方式用临时 project 配置补 extraPaths，报告原样说明）。
- 贴出以下搜索结果：
  - `grep -rnE "(UPDATE|INTO|FROM) (wakes|requests|dialogue_waits|dialogue_jobs|dialogue_state)\b" nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py --include=*.py | grep -v "/db/wakes.py"`
    （预期只剩 store.py/proactive 等处**只读子查询**中引用 wakes 的位置，以及 `expressions.py:285`、`runtime.py:639`；逐条说明）
  - `grep -n "status=" nanobot/channels/telegram/social/db/wakes.py`：除 `_move` 内部外，不应再有直接设置 wakes.status 的 SQL。
  - 被移走的每个 `SocialStore` 方法名的全仓库 grep（预期 0 处旧调用）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 方法清单（Wakes 的每个公开方法 → 原位置）
3. 行为修正：四项各改了哪里；受影响的测试（文件:行号、原断言、新断言、理由）
4. 实际运行的命令及结果（原样贴最后几行）
5. 与简报的偏差及原因
6. 未解决的问题 / 建议
