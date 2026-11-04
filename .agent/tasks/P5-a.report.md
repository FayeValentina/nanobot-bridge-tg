1. 改动摘要（按文件），含生产代码净行数

   生产代码 **+107 / −115，净减少 8 行**。

   - `social/db/schema.py`：schema 21；按指定五步迁移；新库直接采用 `message_ids`，不创建旧表。
   - `social/db/deliveries.py`：统一投递读写、终态保护、跨工具内容保护；原 claim 拆成只读 `queued_action` 和只写 `claim`。
   - `social/store.py`：显式 kind；只有回执更新成功才执行副作用；新增 `claim_retry` 编排。
   - `social/runtime.py`：替换为 `begin_message`、`finish_message`；新增 `followup_note`；适配消息 ID 数组。
   - `telegram/runtime.py`：普通 envelope 只预约一行；删除所有 `social.store` 访问。
   - `social/db/messages.py`：参与统计只关联 actions。
   - `social/db/wakes.py`：通过 kind 保留两类 unknown 恢复规则。
   - `social/followups.py`：回执同步只关联 actions。
   - `social/proactive.py`：诊断统一输出到 `sends`。
   - `social/selection.py`：更新已过时的双表查询注释。
   - 测试及夹具：适配新接口、数组列和历史 schema；新增一个 schema 20→21 迁移测试。

2. 五项行为变化各自的落点（文件:行号）

   - **普通 message 单行；预约拒绝不另写 failed 行**：[social/runtime.py:586](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:586)、[telegram/runtime.py:1137](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/runtime.py:1137)。
   - **终态不可改，拒绝更新不触发计数或跟进副作用**：[deliveries.py:75](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/deliveries.py:75)、[store.py:275](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/store.py:275)。
   - **claim 因预算或过期失败时同步跟进回执**：[store.py:291](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/store.py:291)；调用入口为 [social/runtime.py:791](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:791)。
   - **actions 保护覆盖普通 message unknown**：[deliveries.py:16](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/deliveries.py:16)、[deliveries.py:93](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/deliveries.py:93)。
   - **诊断字段并入 sends**：[proactive.py:78](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/proactive.py:78)。

   返工确认的两处：

   - `unknown_content` 删除 kind 条件：[deliveries.py:93](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/deliveries.py:93)。
   - `latest_content` 查询 text 时包含 message，sticker 保持独立：[deliveries.py:126](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/deliveries.py:126)。

   其他依赖“message 预约行 kind=text”的 kind 过滤问题：**无**。

