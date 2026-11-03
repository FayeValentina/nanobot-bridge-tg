1. 改动摘要（按文件），包括净行数变化

| 生产文件 | 改动 | 净行数 |
|---|---|---:|
| `social/store.py` | 删除指定死方法、旧 reason 分支和 attention 路径，简化 observe 参数 | −50 |
| `social/runtime.py` | 删除退役接口、参数和状态字段；删除告别语豁免；协议仅删除指定句子 | −16 |
| `social/proactive.py` | 删除 retired_config | −1 |
| `social/judge.py` | 删除 from_config | −6 |
| `social/schema.py` | 升级至 v20，删除两张退役表；保留 v0–v19 迁移原文 | +5 |
| `agent/tools/telegram_social.py` | 删除 probability 参数、转发及说明 | −1 |

生产代码净减少 **69 行、6 个函数**；全部改动净减少 **108 行**。未提交、推送或部署。

`git diff --stat`：

```text
 nanobot/agent/tools/telegram_social.py             |  5 +-
 nanobot/channels/telegram/social/judge.py          |  6 --
 nanobot/channels/telegram/social/proactive.py      |  1 -
 nanobot/channels/telegram/social/runtime.py        | 28 ++-------
 nanobot/channels/telegram/social/schema.py         |  7 ++-
 nanobot/channels/telegram/social/store.py          | 68 +++-------------------
 .../telegram/tests/evaluate_social_judge.py        | 11 +++-
 .../telegram/tests/test_reply_reliability.py       |  2 +-
 .../telegram/tests/test_social_dialogue.py         | 43 +-------------
 .../telegram/tests/test_social_judge_runtime.py    |  3 +-
 .../telegram/tests/test_social_learning.py         | 11 ----
 .../channels/telegram/tests/test_social_media.py   |  2 +-
 .../channels/telegram/tests/test_social_memory.py  |  2 +-
 .../telegram/tests/test_social_proactive.py        |  2 +-
 .../channels/telegram/tests/test_social_runtime.py | 27 ++++-----
 .../channels/telegram/tests/test_social_scheduler.py | 11 +++-
 .../channels/telegram/tests/test_social_wake_v2.py |  3 +-
 17 files changed, 62 insertions(+), 170 deletions(-)
```

2. 实际运行的命令及结果

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"

927 passed, 1 xfailed, 13 warnings in 145.66s (0:02:25)
```

```text
.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider

........................                                                 [100%]
24 passed in 3.05s
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_dialogue.py -q -p no:cacheprovider

...................................................                      [100%]
51 passed in 13.56s
```

```text
.venv/bin/ruff check nanobot/channels/telegram/ nanobot/agent/tools/telegram_social.py

All checks passed!
```

原始类型检查命令未加载虚拟环境依赖：

```text
.venv/bin/basedpyright nanobot/channels/telegram/social/ nanobot/agent/tools/telegram_social.py

1000 errors, 0 warnings, 0 notes
```

使用 `/tmp/p1a_pyrightconfig.json` 继承原配置，仅显式指定 venvPath、venv 和源码 extraPaths，未调整诊断规则：

```text
.venv/bin/basedpyright -p /tmp/p1a_pyrightconfig.json nanobot/channels/telegram/social/ nanobot/agent/tools/telegram_social.py

0 errors, 0 warnings, 0 notes
```

```text
grep -rnE "dialogue_attention|wake_samples|'nickname'|_quiet_seconds|allows_personal_delivery|from_config|retired_config" nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py

