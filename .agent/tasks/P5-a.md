# 任务 P5-a：投递日志合并为一张表（schema 21）【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-5。现在一次发送可能记在两张表里：`social_send`/贴纸/多段只写 `actions`；普通 `message` 工具发到 social scope 时，
既在 `actions` 预约一行（计额度），又在 `message_deliveries` 另建一行（不同 ID、无关联列）记录回执，两行分两步更新，崩溃时会不一致；
所有读取方都要 UNION 或各查一张。事实来源：`.agent/tasks/P5-0.report.md`（第 1、2、4 节与第 8 节疑点 1、2、5、7、8、10、20）。行号以当前代码为准。

本任务把两张表合并为 `actions` 一张（**不改表名**），一次发送只对应一行；Telegram channel 对普通发送只调用 social runtime 的两个方法。
发送前检查的统一是下一个任务 P5-b，Telegram 多余接入点的清理是 P5-c，本任务都**不做**。

## 设计（已定，不得改变）

### 1. schema 20 → 21（`db/schema.py`，沿用现有迁移写法，`SCHEMA_VERSION` 改为 21）
按顺序：
1. `ALTER TABLE actions ADD COLUMN message_ids TEXT NOT NULL DEFAULT '[]'`
2. `UPDATE actions SET message_ids=json_array(message_id) WHERE message_id IS NOT NULL`
3. 把 `message_deliveries` 的每一行插入 actions：`id`←id，`scope`←scope，`version`←`COALESCE(version,(SELECT version FROM scopes WHERE key=message_deliveries.scope))`，
   `content`←`''`，`status`←`CASE status WHEN 'queued' THEN 'sending' ELSE status END`，`created`←created，`message_ids`←message_ids，`error`←detail，
   `turn_id`←turn_id，`kind`←`'message'`，`attempts`←1。其余列取默认值。
   （历史上普通 message 预约在 actions 中的那一行保留原样，不做配对删除；它们 7 天后由现有清理规则删掉。）
4. `DROP TABLE message_deliveries`
5. `ALTER TABLE actions DROP COLUMN message_id`
新库的建表语句（`initialize_social_schema` 中首次创建路径）同步为最终形态：actions 有 `message_ids`、无 `message_id`，不再创建 message_deliveries。
`followup_sends.delivery_id` 引用的普通 message ID 在第 3 步中保留，不需要改。

### 2. 统一后的 actions 语义
- `kind`：`text`（social_send 单条/多段）、`sticker`、`message`（普通 message 工具的整个 envelope）。
- `status`：`sending`（已预约、平台结果未定）、`queued`（多段等待/429 重试）、`sent`、`failed`、`unknown`。普通 message 不再有 `queued` 状态。
- `message_ids`：JSON 整数数组，平台已确认的消息 ID（普通 envelope 可以是部分成功的前缀）。
- `error`：失败/未知说明；普通 message 的 detail 也写这里（沿用现有 200 字符截断）。
- `content`：普通 message 为 `msg.content`，无文本时为 media 列表 JSON（与现在预约时写入的值相同）。
- **终态不可改**：`sent`/`failed`/`unknown` 是终态。`Deliveries` 中所有改 `status` 的 UPDATE 都必须带 `AND status IN ('sending','queued')`
  （已带 `status='queued'` 等更窄条件的保持原条件）。`Deliveries.record_delivery` 返回 rowcount。

### 3. 写入路径
- `SocialStore.reserve_send(...)`：把 `sticker` 参数决定 kind 的逻辑改为显式 `kind: str` 参数（调用方传 `"text"`/`"sticker"`/`"message"`），其余检查与写入不变。
- `SocialStore.delivery(action, status, message_ids: tuple[int, ...] = (), error=None)`：
  `changed = self.deliveries.record_delivery(...)`（写 `message_ids=json.dumps(list(message_ids))`）；**只有 changed 为 1 时**才执行原有副作用
  （贴纸 uses 计数、`followups.receipt(action, status, confirmed=bool(message_ids))`、failed/unknown 时让同组后续 queued 段失败并同步其跟进回执）。
  原贴纸计数条件 `previous["status"] != "sent"` 因终态保护而多余，删除该条件，但保留 `kind == "sticker"` 判断。
