1. 改动摘要（按文件，含行数）

已按指定设计实现，**尚未达到全部验收标准**：全量测试剩 1 项失败；另复现了旧会话采用逻辑的问题，见第 4 项。未提交、推送或部署。

- `social/bindings.py`：新增 248 行。移动原样协议文本，抽出 ScopeBindings，缓存角色文本和 revision；删除无使用者的 `SOCIAL_PROTOCOL_VERSION`。
- `social/runtime.py`：1207 → 1023 行，净减 184 行。新增 `sync_scope`，移除校验中的写库操作，按指定模式切换逻辑处理历史。
- `social/judge_runtime.py`：persona 来源改为 `bindings.resolve`。
- 测试、harness 和评估脚本：迁移调用点，更新旧行为断言，新增缓存、纯读校验及历史连续性覆盖。

`git diff --stat` 实际输出：

```text
 nanobot/channels/telegram/social/judge_runtime.py  |   2 +-
 nanobot/channels/telegram/social/runtime.py        | 268 ++++-----------------
 .../telegram/tests/evaluate_social_stickers.py     |   2 +-
 .../channels/telegram/tests/evaluate_social_v4.py  |   2 +-
 nanobot/channels/telegram/tests/social_harness.py  |   6 +
 .../telegram/tests/test_social_activity.py         |   2 +-
 .../telegram/tests/test_social_dialogue.py         |  14 +-
 .../telegram/tests/test_social_followups.py        |  16 +-
 .../channels/telegram/tests/test_social_judge.py   |   9 +-
 .../channels/telegram/tests/test_social_runtime.py |  28 ++-
 .../telegram/tests/test_social_scenarios.py        |  29 ++-
 .../telegram/tests/test_social_scheduler.py        |   3 +-
 12 files changed, 122 insertions(+), 259 deletions(-)
```

另有未暂存的新文件，普通 `git diff --stat` 不计入：

```text
 .../channels/telegram/social/bindings.py           | 248 +++++++++++++++++++++
 1 file changed, 248 insertions(+)
 .../telegram/tests/test_social_bindings.py         | 189 +++++++++++++++++++++
 1 file changed, 189 insertions(+)
```

2. 实际运行的命令及结果（原样贴最后几行）

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"

=========================== short test summary info ============================
FAILED nanobot/channels/telegram/tests/test_social_scheduler.py::test_pause_invalidates_running_tools_timers_and_pending_addresses
1 failed, 969 passed, 13 warnings in 149.22s (0:02:29)
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider

.....................                                                    [100%]
21 passed in 10.35s
```

```text
.venv/bin/ruff check nanobot/channels/telegram/

All checks passed!
```

```text
.venv/bin/basedpyright nanobot/channels/telegram/social/

971 errors, 0 warnings, 0 notes
```

原命令未解析虚拟环境依赖。使用 `/tmp/p2a-pyrightconfig.json` 继承原配置，仅显式设置 `venvPath`、`venv`、源码 `extraPaths`，没有放宽诊断：

```text
.venv/bin/basedpyright -p /tmp/p2a-pyrightconfig.json nanobot/channels/telegram/social/

0 errors, 0 warnings, 0 notes
```

以下命令均无输出；grep 退出码为 1：

```text
git diff --check
grep -rn '_persona(' nanobot tests --include='*.py'
```

设计问题复现命令：

```text
PYTHONPATH="$PWD" .venv/bin/python /tmp/p2a-design-probe.py

