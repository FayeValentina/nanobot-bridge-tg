# V3-A：中间模型契约、调用与持久状态

2026-10-29，范围：V3-A 组件实现与独立验证；消息入口切换属于 V3-B。
本批不接管实际群消息，不把组件就绪表述成 V3 已启用。当前公开回复仍走 V2-B。

## 已冻结的契约

新增 `social.judge` 配置，继续通过现有 Telegram social JSON 设置契约读取；没有第二个启用开关。
V3-A 不使用它改变 V2-B scope 版本，避免新增组件配置清空现有等待/临时条件。

| 项目 | 本批值/含义 |
| --- | --- |
| provider / model | `openai_codex` / `openai-codex/gpt-6-luna`，采用用户确认的本地 Codex OAuth 方向 |
| reasoningEffort | `low`；可选 low/medium/high，后续以实际语境质量与延迟调整 |
| maxTokens | 512；传给现有 provider，不冒充所有后端都支持的服务端硬截断 |
| timeoutSeconds | 15 秒，覆盖一次 provider 调用；同时受批次剩余 TTL 限制 |
| maxRetries / retryDelaySeconds | 1 / 1 秒；每批最多两次；单次 assess 至多发一个请求，重试由持久 due 表达 |
| minIntervalSeconds | 同一 scope 请求最小间隔 1 秒，不占 Agent 锁或睡眠轮询 |
| maxRequestsPerHour | 240，全部 scope 共享的中间请求预算，失败/取消/重试均计数 |
| maxFallbacksPerHour | 6，全部 scope 共享的故障降级预算；0 表示不允许降级 |
| contextMessages / contextChars | 30 条 / 16000 字符；字符上限包含完整序列化快照，提示词另计 |
| batchTtlSeconds | 120 秒；所有时间字段拒绝 NaN/Infinity |

