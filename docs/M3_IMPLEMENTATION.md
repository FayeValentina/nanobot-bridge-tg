# M3：图片、引用与静态贴纸

2026-10-28，在 M2 的隔离、调度、预算和平台回执基础上增加按需媒体读取、结构化引用、
相册关联和当前群/话题的静态贴纸收藏与发送。本批不引入独立服务或媒体转换器。
已发布到本人实际使用实例，版本 `ec17165b7a8593220eb7ccfd9df7bc2f4cdb0474`，
容器 healthy、Telegram 已连接。social 已启用，配置和角色与发布前备份完全一致。

## 支持范围

- 图片：Telegram 照片、图片文档及静态贴纸，解码支持 JPEG、PNG、WebP、单帧 GIF。
  普通旁观只保存平台文件引用，不下载图片、不触发视觉请求。获准回合需要时调用
  `social_get_media(message_id)`，返回现有工具结果协议支持的原生 `image_url` 块。
- 引用：快照提供被引用消息的 ID、发言者 ID/显示名、时间、是否为当前机器人及有界文字，
  能区别回复机器人和群友互相对话。当前 Telegram 更新实际带回的旧引用也可读图和引用发送，
  不把它伪造成新观察序号，也不推进原消息的已读水位。不同群/话题的引用不授予访问权限。
- 相册：每条保留自己的 message_id 和共享 album_id；复用 M2 的短窗口合并唤醒。
  模型按各条消息读取，受整个回合的图片额度约束。
- 静态贴纸：保存 file_id、file_unique_id、emoji、贴纸集及类型/缩略图元信息。
  `social_sticker` 支持 list（可带 query）、collect（message_id、可选 note）、
  send（sticker_id、可选 reply_to）。只能收藏当前作用域实际收到的静态贴纸。
  file_unique_id 用于去重和收藏键；下载/发送使用当前 Bot 的 file_id，重复收藏更新文件引用。
- GIF 动画、WebM 视频/贴纸、TGS：有缩略图时按需读取缩略图，并明确说明未解码动态内容；
  无缩略图时返回 emoji/类型等元信息。逐帧理解、TGS 渲染和动画贴纸发送留作后续按需扩展。
  当前镜像不需要 ffmpeg，也不假设 ffmpeg 能直接解码 TGS。

媒体下载、解码、尺寸或平台错误返回明确的不可用标记；文字观察和后续发送可以继续。
原始二进制与模型 JPEG 副本分别保存于 `<config-dir>/telegram-social/media-cache/`，
文件名由作用域和平台文件标识散列生成。模型不能提交任意 URL、文件路径、file_id 或目标群。
旧 M1/M2 消息只有类型标记，没有文件引用，不能据此补取图片；新收到的引用可提供可用引用。

## 限额与配置

新增字段仍通过现有 Telegram 设置页的 social JSON 与统一 Pydantic 契约配置，使用 camelCase。
已有 enabled、scopes、角色文件和其他设置无需改写。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| mediaMaxBytes | 8388608 | 单次下载最多 8 MiB；未知文件大小拒绝下载 |
| mediaMaxPixels | 20000000 | 解码前检查像素总量 |
| mediaMaxEdge | 1536 | 模型 JPEG 最长边，单张输出最多 1 MiB |
| mediaMaxImagesPerTurn | 4 | 整个回合共用图片尝试额度，缓存读取和失败也计入 |
| mediaCacheBytes | 67108864 | 原件与副本合计最多 64 MiB，写入前清理旧缓存 |
| stickerLimit | 200 | 每个群/话题最多收藏 200 张静态贴纸 |

单次文件下载最长 20 秒，图片解码在工作线程中执行。并发工具调用共享额度；暂停、撤权、
角色/配置变化会在下载后和图片返回前再次校验，旧回合不能继续读取或发送。
贴纸笔记最多 400 字符、查找结果最多 30 条；笔记作为不可信结构化资料返回，
不进入可信人格或其他群的提示词。默认按精确群/话题隔离，比跨话题共享更保守。

