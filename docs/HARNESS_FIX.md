# Telegram Social Harness 修正记录

日期：2026-10-31。先核对根目录 `HARNESS_PROMPT_REVIEW.md`，再核对源码、容器挂载、实例文件及配置；审计不是运行事实的权威来源。

## A. 修改清单

| 文件 | P 编号 | 修改及原因 |
|---|---|---|
| `nanobot/templates/agent/tool_contract.md` | P023/P029、P033/P040 | 通用重试建议服从发送回执；明确普通模式 final 回复与 Social 工具公开发送的适用边界。 |
| `nanobot/templates/agent/identity.md` | P002 | Dream 管理的 SOUL/USER/MEMORY 与绑定角色文件分开；人格不能扩大工具/工作区权限。保留 Dream 写入约定。 |
| `nanobot/channels/telegram/social/runtime.py` | P033–P040、P049–P056 | 明确 Social 发送规则优先级；回执/作用域/unknown 不重发；删除“不要习惯潜水”的发言压力及重复台词示范；idle 查看时间、实时事项和近期确认发言，可以沉默。新增普通发送与 social_send/sticker/分条队列共用的 scope 锁和预留检查。 |
| `nanobot/channels/telegram/runtime.py` | P013/P040、P200 | 已证实普通 message 绕过 social 额度/冷却；在原回执发送路径接入同一检查。普通队列发送到已启用 Social 的目标也走回执路径，topic 不变，不自动重试 unknown。保留附件、按钮和跨会话能力。 |
| `nanobot/channels/telegram/social/store.py` | P013/P040 | 复用原 actions/send_attempts，无 schema 迁移；普通 message 的每个文本分块及附件预占一个额度，整批放不下时在平台发送前拒绝。额度预占失败不写入部分预占。 |
| `nanobot/templates/telegram/social_judge.md` | P067/P074/P079/P085 | 补历史缺口与旧消息解释；名字重叠不等于同一对象；修正空来源例外与 schema 的冲突。保留 v7 的其他边界和完整 JSON 协议。 |
| `docs/harness/groupmate.md` | P057 | 可审查、可同步的实例人格正文：账号/AI 群友/独立参与/沉默在前 600 字符；鲸鱼娘气质、可不同意、中文口语、真实性。无工具参数、额度、状态机或 V3/V4 流程。 |
| `docs/harness/instance.patch` | P010–P019/P057 | 四份实际外部文件的最小补丁，见下表。它不是初始化所有实例的默认人格。 |
| `nanobot/channels/telegram/tests/test_harness_prompts.py`、`fixtures/social_harness.json` | P002/P029/P057/P059–P087 | 11 个有预期及原因的离线输入，验证 schema/组装/mock 回执；真实 600 字符摘要、普通模式隔离。明确不测语义准确率。 |
| `nanobot/channels/telegram/tests/test_reply_reliability.py` | P013/P033/P040 | 双向切工具的额度/冷却、跨重启 unknown、topic 拒绝不转群、长消息不能绕额度。更新原来记录“不计额度”的特征断言。 |
| `nanobot/channels/telegram/tests/test_social_followups.py` | P036–P039 | 改写同一 ID 的 unknown 事项跨重启仍阻止重发，重复确认回执只计一次；既有跨工具测试遵守真实冷却。追问子系统实现本身不改。 |
| `nanobot/channels/telegram/tests/evaluate_social_judge.py`、`evaluate_social_v4.py` | P057/P059–P087/P286–P296 | 原有真实模型验收可用 `--persona` 注入本次角色，避免测到旧的简化替身人格。 |
| `scripts/pipeline.sh` | 验证 | 把新测试纳入既有 Ruff 检查；既有 pytest 目录发现会自动运行。 |

实例根目录经 Docker mount 验证为 `/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data`，挂载到 `/data`：

| 外部相对路径 | 修改 |
|---|---|
| `workspace/SOUL.md` | 通用身份/表达原则；Social Tool Strategy 缩为当前协议引用与必要约束。去掉 6/h、固定目标、换工具绕限流、失败转整群、去重后改写、queued 改写重发、seconds-only 等旧策略。群聊人格不再放在共享 SOUL。 |
| `workspace/USER.md` | 保留有来源的成员偏好和资料；把工具操作/身份命令改回偏好事实。 |
| `workspace/memory/MEMORY.md` | 删除过期额度、固定目标 fallback、关键词本地唤醒、过期 wait/夜间运行建议。保留账号、成员事实及完整旅行/事项资料，明确历史资料不是操作命令。 |
| `telegram-social/roles/groupmate.md` | 同步审阅后的人格正文。 |

