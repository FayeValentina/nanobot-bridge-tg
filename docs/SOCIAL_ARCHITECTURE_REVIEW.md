# Telegram Social 迁移代码审查与重构指引（2026-11-03）

> **读者：实施重构的编码 agent。** 本文是审查结论和执行计划，不是用户手册。
> 审查基线：`main` @ `5da0464f`，社交库 schema 17，部署实例 `~/.local/share/nanobot-bridge-tg-deploy/`。
> 审查范围：直接影响群聊体验的核心代码，包括 agent loop 改动、中间判断模型（judge）、
> 唤醒/等待调度、上下文注入、投递、长期记忆、表达/黑话学习、贴纸。WebUI/TUI 不在范围内。
>
> 目标：**模块职责分明、架构合理、代码清晰、适合长期维护，同时保持现有群聊行为。**

---

## 0. 动手之前必须知道的约束

下列用户偏好来自 `AGENTS.md`，重构中**不得改变**：

- V3 广泛准入：新鲜的群消息默认交给主模型，由主模型决定发言还是沉默。不得加回本地关键词、昵称、问号或概率门槛。
- 群友模式和普通助手**共用同一套工具权限**（`docs/SHARED_TOOL_PERMISSIONS.md`）。
- **不使用哈希或指纹**：比较时用 ID、revision 或直接比较值。
- `social_send` 只能二选一：`text` 或 `parts`（2–3 条）。
- 每件事主动跟进最多两次；`unknown` 投递不得重发；自身消息、重复消息、过期消息的保护保留。
- 贴纸优先、少给建议、空闲活动是“休闲”而不是工作。这些属于提示词和角色层面的行为偏好。
- 验收方式：用户确认的是“构建 + 启动”（`./scripts/pipeline.sh release`），默认不跑全量测试。
  **本次重构的例外（用户 2026-11-03 已同意）：** 新增 §3 的场景测试套件，每个 Phase 都要运行并通过；全量测试仍然不跑。
- 每个可运行批次：提交 → `pipeline.sh release` 部署到实际实例 → 记录。保留自动备份和失败回滚。

**重构通用规则：**

1. 每个 Phase 单独提交，**行为保持不变**；确需改变行为的条目在本文中已标注【行为变更】，单独提交并在提交信息里说明。
2. 线上 SQLite 有真实数据（消息、记忆、黑话、贴纸注释、跟进计数）。任何表结构变化都必须写**前向迁移**，不能删库重建。迁移在实例备份上先演练一遍。
3. 不要顺手做无关清理，也不要给上游通用代码（providers、其他 channel、webui）加新抽象。
4. 拆分时遵守 `.agent/design.md` 的“Prefer duplication over premature abstraction”：目标是**降低耦合**，不是增加层数。不要为了拆而拆出一堆十几行的小文件。

---

## 1. 现状结构速览

```
Telegram update
  └─ channels/telegram/runtime.py::_observe_social          (7 个调用点)
       └─ social/runtime.py::TelegramSocialRuntime.observe
            └─ social/store.py::SocialStore.observe          (写 messages、updates、judge_facts，并触发学习采集)
                 └─ queue_judgement → judge_state
scheduler(run_due) ──┬─ judge_runtime.JudgeCoordinator.start_due → judge.SocialJudge.assess (LLM)
                     │     └─ store._handoff_judgement → wakes(scheduled)
                     ├─ proactive.tick → wakes(proactive)
                     ├─ store.ready → runtime._publish_wake → bus.publish_inbound(快照 JSON)
                     ├─ runtime._retry_sends
                     └─ expressions / jargon learner workers (LLM + web)
AgentLoop (通用) ── SessionPolicy(tool_only, persona) 来自 runtime.policy_for_session
   └─ runner + _SocialExposureHook
        ├─ before_iteration: selector(LLM, 2s) + memory.context → 注入最后一条 user/tool 消息
        ├─ _note: 用子串匹配判断 view 是否已展示给模型 → wakes running / seen_through
        └─ on_finally: acknowledge_seen / finish / keep_reply_intent
工具: agent/tools/telegram_social.py (一个类实现 12 个工具)
```

新增代码规模：社交模块约 7.7k 行（`social/*.py` + 工具），通用核心改动约 1.5k 行，
测试和夹具约 3.4 万行（其中一个回放夹具 2.2 万行）。

---

## 2. 关键问题（按严重度排序）

每条格式：**位置 → 问题 → 证据 → 建议**。

### P0-1 `TelegramSocialRuntime` 是 god class，组件之间循环依赖

- 位置：`nanobot/channels/telegram/social/runtime.py`（1230 行）
- 问题：一个类同时负责授权/绑定解析、角色文件加载、会话策略与上下文重置、媒体下载与图片准备、
  贴纸收集/发送、三条发送路径及其预约/去重/冷却、重试队列、wait/sleep/set_wake 语义、
  `get_state` 报表、普通 message 工具回执记账、调度主循环、学习 worker 排序，以及一个 170 行的 hook。
- 循环依赖：`JudgeCoordinator(self)`、`SocialStickers(self)`、`ExpressionLearner(self)`、`JargonLearner(self)`、
  `ContextSelector(self)` 都持有 runtime 的反向引用，并访问它的私有成员：
  `judge_runtime.py:77,96`（`runtime._wake_conditions`）、`:146`（`runtime._media_lock`）、`:246`（`runtime._persona`）；
  `judge_runtime.py` 顶部甚至用 `# pyright: reportPrivateUsage=false` 关掉了检查。
- 建议：见 §4 目标结构。组件只接收需要的窄依赖（store 仓储、配置访问器、绑定解析器），不再接收 runtime。

### P0-2 `SocialStore` 是 god object，`store.db` 泄漏到所有模块

- 位置：`social/store.py`（1271 行）
- 问题：一个类混合了消息观察、view/曝光追踪、wake 调度状态机、dialogue wait、judge 批次日志、
  发送日志、贴纸库、恢复逻辑。更严重的是，裸 SQL 散布在每个模块里，不只在 store 中。
