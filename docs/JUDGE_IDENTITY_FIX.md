# V3 在多机器人上下文中认错当前账号

2026-10-31。前一批修复让其他机器人的明确 @/回复进入 V3，但用户再次报告肥鱼不回复。本批依据新消息的真实持久记录定位，不把健康检查、mock 或单条简单问候当作实际群聊已恢复的证明。

## 证据与原因

- 群消息 6859“肥鱼回应”已让主模型执行，social_send 获 Telegram 确认，消息 ID 6860。这证明主模型和 topic 发送路径可以工作。
- 随后的另一机器人消息 6862，已存为 eligible=true、directed=true；确实进入 V3，没有被前一批入口再次过滤。
- V3 对该批次返回 observe，理由却写“当前账号是 LunaSea_Faye_Bot”，把对肥鱼的提及当作发给别人。因此没有唤醒主模型，没有新的失败发送或限流。
- 原快照中的 bot_username=Deep_Fat_Fish_bot、bot_name=肥鱼、nicknames 和角色摘要全部正确；缺陷是 V3 的系统指引没有明确锚定被判断的目标账号，模型受历史里另一个账号的称呼干扰。

## 最小修正

- `SocialJudge.system_prompt(snapshot)` 从宿主快照的账号、名字和昵称生成一个很短的 runtime identity block，放在 system 中；不从群友发言、当前发送者或人设散文猜账号。
- V3 模板明确：判断的是该 target bot；mentions_bot、reply.kind=bot、is_self 指向该目标。历史中出现其他机器人账号不会切换被判断的身份。
- 不增加本地强制 wake、不改 schema、模型、参数或 V3/V4 路径。发给其他成员仍可 observe，wake 仍不强迫公开发言，也未增加互聊限制。
- 原评估器支持 `--snapshot` 原模型输入回放、保存的 baseline system、有限重复及期望判断。历史快照只直接送评估模型，不送调度器，不推进生产水位，不重放过时消息到群。
- 新增两个 system 身份组装测试，覆盖不同目标账号，验证不会硬编码成肥鱼或从对话中的 Luna 取身份；参与 fixture 增加多机器人接话正反例。

## 真实输入回放

通过只读 SQLite backup 到临时目录，再调用原 `_restore()` 导出实际失败批次，保留原 30 条消息、昵称、角色摘要、时间和提及/回复信息。未改生产库。完整输入只保存在私有验证目录，不把群聊历史提交进代码仓库。

```bash
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth --snapshot /tmp/fish-judge-failed-snapshot.json --system-prompt /tmp/fish-judge-baseline-prompt.txt --repeat 3 --output /tmp/fish-identity-baseline-replay.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth --snapshot /tmp/fish-judge-failed-snapshot.json --repeat 5 --expected wake --output /tmp/fish-identity-fixed-replay.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth --snapshot /tmp/fish-judge-other-addressee.json --repeat 3 --expected observe --output /tmp/fish-identity-other-addressee.json
```

同一实际失败输入：旧 system 3/3 observe，重现错把“肥鱼”当其他参与者；修正后 5/5 wake，均引用实际新消息 seq 82。只将最新消息收件人改成另一机器人、取消本 bot 提及标志的反例，3/3 observe。以上是有限重复的实测，不是长期准确率，也不保证今后没有其他语义误判。

模型仍是 openai-codex/gpt-6-luna，low，temperature=0，输出协议及上限不改。没有发送真实群探针或伪造 Telegram 新消息。私有源输入和报告在 `/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/verification/judge-identity-20261031/`。

## 本地验证与发布

```bash
.venv/bin/python -m pytest -q nanobot/channels/telegram/tests/test_social_judge.py nanobot/channels/telegram/tests/test_social_judge_runtime.py nanobot/channels/telegram/tests/test_harness_prompts.py
.venv/bin/python -m ruff check nanobot/channels/telegram/social/judge.py nanobot/channels/telegram/tests/test_social_judge.py nanobot/channels/telegram/tests/evaluate_social_judge.py
.venv/bin/python -m basedpyright nanobot/channels/telegram/social/judge.py
```

151 项本地测试通过，Ruff 通过，类型 0 errors / 0 warnings / 0 notes。新组装测试第一次未同步 fixture 的原消息内容，被既有“快照来源改变”校验正确拒绝；完善 fixture 后完整重跑通过，没有放松校验。真实 V3-C 21/21、增补两例后的参与集 10/10 通过。没有重复执行前端全量测试，本批没有 UI 或前端接口变更；部署结果完成后追加。

本批只需源码镜像重建/重启，不修改 config、profile、人格、社交记忆或数据库结构。新模板会通过既有 prompt setting revision 失效旧判断缓存。修复后是否实际公开回复，仍应分别查看新一轮 V3 判断、主模型回合及 Telegram 确认回执。
