# V2-B：提问触发、概率唤醒、潜水中断与等待期限

2026-10-29，覆盖 G05–G08。实现和离线验收已完成，实际发布结果在本批管线完成后回填。
继续使用既有 Telegram 接收者、SocialStore、SocialScheduler 与 Agent 会话锁，不增加分类模型或第二套任务队列。

## 触发语义

- `wakeOnQuestion` 增加独立 `question` 候选：当前文本必须有提问/挑战特征，并有当前 Bot username、管理员昵称的明确称呼，或确实回复本 Bot 的依据。
  username 由 Telegram 身份缓存传入，不从群友消息自称获取。引用旧消息中的名字不当成当前点名。
  单独问号、第三方提问、较长名字中的子串、纯引用/代码及对别人的引用不单独触发 question。
  启用 question 后，疑问文本中被排除的歧义昵称也不经旧昵称回退重新触发；明确喊昵称和平台确认的 @/回复仍可达。
  这是保守本地规则，不能声称理解了所有自然语言指向；不确定时仍可由其他已配置条件提供机会。
- `wakeProbability` 取值 0–1，默认 0。只对获准、有效的新普通成员消息，在没有更明确触发时抽样。
  重复更新、编辑、自身、其他 Bot、过期/未来消息、暂停、未授权成员和不允许概率中断的潜水不抽样。
  抽样值、概率、结果、scope/seq/配置版本与消息入库同事务提交；失败不留下半条消息或孤立候选。
  0 不请求随机源；可注入随机源用于验收。重启和额度延期不重新抽样，记录随消息保留窗口清理。
- `source_seq` 保留真正触发来源，和为合并快照推进的 `trigger_seq` 分开。
  概率候选在执行前核验有效抽样和来源仍存在；提问候选也核验来源。
  点名、问题和其他明确条件可升级随机原因。随机/到期候选尚未进入模型时，明确点名可替换它并退还未使用额度；旧总线项目无法再次认领。
  已有更高优先级未认领机会时，新的低优先级随机机会让位，不额外调用一轮。
- 临时条件保持有界 TTL 和默认 one-shot，新增 `on_question`、`probability`、`sleep_interrupts` 与 `coalesce_seconds`。
  候选绑定建立时的条件；替换/清除条件会取消旧候选。一次性条件在真正准入时消费，包括当前运行中通过读取工具实际展示给模型的新候选。
  旧回合不会消费后来设置的另一组条件。群友与普通助手的普通工具权限不变。

## 潜水与合并

管理员可按 scope 配置 `sleepInterrupts` 的 keywords、senders、question、probability、messages。
全部代码默认 false，保留旧 sleeping 忽略普通条件的行为。模型只能在这个上限内收窄，不可扩大管理员策略。
模式与条件分开：同一关键词可在 observing/waiting 生效，也可在管理员允许时中断 sleeping。
显式选择性条件不会被基础概率、普通 waiting 关注或 V2-A 答后关注绕过。

scope 可配置 `coalesceSeconds`，临时条件可短期覆盖。`maxCoalesceSeconds` 代码默认 10 秒，
同时保留 V2-A 动态静默及其总上限；非法组合在 schema 或工具入口明确拒绝。
这些参数影响消息合并，不改变 quietHours 只约束主动机会的含义，不修改唤醒/发送额度。

## 等待工具

`social_wait` 必须恰选一种期限：

```text
social_wait(seconds=60)
social_wait(until="2026-10-29T23:00:00+08:00", sleeping=true)
social_wait(infinite=true, sleeping=true)
social_wait(infinite=true, purpose="messages", quiet_seconds=3)
```

绝对时间例子只演示格式，实际调用仍须在未来且不超过管理员 maxWaitSeconds。
拒绝缺失/冲突期限、无时区日期、已过期时间、超长等待，以及 infinite+on_timeout、infinite+purpose=reply。
原 seconds/sleeping/on_timeout 调用及 V2-A 的有限 reply/messages 语义继续可用。

无限等待在数据库保存无截止时间，不用一个巨大秒数伪装，也不持有会话锁。
无限 messages 等到第一条相关新消息后才开始静默窗口，并固定本批总上限，连续刷屏不能无限延期。
临时关注 TTL 不因无限等待而延长；到期/清除后回落管理员基础触发，@/回复与明确昵称入口保留。
暂停、撤权或绑定/配置版本变化取消旧等待与候选。

潜水继续经过 V2-A 睡前观察：无限潜水可以本地观察后进入无截止睡眠；绝对期限不会被观察窗口向后挪动。
无新消息时观察与到期收尾均可本地完成，只有显式请求且仍有效的一次性 timeout 才会产生模型机会。

## 配置与升级

代码新增配置默认兼容。本人实际实例拟在本批启用下列 scope 增量，其他 V2-A、角色、范围、额度、模型、主动和 quiet 配置保留：

```json
{
  "wakeOnQuestion": true,
  "wakeProbability": 0.05,
  "sleepInterrupts": {
    "keywords": true,
    "senders": true,
    "question": true,
    "probability": false,
    "messages": false
  }
}
```

这是当前实例的低概率预设，不是所有新群的代码默认值。无需新 WebUI，沿用现有 social JSON 配置与 manifest 契约。
`social_get_state` 提供基础触发/潜水策略、当前临时条件和 `wait_infinite`。

schema **7→8** 事务升级：新增 `wake_samples`；scope 新增无限等待标志；wakes 新增真实来源和关联条件；
扩展 dialogue_waits 支持空总期限及绝对潜水期限。迁移事务保留旧等待字段、消息、水位、记忆、视图和发送回执；
新协议/配置版本仍按既有规则使旧调度与临时条件失效，不追溯重写历史已读。
旧代码不接受 schema 8 和新增配置字段；回滚使用原管线恢复对应镜像与整实例备份。

## 验收与发布记录

`test_social_wake_v2.py` 已加入原管线。验收使用可控时间、可注入随机源、临时 SQLite 和 Telegram/模型替身。
覆盖提问误触发、0/1 与概率边界、去重/编辑、授权和潜水排除、重启/延期、抽样事务失败、来源与条件复验、
优先级竞争、one-shot 忙时消费、有限/无限/绝对等待、观察衔接、TTL 回落、话题隔离和 schema 7 升级。
不向真实群发送验收消息，不以离线通过宣称自然程度已得到证明。

发布版本、完整管线结果、实际参数、备份及健康核验待本批发布后回填。下一批为 V2-C；M5 继续暂缓。