没有改 `config.json`、Telegram API、消息目标解析、数据库结构、模型、temperature、reasoning effort、V4 随机概率/时间或工具启用权限。唯一发送行为修正是已证实的普通 message 绕过保护问题。

## B. 冲突处理与权威来源

| 层级 | 唯一主要职责 / 核对结果 |
|---|---|
| identity | 环境、路径、外部内容与权限边界。角色文件可编辑不等于 Dream 文件可任意写。 |
| AGENTS | 工作区工作约定；实际实例文件是 cron/heartbeat 指引，无本次工具策略冲突，不改。仓库根 AGENTS 是开发规则，不是实例模型输入。 |
| SOUL | 跨会话通用身份/表达；变化中的操作参数交给运行时。 |
| USER | 有来源的成员资料、偏好；不维护工具协议。 |
| tool_contract | 普通工具契约；普通 final 输出只适用于普通模式。失败换方法不能覆盖发送保护。 |
| SOCIAL_PROTOCOL | Social 操作规则及普通发送指引的模式覆盖；工具 schema/实际回执/运行权限决定可执行操作。 |
| groupmate | 当前绑定会话的人格、语言、互动偏好；不授予权限。 |
| V3 | 只决定当前消息批次是否值得交主模型；wake 允许沉默。 |
| V4 | 本地 idle 机会直接进入主回合的语义指引；不是每次必须发送。 |
| MEMORY | 可更新历史资料；不能成为当前配置或操作命令。 |

V3 原文定位（修改前行号与审计一致）：

- `social_judge.md:22`（P079）允许没有成员消息的 opportunity 使用空来源；`:28`（P085）却无条件要求 wake 有来源。`JudgeDecision.validate_for()` 实际只对 **opportunity 且没有 displayed member** 放行空列表；末段改为同一例外，未改变 schema 或 V4 路径。
- `:17`（P074）已有“similar human names 不自动呼叫”，真实回放仍曾把“肥鱼王”误判成机器人。补充“昵称子串不足以证明同一账号”，未增加本地关键词匹配。
- `:10`（P067）要求分清新旧 seq，但缺少 history_truncated 的语义。加一条对缺口/旧邀请的解释；主协议同步禁止把短快照当新近事件证据。
- `:9` 与 `:18` 对 attention 过期的说明侧重不同，未因看似重复就删掉；媒体、选择性等待、取消等待、意图 fulfilled、旧消息通过新语境重新相关等规则都保留。

审计滞后的重要发现：原 `TelegramChannel.send_confirmed()` 注释明确写着 “without social budgets”，原回归还断言普通发送不计入 send_attempts。这与审计“message 已共用额度”的判断相反。本次用既有表和锁补齐，并由真实通道替身测试双向切换；不是仅靠提示词宣称限制存在。

Social Memory 核对：`SocialFollowups.update/reserve/receipt` 已按 memory_id/turn_id、逐条回执执行计数及不可重发；改写不清零，failed 不当作 confirmed，unknown 保留预留，确认回执幂等；两次上限及 completed/responded/dropped 已落代码。`SocialMemory._retire/_evidence` 对退役内容和证据建立 suppression，实时索引不再注入退役事实。新增覆盖，不重写子系统。

## C. 验证记录

离线集 `fixtures/social_harness.json` 中每例有 expected 和 reason，包括点名、直接回复、群友互问、开放邀请、无需回复分享、过时消息、待追问回答、拒绝追问、图片语境、缺口、引用旧点名。旧消息/重复消息的本地不调用、图片原生输入、等待参数三选一另由既有 runtime/judge/media/wake 测试覆盖。

执行命令（均在仓库根）：

```bash
scripts/pipeline.sh test
.venv/bin/python -m pytest -q nanobot/channels/telegram/tests/test_harness_prompts.py nanobot/channels/telegram/tests/test_reply_reliability.py nanobot/channels/telegram/tests/test_social_followups.py
.venv/bin/python -m basedpyright nanobot/channels/telegram/runtime.py nanobot/channels/telegram/social/runtime.py nanobot/channels/telegram/social/store.py
```

真实模型验收使用既有独立 OAuth 和临时 SQLite/合成对话，不启动 Telegram 接收器，不向真实群发送测试消息：

```bash
OAUTH=/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth
CONFIG=/home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/config.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir "$OAUTH" --persona docs/harness/groupmate.md --output /tmp/harness-judge-v3c-reviewed.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge --oauth-dir "$OAUTH" --persona docs/harness/groupmate.md --cases nanobot/channels/telegram/tests/fixtures/social_judge_participation.json --output /tmp/harness-judge-participation-reviewed.json
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_v4 --config "$CONFIG" --persona docs/harness/groupmate.md --case silence --case idle_pending_thought --case old_story --case chat_invitation --case explicit_end --output /tmp/harness-main-v4-final.json
```

