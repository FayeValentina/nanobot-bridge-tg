# V5-A：基线与数据接线

2026-10-31。V5-A 源码已实现，发布结果在本文末尾记录。V5-B/C/D/E 未实施；本批不抽取表达、不研究词义、不选择或注入回复候选，也不宣称自然度已经改善。

## 实际基线与本批范围

开始时源码 `1f3637ff4f3c395f17cc0aa6f40d9e769ca3d4f5`，运行镜像 `41a25e72cbb9e1637034109218409574a618d017`，schema 13、social-judge-v8、主协议 5。只读核对 `memories.kind='slang'` 为 **0**。首次快照：3 scope、293 条消息、11 条记忆、3 条证据、121 张贴纸。工作期间用户继续正常使用；升级副本验收时为 308 条消息、12 条记忆、4 条证据、240 张贴纸。两次快照数目不同不是迁移产生的数据。

实际配置解析为 `deepseek-flash`、temperature **0.1**。本批不改主模型、V3 模型、凭据、角色正文、Dream/共享资料、参与条件、V4 主动参数或工具权限。学习配置不进入既有人设/V3 scope 版本比较；实际库副本启动验证了 3 个 scope 的版本及主 seen/read 原值不变。

实施入口：`social/learning.py`、`store.py`、`memory.py`、`runtime.py` 和 Telegram 事件适配器；没有向 AgentLoop/Runner 添加群聊专用流程。

## 冻结的数据契约：schema 14

在现有 SocialStore 的唯一 SQLite 连接上，13→14 事务升级。既有表只给 `messages` 增加 `learning_source`、`learning_text`，历史行默认为 `unknown`/空串，不猜测旧消息是否真人、不自动回填历史学习素材。

| 表 | 含义 |
| --- | --- |
| `learning_events` | 每 scope 最多 80 条的素材队列；event ID、message_id、seq、revision、sender、时间、来源、顶层原文和有界语境。唯一键是 scope/message_id/revision，无内容摘要 |
| `learning_cursors` | 表达/黑话分别维护 event ID、最近提交 message_id/revision、提交时间；与主 read/seen、V3 assessed_through 完全独立 |
| `learning_batches` | 每 scope/类型最多一个 ready/running/failed 批次；冻结有界来源、范围、配置/作用域版本、状态、尝试数、原因、计数、模型与用量/耗时字段。终态仅留最近 25 批/类型 |
| `learning_counts` | 排除来源、陈旧事件、采集失败、未入批溢出范围及成功/失败累计计数 |
| `expressions` / `expression_evidence` | 情境、表达、语气、适用/不适用、状态、出现次数、来源修订、选中/确认使用时间、显式条目修订 |
| `jargon` / `jargon_evidence` | 稳定词条 ID、原词/别名、群内义/公开义、用法/例句/不适用、公开链接、推断依据/时间、不确定状态；保留旧工具所需 content/basis/status 契约 |
| `jargon_suppressions` / `jargon_resets` | 独立的纠正/删除来源抑制及清空边界，避免旧证据复活 |
| `learning_changes` | 条目修订、操作来源与原因；来源编辑/删除会记录失效 |
| `expression_turns` | 调用数、候选总数、Token、耗时、状态和目标消息；不保存未选候选详情 |
| `expression_selections` | 实际选中的条目/贴纸 ID、修订，以及明确归因且有真实回执的 delivery ID |

表达和黑话表均包含 `scene_tags` JSON 与 `intent_category`，并有 `(scope,status,intent_category,updated)` 索引；黑话另有 `(scope,status,term)` 索引。`EXPLAIN QUERY PLAN` 验证分类粗筛使用对应索引。标签的语义判定留给 B/C/D，不靠本地关键词自动贴标签，也没有引入 embedding 服务。

## 用户指定的四项简化

1. **零存量直连**：没有历史自由文本转换脚本、旧 ID 映射或双写。`social_memory(kind='slang')` 直接代理到 `jargon`；不传 kind 的统一查询仍合并返回记忆和黑话，确认/纠正/删除可直接用新词条 ID。清空 slang 只清黑话索引，统一清空同时处理二者；原有消息、共享资料与 Dream 不在删除范围内。新库仍沿用原有总条数预算、证据展示要求、确认标准及作用域默认。若另一个数据库实际存在旧 slang，升级明确报错，不擅自转换。
2. **独立轻量游标**：无租约、续期、抢占或积压快照表。现有调度周期只准备素材；每链一个有界 ready 批次，B/C 后续调用 `start`、在同一事务提交条目/证据与 `finish`。零候选也可成功提交；失败不推进游标。尝试在调用前计数，重启保留预算，同批至多两次；耗尽后保留 failed 及原因，不无限重试。A 本身不执行这些模型请求，因此真实游标不会因“入批”而假装已学完。
3. **轻量轮次记录**：每 scope 最近 **25** 轮候选/未选说明在内存 deque，并输出 JSON 结构化日志；`social_get_state.learning.recent_selections` 可实时查询。重启清空这部分详情。数据库只保留调用统计和实际选中关联；`confirm_use` 必须匹配同 scope/turn 的真实 social 或普通 message 回执，queued/unknown 不算使用。选中后发送了别的话，不会自动把全部候选记成已使用；使用也不会增加真人出现次数。D 才接入实际选择与明确使用归因。
4. **无向量粗筛**：分类字段与基础索引已经落库，后续可先 SQLite 过滤再模型精排。

## 来源与调度接线

