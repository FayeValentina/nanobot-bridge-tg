# Social 重构实施进度

计划见 [SOCIAL_ARCHITECTURE_REVIEW.md](SOCIAL_ARCHITECTURE_REVIEW.md)，实施流程见 [`.agent/orchestration.md`](../.agent/orchestration.md)。
每个任务验收并部署后追加一行。“验收证据”只写实际运行的命令和结果。

## 交接：当前状态与下一步（新对话从这里开始读）

**最后更新：2026-11-04，线上版本 `64b201a1`（P8-a，Phase 8 进行中），social schema 22。** owner 已体验 Phase 5、B1、P7-a、B2-a，反馈正常；P8-a 待 owner 群内体验。

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
- **Phase 5 已完成**（owner 2026-11-04 确认开始；待 owner 在群内体验普通回复、多段、贴纸、message 工具）。只读清单 P5-0 已完成（`.agent/tasks/P5-0.report.md`：两表字段/状态、21 个读取点、
  发送检查矩阵与 10 类差异、四种发送的生命周期、Telegram 7 处接入点、22 条疑点）。线上数据（11-04 副本）：actions 333 行
  （text sent 295、sticker sent 28、failed 10、88 组多段），message_deliveries 9 行（sent 6、failed 3），每行在 actions 中都有同 turn 的预约行但 ID 不同。
  Claude 的设计：不新建表，把 message_deliveries 并入 `actions`（表名不变，避免测试大量改 SQL），kind 增加 `message`，`message_id`→`message_ids` JSON 数组，
  一次发送一行；投递终态不可改。拆为：
  - **P5-a**（已完成）：schema 21 合并两表 + 全部读取方 + 普通 message 改为 `begin_message`/`finish_message` 两个方法（channel 不再碰 `social.store`）
    + 疑点 7（claim 失败同步跟进回执）、疑点 8（终态保护）。【小幅行为变更】
  - **P5-b**（已完成）：单一 `check_send_allowed`，替换 reserve_message_send/send/send_parts/reserve_send/enqueue_parts 中重复的检查；
    统一 queued 文案与 unknown 保护（不再区分 kind，贴纸内容为 `[sticker:id]` 不会与文字冲突）。各工具的幂等语义（social_send 返回旧回执、
    多段整组返回、message 工具拒绝重复）属于工具语义，保留不动。
  - **P5-c**（已完成）：Telegram 只保留 group=-1 一个 social 入口（P5-0 确认 /start、/help、命令队列、编辑分支、普通 worker 的 5 处调用在稳定配置下都被 group=-1 覆盖）。
  - 评估脚本 `scripts/` 中读取 message_deliveries 的部分会随 schema 21 失效，与已失效的 `store.wake` 一起留给评估脚本迁移。
- **追加项 B1（连发合并回复，owner 2026-11-04 反馈）**：B1-0 只读分析（`.agent/tasks/B1-0.report.md`）确认根因是 1.5 秒合并窗口 + 运行中的 turn 看不到后到消息 + 发送前无检查。
  owner 选择：B1-a 发送前收取新消息（每 turn 最多拦一次，返回新快照，曝光后后续 wake 并入当前 turn）+ 合并窗口 10 秒。
  线上配置已改：`coalesceSeconds 10`、`dynamicWait.quietSeconds 10 / unfinishedSeconds 10 / maxSeconds 30`（备份 `config.json.before-burst-20261104`）。
  代价：单条消息回复约慢 8.5 秒。**owner 实测通过**（14:22:54–14:23:24 每 10 秒一条共 4 条 → 两个 judge wake 并入同一 turn、一次 3 段回复覆盖全部、发送前收取触发 1 次）。
  **之后 owner 要求窗口改为 20 秒**：Claude 小修 `90abae4a`（schema 中 coalesceSeconds、dynamicWait.quietSeconds 上限 10→60），线上配置改为 coalesceSeconds 20、maxCoalesceSeconds 20、quietSeconds 20 / unfinishedSeconds 20 / maxSeconds 45（备份 `config.json.before-burst20-20261104`）；单条消息回复约慢 18.5 秒。
  **2026-11-05 owner 觉得回复偏慢，要求改回 10 秒**：线上配置恢复 coalesceSeconds 10（去掉 maxCoalesceSeconds，用默认）、quietSeconds 10 / unfinishedSeconds 10 / maxSeconds 30（备份 `config.json.before-burst10-20261105`），重启后 healthy。protocol 中 `purpose="reply", seconds=12` 的示例在 20 秒窗口下会先于安静判定超时，回到 10 秒后重新成立；P8-b 改 protocol 时改为不写死秒数。
  观察项：同日 14:22 一次 proactive turn 的 V5 selector 报 `ValidationError ... Choice string_type`（selection 降级为空，不影响回复），来源未查；已知未覆盖：judge 批次含未曝光新消息时仍可能另起 turn；普通 message 工具不做检查。
