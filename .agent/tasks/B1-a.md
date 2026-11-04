# 任务 B1-a：发送前收取新到消息，合并回复连续补充【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
owner 反馈：连续发多条消息时，agent 逐条回复而不是合并回复。你在 B1-0 的分析（`.agent/tasks/B1-0.report.md`）已确认：
第一轮 turn 只看到第一条，后到的 423/424 只能排到下一轮；发送前没有任何“有没有新消息”的检查。
owner 决定：**发送前检查新消息（本任务）**，同时把合并窗口调到 10 秒（配置，Claude 部署时改，不在本任务内）。

## 设计（已定，不得改变）

在 `TelegramSocialRuntime`（`nanobot/channels/telegram/social/runtime.py`）中：

1. 新增实例属性 `self._send_rechecked: dict[str, str] = {}`（scope.session_key → 已经做过一次发送前收取的 turn_id），在 `__init__` 中紧挨 `self._send_locks` 定义。
   每个 scope 只保留最近一个 turn_id，无需清理。

2. 新增方法：
   ```python
   def _collect_new_messages(self, scope: ConversationScope, turn: str | None) -> None:
       """Before a new send, show member messages that arrived after this turn's latest exposed snapshot.

       Runs at most once per turn, so a busy group cannot bounce sends forever."""
   ```
   规则（按顺序）：
   - `turn is None` → 直接返回。
   - `self._send_rechecked.get(key) == turn` → 直接返回。
   - `shown = self.store.views.shown_sequences(key, turn)`；`shown` 为空 → 直接返回（本 turn 没有曝光过快照，没有比较基线）。
   - `fresh = [row for row in self.store.messages.after(key, max(shown), self.config.snapshot_messages) if not row["is_self"]]`；`fresh` 为空 → 直接返回。
   - 否则：`self._send_rechecked[key] = turn`，然后
     `raise PermissionError(NEW_MESSAGES_NOTICE + "\n" + self.read_messages_for(scope, turn))`。
   - `NEW_MESSAGES_NOTICE` 是模块级常量，文本原样为：
     `"NOT sent: new messages arrived after the snapshot you answered. Read them below and reply once to everything together (one send, text or parts); do not answer the earlier messages separately. If nothing changes your reply, send it again."`
   - `read_messages_for(scope, turn)`：把现有 `read_messages(ctx)` 中构造快照并 `encode_view` 的部分抽成这个方法（参数为 scope 与 turn_id），
     `read_messages(ctx)` 改为 `validate_context` 后调用它。快照因此照常登记到 views；工具结果进入下一次模型请求时，
     现有曝光 hook（`note_seen` → `admit_shown`）会把这些消息对应的 scheduled wake 收取到当前 turn。**不新增任何收取逻辑。**

3. 调用位置——只在下面两个方法里、`async with lock:` 块内、`self.validate_context(ctx)` 与 version 读取之后、
   **幂等返回旧回执的分支之后**、`self._check_send_allowed(...)` **之前**调用 `self._collect_new_messages(scope, ctx.turn_id)`：
   - `send`（social_send 的 text 和 social_sticker 的 send 都走这里）
   - `send_parts`
   `begin_message`（普通 message 工具）**不调用**。

4. 协议：`nanobot/channels/telegram/social/bindings.py` 的 SOCIAL_PROTOCOL 中 “New messages can arrive during your turn.” 这句（跨第 34–35 行，`can arrive during your turn.` 之后）
   紧接着追加一句原文（同一行内，空格分隔，不改换行结构以外的内容）：`If social_send reports new messages, reply once to the whole exchange instead of answering each message separately.`
   其他协议文本不动。

## 要做的事
1. 按设计修改 `runtime.py`、`bindings.py`。
2. 测试（加到现有社交测试文件中合适的位置，不新建测试文件；场景测试加到 `test_social_scenarios.py`，只用其中已有的黑盒辅助方式）：
   - 场景：wake 快照只含消息 A；turn 中途观察到同一成员的消息 B、C；`social_send(text=...)` 返回错误，错误文本以 `NOT sent: new messages arrived` 开头且含 B、C 的文本；
     第二次 `social_send` 成功入队；B/C 对应的 scheduled wake（若已由 judge 创建）在下一次模型请求曝光后被收取到当前 turn，turn 结束后**不再**另起 turn。
   - 同一 turn 第二次遇到新消息不再拦截（每 turn 最多一次）。
   - 新到的只有 is_self 消息时不拦截。
   - `send_parts` 同样拦截一次。
   - 幂等：同 turn 重复同内容的 `send` 仍返回旧回执，不被拦截。
3. **只报告、不实现**：用测试或代码阅读确认下列情形，写进报告第 4 节：
   judge 在发送前收取之后才对 B/C 返回 wake（即 wake 创建时 B/C 已被当前 turn 曝光并 mark_read）时，会不会另起一个 turn？
   给出依据（文件:行号或测试输出）。合并窗口将调到 10 秒，这种情形会更常见。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：生产代码新增不超过 30 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/runtime.py`
- `nanobot/channels/telegram/social/bindings.py`
- `nanobot/channels/telegram/tests/` 下已有的测试文件

## 不要做
- 不改配置默认值、schema、judge、wake 调度、runner/loop 的注入逻辑。
- 不改 `_check_send_allowed` 的检查内容与顺序。
- 不改普通 message 工具路径。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/channels/telegram` 干净；`.venv/bin/basedpyright nanobot/channels/telegram/social/runtime.py` 0 errors
- 需要修改已有测试才能通过时，只允许机械性修改，并在报告里逐条列出（例如协议文本快照）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项（含第 3 项“只报告”的结论）
