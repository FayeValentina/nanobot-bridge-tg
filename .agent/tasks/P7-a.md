# 任务 P7-a：judge 准入分层——真人普通消息与 directed 直接唤醒，不再调用 judge【行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md §5.1（含 2026-11-04 “owner 补充决定”一段，必读）；你的 P7-0 清单（`.agent/tasks/P7-0.report.md`）。
Phase 7 拆分：**P7-a（本任务）** 准入分层，只改“谁需要调用 judge”；P7-b judge 内部重写（内存窗口、单次调用、删表、新提示词、用量记录、交接移到 wakes）；
P7-c LearningWorker；P7-d selection 简化与 ValidationError 修复。
本任务**保留**现有合并窗口、批次表、交接（`_handoff`，含已曝光复用）、恢复逻辑不变；只是在调用 LLM 之前，用平台事实决定本批次是否可以直接判 wake。

## 设计（已定，不得改变）

### 1. 记录发送者是否为 bot
- `db/messages.py::set_judge_facts` 写入的 JSON 增加 `"is_bot": msg.is_bot`。旧行没有该键，读取时按 False（真人）处理。
- `JudgeMessage`（`judge_models.py`）增加字段 `is_bot: bool = False`；`JudgeCoordinator._message` 从 `facts.get("is_bot", False)` 填入。
  （judge 在 C 层因此也能看到发送者是 bot；不改提示词。）

### 2. 本地准入（B 层）
在 `JudgeCoordinator` 新增：
```python
def local_admission(self, snapshot: JudgeSnapshot) -> str | None:
    """B layer: platform facts that admit a batch without the intermediary.

    Returns "directed" or "conversation" to wake directly; None asks the judge (C layer)."""
```
规则（按顺序，只用这些事实）：
1. `snapshot.opportunity is not None` 或 `snapshot.new_seqs` 为空 → `None`。
2. `new` = `snapshot.messages` 中 `seq in snapshot.new_seqs` 的消息。任一 `mentions_bot` → `"directed"`。
3. 存在显式选择性等待 → `None`。判定：`conditions, dynamic = self.waiting.conditions(snapshot.scope)`，
   `dynamic and bool(conditions.keywords or conditions.senders or conditions.on_question)`。（仅 `on_messages`、仅 coalesce/sleep_interrupts、purpose=messages/reply 的普通等待、无条件 sleep 都**不是**选择性等待。）
4. `new` 全部 `is_bot` → `None`。
5. 其余 → `"conversation"`。

### 3. 本地决策落库
在 `db/judge.py::Judge` 新增：
```python
def decide_locally(self, batch: str, snapshot: JudgeSnapshot, reason: str) -> JudgeRecord:
    """Record a host wake decision for a batch the intermediary need not assess."""
```
- 与 `finish_attempt` 中 `decision is not None` 的分支**同样的守卫、同样的顺序**（`_input_matches`、record 状态必须为 pending、`current(snapshot)`、`through(scope) == snapshot.after_seq`；不满足时与 finish_attempt 相同地置 expired/cancelled + `stale_input`），
  满足时写 `status='decided'`、`decision`、更新 `judge_state.assessed_through`、调用 `self._handoff(batch, snapshot, decision)`。**不写 judge_attempts，不占用 judge 请求预算。**
- decision 为 `JudgeDecision(decision="wake", through_seq=snapshot.through_seq, source_seqs=(snapshot.new_seqs[-1],), fulfills_intent=False, reason=reason)`。
  reason 文本固定：directed → `"host: directed at the bot"`；conversation → `"host: fresh conversation without a selective wait"`。
  若 `JudgeDecision` 的校验要求 reason 长度/格式，按其约束；不要放宽校验。
- 若现有 record 状态不是 pending（例如已 decided），原样返回 record，不做任何事（与 finish_attempt 对非 running 记录的处理一致的“不复活”原则）。

### 4. 接入 `_run`
`JudgeCoordinator._run` 中，在 batch 打开（或恢复）之后、`result = await self.judge.assess(snapshot)` 之前：
```python
reason = self.local_admission(snapshot)
if reason is not None:
    result = self.store.judge.decide_locally(batch, snapshot, {"directed": "...", "conversation": "..."}[reason])
else:
    result = await self.judge.assess(snapshot)
```
（reason→文本映射写成模块常量或直接内联，二选一，不要新建 helper。）其余收尾逻辑（finally 中推进水位、重排）不变。

### 行为变化（本任务唯一允许的）
- 真人发的普通群消息、以及任何 directed 消息：不再调用 judge LLM，合并窗口到期后直接按 wake 交接（仍经过 `_handoff`，所以“已在本 turn 曝光则复用原 wake”、dialogue_jobs 登记、wake 预算与冷却都照旧）。
- 其他 bot 的非 directed 消息、以及存在显式选择性等待时的非 directed 消息：照旧调用 judge。
- 不改 judge 提示词、超时、重试、fallback、预算（P7-b 统一处理）。

## 要做的事
1. 按设计修改 `db/messages.py`、`judge_models.py`、`judge_runtime.py`、`db/judge.py`。
2. 测试：
   - 新增（放进 `test_social_judge_runtime.py`）：
     (a) 真人普通消息 → 合并窗口到期后产生 wake，judge provider 调用次数为 0，judge_batches 该批 status=decided、reason 为 conversation 文本，judge_attempts 无行；
     (b) directed（mentions_bot）且存在选择性等待 → 直接 wake，judge 0 次调用，`wake_config` 未被清除；
     (c) 存在选择性等待（senders）时真人非 directed 消息 → 调用 judge 1 次；
     (d) 只有 bot 发的非 directed 消息 → 调用 judge 1 次；同一批中再有一条真人消息 → 0 次；
     (e) 只有 `on_messages=true` 的 wake_config → 0 次；
     (f) 本地 wake 的消息已在当前 running turn 曝光 → 复用原 wake，不新建（证明仍经 `_handoff`）。
   - 已有测试因行为变化失败的，按 P7-0 第 5.5 节的“§5.1 新预期”修改断言（例如普通消息的 `judge_calls` 由 1 变 0；要测 judge 故障回退的场景先建立选择性等待或改用 bot 消息）。
     **每一处语义性断言修改都要在报告里逐条列出：文件:行号、原断言、新断言、理由。** 机械改动单独计数。
   - `nanobot/channels/telegram/tests/conftest.py` 的 autouse 伪造 judge 不改逻辑；若因此有测试需要调整，同样逐条列出。
   - 场景测试 `test_social_scenarios.py` 允许按上述规则更新预期，逐条列出。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件、配置项。
- 预期规模：生产代码新增不超过 50 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（db/messages.py、judge_models.py、judge_runtime.py、db/judge.py）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）

## 不要做
- 不改 judge 提示词、`SocialJudge.assess`、合并窗口计算、表结构（schema 版本不变）、恢复逻辑。
- 不改 directed 的识别方式（Telegram channel 中的 `handle in text.lower()` 兜底保持原样）。
- 不改 `snapshot.judgement` / `admission` 字段（P7-b）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/channels/telegram` 干净；basedpyright（social/）0 errors（需要显式 venv 时贴出所用配置）
- 报告中给出行为变化说明：哪些消息不再调用 judge、哪些仍调用

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因（含语义性测试修改清单）
4. 未解决的问题 / 需要编排者决定的事项
