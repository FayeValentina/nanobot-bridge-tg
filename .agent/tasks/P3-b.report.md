1. 改动摘要（按文件）

实现已完成，指定测试和 ruff 通过；**basedpyright 尚有 1 项错误**，来自简报要求原样使用的 `Iterator[None]` 签名。

| 文件 | 改动 |
|---|---|
| [db/wakes.py](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/wakes.py) | 新增枚举、转换表、唯一状态写入口及 wake/dialogue/调度 SQL；526 行 |
| [db/connection.py](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/connection.py) | 原样新增 `transaction` |
| [store.py](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/store.py) | 挂载 `Wakes`，保留跨仓储编排；1036 → 627 行 |
| `proactive.py`、`social/runtime.py`、`followups.py`、`memory.py`、`judge_runtime.py` | wake 相关 SQL 改调仓储 |
| `scheduler.py` | 改调 `store.wakes.next_due()` |
| Telegram 测试目录 | 仅机械性调用改名；场景测试未修改 |

生产代码净增 **120 行**。schema、`SCHEMA_VERSION`、提示词、配置和工具 schema 未改。未提交、推送或部署。工作区原有的进度文档改动及任务简报未动。

2. 方法清单（Wakes 的每个公开方法 → 原位置）

以下行号均指搬迁前代码；`store` 表示 `SocialStore`。

| Wakes 方法 | 原位置 |
|---|---|
| `claim` | `store.py:497` |
| `active_turn` | `store.py:512` |
| `finish` | `store.py:518` |
| `dialogue_job` | `store.py:675` |
| `sleep_observation_due` | `store.py:734` |
| `consume_dialogue` | `store.py:752` |
| `dialogue_status` | `store.py:806` |
| `defer` | `store.py:810` |
| `set_wake_config` | `store.py:824` |
| `schedule` | `store.py:839`，原 `_schedule` |
| `consume_timers` | `store.py:868` |
| `ready` | `store.py:887` |
| `release_pending` | `store.py:952` |
| `next_due` | `store.py:957` |
| `recover` | `store.py:524`，原 `recovery_jobs` 的 wakes 部分 |
| `cancel_opportunity` | `store.py:589` 的 wakes 部分 |
| `wait_for_quiet` | `store.py:683`，judge 更新留在 store |
| `reset_binding` | `store.bind`、原 `_clear_dialogue` |
| `pause` | `store.pause`、原 `_clear_dialogue` |
| `note_member_activity` | `store.observe` 的 fresh 分支 |
| `get` | `store.view` 的 wake 查询 |
| `admit_view_job` | `store.note_seen` 的 queued → running |
| `admit_shown` | `store.note_seen` 的 scheduled 消费循环 |
| `note_observed` | `store.note_seen` 的 dialogue_state 写入 |
| `recent_turns` | `store._handoff_judgement` |
| `scheduled` | `store._handoff_judgement` |
| `attach_judgement` | `store._handoff_judgement` |
| `add_dialogue_job` | `store._handoff_judgement` |
| `clear_wake_config` | `store._handoff_judgement` |
| `turn_expires` | `store.enqueue_parts` |
| `schedule_opportunity` | `proactive.py:241` |
| `requeue` | `social/runtime.py:399`，发布异常回退 |
| `note_sent` | `social/runtime.py:754` |
| `wait` | `judge_runtime.py:80/:98`、原 dialogue_status 等相同查询 |
| `latest_opportunity` | `proactive.py:75` |
| `has_non_sleep_wait` | `proactive.py:91` |
| `active_wake` | `proactive.py:172` |
| `request_count` | `proactive.py:190/:225` |
| `active_opportunities` | `proactive.py:247` |
| `opportunity` | `proactive.py:260` |
| `turn_opportunities` | `proactive.py:271` |
| `is_activity_turn` | `social/runtime.py:324` |
| `running_conditions` | `social/runtime.py:329` |
| `request_usage` | `social/runtime.py:446` |
| `running_activity` | `social/runtime.py:932` |
| `latest_turn_job` | `followups.py:113` |
| `turn_source` | `memory.py:367` |

