# 任务 P3-a：建立 `db/` 包，搬迁 scopes / messages / views / stickers 的 SQL（纯搬迁，行为不变）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-2：`SocialStore`（`nanobot/channels/telegram/social/store.py`）是 god object，
裸 SQL 散落在各模块。Phase 3 按“表归属”把 SQL 搬进 `social/db/` 包，分三个任务：
- **P3-a（本任务）**：建包；搬 schema；建立 `Scopes`、`Messages`、`Views`、`StickerLibrary` 四个仓储，
  把**全仓库**对这几张表的 SQL 搬进去。
- P3-b：wake 状态机（wakes、requests、dialogue_*、scopes 的调度列）。
- P3-c：judge、投递日志、知识/学习表的单一归属，以及 SocialStore 收尾。

本任务是**纯搬迁**：SQL 文本、参数、执行顺序、事务边界（`with self.db:` 的位置）都保持原样，不修任何 bug，不改任何行为。
事实来源：`.agent/tasks/P3-0.report.md`（只读分析的完整清单，行号对应当前代码）。

## 设计（已定，不得改变）

### 目录
```
nanobot/channels/telegram/social/db/
  __init__.py      # 空文件
  connection.py    # open_social_db
  schema.py        # 由 social/schema.py 用 git mv 搬来，内容只改 import
  scopes.py        # class Scopes
  messages.py      # class Messages
  views.py         # class Views
  stickers.py      # class StickerLibrary
```
删除 `social/schema.py`（git mv 之后原路径不存在）。不要留兼容 re-export。

### 表与列的归属（本任务范围）
| 仓储 | 拥有的表 / 列 |
|---|---|
| `Scopes` | `scopes` 表的身份列（key/channel/chat_id/thread_id/version）、`next_seq`、`event_seq`、`last_member_at`、`read_through`、`seen_through`；`runtime_settings` 表 |
| `Messages` | `messages` 表（全部列，包括学习模块写的 `learning_source`/`learning_text`）、`updates` 表 |
| `Views` | `views` 表 |
| `StickerLibrary` | `stickers` 表 |

`scopes` 的调度列（mode、wake_at、wake_on_timeout、wait_after_seq、wait_infinite、wake_config、timeout_through）归 P3-b 的 wakes，
**本任务不搬**这些列的 UPDATE（`state()` 读整行是允许的）。

### 构造与挂载
- 每个仓储是普通类，构造参数为 `(db: sqlite3.Connection, now: Callable[[], float])`；`Views` 额外接收 `scopes: Scopes`
  （用于读 version）。不继承任何基类，不新建基类/协议/工厂。
- `SocialStore.__init__` 中：`self.db = open_social_db(root)`，然后 `initialize_social_schema(self)`，再创建
  `self.scopes = Scopes(self.db, now)`、`self.messages = Messages(self.db, now)`、`self.views = Views(self.db, now, self.scopes)`、
  `self.stickers = StickerLibrary(self.db, now)`，其余（activities/learning/followups/closed/chmod）保持原顺序。
- `open_social_db(root: Path) -> sqlite3.Connection`：原 `SocialStore.__init__` 中的 mkdir、symlink 检查、connect、row_factory、
  三条 PRAGMA，原样搬入。`path.chmod(0o600)` 仍留在 `SocialStore.__init__` 末尾（保持原顺序）。

