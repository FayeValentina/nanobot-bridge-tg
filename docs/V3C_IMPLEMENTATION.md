# V3-C：体验验收与收敛

2026-10-30。实现和离线语境验收已完成；本机 release 结果在发布后补录。
本批不增加 schema，继续使用 10；中间提示协议升级为 `social-judge-v3`。

## 修复与边界

V3C-01 已修复：常驻 `wakeOnQuestion`、`wakeOnMessages`、关键词/成员兴趣改为单独的
`participation` 事实，普通 observing/主模型忙碌时不再生成虚假的“选择性等待问题”。
`intent` 只来自有效的临时 `social_set_wake`、显式等待或潜水。
睡前 `purpose=observe` 明确表示后台观察，不描述成只等问题。
临时条件过期后先刷新状态，避免继续展示已经结束的无限等待。

对 Bot 的直接纠正、偏好改变、停止发言及取消等待请求由同一个中间模型交给主模型处理。
没有新增纠正词表、直接唤醒旁路或固定回复；主模型仍可保持沉默、记忆偏好或设置等待。
明确等待某人/某内容仍可观察无关聊天，保留有限/无限、期限、TTL 和一次性消费。
新的呼叫/纠正可打断等待，但不冒充满足原条件。

启动时取消旧提示/provider 签名的未完成判断，并重新冻结仍有效的待判内容；
不复用旧的虚假等待快照，不重放已经完成的决定。不改变主 read/seen。

## 上下文与配置展示

消息内容哈希仍用于本地恢复、变更校验、去重和输入身份，不再发送给中间模型。
保留原历史条数和字符上限，没有缩短上下文或修改实际模型/额度/群角色配置。

发布前只读汇总的 25 次真实中间调用：全部成功，无待处理批次；
输入中位数 5657 Token，合计 127020；输出合计 1424；总量 128444。
模型调用延迟中位数 2492ms、p95 3775ms；快照通常带满 30 条历史。
其中 22 批普通观察带有旧等待意图。该计数说明输入事实有偏差，不表示 22 次都漏判。

对同一批 25 个保留快照，用本地 `o200k_base` 比较旧提示/完整输入和新提示/去哈希输入，
估算中位数 5545→4517 Token，合计 124220→102041，下降 17.9%。
此估算已包括新增提示和 participation 字段，但未计 HTTP/provider 固定开销，
不等于真实账单节省或延迟改善；上线后的实际调用继续使用原 judge_attempts 记录核对。

`social_get_state` 展示当前 intent 和常驻 participation，退役字段单列 `retired_config`：
`wakeProbability`、`sleepInterrupts.probability`、`replyAttention`、
`dynamicWait.enabled`、`dynamicWait.unfinishedSeconds`。
默认 JSON 不再推荐 replyAttention 或未完成句开关；旧 JSON 仍兼容读取和保存，
不删用户现有值。固定合并、总等待上限、显式等待 quietSeconds 和睡前观察继续生效。

## 验收方法

确定性回归覆盖普通/忙碌观察、常驻偏好、同批主候选、真实选择性等待、
有限/无限等待、TTL 到期、一次性实际接纳、睡前观察、旧签名恢复及配置展示。
沿用已有批次竞争、主模型忙时投递、重启/暂停/跨话题、失败降级、预算、
无新内容零调用、发送回执与共享工具回归。

语义质量另用独立 Bot OAuth / `openai-codex/gpt-6-luna`，low，512 输出 Token、
15 秒超时，对合成语境运行真实中间调用。评估脚本禁用重试/降级，
只写临时 SQLite，没有 Telegram 接收者、真实群发送或实际 read/seen 更新。
第一轮发现“先别发了，等我看完”被 observe，补充“wake 只表示交主模型执行”后重新验收通过。

21 个样例：称呼/话题根、无称呼续问、短回答、成员互聊、引用旧名字、潜水点名、
相似人名、出发承接、直接纠正、偏好改变、停止发言、纠正其他成员、选择性等待无关聊天、
满足等待、新呼叫中断、纠正中断、睡前观察、未知图片、取消等待及 30 条历史中的纠正。
20 例完整批次和额外 30 条历史样例均符合预期；包括 `fulfills_intent` 及主候选数量核验。
这些有限样例不保证真实聊天零漏判，真实使用反馈继续补入同一套样例。

可重复评估入口：

```bash
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_judge \
  --oauth-dir /home/ubuntu/.local/share/nanobot-bridge-tg-deploy/shared/data/telegram-social/oauth \
  --output /tmp/nanobot-v3c-evaluation.json
```

样例保存在 `nanobot/channels/telegram/tests/fixtures/social_judge_v3c.json`；
脚本不随 wheel 安装，也不自动进入发布时的联网测试。

## 发布与后续

本机发布、完整管线结果、安装镜像验证及备份待 release 完成后补录。

V3-C 交付后下一批为 V2-C，再按 V3 状态适配 V2-D：

- V2-C：补近时段参与量、活跃人数、距确认发送的时间等有界事实；
  主动节奏只负责有效来源的机会调度，不恢复消息概率门槛、关键词树或成员硬筛选。
- V2-D：复用一次漏发确认与现有状态，补可靠收尾、分条/间隔、局部查询和本地反馈；
  合法沉默不视为故障，不按“没有发言”循环叫模型或重置偏好。
- V2-E/F：黑话学习、群知识和媒体表达继续后续；M5 仍暂缓。