私有方法：`_check_trigger_source` 整体搬迁；`_refund_unclaimed_opportunity` 改名 `_refund`；三处 INSERT 共用 `_insert_scheduled`。

3. 行为修正

| 修正 | 实现位置及结果 | 测试断言修改 |
|---|---|---|
| 终态不可变／Q3 | `_move` 反查允许源状态；`cancel_opportunity` 保留终态，store 中 actions/followups 仍执行 | 无 |
| Q2 发布失败回退 | runtime 调 `Wakes.requeue`；同一事务先按原条件退款，再仅将 queued 改为 scheduled，due=now+1 | 无 |
| Q9 全局终态清理 | 从 `schedule` 删除清理；放到 `ready` 事务开头，覆盖所有 scope，dialogue_jobs 外键级联删除 | 无 |
| Q12 恢复返回值 | `SocialStore.recover()` 和 `Wakes.recover()` 返回 None；删除恢复过程的 WakeJob 列表构造 | 无 |

没有测试需要修改旧行为断言，因此没有“文件:行号／原断言／新断言”条目。已核对所有测试 diff，全部仅为机械性调用改名。

4. 实际运行的命令及结果

指定测试使用现有沙箱插件运行，两轮均通过；最终一轮：

```bash
PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env' \
.venv/bin/python -m pytest nanobot/channels/telegram/tests \
tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py \
-q -p no:cacheprovider --ignore-glob='*evaluate_*'
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
981 passed, 13 warnings in 149.46s (0:02:29)
```

包含全部 `test_social_scenarios.py`，该文件未修改。

```bash
.venv/bin/ruff check nanobot/ scripts/
```

```text
All checks passed!
```

原始 basedpyright 命令因依赖发现问题产生 1912 errors。按 P3-a 使用 `/tmp/p3-pyright.json`，配置内容为：

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
/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/connection.py
  /home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/connection.py:20:2 - error: The function "contextmanager" is deprecated
    Annotating the return type as `-> Iterator[Foo]` with `@contextmanager` is deprecated. Use `-> Generator[Foo]` instead. (reportDeprecated)
