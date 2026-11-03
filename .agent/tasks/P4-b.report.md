1. 改动摘要（按文件）

- `social/context.py`：新增 `transcript_text` 和 `SocialContextBuilder`，原样搬迁内容构建代码；直接源码对照确认文案、序列化及 blocks 构建一致。
- `social/runtime.py`：挂载 builder；hook 两处共用文本提取函数，保留注入、恢复、曝光登记逻辑；删除 `_last_memory_body`。
- runtime 减少 52 行，新模块 71 行，生产代码净增 19 行。未提交、推送或部署。

2. 实际运行的命令及结果（原样贴最后几行）

```bash
PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env' \
.venv/bin/python -m pytest nanobot/channels/telegram/tests \
tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py \
-q -p no:cacheprovider --ignore-glob="*evaluate_*"
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
983 passed, 13 warnings in 153.77s (0:02:33)
```

```bash
.venv/bin/ruff check nanobot/
```

```text
All checks passed!
```

```bash
.venv/bin/basedpyright nanobot/channels/telegram/social
```

默认命令无法解析现有虚拟环境依赖：

```text
971 errors, 0 warnings, 0 notes
```

使用 `/tmp/p4b-pyright.json` 继承项目配置，并补充仓库及 `.venv/lib/python3.11/site-packages` 到 `extraPaths`：

```bash
.venv/bin/basedpyright --project /tmp/p4b-pyright.json nanobot/channels/telegram/social
```

```text
0 errors, 0 warnings, 0 notes
```

```bash
grep -rn "_last_memory_body" nanobot scripts --include=*.py
```

无输出，退出码 1，0 匹配。

```bash
grep -n "selector\|memory.context\|activities\|running_activity" \
nanobot/channels/telegram/social/runtime.py | awk -F: '$1 >= 823'
```

```text
833:    def _restore_memory_context(self) -> None:
840:        self._restore_memory_context()
897:        self._restore_memory_context()
924:        self._restore_memory_context()
```

句点转义后复查：

```bash
grep -n 'selector\|memory\.context\|activities\|running_activity' \
nanobot/channels/telegram/social/runtime.py | awk -F: '$1 >= 823'
```

无输出，hook 范围内 0 匹配。

`git diff --check` 无输出，退出码 0。

3. 修改过的测试（逐条）

无。`test_social_scenarios.py` 未修改，已随指定测试运行。

4. 与简报的偏差及原因

- 按明确设计执行“先完整 build，再找 target”。但旧代码实际在找到 target 后才调用 `running_activity`；因此无 target 时，新代码会多执行该查询。这是简报事实描述与原代码的差异。
- 原 grep 的 `memory.context` 使用正则通配句点，会匹配 `_restore_memory_context`；保留该方法，另用转义句点确认查询已移出 hook。
- basedpyright 使用临时依赖路径配置完成检查，未修改项目配置或关闭诊断。

5. 未解决的问题 / 建议

建议编排者在正常依赖发现环境复跑默认 basedpyright 命令。P4-0 第 6 节疑点及评估脚本保持未处理。