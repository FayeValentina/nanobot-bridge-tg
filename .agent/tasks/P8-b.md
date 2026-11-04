# 任务 P8-b：social 工具 12→9 合并，描述精简，protocol 同步【行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md §5.3（owner 决策 D5）与 P2-1；事实清单 `.agent/tasks/P8-0.report.md` 第 4–6 节（行号以当前代码为准，P8-a 之后 bindings/context/memory/selection 行号已变）。
§5.3 的保留清单实际是 **9 个**工具（P8-0 疑点 1），按 9 个实施：
`social_send`、`social_wait`、`social_state`、`social_media`、`social_sticker`、`social_memory`、`social_jargon`、`social_expression`、`social_activity`。
删除：`social_get_state`、`social_read_messages`、`social_mark_read`、`social_set_wake`、`social_get_media`（改名）、`social_sticker` 的 send 动作。
会话历史中的旧工具名不做兼容层（P8-0 §5.5 已核实：历史不重放、不校验工具名）。

编排者已写好：
- 新 protocol：`.agent/tasks/P8-b.social_protocol.md`（替换 `nanobot/templates/telegram/social_protocol.md`，逐字节复制）；
- 9 个工具的 description：`.agent/tasks/P8-b.descriptions.json`（每个 ≤400 字符，逐字使用）。

## 设计（已定，不得改变）

### A. `nanobot/agent/tools/telegram_social.py`
- 9 个工具类，一个工具一个类，继续继承 `_SocialTool`（基类不改）。`tool_description` 取自 descriptions.json 对应条目原文。
- 类名：`SocialSendTool`、`SocialWaitTool`、`SocialStateTool`（新）、`SocialMediaTool`（由 `SocialGetMediaTool` 改名，参数与执行不变）、`SocialStickerTool`、`SocialMemoryTool`、`SocialJargonTool`、`SocialExpressionTool`、`SocialActivityTool`。
  删除 `SocialGetStateTool`、`SocialReadMessagesTool`、`SocialMarkReadTool`、`SocialSetWakeTool`、`SocialGetMediaTool`。`SOCIAL_TOOL_CLASSES` 同步。
- 参数 schema 中 description 字段：删除 `text`、`parts` 的参数级 description（规则已在工具描述与 protocol 中）；`set_name` 的保留。不新增其他参数级 description。
- **social_send**：参数 `text`、`sticker_id`（string 1–256）、`parts`、`reply_to`、`followup_id`。
  - `parts`：array 2–3，items `anyOf`：string(1–4000) | `{text!: string 1–4000, reply_to?: int≥1}` | `{sticker_id!: string 1–256, reply_to?: int≥1}`，后两者 `additionalProperties: false`。
  - schema 顶层 `oneOf: [{required:[text]}, {required:[sticker_id]}, {required:[parts]}]`；`validate_params` 与 `run` 都要求 text/sticker_id/parts **恰好一个**，错误文案改为：
    `Provide exactly one of text, sticker_id or parts: text for one message, sticker_id for one sticker, or parts=[first, second, ...] for the complete 2–3 message sequence. No messages were queued.`
  - `run`：text → `delivery.send(ctx, text, reply_to, followup_id=...)`（不变）；sticker_id → `delivery.send_sticker(ctx, sticker_id, reply_to, followup_id=...)`；
    parts → 转成 `list[SendPart]`（见 C），顶层 reply_to 只补到第一段且第一段无自己的 reply_to 时（现有规则），调用 `delivery.send_parts`，返回 `{"actions": [...]}`。有 followup_id 时附 `followup`（现有逻辑不变）。
- **social_wait**：保留现有参数 `seconds/sleeping/on_timeout/purpose/quiet_seconds/infinite/until`，新增
  `until_condition`: object `{members?: array≤20 of string pattern ^[0-9]+$, topics?: array≤20 of string 1–80, questions?: bool, one_shot?: bool, ttl_seconds?: number 1–86400}`，`additionalProperties: false`。
  `run`：若传了 `until_condition`，先调用 `waiting.set_conditions(ctx, members=..., topics=..., questions=..., one_shot=..., ttl_seconds=...)`（缺省：[]、[]、False、True、3600），再调用 `waiting.wait(...)`（参数照旧）；
  返回 `wait` 的结果，若有 until_condition 则加键 `"until_condition": <set_conditions 的返回>`。没传 until_condition 时行为与现在完全相同。
- **social_state**：参数 `action!: get|read|set_proactive`，`paused?: bool`，`additionalProperties: false`。
  get → `runtime.get_state(ctx)`；read → `runtime.read_messages(ctx)`（返回字符串，不变）；set_proactive → 必须有 `paused`，否则 `ValueError("set_proactive requires paused")`，调用 `waiting.set_proactive(ctx, paused)`。
- **social_sticker**：action 枚举去掉 `send`，删除 `reply_to` 参数；其余参数与分支不变；缺参错误文案去掉 `send requires sticker_id; `。

