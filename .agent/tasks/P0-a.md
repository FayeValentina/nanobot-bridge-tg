# 任务 P0-a：社交后台模型调用的失败必须可诊断

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode” 和 `.agent/orchestration.md`）。

## 背景

`docs/SOCIAL_ARCHITECTURE_REVIEW.md` P1-2 最后一条：社交模块的后台模型调用把异常吞掉，只留下
`invalid_or_changed_input` / `provider_exception` 这类错误码，没有日志也没有细节。线上有一个黑话学习批次
两次失败、状态保持 failed，至今无法知道原因。本任务只补**可诊断性**，不改变任何业务行为。

## 要做的事

1. `nanobot/channels/telegram/social/expressions.py::ExpressionLearner.run` 和
   `nanobot/channels/telegram/social/jargon_learning.py::JargonLearner.run`：
   - 每个 `except` 分支（TimeoutError、ValueError、Exception；CancelledError 除外）都要用 `loguru.logger` 记录，
     包括批次 id、kind 和错误码。`ValueError` 和 `Exception` 用 `logger.opt(exception=True).warning(...)` 保留堆栈；
     `TimeoutError` 用 warning，不需要堆栈。
   - 把错误细节写进 `learning_batches` 新增的 `error_detail TEXT` 列：格式为 `"<异常类名>: <消息>"`，截断到 300 字符。
     写入前去掉可能的凭证：复用 `nanobot/security/credentials.py` 里已有的脱敏函数。没有合适的函数时，
     对 `sk-`、`Bearer `、`api_key=`、`token=` 后面的值做简单替换，并在报告里说明。
   - 批次成功时把 `error_detail` 清为 NULL。
   - `jargon_learning.py` 中 `read_public` 的两处 `except Exception: continue`（搜索和抓取），改为 `logger.debug` 记录后继续，行为不变。
2. `nanobot/channels/telegram/social/selection.py::ContextSelector.select`：`except TimeoutError` 用 `logger.info` 记录；
   `except Exception` 用 `logger.opt(exception=True).warning` 记录。`configure_provider` 的 `except Exception` 加 warning 日志。
   不改变返回值和缓存语义。
3. `nanobot/channels/telegram/social/judge.py::SocialJudge.assess`：`except (ValidationError, ValueError)` 和
   `except Exception` 加 warning 日志（带批次 id 和错误码）。注意注释的要求：**不要把 provider 异常的原文写进数据库**。
   日志里只记异常类名和截断的消息，同样做脱敏。
4. schema 迁移：在 `nanobot/channels/telegram/social/schema.py` 把 `SCHEMA_VERSION` 从 17 升到 18，新增一步迁移：
   给 `learning_batches` 加 `error_detail TEXT` 列（先检查列是否已存在，参照 `learning.py::upgrade_jargon_schema` 的写法），
   然后设置 `PRAGMA user_version=18`。迁移代码放在 `schema.py`，**不要**再新建 `upgrade_*` 函数分散到其他文件。
5. 在 `get_state` 的 learning 状态里，如果 `SocialLearning.status` 返回最近批次信息，就把 `error_detail` 一并带出（截断到 120 字符）；
   如果不返回批次信息，则不改动，并在报告里说明。

## 允许改动的路径

- `nanobot/channels/telegram/social/expressions.py`
- `nanobot/channels/telegram/social/jargon_learning.py`
- `nanobot/channels/telegram/social/selection.py`
- `nanobot/channels/telegram/social/judge.py`
- `nanobot/channels/telegram/social/schema.py`
- `nanobot/channels/telegram/social/learning.py`（只限 status 输出）
- 测试：`nanobot/channels/telegram/tests/test_social_learning.py`、`test_social_jargon.py`、`test_social_selection.py`
  （只增加测试，不修改已有断言）

## 不要做

- 不改变任何重试、状态转换、游标、预算逻辑。failed 批次的游标处理保持原样（AGENTS.md 要求保留线上失败证据）。
- 不做 P1-1 等其他阶段的清理，即使看到了明显的死代码。
- 不重构 `run` 方法的结构，只在 except 分支里加记录。

## 完成标准

- 新增测试至少覆盖：(a) expression 和 jargon 的 run 中，provider 抛 `RuntimeError("boom sk-abc123")` 时，
  批次变为 failed，`error_detail` 以 `RuntimeError: boom` 开头且不含 `sk-abc123`；(b) 成功批次的 `error_detail` 为 NULL；
  (c) 从 v17 数据库升级到 v18 时，已有 `learning_batches` 行保留，新增的列为 NULL。
  测试 v17 升级可以先用当前 schema 建库，再 `ALTER TABLE ... DROP COLUMN error_detail` 并把 user_version 设回 17，然后重新打开。
- 以下命令必须通过：
  - `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_learning.py nanobot/channels/telegram/tests/test_social_jargon.py nanobot/channels/telegram/tests/test_social_selection.py nanobot/channels/telegram/tests/test_social_judge.py nanobot/channels/telegram/tests/test_social_expressions.py -q`
  - `.venv/bin/ruff check nanobot/channels/telegram/social/`
  - `.venv/bin/basedpyright nanobot/channels/telegram/social/expressions.py nanobot/channels/telegram/social/jargon_learning.py nanobot/channels/telegram/social/selection.py nanobot/channels/telegram/social/judge.py nanobot/channels/telegram/social/schema.py`
    （与改动前相比不能新增错误。先在改动前运行一次记下基线。）

## 报告格式（最终回复只包含以下内容）

1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行，包括 basedpyright 改动前后的错误数）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
