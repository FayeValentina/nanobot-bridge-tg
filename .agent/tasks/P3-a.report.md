**1. 改动摘要（按文件）**

- `social/db/__init__.py`：空文件。
- `social/db/connection.py`：迁入数据库目录创建、symlink 检查、连接、row_factory 和三条 PRAGMA。
- `social/db/schema.py`：搬迁原 schema，内容完全一致，`SCHEMA_VERSION` 未变；旧路径已删除，无兼容导出。
- `social/db/scopes.py`、`messages.py`、`views.py`、`stickers.py`：建立四个指定仓储。
- `social/store.py`：挂载仓储，删除迁出的方法，保留跨表编排、事务位置和末尾 chmod。由 1213 行减至 1036 行。
- `social/{judge_runtime,learning,jargon,memory,proactive,selection}.py`：迁出指定表的 SQL。
- `social/{bindings,followups,jargon_learning,judge,runtime,stickers}.py`：机械更新仓储调用。
- Telegram 测试、测试辅助模块及评估脚本：更新调用与 schema import；时钟测试按简报自行构造 runtime。
- 新增数据库代码 358 行，不含原样搬迁的 403 行 schema；生产代码净增 150 行。未提交、推送或部署。

**2. 新建方法清单**

以下原位置均指搬迁前代码。

| 仓储.方法 | 替代的原位置 |
|---|---|
| `Scopes.setting_revision` | `SocialStore.setting_revision` |
| `Scopes.owns` | `SocialStore.owns` |
| `Scopes.all` | `SocialStore.scopes` |
| `Scopes.state` | `SocialStore.state` |
| `Scopes.upsert` | `SocialStore.bind` 的 scopes UPSERT |
| `Scopes.allocate_seq` | `SocialStore.observe` 的 next_seq 读取及三条 UPDATE |
| `Scopes.set_seen_through` | `SocialStore.note_seen` |
| `Scopes.set_read_through` | `SocialStore.mark_read`，包括 acknowledge_seen 的间接调用 |
| `Scopes.setting_value` | `SocialProactive.sync` |
| `Scopes.last_member_at` | `SocialProactive.valid`、`tick` |
| `Scopes.latest_member_scope` | `SocialProactive.tick` |
| `Messages.has_message` | `SocialStore.has_message` |
| `Messages.media_for_message` | `SocialStore.media_for_message` |
| `Messages.displayed_revisions` | `SocialStore._judge_current` |
| `Messages.sequences_between` | `SocialStore._judge_current` |
| `Messages.observation` | `SocialStore.observe` 的既有消息查询 |
| `Messages.update_seen` | `SocialStore.observe` 的 updates 查询 |
| `Messages.record_update` | `SocialStore.observe` 的 updates INSERT |
| `Messages.edit` | `SocialStore.observe` 的消息编辑 UPDATE |
| `Messages.insert` | `SocialStore.observe` 的消息 INSERT |
| `Messages.set_judge_facts` | `SocialStore.observe` |
| `Messages.set_revision` | `SocialStore.observe` |
| `Messages.prune` | `SocialStore.observe` 的 messages DELETE |
| `Messages.prune_updates` | `SocialStore.observe` 的 updates DELETE |
| `Messages.after` | `SocialStore.view` 的未读查询 |
| `Messages.recent` | `SocialStore.view` 的最近消息查询 |
| `Messages.by_seq` | `SocialStore.view`、`JudgeCoordinator._restore` |
| `Messages.first_seq` | `SocialStore.view` 的 MIN(seq) 查询 |
| `Messages.retains_seq` | `SocialStore.claim`、`ready` |
| `Messages.judge_source` | `SocialStore._check_trigger_source` |
| `Messages.set_learning_source` | `SocialLearning.observe` |
| `Messages.learning_neighbors` | `SocialLearning.observe` |
| `Messages.learning_text` | `SocialLearning.finish`、`JargonLibrary.sources` 的相同查询 |
| `Messages.by_message_id` | `JudgeCoordinator._message` |
| `Messages.range` | `JudgeCoordinator._snapshot` |
| `Messages.history_through` | `JudgeCoordinator._snapshot` |
| `Messages.self_after` | `JudgeCoordinator._snapshot` |
| `Messages.eligible_after` | `JudgeCoordinator._run` 的 remaining 查询 |
| `Messages.participation` | `SocialProactive._participation` 的 JOIN 查询 |
| `Messages.first` | `SocialProactive._participation` |
| `Messages.timestamp` | `SocialProactive.valid` |
| `Messages.recent_member_texts` | `SocialMemory.context` |
| `Messages.sticker_notes` | `ContextSelector.input` 的 messages⋈stickers 查询 |
| `Messages.current_revision` | `ContextSelector.messages_current` |
| `Views.shown_sequences` | `SocialStore._shown_sequences` |
| `Views.record` | `SocialStore.view` 的 INSERT 和两条 DELETE |
| `Views.unseen` | `SocialStore.note_seen` |
| `Views.mark_seen` | `SocialStore.note_seen` |
| `Views.displayed_bodies` | `memory.displayed_evidence` |
| `Views.latest_body` | `SocialMemory.context` |
| `Views.turn_bodies` | `ContextSelector.input` |
| `StickerLibrary.collect` | `SocialStore.collect_sticker` |
| `StickerLibrary.get` | `SocialStore.sticker` |
| `StickerLibrary.import_set` | `SocialStore.import_stickers` |
| `StickerLibrary.catalog` | `SocialStore.sticker_catalog` |
| `StickerLibrary.annotate` | `SocialStore.annotate_stickers` |
| `StickerLibrary.search` | `SocialStore.list_stickers` |
| `StickerLibrary.count_use` | `SocialStore.delivery` |
| `StickerLibrary.exists` | `SocialLearning.record_selection`、collect 中的相同查询 |