- Telegram 边界只将 `message.text` 作为真人顶层文字。SELF、其他 bot、转发/via-bot、无顶层文字、媒体 caption 与无法核实来源均不进入范本；含平台块引用实体的文字整条保守排除。其他 bot 的正常 V3 接话资格不变。
- 引用和最多 2 条邻近消息只是 `learnable=false` 的语境，每条素材合计语境最多 600 字符；范本原文最多 1000 字符。工具内容、角色正文和模型图片描述没有采集入口。
- 采集在普通观察事务内、滚动裁剪消息前持久化，不依赖主模型发言；用 savepoint 隔离采集失败，失败保留普通观察/V3 事件并记录错误。现有 scheduler 在聊天唤醒、发送处理后准备学习批次，不新增调度器或更新接收者。
- 新事件不足 10 条时继续累积；一批至多 80 条。每链 ready 批次保存有界原文，因此观察池正常裁剪不会让在途材料消失；未入批材料超过队列上限，按类型记录丢弃消息范围和计数，不称为成功学习。
- 编辑使用原消息 ID 与增加的 revision；相关旧批次取消、已有来源条目失效，新修订进入后批。显式 `delete_observed` 同时失效批次/证据，普通 retention 裁剪不会误当用户删除。Bot API 没有通用群消息删除事件，本批不声称能感知平台未通知的删除。
- scope 撤权/暂停或配置变更取消不再有效的批次；不推进学习或主模型水位。重新启用时由仍保留的有界素材恢复。
- 只读历史、收集、准备、失败和重启恢复均不调用 `note_seen`/`mark_read`，也不安排学习完成后的主模型确认发言。

## Pydantic 配置与预算

新增 `channels.telegram.social.learning`，遵循现有 camelCase JSON 契约与 `extra='forbid'`。运行时游标、Token 或凭据不写入此配置。

| 字段/预算 | 默认值 |
| --- | --- |
| `enabled` | true；受 social 总开关和 scope 授权约束，仅启用 A 的采集 |
| `windowMessages` / `minMessages` | 80 / 10 |
| `excerptChars` / `contextMessages` / `contextChars` | 1000 / 2 / 600 |
| `intervalSeconds` / `concurrency` | 300 秒 / 1 |
| `expressionCandidates` / `jargonCandidates` | 10 / 20 |
| `expression` / `jargon` / `selection` | 各自 provider/model=null，后续复用普通 provider/preset；maxTokens=2048，timeoutSeconds=30，maxRetries=1 |
| 回复表达/黑话/贴纸/字符/调用 | 3 / 5 / 5 / 4000 / 自然轮次 1 次 |
| `selectionHistoryTurns` | 25，可配置范围 20–30 |
| 研究搜索/抓取/字符/复核期限 | 3 / 3 / 12000 / 7 天 |

模型、研究和选择预算是 B/C/D 的明确配置契约，**A 未发出学习/研究/选择模型调用**。状态区分 `enabled=true`、`stage=collection_only`、`extractor_enabled=false`、素材/批次、可用条目、选择与确认使用，避免把开关开启当成效果验收。实际 config.json 不需要重写即可采用这些新增默认值。

## 固定真实片段

`tests/fixtures/social_v5_baseline.json`（位于 Telegram 通道 tests 下）与只读导出脚本 `scripts/freeze_social_v5_baseline.py`：

- 提取集：4 段、39 条消息，其中 **10 条真人文字可作为提取候选**；SELF、媒体和其他来源仅为语境。
- 留出评估集：10 段、61 条消息；**所有行 learnable=false**。两集的完整消息范围，包括引用上下文与历史回复，按 `(scope alias,message_id)` 检查没有交叉。
- 提取片段：猜猜、交通方式自述、身份调侃、纠正日期与照片时间；ID 范围 6838–6852、6874–6879、6891–6908。
- 评估片段：白饭/用量与 peer bot 纠错、分享后事实提问、旧行程与调侃、无请求贴纸/反讽夸奖、token 明确相关、太平洋澡与反问、认真求助、取消计划、问号斗图；ID 范围 6913–7012 中明确列出的十段，未把中间所有历史自动加入提取。
- 群/用户 ID 和 @账号用稳定别名替换；原 message_id、seq、revision、文字、媒体类型、引用关系、可关联到的确认发送轮次保留。真人身份用本次人工核对的群成员名册限定，不据此把旧库所有非 SELF 行自动当真人。
- 回放输入必须截到目标消息，后面的原 SELF 回复仅为基线，不能泄漏进本轮输入。评估不要求“玩梗/发图”；明确 token 话题保留用于防止误伤。

本批冻结的是**真实历史语料和原回复基线**，不是同配置前后模型实验。原回复跨历史版本、图片这里只保留类型标记；完整图片回放须补原媒体，不能让模型猜图。B/D/E 后续使用同一留出集执行 baseline / 仅人设 / 学习库三组受控回放，记录逐例表现、实际调用/Token/延迟并由操作者复核；不把历史输出、结构测试或模型自评分当成 V5 自然度已通过。

## 验证与发布

- V5-A 专项 39 项通过：旁观采集、水位隔离、双游标、scope 隔离、编辑/显式删除、重启、两次尝试预算、事务失败回滚、裁剪/溢出、采集故障、代理与来源校验、Ring Buffer、两条发送路径回执、分类索引和分集隔离。
- 实际库只读备份→副本升级：27 张旧表所有旧列直接逐值比较一致；308 条历史记录新增来源均为 unknown；SQLite integrity_check=ok、foreign_key_check=0。没有内容哈希。
- 副本运行时初始化：3 个 scope 的既有版本、主 read/seen 原值保持。
- 相关模块严格类型检查 0 errors / 0 warnings；Telegram/共享发送等回归 847 项通过。完整发布管线和实际安装检查：待发布后补充。

未完成项属于 B/C/D/E：模型抽取/研究、自动条目审核/启用、表达条目管理、当前语境候选精排与注入、实际使用语义归因、人设清理及受控模型效果评估。A 采集到材料或有手动黑话条目均不代表这些后续批次完成。