- **Phase 6 已完成**（owner 2026-11-04 确认开始）。只读清单 P6-0 已完成（`.agent/tasks/P6-0.report.md`：runtime 46 个方法的归属、6 个持 runtime 的组件及其访问面、gateway 装配、test_commands 9 个失败的原因、工具层、测试面、19 条疑点）。
  Claude 的设计约定：组件接收 `config: Callable[[], TelegramSocialConfig]`、`now`、`notify` 回调和具体仓储/服务；runtime 不保留被移走方法的转发，调用方直接改用新位置（测试机械改名）。拆为：
  - **P6-a**（已完成）：`waiting.py`（SocialWaiting）、`media.py` 的 SocialMedia、`validate_context` 移入 ScopeBindings；judge/stickers 不再持 runtime，`reportPrivateUsage` 抑制全部删除。
  - **P6-b**（已完成）：`delivery.py`（全部发送路径 + send_sticker + 普通 message 记账）、`hook.py`。
  - **P6-c**（已完成）：Expression/Jargon learner、ContextSelector 去反向引用；工具一个类一个（名字/参数/语义不变，基类保留捕获 RuntimeError）。
  - **P6-d**（已完成）：`wiring.py`（`build_social_runtime` / `SocialWiring.connect_agent/refresh/close`），gateway 不再导入 social 内部；Telegram 或 social 未启用时不装配（之后开启需重启）；test_commands 的 9 个失败随之消失，替身无需修改。
  Phase 6 结果：social/runtime.py 960→352 行（目标 <300 留给 Phase 7/8 继续收缩，get_state 43 行、policy_for_session 38 行仍在）；social/ 中没有任何组件持有 runtime。
  P6-0 疑点中暂不处理、已记录：recover 标记提前写入、close 只取消不等待、set_wake 普通分支不 notify、文件 watcher 不刷新 social、channel 构造时用自身 social config 覆盖 gateway 补的 OAuth 路径。
  `tests/channels` 的 4 failed + 1 error 是既有问题，尚未安排。
- **Phase 7 已完成**（owner 2026-11-04 确认开始）。只读清单 P7-0 已完成（`.agent/tasks/P7-0.report.md`）。主要事实：线上 16 小时 judge 41 批，33 wake / 8 observe，observe 全部是另一 bot 的 /usage 回执；
  selector ValidationError 原因是模型把 jargon 返回成对象且引用了不存在的候选；黑话旧 failed 批次现在是 cancelled/scope_changed（迁移按实际数据保留，不改回）。
  **owner 决定**：其他 bot 的非 directed 消息仍经 judge（写入审查文档 §5.1）。拆为：
  - **P7-a**（已完成，owner 已体验通过）：准入分层。真人普通消息与 directed 本地 wake（不调 judge），bot 消息与选择性等待下的消息仍调 judge；保留合并窗口、批次表与 `_handoff`。【行为变更】
  - **P7-b**（已完成）：judge 内部重写（内存 PendingBatch、单次 8 秒不重试、删 judge_batches/judge_attempts（导出历史到 backups）、新提示词、`record_llm_call` 用量、已曝光复用移到 `db/wakes.py`、快照 `admission` 字段）。
    P7-0 疑点 3–10、18、19 在此设计时逐条决定。
  - **P7-c**（已完成）：LearningWorker（保留 jargon 多阶段调用与逐次记账的差异，疑点 16–17）。
  - **P7-d**（已完成）：selection 简化（一 turn 一次、turn 结束清缓存、调用后不复核）、修 ValidationError、动态 SQL 迁到各 Library、learning.py 跨拥有者 SQL 迁出。
  `tests/channels` 的 4 failed + 1 error 是既有问题，尚未安排。评估脚本（scripts/ 与 tests/evaluate_*）多处已失效（store.wake、message_deliveries、冻结 hook 编译），留给评估脚本迁移。