### 方法去向（store.py 内）
| 原 `SocialStore` 方法 | 去向 | 说明 |
|---|---|---|
| `setting_revision` | `Scopes.setting_revision` | 原样 |
| `owns` / `state` | `Scopes.owns` / `Scopes.state` | 原样 |
| `scopes()` | `Scopes.all()` | 原样 |
| `has_message` / `media_for_message` | `Messages.has_message` / `Messages.media_for_message` | 原样 |
| `collect_sticker` / `sticker` / `import_stickers` / `sticker_catalog` / `annotate_stickers` / `list_stickers` | `StickerLibrary.collect` / `get` / `import_set` / `catalog` / `annotate` / `search` | 原样 |
| `_shown_sequences` | `Views.shown_sequences(key, turn_id)`（公开） | 原样 |
| `bind` | 留在 SocialStore | 其中 `INSERT INTO scopes ... ON CONFLICT(key) DO UPDATE SET version` 改为调用 `Scopes.upsert(scope, version)`；其余 SQL 不动（P3-b/c 处理） |
| `observe` | 留在 SocialStore（跨表编排） | messages/updates 的 SQL 改为调用 `Messages` 方法；`next_seq`/`event_seq`/`last_member_at` 三条 UPDATE 改为一个 `Scopes.allocate_seq(key, *, event: bool, member_at: float \| None) -> int`（内部依次执行原来的读 next_seq 和三条 UPDATE，条件判断由调用方算好传入：`event = not msg.is_self and not msg.is_bot`；`member_at = msg.timestamp if event and not msg.is_edit and 0 <= now - msg.timestamp < 120 else None`）。dialogue_state/dialogue_waits 的 SQL 不动 |
| `view` | 留在 SocialStore | messages 读取改为 `Messages` 方法；views 的 INSERT 和两条 DELETE 改为 `Views.record(view_id, key, version, body, seqs, turn_id, created)`；其余（wakes/judge_batches/dialogue_jobs/actions）不动 |
| `note_seen` | 留在 SocialStore | views 的 SELECT 改为 `Views.unseen(key, version)`；`UPDATE views SET seen=1` 改为 `Views.mark_seen(view_id, turn_id)`；`UPDATE scopes SET seen_through` 改为 `Scopes.set_seen_through(key, frontier)`；wakes 和 dialogue_state 的 SQL 不动 |
| `mark_read` / `acknowledge_seen` | 留在 SocialStore | `UPDATE scopes SET read_through` 改为 `Scopes.set_read_through(key, through)` |
| `_judge_current`、`claim`、`ready`、`_check_trigger_source`、`consume_dialogue`、`wait_for_quiet` 等中对 messages 的读取 | 改为调用 `Messages` 方法 | 这些方法本身留在原处 |
| `delivery` 中 `UPDATE stickers SET uses=uses+1` | `StickerLibrary.count_use(scope, sticker_id)` | |

### store.py 之外的 SQL（全仓库）
以下位置对本任务四个仓储所拥有的表执行 SQL，全部改为调用对应仓储的方法（P3-0 报告第 4 节有完整清单，以当前代码为准逐一核对）：
- `judge_runtime.py`：messages 的读取（`_message`、`_restore`、`_snapshot`、`_run` 中 `remaining` 查询）。
- `learning.py`：`observe` 中 `UPDATE messages SET learning_source...`（→ `Messages.set_learning_source`）、邻居查询、`finish` 中的 revision/learning 查询；`record_selection` 中 stickers 存在性查询（→ `StickerLibrary.exists`）。
- `jargon.py`：`sources` 中的 messages 查询（与 learning `finish` 的查询相同 → 共用 `Messages.learning_text(key, message_id)`）。
- `memory.py`：`displayed_evidence` 与 `context` 中的 views 查询、`context` 中最近 4 条成员消息查询。
- `proactive.py`：`sync` 中 `SELECT value FROM runtime_settings`（→ `Scopes.setting_value(name) -> str | None`）、`_participation`、`valid`、`tick` 中 messages 与 scopes（last_member_at）的读取。
- `selection.py`：`input` 中的 views 查询和 messages⋈stickers 便签查询（→ `Messages.sticker_notes(key, message_id, revision)`）、`messages_current`。
- `nanobot/channels/telegram/runtime.py`、`nanobot/agent/tools/telegram_social.py`、`social/runtime.py`、`social/stickers.py` 等对上表方法的调用点：随方法去向改名。

**JOIN 查询的归属规则**：一条 SELECT 涉及多张表时，放到 FROM 主表所属的仓储里（例如 `proactive._participation` 的
`FROM messages m LEFT JOIN actions ... LEFT JOIN message_deliveries ...` 放进 `Messages`）。SQL 文本原样保留。

**本任务不搬**：`selection.py` 中 `candidates`/`current` 的动态多表查询（`FROM {table}`，涉及 expressions/jargon/stickers，P3-c 处理）；
任何 wakes、requests、dialogue_*、judge_*、actions、send_attempts、message_deliveries、proactive_state、记忆/黑话/学习/活动表的 SQL。

