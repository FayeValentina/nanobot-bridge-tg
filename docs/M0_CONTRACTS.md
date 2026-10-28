# M0：Telegram 群仿真契约验证

日期：2026-10-28。基于 `563ae41a` 的本地源码执行离线实验。

本文件保留 M0 当时的特征实验结论；后续实现及当前行为见 [M1 实现说明](M1_IMPLEMENTATION.md)。

M0 交付是枚举修复、空工具白名单修复、可复现的接缝实验和后续实现决策。群观察、人格隔离、
静默和仿真发送尚未实现，不能据此开启真实群仿真。M1–M5 的功能范围不变。

## 已修复：Telegram 设置枚举

`TelegramConfig.group_policy` 支持 `mention` / `open`，但 manifest 曾额外提供
`allowlist`。现在复用 `DIRECT_GROUP_POLICIES`，保持已有配置与群消息行为。
不添加一个只有名字、没有执行语义的 `allowlist`。

`nanobot/channels/telegram/tests/test_config_contract.py` 检查运行时 Literal、
manifest、公开字段 choices、默认值、camelCase 输入及配置输出一致，并拒绝非法值。
前端已有组件从后端读取 choices，无需另增前端枚举；原有翻译标签不授予可选值。

## 已修复：空工具白名单回退

实验发现 `tools or self.tools` 会把合法的空 registry 当作没有提供 registry，恢复全部
默认工具。最小修复在 AgentLoop 的回合恢复、运行时资料收集和 runner 调用三处改为
显式判断 `is not None`。不改工具加载架构，不改变省略参数时的默认行为。

新增回归覆盖直接 runner、普通回合和 disabled_tools 排除所有工具三条路径，验证
模型不能看到或执行默认工具，默认工具提供的上下文也不进入请求。测试先在修复前复现失败，
执行工具使用禁止执行的替身，不执行真实命令。

## 实验结果

主体实验位于 `tests/agent/test_telegram_social_contracts.py`，实际 Telegram
发送边界位于 `nanobot/channels/telegram/tests/test_delivery_contract.py`。
两者是现有行为的特征实验，其中部分断言有意记录当前缺口，**不代表群仿真验收通过**。
M1/M2 实现相关接缝后，应将相应缺口实验替换或补充为新策略的正向验收。

| 项目 | 离线实验结果 | 实施决定 |
| --- | --- | --- |
| `before_run` 人格注入 | hook 拿到 deepcopy，修改系统提示不会进入请求 | 不使用该回调装配人格 |
| `before_iteration` 人格注入 | 无压缩状态的直接 runner 调用可见；普通持久化回合的系统前缀在 hook 前已缓存，用户/后台回合均看不到修改 | 为 ContextBuilder 增加可信的可选人格输入，在首次构建、重建时统一应用 |
| 压缩重建 | 重新调用 transcript builder，只改原消息中的人格会丢失；运行时资料块也不属于重建后的系统前缀 | 人格通过 builder 参数保留；群资料以有界资料快照单独注入 |
| 工具请求作用域 | 两个群并发执行同一工具时，ContextVar 中的群、话题、sender 和 session 正确，结束后恢复；factory 可见同一会话路由 | 复用 RequestContext，新增工具不得缓存可变目标 |
| 工具权限 | 非空受限 ToolRegistry 拒绝未注册的 `exec`；空 registry 曾回退到默认全集，已修复并覆盖上下文收集路径 | 复用现有 `tools=` 接口；M1 增加默认拒绝的工具白名单策略 |
| 通用 `message` | Telegram 目标允许显式改成其他群，工具本身没有群仿真授权约束 | 仿真不开放该工具，使用只能操作当前作用域的 social 工具 |
| 空最终回复 | `finalize_content` 返回空字符串会产生 3 次模型请求，随后用户/后台回合都返回空回复兜底 | 增加明确的静默结束语义，绕过空回复恢复和公开兜底 |
| 发言去重 | 用户回合且无新消息注入时可抑制最终回复；有注入或后台回合仍会返回最终文字 | 工具显式发送模式必须贯穿所有触发及续轮路径 |
| 内部通知 | 普通用户回合可把工具提示放入公开队列；dispatch 异常会发送错误兜底 | 统一出站策略要覆盖事件、异常和最终文本 |
| 队列与发送 | `message` 的成功结果及去重目标在入队时产生；无消费端也报告 sent；入队失败不记录成功 | 新工具返回 queued，发送成功只由平台确认推进 |
| Telegram 回执 | SDK 返回消息 ID，但普通文本 `send()` 不向调用者返回回执，也不写入出站消息 | 为仿真出口增加有类型的发送结果和 action_id 关联 |
| 超时不确定性 | 模拟平台已接受但响应丢失，普通 `_send_text` 的超时重试会再次发送同样文本 | 仿真发送遇不确定超时标 unknown，不复用该重试语义 |
| 人格读取隔离 | 切到群项目目录并关闭 include_memory 仍会加载全局 SOUL/USER；群目录 SOUL 不生效 | 文件读取策略与会话持久化分离 |
| 归档隔离 | 群会话压缩摘要写进共享 history.jsonl，且进入全局 Dream 未处理输入 | 保留会话压缩，独立控制是否写全局记忆日志 |
| 统一会话模式 | 显式 session_key_override 可以保留群/话题会话，不进入 unified:default | 所有 social 入站必须提供授权作用域派生的显式键 |