- 证据（各文件 `db.execute` 出现次数）：store 281、learning 82、schema 57、jargon 42、proactive 34、
  expressions 33、memory 31、judge_runtime 31、runtime 30、jargon_learning 30、followups 28、activity 16、selection 8，
  `channels/telegram/runtime.py` 也直接调用 `social.store.delivery(...)`。测试里有 **732** 处 `db.execute`。
  表的语义（例如 `wakes.status` 的合法转换）没有任何单一归属。
- 建议：按表的归属拆成小仓储（见 §4），规定**只有仓储模块可以写 SQL**。
  可在 `scripts/pipeline.sh` 的检查阶段加一条 grep 规则：`social/` 下除 `db/` 目录外出现 `.db.execute` 即失败。

### P0-3 唤醒/等待状态机是隐式的，散落在 4 个文件里

- 位置：`store.py`（`_schedule`、`ready`、`claim`、`finish`、`note_seen`、`wait_for_quiet`、`consume_dialogue`、`defer`、
  `recovery_jobs`、`set_wake_config`）、`runtime.py`（`wait`、`keep_reply_intent`、`_publish_wake`）、
  `proactive.py::tick`、`judge_runtime.py::_run` 的 finally 块。
- 问题：
  - `wakes.status` 有 scheduled/queued/running/completed/cancelled/expired/failed/unsent 八种取值，
    `scopes.mode` 有 observing/active/waiting/sleeping/paused 五种，另有 `dialogue_waits`、`dialogue_jobs`、
    `judge_state.pending_*`、`scopes.wake_at/wait_infinite/timeout_through` 共同决定行为。
    状态转换没有集中定义，只能读完全部 SQL 才能推出来。
  - `store._schedule` 的 `rank` 表（`store.py:1066`）里，`directed/nickname/keyword/sender/question/probability/message/attention`
    这些 reason **已经没有生产代码会产生**。实际只有 `judge`、`proactive`、`timeout`、`wait_reply`、`wait_messages` 五种。
    `note_seen`（`store.py:551`）、`wait_for_quiet`（`:943`）、`defer`（`:1041`）仍在检查 `'directed','nickname'`。
- 建议：新建一个 `wakes` 模块，用显式枚举 + 转换函数（`schedule/queue/claim/finish/cancel/expire`）集中管理状态。
  删除死 reason。每个转换函数配一行 docstring 写清前置状态。这是整个重构里价值最高的一步。

### P0-4 “模型是否看到了快照”靠子串匹配判断

- 位置：`store.py:533`（`note_seen`：`row["body"] not in model_text`）、`selection.py:126`（`input`）、
  `memory.py`（`context` 里按 turn 读取最新 view body）。
- 问题：把整个 view JSON 当子串，去匹配本轮所有消息拼成的文本。只要序列化格式、压缩、截断、
  provider 改写或 runtime context 拼接有任何变化，曝光就会静默失效。失效后 wake 不会进入 running，
  `seen_through` 不推进，`mark_read` 报错，选择器拿不到输入。复杂度是 O(views × prompt 长度)，每轮迭代都要算。
- 建议：给快照一个显式标识。`_publish_wake` 和 `read_messages` 生成的内容里带上 `view_id`，它本来就在 JSON 里。
  hook 只需在本轮消息中查找 `"view_id":"<id>"` 这样的短 token，或者在工具返回、入站消息构造时直接登记
  “view X 属于 turn Y 且已发送给模型”。后者更可靠：入站 wake 消息必然进入 prompt，`social_read_messages`
  的返回值也必然进入下一次迭代。selector 和 memory 改为按 `view_id`/turn 查表，不再做子串比较。

### P0-5 两套并行的投递日志，策略检查复制了三份

- 位置：`runtime.py:797-900`（`reserve_message_send`、`send`、`send_parts`）、`runtime.py:1023`（`record_message_receipt`）、
  `channels/telegram/runtime.py::_send_confirmed`。
- 问题：
  - `social_send` 和贴纸写 `actions` 表；普通 `message` 工具写 `message_deliveries` 表，**同时**还在 `actions` 里预约一行
    （`units=` 按块数计费）。两张表各自维护状态、回执、跟进计数，`recent_usage` 只能用 UNION 合并
    （`selection.py:326`），`keep_reply_intent` 也一样（`runtime.py:977`）。
  - 冷却、“有排队中的发送”、同轮重复、unknown 重发保护、proactive 合法性、版本检查这些规则，
    在 `reserve_message_send`、`send`、`send_parts`、`_deliver`、`_retry_sends`、`store.reserve_send`、`store.enqueue_parts`
    中各写了一遍，错误文案还不一致。
- 建议：合并为一个 `DeliveryJournal`（一张表，加 `kind` 列区分 text/sticker/message_tool），
  只保留一个 `check_send_allowed(scope, turn, units)` 函数。telegram channel 只调用 journal 的
  `reserve / record_result` 两个公开方法，不再碰 `social.store`。迁移时把 `message_deliveries` 的历史行并入新表。

### P0-6 提示词硬编码在 Python 里，层层打补丁，且混入了特定角色的内容

- 位置：
  - `runtime.py:69-228` 的 `SOCIAL_PROTOCOL`：约 1.5 万字符，估算 4–5k token，每轮都发送。
  - `runtime.py:1111-1122` 的 `expression_guidance`：中文，每次迭代注入。
  - `memory.py` 的 `pending_note` 和 `note`，`selection.py:363` 的 `note`。
  - 12 个工具的 description（`telegram_social.py`，整个文件 1.6 万字符）。
