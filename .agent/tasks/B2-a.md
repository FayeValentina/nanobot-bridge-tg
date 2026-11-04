# 任务 B2-a：多段发送时每一段由主模型决定是否引用、引用哪条【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
owner 反馈：`social_send(parts=...)` 多段发送时，引用（Telegram reply）原先每段都带同一个 `reply_to`，看起来像回复了三次；
`ed126e4d` 改为只有第一段引用，但 owner 认为这仍然僵硬：应该**每一段由主模型自己决定**引用哪条消息或不引用。
例子：成员发了一条“猪食”，人会先引用那条回“哈哈哈”，下一段不引用接着说话；也可能第二段引用另一位成员的话。

## 设计（已定，不得改变）

### 1. 工具参数（`nanobot/agent/tools/telegram_social.py::SocialSendTool`）
- `parts.items` 改为两种形式之一（JSON schema 用 `anyOf`）：
  - 字符串（与现在相同：`{"type":"string","minLength":1,"maxLength":4000}`）——该段不引用；
  - 对象 `{"type":"object","properties":{"text":{"type":"string","minLength":1,"maxLength":4000},"reply_to":{"type":"integer","minimum":1}},"required":["text"],"additionalProperties":false}`——该段可引用自己的目标。
- 顶层 `reply_to` 的语义：`text` 单条时不变；与 `parts` 一起使用时，**只作为第一段的引用**，且仅当第一段自己没有给出 `reply_to` 时生效（兼容模型现有习惯）。
- `parts` 的 description 改为原文：
  `"Complete ordered list of 2–3 distinct messages, including the first. Omit text. Each item is a string, or {text, reply_to} to quote an observed message for that part only; quote only where a person naturally would."`
- 工具 `tool_description` 中 `"reply_to must be observed here."` 改为原文：
  `"reply_to must be observed here; with parts, top-level reply_to quotes only the first part, and any part may instead carry its own reply_to."`
- `run` 中把 parts 规范化为 `list[tuple[str, int | None]]`（字符串 → `(s, None)`；对象 → `(text, reply_to 或 None)`），再按上面的规则把顶层 `reply_to` 填到第一段，调用 `delivery.send_parts(ctx, parts, followup_id=...)`。

### 2. `SocialDelivery.send_parts`（`social/delivery.py`）
- 签名改为 `send_parts(self, ctx, parts: list[tuple[str, int | None]], *, followup_id: str | None = None) -> list[DeliveryResult]`。
- 现有检查保持顺序与文案：段数 2–3、`_public_text`、不得重复文本、**每一个**非 None 的 reply_to 都必须 `has_message`（文案不变）、sender 可用。
- 幂等返回旧回执的条件改为：同 turn 已有一组，其 `[(content, reply_to) for row in rows]` 与本次 `parts`（规范化后、`_public_text` 之后）完全相等。
- 其余（`_collect_new_messages`、`_check_send_allowed`、入队、notify）不变。

### 3. `SocialStore.enqueue_parts`（`social/store.py`）
- 签名改为 `enqueue_parts(self, key, version, turn, parts: list[tuple[str, int | None]], *, followup_id=None)`，每段以自己的 reply_to 写入 `insert_part`。
  （撤销 `ed126e4d` 中“只有第一段 reply_to”的写法——现在由调用方逐段给出。）

### 4. 协议（`social/bindings.py` SOCIAL_PROTOCOL）
- 在 “Never provide both fields or repeat/rephrase a part in the list.” 之后追加一句原文：
  `Each part may quote a different observed message, or none: a part item can be {text, reply_to}. Quote only where a person naturally would, e.g. quote a joke for a short laugh, then continue unquoted.`
- 其他协议文本不动。

### 5. 调用方
- 全仓库 `send_parts(` 的调用（约 17 处，测试为主，另有评估脚本）机械改为新签名：`send_parts(ctx, ["a","b"], R)` → `send_parts(ctx, [("a", R), ("b", None)])`
  （保持原测试的语义：原来传入的 reply_to 只放在第一段，与 `ed126e4d` 之后的线上行为一致）。
  若某测试断言了“每段都带 reply_to”，改为断言第一段带、其余不带，并在报告里列出。
- `ed126e4d` 新增的测试 `test_burst_quotes_the_reply_target_only_on_its_first_part` 保留，按新签名调用。

## 要做的事
1. 按设计修改上述文件。
2. 新增测试：
   (a) 工具层：`parts=["哈哈哈", "猪食这词是谁先说的"]` 加顶层 `reply_to=<观察到的 id>` → 第一段引用、第二段不引用；
   (b) 工具层：`parts=[{"text":"哈哈哈","reply_to":A}, {"text":"不过说真的","reply_to":B}]`（A、B 都已观察）→ 两段分别引用 A、B；
   (c) 第二段的 reply_to 未观察 → 返回原文案错误，且没有任何 action 入队；
   (d) 幂等：同 turn 同样的 parts（含各段 reply_to）重复调用返回旧回执；只改其中一段的 reply_to 则不视为重复（按现有规则继续检查，可能被 queued 检查拒绝——断言实际行为并在报告说明）；
   (e) 工具 schema：`parameters["properties"]["parts"]["items"]` 含 `anyOf` 两种形式。
3. 测试修改只允许上面第 5 条所述的机械改动，逐文件计数。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：生产代码净增不超过 25 行。

## 允许改动的路径
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/channels/telegram/social/`（delivery.py、store.py、bindings.py）
- `nanobot/channels/telegram/tests/`（fixtures/ 除外）
- `scripts/evaluate_*.py`（仅机械改签名）

## 不要做
- 不改单条 `text` 的发送、贴纸发送、普通 message 工具。
- 不改 schema 版本、actions 表结构。
- 不做工具合并（Phase 8）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider` 全部通过
- `.venv/bin/ruff check nanobot/ scripts/` 干净；basedpyright（social/ 与 telegram_social.py）0 errors

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
