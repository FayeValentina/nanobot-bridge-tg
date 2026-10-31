# V5-B：表达学习

2026-10-31。**V5-B 已实现、验证并发布到本人实际本机实例**，运行提交 `bf64bccb7972481ef767edeadba2582dd789ac9f`。schema **15**，主聊天协议仍为 5，V3 广泛参与和 V4 主动流程沿用。V5-C/D/E 未实施：本批学习表达，不向回复注入候选，也不宣称聊天自然度已经改善。

## 实现和来源边界

新增 `social/expressions.py` 和 `templates/telegram/social_expression_learning.md`，复用 V5-A 的事件、双游标、批次和唯一 SQLite 连接。没有另建历史库、学习调度器、更新接收者、Embedding 服务或内容哈希。

- Telegram 入口仍只收集已确认真人 `message.text`。SELF、其他 bot、转发/via-bot、图片 caption、图片描述、引用实体、来源未知消息均不作为范本；引用和邻近消息只在 `learnable=false` 的有界语境中出现。工具/系统内容没有采集入口。
- 提取结果严格校验 schema，并逐值匹配本批真人 `message_id/revision/sender_id`。`excerpt` 必须是本条顶层原文的连续子串，`expression` 又必须是该摘录的连续子串。伪造 ID、跨 scope、从上下文搬原句或改写证据使整批事务失败，条目与游标一起回滚。
- 提示要求可复用、具体互动条件，排除私人近况、人名/地点强绑定、辱骂、机械模板、泛化口号和肥鱼招牌词。本地补充原句检查、常见口号/辱骂/链接/账号检查以及“本大爷、白饭、算力、Token”筛除；明显手工引文、代码和工具提示粘贴也跳过。这里筛选的是学习范本，不是聊天硬词禁令。
- 明确可复用且来源校验通过的结果直接为 **available**；语义仍不确定的为 **candidate**。无合格表达提交成功、结果数为 0、只推进表达游标。不存在逐条人工审批队列。
- 情境、语气、适用/不适用条件和粗筛标签一起入库。意图受控值为 `irony / playful_exaggeration / self_mockery / ellipsis / closing / teasing / playful_challenge / reaction`；场景为 `banter / sharing / complaint / correction / praise / disagreement / embarrassment / ending / guessing / surprise`。使用 V5-A 已有分类索引，后续 D 可先按 SQLite 场景/意图过滤。

来源身份和字面证据由程序校验；“是否有趣、是否可复用、是否私人强绑定”等语义判断仍依赖提取模型。不能据此承诺自动判断永不出错，存疑条目不会冒充可用。

## 合并、证据和修订

同 scope 的已有条目连同停用/修订记录一起供提取模型比较。模型用 `merge_id` 指向同义条目的稳定 ID，本地以原值归一化、包含关系和编辑相似度补充近似措辞合并；没有语义向量或额外合并模型请求。不同 scope 不合并；字面不同的同义合并依赖模型判定。

每个新增真人证据保留原消息 ID、消息修订、发送者、时间、摘录和有界语境，`occurrences` 只按新增证据计数。重复输出同一证据不会增加出现次数，选用/实际发送也不会算真人出现次数。合并不改原条目 ID 或自动覆写情境说明；明确新证据可将普通 candidate 提升为 available。

schema 14→15 事务升级只新增 `expression_guards` 及 scope 索引。管理入口为共享工具 **`social_expression`**，支持：

```json
{"action":"list"}
{"action":"get","entry_id":"实际稳定ID"}
{"action":"modify","entry_id":"实际稳定ID","expected_revision":1,"situation":"双方熟悉且正围绕一次小失误互相调侃时","reason":"缩小适用条件"}
{"action":"disable","entry_id":"实际稳定ID","expected_revision":2,"reason":"不适合本群"}
{"action":"enable","entry_id":"实际稳定ID","expected_revision":3,"reason":"明确恢复"}
```