1 error, 0 warnings, 0 notes
```

`git diff --check` 无输出。

临时审计脚本：

```bash
.venv/bin/python /tmp/p3b_audit.py
```

```text
Original wake UPDATE predicates retained (scope mode literals parameterized): 23
Original INSERT SQL retained: 3
Wakes literal writes restricted to owned tables: OK
Schema, SCHEMA_VERSION and P3-a repositories unchanged: OK
Test edits are exclusively mechanical caller renames; scenarios unchanged: OK
Production net line increase: 120
Wakes lines: 526
```

临时库行为检查：

```bash
PYTHONPATH=. .venv/bin/python /tmp/p3b_lifecycle_probe.py
```

```text
Terminal states immutable; cancellation still updates actions/receipts: OK
Publish failure requeues queued jobs once, refunds budget, preserves terminal jobs: OK
Ready prunes terminal wakes across scopes with dialogue_jobs cascading: OK
Store recovery returns None: OK
```

要求的 SQL 搜索：

```bash
grep -rnE "(UPDATE|INTO|FROM) (wakes|requests|dialogue_waits|dialogue_jobs|dialogue_state)\b" nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py --include=*.py | grep -v "/db/wakes.py"
```

```text
nanobot/channels/telegram/social/db/schema.py:84:            UPDATE wakes SET due=created,requested=created;
nanobot/channels/telegram/social/db/schema.py:93:            INSERT INTO requests SELECT scope,requested FROM wakes WHERE requested IS NOT NULL;
nanobot/channels/telegram/social/db/schema.py:202:            UPDATE wakes SET source_seq=trigger_seq;
nanobot/channels/telegram/social/db/schema.py:215:            INSERT INTO dialogue_waits_v8 SELECT *,NULL FROM dialogue_waits;
nanobot/channels/telegram/social/db/schema.py:272:            store.db.execute("UPDATE scopes SET last_member_at=(SELECT last_at FROM dialogue_state WHERE scope=scopes.key)")
nanobot/channels/telegram/social/runtime.py:635:                self.store.db.execute("UPDATE actions SET reply_to=?,expires=(SELECT MAX(expires) FROM wakes "
nanobot/channels/telegram/social/expressions.py:285:            "SELECT 1 FROM scopes WHERE mode='active' UNION ALL SELECT 1 FROM wakes WHERE status IN ('scheduled','queued','running') UNION ALL SELECT 1 FROM judge_state WHERE inflight=1 OR pending_due IS NOT NULL LIMIT 1").fetchone())
```

逐条说明：

- schema.py:84：历史 due/requested 回填，保留。
- schema.py:93：历史 requests 迁移，保留。
- schema.py:202：历史 source_seq 回填，保留。
- schema.py:215：历史 dialogue_waits 表迁移，保留。
- schema.py:272：历史 last_member_at 回填中的只读子查询，保留。
- runtime.py:635：简报明确留给 P3-c 的 actions UPDATE，wakes 仅用于只读子查询。
- expressions.py:285：简报明确保留的忙碌联合查询。

```bash
grep -n "status=" nanobot/channels/telegram/social/db/wakes.py
```

```text
69:        assignments = ",".join(["status=?", *(f"{column}=?" for column in columns)])
74:        self.db.execute(f"INSERT INTO wakes({columns}) " + values.format(status=WakeStatus.SCHEDULED), params)
82:            changed = self._move(WakeStatus.RUNNING, "id=? AND scope=? AND version=? AND status='queued' AND expires>?", (job,key,version,self.now(),), turn_id=turn)
89:        return self.db.execute( "SELECT 1 FROM wakes WHERE scope=? AND version=? AND turn_id=? AND status='running' AND expires>?", (key, version, turn, self.now()), ).fetchone() is not None
93:            self._move(status, "turn_id=? AND status='running'", (turn,))
125:            self._move(WakeStatus.CANCELLED, "status='scheduled' AND created>? AND id IN (SELECT job FROM dialogue_jobs)", (now,))
159:                job = self.db.execute("SELECT id FROM wakes WHERE scope=? AND status='scheduled'", (key,)).fetchone()
183:            self._move(WakeStatus.CANCELLED, "scope=? AND status='scheduled' AND reason <> 'judge' AND (trigger_seq<=? OR ?)", (key,max(shown, default=0),int(sleeping),))
205:        for opportunity in self.db.execute("SELECT id FROM wakes WHERE scope=? AND reason='proactive' AND status='queued'", (key,)).fetchall():
207:        row = self.db.execute("SELECT * FROM wakes WHERE scope=? AND version=? AND status='scheduled'", (key, state["version"])).fetchone()
236:                                           "(SELECT 1 FROM wakes WHERE scope=? AND status='{status}')",
247:            for row in self.db.execute("SELECT * FROM wakes WHERE status='scheduled' AND due<=? ORDER BY due", (now,)).fetchall():
273:                changed = self._move(WakeStatus.QUEUED, "id=? AND status='scheduled'", (row["id"],), requested=now)
278:                        trigger_seq=int(row["trigger_seq"]), status="queued", created=float(row["created"]),
304:            self.db.execute("UPDATE wakes SET due=MIN(due,?) WHERE scope=? AND status='scheduled' AND id NOT IN (SELECT job FROM dialogue_jobs)", (self.now(), key))
308:                              "WHERE status='scheduled' UNION ALL SELECT MIN(wake_at) FROM scopes "
309:                              "UNION ALL SELECT MIN(MIN(due,expires)) FROM actions WHERE status='queued' "
323:            self._move(WakeStatus.CANCELLED, "status IN ('scheduled','queued','running') AND EXISTS (SELECT 1 FROM message_deliveries d WHERE d.scope=wakes.scope AND d.turn_id=wakes.turn_id AND d.status='unknown')", ())
326:            for opportunity in self.db.execute("SELECT * FROM wakes WHERE reason='proactive' AND status='queued'").fetchall():
330:            self._move(WakeStatus.CANCELLED, "reason='proactive' AND status='running' AND json_extract(proactive_source,'$.kind') IN ('idle','activity')", ())
333:            self._move(WakeStatus.COMPLETED, "status='running' AND scope IN (SELECT key FROM scopes WHERE mode IN (?,?))", (ScopeMode.WAITING, ScopeMode.SLEEPING))
338:                       "AND actions.version=wakes.version AND actions.status='unknown' AND actions.created>=wakes.created)", ())
371:        original_source = self.db.execute("SELECT MAX(trigger_seq) FROM wakes WHERE scope=? AND turn_id=? AND status='running'", (key, turn)).fetchone()[0]
388:            pending = self.db.execute("SELECT MAX(trigger_seq) FROM wakes WHERE scope=? AND status='scheduled' AND reason='judge' AND trigger_seq>?", (key, after)).fetchone()[0]
401:            self._move(WakeStatus.CANCELLED, "scope=? AND status='scheduled' AND reason <> 'judge' AND trigger_seq<=?", (key,after,))
427:        return bool(self._move(WakeStatus.RUNNING, "id=? AND scope=? AND status='queued' AND version=? AND expires>?",
433:            candidates = self.db.execute("SELECT id FROM wakes WHERE scope=? AND version=? AND status='scheduled' AND trigger_seq=? AND expires>?", (key, version, seq, self.now())).fetchall()
441:            self._move(WakeStatus.RUNNING, "scope=? AND version=? AND status='scheduled' AND trigger_seq=? AND expires>?",
451:        return self.db.execute("SELECT id,turn_id FROM wakes WHERE scope=? AND (status='completed' OR (status='running' AND expires>?)) ORDER BY created DESC LIMIT 32", (key, self.now())).fetchall()
454:        return self.db.execute("SELECT id,reason FROM wakes WHERE scope=? AND status='scheduled'", (key,)).fetchone()
468:        return self.db.execute("SELECT MAX(expires) FROM wakes WHERE turn_id=? AND status='running'", (turn,)).fetchone()[0]
480:            self._move(WakeStatus.SCHEDULED, "id=? AND status='queued'", (job,), due=self.now()+1)
514:        return self.db.execute("SELECT condition_config FROM wakes WHERE id=? AND scope=? AND status='running'", (job, key)).fetchone()
520:        return self.db.execute("SELECT 1 FROM wakes WHERE scope=? AND turn_id=? AND status='running' AND reason='proactive' AND json_extract(proactive_source,'$.kind')='activity'", (key, turn)).fetchone()
526:        return self.db.execute("SELECT proactive_source FROM wakes WHERE scope=? AND turn_id=? AND status='running'", (scope, turn_id)).fetchone()
```

只有第 69 行构建 status SET；其他匹配为 WHERE/SELECT、固定 scheduled 插入格式参数或 WakeJob 字段。

对每个移走的方法，以 `\b(?:store|upgraded|SocialStore)\.<方法名>\s*\(` 在全仓库 Python 文件中逐项运行 `rg -n`，输出：

```text
claim: 0 old calls
active_turn: 0 old calls
finish: 0 old calls
dialogue_job: 0 old calls
sleep_observation_due: 0 old calls
consume_dialogue: 0 old calls
dialogue_status: 0 old calls
defer: 0 old calls
set_wake_config: 0 old calls
_schedule: 0 old calls
consume_timers: 0 old calls
ready: 0 old calls
_check_trigger_source: 0 old calls
release_pending: 0 old calls
next_due: 0 old calls
_refund_unclaimed_opportunity: 0 old calls
recovery_jobs: 0 old calls
```

5. 与简报的偏差及原因

- **类型检查未达到 0 errors**：补依赖路径后仅剩指定 `Iterator[None]` 签名引起的弃用错误。保留了简报原样代码，没有关闭诊断。
- `wakes.py` 为 526 行，比估计上限多 26 行；包含要求的 47 个公开入口，生产代码净增仍为 120 行。
- SQL 搜索另命中五处历史 schema 迁移；遵守“不改 schema”要求保留，未搬迁。

6. 未解决的问题 / 建议

- 需编排者决定是否允许将 `transaction` 返回标注改为 `Generator[None, None, None]`，以解决唯一的类型检查错误；运行逻辑无需改变。
- Q4/Q5/Q6/Q10/Q11 等其余疑点保持原状，留待后续任务处理。