# 任务 P5-b：单一的发送许可检查【小幅行为变更：错误文案统一】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-5 后半：冷却、“已有排队中的发送”、unknown 重发保护、每小时预算、暂停、proactive 合法性这些发送前检查，
在 `TelegramSocialRuntime.begin_message`、`send`、`send_parts`（`nanobot/channels/telegram/social/runtime.py`）和 `SocialStore.reserve_send`、
`SocialStore.enqueue_parts`（`social/store.py`）里各写了一遍，文案还不一致。P5-a（`7b9fe9d2`）已把投递日志合并为 `actions` 一张表。
事实来源：`.agent/tasks/P5-0.report.md` 第 3 节（检查矩阵、错误码 E1–E28、“副本差异”表）。行号以当前代码为准。

## 设计（已定，不得改变）

### 1. 唯一的检查方法
在 `TelegramSocialRuntime` 中新增：
```python
def _check_send_allowed(self, scope: ConversationScope, turn: str | None, version: str,
                        contents: Sequence[str], units: int) -> None:
    """Every new send passes here once, inside the scope's send lock."""
```
按以下顺序检查，第一项不满足即抛出（文案逐字使用下表）：
| 顺序 | 检查 | 条件（沿用现有查询） | 异常与文案 |
|---|---|---|---|
| 1 | 暂停 | `store.scopes.state(key)["mode"] == "paused"` | `PermissionError("social conversation is paused")` |
| 2 | 主动机会/跟进合法性 | `self.proactive.check_send(scope, turn, self.binding(scope))` | 由其自身抛出（不改） |
| 3 | 冷却 | `last_send` 存在且 `now < last_send + send_cooldown_seconds` | `PermissionError("send cooldown is active")` |
| 4 | 已有排队发送 | `store.deliveries.queued(key)` | `PermissionError("an earlier send is awaiting delivery")` |
| 5 | unknown 重发保护 | 对 `contents` 中**每一项**调用 `store.deliveries.unknown_content(key, version, content, now - 86400)` | `PermissionError("delivery of this text is unknown; do not blindly repeat it")` |
| 6 | 每小时预算 | `store.deliveries.attempt_count(key, now - 3600) + units > max_sends_per_hour` | `PermissionError("hourly send budget exhausted")` |

### 2. 调用方
- `begin_message`：binding 为 None 时返回 None（不变）→ 读 version → **先**做“同 turn 已尝试”拒绝（原 `attempted_content` 检查，原文案不变，属于 message 工具语义）
  → `_check_send_allowed(scope, turn, version, [content], units)` → `store.reserve_send(...)`。
- `send`：锁内 `validate_context` → 读 version → `latest_content` 幂等返回（不变，social_send 语义）→ `_check_send_allowed(scope, ctx.turn_id, version, [text], 1)` → `store.reserve_send(...)`。
  删除锁内原有的单独 `proactive.check_send`、冷却、queued 检查（已在 `_check_send_allowed` 中）。
- `send_parts`：锁内 `validate_context` → 读 version → 整组幂等返回（不变，多段语义）→ `_check_send_allowed(scope, ctx.turn_id, version, texts, len(texts))` → `store.enqueue_parts(...)`。
  同样删除锁内原有的单独检查。
- 三处原来在 `_check_send_allowed` 之前的 `validate_context`、文本/形状/sender/reply 目标检查保持原位置不变。
- `_deliver`、`_retry_sends`、`SocialStore.claim_retry` 中的检查**不动**：它们是平台调用/排队重试时刻的再校验，语义不同（P5-0 已列出）。

### 3. 仓储与 store 只写不判
- `SocialStore.reserve_send`：删除其中的 version 比较、unknown 检查和预算检查，只保留写入（insert、followup reserve、prune、charge）。
  删除 version 比较的理由：所有调用方都在同一把 scope 发送锁内、在无 await 的同步代码里刚刚读出 version，比较恒为真。
  `limit` 参数随之删除。
- `SocialStore.enqueue_parts`：删除预算检查和逐段 unknown 检查；保留 `parts require an active authorized turn`（多段专有前提，不属于发送许可）。`limit` 参数删除。
- `Deliveries.unknown_part` 删除（与 `unknown_content` 文本相同的查询，只剩一个）。

### 4. 行为变化（只有以下几项，报告逐条确认）
1. 文案统一：`an earlier send is awaiting a platform retry`→`an earlier send is awaiting delivery`；`delivery is unknown; do not blindly repeat a part`→`delivery of this text is unknown; do not blindly repeat it`；
   `hourly send budget cannot accommodate all parts`→`hourly send budget exhausted`。
2. `begin_message` 中同 turn 重复检查提前到冷却/排队检查之前（多项同时不满足时，报出的是“已尝试”而不是“冷却”）。
3. `send`/`send_parts` 也执行暂停检查（`validate_context` 已经拒绝暂停的 scope，结果相同）。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、配置项、日志表。
- 不改 `_deliver`/`_retry_sends`/`claim_retry`/`queue_retry` 的逻辑；不改 Telegram channel（P5-c）。
- 预期规模：生产代码净减少约 20 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含 `db/`）
- `nanobot/channels/telegram/tests/`：断言旧文案的测试（例如 `test_social_scheduler.py` 中 `match="all parts"`）改为新文案中的子串；调用 `reserve_send`/`enqueue_parts` 时传 `limit` 的测试做机械修改。逐条列出。

## 不要做
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（基线 984 passed）；`test_social_scenarios.py` 不得修改。
- `.venv/bin/ruff check nanobot/` 无错误；basedpyright 对 `nanobot/channels/telegram/social` 0 errors。
- 贴出：`grep -n "send cooldown is active\|awaiting\|blindly repeat\|hourly send budget\|social conversation is paused" -r nanobot/channels/telegram/social --include=*.py`
  （预期：每条文案在生产代码中只出现一次，均在 `_check_send_allowed` 内；`validate_context` 中原有的暂停文案除外，说明之）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件），含生产代码净行数
2. 三项行为变化的落点（文件:行号）
3. 实际运行的命令及结果（原样贴最后几行）
4. 修改过的测试（逐条）
5. 与简报的偏差及原因
6. 未解决的问题 / 建议
