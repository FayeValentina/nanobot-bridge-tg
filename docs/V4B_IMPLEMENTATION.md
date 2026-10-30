# V4-B：未完成交流、事项计数与答后接话

2026-10-30，已完成 [V4 计划](MIGRATION_V4_PLAN.md) 的 B 批次并发布到本人实际实例。
实际镜像为 `9e75871d5e0559ae96eb36f8784b9e1f7a744b0a`，schema 13，social-judge-v5。
V4-A 的随机冷场周期及本人预设保留；完整体验收敛仍属 V4-C。

## 记忆与事项契约

扩展现有 social_memory，沿用 memory_id、证据、revision、TTL、确认/纠错/删除和 scope。
新增的字段使用工具现有的 snake_case 风格：pending_question、pending_thought、motivation、followup_at、
followup_state、state_reason。它们分别对应计划中的 pendingQuestion、pendingThought 和跟进状态。
状态为 active / responded / completed / dropped；mentioned_at、responded_at、origin_turn 由运行时记录。

成员事实仍用 record/confirm/correct 与实际展示过的 message_ids。可在 topic 上使用
`action="followup", memory_id=...` 保存自身待追问，不修改原事实与证据。
自身想法可用 `action="record", kind="pending", origin="self"` 创建，提供 content、pending_thought
与 motivation；自身问题也可使用 pending_question。不为自身意图伪造成员证据，来源回合单独保留。
改写待说内容继续使用 followup 操作及原 ID；一般 revision 更新不清零尝试。

主回合的有界实时记忆索引展示进行中事项、来源、问题/想法、动机、适宜时机及尝试/未决发送状态。
完成、删除、过期、放下的自身意图退出活动建议；仍有用的成员 topic 事实可保留，但不再注入旧待追问文本。
完整记录可通过 list + memory_id + include_inactive 检查。
V3 输入另有最多 4 项的紧凑摘要，配合 Bot 确认消息识别无点名的短回复；输入预算不足时缩减摘要。

## 发送关联与两次上限

social_send 和普通 message 都接受可选 followup_id，关联主动追问/分享所属的稳定 memory_id。
普通答复省略此参数；所有普通工具、技能、MCP 和跨会话能力保持原权限。

同一事项/回合中的分条视为一次尝试，每条仍保存原生 action/delivery ID 和独立回执。
仅平台确认成功使 confirmed_attempts 增加；queued/sending 只预留，failed 释放名额，unknown 占未决名额且阻止自动重发。
部分发送成功后结果未知，会计一次确认尝试，同时保留 unknown，不重发已确认前缀。
重复回执按同一发送 ID 更新，不重复计数。第二次确认后，事项自动 dropped / attempt_limit，退出建议及概率加权。
回应后标为 responded，正常对话答复不受主动次数限制；新未完成事项需要明确的新意图/证据。

发送和预留同事务保存；无效事项不能留下待发但未关联的分条。分条发送前重新检查状态、TTL、修订号、
适宜时机及停止联系状态，纠错/完成/删除后不会继续发送旧内容。
实际发送及不确定恢复仍复用原队列/回执；不增加发送器或定时服务。
工具结果或普通发送 detail 明示 remaining_attempts 与未决状态，social_get_state 还展示近期事项
（包括放下原因）、回合/机会 ID 和原生发送回执的有界关联记录。

这不是自由文本语义去重：模型仍须正确关联事项 ID。未关联普通文本不会被声称已受语义两次上限保护。
原样重复的 record 采用原有直接文本比较，原 ID/计数保留；不采用哈希、指纹或自动改写识别器。

## 概率与连续性

V4-A 已冻结的 pendingThoughtMultiplier=1.4 现已接入：只对有效、已到适宜时间、有待说内容、
仍可尝试且无未决发送的 active 事项加权。先乘并封顶 1，再执行近期发送、夜间和一小时发言修正。
完成、放下、过期、两次用尽和未知发送均不持续加权。

