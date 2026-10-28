# M4b：主动机会与安静时段

2026-10-28 实现 M4 第二批。继续使用 SocialStore 的 SQLite、SocialScheduler 和已有 Agent 会话锁，
不增加常驻进程、模型轮询或 LocalTrigger 投递副本。权限沿用 [共用工具约定](SHARED_TOOL_PERMISSIONS.md)。

## 配置与实际行为

每个已配置 scope 新增 proactive、quietHours，默认都关闭。升级不会改写既有配置，
也不会自动给本人当前群开启主动发言。现有 @/回复、关键词、发言者、等待/潜水机制继续工作。
可在现有 Telegram 设置页的 social JSON 字段中，把下面两项加入目标 scope，保留其 chatId/threadId/roleId：

```json
{
  "proactive": {
    "enabled": true,
    "intervalSeconds": 1800,
    "ttlSeconds": 120,
    "contextMaxAgeSeconds": 1800,
    "requireContext": true
  },
  "quietHours": {
    "enabled": true,
    "timezone": "Asia/Tokyo",
    "start": "23:00",
    "end": "08:00"
  }
}
```

| 配置 | 默认值 / 范围 |
| --- | --- |
| proactive.enabled | false，显式按群/话题开启 |
| intervalSeconds | 1800；60–86400 秒 |
| ttlSeconds | 120；30–600 秒，候选与错过检查的有效窗口 |
| contextMaxAgeSeconds | 1800；60–86400 秒，未读消息的时效 |
| requireContext | true；false 允许无上下文的显式周期机会 |
| quietHours.enabled | false |
| timezone | UTC；接受有效 IANA 时区，示例显式选择 Asia/Tokyo |
| start / end | 23:00 / 08:00，严格 HH:MM、不可相同，允许跨午夜 |

安静时段按该 scope 的配置时区计算，不依赖宿主时区；重复的夏令时小时也按实际当地时间判断。
它只跳过主动机会，明确 @/回复及原有消息触发仍走原路径。不是全局免打扰或工具权限开关。
时段结束后按后续检查重新评估有效内容，不补发整个夜间的机会。

配置由 schema 声明，Telegram manifest 的 JSON 默认值取同一模型；沿用后端 JSON 校验和现有编辑器，
不另建一套前端字段模型。专用角色/记忆/行为管理面板仍属 M5。

## 候选与单一队列

每次到期检查先在本地检查状态、安静时段、已有任务及已知请求/发送预算，再寻找：

1. 仍有效、尚未尝试该修订版本的 pending 或 topic 记忆，优先待说内容；
2. 尚未考虑过、未确认已读且在时效内的普通成员消息；
3. requireContext=false 时，明确配置的无上下文机会。

偏好和经历不会单独成为无限重复的话题。每个记忆 ID/修订版至多产生一次候选，
一次候选同时记录已考虑的消息水位；模型沉默后，不为同一来源反复唤醒。
纠正后的新版本、后续新消息可以产生新机会。没有候选且未配置无上下文机会时，模型调用为零。

候选只在既有 wakes 队列保存类型、消息序号、记忆 ID/修订号与截止时间，不复制第二份记忆正文。
模型可用 social_memory(action="list", memory_id=...) 查当前条目。候选和资料始终是数据，
不是要求发言的指令；模型仍可选择沉默，公开回复使用显式发送。

已存在消息唤醒或运行中的回合时，不另发主动回合。新点名替换尚未认领的主动投递，旧的总线项在准入时失效；
未调用模型的预占请求额度会退回，避免额度较小时吞掉点名。运行中实际展示的新点名可并入当前回合。
已有合并、冷却和限频延期逻辑继续生效。

## 准入、发送与恢复

- 在调度、模型准入和 social 真实发送/重试前重新校验主动来源。记忆删除、清空、修订或过期后，
  旧机会取消；暂停/关闭/撤销 scope 和角色绑定/调度配置变化使旧任务失效。
- 角色文件正文修改仍在下一回合加载，不取消当前机会或修改确认。
- 等待和潜水不会被主动机会中断。已认领回合确认已读不会阻止其回应，
  已排队的分条消息也可在随后等待时继续投递；安静时段、暂停和来源失效仍会取消其主动发送。
- 安静时段开始后，尚未发送的主动 social 文本、贴纸、分条和重试会失效。
  普通 message、cron、跨会话和后台工具保持普通行为，不加全工具出站门禁。
- 机会检查期限与已考虑来源持久化。重启只恢复仍在有效期的一份候选；错过窗口时跳过并设下一次检查，
  不回放停机期间的全部机会。未认领主动请求额度在恢复时退回，包括已经过期的排队项。
- 请求预算约束 social 调度，发送节奏/预算/回执约束 social_send/sticker，不宣称是全工具的总硬额度。

social_get_state 新增主动配置、next_check/last_check/decision，以及安静时段配置和 active 状态。
decision 包括 waiting、disabled、no_context、quiet_hours、deferred、busy、send_budget、request_budget、
scheduled、missed、memory_changed 等，可解释为何没有调用模型。

## 数据与验收

schema 4→5 增加 proactive_state 表和 wakes.proactive_source；原观察、水位、群记忆与发送记录保留。
沿用整实例备份和失败回滚。旧程序不接受 schema 5，降级需要同时恢复对应的数据备份。

新增 51 项可控时钟验收，覆盖无上下文零调用、来源去重/修订、跨午夜与 DST、quiet/direct 竞争、
额度退回、暂停与配置变更、已读后回应、等待后的分条投递、来源删除/过期、重启恢复和 schema 升级。
现有群友/记忆/共用权限验收继续保留，模型与 Telegram 均使用替身，不向真实群发送测试消息。
完整既有 `scripts/pipeline.sh test` 已通过：后端 818 项、provider 116 项、前端 96 文件共 1553 项，
Ruff、WebUI lint/build 通过；修改模块严格类型 0 errors / 0 warnings。
发布版本和实际实例核对在发布完成后补记。