- social 单条发送成功处（`social/runtime.py` `_deliver`）改为 `store.delivery(action, "sent", (message_id,))`；其他调用点按新签名改。
- `SocialStore.claim_retry(action, limit) -> bool`（新的编排方法，取代调用方直接调 `Deliveries.claim_retry`）：
  读 queued 行（不是 queued 返回 False）；过期或预算已满时调用 `self.delivery(action, "failed", error="budget or expiry")` 并返回 False（疑点 7：原来直接写 failed、不同步跟进回执）；
  否则调用 `Deliveries` 的 claim（sending、attempts+1、due=now、插 1 条 send_attempts）返回 True。`Deliveries.claim_retry` 拆成只读查询和只写 claim 两个方法，或改名为 `claim`，二选一，报告说明。

### 4. 普通 message 工具：一行、两个方法
在 `TelegramSocialRuntime` 中用两个方法取代 `reserve_message_send` 和 `record_message_receipt`（删除这两个旧方法）：
```python
def begin_message(self, scope: ConversationScope, content: str, turn: str | None, *, units: int,
                  followup_id: str | None) -> str | None:
    """Reserve one ordinary envelope (kind="message"); None when the scope has no binding."""

def finish_message(self, scope: ConversationScope, delivery_id: str, receipt: DeliveryReceipt,
                   texts: list[tuple[int, str]]) -> None:
    """Record the envelope's final receipt once, then its confirmed-send side effects."""
```
- `begin_message`：binding 为 None 返回 None。否则执行原 `reserve_message_send` 的全部检查（paused、`proactive.check_send`、冷却、已有 queued、同 turn 重复），
  然后 `store.reserve_send(..., kind="message", units=units, followup_id=followup_id, turn=turn)`。
  followup 的预约随之在同一事务里完成（原来在 `record_message_receipt` 的 queued 分支里做，并在其前再调一次 `proactive.check_send`——这次调用在本方法开头已经做过，不重复）。
- `finish_message`：`store.delivery(delivery_id, receipt.status, receipt.message_ids, receipt.detail)`；之后对 `texts` 逐条 observe（原代码），
  再在“该行 version 等于当前 version，且本次 delivery 确实改变了状态，且（`receipt.status == "sent"` 或 texts 非空）”时调用 `_confirmed_send`（原条件的单行等价形式）。
  需要的话让 `SocialStore.delivery` 返回 changed（bool）。
- 新增 `followup_note(followup_id: str) -> str`：返回原 channel 代码拼接的 `remaining_attempts=...; unresolved_send=...` 文本，供 channel 使用。
- `nanobot/channels/telegram/runtime.py`：
  - `_send_confirmed`：预约改为 `delivery_id = self.social.begin_message(...)`（仅当 social 非 None 且 binding 存在时，与现条件相同），删除 `action_id` 变量和
    “queued 回执”那次 `_record_message_receipt` 调用；取消分支和最终回执分支改为调用 `_record_message_receipt(scope, delivery_id, receipt, sent_texts)`；
    删除两处 `self.social.store.delivery(...)`；跟进说明改用 `self.social.followup_note(...)`。
  - `_record_message_receipt` 保留为 channel 内的小包装：记日志；`delivery_id` 非 None 且 social 非 None 时调用 `finish_message`，异常只记日志（保留原注释的含义：记账失败不能诱导重发）；
    删除 queued 分支的重新抛出（预约失败现在发生在 `begin_message`，会作为 PermissionError/ValueError 被现有 except 转成 failed 回执）。
  - channel 中不得再出现 `social.store`。

### 5. 读取方（全部改到 actions，删除对 message_deliveries 的引用）
- `confirmed_turn`、`confirmed_receipt`：单表，条件 `(status='sent' OR json_array_length(message_ids)>0)`。
- `recent_usage`：单表 `status='sent'`；text 与 message 用 `content`，sticker 用 `media_ref`（原 actions 分支写法）；删除 message_deliveries ⋈ messages 分支。`content=''` 的行（迁移来的历史 message 行）不产出文本。
- `Messages.participation`：只 LEFT JOIN actions：`a.scope=m.scope AND a.status='sent' AND m.message_id IN (SELECT value FROM json_each(a.message_ids))`，
  `send_group = COALESCE(a.group_id, a.id, CAST(m.message_id AS TEXT))`。