可信协议鼓励公开发送后 `social_wait(purpose="messages", seconds=30, on_timeout=false)`。
主回合已确认发送后若未选择其他等待/睡眠，运行时补上同一后台等待；迟到的分条确认也能续接。
上限取 30 秒与现有 maxWaitSeconds 的较小值，显式选择性的等待不被覆盖。
等待释放会话锁，成员消息继续由 V3 判断；无回复到期只在本地结束，不轮询主模型。
主请求冷却和预算保持原值，因此短回复主回合仍可能等现有冷却到期。

## 升级、验证与范围

SQLite schema **12→13**，在原 owner 中添加 memory_followups（一对一附加字段）、followup_attempts
和 followup_sends。未建立第二份人格/成员档案；原 memories/evidence/source 消息和主水位保留，
不把旧 pending 自动猜测成新意图。升级失败回滚事务，降级需恢复匹配整实例备份。
当前协议为 social-judge-v5；中间模型/主模型/OAuth 和本人 V4-A 配置不改。

新增确定性用例覆盖来源、生命周期、倍率、两次上限、改写/重启、失败/未知/部分成功、两组分条、
普通 message、失效队列、短回复、无回复零调用、多话题、停止联系及迁移回滚。
26 项新增定向用例通过。完整原 release 通过后端 1121 项、provider/OAuth 155 项、
前端 96 文件 / 1553 项、Ruff、WebUI lint/build、wheel/镜像冒烟与健康检查。
修改运行模块严格类型 0 errors / 0 warnings。

实际主模型 deepseek-flash，temperature=0.1、reasoning_effort=null，合成验证 max_tokens=4096。
5 项实际模型样例通过：自身问题/动机、待说想法/动机、关联事项主动追问、短回复后正常接话、两次无人接后结束。
累计 55,619 tokens，样例各耗时约 2.0–6.2 秒；本轮源端样例未单独记录模型调用次数，脚本已补充采集供安装轮及后续验收使用。
示例实际输出包括“明天就出发啦？几点集合，去哪儿玩？”和“三点挺好，跟朋友一起更热闹。打算去哪玩？”。
保存意图的两例均未公开发言；结束例只确认“好，你先忙，回头聊。”，没有第三次追问。
验证命令：`python -m nanobot.channels.telegram.tests.evaluate_social_followups --config <配置路径> --output <报告路径>`。
输入为非私有合成对话，输出发送使用替身，不启动 Telegram 接收者、不向真实群发探测消息。

安装包内另验证主动追问与短回复两例，均通过，导入路径为 `/opt/venv/lib/python3.11/site-packages/`。
每例主模型调用 2 次，分别使用 9,660 / 10,172 tokens，主模型阶段耗时约 3.66 / 2.67 秒。
短回复还经过实际 openai-codex/gpt-6-luna（low）一次调用，1,486 tokens / 2.56 秒，
明确判定“下午三点，跟朋友一起”为待续对话并唤醒主模型；随后主模型标记回应，普通接话未增加旧事项主动计数。
该安装轮合计 4 次主模型 + 1 次中间模型调用、21,318 tokens。
验证入口按 gateway_runtime 规则推导独立 OAuth 目录，沿用原凭据；未修改配置或重新登录。
测试脚本的目录推导/有限重试驱动修正随验收记录提交，不改变已安装运行模块。

实际库 12→13、quick_check / foreign_key_check 和源值核对通过：发布备份中的 119 条消息、
5 条记忆、6 条证据及主 seen/read/next 水位保留；7 份角色/工作区 Markdown 与 16 份 JSONL 对话源保留。
config.json 与发布备份原始字节相同，模型、OAuth 设置、角色、群范围、额度和 V4-A 预设均保持原值。
运行 epoch/context/归档位置、WebUI 派生索引和 dream/heartbeat 系统任务登记仍按既有启动逻辑刷新，
不能将这些运行元数据宣称为逐字不变；原始对话正文仍在。

容器 healthy，Telegram 已连接，网页/健康端口仍为 8765 / 18790。
上一镜像为 `af33a0c6226a276c935e85171948b6f9f0dd685c`，整实例备份为
`backups/1793364494923407510/`。回滚使用原管线恢复该镜像和匹配数据/配置。

这些样例检验事项关联和连续性，不保证每次措辞都自然；V4-C 的扩大语义样例、有限搜索、成本/体验收敛仍未实施。
