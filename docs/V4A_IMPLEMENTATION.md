# V4-A：冷场自主唤醒

2026-10-30，实施范围为 [V4 计划](MIGRATION_V4_PLAN.md) 的 A 批次。
源码已实现；检查、提交及实际发布结果在本轮发布后补充。

周期检查现在独立于新消息、已读水位和记忆修订号。命中后将 `kind=idle` 的定时事件直接放入
既有 wakes 队列、沿既有会话锁进入主 Agent，不经过 V3 中间模型。它使用 `seq=0` 和 scope 内
递增机会序号，不伪造成员消息，也不推进已读。新成员消息仍由 V3 判断。

## 配置契约

现有 `scope.proactive` JSON 编辑入口与 manifest 继续从声明 schema 生成配置，无新增设置页面。

| 字段 | 默认值 / 语义 |
| --- | --- |
| enabled | false；保留总开关，不替新用户启用 social |
| minIntervalSeconds / maxIntervalSeconds | 1800 / 5400；每次均匀抽取下次期限，允许配置相同值 |
| idleSeconds | 900；最近有效新成员消息后的最短空闲时间 |
| probability | 0.3；0 和 1 原值参与概率计算 |
| timezone | UTC；明确 IANA 时区，本人预设沿用原 quietHours 的 Asia/Tokyo |
| recentSendSeconds / recentSendMultiplier | 1800 / 0.5 |
| nightMultiplier | 0.3；当地 23:00–08:00 |
| hourlySendThreshold / frequentSendMultiplier | 5 / 0.3；近一小时确认公开消息条数 |
| pendingThoughtMultiplier | 1.4；契约预留，随 V4-B 的有效待说想法及尝试状态接入 |
| ttlSeconds | 120；保留本次机会有效期 |
| contextMaxAgeSeconds / requireContext | 1800 / true；只约束旧来源候选，不限制冷场事件 |

旧 `intervalSeconds` 接受读取、验证并发出废弃提示，随后丢弃；不映射为固定周期，不覆盖新范围，
序列化时移除。缺少新区间时采用 1800–5400；新区间存在时按新值执行。JSON 往返测试覆盖该行为。

本批修正顺序为基础概率 → 近期发言 → 夜间 → 频繁发言；待说想法乘数将在 V4-B 放到最前。
旧 pending 记忆尚无“说完/放下/已尝试次数”，本批不把它冒充可主动尝试的 pendingThought。
普通 message 和 social_send 的确认消息均计入；分条按消息计数，同时展示发送组数。
queued/failed/unknown 本身不计成功。统计历史不足会标注覆盖起点与 history_complete=false。

## 状态、等待与提示

SQLite schema **11→12** 事务迁移，增加成员来信时间、机会序号、抽样/概率/命中结果、统计覆盖和停止联系状态。
成员来信时间从原 dialogue_state.last_at 回填；之后编辑、重复、Bot 消息不重置该时间。
下次期限和本次检查结果持久化。重启不重掷已完成的检查；错过窗口只记 missed，不补发。
已认领冷场回合在重启后消费结束；只有未认领且仍有效的机会能恢复，沿用已有未知发送恢复处理。

普通 observing/sleeping 可产生机会；有效显式等待、暂停、quietHours、忙碌、预算不足均本地跳过。
待认领机会遇到新消息优先让消息回合处理，沿用请求预留退款与队列合并，避免多跑一轮。
`social_set_wake(proactive_paused=true)` 保存成员要求停止主动联系的意图，独立于临时等待 TTL；
清除等待、切换潜水或重启均不恢复，明确要求后通过 false 恢复。它不关闭正常成员消息回复。

当前可信协议鼓励具体好奇、自然追问和分享；冷场专门提示可查看记忆、接续事情或有限搜索，
如实说明来源、没有自然内容时沉默。角色编辑、重启与压缩后仍通过现有可信协议注入生效。
工具、MCP、技能及普通跨会话权限沿用共用策略。未新增服务、数据库或哈希/指纹比较。

`social_get_state.proactive` 展示下次/上次检查、空闲与参与事实、概率与抽样、跳过原因、机会 ID、
主回合状态及 social/普通发送回执。completed 与确认已发送分开呈现。

## 验证与未迁入范围

使用可控时钟、随机源、临时 SQLite 和模型/Telegram 替身，覆盖无新内容到主回合、概率零调用、
30/90 分钟及 15 分钟边界、概率修正、普通潜水/显式等待、消息竞争、停止/恢复、重启与 11→12 回滚。
当前定向测试 109 项通过（主动机会和媒体迁移），Ruff 通过，修改模块严格类型 0 errors / 0 warnings。
实际库临时副本 11→12 升级通过，114 条消息、4 条记忆、5 条证据及原水位按源值保留。
离线链路探针确认空消息/空 judgement → 主回合认领 → 替身确认发送，已读保持 0，停止意图生效。
安装产物与实际发布数据保留核对、完整管线结果及发布信息另在完成记录中补充。

V4-B 的 pendingQuestion、pendingThought/motivation、事项关联与两次上限、答后接话策略尚未实现；
V4-C 的实际模型合成语义验收和体验收敛尚未进行。本批的替身发送仅证明主回合/工具链路，
不作为模型自然主动交流的语义验收，不向真实群发送探测消息。

旧版本不支持 schema 12 和新配置字段。回滚须使用原发布管线恢复匹配镜像及整实例数据/配置备份。
