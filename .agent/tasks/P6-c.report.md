1. 改动摘要（按文件）

- `social/expressions.py`：注入 store、bindings、judge_busy；删除 runtime 引用；通过 peer 保持学习批次互斥。
- `social/jargon_learning.py`：调整构造签名及 binding 调用。
- `social/selection.py`：改用注入的 store、config、now；SQL 未改。
- `social/runtime.py`：删除 learning_task，连接两个 learner 的 peer；新增 activity_action、expression_action。
- `social/delivery.py`：新增 followup_status。
- `agent/tools/telegram_social.py`：拆为基类及 12 个工具类，新增 SOCIAL_TOOL_CLASSES；保留工具契约及错误处理。
- `cli/gateway_runtime.py`：仅调整工具导入和注册。

机械改名数量如下；除 `scripts/` 外，路径均相对 `nanobot/channels/telegram/tests/`：

| 文件 | 导入 | 构造调用 | task 引用 |
|---|---:|---:|---:|
| social_harness.py | 1 | 1 | 0 |
| test_reply_reliability.py | 1 | 2 | 0 |
| test_social_jargon.py | 0 | 0 | 2 |
| test_social_media.py | 1 | 2 | 0 |
| test_social_memory.py | 1 | 2 | 0 |
| test_social_runtime.py | 1 | 1 | 0 |
| test_social_stickers.py | 1 | 1 | 0 |
| test_social_wake_v2.py | 1 | 2 | 0 |
| evaluate_social_followups.py | 1 | 1 | 0 |
| evaluate_social_stickers.py | 1 | 1 | 0 |
| evaluate_social_v4.py | 1 | 1 | 0 |
| scripts/evaluate_social_selection.py | 1 | 1 | 0 |
| scripts/evaluate_social_v5.py | 1 | 1 | 0 |

另在 `test_social_runtime.py` 新增指定的工具集合一致性测试。已有断言仅机械改名；fixtures 和场景文件未修改，评估脚本未运行。

2. 实际运行的命令及结果（原样贴最后几行）

```text
$ PYTHONPATH=.agent:. PYTEST_ADDOPTS="-p codex_pytest_env" .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
958 passed, 13 warnings in 152.21s (0:02:32)

$ PYTHONPATH=.agent:. PYTEST_ADDOPTS="-p codex_pytest_env" .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
......................                                                   [100%]
22 passed in 11.59s

$ PYTHONPATH=.agent:. PYTEST_ADDOPTS="-p codex_pytest_env" .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider
.......s.....                                                            [100%]
83 passed, 2 skipped in 5.93s

$ .venv/bin/ruff check nanobot/ scripts/
All checks passed!

$ .venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py
989 errors, 0 warnings, 0 notes

$ .venv/bin/basedpyright -p /tmp/p6c-pyright.json nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py
0 errors, 0 warnings, 0 notes
```

`/tmp/p6c-pyright.json` 保持 strict、Python 3.11、排除 tests，并显式指定项目 `.venv`。未修改项目配置或沙箱插件。

```text
$ PYTHONPATH=. .venv/bin/python /tmp/p6c-export-tools.py /tmp/p6c-tools-before.json
12 tool contracts exported to /tmp/p6c-tools-before.json

$ PYTHONPATH=. .venv/bin/python /tmp/p6c-export-tools.py /tmp/p6c-tools-after.json
12 tool contracts exported to /tmp/p6c-tools-after.json

$ cmp /tmp/p6c-tools-before.json /tmp/p6c-tools-after.json
```

无输出，退出码 0，两份契约逐字节相同。

```text
$ grep -rn "self.runtime" nanobot/channels/telegram/social/
$ grep -n "runtime.store\|\.library\." nanobot/agent/tools/telegram_social.py
```

均无输出、退出码 1，即零命中。

```text
$ wc -l nanobot/channels/telegram/social/runtime.py nanobot/agent/tools/telegram_social.py nanobot/channels/telegram/social/expressions.py nanobot/channels/telegram/social/selection.py
  352 nanobot/channels/telegram/social/runtime.py
  301 nanobot/agent/tools/telegram_social.py
  366 nanobot/channels/telegram/social/expressions.py
  363 nanobot/channels/telegram/social/selection.py
 1382 total
```

`git diff --check` 无输出，通过。

3. 与简报的偏差及原因

- 工具文件从 201 行增至 301 行，净增 100 行，超过预计 ±40 行；增加来自拆类后的独立声明、方法和注册表。其余生产代码合计净增 14 行，符合 +20 行预期。
- 原始 basedpyright 命令未达到零错误，默认环境未解析 `.venv` 依赖；显式指定环境后为零错误。
- 基类返回处增加了类型 cast，解决 `list[Unknown]` 收窄错误，无运行时行为变化。
- 未提交、推送或部署。

4. 未解决的问题 / 需要编排者决定的事项

- 需编排者确认是否接受工具文件行数超过预期。
- 验收类型检查时需显式指定 `.venv`；原始命令的零错误标准尚未满足。