四个仓储的 `__init__` 保存指定的连接与时钟，`Views.__init__` 额外保存 Scopes。`open_social_db` 替代原 `SocialStore.__init__` 中的连接初始化代码。

**3. 实际运行的命令及结果**

全目录测试使用编排文档已有的沙箱插件：

```bash
PYTHONPATH=.agent PYTEST_ADDOPTS="-p codex_pytest_env" \
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q \
-p no:cacheprovider --ignore-glob="*evaluate_*"
```

最后几行：

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
947 passed, 13 warnings in 145.90s (0:02:25)
```

场景、工具契约及网关测试：

```bash
PYTHONPATH=.agent PYTEST_ADDOPTS="-p codex_pytest_env" \
.venv/bin/python -m pytest \
nanobot/channels/telegram/tests/test_social_scenarios.py \
tests/agent/test_telegram_social_contracts.py \
tests/cli/test_gateway_runtime.py -q -p no:cacheprovider
```

```text
.......................................................                  [100%]
55 passed in 11.35s
```

中间发现 Mock/monkeypatch 仍引用旧属性，机械修正后单独运行：

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_bindings.py \
-q -p no:cacheprovider
```

```text
16 passed in 2.68s
```

Lint：

```bash
.venv/bin/ruff check nanobot/ scripts/
```

```text
All checks passed!
```

类型检查原命令：

```bash
.venv/bin/basedpyright nanobot/channels/telegram/social \
nanobot/channels/telegram/runtime.py nanobot/agent/tools/telegram_social.py
```

沙箱未自动发现虚拟环境依赖，首次结果：

```text
1902 errors, 0 warnings, 0 notes
```

使用 `/tmp/p3-pyright.json` 补充依赖搜索路径，未关闭任何诊断：

```json
{
  "extends": "/home/ubuntu/nanobot-bridge-tg/pyproject.toml",
  "extraPaths": [
    "/home/ubuntu/nanobot-bridge-tg",
    "/home/ubuntu/nanobot-bridge-tg/.venv/lib/python3.11/site-packages"
  ]
}
```

```bash
.venv/bin/basedpyright --project /tmp/p3-pyright.json \
nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py \
nanobot/agent/tools/telegram_social.py
```

```text
0 errors, 0 warnings, 0 notes
```

`git diff --check` 无输出。另直接比较搬迁前后 SQL 字面值集合及 schema 内容，结果：

```text
SQL added: set()
SQL missing: set()
Schema identical: True
Production net lines: 150
New db lines excluding moved schema: 358
Store original lines: 1213
Store final lines: 1036
```

指定 SQL 搜索：

```bash
grep -rnE "FROM (scopes|messages|updates|views|stickers|runtime_settings)\b|(INTO|UPDATE) (scopes|messages|updates|views|stickers|runtime_settings)\b" nanobot/channels/telegram/social --include=*.py | grep -v "/db/"
```

原样输出：