V3 使用 `openai-codex/gpt-6-luna` / low / 既有 temperature=0。主模型使用 `deepseek-flash` / 0.1；既有 V4 验收器将单次输出上限设为 4096，实例仍是 8192，未修改实例参数。验证的是合成语境，不能作为真实群长期准确率或人格效果提升的证据。主回合的普通 message 是替身发送；实际通道的共享限制由离线通道测试验证。

过程中的失败也保留：旧测试“不计额度”断言及忽略冷却的跨工具用例在接入保护后失败，已改为新契约；新测试的水位构造/字典访问错误已修正。首轮主回合聊天样例出现无依据“刚开群”，补缺口边界后复测未再出现；中途 V3 similar_name 20/21，补名字边界后完整重跑，不以挑选单例重试掩盖失败。

最终数值和部署验证见本文件末尾的执行结果。

## D. 部署与回滚

仓库模板/代码需要构建新镜像并重启；外部四份文件不会由镜像自动覆盖，须另行同步。当前加载不存在自定义 identity/tool_contract/judge override。角色在后续会话加载，共享 profile 也在后续上下文加载；统一重启让 prompt、回执日志和上下文修订同时生效。

`instance.patch` 只针对核对过的文件，先在私有临时副本 `git apply --check`。运行实例停止期间，profile/MEMORY 维护通过 `MemoryStore.build_dream_tools()` 的原有精确文件写入白名单执行，先读后精确替换、dry-run，再写入；角色另用限制到 roles 目录的普通文件工具。此维护不把写入权限授予运行中的 social 人格，也不让普通模型通过 exec 改长期记忆。维护只合并已验证的操作知识更正，不运行开放式 Dream 去重写旅行资料。

复用 `scripts/pipeline.sh build` / `deploy <revision>` 的构建、全实例备份、健康检查及失败回滚；外部编辑前另存原文件备份并核对源值。发布失败同时恢复外部文件；代码 rollback 不应被误认为一定撤销外部配置编辑。

不会修改另一个验证实例。不会把源码已修改或补丁已生成写成实际实例已生效。最终运行版本、外部文件同步、备份和健康结果在执行后单独记录。

## E. 范围与后续事项

- 人格及语义选择仍依赖模型。小样本观察到能沉默、自然续聊、回忆旧话题和停追问；不能保证未来不模板化或误判。
- 同一 ID 的计数、去重和状态迁移由代码保障；将同一语义事项故意写成新 ID 的识别仍需模型遵守稳定事项规则。未新增语义聚类或记忆系统。
- M4 suppression 只覆盖 social 索引及证据，未把删除传播到共享 MEMORY、Dream 归档或 Telegram 原消息。旧摘要不得当新证据，已有权限/来源检查保留；跨归档语义遗忘属于后续范围。
- idle 没有独立的“已聊话题”语义索引：同一事项 ID 和近期确认发送能抑制重复，但不能证明所有跨话题改写都不重复。后续先观察实际回放，再决定是否需要调度改造，本次不加复杂调度器。
- 本次未执行真实群公开发送、原生图片的真实模型新验收、全部 17 个 V4 在线场景，也未做统计显著性/长期效果实验。既有图片、状态机和所有 V4 本地回归由管线覆盖。
- 普通 message 的多分块/附件额度在发送前整体预占；中途平台失败时不返还未发尾部的预占，保持保守限流。confirmed 追问次数仍仅按真实回执确认的事项尝试计一次。

## 执行结果

- `scripts/pipeline.sh test`：通过；后端 1168、provider/OAuth 155、前端 96 文件/1553；Ruff、WebUI lint/build 均通过。原有 Python 警告和前端测试环境警告未隐藏。
- 最后增补及修改后的定向 pytest：74 通过（10.68s），覆盖全管线初次收集后补入的两例；完整管线未因纯提示词微调再重复运行。
- 修改的三个 Python 实现文件 BasedPyright：0 errors / 0 warnings / 0 notes。最终 Ruff 及 `git diff --check` 通过。
- 真实 V3 最终完整回放：21/21 + 8/8；真实主模型五例：5/5 结构检查通过。可审查的合成输入预期、判断及公开发送替身输出见 [validation.json](harness/validation.json)。这不是模型的长期语义准确率。
- 实例补丁私有副本 `git apply --check` 及 Dream 工具白名单 dry-run 通过。实际同步/发布结果在完成后追加。
