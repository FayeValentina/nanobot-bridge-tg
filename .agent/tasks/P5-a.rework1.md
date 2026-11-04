# P5-a 返工 1：跨工具保护的 kind 条件（编排者确认）

你指出的冲突成立，是简报遗漏。以下两处查询**允许修改 kind 条件**，其余设计不变，请完成 P5-a 全部内容：

1. `Deliveries.unknown_content`（unknown 内容防重）：删除 `kind=?` 条件，不再按 kind 区分（与 `unknown_part` 一致）。
   贴纸的 content 形如 `[sticker:<unique_id>]`，不会与文字内容相同，所以对贴纸语义不变；text 与 message 由此共享同文 unknown 防重。
   `SocialStore.reserve_send` 中传给它的 kind 参数随之删除。
2. `Deliveries.latest_content`（social_send 同 turn 同内容返回旧回执）：kind 为 `text` 时匹配 `kind IN ('text','message')`，kind 为 `sticker` 时只匹配 `sticker`。
   原因：合并前普通 message 的预约行 kind 是 `text`，同 turn 先用 message 工具发、再用 social_send 发同样内容时会返回原回执而不是再发一次；合并后要保持这一点。

请同时检查其他按 kind 过滤的查询是否存在同类问题（原来依赖“message 预约行 kind=text”的地方），如有，按同样原则（text 包含 message）处理并在报告中列出；没有就写“无”。
完成后按原简报的报告格式输出完整报告，第 2 节补充这两处的落点。
