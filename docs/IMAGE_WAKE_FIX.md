# 群图片参与修复

2026-10-30。修复 V3 只能根据图片占位符决定是否唤醒主 Agent 的缺口，并修正普通接话等待继承问题偏好的情况。
本批属于 V4-A/B 上线后的修复，不代表 V4-C 已完成。数据库保持 schema 13，判断协议更新为 social-judge-v6。

## 原因与行为

实际实例中无配文图片已收到、保存引用并提交 V3，但中间模型只收到 `[non-text message]` 和 `media=["photo"]`。
近期图片被判为 observe，未创建主回合，因此主 Agent 无机会调用 social_get_media。
其中普通接话等待还被描述为 waiting_for_questions=true，加重了图片不接话的问题。

V3 现在在认领请求额度后，获取本批新消息及其明确引用消息的图片，将压缩 JPEG 作为原生多模态输入提交同一个判断请求。
图片标签关联 message_id/seq；先处理新图片，再处理引用图片。复用现有按 scope 的 Telegram 下载器、缓存、
字节/像素限制和动态媒体缩略图处理，不增加模型服务或另一条绕过 V3 的唤醒通道。

新增 judge 配置均有声明和 JSON/manifest 默认值：mediaMaxImages=4、mediaMaxEdge=768、mediaTimeoutSeconds=5。
数量同时受原 mediaMaxImagesPerTurn 上限约束，最长边不超过原 mediaMaxEdge；准备图片的总时间最多为
mediaTimeoutSeconds 与判断超时一半的较小值，给模型判断留出时间。原图每张最大 8 MiB、2000 万像素、
转换后 JPEG 不超过 1 MiB 的既有限制保留。超量、离线、损坏或超时图片明示未知，不伪造内容。
图片数据只在本次模型请求中使用，不存入判断批次 JSON/会话快照；原图沿用既有有界媒体缓存。
未到请求额度不下载；重试仍沿用既有次数/期限并复用原图缓存；编辑、暂停、撤权或过期使结果失效。

无配文图片可以是提问、回答或值得参与的分享，由 V3 结合可见内容和语境决定 wake/observe。
有用的图片无需额外点名或问号；无关图片仍可观察，明确的选择性等待继续生效。
图片唤醒理由包含简短可见事实，主 Agent 可继续通过 social_get_media 获取详情并显式发送。

普通 reply/messages 等待不再把群级 wakeOnQuestion、wakeKeywords、wakeSenders 复制成临时选择条件；
这些仍作为 participation 偏好提供。social_set_wake 明确设置的临时条件及其 TTL/一次性语义保留。

## 验证

新增回归覆盖纯图片/配文/图片文档、原生图片输入及 768 像素压缩、相册上限、引用图片、跨 scope 隔离、
下载失败/损坏/超限/超时、下载时编辑或暂停、额度耗尽不下载、重试缓存和普通接话等待不继承问题偏好。
原图片工具测试继续验证主 Agent 取图和原有每回合预算。

实际 openai-codex/gpt-6-luna（low）与 deepseek-flash（temperature=0.1）使用非私有合成图片完成四例：

| 场景 | 中间模型与主 Agent 结果 |
| --- | --- |
| 无配文图片中写有 What is 7 x 8? | wake；主 Agent 取图，显式发送“56” |
| Bot 要求看图，成员仅发送红色方形 | wake、满足接话意图；主 Agent 取图后描述红色正方形并追问 |
| 明确等待指定成员“到家”，收到无关红色方形 | observe；不调用主模型、不发送 |
| 成员引用题目图片问“这个怎么解？” | wake；主 Agent 读取被引用图并回答 7×8=56 |

四例有效验收共 4 次中间模型与 9 次主模型调用，36,737 tokens；中间模型阶段约 2.1–3.4 秒，
主模型阶段约 3.4–3.7 秒。均使用临时 SQLite、替身下载和替身发送，不启动 Telegram 接收者、不向真实群发探测消息。
合成样例证明图像输入、语义判断和明确发送可达，不保证每张图片均应回复。

## 发布与安装包核验

本机实际实例已更新到 `2ceb736339f1a90b6f9aa420c15b0678966a96c8`，schema 13，social-judge-v6。
完整 release 通过后端 1141 项、provider/OAuth 155 项、前端 96 文件 / 1553 项、Ruff、
WebUI lint/build、wheel/镜像冒烟及健康检查。修改运行模块严格类型 0 errors / 0 warnings。

安装包内再次验证无配文图片题目和图片回答两例，均由实际 V3 唤醒主 Agent；主 Agent 实际调用
social_get_media、social_send 和 social_wait，替身发送分别为“56 呀”和对红色正方形的自然接话。
导入路径为 `/opt/venv/lib/python3.11/site-packages/`，无源码覆盖。两例共 2 次中间模型与 6 次主模型调用、
23,134 tokens；中间模型阶段约 2.5–2.8 秒，主模型阶段约 3.3–3.8 秒。
验收脚本及源端/安装报告保存在实际实例 `verification/image-wake/`。

config.json 与发布前备份原始字节相同，模型/OAuth、角色、群范围、主动周期和额度配置保留。
按源值直接比较，123 条既有消息、5 条记忆、6 条证据、3 个 scope 的主 seen/read/next 水位、
8 份 Markdown 和 18 份 JSONL 对话正文保留，SQLite quick_check / foreign_key_check 正常。
运行 epoch/context、会话归档元数据、派生索引和 dream/heartbeat 登记仍按既有启动逻辑刷新。
容器 healthy，Telegram 已连接，端口仍为 8765 / 18790。未向真实群发送验收消息。

上一镜像 `9e75871d5e0559ae96eb36f8784b9e1f7a744b0a`；发布前整实例备份为
`backups/1793367613346247434/`，需要回滚时沿用原管线恢复对应镜像与数据/配置。
