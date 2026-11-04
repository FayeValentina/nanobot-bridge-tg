**1. 改动摘要（按文件）**

- `templates/telegram/social_protocol.md`：直接复制指定正文，逐字节一致。
- `social/bindings.py`：模块级加载模板；保留 `SOCIAL_PROTOCOL` 名称及 assembled 拼接格式。
- `social/context.py`：删除 `facts_note`、`expression_guidance`。
- `social/memory.py`：删除 `pending_note`、`note`。
- `social/selection.py`：删除近期用语 `note`；口头禅计数读取配置。
- `config/schema.py`：新增 `learning.catchphrases`，默认空列表，上限 20。
- `social/expressions.py`：过滤器接收配置词表；`accept()` 改为实例方法，读取现有 store。
- `social/jargon.py`：`eligible()` 改为实例方法，保留九个普通词，另排除配置口头禅。
- `templates/telegram/social_expression_learning.md`：仅替换指定句子。
- `test_social_runtime.py`：修改指定的 admission 文案断言。
- `test_social_selection.py`：两处测试设置原四词；新增空配置计数测试。
- `test_social_expressions.py`：角色词排除样例设置原四词。

未找到需要删除的五类指导字段断言，删除数量为 **0**；jargon 测试未发现依赖四词的样例，因此未修改。

行为变化：system 使用新 protocol；每次迭代的 live memory 不再含五段指导；口头禅统计和过滤由配置决定，默认不排除角色词。生产 Python 净减少 **174 行**。

**2. 实际运行的命令及结果（原样贴最后几行）**

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
FAILED nanobot/channels/telegram/tests/test_social_runtime.py::test_persona_source_is_available_in_scoped_state_tool[scope0-a]
FAILED nanobot/channels/telegram/tests/test_social_runtime.py::test_persona_source_is_available_in_scoped_state_tool[scope1-b]
2 failed, 947 passed, 13 warnings in 124.75s (0:02:04)
```

```text
$ .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider
........................................................................ [100%]
144 passed in 6.18s
```

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
......................                                                   [100%]
22 passed in 10.30s
```

```text
$ .venv/bin/ruff check nanobot/
All checks passed!
```

```text
$ .venv/bin/basedpyright nanobot/channels/telegram/social nanobot/config/schema.py
1138 errors, 0 warnings, 0 notes
```

原命令无法解析 `pydantic` 等虚拟环境依赖。仅在 `/tmp/p8-a-pyright.json` 中显式指定现有 `.venv`，保持 strict、Python 3.11 和测试排除设置：

```text
$ .venv/bin/basedpyright -p /tmp/p8-a-pyright.json nanobot/channels/telegram/social nanobot/config/schema.py
0 errors, 0 warnings, 0 notes
```

仅调整 PATH 的复查仍为 `1138 errors`；HEAD 临时副本的默认检查为 `1131 errors`。

以下命令均无输出：

```text
$ cmp nanobot/templates/telegram/social_protocol.md .agent/tasks/P8-a.social_protocol.md
$ rg -n '本大爷|白饭|算力|肥鱼|嘴硬摸鱼' nanobot --glob '!**/tests/**'
$ git diff --check
```

`rg` 退出码为 1，表示零命中。场景文件及冻结 V5 基线相关文件均未修改。

**3. 与简报的偏差及原因**

- 社交目录测试未全通过：`test_social_runtime.py:333` 还有一处简报未列出的旧 protocol 文案断言，导致两个参数化案例失败。遵守“只做指定测试改动”的限制，未修改它或模板。
- 默认 basedpyright 命令未通过；显式指定虚拟环境后为零错误。未修改项目配置处理环境问题。
- 未提交、推送、构建、部署或操作学习批次及游标。

**4. 未解决的问题 / 需要编排者决定的事项**

- 决定是否将 `test_social_runtime.py:333` 的旧断言：
  `social_get_state for the actual role_id and role_file`
  改为新模板现有句子：
  `social_get_state shows its real role_id and role_file`，随后重跑社交目录测试。
- 部署时需由编排者配置 `learning.catchphrases=["本大爷", "白饭", "Token", "算力"]`，恢复原角色词过滤行为。