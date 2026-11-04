# Bot 主动性修复大纲

状态：大纲（2026-11-05，owner 确认五项都做，细节待后续细化）。实施流程见 [`.agent/orchestration.md`](../.agent/orchestration.md)。

## 现象与证据

线上 11-04 04:42 – 11-05 04:04：主动 turn 63 次（idle 47、activity 16），**发出消息 1 次（1.6%）**。
调度器正常触发，问题在于模型收到机会后几乎总是沉默，且机会常落在无人的 scope 或时段。
activity 笔记本有 28 条活动（鲸咔哒、沙堆、SOFAR 等），笔记里多次写“等有成员在时再发”，但几乎没有分享出去。

## 五项修复

### A. 提示：主动机会默认开口
- 根因：protocol（`nanobot/templates/telegram/social_protocol.md`）“Silence is a normal outcome”“If nothing natural is ready, end silently”“Do not chase old plans”；
  角色文件第 5 条“没话说就安静听着”；工作区 SOUL.md 第 19 条、MEMORY/USER 中 “silence is acceptance… never resend”。模型推理里反复引用这些。
- 约束（AGENTS.md owner 2026-11-01）：闲时自主是休闲，不能变成“找东西发”、提高发言概率或发进度报告，也不能设分享配额。
  因此 A 只去掉过强的沉默倾向，不要求必须发言。
- 方向：idle/activity 段改为“手头有真实的发现、吐槽或群友关心的话题时自然地说出来，不必等到‘完美时机’”；角色第 5 条限定在回复场景；SOUL 第 19 条限定“同一话题”。
- 涉及：protocol（代码仓库）、角色文件与 SOUL（线上数据，Claude 手工改并备份）。

### B. idle turn 注入“可分享素材”
- 根因：idle turn 只看到已读旧消息、空 followups、几乎无 pending 记忆，只能判断旧消息 stale 后沉默（topic 6532 同一条 5 天前的消息被回放几十次）。
- 方向：`SocialContextBuilder` 在 idle turn 注入一个短清单：最近完成/暂停活动的结论与 artifacts、近几天话题记忆、成员兴趣/偏好。控制长度（参考 P9 token 方针）。
- 待定：清单条数与字段、已分享过的条目如何标记避免重复。

### C. 打通活动成果 → 分享
- 根因：activity/idle 只在成员沉默 ≥15 分钟时触发（无人），有人时是回复 turn 且 protocol 禁止追旧计划，成果没有发出去的时机。
- 方向：activity complete/pause 时可生成 `kind=pending, origin=self` 的待分享想法（复用 `pendingThoughtMultiplier`）；成员回来后的首个 turn 允许顺带分享一次（每次回来最多一次）。
- 待定：自动生成还是由模型在 social_activity 中显式标记 `share_candidate`。

### D. 调度：只投给有人、可发送的 scope 与时段
- 根因：topic 6532 无成员发言且不可写，却占 15/63 次；主聊天 scope 17 次（8 小时无人）；idle 无 activity 那样的 `latest_member_scope` 去重；
  `quietHours` 关、`nightMultiplier=1.0`，02–08 点成员只有 3 条消息却落了约 1/3 机会。
- 方向（`social/proactive.py` `tick`）：
  - idle 也走 allTopics 去重，只投给最近有成员发言的 scope；从未有成员发言的 scope 不投。
  - 触发窗口改为“群里刚安静下来”的区间（如成员沉默 15–90 分钟），超出上限不再投 idle。
  - 线上配置：开启 quietHours 或下调 nightMultiplier（配置改动，备份后改）。
- 待定：窗口上限数值；是否按成员历史活跃时段加权。

### E. 沉默的主动 turn 不再压制后续机会
- 根因：模型沉默时常调 `social_wait(1800/3600)`（delivery check 文案“call social_wait or return empty”也在引导），scope 进入 waiting，后续检查判 `deferred`。
- 方向：idle/activity turn 内的 `social_wait(purpose="messages")` 不使 `_deferred` 成立（或 idle turn 结束时复位），delivery check 文案对主动 turn 改为“return empty”。
- 待定：在 `_deferred` 中区分 wait 的来源 turn，还是在 waiting 侧拒绝主动 turn 的长等待。

## 建议顺序与验收

1. 只读清单 PF-0：确认上述行号/调用点、idle turn 实际注入内容与长度、wait 来源可否区分。
2. PF-a：D + E（调度与状态，纯代码，可测试）。
3. PF-b：A + B（提示与注入，行为变更，需 owner 体验）。
4. PF-c：C（活动→分享）。
- 指标：主动 turn 发出率（基线 1.6%）、凌晨机会占比（基线约 1/3）、无人 scope 机会数（基线 32/63）、成员对主动消息的回应率；同时观察是否刷屏（maxSendsPerHour、连续无人回应）。
