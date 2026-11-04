# Social 重构实施进度

计划见 [SOCIAL_ARCHITECTURE_REVIEW.md](SOCIAL_ARCHITECTURE_REVIEW.md)，实施流程见 [`.agent/orchestration.md`](../.agent/orchestration.md)。
每个任务验收并部署后追加一行。“验收证据”只写实际运行的命令和结果。

## 交接：当前状态与下一步（新对话从这里开始读）

**最后更新：2026-11-04，线上版本 `7b9fe9d2`（P5-a），social schema 21，**

- **已完成**：Phase 0、0.5、1、2、3，以及追加项 A1。测试 982 个（社交目录 + 契约 + gateway），场景测试 21 个，全部通过，零 xfail。
- **Phase 3 结果**（P3-0 只读清单 + P3-a～P3-d，明细见下表）：`social/db/` 有 connection、schema 和 8 个仓储
  （scopes/messages/views/stickers/wakes/judge/deliveries/proactive_state）；wake 状态机在 `db/wakes.py`（`WakeStatus`/`TRANSITIONS`/`_move`，终态不可变）；
  store.py 1213→约 300 行，只做跨仓储编排；`test_social_sql_ownership.py` 固定可以执行 SQL 的文件。
  P3-a 部署后线上已有真实活动正常走过新代码（1 次 judge wake、3 次主动机会完成、1 次发送成功，无异常）。
- **Claude 的设计决定（偏离审查文档的量化目标）**：知识/学习/活动表仍由各自拥有者模块（memory/jargon/followups/activity/learning/expressions）
  执行 SQL，只要求每张表只有一个拥有者；“`db/` 之外 0 处 `db.execute`”推迟到 Phase 6/7 重组 knowledge/learning 包时完成。
- **Phase 4 已完成**（owner 2026-11-04 确认开始；P4-a 的行为变更待 owner 在群内体验反馈）。只读清单 P4-0 已完成（`.agent/tasks/P4-0.report.md`）。主要事实：
  快照只有两个生产来源（`_publish_wake` 入站消息、`social_read_messages` 工具返回）；曝光判定点 13 处，核心是 `note_seen` 和 `selection.input`
  两处整串子串匹配；busy-time wake 是运行中途直接注入的用户消息，不经过 claim，所以仍需曝光时收取（`admit_view_job`）。
  Claude 的设计：**不改登记时机**（仍在 provider 返回后用 `model_messages` 登记），只改识别方式：快照 JSON 首尾各有一个 view_id 标记
  （`view_id` 本来就是第一个字段，新增末尾字段 `view_end`），两者都出现才算完整看到。拆为：
  - **P4-a**（已完成，待 owner 群内体验）：首尾标记 + `complete_view_ids()`，替换 `note_seen` 与 `selection.input` 的子串匹配；`displayed_bodies` 排序补 rowid；
    修 Q4（曝光收取的 wake 也执行 claim 的 scope 迁移）。【小幅行为变更】
  - **P4-b**（已完成）：`SocialContextBuilder`（memory/selection/近期用语/活动 → RuntimeContextBlock），hook 只负责注入、恢复和曝光登记。纯重构。
  - P4-0 中**暂不处理、已记录**的疑点：max-iterations 的无工具收尾请求绕过曝光 hook；compaction 开启时临时注入块会留在 accepted_messages 和摘要输入中；
    发布“尝试”而非回执决定 unsent 修复；Q5（wake 过期后 mode 仍为 active）经核实只在 turn 运行期间存在，finish 时会复位，不是 bug；
    但 wake 在调度 120 秒后过期、长 turn 中 social 工具会因此被拒，留给 Phase 6/8 评估；
    `scripts/evaluate_social_selection.py`、`evaluate_social_v5.py` 仍调用已不存在的 `store.wake(...)`，评估脚本迁移时修。
