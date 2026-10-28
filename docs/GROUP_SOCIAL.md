# 按群启用 Telegram 仿真群友

2026-10-28：social scope 支持 `allTopics: true`，一条群配置覆盖该群的所有现有及未来话题，
也覆盖 General/没有 thread ID 的消息。群以 `chatId` 匹配，不会扩展到其他群或私聊。

在实际挂载的 `config.json` 的 `channels.telegram.social.scopes` 中设置：

```json
{
  "chatId": -1001234567890,
  "allTopics": true,
  "roleId": "groupmate",
  "enabled": true,
  "nicknames": ["肥鱼", "肥魚"],
  "proactive": {"enabled": true, "intervalSeconds": 1800},
  "quietHours": {"enabled": false}
}
```

`allTopics` 默认 false，旧配置继续只匹配 `threadId` 指定的话题（默认 0）。
设为 true 后 `threadId` 不参与该条群配置的匹配，可以省略。
显式单话题配置优先于群配置，`enabled: false` 的单话题配置可作为例外关闭该话题。
网页仍使用原 Telegram 设置中的 social JSON 编辑器，保存需通过同一后端 schema 校验。

角色、观察权限、主动机会和安静时段配置由该群配置提供；聊天记录、已读水位、等待/潜水、
预算和主动候选仍按实际群/话题分别记录。回复发回原话题，不合并不同话题的历史。
新话题在收到第一条可观察消息或首次建立会话时按需建立状态，不预先枚举 Telegram 话题。
重新配置及重启使用已发现话题的实际绑定恢复候选，撤销群配置使相关任务失效。
现有共用工具权限、私聊和其他群的普通处理不变。

身份说明：`directed` 唤醒已经由 Telegram 的真实账号提及/回复校验，不能因为旧共享记忆
使用另一名称而当作在叫其他机器人。运行角色文件应明确账号和昵称，纠正共享记忆中的错误
身份断言；不删除聊天历史或普通共享记忆，不另加 social 工具权限限制。

验证覆盖普通旁观零调用、General 与新话题的点名及原话题发送、显式关闭例外、其他群边界、
旧配置往返、历史保留、主动候选重启恢复和撤销后取消。实际发布结果记入本机管线说明。

## 本机发布结果

2026-10-28 已发布 `ea734a37` 并启用当前群的 allTopics，配置保留主动机会开启、quietHours 关闭，
补充“肥鱼/肥魚”昵称。角色、SOUL 和共享记忆明确当前账号就是 @Deep_Fat_Fish_bot，
保留其他记忆和原始会话历史，不再将账号当作另一个机器人。
新增 8 项回归；完整 release 通过后端 826 项、provider 116 项、前端 1553 项及 lint/build。
修改模块严格类型为 0 errors / 0 warnings。安装镜像临时数据验证 General、原话题、新话题的
旁观、唤醒与原路发送；未向真实群发送测试消息，也未调用真实模型做测试。
实际实例健康、Telegram 已连接，SQLite quick_check 正常，原消息和已读水位保留。
