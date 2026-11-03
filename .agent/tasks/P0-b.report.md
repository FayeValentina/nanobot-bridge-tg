1. 改动摘要

新增 `social_harness.py`，使用真实 runtime/store/AgentLoop、可控时钟及脚本化模型。

公开方法：`member_says`、`advance`、`tick`、`queue_due`、`script_model`、`tool`、`edit_role`、`set_proactive`、`restart`、`close`、`sender_outcome`、`interrupt_send`、`fail_judge`、`judge_calls`、`sent`、`wakes`、`last_tool_result`、`model_calls`、`model_saw`。

新增场景覆盖：

- 1：普通消息唤醒、发送回复、30 秒内无 @ 追问继续唤醒。
- 2：judge 超时和异常均 fallback 到主模型。
- 3：选择性等待忽略其他人，目标 sender 发言后唤醒。
- 4：@bot 可打断选择性等待。
- 5：multipart 第二段限流后自动重试，顺序正确且无重复。
- 6：unknown 投递在后续回合不能重复发送。
- 7：非零发送冷却拒绝第二次发送。
- 8：sleeping 后新消息仍经 judge 并能唤醒。
- 9：未领取的 idle 机会被新会话取代。
- 10：activity 开启后仍应产生 idle；严格 XFAIL。
- 11：同一 pending 事项两次 idle 主动跟进后，第三次被拒。
- 12：新 persona 下一回合生效；历史保留独立测试为严格 XFAIL。
- 13：重启恢复未领取 wake；中断发送恢复为 unknown 且不重发。
- 14：假贴纸包完成导入、查看、注释、搜索和发送。
- 15：普通 `message` 与 `social_send` 共用每小时配额。

2. 实际运行的命令及结果

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q
..........x..x....                                                       [100%]
16 passed, 2 xfailed in 9.30s
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
932 passed, 2 xfailed, 13 warnings in 137.64s (0:02:17)
```

```text
.venv/bin/ruff check nanobot/channels/telegram/tests/social_harness.py nanobot/channels/telegram/tests/test_social_scenarios.py nanobot/channels/telegram/tests/conftest.py
All checks passed!
```

```text
grep -nE 'db\.execute|\.store\.|\b_[a-z]' nanobot/channels/telegram/tests/test_social_scenarios.py
```

无输出，退出码 1（未匹配）。

```text
git diff --check
```

无输出，退出码 0。

额外取消 XFAIL 标记定位缺陷：

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q --runxfail -k 'idle_opportunity_remains or role_edit_keeps' --tb=short
2 failed, 16 deselected in 1.85s
```

两处均失败于目标行为断言。

3. 与简报的偏差及原因

- 无 skip。15 个场景拆为 17 个函数、18 个用例，分别隔离 persona 和重启的两种行为，并参数化 judge 故障。
- `conftest.py` 增加两个 fixture：0.05 秒事件循环计时器解决沙箱线程完成通知停顿；本地解析 `example.com` 解决既有附件测试的 DNS 失败。保留真实 SSRF 检查，未修改 `offline_intermediary`。
- 硬崩溃发送窗口由 harness 注入，因为正常协程取消会立即记录 unknown。
- 未改生产代码、已有测试，未提交、推送或部署。工作区已有的 `.agent/tasks/P0-b.md` 修改未触碰。

4. 未解决的问题 / 需要编排者决定的事项

两个已知缺陷仍保留严格 XFAIL：activity 遮蔽 idle，以及 persona 更新截断历史。无其他阻塞事项。