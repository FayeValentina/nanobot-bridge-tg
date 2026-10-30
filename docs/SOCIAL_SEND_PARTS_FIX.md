# social_send 首段重复修复

2026-10-30，V4-A/B 上线后的发送契约修复。schema 保持 13，图片判断 social-judge-v6 保留，V4-C 仍待实施。

## 实际问题

成员消息 6846“明天还在花莲 给点建议 怎么安排”只产生一次 V3 wake 和一个主回合。
主模型仅调用一次 social_send，却把上午行程同时放进 text 与 parts[0]；两段仅换行、标点略有差别。
旧工具把 `[text, *parts]` 拼成三条，得到消息 6847、6848、6849。三个原生 action 均为一次发送、sent，
属于同一 group_id。第一段重复来自双字段表达歧义，平台重试与重复主回合均未发生。

## 修复

social_send 现在只接受以下一种形式：

```json
{"text": "单条完整消息"}
```

或：

```json
{"parts": ["第一条完整消息", "第二条完整消息"]}
```

parts 是完整的 2–3 条有序消息，包含首条；text 与 parts 互斥。JSON Schema、参数说明、可信协议、
工具参数校验和直接 execute 入口均表达该约束。旧双字段输入会返回明确纠错信息，整次调用不入队、
不占发送额度；主模型可以改用完整 parts 重新调用，不会发送错误参数中的首段。

沿用现有 send_parts 队列、节奏、逐条回执、相同调用复用 action、失败/未知处理及 followup_id 关联。
这次没有增加文本相似度算法、哈希或独立发送通道；不修改已有消息，不自动删除群里的重复回复。

## 验证

六个新增确定性用例覆盖遗漏字段、双字段原样/格式变体、2/3 条完整 parts、重入复用回执，
以及实际 AgentRunner 收到旧参数错误后纠正并只发送两条。
受影响发送/调度/跟进/主循环 138 项通过；修改运行模块严格类型零错误/警告，Ruff 通过。

实际 deepseek-flash（temperature=0.1）完成三项合成验收：正常分两条发送、旧双字段错误后改用完整 parts、
单条 text 发送。均通过，每项两个模型调用，共 17,234 tokens；使用临时 SQLite 与替身发送，不向真实群发消息。
验收脚本匹配 provider 的 reasoning_content 历史回传要求；该驱动修正不改变运行时 provider。

## 发布与安装包验证

实际实例已更新到 `310011ce124368b46c4b5f520a7dfc965651fdf7`，schema 13，social-judge-v6。
完整 release 通过后端 1147 项、provider/OAuth 155 项、前端 96 文件 / 1553 项、Ruff、
WebUI lint/build 和 wheel/镜像冒烟、健康检查。

安装包内重新执行以上三项实际模型验收，均通过；正常分条和错误纠正各只确认两次替身发送，
单条只确认一次发送，每条均有独立原生 action/平台消息 ID。
导入路径为 `/opt/venv/lib/python3.11/site-packages/`，无源码覆盖；共 6 次主模型调用、17,187 tokens。
脚本与源端/安装报告保存在实例 `verification/social-send-parts/`，没有向真实群发探测消息。

config.json 与发布前备份原始字节相同；131 条旧消息、5 条记忆、6 条证据、3 个 scope 的既有水位、
8 份 Markdown 与 18 份 JSONL 对话正文按源值核对保留，SQLite 完整性和外键检查正常。
运行 epoch/context、归档元数据、派生索引及 dream/heartbeat 登记仍按原启动逻辑刷新。
容器 healthy，Telegram 已连接，端口仍为 8765 / 18790。
上一镜像为 `2ceb736339f1a90b6f9aa420c15b0678966a96c8`，发布前整实例备份为
`backups/1793371526866276960/`，回滚沿用原管线恢复配套镜像和数据/配置。