修改/停用/启用都校验当前版本、增加显式 `revision` 并记录 `learning_changes`。旧措辞和情境封存到 guards；旧来源 ID（包括其后续编辑）、近似旧措辞以及模型指向已修订 ID 的同义候选均不再自动写回。人工修改过的条目不接受后台覆写或自动追加；恢复只能显式 enable。在途提取还比较开始时的修订水位，期间发生管理修改则整批不提交。guards 和来源抑制在重启后继续有效。源消息编辑/显式删除会使 available 与 candidate 一样失效。

这是一套轻量保护：完全不同措辞的语义近义词仍需提取模型识别已有停用条目；不声称字符相似度能证明任意同义表达都已被识别。上下文超过 180000 字符时保留 ready 批次并记录 `library_context_budget`，不会截掉停用记录继续学习。

## 静默运行和预算

运行时的既有 scheduler 先处理 V3、主聊天唤醒和发送重试，再准备学习批次，并启动一个独立后台 task。网络等待不占调度锁；主聊天有运行任务、待唤醒或待判断消息时不启动学习。已发出的请求在有界超时内完成，不阻塞之后的主回合。

- 默认满 **10 条真人素材**触发，一批最多 **80 条**（即使采集窗口配置更大也不突破）；候选最多 10，且遵守较小的 `expressionCandidates` 配置。
- 全局学习并发 **1**；两次请求启动至少间隔 **300 秒**。持久化 deadline 也覆盖失败/取消请求及进程重启，不因重启或新批次立即重试。
- 默认输出最多 **2048 Token**、超时 **30 秒**、最多 1 次重试；`maxRetries=0` 时仅尝试一次。调用原始 provider `chat`，不用内部自动重试或主模型 fallback 链。
- 默认复用配置解析出的普通 provider/preset（当前 `deepseek-flash`），但它是独立无工具学习请求，temperature=0；可用 V5-A 的 expression provider/model 单独配置。provider 未配置时只采集，状态为 unavailable，不影响主聊天启动。
- 请求次数、实际模型、累计输入/输出 Token、耗时、错误固定码、批次状态和结果数可从 `social_get_state.learning` 查看。异常文本/凭据不入原因字段。失败不推进游标，两次耗尽保留 failed，沿用 A 的失败契约。
- 提取和管理均不发送群消息、不写公开学习日志、不发布 inbound、不新增 wake，不改主 seen/read。没有为学习结果再唤醒主模型。尚未启动 C 的黑话研究或 D 的选择请求。

## 实际历史语料提取

执行入口：

```bash
.venv/bin/python -m scripts.evaluate_social_expressions \
  --config /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/config.json \
  --output /tmp/v5b-verification/real-extraction-verified.json
```

只在临时 SQLite 回放 V5-A 冻结、人工核实名册的历史提取集：39 条观察、其中 10 条真人文字；不读取留出评估集。两个 scope 各只有 7/3 条真人消息，所以**仅这次显式离线回放将 minMessages 设为 1 并直接执行两批，跳过调度等待**。实际默认仍为 10 条/300 秒，另有调度回归验证。不合并两个 scope 凑窗口、不自动回填线上 unknown 历史、不挂 Telegram receiver 或 sender。

实际 `deepseek-flash` 两次独立回放均得到 **3 条：2 available、1 candidate**。第二次附带证据逐值回查，以及对这 3 条实际结果逐条停用后重放旧来源修订和构造的近似措辞；**3/3 证据有效、3/3 旧来源不复活、3/3 近似措辞不复活**。后两类是基于真实提取结果的确定性持久化测试，不冒充新的真人对话或额外语义实验。

| 原句与消息证据 | 学到的情境 | 状态与粗筛 |
| --- | --- | --- |
| `猜猜`，main / **6844 / r1 / participant-1** | 对方已有猜测或追问答案，轻松卖关子，逗对方继续猜；认真求助/急需确切信息时不适用 | available；`playful_challenge`；guessing、banter |
| `昨天？不是今天吗`，main / **6895 / r1 / participant-1** | 对方记错时间，重复那个时间词再反问纠正；严肃责任或争执时不用作调侃 | available；`reaction`；correction、disagreement |
| `你自己不就是吗，这个时间段，你一般在哪`，topic / **6878 / r1 / participant-1** | 把对方当成刚讨论的事物，顺势把问题反问回去；仍需特定共享玩笑背景 | candidate；`playful_challenge`；banter |

