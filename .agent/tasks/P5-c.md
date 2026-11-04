# 任务 P5-c：Telegram channel 只保留一个 social 观察入口（P2-2）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P2-2：`nanobot/channels/telegram/runtime.py` 里 `_observe_social` 有 6 个调用点。真正的入口是 group=-1 的
`_on_social_update`（处理成功即 `raise ApplicationHandlerStop`，阻止后续 handler）。其余 5 处是在普通 handler 开头再拦一次。
P5-0（`.agent/tasks/P5-0.report.md` 第 5 节）已核实：在 Application 正常分发下，这 5 处都被 group=-1 覆盖——
`/start`、`/help` 在 `_observe_social` 内被豁免（两处调用永远返回 False）；群内编辑消息由 group=-1 处理（PTB 的 GROUPS 过滤器接受 edited_message）；
已绑定群的新消息和命令在 group=-1 处理后 Stop，到不了 `_process_message_update`/`_process_forward_command`。
唯一的理论差异：某个群在消息排队的约 0.2 秒窗口内刚被启用绑定。编排者判断这一差异可以接受。P5-a/P5-b（`7b9fe9d2`、`b7f8bf3f`）已完成，channel 中已没有 `social.store`。

## 要做的事
删除以下 5 处调用（只删 `if await self._observe_social(update): return` 这类拦截语句本身，函数其余部分不动）：
1. `_on_start` 开头；
2. `_on_help` 开头；
3. `_process_forward_command` 开头；
4. `_on_message` 开头的 `if self.social is not None and update.edited_message is not None and await self._observe_social(update): return`；
5. `_process_message_update` 开头。
保留 `_on_social_update`（group=-1）及 `_observe_social` 本身。

## 防御性代码规则
- 不新增任何检查、分支或注释段落；不改 `_observe_social` 的逻辑、豁免规则和 handler 注册。
- 预期规模：生产代码净减少约 10 行。

## 允许改动的路径
- `nanobot/channels/telegram/runtime.py`
- `nanobot/channels/telegram/tests/`：如果有测试**绕过 Application、直接调用**上述 5 个 handler 来驱动 social 观察而因此失败，改为调用 `_on_social_update`（真实入口），
  或在测试中按真实分发顺序先调用 `_on_social_update`。逐条列出（文件:行号、原调用、新调用）。不得修改断言的含义。

## 不要做
- 不要改 social 包、不要改 handler 的注册顺序或 group、不要改 allowed_updates。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py tests/channels -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（如 `tests/channels` 中有与本改动无关、改动前就失败的用例，先在改动前运行一次记录基线，报告中对比）；`test_social_scenarios.py` 不得修改。
- `.venv/bin/ruff check nanobot/` 无错误；basedpyright 对 `nanobot/channels/telegram/runtime.py` 不新增错误（报告改动前后数量）。
- 贴出 `grep -n "_observe_social" nanobot/channels/telegram/runtime.py`（预期 2 处：定义与 `_on_social_update` 中的调用）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要
2. 实际运行的命令及结果（原样贴最后几行；含改动前后基线对比）
3. 修改过的测试（逐条）
4. 与简报的偏差及原因
5. 未解决的问题 / 建议