legacy adoption first call: history_count= 1 social_mode= None
legacy adoption second call: history_count= 0 social_mode= social
paused=True: paused
paused=False: paused
```

3. 与简报的偏差及原因；测试改动逐条列出

实现未扩展指定设计。验收偏差是全量测试仍有 1 项失败，以及原始类型检查存在环境依赖解析问题。未修改环境插件、工程配置或诊断规则。

已有测试修改如下；没有删除测试：

| 文件 | 测试名 | 旧断言 → 新断言或处理方式、原因 |
|---|---|---|
| `test_social_runtime.py` | `test_role_edit_can_be_confirmed_in_the_same_turn_and_reloads_next_turn` | 角色编辑后历史为空 → 旧历史仍可见，新 persona 生效。 |
| 同上 | `test_budget_configuration_change_invalidates_an_admitted_turn` → `test_budget_configuration_change_keeps_an_admitted_turn` | 预算变更撤销 turn → 调用 configure 后原 turn 仍可发送。 |
| 同上 | `test_role_symlink_cannot_read_private_files` | 改用 `bindings.resolve`；越界仍抛 PermissionError。 |
| 同上 | `test_replacing_role_directory_with_a_symlink_cannot_escape_root` | 改用 `bindings.resolve`；目录替换后的越界保护不变。 |
| 同上 | `test_cached_old_summary_is_not_replayed_after_role_switch` → `test_persisted_summary_is_preserved_after_role_edit` | summary 为空 → 持久化会话 summary 保留，新 persona 生效。 |
| 同上 | `test_group_wide_role_and_existing_topic_history_survive_reconfiguration` | 建立 scope 改用 `sync_scope`，读 persona 改用 `bindings.resolve`；原断言保留。 |
| `test_social_dialogue.py` | `test_paced_group_records_confirmed_context_after_last_child` | 删除配置变更后的重新 admission，直接使用原 turn，验证连续性。 |
| 同上 | `test_operator_changes_invalidate_pending_observation` → `test_operator_changes_only_invalidate_pending_observation_for_binding_changes` | background 变更取消等待 → 保留等待；增加真实 role_id 变更取消等待的覆盖。原有观察窗口到期行为保留。 |
| `test_social_followups.py` | `test_compaction_rebuild_keeps_trusted_protocol_and_injects_only_live_intentions` | persona 改用 `bindings.resolve`，断言不变。 |
| 同上 | `test_queued_parts_recheck_live_intention_before_sending` | background 变更释放预约、不发送 → 保留预约并发送；新增 role_binding 参数保留取消覆盖。 |
| `test_social_judge.py` | `test_v3b_judge_settings_change_invalidates_the_active_scope_version` → `test_v3b_judge_settings_change_preserves_the_active_scope_version_and_wait` | judge 设置改变 version、清空等待 → version、mode、wake_at、wake_config 均保留。 |
| `test_social_activity.py` | `test_all_topics_work_uses_most_recent_conversation` | 建立新 topic 改用 `sync_scope`，断言不变。 |
| `test_social_scenarios.py` | `test_role_edit_keeps_earlier_group_history_in_context` | 删除 strict xfail，现已通过。 |

新增测试：

- `test_social_scenarios.py::test_unrelated_config_change_keeps_waiting_and_history`：等待期间修改 judge 超时，后续消息仍唤醒模型，且旧消息已移出 snapshot 后仍可见。
- `test_social_bindings.py` 共 8 个测试函数、16 个参数化用例：
  - `test_role_text_is_cached_until_stat_changes`：读取缓存及文件更新。
  - `test_revisions_only_follow_persona_text_and_role_id`：revision 缓存、background、无关配置及 role_id。
  - `test_resolve_rejects_role_outside_authorized_directory_without_binding`：越界拒绝且不写库。
  - `test_resolve_rejects_unauthorized_scope`：未授权 scope。
  - `test_unavailable_role_keeps_original_placeholder`：缺失、空、超限及非法编码。
  - `test_validate_context_does_not_write_database`：包含 active claim 检查，`total_changes` 不变。
  - `test_policy_changes_preserve_history_and_pending_metadata`：配置、角色文本、background、role_id 变更保留历史及 pending metadata。
  - `test_only_social_ordinary_mode_switch_truncates_history`：仅模式切换截断历史。

辅助调用点：

- `test_social_runtime.py::context`、`test_social_scheduler.py::context`：改为 `sync_scope` 后读取 store version。
- `social_harness.py`：新增 `reconfigure(**overrides)`。
- `evaluate_social_v4.py::run_main`：改用 `bindings.resolve(...).persona`。
- `evaluate_social_stickers.py::main`：改用 `sync_scope`。
- 没有发现简报表格之外的其他运行时 `_persona` 调用点。

4. 未解决的问题 / 建议（未自行实现）

- **取消暂停无法恢复。** 指定 `sync_scope` 在 `paused=False` 时没有恢复存储状态；role_id 未变，`store.bind` 直接返回。现有 `test_pause_invalidates_running_tools_timers_and_pending_addresses` 因预期 1 个 wake、实际 0 个而失败。需编排者明确恢复逻辑；未改弱该测试。
- **旧会话第二次解析仍会截断历史。** 指定采用逻辑仅设置局部 `previous`，随后删除 `_context_version`，但没有保存 `_social_mode`。复现显示第一次保留 1 条历史，第二次变为 0 条。建议允许采用时持久化 `_social_mode`；当前严格保留简报指定逻辑。
- 原始 basedpyright 命令需编排者在正常依赖解析环境复跑；临时显式虚拟环境检查已为 0 errors。