- 问题：
  1. **补丁式累积**：每个线上事故追加一段规则（天气/出发时间、症状无害、三次搜索上限、贴纸收尾……），
     同一条规则在 protocol、工具描述、hook guidance 里各说一遍（例如“事实问题先给实质回答”出现了 3 次，
     “贴纸可单独发送”至少 4 次）。规则之间存在张力，长度本身也在稀释重点。V5-E 结论里“无依据事实回复、
     不自然斗图收尾”仍未解决，与提示词过载有直接关系。
  2. **角色内容泄漏进框架代码**：`expression_guidance` 写死了“保留嘴硬摸鱼的性格”，
     `selection.py:351` 写死了口头禅列表 `("本大爷", "白饭", "Token", "算力")`，
     `selection.py:74` 的 `SCENE_HINTS` 写死了中文场景词。换一个角色文件，这些内容就是错的。
  3. 大量向模型解释内部机制的文本（queued 和 unknown 的区别、one-shot 条件何时消费、seq 连续性），
     这些规则代码已经强制执行，模型只需要知道“工具返回什么就照做”。
- 建议：
  - 把 protocol 移到 `nanobot/templates/telegram/social_protocol.md`，并拆成几段：身份与参与、发送规则、
    等待规则、记忆与跟进、主动/空闲。去重后目标是减少 40% 以上。代码已经强制执行的约束，在提示里只留一句“以工具返回为准”。
  - 角色相关内容（口头禅、性格提醒、场景词）移到角色文件或 `TelegramSocialScopeConfig` 里的显式字段。
    代码里不保留任何特定角色的字面量。
  - `SOCIAL_PROTOCOL_VERSION` 改为模板文件自带的版本号（或直接比较模板内容，不得用哈希）。
  - 提示词改动是【行为变更】。单独提交，并在实际群里观察一天。

### P0-7 配置有任何变化，就会重置会话上下文、取消所有等待

- 位置：`runtime.py:432-436`（`_persona` 的 version 由整个 `config.model_dump(exclude={"scopes","learning"})` 生成）、
  `runtime.py:464-471`（`policy_for_session`：version 变化时 `session.last_archived = len(session.messages)`，并清空 summary）、
  `store.py:84-113`（`bind`：version 变化时取消 wakes、清空 dialogue、wake_config、judge 批次、排队的 actions）。
- 问题：改 judge 超时、改 dynamic_wait 秒数这类与角色无关的配置，也会让机器人**“失忆”**（历史不再进入上下文），
  并丢掉正在进行的等待。`_social_runtime_epoch`（`runtime.py:472`）让每次进程重启都清空 provider_state 和 checkpoint。
  这是用一个粗粒度的版本号兜住所有一致性问题，属于过度防御，而且直接伤害聊天连续性。
- 用户已确认（D4）：**必须保留历史**。做法【行为变更】：拆成两个版本。
  - `persona_revision`：角色文件内容、background、protocol 模板变化时递增，只用于让新回合使用新 persona。
    **不截断历史**；如果确实要隔离，最多清掉 provider_state。
  - `binding_revision`：只在 scope 被禁用、暂停或改绑角色时递增，用于取消在途 wake 和排队发送。
  - 其余配置在下一次读取时直接生效，不触发任何重置。

### P1-1 死代码和“已退役”的遗留物（低风险，优先清理）

全部删除。表结构用前向迁移 DROP：

| 项 | 位置 | 说明 |
|---|---|---|
| 退役的 wake reason | `store.py:551,943,1041,1066-1085` | 只保留 judge/proactive/timeout/wait_reply/wait_messages |
| `dialogue_attention` 表 | `store.py:645,887,936,976-977,1026,1033,1202`；schema | **没有任何 INSERT**，却在 7 处维护 |
| `wake_samples` 表 | schema 及 v11 迁移 | 概率采样已退役 |
| `SocialStore.wake()`、`SocialStore.schedule()`、`delete_observed()` | `store.py:613,1059,364` | 仅测试使用 |
| `store.observe` 的 `reason/delay/quiet_seconds/wait_relevant` 参数 | `store.py:370` | 生产调用从不传这些参数 |
| `_quiet_seconds` | `runtime.py:563` | 无调用者 |
| `allows_personal_delivery` | `runtime.py:368` | 恒为 True |
| `set_wake(probability=)` 及工具 schema 中的 `probability` | `runtime.py:739,758`、`telegram_social.py:114,186` | 只能传 0，否则报错 |
| `get_state` 的 `retired_config`、`proactive.status` 的 `retired_config` | `runtime.py:662`、`proactive.py:68` | 向模型展示历史包袱，浪费 token |
| `TelegramSocialReplyAttentionConfig`、`dynamic_wait.enabled/unfinished_seconds`、`sleep_interrupts.probability`、`wake_probability` | `config/schema.py` | 已退役配置。用 loader 兼容读取后忽略即可 |
| `SocialJudge.from_config` | `judge.py:89` | 无调用者 |
| `JargonLibrary.legacy` + `SocialMemory(jargon=True)` 自嵌套 + memory 的 `slang` kind | `memory.py:101-110,212-224`、`jargon.py:259` | 黑话有两套 API。保留 `social_jargon`；`social_memory` 不再接受 `kind=slang`，返回明确错误并提示改用 `social_jargon` |
| `farewell` 正则 | `runtime.py:716` | 和“没有本地关键词规则”的原则矛盾。改为由模型显式选择 `sleeping` 时不做观察窗口，或直接删除这个分支【行为变更，轻微】 |

**M0–M3 隔离模式的残留（用户已在 2026-10-28 撤回隔离）：**

- `session/manager.py`：`SessionPolicy.allowed_tools/include_profile/include_memory/include_skills/archive_memory/allow_commands/enabled`、
  `ISOLATED_SESSION_POLICY`、`ISOLATED_CONTEXT_KEY` 以及 `Session.__post_init__`、`resolve_policy` 中对它们的处理。
- `agent/context.py` 的 `include_profile/include_skills` 参数链；`agent/memory.py` 的 `prompt_policy/tool_definitions` 与 `archive_memory` 分支；
  `agent/loop.py` 中的 `allowed_tools`、`requires_explicit_allowlist` 分支；`agent/tools/base.py::requires_explicit_allowlist`。