### B. `nanobot/channels/telegram/social/waiting.py`
- 删除 `set_wake`。新增两个方法（只是把现有分支拆开，不改语义）：
  - `set_conditions(self, ctx, *, members: list[str], topics: list[str], questions: bool, one_shot: bool, ttl_seconds: float) -> dict[str, Any]`：
    保留 `allow_ambient_wake` 检查与 TTL 检查（原文案不变）；三者全空 → `set_wake_config(key, "{}")` 返回 `{"cleared": True}`；
    否则写入 `TelegramSocialWakeConfig(keywords=topics, senders=members, on_question=questions, expires_at=now+ttl, one_shot=one_shot)`（on_messages、sleep_interrupts、coalesce_seconds 用默认值），
    返回 `{"members": members, "topics": topics, "questions": questions, "one_shot": one_shot, "expires_at": ...}`。
  - `set_proactive(self, ctx, paused: bool) -> dict[str, bool]`：即原 `proactive_paused` 分支（set_stopped、paused 时 `cancel_invalid`、notify），**不再**受 `allow_ambient_wake` 限制（P8-0 疑点 10，编排者决定解除）；返回 `{"proactive_paused": paused}`。
- 原 `coalesce_seconds`、`sleep_interrupts` 校验分支随 set_wake 一起删除。`TelegramSocialWakeConfig` / `TelegramSocialSleepInterruptConfig` 类本身**不改**（配置面收缩留给 Phase 9）；若删除后 `TelegramSocialSleepInterruptConfig` 在 waiting.py/telegram_social.py 不再使用，删除对应 import。

### C. 贴纸进入 parts：`models.py`、`delivery.py`、`store.py`、`db/deliveries.py`
- `models.py` 新增：
  ```python
  class SendPart(NamedTuple):
      text: str | None
      sticker_id: str | None
      reply_to: int | None
  ```
- `delivery.py`：
  - 抽出 `_static_sticker(self, scope, sticker_id) -> MediaReference`（即 `send_sticker` 现有的取库与“only static stickers”检查）；`send_sticker(ctx, sticker_id, reply_to, *, followup_id: str | None = None)` 用它，并把 followup_id 传给 `send`。
  - `send_parts(ctx, parts: list[SendPart], *, followup_id=None)`：每段解析为 `(content, reply_to, media)`：sticker 段 `content=f"[sticker:{ref.unique_id}]"`、`media=ref`；文本段 `content=_public_text(text)`、`media=None`。
    之后的步骤与现在相同，只是作用于 content：去重、reply_to 已观察检查、幂等比较 `[(content, reply_to)]`、`_collect_new_messages`、`_check_send_allowed(contents, len(parts))`。
    发送方可用性检查：有文本段要求 `self.sender`，有贴纸段要求 `self.sticker_sender`（同一个 RuntimeError 文案）。
  - `_deliver`、`retry_due`、`_confirmed_send` **不改**（已按行的 kind/media_ref 选择 sticker_sender 与贴纸副作用；请核实并在报告中写明依据行号）。
- `store.py` `enqueue_parts(key, version, turn, parts: list[tuple[str, int | None, MediaReference | None]], *, followup_id=None)`：插入时传 `kind="sticker" if media else "text"` 与 `media.model_dump_json() if media else None`。
- `db/deliveries.py` `insert_part` 增加参数 `kind: str, media_ref: str | None`，INSERT 写入 `kind, media_ref` 两列（列已存在，`kind` 默认 'text'；**不改 schema、不加迁移**）。

### D. runtime / hook / 模板
- `runtime.py`：`SOCIAL_TOOLS` 改为 9 个新名字；删除 `mark_read` 方法；`get_state` 返回值改为且仅为：
  ```text
  role_id, role_file, mode, read_through, latest_seq, wake_at, wait_infinite,
  until_condition,   # 动态条件存在时 {members, topics, questions, one_shot, expires_at}，否则 None
  dialogue,          # store.wakes.dialogue_status(key)（不变）
  proactive_paused,  # bool(self.store.proactive_state.stopped(key) 的 stopped 值)，无行时 False
  quiet_hours_active,# is_quiet(binding, now)
  remaining_wakes, remaining_send_attempts, send_ready_at   # 计算方式不变
  ```
  删除 judge、base_wake、dialogue_config、proactive 对象、activities、followups、learning、quiet_hours 对象、max_sends_per_hour、tool_permissions、seen_through、wake_config。
  因此不再使用的 import/属性引用随之删除（例如 `self.judging.intent` 若仅此处使用，只删调用，不删 judging 本身）。