## M1/M2 的最小接缝

以下为实施契约，字段名称在对应小提交中确定。本阶段不预建 social 空目录，
也不为尚未存在的运行时添加占位全局变量。

### 可信人格和记忆策略（M1 前置）

- 为 `ContextBuilder` 的 system/transcript 构建添加有类型的可选输入，承载操作者
  管理的人格和群背景。保留固定工具、权限规则，群消息和模型笔记不能进入可信人格。
- 群回合不读取实例个人 SOUL、USER、长期记忆；全局 AGENTS 和自动技能也要审核，
  不让私人资料通过其他 bootstrap 或技能路径重新进入提示词。
- `SessionPolicy` 或等价的通用会话策略分别控制全局资料读取、全局记忆读取、
  全局记忆归档和允许工具。`persist=True` 仍保留 JSONL、恢复检查点与会话内摘要。
- 运行中压缩、provider compaction、空闲/手动压缩和原始归档降级都必须采用同一
  归档策略。群摘要可以进入本群会话，不能写入个人 Dream 的共享日志。
- 当前 SessionPolicy 是内存规则，不落盘。M1 必须在会话恢复和空闲压缩前根据可信
  social 绑定重新解析策略；绑定缺失或服务不可用时拒绝群执行/全局归档，不能退回
  默认个人策略。不能仅在入站 hook 临时设置策略。
- 验收包括两群不同人格、压缩后保持人格、重启后恢复隔离、私聊不受影响，以及
  正常/失败/原始归档路径都不污染全局记忆。

### 工具与运行时组装（M1 前置）

- 保留一次 gateway 组装、一个通道生命周期所有者。通道、social 工具和 hook
  使用同一个有类型服务句柄；必要时在 ToolContext / gateway 构造器增加可选依赖，
  不用进程级可变单例定位 TelegramChannel。
- 角色和权限从可信绑定解析，RequestContext 提供真实 channel/chat/session/thread。
  工具参数中的群号或“管理员”标记不构成授权。
- 在现有 `tools=` 入口构造明确白名单；未注册工具不仅不展示，而且无法执行。
  新增工具不自动加入群权限。runtime context providers 也只从受限 registry 收集。
- 自动唤醒要组装与普通唤醒相同的请求作用域。执行期间注入的消息必须属于同一作用域；
  M0 已证明 ContextVar 隔离可复用，尚未实现 social 的授权和输入校验。

### 静默与出站（M1 建门禁，M2 完成行为）

- 通用回合配置区分正常回复和工具显式发送。静默是有类型的结束结果，不能用空字符串
  猜测；runner 在该模式下不启动空回复重试/强制总结，仍记录失败与用量。
- 同一出站策略覆盖用户、后台、续轮、回合内注入、最大迭代、模型错误、dispatch
  异常及停止/压缩通知；关闭公开思考流、工具提示和其他内部通知。