```text
nanobot/channels/telegram/social/store.py:73:                "UPDATE scopes SET mode='observing',wake_at=NULL,wake_config='{}',wait_infinite=0 WHERE key=?",
nanobot/channels/telegram/social/store.py:80:            self.db.execute("INSERT INTO judge_state(scope,assessed_through) SELECT key,next_seq-1 FROM scopes WHERE key=? "
nanobot/channels/telegram/social/store.py:237:                            "WHERE status IN ('pending','running') AND (created>? OR version<>(SELECT version FROM scopes WHERE key=scope) "
nanobot/channels/telegram/social/store.py:238:                            "OR (SELECT mode FROM scopes WHERE key=scope)='paused')", (now,))
nanobot/channels/telegram/social/store.py:284:                    self.db.execute("UPDATE scopes SET wake_config='{}' WHERE key=?", (key,))
nanobot/channels/telegram/social/store.py:509:            self.db.execute("UPDATE scopes SET mode='active',wake_at=NULL,wait_infinite=0 WHERE key=?", (key,))
nanobot/channels/telegram/social/store.py:521:            self.db.execute("UPDATE scopes SET mode='observing' WHERE mode='active' AND key IN "
nanobot/channels/telegram/social/store.py:542:                            "(SELECT key FROM scopes WHERE mode IN ('waiting','sleeping'))")
nanobot/channels/telegram/social/store.py:579:            self.db.execute("UPDATE scopes SET mode='observing' WHERE mode='active'")
nanobot/channels/telegram/social/store.py:660:            self.db.execute("UPDATE scopes SET mode='paused',wake_at=NULL,wait_infinite=0 WHERE key=?", (key,))
nanobot/channels/telegram/social/store.py:723:            self.db.execute("UPDATE scopes SET mode='waiting',wake_at=NULL,wait_after_seq=?,wait_infinite=? WHERE key=?",
nanobot/channels/telegram/social/store.py:769:                self.db.execute("UPDATE scopes SET mode='observing',wait_infinite=0 WHERE key=? AND mode='waiting'", (key,))
nanobot/channels/telegram/social/store.py:779:                        self.db.execute("UPDATE scopes SET mode='sleeping',wake_at=?,wake_on_timeout=?,wait_infinite=? WHERE key=?",
nanobot/channels/telegram/social/store.py:789:                    self.db.execute("UPDATE scopes SET timeout_through=event_seq WHERE key=?", (key,))
nanobot/channels/telegram/social/store.py:815:                "UPDATE scopes SET mode=?,wake_at=?,wake_on_timeout=?,wait_after_seq=?,wait_infinite=? WHERE key=?",
nanobot/channels/telegram/social/store.py:830:            self.db.execute("UPDATE scopes SET wake_config=? WHERE key=?", (body, key))
nanobot/channels/telegram/social/store.py:835:                    self.db.execute("UPDATE scopes SET mode='observing',wait_infinite=0 WHERE key=? AND mode='waiting'", (key,))
nanobot/channels/telegram/social/store.py:836:                self.db.execute("UPDATE scopes SET mode='observing',wait_infinite=0,wake_at=NULL WHERE key=? "
nanobot/channels/telegram/social/store.py:876:                self.db.execute("UPDATE scopes SET mode='observing',wake_at=NULL,wait_infinite=0 WHERE key=?", (scope.session_key,))
nanobot/channels/telegram/social/store.py:885:                    self.db.execute("UPDATE scopes SET timeout_through=event_seq WHERE key=?", (scope.session_key,))
nanobot/channels/telegram/social/store.py:959:                              "WHERE status='scheduled' UNION ALL SELECT MIN(wake_at) FROM scopes "
nanobot/channels/telegram/social/store.py:966:                              "UNION ALL SELECT MIN(CAST(json_extract(wake_config,'$.expires_at') AS REAL)) FROM scopes WHERE wake_config<>'{}' "
nanobot/channels/telegram/social/expressions.py:285:            "SELECT 1 FROM scopes WHERE mode='active' UNION ALL SELECT 1 FROM wakes WHERE status IN ('scheduled','queued','running') UNION ALL SELECT 1 FROM judge_state WHERE inflight=1 OR pending_due IS NOT NULL LIMIT 1").fetchone())
```

保留原因逐项对应：

| 位置 | 原因 |
|---|---|
| store:73、284、509、521、579、660、723、769、779、789、815、830、835、836、876、885 | 仅更新 scopes 调度列，属于 P3-b |
| store:542 | 外层更新 wakes，子查询读取调度列 mode，属于 P3-b |
| store:80 | 外层写 judge_state，按简报保留，属于 P3-c |
| store:237、238 | 同一条 judge_batches UPDATE 的子查询，属于 P3-c |
| store:959、966 | next_due 的跨表调度查询，属于 P3-b/c |
| expressions:285 | mode/wakes/judge_state 的跨表忙碌判断，属于后续阶段 |