- **Phase 8 进行中**（owner 2026-11-04 确认开始）。只读清单 P8-0 已完成（`.agent/tasks/P8-0.report.md`：主模型收到的全部提示来源与长度、protocol 逐条的强制/重复/冲突、版本路径（protocol 变化只清 provider_state，不违反 D4）、中文与角色字面量、12 个工具的 schema 与线上调用统计（906 次）、§5.3 合并事实、测试面、18 条疑点）。
  - **P8-a**（已完成，待 owner 群内体验）【行为变更】：protocol 由 Claude 重写并外置为 `templates/telegram/social_protocol.md`（15,237→8,869 字符，-41.8%），按 P7 后的准入机制更新；每次迭代注入的五段中文指导（facts_note、expression_guidance、pending_note、memory note、近期用语 note）并入 protocol 后删除；发送后不再要求每次显式调 social_wait（运行时本来会自动注册短等）。角色口头禅改为配置 `learning.catchphrases`（线上已填原四词，备份 `config.json.before-catchphrases-20261104`），代码与学习模板中不再有角色字面量。
  - **P8-b**（下一步）：工具合并 §5.3【行为变更】，实际保留 9 个（§5.3 表格逐个数是 9，不是 8）：send 吸收贴纸发送（需 parts 持久化 kind/media_ref）、wait 吸收 set_wake 条件、state 合并 get_state/read_messages/proactive_paused、删 mark_read（线上用过 98 次；与回合末自动确认的差异见 P8-0 §5.4）、get_media→media；工具描述 ≤400 字符；protocol 同步改工具名。
  - 非角色的中文数据（SCENE_HINTS、jargon 九个普通词、泛化口号、辱骂正则、研究查询后缀）作为数据保留，不算提示词字面量（Claude 决定）。
  - 观察项：expression 学习批次 `9f396031` 在 P8-a 前（12:58 起）已两次 `invalid extraction response`，改配置后按 config_changed 取消，同窗口重建为 `521226af`；若新批次仍失败，需查 expression 学习模型输出。
  Codex CLI 登录凭证 2026-11-04 曾失效（refresh token 被其他 Codex 进程用掉），owner 重新登录后恢复；再出现时请 owner 运行 `codex logout && codex login`。
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
- 核心测试 20 个失败（task_cancel、unified_session、session_fsync、message_tool_suppress 等），不在发布管线中。（`tests/cli/test_commands.py` 的 9 个失败已在 P6-d 解决。）
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
| P5-b 单一发送许可检查【小幅行为变更：文案统一】 | 5 | `b7f8bf3f` | 2026-11-04 healthy，日志无异常栈 | 社交目录 + 契约 + gateway 984 passed（Claude 复跑）；场景测试未改；ruff 干净；basedpyright 0 errors；暂停/冷却/排队/unknown/预算五条文案在生产代码中只剩 `_check_send_allowed` 一处（暂停另见 validate_context） | `TelegramSocialRuntime._check_send_allowed` 按“暂停→proactive→冷却→排队→unknown（逐段）→预算”检查；reserve_send/enqueue_parts 只写不判（删除恒为真的 version 比较与 limit 参数）；删除重复查询 `unknown_part`。各工具的幂等语义保留在入口。生产代码净减 13 行。测试只改 1 处文案断言和若干 limit 实参 |
| P5-c Telegram 只保留一个 social 观察入口（P2-2） | 5 | `5174ea59` | 2026-11-04 healthy，日志无异常栈 | 社交目录 + 契约 + gateway 984 passed（Claude 复跑）；ruff 干净；basedpyright telegram/runtime.py 0 errors；`_observe_social` 只剩定义与 group=-1 入口两处；tests/channels 有 4 failed + 1 error，改动前后名单完全一致（既有问题，与本改动无关） | 删除 `_on_start`/`_on_help`/`_process_forward_command`/`_on_message` 编辑分支/`_process_message_update` 中的 5 处拦截，生产代码净减 10 行。测试只把 4 处直接调用普通 worker 改为调用真实入口 `_on_social_update`，断言不变 |
| B1-a 发送前收取新到消息，连发合并回复【小幅行为变更】 | 追加 | `eb913c04` | 2026-11-04 healthy，日志无异常；同时生效配置 coalesceSeconds 1.5→10、dynamicWait quiet 1.5→10 / unfinished 4→10 / max 12→30 | 社交目录 957 passed（Claude 复跑）；场景 22 passed（新增连发场景）；契约 + gateway 83 passed；ruff 干净；basedpyright 0 errors；新配置经 `TelegramSocialConfig` 校验通过 | 生产代码 +28 行。Codex 确认：judge 稍后对已曝光的消息返回 wake 时，`Judge._handoff` 复用原 wake，不另起 turn（有测试）。待 owner 体验 |
| 窗口上限放宽（Claude 小修） | 追加 | `90abae4a` | 2026-11-04 healthy，日志无异常；配置 coalesce 20 / maxCoalesce 20 / quiet 20 / unfinished 20 / max 45 | 社交目录 + config + 契约 1087 passed；ruff 干净；新配置经 `TelegramSocialConfig` 校验通过 | 只改两个 `le=` 上限 |
| P6-a waiting/media 拆出，judge/stickers 去反向引用（纯重构） | 6 | `8d50aa77` | 2026-11-04 healthy，日志无异常 | 社交目录 + 契约 + gateway 1040 passed（Claude 复跑）；场景测试未改；ruff 全仓干净；basedpyright social/ 0 errors；judge_runtime/stickers/jargon 中 `self.runtime`、`reportPrivateUsage` 0 处 | runtime.py 960→727；生产代码净增约 59 行（构造与签名）。**Claude 修正简报设计缺陷**：`save_preview` 放在 SocialMedia 会跨类访问 cache 私有方法（Codex 如实上报未绕过），改为 `SocialMediaCache.save_preview(scope, raw, limit)`。删除死方法 `JudgeCoordinator.enqueue` |
| P6-b delivery/hook 拆出（纯重构） | 6 | `8bd39b18` | 2026-11-04 healthy，日志无异常 | 社交目录 + 契约 + gateway 1040 passed（Claude 复跑）；场景测试未改；ruff 干净；basedpyright social/ 0 errors；Codex 用 AST 比对 14 个发送方法与 8 个 hook 方法在替换后与原代码一致 | runtime.py 727→341；生产代码净增 67 行（导入与构造）。`scripts/evaluate_social_v5.py` 冻结 hook 编译依赖已移出的名字，评估脚本迁移时处理 |
| P6-c learner/selector 去反向引用，工具一个类一个（纯重构） | 6 | `3fa0f3e7` | 2026-11-04 healthy，日志无异常 | 社交目录 + 契约 + gateway 1041 passed（Claude 复跑，新增工具集合一致性测试）；场景测试未改；12 个工具 name/description/schema 改动前后导出逐字节相同；ruff 干净；basedpyright 0 errors；social/ 中 `self.runtime` 0 处 | 两个 learner 的单批次互斥改为 `peer` 互查，取代 `runtime.learning_task`；runtime 新增 `activity_action`/`expression_action`，delivery 新增 `followup_status`，工具层不再访问 store。工具文件 201→301 行（12 个类声明），其余生产代码净增 14 行 |
| P6-d `wiring.py` 成为 gateway 唯一 social 入口【小幅装配变更】 | 6 | `7b6dff01` | 2026-11-04 healthy，Telegram channel 正常启动（social 启用时 channel 缺 runtime 会直接报错，未报），日志无异常 | tests/cli + 契约 + gateway 594 passed（Claude 在沙箱外复跑；此前 test_commands 9 failed）；社交目录 958 passed；ruff 干净；basedpyright 0 errors；gateway 中 social 内部名字 0 处 | wiring.py 69 行，gateway 净减 30 行。Codex 沙箱内另有 5 个 cli 失败（临时 git 初始化、Unix socket 权限），沙箱外全部通过。保持原状的装配疑点：文件 watcher 不刷新 social、channel 构造时用自身 social config 覆盖、热刷新 judge 配置两次 |
| P7-a judge 准入分层【行为变更】 | 7 | `f6d0dd50` | 2026-11-04 healthy，日志无异常；待 owner 群内体验 | 社交目录 + 契约 + gateway + cli 1561 passed（Claude 复跑）；场景 22 passed；ruff 干净；basedpyright 0 errors | 生产代码 +45 行（`local_admission`、`decide_locally`、judge_facts.is_bot）。23 处语义性断言按 §5.1 新预期修改（报告逐条列出）；judge 故障场景改为先建立选择性等待。旧消息无 is_bot 键按真人处理 |
| 多段回复只在第一段引用（Claude 小修）【小幅行为变更】 | 追加 | `ed126e4d` | 2026-11-04 healthy | owner 反馈“连续回复 3 次、每次都回复最后一条”：核实为 18:35 五条消息正确合并为 1 个 judge 批次（本地准入）、1 个 turn、1 次 3 段发送，但每段都带 reply_to，看起来像三次回复（线上历史 97 组多段中 71 组每段都引用，原有行为，非 P7-a 引入）。`enqueue_parts` 只给第一段 reply_to；新增测试在旧代码上失败、新代码通过；社交目录 967 passed | 管线正常：18:35:04–21 五条 → 18:35:42 wake（20 秒窗口）→ 18:36:32 发送，judge 0 次 LLM 调用 |
| B2-a 多段发送逐段决定引用【小幅行为变更】 | 追加 | `66457345` | 2026-11-04 healthy | owner 认为“只有第一段引用”仍僵硬，应由主模型逐段决定。parts 项可为字符串或 `{text, reply_to}`，顶层 reply_to 仅作第一段缺省；每个目标须已观察；幂等按 (content, reply_to) 逐段比较；协议加一句引用指导。社交目录 + 契约 + gateway 1058 passed；场景 22 passed；ruff/basedpyright 干净 | 生产代码净增 13 行。待 owner 体验模型是否会自然地逐段引用 |
| P7-b judge 内部重写（schema 22）【小幅行为变更】 | 7 | `1f50af8b` | 2026-11-04 healthy；线上库已迁移到 schema 22，`telegram-social/backups/judge-history-schema21.json` 已生成，3 个 scope 水位不变，日志无异常 | 社交目录 + 契约 + gateway + config + cli 1619 passed（Claude 复跑）；场景测试断言未改；ruff 干净；basedpyright 0 errors；Claude 在线上库新副本上独立复演迁移：导出 59/41 行，其余表行数与水位不变，quick_check ok | judge 相关代码 1043→392 行，生产代码净减 627 行。judge 配置只剩 7 个字段（timeout 8、context 12）。judge 用量进 llm_usage（source=system）。旧表名只留在 schema 历史升级与迁移测试中（Claude 接受此例外）。`_run` 异常时窗口已取走、水位未推进，需重启 recover 才重新处理（记录，未处理） |
| P7-c LearningWorker；供应商错误退还重试次数【小幅行为变更】 | 7 | `4d803e1f` | 2026-11-04 healthy，日志无异常 | 社交目录 + 契约 + gateway + cli 1527 passed（Claude 复跑）；场景 22 passed；ruff 干净；basedpyright 0 errors | 两份生命周期合并为 `learning_worker.py`（106 行），生产代码净增 41 行（导入、签名、jargon 独立 provider 配置）。**Claude 补修**：退款后 attempts 回到 0 时 `last_batch_due` 忽略该批次，欠费期间会每个周期重试；改为 `attempts>0 OR model_calls>0`，新增断言在旧查询上失败。**线上遗留**：表达批次 `54c6bbb2…`（19:37–19:43 欠费期间耗尽重试，failed）会永久阻止 topic 53 的表达学习；重新排队 SQL 见 P7-c 报告第 4 节。另有同一时段的 topic 53 批次 `3f11241a…`。**owner 2026-11-04 同意重新排队**：Claude 先备份 `telegram-social/backups/social-before-learning-requeue-20261104.sqlite3`，再把这两行改为 ready、attempts=0（游标、来源、失败计数、错误详情不变） |
| P7-d selection 容错、一 turn 一次、SQL 回归拥有者【小幅行为变更】 | 7 | `b620709c` | 2026-11-04 healthy，日志无异常 | 社交目录 + 契约 + gateway + cli 1542 passed（Claude 复跑）；场景 22 passed；ruff 干净；basedpyright 0 errors；selection.py 中 `db.execute` 0 处；两次线上 ValidationError 的原始输出回放后都保留贴纸选择 | 生产代码净增 30 行（新增 Library 方法 15 个）。provider 重配不再清缓存（缓存按 turn 结束清除）。首次派发因 Codex CLI 凭证失效未执行，owner 重新登录后重派 |
| P8-a protocol 外置与去重、角色口头禅改为配置【行为变更】 | 8 | `64b201a1` | 2026-11-04 healthy；配置加 `learning.catchphrases` 后重启仍 healthy | 社交目录 949 passed、契约 + gateway + config 144 passed（Claude 复跑）；场景测试未改；ruff 干净；basedpyright 0 errors；模板与简报正文 cmp 一致；生产代码中“本大爷/白饭/算力/肥鱼/嘴硬摸鱼” 0 处 | 生产 Python 净减 174 行。Codex 按简报停在一处未列出的旧文案断言（test_social_runtime.py:333），Claude 改为新句子。部署顺序：先发布、再改配置并重启（learning 配置 extra=forbid，旧代码不认新字段） |