- 现在唯一会设置这些字段的路径，是 `ISOLATED_CONTEXT_KEY` 标记，而 social runtime 每次加载会话都会把这个标记 pop 掉（`runtime.py:451`）。
  清理步骤：先写一个一次性脚本，确认实例里所有会话 JSON 都没有 `_isolated_context`（或直接删除该键），再删除代码。
  `SessionPolicy` 只保留 `persist/log_content/disabled_tools/tool_only/persona/context_version`。
- 价值：这些是对上游通用核心的侵入式改动。删掉后，loop/context/memory 与上游的 diff 会明显变小，以后合并上游更容易。

### P1-2 过度防御：校验函数有副作用，同一检查重复多次

- `validate_context`（`runtime.py:479`）每次都调用 `_persona`：`stat` + 读取角色文件 + `json.dumps` 整个配置
  + 查 `runtime_settings` + 必要时 `store.bind` 写库。一次 `social_send` 至少触发 3 次（`send` 入口、拿锁后、`_deliver`），
  hook 里也会调用。**校验函数不应写库。**
  建议：persona 按文件 mtime 缓存；版本计算只在 `configure()` 和角色文件变化时做；`validate_context` 改为纯读。
- `ContextSelector.select`（`selection.py:203-307`）：LLM 调用前后各做一遍 input、`messages_current`、`current(candidate)`
  的全量校验；同一回合输入一变就返回空，并写入 `reuse_status`。对一个 2 秒、可选、失败即空的提示增强来说太重。
  建议：一个 turn 内只调用一次，结果缓存到 turn 结束；不做调用后的复核（候选在注入时已经是只读数据，过期的影响可以忽略）。
- judge 批次日志（`store.py:135-350` + `judge_runtime.py` + `judge.py`）：`judge_batches/judge_attempts/judge_state`、
  `input_meta` 冻结与 `_restore` 重建、`_judge_input_matches`、`_judge_current` 中按 `startswith` 比较文本、
  fallback 每小时预算、provider 变更取消……而这个 judge 的提示词明确要求**默认 wake**（`templates/telegram/social_judge.md` 规则 4）。
  它真正过滤掉的只有“显式私聊/显式要求不插话”和“选择性等待期间的无关消息”。
  做法见 §5.1（用户已确认保留并简化）。
- 学习 worker 吞掉异常且不记录日志：`expressions.py:336-339`、`jargon_learning.py:211-214`、`selection.py:112,294,304`
  只留下 `invalid_or_changed_input` / `provider_exception` 这样的错误码。AGENTS.md 里记录的“线上黑话批次两次失败、状态保持 failed”
  **正是因此无法诊断**。
  建议：所有 `except Exception` 加 `logger.exception`；在 `learning_batches` 增加 `error_detail` 列，保存异常类型和截断到 300 字符的消息
  （先去掉凭证，可复用 `security/credentials.py` 的规则）。这一条请**最先做**。

### P1-3 hook 每次迭代都改写消息并恢复

- 位置：`runtime.py:1078-1144`（`_SocialExposureHook.before_iteration`）
- 问题：每次迭代都把记忆索引、选择结果、近期用语、活动笔记、指导语拼进最后一条 user/tool 消息，迭代结束再恢复。
  原因写在注释里（不能改早先消息，否则前缀缓存失效）。这个技巧可行，但让 hook 承担了“上下文构建”职责，
  而且依赖 runner 对 `context.messages` 和 `model_messages` 的内部约定。
- 建议：保留“只注入最新增量”的机制，把内容构建移到独立的 `SocialContextBuilder`（纯函数：scope、turn → `RuntimeContextBlock` 列表），
  hook 只负责注入和恢复。memory、selection、activity 不再在 hook 里各自查库。
  同时确认这段上下文只在“新的模型输入”时才需要刷新：工具结果迭代可以复用同一份，避免每次迭代都重建。

### P1-4 主动发言中，activity 分支会让 idle 发言永远走不到【已确认是 bug】

- 位置：`proactive.py:178`
- 问题：`elif policy.activity_enabled:` 排在概率 idle 分支之前，命中后直接生成 `kind="activity"`。
  所以只要开启空闲活动，V4 的 idle 主动开话题（概率 0.6、待说想法加权、夜间系数）**就不会再执行**。
  当前实例已开启 activity。
- 修复方案见 §5.2。

### P1-5 学习 worker 用继承复用，但子类覆盖了全部核心方法

- `JargonLearner(ExpressionLearner)`（`jargon_learning.py:44`）完全重写了 `run`，只复用 `start_due/busy/cancel/configure_provider`。
  两个 `run` 的 finally 块几乎一样（批次状态、计数、tokens）。
- `busy()`（`expressions.py:275`）通过 `runtime.judging.tasks` 和 gateway 注入的 `agent._active_tasks`（`gateway_runtime.py:537`，访问私有成员）
  判断主循环是否空闲。
- 调度顺序靠 `run_due` 里一段内联 SQL 排序（`runtime.py:607-611`）。
- 建议：抽出一个 `LearningWorker`（组合方式）负责批次生命周期：claim → 运行 → 成功/失败记账 → 日志。
  expression 和 jargon 只实现 `async def process(batch) -> int`。主循环忙闲改为 AgentLoop 暴露的公开只读属性，或者由 runtime 传入的回调提供。

### P2-1 工具层：一个类按名字分发 12 个工具

- 位置：`agent/tools/telegram_social.py`
- 问题：`description`、`parameters`、`execute` 都是 `if self.name == ...` 链；`social_sticker` 内部又按 action 二级分发。
  这违背了仓库里“一个工具一个类”的惯例，也难以单独修改某个工具。
- 建议：拆成每个工具一个小类（同一文件或 `agent/tools/telegram_social/` 包均可），共享一个 `_SocialTool` 基类，
  基类只负责取 ctx、捕获 `PermissionError/ValueError`、做 JSON 编码。参数 schema 统一由 pydantic 请求模型生成，
  已有的 `MemoryRequest/JargonRequest/ActivityRequest` 是好例子。`send/wait/set_wake/sticker` 也补上请求模型，不再手写 dict。