3. 实际运行的命令及结果（原样贴最后几行）

   ```bash
   PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env' .venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob='*evaluate_*'
   ```

   ```text
   -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
   984 passed, 13 warnings in 159.33s (0:02:39)
   ```

   包含全部 **21 个场景测试**；`test_social_scenarios.py` 未修改。

   前两轮完整运行的末行如下；对应夹具及重复断言已修正：

   ```text
   2 failed, 982 passed, 13 warnings in 155.06s (0:02:35)
   1 failed, 983 passed, 13 warnings in 162.10s (0:02:42)
   ```

   ```bash
   .venv/bin/ruff check nanobot/
   ```

   ```text
   All checks passed!
   ```

   ```bash
   .venv/bin/basedpyright --project /tmp/p5-pyrightconfig.json nanobot/channels/telegram/social nanobot/channels/telegram/runtime.py
   ```

   ```text
   0 errors, 0 warnings, 0 notes
   ```

   临时配置继承项目 `pyproject.toml`，指定现有 `.venv` 和源码搜索路径；没有关闭检查规则。默认依赖发现及仅指定 pythonpath 的尝试均报 1879 errors；临时配置补齐源码路径前报 143 errors，均为环境路径解析问题。

   ```bash
   .venv/bin/python /tmp/p5_receipt_probe.py
   ```

   ```text
   budget: failed receipt synced; remaining_attempts=2; terminal overwrite rejected
   expiry: failed receipt synced; remaining_attempts=2; terminal overwrite rejected
   ```

   ```bash
   git diff --check
   ```

   无输出，退出码 0。

   ```bash
   grep -rn "message_deliveries\|reserve_message_send\|record_message_receipt" nanobot --include=*.py
   ```

   ```text
   nanobot/agent/loop.py:41:from nanobot.agent.tools.message import capture_message_deliveries
   nanobot/agent/loop.py:1867:        with capture_message_deliveries() as message_sends:
   nanobot/agent/tools/message.py:31:def capture_message_deliveries() -> Generator[set[tuple[str, str]], None, None]:
   nanobot/channels/telegram/runtime.py:1176:            self._record_message_receipt(scope, delivery_id, DeliveryReceipt("unknown", tuple(message_ids), "Cancelled during delivery; do not resend."), sent_texts)
   nanobot/channels/telegram/runtime.py:1180:        self._record_message_receipt(scope, delivery_id, receipt, sent_texts)
   nanobot/channels/telegram/runtime.py:1187:    def _record_message_receipt(self, scope: ConversationScope, delivery_id: str | None,
   nanobot/channels/telegram/tests/test_social_proactive.py:48:    db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_activity.py:179:        db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_wake_v2.py:264:    db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_judge.py:430:        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_followups.py:381:    social.store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_followups.py:402:    social.store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_followups.py:452:        db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_followups.py:453:        db.execute("INSERT INTO message_deliveries VALUES('legacy-sent',?,'legacy-turn',?,'sent','[601,602]',NULL,NULL)",
   nanobot/channels/telegram/tests/test_social_followups.py:455:        db.execute("INSERT INTO message_deliveries VALUES('legacy-queued',?,'pending-turn',?,'queued','[]','pending receipt','old-version')",
   nanobot/channels/telegram/tests/test_social_followups.py:475:        assert restored.db.execute("SELECT name FROM sqlite_master WHERE name='message_deliveries'").fetchone() is None
   nanobot/channels/telegram/tests/test_social_learning.py:258:        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/test_social_learning.py:415:        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/tests/evaluate_social_v4.py:259:                delivery = runtime.record_message_receipt(SCOPE, DeliveryReceipt("queued"), ctx.turn_id, [], followup_id=matter)
   nanobot/channels/telegram/tests/evaluate_social_v4.py:262:                runtime.record_message_receipt(SCOPE, receipt, ctx.turn_id, [(sent, message.content)], delivery)
   nanobot/channels/telegram/tests/evaluate_social_v4.py:379:                "ordinary_deliveries": [dict(row) for row in runtime.store.db.execute("SELECT status,message_ids FROM message_deliveries")]}
   nanobot/channels/telegram/tests/test_social_dialogue.py:612:    dialogue.store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT)")
   nanobot/channels/telegram/tests/evaluate_social_followups.py:69:                delivery = runtime.record_message_receipt(scope, DeliveryReceipt("queued"), ctx.turn_id, [], followup_id=matter)
   nanobot/channels/telegram/tests/evaluate_social_followups.py:72:                runtime.record_message_receipt(scope, receipt, ctx.turn_id, [(sent,message.content)], delivery)
   nanobot/channels/telegram/tests/test_social_revisions.py:58:        store.db.execute("CREATE TABLE message_deliveries (id TEXT PRIMARY KEY,scope TEXT NOT NULL REFERENCES scopes(key),turn_id TEXT,created REAL NOT NULL,status TEXT NOT NULL,message_ids TEXT NOT NULL,detail TEXT,version TEXT)")
   nanobot/channels/telegram/social/db/schema.py:152:            CREATE TABLE message_deliveries (
   nanobot/channels/telegram/social/db/schema.py:157:            CREATE INDEX message_deliveries_scope ON message_deliveries(scope,created);
   nanobot/channels/telegram/social/db/schema.py:164:            {"ALTER TABLE message_deliveries ADD COLUMN version TEXT;" if version else ""}
   nanobot/channels/telegram/social/db/schema.py:330:                SELECT id,scope,COALESCE(version,(SELECT version FROM scopes WHERE key=message_deliveries.scope)),
   nanobot/channels/telegram/social/db/schema.py:332:                    turn_id,'message',1 FROM message_deliveries;
   nanobot/channels/telegram/social/db/schema.py:333:            DROP TABLE message_deliveries;
   nanobot/channels/telegram/social/db/schema.py:372:            tables = ("views", "wakes", "actions", "proactive_state", "message_deliveries", "wake_samples", "judge_batches")
   ```

   ```bash
   grep -n "social\.store" nanobot/channels/telegram/runtime.py
   grep -rn "\bmessage_id\b" nanobot/channels/telegram/social/db/deliveries.py
   ```

   两条均无输出，退出码均为 1（无匹配）。

