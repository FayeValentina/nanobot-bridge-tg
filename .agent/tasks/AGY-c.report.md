1. 改动摘要（按文件）

- `antigravity_provider.py`：显式 effort 优先；`none/minimal` 映射为 `low`，未指定时保留模型名档位。
- `gemini_wire.py`：指定思考档位时增加 `includeThoughts: True`。
- `social/models.py`：末尾新增 `strip_json_fence`。
- `social/selection.py`：Choice 解析前去除代码块。
- `social/jargon_learning.py`：JargonDiscovery、ResearchResult 解析前去除代码块。
- `social/expressions.py`：ExtractionResult 解析前去除代码块。
- `social/judge.py`：JudgeVerdict 解析前去除代码块。
- `test_antigravity_provider.py`：补充档位映射测试；机械更新两项覆盖规则预期及流式请求预期。
- `test_gemini_wire.py`：更新 thinkingConfig 预期。
- `test_social_json_fence.py`（新增）：覆盖代码块、无包裹及首尾空白，共五例。
- `test_social_jargon.py`：参数化现有集成测试，覆盖发现、研究两阶段返回代码块 JSON。

生产代码净增 16 行。

2. 实际运行的命令及结果

pytest 继承环境：`PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`，使用既有沙箱插件。

```text
.venv/bin/python -m pytest tests/providers/test_antigravity_provider.py tests/providers/test_antigravity_auth.py tests/providers/test_gemini_wire.py -q -p no:cacheprovider

.....                                                                    [100%]
77 passed in 0.94s
```

```text
.venv/bin/ruff check nanobot/providers/antigravity_provider.py nanobot/providers/gemini_wire.py nanobot/channels/telegram/social/models.py nanobot/channels/telegram/social/selection.py nanobot/channels/telegram/social/jargon_learning.py nanobot/channels/telegram/social/expressions.py nanobot/channels/telegram/social/judge.py tests/providers/test_antigravity_provider.py tests/providers/test_gemini_wire.py nanobot/channels/telegram/tests/test_social_json_fence.py nanobot/channels/telegram/tests/test_social_jargon.py

All checks passed!
```

首次 ruff 报一项 I001，已修正 `judge.py` 的导入格式并复跑通过。

```text
.venv/bin/basedpyright nanobot/providers/antigravity_provider.py nanobot/providers/gemini_wire.py nanobot/channels/telegram/social/models.py nanobot/channels/telegram/social/selection.py nanobot/channels/telegram/social/jargon_learning.py nanobot/channels/telegram/social/expressions.py nanobot/channels/telegram/social/judge.py tests/providers/test_antigravity_provider.py tests/providers/test_gemini_wire.py nanobot/channels/telegram/tests/test_social_json_fence.py nanobot/channels/telegram/tests/test_social_jargon.py

0 errors, 0 warnings, 0 notes
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
950 passed, 13 warnings in 79.63s (0:01:19)
```

`git diff --check`：退出码 0，无输出。

3. 与简报的偏差及原因

实现无偏差，改动路径均在允许范围内；未提交、部署或发起真实网络请求。

查找 AGENTS.md 时误执行 `find .. -name AGENTS.md -print`，枚举了仓库外部署目录中的文件路径；未读取文件内容、未修改工程外数据，后续搜索已限定在仓库内。

4. 未解决的问题 / 需要编排者决定的事项

无。指定检查全部通过。