- 工具合并方案见 §5.3（用户已确认必须合并）。

### P2-2 Telegram channel 中的接入点过多

- `channels/telegram/runtime.py` 有 7 处调用 `_observe_social`（1798、1816、2051、2272、2303、2314，以及 group=-1 的 handler），
  因为 social 既注册了优先级 handler，又在各个普通 handler 开头再拦截一次。
- `_send_confirmed` 里混入了 social 预约和回执记账（大约 90 行），直接操作 `social.store`。
- 建议：只保留 group=-1 handler 这一个入口（它已经会 `raise ApplicationHandlerStop`），删除其余调用点；
  先核实 edited_message、callback 等是否真的需要单独入口。投递记账改为调用 `DeliveryJournal`（见 P0-5）。

### P2-3 gateway 装配写在通用代码里

- `cli/gateway_runtime.py:~470-540`：无论 Telegram 是否启用，都会构造 social runtime、judge provider，注册 12 个工具和 hook。
  另外在 `:754` 附近的热重载路径里重复了一遍 Codex OAuth 目录推导。
- 建议：在 `channels/telegram/social/wiring.py` 提供 `build_social_runtime(config, config_path, bus, tools) -> SocialWiring | None`，
  Telegram 未启用或 `social.enabled=false` 时返回 None。gateway 只调用这一个函数。热重载复用同一个函数。

### P2-4 schema 迁移分散在三个文件，共 17 个版本

- `schema.py`（v0→13、v11 去哈希）、`learning.py`（v14–16）、`activity.py`（v17）。
- 这是单用户单实例，没有必要永久保留 17 步迁移链。
- 建议：迁移全部集中到 `schema.py`。新增一个迁移版本完成 P1-1 和 P0-5 的表变更，然后生成“v18 基线”DDL：
  新库直接建 v18；旧库只允许从 17 升到 18，更早的版本报错并提示先用旧版升级。删除 v0–v17 的逐步迁移代码。

### P2-5 配置面过大

- `TelegramSocial*Config` 共有约 140 个字段，很多是之前方案的调节旋钮（多种冷却、合并、quiet、attention、阈值、乘数）。
- 建议：在 P1-1 删除退役字段之后，列出线上实际使用非默认值的字段（看实例的 `config.json`），其余字段改为模块常量。
  先不删 loader 对旧键的兼容。

### P3 与聊天体验无关的通用基础设施（冻结，不扩展）

- `session/incremental_log.py`（460 行）+ 会话格式能力门控 + 部署时的格式检查：
  为每次保存都重写 JSONL 而做的性能优化，与群聊体验无关，却明显扩大了与上游的差异，提高了以后合并上游的冲突风险。
  建议：本次重构**不改动**它，也不要再扩展。如果以后要合并上游 session 层，优先考虑回退到上游实现。
- `security/landlock.py`、`credentials.py`、`identity.md` 的源码目录说明：属于用户明确要求的能力，保留。

---

## 3. 测试现状与策略

- 现状：测试加夹具共约 3.4 万行。`test_social_*.py` 共有 732 处直接 `db.execute`，大量测试断言内部表的列值。
  `tests/` 下混有 `evaluate_social_*.py` 这类评估脚本和一个 2.2 万行的回放夹具。
  这些测试把现在的表结构和实现细节**焊死**了：任何 §2 中的结构改动都会让几百个测试失败，但失败本身不能说明行为错了。
- 策略：
  1. 评估脚本和大夹具（`evaluate_social_*.py`、`fixtures/social_v5*_actual*.json`、`social_v5_baseline.json`）移到
     `scripts/eval/`，不作为单元测试。`pipeline.sh` 中的引用同步修改。
  2. 新建 `nanobot/channels/telegram/tests/test_social_scenarios.py`，写 **12–15 个黑盒场景**。
     只使用公开入口：`runtime.observe`、`run_due`、工具 `execute`、hook 回调，配合假时钟、假 sender、假 provider。
     断言只看外部可观察结果：是否发布了 wake、发出了哪些消息、工具返回了什么。建议覆盖的场景：
     - 普通群消息 → judge wake → 主模型 `social_send` → 回执 sent → 30 秒回复等待
     - judge 超时或失败 → fallback wake
     - 选择性等待期间无关消息 → observe；被直接点名 → wake
     - `parts` 分段发送，第 2 段被限流后排队重试
     - unknown 投递 → 同文案禁止重发
     - 冷却期间发送被拒
     - sleep → 新消息被 judge 评估 → 唤醒
     - idle/activity 主动机会 → 被新的会话消息取代
     - 跟进两次之后不再提供 `followup_id`
     - 角色文件修改后的下一回合使用新 persona（重构后**不应**截断历史）
     - 重启恢复：在途 wake 重新排队、`sending` 变为 unknown
     - 贴纸导入、注释、搜索、发送
     - 普通 message 工具发到 social scope 时计入同一配额
  3. 每个 Phase 改到哪个模块，就删除或改写对应的白盒测试，以场景测试为准。
     不要为新的内部结构再写大量白盒测试；每个仓储最多写 3–5 个关键不变量测试。

---

## 4. 目标结构

以“谁拥有哪张表、哪段语义”来划分，不按技术层次划分。