- 最终门禁归 social 出站模块所有，检查作用域、授权、暂停、角色版本、预算、时效。
  文本、引用、媒体和贴纸都受该门禁约束；不能只在模型提示词里限制。
- 仿真 action 持久化区分 queued / sending / sent / failed / unknown，平台回执携带
  message_id 和 thread_id。入队结果和模型回合完成事件都不能当作 sent。
- 仿真路径遇响应不明的网络超时，不调用当前会自动重试 TimedOut 的文本发送器；
  单独选择不盲重试的发送行为。429 等明确拒绝可按 Retry-After 有界重排并重新过门禁。
  部分分条成功后失败需记录每个子动作，不能重发整个分条集合。
- 角色切换、暂停和撤权使旧任务失效；真正调用 API 前再次检查。不能承诺撤回已在
  平台处理中的请求，也不承诺平台不提供的端到端恰好一次。

### 唤醒队列：选择 social SQLite 为唯一权威

核对了 `LocalTriggerStore` / `run_local_trigger_queue`：它们拥有文件队列、认领、
恢复与重试，成功意味着 session turn 完成，并不代表 Telegram 发送成功。它们没有
social 所需的消息水位、角色版本、时效和预算事务。若同时维护 SQLite WakeJob，
将产生两套认领/恢复状态。

因此 social 不再往 LocalTriggerStore 持久化第二份唤醒任务。SQLite 同一事务内维护
水位、合并原因、版本和 WakeJob；异步调度器原子认领后通过现有 MessageBus /
会话 dispatch 执行入口投递。进程内入站队列仅负责运输，不是另一份持久任务队列。

现有 `_dispatch` 会话锁、pending queue 和并发门控继续复用。M1/M2 需要一个有类型的
完成/失败关联，且不能因 bus 入队就完成 WakeJob。启动恢复、过期、忙时合并和租约
失效由 social 所有；不得用 sleep 占住 Agent 回合，也不允许两个 worker 同时认领。
普通 cron 和 LocalTrigger 功能保持各自现有行为。

## 验证方式和范围

```bash
.venv/bin/python -m pytest -q tests/agent/test_telegram_social_contracts.py nanobot/channels/telegram/tests/test_config_contract.py nanobot/channels/telegram/tests/test_delivery_contract.py
./scripts/pipeline.sh test
```

M0 实验纳入本机管线。所有模型响应、Telegram API 和发送均使用替身，会话与数据
写入 pytest 临时目录；没有使用生产凭据、运行真实模型、轮询 Bot 或向真实群发消息。
同时运行现有 ContextBuilder、ContextGovernor、Consolidator、工具上下文、turn hooks、
TurnDelivery 和 channel setup 回归。

最终记录：

| 检查 | 结果 |
| --- | --- |
| 新增 M0 实验和回归 | 31 项通过，纳入本机管线 |
| 最终 `pipeline.sh test` 后端 | 229 项通过 |
| 最终完整 WebUI 测试 | 96 个文件、1553 项通过 |
| WebUI lint / 生产 build | 通过 |
| 修改的 Python 文件 Ruff | 通过 |
| AgentLoop / Telegram manifest BasedPyright | 0 errors / 0 warnings |
| 补丁空白检查 | `git diff --check` 通过 |

额外扩展的 175 项回归中，174 项通过；唯一未完成项是跨全部通道加载 runtime 的
`test_every_runtime_channel_field_has_a_webui_contract`，因当前 Telegram 开发环境缺少
Matrix 的 `nh3` 依赖而中断，没有为本次 M0 安装无关通道。其余 channel setup 检查通过。
第一次完整前端测试曾出现 xAI OAuth 文案的异步时序失败，单文件复测 8 项通过，
最终完整管线全部通过；没有修改该无关界面或放宽测试。上述结果不代表全量后端通过。

本次修改尚未提交或部署，生产实例与凭据未改动。

M0 的通过意味着已修复配置缺陷并收敛必要核心改动。M1 开启真实测试群之前，必须将
人格/记忆隔离和出站门禁实现并验收，不能把这里的缺口特征实验当作安全性保证。
