# 任务 P9-c：social SQLite schema 压缩为 v22 基线，删除 v1–21 逐步迁移

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P2-4；`.agent/tasks/P9-0.report.md` 第 5 节（必读）。
线上库为 v22；P9-0 已把线上 `sqlite_master` 的 DDL 导出为 `/tmp/p9/work/baseline-live-v22.sql`（193 行，39 表 + 16 索引），并验证据此重建的空库与线上库的 `sqlite_master`、列、外键、索引完全一致。
本机只有线上实例使用此代码；staging 与旧备份（v0–21）不再需要由新代码打开——它们应由当时的旧版本恢复/升级。最后一个仍含完整迁移链的版本是 `961ea247`。

## 设计（已定，不得改变）
`nanobot/channels/telegram/social/db/schema.py` 重写为：
- 模块 docstring 改为：`"""v22 baseline DDL for the Telegram social journal."""`
- `SCHEMA_VERSION = 22`（名字与值不变）。
- `BASELINE_DDL: str`：内容为 `/tmp/p9/work/baseline-live-v22.sql` 的语句，**逐字复制**（保留其中的原始空白与列顺序；不重排、不美化），按原文件顺序。
- `initialize_social_schema(store) -> None`：
  1. 保留现有的 `quick_check` 损坏检查（文案不变）与关闭连接后抛错的方式。
  2. 读取 `PRAGMA user_version` 为 `version`，以及 `has_tables = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone() is not None`。
  3. `version == SCHEMA_VERSION` → 直接返回。
  4. `version == 0 and not has_tables` → 在一个事务中执行 `BASELINE_DDL` 并设 `PRAGMA user_version=22`（用与现有代码相同的 executescript + BEGIN/COMMIT 方式）。
  5. 其余情况 → 关闭连接并 `raise ValueError(f"unsupported social schema version: {version}; open it with release 961ea247 or earlier to upgrade, or restore a v22 backup")`。
  （注意：P9-0 疑点 9——不能只凭 user_version=0 判断空库，所以 0 且已有表时也走第 5 步。）
- 删除全部 `_upgrade_*` helper、逐版本分支，以及因此不再使用的 import（json、re 等）。检查 quick_check 与 version 读取的先后顺序与现有代码一致（若现有代码先检查版本范围再 quick_check，保持“先 quick_check 后版本判断”是否改变了行为请在报告说明；以不改变 v22 库打开结果为准）。

## 要做的事
1. 按设计重写 schema.py。
2. 测试（`nanobot/channels/telegram/tests/`）：
   - 删除 P9-0 §5 列出的 17 个迁移测试函数，以及只被它们使用的 helper（例如 `test_social_scheduler.py` 的 `remove_v4_columns`、`remove_v4b_tables`；先 grep 确认无其他调用者）。逐个列出删除项。
   - `test_social_dialogue.py` 中 `test_schema_six_upgrade_preserves_data_and_unknown_versions_are_rejected` 的“未知版本拒绝”断言拆出为新测试保留（例如 user_version=23 被拒绝）。
   - 写死旧版本号或依赖旧迁移路径的其他断言：改为引用 `SCHEMA_VERSION` 或删除，逐条列出。
   - 新增测试（放在 `test_social_scheduler.py` 或最贴近 schema 的现有文件）：
     a. 新空库初始化后 `user_version == 22`，表与索引名集合与 `BASELINE_DDL` 中创建的对象一致；
     b. 已初始化的 v22 库再次打开不改变任何对象（比较前后 `sqlite_master`）；
     c. `user_version=21` 的库被拒绝且错误文案含 `961ea247`；
     d. `user_version=0` 但已有一张表的库被拒绝。
   - `test_upgrade_defaults_keep_ordinary_observation_zero_calls` 是运行行为测试，保留。
3. 线上库副本演练（只在 /tmp）：把 `/tmp/p9/social.sqlite3` 复制到 `/tmp/p9/work/p9c/social.sqlite3`，用新代码打开（构造 `SocialStore` 或直接调用 `initialize_social_schema`），确认成功且 `sqlite_master` 前后逐行相同、各表行数不变。贴出比较结果。
4. 场景测试 `test_social_scenarios.py` 不得修改。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不保留任何“从旧版本升级”的代码路径；不新增夹具文件。
- 预期规模：schema.py 从 587 行降到约 210 行（DDL 约 193 行）；测试净删除数百行。

## 允许改动的路径
- `nanobot/channels/telegram/social/db/schema.py`
- `nanobot/channels/telegram/tests/`（不含 evaluate_*、fixtures、test_social_scenarios.py）

## 不要做
- 不改任何表结构、不改其他 db 仓储、不碰线上库与备份。
- 不删 `backups/judge-history-schema21.json` 等实例数据（本任务也不应接触它们）。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/ruff check nanobot/`；`.venv/bin/basedpyright -p <你上次使用的显式 venv 配置> nanobot/channels/telegram/social` 0 errors。
- `grep -rn "_upgrade_\|user_version=2[01]\b" nanobot/channels/telegram --include=*.py` 在生产代码中无输出。
- 第 3 步演练结果。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件，列出删除的测试与 helper）
2. 实际运行的命令及结果（原样贴最后几行）
3. 线上库副本演练结果
4. 与简报的偏差及原因
5. 未解决的问题 / 需要编排者决定的事项