```
nanobot/channels/telegram/social/
  wiring.py          # build_social_runtime()：gateway 唯一入口（P2-3）
  runtime.py         # 薄 facade：configure / observe / run_due / recover / policy_for_session / hook_factory
                     # 只做编排，不写 SQL，不含提示词。目标 < 300 行
  bindings.py        # scope → binding 解析、persona 加载（按 mtime 缓存）、persona/binding revision（P0-7）
  db/
    connection.py    # 连接、PRAGMA、事务辅助
    schema.py        # v18 基线 + 17→18 迁移（P2-4）
    messages.py      # messages/updates：observe、retention、has_message、media_for_message
    views.py         # 快照构建 + 显式曝光登记（P0-4）
    wakes.py         # wake 状态机，含 scopes.mode / dialogue_waits / timers（P0-3）
    deliveries.py    # 统一投递日志 + 配额/冷却检查（P0-5）
    judge_state.py   # assessed_through 水位（+ 可选 attempts 统计）
  judge.py           # 合并窗口 + 一次 LLM 调用 + fallback（合并 judge/judge_runtime/judge_models 中仍需要的部分）
  context.py         # SocialContextBuilder：记忆、选择结果、近期用语、活动 → RuntimeContextBlock（P1-3）
  hook.py            # 曝光登记、注入/恢复、未发送 final 修复、turn 结束收尾
  delivery.py        # send / send_parts / sticker send / 重试，统一走 db/deliveries
  waiting.py         # wait / sleep / set_wake 的参数校验与语义（调用 db/wakes）
  proactive.py       # idle/activity 机会（P1-4）
  knowledge/
    memory.py        # 仅 memories（不再自嵌套 jargon）
    jargon.py        # 黑话库 + 工具语义
    followups.py
    activity.py
  learning/
    worker.py        # LearningWorker：批次生命周期 + 错误记录（P1-5、P1-2）
    collect.py       # 原 learning.py 的采集/游标部分
    expressions.py
    jargon.py        # 发现 + 研究
    selection.py     # 生成前选择（简化后）
  stickers.py
  media.py
  models.py
nanobot/templates/telegram/
  social_protocol.md (+ 分段 include)   # P0-6
nanobot/agent/tools/telegram_social.py  # 每个工具一个类（P2-1）
```

依赖方向（单向，不允许反向引用 runtime）：

```
wiring → runtime → {bindings, judge, delivery, waiting, proactive, hook, context, learning/*, knowledge/*, stickers}
                 → db/*  ←（只有 db/* 写 SQL）
tools  → runtime 的公开方法（不访问 runtime.store）
telegram channel → runtime 公开方法 + DeliveryJournal 公开方法
```

---

## 5. 用户决策（2026-11-03 已确认）与对应设计

| 编号 | 决策 | 落实位置 |
|---|---|---|
| D1 | 保留 judge 并简化 | §5.1 |
| D2 | activity 屏蔽 idle 主动开话题**是 bug，必须修** | §5.2 |
| D3 | **允许**为本次重构新增并运行场景测试套件（之前没加是因为额度不够，不是用户反对） | §3、§6 |
| D4 | 配置或角色变化时**必须保留会话历史**，现在的做法是错的 | P0-7 |
| D5 | 低频工具**必须合并**，现在的分散是历史遗留 | §5.3 |

### 5.1 judge 简化设计（D1）

**背景（用户说明）：** 最早是用纯代码判断要不要转发给主模型，结果完全处理不了人类语言，于是引入中间模型。
后来发现中间模型会拦掉大量群友本意要发给主模型的消息，所以改成现在“默认唤醒、只拦少数情况”。
结论是：**judge 的价值只在少数确实需要语义判断的场景；凡是能用平台事实确定的，不调用模型；凡是判断不了的，一律唤醒。**

**1. 准入分三层，按顺序判断：**

| 层 | 条件（只用平台事实，不用语言规则） | 结果 | 调模型 |
|---|---|---|---|
| A 宿主保护 | 自己发的、编辑过的、重复 update、超过 120 秒的旧消息、scope 暂停 | 只观察 | 否 |
| B 确定唤醒 | `directed`（@本 bot，或回复本 bot 的消息） | 唤醒 | 否 |
| B 确定唤醒 | 当前**没有**显式选择性等待（`wake_config` 为空，或者没有 keywords/senders/on_question） | 唤醒 | 否 |
| C 语义判断 | 有显式选择性等待（模型调用过 `social_wait` 的等待条件，或旧的 `social_set_wake`），且新消息不是 directed | 交给 judge，判断是否满足等待条件 | 是 |

- **owner 补充决定（2026-11-04，依据 P7-0 线上数据）**：其他 bot 发出的、非 directed 的消息仍交给 judge（C 层）。
  理由：P7-0 统计最近 16 小时 judge 判 observe 的 8 条全部是另一 bot 的 /usage 用量回执，若归 B 层每条都会唤醒主模型（每次调用约 6.6 万 token）。
  判定只用平台事实 `from_user.is_bot`：批次中只要有一条真人消息就走 B；全部新消息都来自 bot 且无 directed 才走 C。
  仅 `on_messages=true` 不算选择性等待（走 B）；directed 走 B 时 `fulfills_intent=false`，不消费 one-shot 条件。
- C 层是唯一必须做语义判断的地方：模型说过“等小明回来”“等有人问我 X”。这类条件只能靠模型理解，也是当初引入 judge 的初衷。
- 普通观察、普通回复等待（`purpose=messages/reply`）、没有附加条件的 sleep，现在的 judge 提示词规则 4 本来就要求 wake，
  简化后直接走 B 层，**省掉一次 LLM 调用和 1–3 秒延迟**，行为不变。
- 【行为变更，很小】现在的规则 3（“明确私聊 / 明确要求别人不要插话”→ observe）在 B 层不再由 judge 过滤，改由主模型自行选择沉默。
  主模型 protocol 里已有“不要把别人之间的对话当作对你说的”，所以影响仅限这类罕见消息多一次主模型调用。
  如果上线后观察到主模型在这类场景里插话，**不要**恢复 judge 全量评估，而是在主 protocol 中加强一句沉默规则。
- 设计原则写进 judge 模块的 docstring：**不确定就唤醒；judge 只回答“是否满足模型自己设下的等待条件”，不回答“这条消息值不值得回”。**

**2. 合并窗口（coalescing）保留，改成内存状态：**

- 每个 scope 一个内存对象 `PendingBatch(first_at, last_at, through_seq, has_directed)`。
  到期时间 = `min(last_at + coalesce_delay, first_at + max_seconds)`；directed 消息可以缩短到 `min(当前到期, now + 1s)`。
- 持久化只保留一个水位：`judge_state(scope, assessed_through)`。重启时，水位之后、仍在 120 秒内的消息重新进入合并窗口。
  这就是全部恢复逻辑。