实际目录与官方资料列出的型号是 `gpt-6-luna`，未使用未确认存在的 `gpt-6.1-luna`。
模型适合分类/提取类任务的定位见 [官方模型说明](https://learn.chatgpt.com/docs/models)，
OAuth 缓存及刷新的一般行为见 [官方认证说明](https://learn.chatgpt.com/docs/auth)。

### 输入

`JudgeSnapshot` 是严格、有界且不可变的 DTO，包含：

- scope、运行版本、after_seq、through_seq、新消息 seq、创建/过期时间。
- 按 seq 排序的消息：发言者、时间、正文、Bot 已确认发言、mention 事实、媒体说明。
- 结构化引用 `bot/member/topic_root/unknown`；话题根不是“正在回复创建者”。
- Bot 身份/昵称、必要角色与背景摘要、observing/waiting/sleeping、主模型是否忙、等待意图。
- 上下文截断标志；不复制主模型的完整记忆/工具结果。

输入必须对应同 scope 仍保留的消息和当前版本，不能跳过批次范围内仍保留但未展示的消息。
主模型自身消息只作为历史，不能充当新事件或 wake 来源。V3-A 不修改 Telegram 接收适配层；
真实引用归一化、快照组装及持续调度在 V3-B 接入。

### 输出

```json
{"decision":"wake","through_seq":3,"source_seqs":[3],"reason":"成员正在接着 Bot 的问题说话"}
```

仅接受 wake/observe，水位必须等于提交批次，来源是实际展示的成员 seq，升序去重，理由 1–240 字符。
wake 必须有来源，observe 的来源必须为空。允许新语境让历史消息变得相关，但只能引用当前显示的历史。
代码块、坏 JSON、额外字段、伪造 seq、空理由、工具调用、非最终输出均算错误，不算合法沉默。

使用 `OpenAICodexProvider` 与既有 factory；Codex 请求携带严格 JSON Schema，返回后再做语境水位校验。
独立复制配置以隔离主模型的 fallback 链，不修改主模型设置、不调用 AgentRunner、不执行工具或公开发送。
提示词位于 `nanobot/templates/telegram/social_judge.md`，随原 wheel 模板路径打包。

## 持久化、重试与故障降级

social SQLite 从 schema 8 事务升级至 **schema 9**，保留原表与水位：

- `judge_state`：独立 assessed_through，不写主模型 seen/read，不授予睡前观察凭证。
- `judge_batches`：批次 ID、scope/版本、配置/提示签名、输入摘要、水位、期限、状态、决定、短理由和固定错误码。
- `judge_attempts`：每次尝试的时间、延迟、错误和 Token；无用量时保留 NULL，不伪装成 0。

不重复存快照正文、凭据、异常原文或思维链；输入内容继续从现有消息池取得。
同一已完成快照/签名重复提交返回已有决定，不重复调用；每 scope 仅一个 running 判断。
判断中来新消息不会推进到旧快照之外，暂停、换角色、过期、来源被编辑/清理会拒绝旧结果。
决定与独立判断水位同事务；取消的晚到结果仍记录可取得的用量，但不能恢复已取消决定。

每次 assess 至多执行一个 raw provider.chat，明确不调用带隐式重试的 chat_with_retry。
失败把下一次 due 持久化；限流 Retry-After 在批次期限内处理，不循环等待。
一次启动恢复可将被中断的 running 批次归回 pending，已计入的尝试不退还；已完成决定不重放。
恢复需要原快照仍能重建且摘要一致；丢失/变更输入的取消与重新组批由 V3-B 调度适配完成。

超过尝试上限时，最多登记一次 `fallback` 意图；该预算独立限额，主唤醒/发送预算仍需在 V3-B 投递时检查。
V3-A 的 fallback **不会调用主模型、创建 wake 或发送消息**。额度不够的批次保留 pending/due，最终由期限收尾。
`recover_judge_batches` 仅供唯一存储所有者在启动时调用，不能在已有请求运行时当成周期清理。

## V2 字段/工具迁移表（V3-B 执行）

下表冻结目标，V3-A 不静默改变旧字段含义；本批旧工具和触发行为继续有效。

| 旧字段/能力 | V3-B 目标 |
| --- | --- |
| nicknames、Bot username | 保留为身份资料，退出正则/子串语义准入 |
| wakeOnQuestion | 移除代码提问入口，交中间模型理解当前问句及指向 |
| wakeProbability | 废弃消息抽样；不映射成硬概率，也不先丢弃 95% 输入 |
| wakeOnMessages | 统一提交新消息批次，不再意味着无条件要求主模型参与 |
| wakeKeywords / wakeSenders、临时关键词/成员 | 迁移为 JudgeIntent 的关键词/成员与描述，由模型结合上下文解释 |
| sleepInterrupts / allowAmbientWake | 形成明确的潜水/参与意图与 interrupt_policy，不再跑布尔触发树；显式 operator 要求必须保留在输入 |
| replyAttention | 最近已确认发言、交互上下文与时间事实；名单/窗口退出硬相关性筛选 |
| dynamicWait.unfinishedSeconds | 退役句尾/短句词表；quiet/max 等基本合并期限保留，模型判断是否需要更多上下文 |
| social_wait 的 seconds/until/infinite、purpose、on_timeout | 保留实际期限与续接调度；purpose、until/infinite 进入 JudgeIntent，不允许首答无限等待 |
| social_set_wake 的 TTL/one_shot/clear | 保留意图到期、消费与清空；condition_expires 和 one_shot 随输入传递，consume 与主回合认领在 V3-B 接线 |
| 主动机会 / quietHours | 保留来源和时间边界；机会交同一判断入口，无新来源时不空转 |

V3-B 切换时删除并行旧语义入口并做配置迁移，不长期维护两套运行模式。
主模型原有工具、技能、MCP、共享记忆、Dream 与后台/跨会话投递权限不变。

## 独立验证与部署口径

已用当前本地 Codex 登录，对 **8 个合成样例** 实际调用 `gpt-6-luna`（low），8/8 与预期一致：
话题内“肥鱼呢”、语气词点名、无昵称承接、回答 Bot 问题、群友互聊、引用旧称呼、潜水中呼叫、相似人名。
首次测量约 **1.68–2.65 秒/次**，约 715–774 输入 Token、37–55 输出 Token。
这是初步可行性结果，不代表已证明所有群聊语境准确，也不是完整 V3-C 体验验收。

探针使用只读的当前 Codex access token 注入既有 provider 的 token 获取接缝；真实 OAuth 请求和 JSON Schema 输出通过，
未刷新或改写宿主机登录缓存，未调用真实 Telegram、没有向真实群发送测试消息。
此探针不等于验证了 `oauth-cli-kit` 对宿主机/容器共享凭据的长期刷新。

**常驻凭据边界**：V3-A 不增加容器登录缓存挂载、不复制 token 到仓库或镜像、不切换实际消息入口。
V3-B 启用前必须解决容器可访问的持久凭据及唯一刷新归属，避免把同一 refresh token 复制成两个独立刷新的副本。
继续复用现有 OAuth provider，而非每个消息批次启动 Codex CLI。

定向回归、严格类型、完整管线、安装 wheel 和本机发布的最终结果在本页后续记录。
实际启用后才能将“组件可调用”更新为“中间模型接管群消息”；下一批为 V3-B，优先于 V2-C。
