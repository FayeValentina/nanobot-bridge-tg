# Social 重构实施进度

计划见 [SOCIAL_ARCHITECTURE_REVIEW.md](SOCIAL_ARCHITECTURE_REVIEW.md)，实施流程见 [`.agent/orchestration.md`](../.agent/orchestration.md)。
每个任务验收并部署后追加一行。“验收证据”只写实际运行的命令和结果。

## 交接：当前状态与下一步（新对话从这里开始读）

**最后更新：2026-11-03，线上版本 `cf1a492e`（P3-a），social schema 20。**

- **已完成**：Phase 0、0.5、1、2，以及追加项 A1。场景测试 21 个，全部通过，零 xfail。
- **进行中：Phase 3**（owner 2026-11-03 确认开始）。只读事实清单 P3-0 已完成：`.agent/tasks/P3-0.report.md`
  （wakes 状态转换表、表归属矩阵、15 条疑点 Q1–Q15），后续简报的事实以它为准，但每条仍要用 grep 复核。
  Phase 3 拆为三个施工任务：
  - **P3-a**（已完成，纯搬迁）：建 `social/db/`（connection、schema 迁入、Scopes、Messages、Views、StickerLibrary），
    把全仓库对 scopes 身份/计数列、messages、updates、views、stickers、runtime_settings 的 SQL 搬进仓储；不改事务边界。
  - **P3-b**（已完成）：`db/wakes.py` 状态机。拥有 wakes、requests、dialogue_waits/jobs/state 和 scopes 的调度列；
    `WakeStatus`/`WakeReason`/`ScopeMode` 枚举；所有 status 更新经过带前置状态 WHERE 的转换函数，终态不可再改；
    引入 `transaction()`（已在事务内则不开新事务）。计划顺带修的疑点：Q2（发布失败回退覆盖终态且不退请求预算）、
    Q3（终态 proactive 行被改成 cancelled）、Q9（终态清理只挂在 `_schedule` 上）、Q12（`recovery_jobs` 返回无用的旧值）。
  - **P3-c**（下一步）：`db/judge.py`、`db/deliveries.py`（只搬迁，表合并留给 Phase 5）；知识/学习/proactive_state 各表单一归属
    （SQL 只在拥有者模块中）；解决 memory↔jargon 延迟导入；SocialStore 只保留跨仓储的事务编排；在 pipeline 检查中加 grep 规则。
  - **Claude 的设计决定（偏离审查文档的量化目标）**：知识、学习、活动、proactive_state 这些表在 Phase 3 仍由各自模块
    （memory/jargon/followups/activity/learning/expressions/jargon_learning/proactive）直接执行 SQL，只要求“每张表只有一个模块写 SQL”；
    “`db/` 之外 0 处 `db.execute`”推迟到 Phase 6/7 重组 knowledge/learning 包时完成。
  - 不在 Phase 3 处理的疑点：Q4/Q5（曝光消费 wake 时不改 scope.mode，留给 Phase 4 曝光重做）、Q6（judge pending_first 残留，Phase 7）、
    Q10/Q11/Q13–Q15（记录在案，按所属 Phase 处理）。Q1（遗留 `directed` reason）线上库中 0 行，枚举化后不再出现。
  - 线上库观察（2026-11-03 副本）：wakes 中 proactive failed 171 行（402 欠费期间），终态行最早约 4 天前，印证 Q9。
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
| P3-b `db/wakes.py` 显式 wake 状态机【小幅行为修正】 | 3 | 待填 | 待填 | 社交目录 + 契约 + gateway 981 passed（Claude 复跑），场景测试未改动；ruff 全仓干净；basedpyright social/ 0 errors；wakes.py 中设置 status 的只有 `_move` 一处；**线上库副本演练**：`recover()` + `ready()` 正常，Q9 清理删除一天前的终态 wake（330→125 行），dialogue_jobs 级联 33→2，messages 不变 | 修正 Q2/Q3/Q9/Q12，没有测试断言旧行为。Claude 补修 2 处：(1) 简报给的 `transaction` 返回注解 `Iterator[None]` 被 basedpyright 判为弃用，改为 `Generator[None, None, None]`；(2) `proactive._cancel` 仍是旧式 `with self.db:`，块内第一条就调用已改用 `transaction()` 的 `store.cancel_opportunity`，会把原来一个事务拆成两个，改为 `transaction()`。store.py 1036→627 行，wakes.py 526 行（略超 500 的目标，Phase 6 拆 runtime 时再看），生产代码净增 120 行。**遗留**：其他模块仍有旧式 `with self.db:`，P3-c 统一改为 `transaction()` |
