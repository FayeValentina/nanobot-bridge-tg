1. 改动摘要（按文件）

- `runtime.py`：新增每 scope 的发送前检查记录、通知常量和 `_collect_new_messages`；抽出 `read_messages_for`；仅在 `send`、`send_parts` 的幂等分支之后接入检查。
- `bindings.py`：原位追加指定协议句。
- `test_social_scenarios.py`：新增黑盒场景，覆盖 turn 中途到达 B/C、首次发送拒绝、合并发送成功及结束后不再唤醒。
- `test_social_scheduler.py`：新增 text/parts 单次拦截、曝光收取 scheduled wake、自身消息、幂等回执和延迟 judge 测试。

生产代码新增 28 行、删除 2 行。既有测试未作修改。

2. 实际运行的命令及结果（原样贴最后几行）

沿用派发环境 `PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`。

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
957 passed, 13 warnings in 143.79s (0:02:23)
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
22 passed in 10.73s
```

```text
.venv/bin/ruff check nanobot/channels/telegram
All checks passed!
```

```text
.venv/bin/basedpyright nanobot/channels/telegram/social/runtime.py
0 errors, 0 warnings, 0 notes
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider -k send_rechecks
1 failed, 21 deselected in 1.52s
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py nanobot/channels/telegram/tests/test_social_scheduler.py -q -p no:cacheprovider -k 'send_rechecks or collects_new or new_self or old_receipt'
1 failed, 4 passed, 83 deselected in 2.34s
```

上述开发期失败分别修正了新增测试的中途执行时点、旧回执附带 `retry=False` 导致的整体比较问题。最终定向检查：

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py nanobot/channels/telegram/tests/test_social_scheduler.py -q -p no:cacheprovider -k 'send_rechecks or collects_new or new_self or old_receipt or late_judge_reuses'
7 passed, 83 deselected in 3.13s
```

`git diff --check`：退出码 0，无输出。

3. 与简报的偏差及原因

无。未修改配置、judge、wake 调度、runner/loop、普通 message 路径或 `_check_send_allowed`。未提交、推送或部署。

4. 未解决的问题 / 需要编排者决定的事项

延迟 judge 的结论：**对于 B/C 所在批次的 `new_seqs` 和 `source_seqs` 均已曝光的情形，不会另起 turn。** 现有 [`Judge._handoff`](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/judge.py:205) 会把 `handoff_job` 指向已曝光该批消息的原 wake，并直接返回；查找范围包含运行中及已完成的最近 32 个 wake，见 [`recent_turns`](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/wakes.py:460)。

新增[延迟 judge 测试](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/tests/test_social_scheduler.py:552)验证了 B/C 已曝光并 mark_read 后，原 turn 仍运行、已经完成两种情况：judge 返回 wake，但 wake 总数仍为 1，后续入站队列为空。

若 judge 批次还包含未曝光的新消息，上述复用条件不成立，仍可能创建新 wake；本任务未改变此行为。10 秒合并窗口留待 Claude 部署时调整。