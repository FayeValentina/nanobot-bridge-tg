# 任务 P1-c：从通用核心中移除已撤回的“隔离模式”残留，恢复上游写法

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode” 和 `.agent/orchestration.md`）。

## 背景

`docs/SOCIAL_ARCHITECTURE_REVIEW.md` P1-1 “M0–M3 隔离模式的残留”。owner 已在 2026-10-28 撤回群聊隔离（群友与普通助手共用工具权限，
见 `docs/SHARED_TOOL_PERMISSIONS.md`）。但为隔离加进通用核心的字段和分支还在，它们扩大了与上游 nanobot 的差异。

已核实的事实：
- 上游基线 `1bb712d3` 的 `SessionPolicy` 只有 `persist`、`log_content`、`disabled_tools`。
- 迁移新增字段中，`tool_only`、`persona`、`context_version` 仍被社交模块使用，**保留**。
  `allowed_tools`、`include_profile`、`include_memory`、`include_skills`、`archive_memory`、`allow_commands`、`enabled` 只由
  `ISOLATED_SESSION_POLICY` 设置为非默认值，而它只在会话 metadata 含 `_isolated_context=True` 时生效。
  社交 runtime 每次加载会话都会删除这个标记，线上会话文件中也没有这个标记。
- 没有任何工具把 `requires_explicit_allowlist` 设为 True。
- `RequestContext.allow_private_context` 只由 loop 根据 `include_profile and include_skills` 设置，删除后恒为 True。
- 可以用 `git show 1bb712d3:<path>` 查看上游原文。

## 设计（已定，不得改变）

目标：这些位置恢复为上游写法，只保留社交模块仍需要的 `tool_only`、`persona`、`context_version` 扩展点。

1. `nanobot/session/manager.py`
   - `SessionPolicy` 删除字段 `allowed_tools`、`include_profile`、`include_memory`、`include_skills`、`archive_memory`、`allow_commands`、`enabled`。
     保留 `persist`、`log_content`、`disabled_tools`、`tool_only`、`persona`、`context_version`。
   - 删除 `ISOLATED_CONTEXT_KEY`、`ISOLATED_SESSION_POLICY`，以及 `Session.__post_init__` 和 `SessionManager.resolve_policy` 中处理它们的分支。
2. `nanobot/agent/loop.py`
   - 删除 `if not policy.enabled: raise PermissionError(...)`。
   - 工具过滤块恢复为上游结构：只按 `policy.disabled_tools` 过滤（删除 `has_scoped_tools`、`allowed_tools`、`requires_explicit_allowlist` 相关条件）。
     `policy.context_version` 写入 `ctx.attributes` 的两行保留。
   - 删除两处 `allow_commands` 检查（`policy_for_key(key).allow_commands` 和 `session.policy.allow_commands`），恢复上游流程。
   - 构建上下文处：`include_memory=session.policy.persist if session is not None else True`（上游写法）；删除 `include_profile=`、`include_skills=` 两个参数。
     `trusted_persona=` 保留。
   - `build_current_message(..., include_skills=...)` 删除该参数。
   - 构造 `RequestContext` 时删除 `allow_private_context=...`；凡是读取 `request.allow_private_context` / `request_ctx.allow_private_context` 的地方，
     按“恒为 True”化简（条件分支只保留 True 的一侧，删除向下传递的同名参数）。
   - 传给 Consolidator 或 archiver 的 `resolve_policy=self.sessions.policy_for_key` 参数删除（见第 4 条）。
3. `nanobot/agent/tools/context.py`：删除 `RequestContext.allow_private_context` 字段。`nanobot/agent/tools/base.py`：删除 `requires_explicit_allowlist`。
4. `nanobot/agent/memory.py`
   - 删除 `resolve_policy` 构造参数、`_resolve_policy` 属性、`policy()`、`prompt_policy()`、`tool_definitions()` 方法。
   - 使用它们的地方恢复上游写法：`raw_archive(...)` 无条件调用；删除 `if not self.policy(session_key).enabled: return raw_fallback()`；
     summary 的条件恢复为 `if summary != "(nothing)":`；`**self.prompt_policy(...)` 删除；工具定义直接用 `self._get_tool_definitions()`
     （archiver 调用处同理，改为上游对应写法）。
