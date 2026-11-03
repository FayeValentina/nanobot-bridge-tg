# 任务 P0-b：建立黑盒场景测试套件（重构安全网）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode” 和 `.agent/orchestration.md`）。

## 背景

`docs/SOCIAL_ARCHITECTURE_REVIEW.md` §3：现有社交测试有 732 处直接 `db.execute`，把表结构焊死了，后续重构会让它们大面积失败，
而这些失败并不能说明行为错了。我们需要一套**只看外部行为**的场景测试，作为 Phase 1–9 每一步的验收依据。
这套测试会在整个重构过程中存活，所以它的设计目标是：**重构内部结构时，只需要改 harness，不需要改场景。**

## 要做的事

1. 新建 `nanobot/channels/telegram/tests/social_harness.py`，实现一个 `SocialHarness` 类。所有对内部实现的了解都集中在这个类里：
   - 构造：临时目录、角色文件、`TelegramSocialConfig`（coalesce、冷却等设为 0 或很小，便于测试）、**可控时钟**
     （runtime/store 构造参数 `now=` 已支持，harness 提供 `advance(seconds)`）、`MessageBus`、假 sender（记录发出的文本和贴纸，可编程让第 N 次调用抛
     `SocialRateLimitError`、`SocialDeliveryUnknownError` 或成功）、真实的 `AgentLoop` 加脚本化主模型 provider
     （参考 `test_social_runtime.py::agent()` 的写法）、judge 使用 `conftest.py` 已有的 offline fake。
   - 输入动作：`member_says(text, *, sender="123", directed=False, reply_to=None) -> message_id`、`advance(seconds)`、
     `tick()`（运行 `run_due`，并把 bus 上的 wake 交给 AgentLoop 处理完）、`script_model(*responses)`（安排主模型下一回合的
     `LLMResponse` 和工具调用序列）、`edit_role(text)`、`set_proactive(...)`（修改 binding 的 proactive 配置）、`restart()`（关闭后用同一目录重建 runtime，并调用 `recover()`）。
   - 观察结果（只返回外部可观察的事实，不返回表行）：`sent()`（按顺序发到群里的文本或贴纸列表）、`wakes()`（发布给主模型的 wake 列表，包含 reason 或 opportunity kind）、
     `last_tool_result(name)`、`model_calls()`（主模型回合数）、`model_saw(text) -> bool`（某段文本是否出现在主模型最近一次收到的消息里）。
   - harness 内部**可以**读写 store/db；场景文件**不可以**。
2. 新建 `nanobot/channels/telegram/tests/test_social_scenarios.py`，只通过 `SocialHarness` 的公开方法编写以下场景。每个场景一个测试函数，函数名描述行为：
   1. 普通群消息 → 被唤醒 → 主模型 `social_send` → 群里出现回复 → 之后 30 秒内的追问不需要 @ 也能唤醒主模型。
   2. judge provider 超时或抛异常 → 仍然唤醒主模型（fallback）。
   3. 主模型设定选择性等待（`social_set_wake` 只等某个 sender）→ 其他人的无关消息不唤醒 → 该 sender 发言后唤醒。
   4. 选择性等待期间，有人 @bot → 唤醒。
   5. `social_send(parts=[a,b,c])`：第 2 段被限流 → 稍后自动重试 → 最终三段按顺序发出，且没有重复。
   6. 发送结果为 unknown 后，主模型在后续回合用同样的文本再发 → 被拒绝，群里只出现一次。
   7. 冷却期内第二次发送被拒（用 config 设一个非零 `sendCooldownSeconds`）。
   8. 主模型 `social_wait(sleeping=True)` 后，新消息仍然经 judge 评估并能唤醒。
   9. idle 主动机会：群里安静超过 idle 阈值 → 产生 `kind=idle` 的 wake；在这个 wake 被领取前来了新的群消息 → 主动机会被取代，主模型收到的是会话 wake。
   10. **activity 开启时 idle 仍能触发**：这是已确认的 bug（审查文档 §5.2），当前代码会失败。用 `@pytest.mark.xfail(strict=True, reason="P0.5: activity shadows idle, see SOCIAL_ARCHITECTURE_REVIEW §5.2")` 标记。
   11. 跟进上限：一个 pending 事项用 `followup_id` 主动发过两次之后，第三次带同一 `followup_id` 的发送被拒。
   12. **修改角色文件后，下一回合使用新 persona，且之前的群聊历史仍在上下文里**：当前代码会截断历史（审查文档 P0-7），用
       `xfail(strict=True, reason="Phase 2: config/persona change must keep history, see P0-7")` 标记。新 persona 生效这一半可以单独写成一个通过的测试。
   13. 重启恢复：一个 wake 已排队但未被领取时重启 → 重启后仍会被处理；一个发送在 `sending` 状态时重启 → 变为 unknown，不会被自动重发。
   14. 贴纸：导入一个假的贴纸集（假 `sticker_set_loader`）→ annotate → `list` 能搜到 → `send` 后群里出现该贴纸。
   15. 普通 `message` 工具发往社交群 → 计入同一个每小时发送配额（配额设为很小的数，用 `message` 工具发满后，`social_send` 被拒）。
3. 时间：整个文件在本机运行时间 < 30 秒。不要用真实 `sleep` 等待超过 0.1 秒，靠可控时钟推进。

## 允许改动的路径

- 新建：`nanobot/channels/telegram/tests/social_harness.py`、`nanobot/channels/telegram/tests/test_social_scenarios.py`
- 如果确实需要，可以在 `nanobot/channels/telegram/tests/conftest.py` 增加 fixture，但**不要改**已有的 `offline_intermediary` 行为。
- **不允许修改任何生产代码。** 如果某个场景在不改生产代码的情况下无法测试（例如时钟无法注入到某处），就在报告里说明，并用 `pytest.skip("reason")` 暂时跳过，不要绕过。

## 不要做

- 场景文件里不得出现 `db.execute`、`store.`、表名、`_` 开头的私有属性。
- 不要断言日志文本、内部状态值、表列。
- 不要修改或删除已有测试。

## 完成标准

- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q` 的结果为：除标记了 xfail 和明确 skip 的场景外全部通过；
  两个 xfail 场景以 XFAIL 结束（不能是 XPASS）。
- `grep -nE "db\.execute|\.store\.|\b_[a-z]" nanobot/channels/telegram/tests/test_social_scenarios.py` 没有输出（`__` 魔术方法除外）。
- `.venv/bin/ruff check nanobot/channels/telegram/tests/social_harness.py nanobot/channels/telegram/tests/test_social_scenarios.py` 通过。
- 已有社交测试不受影响：`.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过（编排者会复跑整个目录）。

## 报告格式（最终回复只包含以下内容）

1. 改动摘要（harness 的公开方法清单 + 每个场景一行说明）
2. 实际运行的命令及结果（原样贴最后几行，包括耗时）
3. 与简报的偏差及原因（尤其是 skip 的场景和原因）
4. 未解决的问题 / 需要编排者决定的事项
