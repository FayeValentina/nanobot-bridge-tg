1. 改动摘要

`social/db/schema.py` 缩至 **225 行**：逐字复制 v22 基线 DDL，删除全部六个 `_upgrade_*` helper 和逐版本迁移分支；仅接受新空库和 v22 库。

以下文件均位于 `nanobot/channels/telegram/tests/`，删除项逐一列出：

| 文件 | 删除的测试 |
|---|---|
| `test_social_scheduler.py` | `test_schema_v1_migration_preserves_watermarks_and_delivery_journal` |
| `test_social_media.py` | `test_schema_v2_upgrade_preserves_messages_actions_and_watermarks` |
| `test_social_memory.py` | `test_schema_three_upgrade_preserves_observations_views_and_watermarks` |
| `test_social_proactive.py` | `test_schema_four_upgrade_preserves_memory_watermarks_and_history` |
| `test_reply_reliability.py` | `test_schema_five_upgrade_adds_receipts_without_losing_history` |
| `test_social_dialogue.py` | `test_schema_six_upgrade_preserves_data_and_unknown_versions_are_rejected` |
| `test_social_wake_v2.py` | `test_schema_seven_upgrade_preserves_an_existing_wait_and_watermarks` |
| `test_social_judge.py` | `test_schema_eight_upgrade_preserves_main_data` |
| `test_social_revisions.py` | `test_schema_ten_upgrade_removes_keys_and_preserves_journals` |
| `test_social_proactive.py` | `test_schema_eleven_upgrade_preserves_source_values_and_rolls_back` |
| `test_social_followups.py` | `test_schema_twelve_upgrade_preserves_sources` |
| `test_social_followups.py` | `test_schema_twelve_upgrade_failure_rolls_back` |
| `test_social_learning.py` | `test_upgrade_schema_thirteen_preserves_all_existing_source_values` |
| `test_social_activity.py` | `test_schema_sixteen_migration_preserves_data_and_rolls_back` |
| `test_social_learning.py` | `test_upgrade_seventeen_retains_learning_batches_and_adds_nullable_detail` |
| `test_social_followups.py` | `test_schema_twenty_merges_delivery_rows_and_preserves_followup_ids` |
| `test_social_revisions.py` | `test_schema_twenty_one_exports_judge_history_and_preserves_watermarks` |

- 删除 scheduler 中的 `remove_v4_columns`、`remove_v4b_tables`；搜索确认调用者均为上述删除项。
- `test_social_revisions.py` 仅包含两个迁移测试，整文件删除。
- 清除迁移测试专用 import；没有其他需要修改的旧版本断言。
- scheduler 新增空库基线、v22 重开、拒绝 v21、拒绝非空 v0 四项测试。
- dialogue 拆出 `test_unknown_schema_version_is_rejected`，验证拒绝 v23，替代原混合测试中的 v99 断言。
- 保留 `test_upgrade_defaults_keep_ordinary_observation_zero_calls`；场景测试未修改。其余保留测试定义经 AST 比较均未改变。

合计净删除 **1126 行**，其中测试净删除 **763 行**。

2. 实际运行的命令及结果

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env' .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob='*evaluate_*' > /tmp/p9/p9c-social.log 2>&1

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
944 passed, 13 warnings in 83.50s (0:01:23)
```

上述测试包含未修改的 `test_social_scenarios.py`。

```text
$ .venv/bin/ruff check nanobot/
All checks passed!

$ .venv/bin/basedpyright -p /tmp/p8-a-pyright.json nanobot/channels/telegram/social
0 errors, 0 warnings, 0 notes

$ git diff --check
```

最后一项无输出，退出码 0。

```text
$ grep -rn '_upgrade_\|user_version=2[01]\b' nanobot/channels/telegram --include='*.py' --exclude-dir=tests
```

生产代码无输出，退出码 1。未排除测试时仅命中：

```text
nanobot/channels/telegram/tests/test_social_proactive.py:189:async def test_upgrade_defaults_keep_ordinary_observation_zero_calls(social):
nanobot/channels/telegram/tests/test_social_scheduler.py:838:def test_schema_v21_is_rejected_with_upgrade_release(tmp_path):
nanobot/channels/telegram/tests/test_social_scheduler.py:840:        db.execute("PRAGMA user_version=21")
```

3. 线上库副本演练结果

实际命令：

```text
$ PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python /tmp/p9/work/p9c/rehearsal.py > /tmp/p9/p9c-rehearsal.log 2>&1
```

实际比较输出：

```text
copied: /tmp/p9/social.sqlite3 -> /tmp/p9/work/p9c/social.sqlite3
BASELINE_DDL bytes equal source SQL: True
SocialStore open: success
user_version: 22 -> 22
sqlite_master rows: 93 -> 93; row-by-row equal: True
table counts: 40 -> 40; equal: True
changed table counts: {}
```

39 张业务表及 `sqlite_sequence` 的行数全部不变。逐表结果见 [演练日志](/tmp/p9/p9c-rehearsal.log)。

4. 与简报的偏差及原因

无实现偏差。按简报将检查顺序从“读取版本、拒绝越界版本、quick_check”调整为“quick_check、读取版本、判断版本”。因此同时存在损坏和越界版本时，检查错误的优先级改变；正常 v22 库的打开结果不变，已通过副本演练确认。

未提交、推送或部署，未接触线上库及备份。

5. 未解决的问题 / 需要编排者决定的事项

无。