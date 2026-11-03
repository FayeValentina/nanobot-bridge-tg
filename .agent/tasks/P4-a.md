# 任务 P4-a：快照曝光改为按 view_id 显式识别【小幅行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-4：判断“模型是否看到了某份群聊快照”，目前是把整份快照 JSON（`views.body`）当子串，
去本轮全部消息文本里查找（`SocialStore.note_seen` 中 `row["body"] not in model_text`，以及 `ContextSelector.input` 中 `row[0] not in model_text`）。
这既慢（每份未曝光快照都要在整段 prompt 文本里做一次整串搜索），又脆弱（序列化细节一变就静默失效）。
事实来源：`.agent/tasks/P4-0.report.md`（第 1–4 节、第 6 节）。行号以当前代码为准。

本任务**不改变登记的时机**（仍在 provider 返回后，由 hook 的 `_note` 用 `context.model_messages` 登记），只改变“怎样认出一份快照”。

## 设计（已定，不得改变）

### 1. 快照首尾标记
- `models.Snapshot`（TypedDict）在**最后**增加字段 `view_end: str`。
- `SocialStore.view` 构造结果字典时，把 `"view_end": view_id` 写成字典字面量的**最后一个键**（`opportunity` 键已经在字面量中，后面对它的赋值不改变键顺序）。
  于是编码后的快照文本以 `{"view_id":"<id>",` 开头，以 `"view_end":"<id>"}` 结尾。`encode_view` 不变。
- “一份快照被完整看到”的定义：同一段文本中**同时**出现 `"view_id":"<id>"` 和 `"view_end":"<id>"`。只出现开头（例如被截断、被外置成前缀）不算。

### 2. 识别函数
在 `social/db/views.py` 增加模块级函数（不是 `Views` 的方法，不访问数据库）：
```python
_VIEW_ID = re.compile(r'"view_id":"([0-9a-f]{32})"')

def complete_view_ids(text: str) -> set[str]:
    """View ids whose opening and closing markers both appear in text."""
    return {view_id for view_id in _VIEW_ID.findall(text) if f'"view_end":"{view_id}"' in text}
```

### 3. 改用 view_id 的位置
- `Views.unseen(key, version)` 改为 `Views.unseen(key, version, view_ids: set[str]) -> list[sqlite3.Row]`：
  在原 WHERE 条件上增加 `id IN (...)`（ids 为空时直接返回 `[]`，不执行 SQL）。
- `SocialStore.note_seen(key, turn_id, model_text)` **签名不变**；内部先 `ids = complete_view_ids(model_text)`，再 `self.views.unseen(key, version, ids)`，
  循环中删除 `or row["body"] not in model_text` 这一条件（turn 条件 `row["turn_id"] not in (None, turn_id)` 保留）。其余逻辑不变。
- `Views.turn_bodies(key, turn)` 的 SELECT 增加 `id` 列（放在第一列：`SELECT id,body,turn_id ...`）。
- `ContextSelector.input(scope, turn, model_text="")`：`model_text` 非空时，条件改为 `row["id"] not in ids`（`ids = complete_view_ids(model_text)`，循环前算一次）；
  `model_text` 为空时的 turn 条件不变。随列序变化，把 `row[0]`/`row[1]` 改为 `row["body"]`/`row["turn_id"]`。
- `Views.displayed_bodies` 的 `ORDER BY created` 改为 `ORDER BY created,rowid`（P4-0 第 6 节：同一时间戳下顺序不确定）。

### 4. Q4：曝光收取的 wake 与 claim 的 scope 迁移一致
P4-0 第 6 节 Q4：`Wakes.claim` 在 wake 进入 running 时会把 scope 置为 active、清 `wake_at`/`wait_infinite`、删除 `dialogue_waits`；
而 `Wakes.admit_view_job` 和 `Wakes.admit_shown` 让 wake 进入 running 时不做这些。改为一致：
- 在 `Wakes` 中新增私有方法 `_enter_running(key)`，内容就是 `claim` 中 wake 迁移成功后的两条 SQL（`UPDATE scopes SET mode=?,wake_at=NULL,wait_infinite=0 WHERE key=?` 与
  `DELETE FROM dialogue_waits WHERE scope=?`），`claim` 改为调用它。
- `admit_view_job` 迁移成功（rowcount 为 1）时调用 `_enter_running(key)`；`admit_shown` 在本次调用中有任何 wake 被迁移为 running 时
  （以两处 `_move(RUNNING, ...)` 返回的 rowcount 之和大于 0 判断）调用一次 `_enter_running(key)`。

## 行为变化（只有以下几项，报告中逐条确认）
1. 模型看到的快照 JSON 末尾多一个 `view_end` 字段。
2. 只看到快照开头、没看到结尾的情况仍判为未看到（与现状相同）；完整快照的判定不再依赖整串逐字一致，只依赖首尾标记。
3. Q4：通过曝光收取的 wake 也会把 scope 置为 active 并清除旧的等待。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不改登记时机，不改 hook 的结构（hook 重构是 P4-b），不改 `memory.context`、`Views.latest_body` 的语义。
- 不改 schema、不改 `SCHEMA_VERSION`。
- 预期规模：生产代码净增不超过 25 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含 `db/`）
- `nanobot/channels/telegram/tests/`：测试中手写的快照 JSON 若缺少 `view_end` 而因此失败，可以补上该字段（机械性修改）；
  断言了旧行为的测试只在上面三项行为变化范围内修改。每处修改都在报告中列出（文件:行号、原内容、新内容、理由）。
- `scripts/evaluate_social_*.py`：仅在因 `view_end` 失败时做同样的机械修改。

## 不要做
- 不要改 `_SocialExposureHook` 的方法结构和调用时机。
- 不要处理 P4-0 第 6 节其余疑点（max-iterations 收尾请求、accepted_messages 中残留注入块等）；写进“建议”。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过（基线 982 passed）；`test_social_scenarios.py` 不得修改。
- 新增**一个**测试（放在 `test_social_runtime.py`，与现有 `test_unseen_truncated_snapshot_does_not_grant_read_ack` 相邻），覆盖：
  同一份快照 JSON 的键之间被插入空格（例如用 `json.dumps(view, ensure_ascii=False)` 默认分隔符重新编码）后作为 model_text，`note_seen` 仍然登记，随后 `mark_read` 成功。
- `.venv/bin/ruff check nanobot/ scripts/` 无错误；basedpyright 对 `nanobot/channels/telegram/social` 0 errors。
- 贴出 `grep -rn "not in model_text\|in model_text" nanobot/channels/telegram/social --include=*.py` 的输出（预期 0）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 三项行为变化各自的落点（文件:行号）
3. 实际运行的命令及结果（原样贴最后几行）
4. 修改过的测试（逐条）
5. 与简报的偏差及原因
6. 未解决的问题 / 建议