4. 修改过的测试（逐条）

   以下测试文件均位于 `nanobot/channels/telegram/tests/`：

   | 文件 | 测试／夹具 | 修改 |
   |---|---|---|
   | `social_harness.py` | `interrupt_send` | 显式传 `kind="text"`。 |
   | `test_reply_reliability.py` | `test_ordinary_message_gets_platform_ids_and_a_durable_receipt` | 查询 actions；断言单行；补充同 turn 跨工具返回原回执。 |
   | 同上 | `test_ordinary_timeout_is_unknown_and_never_retried` | 查询 actions。 |
   | 同上 | `test_ordinary_envelope_reserves_every_chunk_before_any_platform_send` | 预约拒绝后断言 actions 无行。 |
   | 同上 | `test_schema_five_upgrade_adds_receipts_without_losing_history` | 还原旧 actions 列；升级后查询新表。 |
   | 同上 | `test_confirmed_ordinary_delivery_keeps_cross_chat_permissions` | 查询 message kind 的 actions 行数。 |
   | 同上 | `test_interrupted_ordinary_send_is_not_reawakened_after_restart` | 预约状态改断言 sending；恢复后仍为 unknown。 |
   | `test_social_activity.py` | `test_schema_sixteen_migration_preserves_data_and_rolls_back` | 补齐旧投递表结构。 |
   | `test_social_dialogue.py` | `test_ordinary_message_receipt_records_confirmed_context_once` | 改用 begin/finish。 |
   | 同上 | `test_schema_six_upgrade_preserves_data_and_unknown_versions_are_rejected` | 重建 schema 6 投递结构。 |
   | `test_social_followups.py` | `test_failed_send_does_not_count_and_unknown_send_blocks_resending` | 断言 unknown 不能改成 sent，跟进仍 unresolved。 |
   | 同上 | `test_reworded_unknown_matter_stays_blocked_after_process_restart` | 同上，覆盖重启后。 |
   | 同上 | `test_schema_twelve_upgrade_preserves_sources` | 补齐旧投递结构。 |
   | 同上 | `test_schema_twelve_upgrade_failure_rolls_back` | 补齐旧投递结构。 |
   | 同上 | `test_schema_twenty_merges_delivery_rows_and_preserves_followup_ids` | **新增唯一迁移测试**：覆盖合并行数、kind、状态映射、ID 数组、版本回填、跟进 ID 保留及旧表删除。 |
   | `test_social_judge.py` | `test_schema_eight_upgrade_preserves_main_data` | 补齐旧投递结构。 |
   | `test_social_learning.py` | `test_upgrade_schema_thirteen_preserves_all_existing_source_values` | 补齐旧投递结构。 |
   | 同上 | `test_usage_requires_real_receipt_and_keeps_human_occurrences_unchanged` | 两类投递统一写 actions；使用数组列和明确 kind。 |
   | 同上 | `test_upgrade_seventeen_retains_learning_batches_and_adds_nullable_detail` | 补齐旧投递结构。 |
   | `test_social_media.py` | `test_static_sticker_collect_search_dedup_and_receipt` | 补充终态拒绝更新、ID 保留、uses 不重复增加的断言。 |
   | 同上 | `test_schema_v2_upgrade_preserves_messages_actions_and_watermarks` | 新签名、数组断言及旧列重建。 |
   | `test_social_memory.py` | `test_schema_three_upgrade_preserves_observations_views_and_watermarks` | 重建旧 actions 列。 |
   | `test_social_proactive.py` | `test_schema_eleven_upgrade_preserves_source_values_and_rolls_back` | 补齐旧投递结构。 |
   | 同上 | `test_probability_modifiers_and_confirmed_message_groups` | 改用 begin/finish；调整准备数据所需额度及 sending 状态。 |
   | 同上 | `test_known_budget_exhaustion_skips_model_calls` | 改用 begin/finish。 |
   | 同上 | `test_schema_four_upgrade_preserves_memory_watermarks_and_history` | 重建旧 actions 列。 |
   | `test_social_revisions.py` | `test_schema_ten_upgrade_removes_keys_and_preserves_journals` | 补齐旧投递结构。 |
   | `test_social_runtime.py` | `test_tool_send_is_scope_checked_confirmed_and_final_is_silent` | ID 数组查询及断言。 |
   | 同上 | `test_uncertain_send_cancels_recovery_of_the_old_question` | 显式 kind。 |
   | `test_social_scheduler.py` | `test_schema_v1_migration_preserves_watermarks_and_delivery_journal` | 升级结果改断言 ID 数组。 |
   | `test_social_selection.py` | `test_recent_usage_is_sent_only_all_paths_grouped_by_turn` | 改用 begin/finish；调整准备数据时间和冷却。 |
   | `test_social_wake_v2.py` | `test_schema_seven_upgrade_preserves_an_existing_wait_and_watermarks` | 补齐旧投递结构。 |

   `tests/agent/test_telegram_social_contracts.py`、`test_social_scenarios.py` 均未修改。

5. 受影响但未修改的 scripts/ 文件

   - `scripts/evaluate_social_v5.py`：旧回执接口、旧表及单 ID 列。
   - `scripts/freeze_social_v5_baseline.py`：查询 `actions.message_id`。

   另有评估脚本待统一迁移：`nanobot/channels/telegram/tests/evaluate_social_v4.py`、`evaluate_social_followups.py`。

6. 与简报的偏差及原因

   - kind 查询按返工确认调整，其他设计未扩展。
   - 第一条 grep 无法只剩 schema：还匹配必须保留的 channel 包装、无关的 `capture_message_deliveries`、历史迁移测试及暂缓迁移的评估脚本。生产 social runtime 的两个旧方法已删除，无兼容转发。
   - 类型检查使用 `/tmp` 临时配置解决环境路径解析；未修改项目配置。
   - 未实施 P5-b/P5-c，未提交、推送或部署；未修改已有的文档和任务文件改动。

7. 未解决的问题 / 建议

   **确认存在一个简报未要求调整的边界问题**：[deliveries.py:33](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/deliveries.py:33) 的 `queue_retry` 越界分支先直接写 failed；随后 `_deliver` 调用 `store.delivery` 被终态保护拒绝，跟进回执仍为 queued。临时库复现输出：

   ```text
   Out-of-bounds retry: action=failed; delivery_changed=False; followup=queued
   remaining_attempts=1
   ```

   建议编排者明确授权将该分支的失败落账交给 Store，以同步跟进回执。本次未擅自扩展该路径。