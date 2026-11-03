**1. 改动摘要（按文件，含净行数）**

| 文件 | 改动 | 净行数 |
|---|---|---:|
| `social/memory.py` | 提取 `displayed_evidence`；删除黑话模式、自嵌套及旧字段；固定表名；合并列表实现；全量 clear 调用黑话库 | −40 |
| `social/jargon.py` | 删除 legacy；独立验证黑话来源；新增无嵌套事务的 clear | −44 |
| `social/runtime.py` | 更新调用签名及指定协议文字 | 0 |
| `tests/test_social_memory.py` | 更新接口测试，覆盖联合清除及旧 kind 参数错误 | +30 |
| `tests/test_social_jargon.py` | 适配新签名，改写旧接口测试 | +3 |
| `tests/test_social_learning.py` | 使用黑话新接口保留来源生命周期覆盖 | 0 |

路径均位于 `nanobot/channels/telegram/`。**生产代码新增 91 行、删除 175 行，净减少 84 行。**

`git diff --stat` 限定上述六个文件的原始输出：

```text
 nanobot/channels/telegram/social/jargon.py         | 112 +++++-----------
 nanobot/channels/telegram/social/memory.py         | 148 ++++++++-------------
 nanobot/channels/telegram/social/runtime.py        |   6 +-
 .../channels/telegram/tests/test_social_jargon.py  |  63 ++++-----
 .../telegram/tests/test_social_learning.py         |  22 +--
 .../channels/telegram/tests/test_social_memory.py  |  72 +++++++---
 6 files changed, 186 insertions(+), 237 deletions(-)
```

**2. 实际运行的命令及结果**

测试继承编排环境的 `PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`，未修改环境插件。

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider --ignore-glob="*evaluate_*" > /tmp/p1b-pytest.log 2>&1
tail -2 /tmp/p1b-pytest.log
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
952 passed, 1 xfailed, 13 warnings in 158.99s (0:02:38)
```

```bash
.venv/bin/ruff check nanobot/channels/telegram/
```

```text
All checks passed!
```

```bash
.venv/bin/basedpyright nanobot/channels/telegram/social/
```

```text
0 errors, 0 warnings, 0 notes
```

```bash
grep -rnE 'is_jargon|jargon=True|\.legacy\(|_list_local|jargon_status' nanobot tests
```

无输出，退出码 1（无匹配）。

```bash
git diff --check
```

无输出，退出码 0。

**3. 与简报的偏差及原因；测试改动逐条列出**

无设计偏差。`re` 仍被 `discover` 使用，因此保留。`learning.py`、`test_social_scenarios.py`、`social_harness.py` 和 agent contracts 未修改。未提交、推送或部署。

工作期间 `.agent/orchestration.md` 出现外部改动，我未编辑或回退该文件；上述统计不包含它。

以下测试路径均位于 `nanobot/channels/telegram/tests/`。无整项测试删除，四项更名改写。

`test_social_memory.py`：

- `test_all_knowledge_kinds_have_provenance_and_candidate_status`：移除 slang 参数分支，保留普通记忆溯源覆盖。
- `test_clear_covers_all_observed_old_sources_and_allows_new_evidence`：参数化全量/指定 kind 清除；覆盖联合计数、黑话字段清空、suppression、reset 和变更日志。
- `test_active_limit_does_not_silently_evict_and_queries_page`：通过新接口创建黑话，保留共享容量和普通记忆分页覆盖。
- `test_tool_boundary_rejects_ambiguous_or_unbounded_payloads`：改用有效的 topic kind，使各非法字段仍被独立验证。
- `test_delete_refreshes_live_index_on_next_iteration_and_next_wake` → `test_disable_refreshes_live_index_on_next_iteration_and_next_wake`：改用 `social_jargon` explain/disable，检查黑话实时上下文刷新。
- `test_memory_tool_is_in_shared_catalog_and_errors_are_clear`：改用 topic，继续验证未展示证据的错误。
- 新增 `test_memory_tool_rejects_slang_kind_as_parameter_validation_error`：验证旧 kind 返回 Pydantic 参数错误且不写入记录。

`test_social_jargon.py`：

- `test_explicit_correction_overwrites_inference_and_old_windows_cannot_restore_it`：删除旧 memory 参数。
- `test_manual_query_submission_and_review_never_wake_or_search_foreground`：适配新签名。
- `test_correction_during_web_research_rejects_stale_result`：适配新签名。
- `test_legacy_slang_uses_same_ids_and_corrections_are_versioned` → `test_jargon_uses_same_ids_and_corrections_are_versioned`：通过 explain/correct/submit 验证稳定 ID、修订和旧来源抑制。
- `test_local_and_public_meanings_remain_separate_on_conflict`：适配新签名。
- `test_operator_correction_is_locked_and_scope_and_revision_are_checked`：适配新签名，保留原检查。
- `test_legacy_candidate_can_be_confirmed_with_explicit_human_evidence` → `test_candidate_can_be_explained_with_explicit_human_evidence`：使用 submit/explain/list/get，删除旧自由文本释义语义。
- `test_understanding_only_cannot_be_attributed_as_natural_use`：适配新签名。

`test_social_learning.py`：

- `test_slang_proxy_edit_delete_and_no_legacy_rows` → `test_jargon_edit_disable_and_no_legacy_rows`：使用新接口查询和禁用，保留编辑失效及无旧表记录检查。
- `test_slang_rejects_bot_and_stale_view_sources`：改用新接口，保留 bot 和过期曝光来源拒绝覆盖。

辅助函数 `expose` 删除自嵌套对象返回值；`jargon_record` 改用新接口。后者也服务于 `test_late_edit_after_observation_and_material_eviction_keeps_monotonic_revision`，该测试正文未改，已随指定测试集通过。

**4. 未解决的问题 / 建议**

无未解决问题；无额外实现建议。