- 删除 `judge_state.pending_first/pending_last/pending_seq/pending_due/pending_max/batch_id/inflight/opportunity`。
  其中 `opportunity` 路径已是死代码：`JudgeCoordinator.enqueue(opportunity=...)` 没有任何调用者，主动机会直接写 `wakes`。

**3. C 层的 judge 调用：**

- 输入：bot 身份（username/nicknames）、等待意图描述（模型原话 + 条件字段）、最近 N 条上下文（默认 12 条）、新消息
  （文本、发送者、reply 关系、媒体标记）。**不发图片**：选择性等待的条件几乎都是人或话题；图片消息仍按发送者、说明文字和 reply 关系判断，
  模型判断不了时按“不确定就唤醒”处理。
- 输出：`{"decision":"wake"|"observe","fulfills_intent":bool,"reason":"≤120字"}`。
  去掉 `through_seq` 回显和 `source_seqs`：宿主知道批次范围，trigger_seq 直接取批次里最后一条新消息。
- 一次调用，超时 8 秒，**不重试**。超时、异常、非法 JSON 一律按 wake 处理（`fulfills_intent=false`），并 `logger.warning` 记录原因。
  不再设置每小时 fallback 预算：主模型 wake 已经受 `max_requests_per_hour` 约束。
- 结果回来后只检查一件事：scope 的 `binding_revision` 是否变化或已暂停（P0-7）；变了就丢弃。
  **不再**冻结输入、重建快照、比对 revision、用 `startswith` 比较文本。消息在判断期间被编辑，按判断结果走，影响可以忽略。
- 成本统计用现有 `nanobot.llm_usage.record_llm_call`，删除 `judge_batches`、`judge_attempts` 两张表
  （迁移时可以把历史行导出成一个 JSON 文件放在实例 backups 下，然后 DROP）。
- 提示词重写为 `templates/telegram/social_judge.md`，只讲 C 层：判断新消息是否满足给定的等待条件、是否被明确叫到；
  不确定时返回 wake。目标长度是现在的三分之一以下。

**4. 交接给主模型：**

- 不管 judge 判 wake，还是 B 层直接 wake，都走同一个 `wakes.schedule(scope, trigger_seq, reason="judge"|"directed"|"conversation")`。
- 现在 `_handoff_judgement` 里“消息已经在运行中的回合里展示过，就不再唤醒”的逻辑（`store.py:322-330`）**保留**，
  但移到 `db/wakes.py`，因为它属于唤醒状态机，不属于 judge。依赖 P0-4 的显式曝光登记。
- `fulfills_intent=true` 且条件是 one-shot 时，清空 `wake_config`（现有语义，保留）。
- wake 入站快照里的 `judgement` 字段改为一行 `admission: {"by":"directed"|"conversation"|"judge","reason":...}`，方便主模型知道为什么被叫醒。

**5. 结果：** judge 相关代码从约 1000 行（`judge.py` 180 + `judge_runtime.py` 369 + `judge_models.py` 172 + store 中约 220 行 + 配置 30 个字段）
缩减到一个约 200 行的 `judge.py` 和 4–5 个配置字段（model、provider、timeout、context_messages、coalesce 秒数）。

### 5.2 主动发言 bug 修复（D2）

- 现状：`proactive.py:178` 的 `elif policy.activity_enabled:` 排在概率 idle 分支之前，只要开启空闲活动，idle 主动开话题永远不会执行。
- 修复：同一次 tick 中按以下顺序判断：
  1. 公共前置条件不变：missed / stopped / disabled / quiet_hours / deferred / busy / member_active。
  2. **先**走 idle 发言判定：检查发送预算、请求预算，计算概率（含待说想法加权、近期发言、夜间、高频系数），抽样。命中就生成 `kind="idle"`。
  3. idle 未命中（或因发送预算不足被跳过）且 `activity_enabled`，再走 activity 判定（`other_topic_active`、请求预算），命中就生成 `kind="activity"`。
  4. `proactive_state.decision` 分别记录两步的结果，例如 `idle:probability_miss;activity:scheduled`，便于在 `social_state` 里观察。
- 这是【行为变更（修 bug）】，单独提交。场景测试至少覆盖三种情况：activity 开启时 idle 仍能命中；idle 未命中时进入 activity；两者都不满足时不调度。
- 修复后要在实际实例上确认：空闲时段能看到 idle 主动发言，而不只是 activity。

### 5.3 工具合并（D5）

现在有 12 个工具，合并为 8 个。合并后每个工具都由 pydantic 请求模型生成 schema，一个工具一个类（P2-1）。

| 合并后 | 吸收的旧工具 / 功能 | 说明 |
|---|---|---|
| `social_send` | 原 `social_send` + `social_sticker(action=send)` | `text` / `parts` / `sticker_id` 三选一；也可以 `sticker_id` 加一句短 `text`（先发贴纸，再发文字，按 parts 规则计费）。贴纸回复由此成为发送的一等方式，符合“先选贴纸”的偏好 |
| `social_wait` | 原 `social_wait` + `social_set_wake` 的等待条件 | 新增可选 `until_condition: {members, topics, questions, one_shot, ttl_seconds}`，表示“等到满足这个条件再叫醒我”，由 §5.1 的 C 层判断。删除 probability、coalesce_seconds、sleep_interrupts 这类模型不该调的参数 |
| `social_state` | 原 `social_get_state` + `social_read_messages` + `social_set_wake(proactive_paused)` | `action="get"`：精简状态，包括 role_id、role_file、模式、等待、剩余预算、proactive_paused，**不再**输出 judge/配置/退役字段；`action="read"`：读取未读或最近消息；`action="set_proactive"`，参数 `paused: bool` |
| 删除 | `social_mark_read` | 回合结束时 `acknowledge_seen` 已自动确认连续展示过的消息，手动标记是冗余操作 |
| `social_media` | 原 `social_get_media` | 只改名 |
| `social_sticker` | 原 `social_sticker`，去掉 send | 只管贴纸库：import_set / view / annotate / list / collect |
| `social_memory` | 不变，但不再接受 `kind=slang` | 见 P1-1 |
| `social_jargon`、`social_expression`、`social_activity` | 不变 | `social_expression` 使用频率低，以后可以并入 `social_jargon` 的“词库”工具，本次不做 |