- **进行中：Phase 5**（owner 2026-11-04 确认开始）。只读清单 P5-0 已完成（`.agent/tasks/P5-0.report.md`：两表字段/状态、21 个读取点、
  发送检查矩阵与 10 类差异、四种发送的生命周期、Telegram 7 处接入点、22 条疑点）。线上数据（11-04 副本）：actions 333 行
  （text sent 295、sticker sent 28、failed 10、88 组多段），message_deliveries 9 行（sent 6、failed 3），每行在 actions 中都有同 turn 的预约行但 ID 不同。
  Claude 的设计：不新建表，把 message_deliveries 并入 `actions`（表名不变，避免测试大量改 SQL），kind 增加 `message`，`message_id`→`message_ids` JSON 数组，
  一次发送一行；投递终态不可改。拆为：
  - **P5-a**（已完成）：schema 21 合并两表 + 全部读取方 + 普通 message 改为 `begin_message`/`finish_message` 两个方法（channel 不再碰 `social.store`）
    + 疑点 7（claim 失败同步跟进回执）、疑点 8（终态保护）。【小幅行为变更】
  - **P5-b**：单一 `check_send_allowed`，替换 reserve_message_send/send/send_parts/reserve_send/enqueue_parts 中重复的检查；
    统一 queued 文案与 unknown 保护（不再区分 kind，贴纸内容为 `[sticker:id]` 不会与文字冲突）。各工具的幂等语义（social_send 返回旧回执、
    多段整组返回、message 工具拒绝重复）属于工具语义，保留不动。
  - **P5-c**：Telegram 只保留 group=-1 一个 social 入口（P5-0 确认 /start、/help、命令队列、编辑分支、普通 worker 的 5 处调用在稳定配置下都被 group=-1 覆盖）。
  - 评估脚本 `scripts/` 中读取 message_deliveries 的部分会随 schema 21 失效，与已失效的 `store.wake` 一起留给评估脚本迁移。
- **留给后续 Phase 的已知事项**：learning.py 直接读写 expressions/jargon 表、selection 动态候选查询（Phase 7）；
  `Wakes._check_trigger_source` 读 judge_batches 的只读例外（Phase 7 judge 简化时重看）；P3-0 疑点 Q4/Q5（Phase 4）、Q6（Phase 7）、
  Q10/Q11/Q13–Q15（按所属 Phase）。P3-0 清单 `.agent/tasks/P3-0.report.md` 的行号已过时，只作线索。
- **owner 决策**（D1–D5 详见审查文档 §5）：judge 保留但要简化（§5.1，属于 Phase 7）；idle/activity 已修好（D2）；允许场景测试（D3）；
  改配置时保留历史（D4，已在 P2-a 实现）；工具从 12 个合并为 8 个（§5.3，属于 Phase 8）。

**线上配置（owner 已同意的改动）**
- `gateway.modelFailureAlert.chatId = 2041312857`（owner 本人的 Telegram ID），连续 3 次失败告警；2026-11-03 17:56 的 503 告警已实际送达。
- 空闲活动降本：`activityIntervalSeconds 3600`、`activityMaxIterations 4`（原为 600 / 8）。主模型为 DeepSeek `deepseek-flash`，
  2026-11-03 曾因余额不足（HTTP 402）中断 28 小时。每次调用的基础上下文约 3.5 万 token，Phase 4、8 会从根上压缩。

**环境前提**
- `/etc/apparmor.d/bwrap`（2026-11-03 经 owner 同意添加）：没有它 Codex 沙箱无法启动。
- Codex 派发命令、`.agent/codex_pytest_env.py` 沙箱插件、发布规则（**禁止从 worktree 部署**）都写在 `.agent/orchestration.md`。

**已知的既有问题（重构前就存在，尚未安排）**
- 核心测试 20 个失败（task_cancel、unified_session、session_fsync、message_tool_suppress 等）；`tests/cli/test_commands.py` 9 个失败
  （测试里伪造的 `make_provider` 不接受 `preset` 参数，计划在 Phase 6 修）。这两组测试都不在发布管线中。
- 发布脚本回滚时会沿用失败那次部署的挂载参数（A1 事故中暴露），以后改到部署脚本时修。
- memory 和 jargon 之间有一个延迟导入，Phase 3 按表重新划分时解决。
- `nanobot/channels/telegram/tests/evaluate_*` 评估脚本和大夹具还没移到 `scripts/eval/`（原 P0 计划项，优先级低）。

## 任务记录