nanobot/channels/telegram/social/schema.py:182:            CREATE TABLE dialogue_attention (
nanobot/channels/telegram/social/schema.py:203:            CREATE TABLE wake_samples (
nanobot/channels/telegram/social/schema.py:327:            store.db.execute("DROP TABLE IF EXISTS dialogue_attention")
nanobot/channels/telegram/social/schema.py:328:            store.db.execute("DROP TABLE IF EXISTS wake_samples")
nanobot/channels/telegram/social/schema.py:362:            tables = ("views", "wakes", "actions", "proactive_state", "message_deliveries", "wake_samples", "judge_batches")
```

`git diff --check`：退出码 0，无输出。

3. 与简报的偏差及原因；测试改动逐条列出

- 实现范围和设计无偏差。实际生产净减少 69 行，低于预估的 100–200 行；未为达到预估规模扩大删除范围。
- 类型检查使用临时环境配置取得零错误；原命令的环境问题保留说明。
- 首轮完整测试为 `1 failed, 926 passed, 1 xfailed`：修改告别语测试时误改了“已安静超过窗口”的断言。已恢复该断言，最终完整重跑通过。
- `test_social_scenarios.py`、`social_harness.py`、契约测试文件均未改动。

以下文件均位于 `nanobot/channels/telegram/tests/`：

| 文件 | 测试名或辅助函数 | 处理方式及原因 |
|---|---|---|
| `test_social_runtime.py` | `context()` | 替换辅助函数内部搭建代码：SQL 插入 wake 和 request，替代已删除的 wake() |
| 同上 | `test_personal_heartbeat_can_choose_a_social_group` | 删除：直接依赖 allows_personal_delivery |
| `test_social_scheduler.py` | `context()` | 替换辅助函数内部搭建代码：等价 SQL 替代 wake() |
| `test_social_learning.py` | `test_explicit_delete_invalidates_sources_without_rewinding_main_watermarks` | 删除：专门测试 delete_observed |
| `test_social_judge_runtime.py` | `test_every_new_batch_reaches_model_even_with_legacy_probability_zero` | 改断言：wake_samples 空表改为表不存在 |
| 同上 | `test_state_distinguishes_retired_configuration_and_effective_preferences` | 改断言：删除 retired_config 断言，保留其他断言 |
| `test_social_dialogue.py` | `test_send_confirmed_after_configuration_change_does_not_create_attention` | 删除：测试退役 attention |
| 同上 | `test_ordinary_receipt_from_old_configuration_does_not_create_attention` | 删除：测试退役 attention |
| 同上 | `test_only_confirmed_sends_establish_attention` | 删除：测试退役 attention |
| 同上 | `test_ordinary_message_receipt_records_confirmed_context_once` | 改断言：删除 attention 断言，保留回执和上下文断言 |
| 同上 | `test_paced_group_records_confirmed_context_after_last_child` | 改断言：删除两处 attention 断言，保留发送及时间断言 |
| 同上 | `test_explicit_farewell_can_sleep_directly` | 改名为 `test_explicit_farewell_enters_sleep_observation`，断言进入 observe |
| 同上 | `test_schema_six_upgrade_preserves_data_and_unknown_versions_are_rejected` | 修改迁移搭建：不再 DROP 已不存在的退役表 |
| `test_reply_reliability.py` | `test_schema_five_upgrade_adds_receipts_without_losing_history` | 修改迁移搭建：移除退役表 DROP |
| `test_social_media.py` | `test_schema_v2_upgrade_preserves_messages_actions_and_watermarks` | 修改迁移搭建：移除退役表 DROP |
| `test_social_memory.py` | `test_schema_three_upgrade_preserves_observations_views_and_watermarks` | 修改迁移搭建：移除退役表 DROP |
| `test_social_proactive.py` | `test_schema_four_upgrade_preserves_memory_watermarks_and_history` | 修改迁移搭建：移除退役表 DROP |
| `test_social_wake_v2.py` | `test_schema_seven_upgrade_preserves_an_existing_wait_and_watermarks` | 修改迁移搭建：移除 wake_samples DROP |
| 同上 | `test_tools_accept_infinite_wait_and_new_temporary_conditions` | 移除已删除的 probability 入参，保留返回值断言 |
| `evaluate_social_judge.py` | `evaluate()` | 评估场景内用 SQL 替代 wake()；未运行真实模型评估 |

同时删除测试中因此未使用的 import；未新增测试函数。

4. 未解决的问题 / 建议

原始 `basedpyright` 命令在当前环境中无法解析已有依赖；临时配置检查已为零错误，建议编排者在正常环境复跑原命令。无其他已知代码问题。