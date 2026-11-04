1. 改动摘要（按文件）

- `social/runtime.py`：移除发送方法和 hook，装配 `SocialDelivery`；保留指定接入点，无转发方法。
- `social/context.py`：移入 `encode_view`，新增 `snapshot_text`。
- `social/delivery.py`：迁入全部 14 个发送方法、状态和常量；`_retry_sends` 改为 `retry_due`。
- `social/hook.py`：迁入 `SocialExposureHook`，按指定签名注入依赖，删除 runtime 引用。
- `telegram/runtime.py`：机械修改 4 处调用位置。
- `agent/tools/telegram_social.py`：机械修改 3 处调用位置。
- `scripts/evaluate_social_selection.py`、`scripts/evaluate_social_v5.py`：分别修改 2、5 处；未运行评估脚本。
- 测试逐文件改名数量如下；断言仅替换访问位置，内容不变：

| 文件 | 数量 |
|---|---:|
| `test_reply_reliability.py` | 4 |
| `test_social_activity.py` | 1 |
| `test_social_dialogue.py` | 11 |
| `test_social_followups.py` | 19 |
| `test_social_media.py` | 15 |
| `test_social_memory.py` | 2 |
| `test_social_proactive.py` | 11 |
| `test_social_runtime.py` | 11 |
| `test_social_scheduler.py` | 29 |
| `test_social_selection.py` | 8 |

`social_harness.py` 无需修改；`test_social_scenarios.py` 和 fixtures 均未修改。

2. 实际运行的命令及结果（原样贴最后几行）

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
957 passed, 13 warnings in 152.56s (0:02:32)
```

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
......................                                                   [100%]
22 passed in 11.61s
```

```text
$ .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider
.......s.....                                                            [100%]
83 passed, 2 skipped in 5.78s
```

```text
$ .venv/bin/ruff check nanobot/ scripts/
All checks passed!
```

```text
$ .venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py nanobot/channels/telegram/runtime.py
1886 errors, 0 warnings, 0 notes
```

上述原命令修改前、后均为 1886 errors，默认未找到 `.venv` 依赖。前后诊断直接比较结果：

```text
telegram/runtime.py diagnostics: before=897, after=897, identical=True
```

用 `/tmp/p6b-pyright.json` 保持 strict、Python 3.11、排除 tests，并显式指定项目 `.venv`：

```text
$ .venv/bin/basedpyright -p /tmp/p6b-pyright.json nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py nanobot/channels/telegram/runtime.py
0 errors, 0 warnings, 0 notes
```

```text
$ grep -n "self.runtime" nanobot/channels/telegram/social/hook.py nanobot/channels/telegram/social/delivery.py
```

无输出，退出码 1，即 0 命中。`git diff --check` 无输出，通过。

直接 AST 比对结果：

```text
delivery: 14 moved methods match original AST after specified substitutions
test assertions: all unchanged after reversing mechanical delivery names
hook: 8 moved methods match original AST after specified substitutions
```

`wc -l` 前后对比：

| 文件 | 修改前 | 修改后 |
|---|---:|---:|
| `social/runtime.py` | 727 | 341 |
| `social/delivery.py` | 不存在 | 296 |
| `social/hook.py` | 不存在 | 143 |
| `social/context.py` | 71 | 85 |
| 合计 | 798 | 865 |

3. 与简报的偏差及原因

- 生产代码净增 **67 行**，超过预期上限 27 行；增量来自拆分模块所需导入、依赖注入构造及模块说明，未增加业务逻辑或额外抽象。
- 指定的原始 basedpyright 命令未达到 0 errors；环境显式指定 `.venv` 后为 0 errors。未修改项目配置或代码处理环境问题。
- 未提交、推送、构建或部署，遵守编排模式。

4. 未解决的问题 / 需要编排者决定的事项

- 决定是否接受净增 67 行的规模偏差。
- `evaluate_social_v5.py` 的冻结编译部分按要求保留；冻结 hook 仍引用 `self.runtime`，并依赖 runtime 模块中已移出的 `current_request_context`、`append_runtime_context`。此次未做兼容适配、未运行评估，后续需编排者决定处理方式。