- 工具描述随 P0-6 一起精简：每个工具 description 不超过 400 字符，规则只写在 protocol 一处，不在描述里重复。
- 工具改名或合并会让会话历史里的旧 tool_call 名字失效。这里不需要兼容层：历史只作为上下文，不会被重放执行。
- 这是【行为变更】，和 P0-6 的提示词改动放在同一个 Phase，一起观察。

---

## 6. 实施顺序

每个 Phase 都是一个或多个独立提交；完成后执行构建 + 启动 + 部署，并在 `docs/` 下追加一份简短实施记录
（沿用 `CODE_REVIEW_FIXES_*.md` 的格式，只写改了什么和验收证据）。

| Phase | 内容 | 风险 | 依赖 |
|---|---|---|---|
| 0 | 学习/选择器异常加日志和 `error_detail`（P1-2 最后一条）；建立 §3 场景测试；评估脚本和夹具移到 `scripts/eval/` | 低 | — |
| 0.5 | 修复 activity 屏蔽 idle 的 bug（§5.2），单独提交并部署。这是唯一不等重构完成就先上线的行为修复 | 低 | 0 |
| 1 | 删除死代码和退役遗留（P1-1 全部，含隔离残留）；schema 迁移 17→18 删掉死表 | 低 | 0 |
| 2 | `validate_context` 去除副作用，persona 缓存（P1-2 第一条）；bindings.py；版本拆分并保留历史（P0-7） | 中 | 1 |
| 3 | 拆 store：建 `db/` 包，按表迁移方法，禁止 `db/` 外写 SQL（P0-2）；wakes 状态机显式化（P0-3） | 高 | 2 |
| 4 | 显式曝光登记取代子串匹配（P0-4）；SocialContextBuilder + hook 瘦身（P1-3） | 高 | 3 |
| 5 | 统一投递日志和策略检查（P0-5）；telegram channel 只保留单一接入点（P2-2） | 高 | 3 |
| 6 | runtime 拆分并去掉反向引用（P0-1）；wiring.py（P2-3）；工具一类一个（P2-1） | 中 | 3–5 |
| 7 | judge 简化（§5.1）；LearningWorker（P1-5）；selection 简化 | 中 | 6 |
| 8 | 提示词外置、去重、移除角色字面量（P0-6）；工具合并（§5.3）【行为变更】 | 中，行为层面 | 6 |
| 9 | schema 基线压缩（P2-4）；配置面收缩（P2-5） | 低 | 1–8 |

**每个 Phase 的完成标准：**

- `ruff check nanobot/` 通过；`basedpyright` 对改动文件没有新增错误。
- 场景测试全部通过（D3 已允许）。
- `./scripts/pipeline.sh release` 构建并启动成功，健康检查通过，实例 `state.json` 指向新版本。
- 部署后在实际群里观察：收到群消息能 wake，能正常发出回复，回复后的等待正常。实施记录里写清楚实际看到的现象，不要写推测。
- 不得出现：重复发送、unknown 重发、`followup` 计数被重置、角色文件或记忆数据丢失。

**量化目标（重构结束时）：**

- `social/runtime.py` 少于 300 行；没有超过 500 行的 social 模块。
- `social/` 中 `db/` 之外 `db.execute` 出现 0 次；测试中 `db.execute` 少于 50 处。
- 主 protocol 模板（不含角色）比现在的 1.5 万字符减少 40% 以上；代码里没有中文提示词字面量，也没有特定角色的字面量。
- 通用核心（`agent/loop.py`、`agent/context.py`、`agent/memory.py`、`session/manager.py`）相对上游 `1bb712d3` 的 diff 明显缩小，
  只保留 `tool_only/persona/context_version`、hook 字段（`end_turn/continue_turn/model_messages`）和 `silent_final` 这类必需的扩展点。

---

## 7. 明确不要做的事

- 不要引入新框架、DI 容器、事件总线、ORM。sqlite3 加小仓储函数就够了。
- 不要为跨 channel 复用设计抽象。social 只服务 Telegram。
- 不要在重构 Phase 里顺手改提示词或行为；行为变更只放在标注了【行为变更】的提交里。
- 不要重置线上数据，包括黑话 failed 批次的游标（AGENTS.md 要求保留证据）。迁移时原样搬过去。
- 不要恢复任何已撤回的设计：社交专用工具白名单、概率采样、本地关键词门槛、哈希比较。
- 不要动 WebUI/TUI。

---

## 8. 实施中发现的追加事项

### A1 主模型持续失败时没有任何提醒（2026-11-03 发现）

- 事实：主模型 provider 从 11-02 12:47 起全部返回 HTTP 402（账户余额不足），持续约 28 小时，用户完全不知情。
  在此期间 judge（独立账号）照常唤醒，主模型每次都失败，空闲活动和 Dream 也全部失败（llm_usage 中 157 次 402）。
- 要求：主模型调用**连续失败**时主动告知 owner，至少满足以下两点：
  1. 连续 N 次（建议 3 次）或持续 M 分钟失败时，打出一条醒目的 `logger.error`，包含 HTTP 状态码和 provider 名称；
  2. 通过已有的 Telegram 私聊通道给 owner 发一条简短提醒（owner 的私聊 chat id 取自配置中已有的 allow_from，或者新增一个显式配置项），
     同一种故障在恢复之前只提醒一次，恢复后再发一条“已恢复”。
- 位置：属于通用 agent 运行时（`agent/runner.py` 的模型错误路径附近，或 `llm_usage` 记录处），不属于 social 模块。
  设计在派发前由 Claude 定稿；建议放在 Phase 1 之后、Phase 3 之前单独做，因为它与重构无关，且价值很高。