对每个删除的接口分别执行全仓库 Python 搜索，排除 `.git`、`.venv`、`node_modules`、`__pycache__`；匹配旧 Store 接收者及测试别名，避免把新仓储接口或同名无关接口计作残留：

```text
setting_revision: 0 matches
owns: 0 matches
state: 0 matches
scopes: 0 matches
has_message: 0 matches
media_for_message: 0 matches
collect_sticker: 0 matches
sticker: 0 matches
import_stickers: 0 matches
sticker_catalog: 0 matches
annotate_stickers: 0 matches
list_stickers: 0 matches
_shown_sequences: 0 matches
```

旧 schema import 的生产代码、测试及脚本搜索亦无输出。

**4. 修改过的测试**

下表路径均相对于 `nanobot/channels/telegram/tests/`；除注明的时钟构造外，均为机械改名或 import 更新。

| 文件:行号 | 修改 |
|---|---|
| `test_harness_prompts.py:48,70` | state → scopes.state |
| `test_reply_reliability.py:15,51,76,96,99,100,112,124,127,143,144,297` | schema import、state 调用 |
| `test_shared_tools.py:152` | owns → scopes.owns |
| `test_social_activity.py:11` | schema import |
| `test_social_bindings.py:57,58,153` | Mock、monkeypatch 的 setting_revision 接收者及 state 调用 |
| `test_social_dialogue.py:13,185,211,422,425,440,455,482,617` | schema import、state 调用 |
| `test_social_expressions.py:73,240` | state 调用 |
| `test_social_followups.py:18,364,367,380,386` | schema import、state 调用 |
| `test_social_judge.py:12,145,155,314,315,340,434,558,565,566,567,568` | schema import、state 调用 |
| `test_social_judge_runtime.py:71,85,158,194,198,235,328,348,358,360,422,451` | state 调用 |
| `test_social_learning.py:12,50,60,61,87,147,229,235,238,291` | schema import、state 调用 |
| `test_social_media.py:21,93,521,524` | schema import、messages/scopes 调用 |
| `test_social_memory.py:16,17,28,185,300` | import、state 调用；expiry 测试按原 fixture 方式构造 runtime，传入 `now=lambda: clock[0]` 并关闭 runtime |
| `test_social_proactive.py:16,123,258,546,620,637,690,691` | schema import、state 调用 |
| `test_social_revisions.py:7,79` | schema import、state 调用 |
| `test_social_runtime.py:106,113,157,193,207,216,261,264,495,516,527,558,723,808,832,833,838,839,863,865` | scopes/state 调用 |
| `test_social_scheduler.py:17,85,92,183,208,210,211,223,239,247,257,258,312,379,402,421,442,456,664` | schema import、state 调用 |
| `test_social_selection.py:34,64,90,163,164,167,255` | scopes、stickers 调用 |
| `test_social_stickers.py:59,60,65,78,87,126,152,183` | stickers 仓储调用 |
| `test_social_wake_v2.py:13,119,127,139,140,153,188,205,219,240,245,280` | schema import、state 调用 |
| `social_harness.py:274` | state 调用 |
| `evaluate_social_followups.py:90` | state 调用 |
| `evaluate_social_judge.py:103,108,141` | state 调用 |
| `evaluate_social_stickers.py:83,89,92` | sticker_catalog → stickers.catalog |
| `evaluate_social_v4.py:131,156,234` | state 调用 |

另有 `scripts/evaluate_social_v5.py:275,362,399` 的 state 调用机械改名。未修改 fixture 本身、测试断言或测试中的直接 SQL。

**5. 与简报的偏差及原因**

- `git mv` 实际失败：

  ```text
  fatal: Unable to create '/home/ubuntu/nanobot-bridge-tg/.git/index.lock': Read-only file system
  ```

  因此使用普通文件移动；schema 内容直接比较完全一致，未修改 Git 索引。

- basedpyright 原命令受到沙箱依赖发现问题影响；通过临时配置补充现有依赖路径后为 0 errors。没有修改项目配置或压制诊断，不能声称未经补充的原命令通过。
- 全目录实际结果为 **947 passed**，与简报的 971 不一致；没有增删测试。
- 工作期间发现 `docs/SOCIAL_REFACTOR_PROGRESS.md` 出现外部变动，本任务未编辑或回退该文件。

**6. 未解决的问题 / 建议**

- 编排者应在正常环境复跑原始 basedpyright 命令，并确认 947 与简报基线数量的差异。
- 代码搬迁无已知未解决问题；P3-0 中的历史疑点未修复，P3-b/c 范围 SQL 按要求保留。
- 未执行构建、启动、提交或部署。