- `Wakes.recover` 的两条“unknown 发送取消 wake”规则保持原语义：原 message_deliveries 规则改为 `actions ... kind='message' AND status='unknown'`（scope+turn 关联），
  原 actions 规则加 `kind<>'message'`（scope+version+created 关联）。
- `SocialFollowups.sync_receipts`：只 LEFT JOIN actions。
- `proactive.status` 的诊断：`turn_actions` 返回 `id,kind,status,message_ids`，删除 `turn_messages` 与输出中的 `message_deliveries` 键。
- `Deliveries.recover`：删除 message_deliveries 那条 UPDATE；其余两条不变（普通 message 的 sending 行随第二条变为 unknown）。
- `uncertain_send`、`unknown_content`、`unknown_part`、`attempted_content`、`latest_content`、`last_send`、`queued` 等：SQL 不变（它们本就只查 actions，现在自然覆盖 message 行）。
  `_action_result` 等读取 `message_id` 的地方改为从 `message_ids` 取第一个。

## 行为变化（只有以下几项，报告逐条确认落点）
1. 普通 message 一次发送只有一行（kind='message'）；预约被拒时不再额外写一条 failed 回执行。
2. 投递终态不可改；被拒绝的终态写入不再触发贴纸计数和跟进回执（疑点 8）。
3. 多段/重试在 claim 时因过期或预算失败，会同步跟进回执（疑点 7）。
4. `uncertain_send` 等只查 actions 的保护，现在自然覆盖普通 message 的 unknown（原来它们只看到预约行，结果相同或更完整）。
5. proactive 状态诊断中 `message_deliveries` 字段并入 `sends`。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、配置项、日志表。迁移只做上面 5 步，不加“表不存在时”之类的保护分支（沿用现有迁移的写法）。
- 不统一发送前检查（P5-b），不删 Telegram 中的 `_observe_social` 调用点（P5-c），不改错误文案。
- 预期规模：生产代码净减少（两表合并后查询变少）；报告给出净行数。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含 `db/`）、`nanobot/channels/telegram/runtime.py`、`nanobot/agent/tools/telegram_social.py`、`nanobot/agent/tools/message.py`（仅当必要）
- `nanobot/channels/telegram/tests/`、`tests/agent/test_telegram_social_contracts.py`：
  测试里对 `message_deliveries` 表、`actions.message_id` 列的直接 SQL 改为新形态（机械性）；调用 `reserve_message_send`/`record_message_receipt` 的测试改用新方法；
  断言了上面 5 项旧行为的测试按新行为修改。每处修改逐条列出。新增迁移测试**一个**：构造 schema 20 的 actions + message_deliveries 行（含 queued、sent、带 message_id 的 action），
  打开 store 后断言合并结果（行数、kind、status 映射、message_ids、followup 关联 ID 不变、message_deliveries 表已不存在）。
- 不改 `scripts/`（评估脚本将来统一迁移；报告列出受影响的脚本）。

## 不要做
- 不要给 SocialStore/runtime 留旧方法的转发。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（基线 983 passed，加新迁移测试）；`test_social_scenarios.py` 不得修改，必须全部通过（其中“普通 message 共享额度”“unknown 跨 turn 保护”“平台发送中断重启”等发送场景是重点）。
- `.venv/bin/ruff check nanobot/` 无错误；basedpyright 对 `nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py` 0 errors。
- 贴出：`grep -rn "message_deliveries\|reserve_message_send\|record_message_receipt" nanobot --include=*.py`（预期只剩 db/schema.py 的历史迁移语句）；
  `grep -n "social\.store" nanobot/channels/telegram/runtime.py`（预期 0）；`grep -rn "\bmessage_id\b" nanobot/channels/telegram/social/db/deliveries.py`（预期 0）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件），含生产代码净行数
2. 五项行为变化各自的落点（文件:行号）
3. 实际运行的命令及结果（原样贴最后几行）
4. 修改过的测试（逐条）
5. 受影响但未修改的 scripts/ 文件
6. 与简报的偏差及原因
7. 未解决的问题 / 建议
