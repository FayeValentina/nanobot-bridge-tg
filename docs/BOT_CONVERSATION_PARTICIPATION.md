# Bot 是群聊参与者，不以点名当前账号为参与前提

2026-10-31。用户进一步明确：即使当前判断的是 Luna，也不能仅因对话对象是肥鱼或其他 bot 就拒绝参与；允许 bot 间自然聊天，不增加互聊限制。本记录取代此前“其他 bot 仅在明确 @/回复当前账号才进入 V3”的阶段性策略。身份绑定仍然必要，用来读懂对话，不作为参与禁令。

## 修正

- `TelegramSocialRuntime.observe()` 去掉剩余的 `is_bot and not directed` 资格门槛。授权 scope 中其他 bot 的新消息和人类消息一样进入 V3；自身、重复、旧/未来消息、编辑、暂停和原观察授权保护保留。
- V3 去掉“点名另一成员必须优先 observe”的硬规则。共同话题、成员邀请两个 bot 聊天、已有互相接话，可以在没点名当前账号时 wake；不能冒充对方，也不能把所有成员间的问题当自己的任务。明确封闭的私人对话及拒绝接话仍可 observe。
- 图片参与段同步去掉同类硬禁令，改成不能代别人回答；共同图片话题仍可参与。
- 主 Social 协议明确“分清问题问谁”不意味着“禁止加入”；角色、身份、模型、采样参数、V3/V4 架构、工具权限、发送限制和数据库结构不改。
- 仓库 AGENTS 记录用户的当前偏好，避免后续把已退役的 bot-only 提及门槛加回来。

## 验证

```bash
.venv/bin/python -m pytest -q nanobot/channels/telegram/tests/test_social_judge_runtime.py nanobot/channels/telegram/tests/test_social_scheduler.py nanobot/channels/telegram/tests/test_social_judge.py nanobot/channels/telegram/tests/test_harness_prompts.py
.venv/bin/python -m pytest -q nanobot/channels/telegram/tests/test_social_runtime.py nanobot/channels/telegram/tests/test_reply_reliability.py nanobot/channels/telegram/tests/test_social_followups.py
.venv/bin/python -m ruff check nanobot/channels/telegram/tests/evaluate_social_v4.py nanobot/channels/telegram/social/runtime.py nanobot/channels/telegram/tests/test_social_judge_runtime.py nanobot/channels/telegram/tests/test_social_scheduler.py
.venv/bin/python -m basedpyright nanobot/channels/telegram/social/runtime.py
```

两组 213 + 130 = 343 通过；Ruff 通过；类型 0 errors / 0 warnings / 0 notes。更新的测试覆盖无 @的 bot 消息确实交给 V3，以及 V3 仍能 observe 而不公开发送；超时不轮询的用例改用自身消息，因为其他 bot 现在也是有效外部消息。

真实模型验证（沿用既有独立 OAuth，临时库、替身发送，不向真实群发探针）：

- V3-C 21/21，参与集 12/12。其中新增“@另一 bot，让它与肥鱼聊天”也允许 wake，以及不带新 @的接话。
- 分别以 Deep_Fat_Fish_bot 和 LunaSea_Faye_Bot 为 target，历史正确标记各自 is_self，另一 bot 在共同早餐话题中不带新 @接话：各 3/3 wake。假设的 Luna 身份只用于离线输入，不修改真实账号或角色。
- 既有主回合验收新增 `peer_bot_shared`。`peer_bot_shared` 与 `peer_bot_call` 两例都通过真实 V3→主模型→替身发布，前者的发送者 is_bot=true、directed=false，没有通过本地 @门槛绕测。

```bash
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_v4 --config /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/config.json --persona docs/harness/groupmate.md --case peer_bot_shared --case peer_bot_call --output /tmp/shared-bot-main.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth --persona docs/harness/groupmate.md --output /tmp/shared-bot-v3c.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth --persona docs/harness/groupmate.md --cases nanobot/channels/telegram/tests/fixtures/social_judge_participation.json --output /tmp/shared-bot-participation-final.json
```

首轮旧 fixture 把“小明，没人说话，你陪我聊会儿”绝对规定为 observe；新规则下模型认为存在群聊参与机会而 wake。该旧预期来自已退役的点名排他规则，不能继续当参与禁令。反例改为明确的“私事单独聊，群里先别接话”，保留封闭对话边界；原首轮结果不隐藏。wake 只表示主模型可以考虑接话，并不宣称所有这些句子都必须公开回复。

这些是真实调用的小样本验证，不是长期准确率。没有新增随机唤醒、强制发言、轮数或专用冷却；V3 与主模型仍可保持沉默。

## 部署

需要构建/重启源码镜像，不需要手动同步 config、SOUL、USER、MEMORY 或 groupmate。沿用原构建、备份、健康检查和失败回滚。历史消息不重新伪造成新输入，已确认或 unknown 的发送不重试。本批未重复前端全量测试，因为没有 UI/前端接口变更。部署和安装包检查完成后追加。

### 已完成发布

- 运行源码版本 `6355b7b25520910241fc4323c9f18e3ae1e32b70`，原 build/deploy 和冒烟通过；容器 healthy，Telegram connected，检查时 ERROR=0。
- 安装 wheel 中用原生 Telegram Update 验证：@当前 bot、@另一 bot、没有 @、直接回复都能进入 V3；V3 observe 时保持沉默。自身消息仍不触发，没有真实群发送。
- 安装包内使用实例 provider 配置、持久 OAuth，再回放合成的 Luna target 场景，未被新消息 @仍返回 wake。真实账号仍是肥鱼；该测试不启动另一 Telegram 账号或修改实际角色。
- 143 条消息、6 条记忆、7 条证据逐行保留，config/profile/角色文件原始字节与发布备份一致，数据库完整性和外键检查通过。
- 验证输入与报告位于私有目录 `/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/verification/bot-participation-20261031/`；包含首轮旧预期不匹配及修订后的反例结果。文档后续提交不需要重建运行代码。
