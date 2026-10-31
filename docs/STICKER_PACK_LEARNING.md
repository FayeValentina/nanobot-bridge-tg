# Telegram 表情包链接与看图学习

2026-10-31，用户提供 `https://t.me/addstickers/LLM_Moe` 后，肥鱼只用网页工具读到
「LLM小妹」标题，随后要求用户逐张发送。旧 `social_sticker` 只有 list/collect/send，
没有整包读取入口。这是本地工具能力缺口；Telegram `getStickerSet` 可返回整包及各张 file_id。

## 行为

主协议 5 保留原有参与与发送规则，新增由主模型执行的学习流程：

1. `social_sticker(action="import_set", set_name="https://t.me/addstickers/LLM_Moe")`
   通过 Telegram SDK 读取并收藏整包。也接受包名与 `tg://addstickers?set=...`。
   请求只传包名给固定 Telegram 接口，不抓取模型提供的任意网址。
2. `social_sticker(action="view", set_name="LLM_Moe")` 返回带序号的原生图片拼图和
   序号与 sticker_id 的对应表；每批最多 30 张，默认只展示还没有描述的贴纸。
3. `social_sticker(action="annotate", notes=[{"sticker_id":"...", "note":"画面、可见文字、情绪、适用语境"}])`
   保存主模型看图后的描述。只能标注当前回合成功展示的图片，一批错误不产生部分写入。
4. 重复 view/annotate，下一批保持 offset=0；已标注项自动移出未学习列表。
   之后用原有 list 按描述或 emoji 搜索，用 send 发送，支持直接用贴纸接梗。
   查看特定已收藏贴纸可传 sticker_id；也可 unlearned_only=false 并按 offset/limit 翻页。

导入和学习结果分开记录：空描述表示尚未看图标注；重启后仍能接着处理未学习项。
重复导入更新 Telegram 文件引用，保留现有描述和使用次数。整包超过收藏上限时完整拒绝，
不会只导入一部分。当前表 stickers 及 media_ref 字段足够承载，无数据库 schema 变更。

角色新增偏好：合适时多用肥鱼贴纸，可以只发贴纸；收到包链接并被要求查看或学习时，
自行导入、看图、记录语义，不要求逐张投喂。网页标题不算看过图片。

## 范围与限制

仍只支持静态普通贴纸。导入时明确计数跳过的动态/视频贴纸，纯动态包明确报错。
当前用户提供的 LLM_Moe 已实际读取确认是 120 张静态普通贴纸。

一张拼图占一个现有图片额度；每张贴纸分别经过原有下载字节、像素、格式和缓存限制。
默认每轮 4 张图片，最多可看 4 批共 120 张；如果当轮已用掉图片额度或部分图片读取失败，
模型应如实说明部分进度，后续回合再继续。没有新增后台模型、定时学习任务或无限重试。
描述存入当前群/话题的原有表情库；发送保持原有节奏、额度、去重与确认回执。

## 验证方式

`test_social_stickers.py` 覆盖链接解析、120 张导入、重复导入与保留已有学习、原子限额、
暂停期间丢弃查包结果、跨话题隔离、图片拼图、看图后标注、失败图片与共享图片额度、
SDK 归一化，以及真实 Agent 执行链上的导入→原生看图→标注→发送。

可选实际模型验收（只读 Telegram，临时数据库，全部发送为替身）：

```bash
.venv/bin/python -m nanobot.channels.telegram.tests.evaluate_social_stickers \
  --config /path/to/private/config.json --output /path/to/private/verification
```

该验收以普通中文请求触发主模型自行选择工具，检查全包学习与按语境选择发送。
私有报告保留实际工具参数、描述、用量和确认发送记录，不把替身发送算作真实群发送。

源端验收已通过：实际实例的 `deepseek-flash` 在普通中文请求后，自行导入 120 张，
依次查看 4 张拼图并标注全部 120 张；随后两次按语境选择贴纸并取得替身发送回执。
没有向真实群发送测试消息。整段验收耗时 214.43 秒，记录用量为输入 292802、输出 10286
Token（含重复回传的图片/工具历史）；这是一次整包学习验收，不代表日常单次选图成本。
18 项新增回归和相关运行模块严格类型检查通过；实际描述与报告在实例私有
`verification/sticker-learning-20261031/`，不将它们提交进源码仓库。
完整本机管线检查通过：后端 1195 项、provider/OAuth 155 项、前端 1553 项及 lint/build。