| 任务 | 阶段 | 提交 | 部署 | 验收证据 | 偏差 / 遗留 |
|---|---|---|---|---|---|
| P0-a 后台调用失败可诊断 | 0 | `8a0e9338` | 2026-11-03 healthy，实例 schema 18 | 社交测试目录 916 passed；ruff 干净；basedpyright social/ 0 errors；线上库副本迁移 17→18，各表行数不变；部署后容器 healthy，日志无新异常 | Claude 补修：13 处写死的 schema=17 断言改为引用 `SCHEMA_VERSION`；store.py 两个历史遗留 lint 问题。线上库已无 failed 批次，此前失败的黑话批次原因无法追溯 |
| P0-b 黑盒场景测试套件 | 0 | `5dba81ac` | 无需部署（仅测试） | 场景 16 passed + 2 xfailed（strict，对应 §5.2 bug 和 P0-7）；社交测试目录 932 passed 2 xfailed；场景文件无 db/store/私有访问；ruff 干净 | 退回 Codex 写进 conftest 的两个沙箱补丁，改为 `.agent/codex_pytest_env.py`（只在派发 Codex 时加载）。第 8 阶段工具合并时需同步更新场景中的工具名 |
| P0.5 idle/activity 独立计时器【行为变更】 | 0.5 | `14d30aba` | 2026-11-03，实例 schema 19 | 场景 19 passed 1 xfailed（原 xfail 的 idle 场景转为通过，新增 2 个计时器场景）；社交测试目录 935 passed 1 xfailed；ruff 干净；basedpyright 0 errors；线上库副本迁移 18→19，行数不变，3 个话题的计时迁移后保留原到期时间 | 修改 test_social_activity 3 处 decision 断言为新格式 `idle:..;activity:..`；Claude 补修 1 处写死 schema=18 的断言。观察项：allTopics 下 idle 按话题各自计时（与 V4 原行为一致） |
| P1-a 删除社交模块死代码与退役路径 | 1 | `58c10e15` | 2026-11-03，实例 schema 20 | 社交目录 + 契约 + gateway/CLI 测试共 1118 passed 1 xfailed；ruff 干净；basedpyright 0 errors；线上库副本迁移 19→20：删除 dialogue_attention（0 行）、wake_samples（2 行退役概率样本），其余表行数不变 | 验收拦下启动崩溃：`gateway_runtime.py` 仍引用被删除的 `allows_personal_delivery`（简报中的事实核实有误）。Claude 修正：删除上游 heartbeat 目标选择中的 `target_allowed` 参数（恢复上游原样）、孤立方法 `last_shown_member_text`、协议中的双空格。生产代码净减约 85 行 |
| A1 主模型连续失败告警 | 追加 | `faebe8fa` | 2026-11-03 healthy（首次从 worktree 部署导致启动失败、线上中断约 5 分钟，在主仓库执行 deploy 后恢复，详见 orchestration.md 第 5 步）；实例配置写入 `gateway.modelFailureAlert.chatId=2041312857`（owner 确认），旧配置备份为 `config.json.before-model-alert-20261103` | tests/llm_usage + tests/cli/test_gateway_runtime + tests/config 134 passed；社交目录 927 passed 1 xfailed；ruff 干净；basedpyright 0 errors | 发现既有问题：`tests/cli/test_commands.py` 有 9 个失败，在重构前的基线 `5da0464f` 上同样存在（测试里伪造的 `make_provider` 不接受 judge 装配传入的 `preset` 参数），不在发布管线测试集中。归入 Phase 6（gateway 装配）一并修复 |
| P1-b 黑话只保留 social_jargon 一套接口 | 1 | `29b6491d` | 2026-11-03 | 社交目录 + 契约 952 passed 1 xfailed；ruff 干净；basedpyright 0 errors；全仓库 `is_jargon|jargon=True|.legacy(|_list_local|jargon_status` 0 命中 | 生产代码净减 84 行。`social_memory(kind="slang")` 由参数校验拒绝；memory 的 clear 仍会联动清除黑话（`JargonLibrary.clear`）。memory↔jargon 之间仍有一个延迟导入，留给 Phase 3 按表重新划分时解决 |
| P1-c 移除核心中的隔离模式残留 | 1 | `d123bc15` | 2026-11-03 | 社交目录 + 契约 951 passed 1 xfailed；核心 tests/agent+session+tools+gateway 2654 passed，20 failed，与改动前基线逐项相同（0 新增，均为既有问题）；ruff 全仓干净；残留名称全仓 grep 0 命中；5 个核心文件与上游的差异从 727 行降到约 600 行 | **Claude 修正简报的设计错误**：`prompt_policy()` 除了传递隔离字段，还负责在压缩社交会话时把群聊人设传给摘要模型。Codex 发现冲突并上报（3 个测试失败），Claude 在 archiver 和 token 探测两处直接传入 `trusted_persona=session.policy.persona`。另修 3 个既有 lint 问题。**既有问题待安排**：核心测试有 20 个失败（task_cancel、unified_session、session_fsync、message_tool_suppress 等），不在发布管线测试集中 |
| P2-a ScopeBindings、校验纯读、配置或角色变化时保留历史【行为变更】 | 2 | `e68b19bc` | 2026-11-03；同时生效 owner 要求的活动降本配置（activityIntervalSeconds 600→3600，activityMaxIterations 8→4，旧配置备份为 `config.json.before-activity-cost-20261103`） | 社交目录 + 契约 971 passed，**零 xfail**（场景套件 21 个全部通过）；ruff 全仓干净；basedpyright social/ 0 errors；`_persona(` 全仓 0 命中；`validate_context` 有测试证明不写库；**线上会话副本演练**：3 个真实群聊会话（2713/632/161 条消息）连续加载两次，归档位置不变，模式都识别为 social | Codex 第一版找出简报的两个设计缺陷并上报，Claude 修正后在同一会话返工 1 轮：(1) version 改为由 role_id+paused 决定，否则取消暂停后无法恢复；(2) 采用旧会话时必须持久化 `_social_mode`，否则第二次加载仍会截断。runtime.py 1230→1023 行，新增 bindings.py 248 行（其中 SOCIAL_PROTOCOL 约 160 行）。部署时 scope version 格式变化，会一次性取消在途 wake |
| P3-a 建立 db/ 包，搬迁 scopes/messages/views/stickers 的 SQL | 3 | `cf1a492e` | 2026-11-03 healthy，schema 仍为 20，启动日志无异常栈 | 社交目录 + 契约 + gateway 981 passed（Claude 复跑；较 P2-a 的 971 多出的是 gateway 测试，社交目录本身 947）；ruff 全仓干净；basedpyright social/ 0 errors；schema 文件迁移前后内容逐字相同；diff 逐文件审阅均为原 SQL 原样替换为仓储方法 | store.py 1213→1036 行；新增 db/ 358 行（不含 schema），生产代码净增 150 行（类与方法签名开销，P3-b/c 会收回）。Codex 沙箱内 `git mv` 因 .git 只读失败，改为普通移动，由 Claude 提交时登记。时钟测试 `test_expiry_hides_fact...` 原先构造后改写 `store.now`，仓储已捕获时钟，改为在测试内构造 runtime 并传入时钟（断言不变） |
| P3-b `db/wakes.py` 显式 wake 状态机【小幅行为修正】 | 3 | `c2668524` | 2026-11-03 healthy，schema 20，日志无异常栈 | 社交目录 + 契约 + gateway 981 passed（Claude 复跑），场景测试未改动；ruff 全仓干净；basedpyright social/ 0 errors；wakes.py 中设置 status 的只有 `_move` 一处；**线上库副本演练**：`recover()` + `ready()` 正常，Q9 清理删除一天前的终态 wake（330→125 行），dialogue_jobs 级联 33→2，messages 不变 | 修正 Q2/Q3/Q9/Q12，没有测试断言旧行为。Claude 补修 2 处：(1) 简报给的 `transaction` 返回注解 `Iterator[None]` 被 basedpyright 判为弃用，改为 `Generator[None, None, None]`；(2) `proactive._cancel` 仍是旧式 `with self.db:`，块内第一条就调用已改用 `transaction()` 的 `store.cancel_opportunity`，会把原来一个事务拆成两个，改为 `transaction()`。store.py 1036→627 行，wakes.py 526 行（略超 500 的目标，Phase 6 拆 runtime 时再看），生产代码净增 120 行。**遗留**：其他模块仍有旧式 `with self.db:`，P3-c 统一改为 `transaction()` |
| P3-c `db/judge.py` 与 `db/deliveries.py`（纯搬迁） | 3 | `7ec03fa3` | 2026-11-04 healthy，schema 20，日志无异常栈 | 社交目录 + 契约 + gateway 981 passed（Claude 复跑）；ruff 全仓干净；basedpyright social/ 0 errors；`_judge_current` 私有访问全仓 0 处；线上库副本上 `recover()`/`judge.recover()`/`ready()` 正常 | store.py 627→325 行；judge.py 293 行、deliveries.py 164 行；生产代码净增 120 行。唯一保留的跨表读取例外：`Wakes._check_trigger_source` 读 judge_batches（Judge 依赖 Wakes，反向会循环），已加注释。Claude 小修：`Wakes.any_active` 原被实现成由调用方传入 mode/status 字面量，改为无参并使用枚举。`expressions.busy` 的三段 UNION 按简报拆成 `wakes.any_active() or judge.any_pending()` |
| P3-d Phase 3 收尾：单一归属、schema 集中、事务统一、SQL 归属测试 | 3 | `5e484184` | 2026-11-04 healthy，schema 20，日志无异常栈 | 社交目录 + 契约 + gateway 982 passed（新增 `test_social_sql_ownership.py`）；ruff 全仓干净；basedpyright social/ 0 errors；store/runtime/proactive/judge_runtime/jargon_learning 中 0 处 SQL；db/ 之外旧式 `with db:`/nullcontext 0 处；Codex 先逐条核对了全部块外写语句，均有调用方事务包住，没有依赖“顺带提交”的路径；**线上库副本演练**：打开（经迁入 db/schema.py 的升级函数）、recover、judge/learning recover、consume_timers、ready 后 `in_transaction` 均为 False | memory↔jargon 循环解开（`MemoryEvidence` 移到 models，`displayed_evidence` 移到 `Views`）；新增 `db/proactive_state.py`；4 个 schema 升级函数迁入 db/schema.py；`savepoint()` 取代手写 SAVEPOINT。**Claude 退回一处迎合测试的改动**：Codex 为了躲过子串扫描，把 jargon_learning 中两处 `self.tools.execute(...)` 改成局部变量绑定；已恢复原代码，并把测试的匹配串收窄为 `db.execute(` 等（social 代码中所有连接变量均名为 db，已 grep 核实）。生产代码净增 140 行（超出预期 80 行，均为方法签名）。**留给 Phase 7**：learning.py 对 expressions/jargon 表的读写、selection 的动态候选查询 |
| P4-a 快照曝光改为按 view_id 首尾标记识别【小幅行为变更】 | 4 | `a4dcb52b` | 2026-11-04 healthy，日志无异常栈；待 owner 群内体验 | 社交目录 + 契约 + gateway 983 passed（新增：默认分隔符重新编码的快照仍能登记并 mark_read）；场景测试未改；ruff 全仓干净；basedpyright social/ 0 errors；`in model_text` 子串判断全仓 0 处；手工核对 `complete_view_ids`：完整快照识别、只有开头的截断快照不识别、带空格的重新编码识别 | 行为变化：快照 JSON 末尾多一个 `view_end` 字段；Q4 已修（曝光收取的 wake 也执行 `_enter_running`）。**返工 1 轮**：简报自相矛盾（识别正则只认紧凑分隔符，验收测试却用默认分隔符），Codex 停下来上报，Claude 修订正则容许冒号两侧空白。顺带发现 `codex exec resume` 不接受 `-C`/`-s`，已更正 orchestration.md |
| P4-b `SocialContextBuilder`，hook 只负责注入/恢复/登记（纯重构） | 4 | `64a4d33d` | 2026-11-04 healthy，日志无异常栈 | 社交目录 + 契约 + gateway 983 passed（Claude 复跑）；场景测试未改；ruff 干净；basedpyright social/ 0 errors；注入文本逐字未改（diff 核对） | 新增 `social/context.py`（`transcript_text` 合并了 hook 中两段相同的文本提取；`SocialContextBuilder.build` 不持有 runtime）；删除无人读取的 `_last_memory_body`；social/runtime.py 990→938 行。唯一差别：找不到注入目标时多一次只读的 running_activity 查询（Codex 如实指出，可忽略） |
| P5-a 投递日志合并为一张表（schema 21）【小幅行为变更】 | 5 | `7b9fe9d2` | 2026-11-04 healthy；线上库已迁移到 schema 21（message_deliveries 已删除，actions 348 行），日志无异常栈 | 社交目录 + 契约 + gateway 984 passed（新增 schema 20→21 合并迁移测试）；场景测试未改；ruff 干净；basedpyright 0 errors；channel 中 `social.store` 0 处；**线上库副本迁移演练**：actions 333→342（+9 条 message），message_deliveries 已删除，323 个 message_id 全部转入 message_ids，9 条 message 行的 id/status/message_ids 与原表逐条一致，3 个 followup 引用 ID 均可解析，recover 正常，quick_check ok，外键检查无问题 | **返工 1 轮**：简报遗漏跨工具 kind 条件（合并后普通 message 的 kind 不再是 text），Codex 停下上报；Claude 决定 `unknown_content` 不再按 kind 区分、`latest_content` 对 text 同时匹配 message。**Claude 补修**：终态保护使 `queue_retry` 越界分支先写 failed、随后 `store.delivery(failed)` 被拒、跟进回执停在 queued（Codex 复现并上报），改为越界时只返回 False，由调用方经 `store.delivery` 记失败。生产代码净减少约 10 行。scripts/ 中 evaluate_social_v5、freeze_social_v5_baseline 受影响，留给评估脚本迁移 |
