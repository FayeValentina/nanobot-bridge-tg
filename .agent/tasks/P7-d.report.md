1. 改动摘要（按文件）

| 文件 | 改动 |
|---|---|
| `selection.py` | 容忍对象 ID、忽略额外字段；跳过无效候选并记录 `dropped`；同 turn 直接复用首次结果，结束时清缓存；移除输入复核及全部 SQL。 |
| `expressions.py` | 接收表达候选查询、来源失效、选择合法性、使用时间和数量查询。 |
| `jargon.py` | 接收黑话对应查询及 usable 确认、状态统计。 |
| `db/stickers.py` | 新增固定表名的候选查询和 `row()`。 |
| `learning.py` | 新增 `attach_libraries()`；跨拥有者 SQL 改调 Library，保留写入业务约束。 |
| `runtime.py` | 装配 Library 引用。 |
| `hook.py` | 通过已有 `context_builder.selector` 调用 `forget()`，不增加构造依赖。 |
| 测试 | 新增容错、缓存、截断、整体错误及两次原始日志输出回放；收紧 SQL 归属断言；适配独立 Store 装配。 |

生产代码净增 **30 行**。`wc -l` 前后：

| 文件 | 修改前 | 修改后 |
|---|---:|---:|
| selection.py | 363 | 337 |
| learning.py | 377 | 369 |
| expressions.py | 312 | 336 |
| jargon.py | 380 | 408 |

2. 实际运行的命令及结果

以下 pytest 命令均带环境变量：
`PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env'`。

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
948 passed, 13 warnings in 124.96s (0:02:04)

$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
22 passed in 11.53s

$ .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider
83 passed, 2 skipped in 6.91s

$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_selection.py -q -p no:cacheprovider
38 passed in 19.91s

$ .venv/bin/ruff check nanobot/ scripts/
All checks passed!

$ .venv/bin/basedpyright -p /tmp/p7-d-pyrightconfig.json
0 errors, 0 warnings, 0 notes
```

`grep -n "db.execute" nanobot/channels/telegram/social/selection.py`：无输出，退出码 1，零命中。

`git diff --check`：无输出，退出码 0。

首轮四文件定向测试曾为 `3 failed, 106 passed in 23.33s`：两个新增回放测试的 turn ID 不匹配，以及新增 SQL 断言误匹配 `kind='jargon'`；均已修正。

3. 与简报的偏差及原因

无业务设计偏差。为满足缓存持续到 turn 结束，同时移除了 provider 重配时的 `cache.clear()`。

直接运行 basedpyright 未解析到虚拟环境依赖，报告 802 errors；最终使用 `/tmp/p7-d-pyrightconfig.json` 显式指定现有 `.venv`、仓库路径及项目相同的 strict 检查选项，未关闭诊断、未修改工程配置。

语义性测试修改均位于 `nanobot/channels/telegram/tests/test_social_selection.py`：

| 原行号 → 新行号 | 原预期 → 新预期及理由 |
|---|---|
| 77 → 79 | 消息编辑、新消息、条目 revision、停用、贴纸备注、配置变化后返回空 → 返回首次结果且 provider 仅调用一次；符合 turn 缓存语义。另新增 scope、provider 重配两个参数。 |
| 98 → 106 | 非 JSON、未知 ID、超额列表统一 error → 非 JSON 保留 error；未知 ID 改由 :332 验证跳过并保留有效项；重复 ID、超额列表分别验证去重与 limit 截断（:332、:378）。 |
| 146 → 154 | 调用期间停用条目后整次失败 → 该候选被跳过，`status=empty、dropped=1`；保留明确要求的 `current()` 检查。新增 :168 覆盖调用期间消息、scope、配置变化仍保留选择。 |

原 :195 → 新 :223 的端到端测试增加断言：迭代期间缓存存在，turn 结束后删除。没有删除测试函数。日志 :208、:425 的原始输出各形成一个参数化用例，均保留贴纸选择。

其余机械适配：

| 测试文件 | 装配调用数 | 新增导入数 |
|---|---:|---:|
| test_social_expressions.py | 2 | 1 |
| test_social_learning.py | 1 | 1 |
| test_social_judge.py | 1 | 2 |
| test_social_media.py | 1 | 2 |
| test_social_revisions.py | 1 | 2 |

新增 Library 方法：

- `ExpressionLibrary`：`candidates`、`row`、`invalidate_source`、`selection_entry`、`mark_usage`、`usable_count`。
- `JargonLibrary`：`candidates`、`row`、`invalidate_source`、`selection_entry`、`mark_usage`、`usable_entry`、`counts`。
- `StickerLibrary`：`candidates`、`row`。

`mark_usage` 承接原有 `last_selected/last_confirmed` 更新；候选 SQL 的参数、relevance/variety 排序及业务条件保持原样。

4. 未解决的问题 / 需要编排者决定的事项

无。未提交、推送或部署；未运行评估脚本、构建或实例启动。