5. `nanobot/agent/context.py`：删除 `include_profile`、`include_skills` 参数及其分支（`build_system_prompt`、`build_messages`、
   `build_current_message` 等所有经手的签名），恢复上游行为。`include_memory` 和 `trusted_persona` 保留。
6. `nanobot/agent/autocompact.py`：`if session.policy.context_version is not None or not session.policy.include_profile:` 改为
   `if session.policy.context_version is not None:`。
7. `nanobot/channels/telegram/social/runtime.py`：删除 `ISOLATED_CONTEXT_KEY` 的 import，`policy_for_session` 中的
   `and not session.metadata.get(ISOLATED_CONTEXT_KEY)` 条件，以及 `session.metadata.pop(ISOLATED_CONTEXT_KEY, None)` 和它上面两行注释。

## 测试的处理

- 专门测试隔离行为的测试（`ISOLATED_*`、`allowed_tools`、`include_profile=False`、`allow_commands=False`、`enabled=False`、
  `requires_explicit_allowlist`、`allow_private_context=False`、isolated 标记被清除等）：删除。
- 其他测试中顺带构造了这些字段的：去掉这些字段，断言不变。
- 逐条列出每个修改或删除的测试：文件、测试名、处理方式、原因。
- `test_social_scenarios.py` / `social_harness.py` 不应受影响；受影响就停下报告。

## 允许改动的路径

- `nanobot/session/manager.py`、`nanobot/agent/loop.py`、`nanobot/agent/context.py`、`nanobot/agent/memory.py`、`nanobot/agent/autocompact.py`、
  `nanobot/agent/tools/context.py`、`nanobot/agent/tools/base.py`、`nanobot/channels/telegram/social/runtime.py`
- `tests/` 和 `nanobot/channels/telegram/tests/` 下引用了被删内容的测试文件

## 防御性代码规则

- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、兼容层、迁移步骤、配置项。
- 不要为“万一还有 `_isolated_context` 标记的会话”加兼容处理，背景中已核实不存在。
- 不要改动与本任务无关的上游差异（例如 turn options、pending messages、incremental log）。
- 预期规模：生产代码净减少约 60–120 行。

## 完成标准

- `.venv/bin/python -m pytest tests/agent tests/session tests/tools tests/cli/test_gateway_runtime.py tests/test_tool_contextvars.py -q -p no:cacheprovider` 全部通过。
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过（1 个 xfail）。
- 全仓库搜索 `grep -rnE "allowed_tools|include_profile|include_skills|archive_memory|allow_commands|requires_explicit_allowlist|ISOLATED_|allow_private_context|prompt_policy" nanobot tests --include=*.py`
  无输出；`grep -rn "policy.enabled" nanobot --include=*.py` 无输出（与 SessionPolicy 无关的同名字段命中，请在报告里说明）。
- `.venv/bin/ruff check nanobot/ tests/` 通过；
  `.venv/bin/basedpyright nanobot/agent/loop.py nanobot/agent/context.py nanobot/agent/memory.py nanobot/session/manager.py nanobot/agent/autocompact.py nanobot/agent/tools/context.py nanobot/agent/tools/base.py nanobot/channels/telegram/social/runtime.py`
  与改动前相比不新增错误（先记录基线）。
- 报告给出 `git diff --stat`、生产代码净行数，以及 `git diff 1bb712d3 --stat -- nanobot/session/manager.py nanobot/agent/loop.py nanobot/agent/context.py nanobot/agent/memory.py nanobot/agent/autocompact.py`
  在改动前后的对比（与上游差异应缩小）。

## 报告格式（最终回复只包含以下内容）

1. 改动摘要（按文件，含净行数）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因；测试改动逐条列出
4. 未解决的问题 / 建议（不要自己实现）