- `hook.py` `before_execute_tool`：`_publication_attempted` 的条件改为 `tool_call.name == "social_send"` 或现有 message 分支（删除 social_sticker send 分支）。hook 中修复提示的英文文案不改（仍提到 social_send / social_wait，名字有效）。
- `nanobot/templates/telegram/social_protocol.md` ← `.agent/tasks/P8-b.social_protocol.md` 逐字节复制。
- `nanobot/templates/telegram/autonomous_activity.md` 第 18 行 `check social_read_messages between longer steps` → `check social_state(action="read") between longer steps`，其余不动。

## 要做的事
1. 按 A–D 实施。
2. 测试（全部在 `nanobot/channels/telegram/tests/` 与 `tests/` 内）：
   - 机械改名/改调用：P8-0 报告 §5.5 与 §6 列出的全部引用（旧 get_state → `social_state(action="get")`，read_messages → `action="read"`，get_media → `social_media`，sticker send → `social_send(sticker_id=...)`，set_wake → `social_wait(..., until_condition=...)`，mark_read 见下）。逐文件列出改动。
   - 依赖已删除 get_state 字段的断言：改为新字段或删除该断言，逐条列出。
   - `test_social_scenarios.py` 只允许改这些位置：
     - set_wake 的三个场景（约 :80、:96–99、:115）：`chat.tool("social_set_wake", senders=["456"], on_messages=False)` → `chat.tool("social_wait", infinite=True, until_condition={"members": ["456"]})`，`last_tool_result("social_set_wake")` → `"social_wait"`。其余断言不改；若因此失败，停下报告，不要改断言。
     - mark_read 的两个场景（约 :268–271、:287–290）：删除 `script_model(... social_mark_read ...)` 与对应结果断言，改为在第一个 `tick()` 后断言该 scope 的 `read_through >= 1`（用 harness/store 现有读取方式）；其余断言不改。
     - sticker send（约 :356–359）：`social_sticker(action="send", ...)` → `social_send(sticker_id=...)`，结果查找改名。
   - 新增测试（放在最贴近的现有文件，不新建测试文件）：
     1. parts 混合 `[{sticker_id}, "短句"]`：两行入队，kind 分别为 sticker/text；按序发送，贴纸段用 sticker_sender 发 file_id、文本段用 sender；贴纸 uses 计数 +1；同一调用重复返回相同回执；用量扣 2。
     2. `social_send(sticker_id=..., followup_id=...)` 预约 followup（与文本发送一致）。
     3. `social_wait(seconds=30, purpose="messages", until_condition={"members": ["456"]})` 写入条件且等待生效；`until_condition={}` 清除条件。
     4. `social_state(action="set_proactive", paused=True)` 在 `allow_ambient_wake=False` 的绑定下也成功并持久。
     5. 注册的 social 工具名恰好是 9 个新名字；每个 description ≤400 字符。
     6. `social_state(action="get")` 返回的键集合恰好等于 D 中列出的集合。
   - 冻结的 V5 基线与 `evaluate_*` / `scripts/` **不修改**（已知失效，留给评估脚本迁移）。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不为旧工具名做别名或兼容层；不保留被删方法的转发。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里。
- 预期规模：生产代码净减少（工具层删 4 个类、get_state 缩短约 25 行）；新增的 SendPart 解析与 set_conditions/set_proactive 拆分不应超过 60 行。

## 允许改动的路径
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/channels/telegram/social/{waiting,delivery,store,models,runtime,hook}.py`、`nanobot/channels/telegram/social/db/deliveries.py`
- `nanobot/templates/telegram/social_protocol.md`、`nanobot/templates/telegram/autonomous_activity.md`
- `nanobot/channels/telegram/tests/`（不含 evaluate_*、fixtures、test_social_v5e_replay.py）、`tests/agent/test_telegram_social_contracts.py`、`tests/cli/test_gateway_runtime.py`

## 不要做
- 不改 judge（`judge.py` 读取的 keywords/senders/on_question/one_shot/expires_at 字段名不变）、schema、配置 schema、`message` 工具、`tool_contract.md`。
- 不改 social_memory/jargon/expression/activity 的请求模型与执行，只换 description。
- 不改 `_deliver`、`retry_due`、`_check_send_allowed`、`_collect_new_messages` 的逻辑。
- 不删 `store.mark_read` 与 `acknowledge_seen`（回合末自动确认仍需要）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider`
- `.venv/bin/ruff check nanobot/`；`.venv/bin/basedpyright -p <你上次使用的显式 venv 配置> nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py` 0 errors（同时贴默认命令结果）。
- `cmp nanobot/templates/telegram/social_protocol.md .agent/tasks/P8-b.social_protocol.md` 无输出。
- `rg -n 'social_(get_state|read_messages|mark_read|set_wake|get_media)\b' nanobot --glob '!**/tests/**'` 无输出；对 tests 同样搜索，剩余命中只能在 evaluate_*、fixtures、test_social_v5e_replay.py。
- 行为变更说明（报告中写）：工具集合与参数的变化；暂停主动联系不再受 allow_ambient_wake 限制；social_state(get) 的字段变化。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