没有从 SELF 的长篇人设话语、口头禅、图片描述中提取表达；行程建议、交通近况、“你怎么查的”和“没药”等没有被硬凑入库。第二次回放实际开销 **2 请求、5772 输入 / 2324 输出 Token、约 16.55 秒累计请求耗时**。首次探索同样 2 请求，5772/1695 Token、约 12.11 秒；本批历史模型实验总计 **4 请求、11544/4019 Token**。没有将合成保护断言算成模型调用。

可入库的历史样例及回查结果已脱敏冻结为 `tests/fixtures/social_v5b_actual_extraction.json`（Telegram 通道 tests 下）；剔除重复的 SELF 语境拷贝，原始提取素材仍以 `social_v5_baseline.json` 为准。离线断言会重新核对这些证据、来源范围与入库结构；实际线上表达数量单独记录。

## 验证与发布

- V5-B 专项 **32 项**，与 A/消息修订回归合计 **67 项通过**：来源排除、证据回查、空批推进、标签、自动可用、同义/近似合并、出现次数、停用/修改/重启/在途结果防复活、scope 隔离、主水位隔离、10/80 窗口、300 秒间隔、全局并发、主聊天优先、无群内发送、provider/重试契约。
- 先行 Telegram/共享工具/网关回归 **873 项通过**；相关运行模块严格类型检查零错误/警告。最终完整 `pipeline.sh release` 通过 **后端 1267 项、provider/OAuth 155 项、前端 96 文件/1553 项**，lint、WebUI/wheel 构建、隔离冒烟、认证与健康检查均通过。
- 实际 schema 14 只读备份→副本升级：**41 张旧表的所有旧列逐值一致**，schema 15、integrity_check=ok、foreign_key_check=0；不使用内容摘要。
- 安装包路径 `/opt/venv/lib/python3.11/site-packages/` 内，临时 SQLite/替身 provider 的 **5 组**探针通过：真人顶层来源、自动可用/标签/证据、停用后的重启/旧来源/近似句式保护、schema 15 完整性/主水位、满 10 条后台空批成功且无发送/唤醒。未调用真实群发送或启动第二个 receiver。

实际发布使用自动备份 `backups/1793448891770719926/`，上一运行版本 `df7348a6a8aff4dad470462c8c083e09a9243c5d`。容器 **healthy**，健康接口 ready=true，Telegram polling 已连接；只有一个该实例的 gateway。

与停机发布备份逐值核对：308 条消息、12 条记忆、4 条证据、240 张贴纸、6 个学习游标及表达/素材空表保持；config.json 原始字节与本次发布前 9 份 Markdown 正文一致。3 个 scope 主 read/seen 分别为 **125/128、177/177、1/1**，发布前后相同。既有启动 bind 路径使 scope 版本 30→32，与 A 已对照验证的启动行为一致；本批没有修改该配置/角色绑定版本逻辑，不将其算成学习进度。

线上仍为 308 条 unknown 历史、素材/批次/表达 **0**，六个游标未消费旧消息；等待发布后的真人新素材达到窗口。上述 3 条样例是实际模型在隔离历史回放库中写入和验证的结果，未向线上库写入脱敏 alias 或测试变体。实际学习默认启用；安装包解析配置确认 deepseek-flash、10/80 条、300 秒、并发 1、2048 Token、30 秒、1 次重试。

本机验证输出位于 `/tmp/v5b-verification/`：真实提取、安装包探针/配置、副本升级及发布逐值审计、67 项专项检查；完整发布日志 `/tmp/v5b-release.log`。这些都是直接值和明确 ID/revision 核验，没有新增摘要校验。

限制：冻结历史只有一名已核实真人，不足以证明覆盖所有群友风格；近似/同义合并是轻量规则加同批提取判断，不是完备语义等价判定。当前未实施 D/E 的相关候选注入和回复效果评估，不能把学出 3 条说成已证明回复更自然。
