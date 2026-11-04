1. 改动摘要（按文件）

- `telegram_social.py`：增加 `parts.items.anyOf`；规范化逐段引用；顶层 `reply_to` 仅补充第一段缺省目标；更新指定描述。
- `social/delivery.py`：逐段验证引用目标，按完整 `(content, reply_to)` 列表判断幂等。
- `social/store.py`：每段分别保存自己的 `reply_to`。
- `social/bindings.py`：仅追加指定协议原文。
- `test_social_scheduler.py`：机械修改 10 处旧调用，保留原首段引用测试；新增要求的工具、拒绝入队、幂等及 schema 覆盖。
- `test_social_dialogue.py`：机械修改 1 处调用。
- `test_social_followups.py`：机械修改 3 处调用。
- `test_social_proactive.py`：机械修改 2 处调用。

生产代码净增 13 行。共机械修改 16 处旧测试调用，未修改旧断言；AST 对比确认已有测试函数其余内容不变。未发现评估脚本调用。

2. 实际运行的命令及结果

pytest 沿用已有 `PYTHONPATH=.agent` 和 `PYTEST_ADDOPTS="-p codex_pytest_env"`。

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
975 passed, 13 warnings in 146.94s (0:02:26)
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
22 passed in 11.15s
```

```text
.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider
83 passed, 2 skipped in 5.27s
```

```text
.venv/bin/ruff check nanobot/ scripts/
All checks passed!
```

```text
.venv/bin/basedpyright nanobot/channels/telegram/social/ nanobot/agent/tools/telegram_social.py
0 errors, 0 warnings, 0 notes
```

首次定向检查：

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scheduler.py -q -p no:cacheprovider -k 'burst or social_send_tool or social_send_parts_schema'
4 failed, 13 passed, 59 deselected in 3.58s
```

原因是新增测试读取的查询结果不包含 `reply_to` 字段；已移除该冗余断言，通过实际 sender 参数验证引用。上述最终完整测试全部通过。

`git diff --check` 通过，无输出。

3. 与简报的偏差及原因

无设计或范围偏差。

幂等实测：相同各段文本及引用返回旧回执；仅改变第二段引用目标时，进入现有检查并返回 `an earlier send is awaiting delivery`，未新增 action。

4. 未解决的问题 / 需要编排者决定的事项

无。未提交、推送或部署。