### 新方法的命名
上表已命名的按上表。其余“把一条读取 SQL 搬成方法”的情况：一条不同的 SQL 对应一个方法，方法名描述返回内容
（例如 `Messages.recent_member_texts(key, limit)`、`Messages.first(key)`、`Views.displayed_bodies(key, turn_id)`），
文本完全相同的 SQL 只建一个方法。返回值保持原代码使用的类型（`sqlite3.Row`、`list[sqlite3.Row]`、标量），不要新建 dataclass/TypedDict。
报告里列出你新建的每个方法及它替代了哪些位置。

## 要做的事
1. `git mv nanobot/channels/telegram/social/schema.py nanobot/channels/telegram/social/db/schema.py`，修正它和所有引用它的 import（含测试）。
2. 建 `db/__init__.py`、`connection.py`、`scopes.py`、`messages.py`、`views.py`、`stickers.py`，按上面的设计搬入 SQL。
3. 修改 `store.py`，删除已搬走的方法，改为调用仓储。
4. 修改 store.py 之外的生产代码，按“store.py 之外的 SQL”一节处理。
5. 全仓库更新调用点：`store.state(` → `store.scopes.state(`，`store.owns(` → `store.scopes.owns(`，`store.scopes()` → `store.scopes.all()`，
   以及其他被搬走方法的调用（生产代码、测试、`scripts/` 下的评估脚本、`nanobot/channels/telegram/tests/evaluate_*`）。
   用 `grep -rn` 对每个被删除的方法名做全仓库搜索，确认 0 处残留。
6. 测试中的修改只允许机械性改名/改 import；`test_social_memory.py:185` 在构造后重新赋值 `social.store.now`，
   仓储构造时已捕获原时钟，因此改为在构造 runtime 时传入 `now=lambda: clock[0]`：该测试用的是 `social` fixture，
   请在这个测试函数里按 fixture 的同样方式自行构造 runtime 并传入时钟（不要修改 fixture 本身，不要新建 fixture）。
   测试里直接执行的 `store.db.execute(...)` 不需要改。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 搬迁时不得“顺手修正”任何 SQL 或逻辑，哪怕你认为有 bug（例如 P3-0 报告第 7 节列出的疑点）；写进报告的“建议”。
- 不得新增 docstring 以外的注释段落；原有注释随代码搬走。
- 预期规模：store.py 减少约 200 行；新增 db/ 文件合计约 300–400 行；生产代码净增不超过 150 行。明显超出请在报告中解释。

## 允许改动的路径
- `nanobot/channels/telegram/social/`（含新建 `db/`）
- `nanobot/channels/telegram/runtime.py`
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/channels/telegram/tests/`（仅机械性改名/改 import，以及第 6 条所述）
- `tests/agent/test_telegram_social_contracts.py`（仅机械性改名，如需要）
- `scripts/evaluate_social_*.py`（仅机械性改名）

## 不要做
- 不要搬 wakes/requests/dialogue_*/judge_*/actions/send_attempts/message_deliveries/proactive_state 以及知识/学习表的 SQL。
- 不要引入 `transaction()` 辅助函数或改变任何 `with self.db:` 的位置（P3-b 处理）。
- 不要修改 schema 内容或 `SCHEMA_VERSION`。
- 不要给 `SocialStore` 留转发方法（例如 `def state(self, key): return self.scopes.state(key)`）。
- 不要提交、推送、部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过（当前基线 971 passed）。
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider` 通过。
- `.venv/bin/ruff check nanobot/ scripts/` 无错误。
- `.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py nanobot/agent/tools/telegram_social.py` 0 errors。
- 以下搜索的输出贴进报告：
  - `grep -rnE "FROM (scopes|messages|updates|views|stickers|runtime_settings)\b|(INTO|UPDATE) (scopes|messages|updates|views|stickers|runtime_settings)\b" nanobot/channels/telegram/social --include=*.py | grep -v "/db/"`
    （预期只剩：store.py 中对 scopes 调度列的 UPDATE、`next_due` 等 P3-b/c 范围内的位置；逐条说明为何保留）
  - 每个被删除的 `SocialStore` 方法名的全仓库 grep 结果（预期 0）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 新建方法清单（仓储.方法 → 替代的原位置）
3. 实际运行的命令及结果（原样贴最后几行）
4. 修改过的测试（逐条：文件:行号，改了什么）
5. 与简报的偏差及原因
6. 未解决的问题 / 建议