## 平台发送与恢复

贴纸和文字共用同一 scope 的发送锁、冷却、小时发送尝试预算、动作日志与有界 429 重排。
重复调用返回原动作；收到 Telegram message_id 后才记录 sent，并在同一事务增加使用次数。
超时/断网结果不明为 unknown，不自动重发；明确 BadRequest（如失效 file_id、失效引用）
为 failed，不计使用次数。暂停取消排队动作；重启把已尝试但未确认的动作变为 unknown。
所有发送保留 thread_id，引用失效时失败，不改发到默认话题。

SQLite schema 2→3 在事务内增加媒体/引用/相册字段、动作种类及 stickers 表，
保留旧消息、水位、动作和平台回执。schema 1 仍经既有升级路径进入 3。
损坏库不按空库覆盖。旧程序不接受 schema 3，降级必须连同发布前实例数据备份恢复；
现有本机发布管线会在停服后备份整个 shared/data，并在健康检查失败时自动恢复。

## 模型适配与验证

角色来源查询：可信提示说明人格内容已经注入；被问及配置位置时调用 `social_get_state`，
读取当前作用域的 `role_id`、运行环境内的 `role_file` 和 `can_edit_role: false`。
模型据此回答真实路径，不推测宿主挂载路径；群仿真仍由操作者管理角色文件。
两群实际 Agent/工具回归确认路径按绑定返回，私人资料和通用文件工具仍不可见。

当前实际模型继续使用接口名 deepseek-flash。修正旧兼容适配仅允许视觉实验模型的限制，
为当前 Flash 及其兼容别名保留 user/tool 消息内的原生图片块；未更改接口模型名。
平台/模型拒绝图片时沿用 provider 的明确“图片未送达模型”降级，禁止依据类型标记编造图像。

离线验收在 `nanobot/channels/telegram/tests/test_social_media.py`，使用真实 SQLite、Pillow、
AgentLoop 和本地 Telegram/provider 替身；已纳入本机管线。覆盖旁观零下载、原生图片工具结果、
缩放与缓存、坏图与限额、旧引用、相册、跨群/话题拒绝、静态贴纸收藏去重、共享预算、
真实回执计数、平台失败、429、暂停、恢复、损坏库及 schema 升级。验收不向实际群发送测试消息。

媒体和仿真修改模块严格类型检查为 0 errors / 0 warnings。
整个 OpenAI 兼容 provider 的类型检查仍有 3 项可选 langfuse 缺依赖错误；
已对比未修改基线，错误完全相同，本批未安装该无关可选依赖。

| 最终检查 | 结果 |
| --- | --- |
| 新增 M3 媒体验收 | 44 项通过，已纳入管线 |
| 正式 release 后端 / provider 适配 | 630 / 116 项通过 |
| WebUI | 96 个文件、1553 项通过，lint/build 通过 |
| Ruff / 补丁空白检查 | 通过 |
| 实际实例 | 健康、Telegram 已连接，运行人格/工具配置指纹匹配 |
| 数据与配置 | schema 2→3、quick_check、水位保留通过，配置及角色与备份一致 |

发布前完整备份位于实例目录 `backups/1793176258314665672/`，上一运行版本为 `3e2f50ba`。
回滚继续使用现有 `./scripts/deploy-local.sh rollback`，同时恢复版本及对应数据。

参考 qq-bridge `src/sticker-lib.js`、`src/media-normalize.js` 的二代行为契约，Python 实现独立重写，
未打包其角色、媒体素材或凭据；来源为 [qq-bridge a49877f](https://github.com/Derpyu520/qq-bridge/tree/a49877f)，
MIT，Copyright (c) 2026 Derpyu520 and qq-bridge / QSH contributors。
协议依据：[Telegram Bot API](https://core.telegram.org/bots/api#sendsticker)、
[DeepSeek Chat Completions（含工具图片输入）](https://api-docs.deepseek.com/api/create-chat-completion/)。
