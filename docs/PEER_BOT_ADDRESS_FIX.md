# 其他机器人明确点名肥鱼时的唤醒修复

2026-10-31。用户报告 Telegram 群中肥鱼不回复，随后明确要求允许其他机器人的直接 @/回复，并明确不增加互聊次数或专用冷却限制。

## 实际原因

网关 healthy，Telegram polling 已连接，没有近期 ERROR、发送额度耗尽或新的失败发送。最新成员消息 6857 是发给 Luna 的邀请，V3 判为 observe；这符合当前受话人。随后消息 6858 是另一机器人对肥鱼的明确 @，但被两处原有逻辑阻断：

- `TelegramChannel._observe_social()` 把机器人来源的 directed 强制设为 false。
- `TelegramSocialRuntime.observe()` 把所有 `is_bot` 消息设为不具备唤醒资格。

因此消息被保存，却没有交给 V3，更没有主模型回合或发送尝试。这不是上一批共享发送额度把回复拦住。

另外观察到 `/new` 命令进入了处理流程；其确认文字被现有 tool_only 会话输出抑制。该独立的命令回执行为没有导致 Telegram 收消息或 V3 停止，本批不混入通用命令输出改造。

## 修正范围

- 通道保留从实际提及/回复对象识别的 directed，不再因发送者是机器人清零。
- Social 观察允许 **其他机器人且明确 directed** 的新消息交给原有 V3。未明确点名的其他机器人发言、自身消息、编辑、旧/未来消息、暂停及原观察授权边界保留。
- 没有增加互聊轮数、专用冷却、随机概率、关键词门控或新数据库字段。现有发送额度、回执和去重保持不变。V3 仍可 observe，wake 后主模型仍可沉默。
- 不重放已过时的 6858，不改历史记录或已读水位，不向真实群发送探测消息。

## 验证

```bash
.venv/bin/python -m ruff check nanobot/channels/telegram/runtime.py nanobot/channels/telegram/social/runtime.py nanobot/channels/telegram/tests/test_social_runtime.py nanobot/channels/telegram/tests/test_social_judge_runtime.py nanobot/channels/telegram/tests/test_social_scheduler.py nanobot/channels/telegram/tests/evaluate_social_v4.py
.venv/bin/python -m pytest -q nanobot/channels/telegram/tests/test_social_judge_runtime.py nanobot/channels/telegram/tests/test_social_scheduler.py nanobot/channels/telegram/tests/test_social_runtime.py
.venv/bin/python -m pytest -q nanobot/channels/telegram/tests/test_reply_reliability.py nanobot/channels/telegram/tests/test_social_followups.py
.venv/bin/python -m basedpyright nanobot/channels/telegram/runtime.py nanobot/channels/telegram/social/runtime.py
```

第一组 182 通过；覆盖机器人明确 @、直接回复、未点名不触发，以及连续两次明确点名都能进入 V3、没有新增互聊门槛。新原生 Telegram 测试首次因真实时钟与手动时钟跨秒而失败，现显式使用 fixture 时钟，完整重跑通过。类型检查 0 errors / 0 warnings / 0 notes。

现有真实模型验收器增加 `--case peer_bot_call`：使用 `is_bot=true`、明确 directed 的合成消息，经实际 V3 与主模型执行，所有发布都是替身。保留原模型/参数，V3 1 次、主模型 3 次请求；结构检查通过并生成接话。另用当前人格真实复测“肥鱼呢”“肥鱼在吗”均 wake。结果仅证明这些样例，不代表长期群聊表现。

```bash
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_v4 --config /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/config.json --persona /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/roles/groupmate.md --case peer_bot_call --output /tmp/peer-bot-real-check.json
```

发布与安装包核验结果完成后另行记录；本批不需要同步角色、SOUL、USER、MEMORY 或 config。
