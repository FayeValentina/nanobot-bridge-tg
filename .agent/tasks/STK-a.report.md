1. 改动摘要（按文件，含净行数）

以下 `social/`、`tests/` 均位于 `nanobot/channels/telegram/`。

| 文件 | 改动 | 净行数 |
|---|---|---:|
| `social/db/stickers.py` | 新增完整 `index()`，删除候选检索 | −2 |
| `social/context.py` | 注入 `sticker_library` | +1 |
| `social/selection.py` | 移除贴纸候选、分组和确认归因 | −12 |
| `social/db/deliveries.py` | 删除无调用者的 `sent_sticker()` | −3 |
| `social/delivery.py` | 删除贴纸归因及 selector 依赖 | −6 |
| `social/runtime.py` | 同步投递构造参数 | 0 |
| `social/learning.py` | 删除 `confirm_use()`、`selected_sticker()` | −22 |
| `nanobot/config/schema.py` | 删除两个贴纸选择配置项 | −2 |
| `nanobot/templates/telegram/social_protocol.md` | 原样替换指定两行 | 0 |
| `tests/test_social_stickers.py` | 新增索引测试 | +15 |
| `tests/test_social_selection.py` | 更新选择和回执断言，新增 live 索引测试 | +14 |
| `tests/test_social_learning.py` | 删除失效归因测试 | −17 |
| `tests/test_social_jargon.py` | 删除失效归因测试 | −11 |
| `tests/fixtures/social_v5e_actual_replay.json` | 删除已移除配置名 | −2 |

合计新增 69 行、删除 116 行，净减少 47 行；生产 Python 净减少 46 行。

2. 实际运行的命令及结果

测试继承编排环境：`PYTHONPATH=.agent`、`PYTEST_ADDOPTS=-p codex_pytest_env`。

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"

931 passed, 13 warnings in 76.35s (0:01:16)
```

```text
.venv/bin/python -m pytest tests/config -q -p no:cacheprovider

106 passed in 2.67s
```

索引用例改为逆序导入后单独复跑：

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_stickers.py::test_sticker_index_only_includes_annotated_entries_in_scope_sorted_by_id -q -p no:cacheprovider

1 passed in 0.44s
```

```bash
mapfile -t changed_python < <(git diff --name-only -- '*.py')
.venv/bin/ruff check "${changed_python[@]}"
```

```text
All checks passed!
```

```bash
.venv/bin/basedpyright -p /tmp/stk_a_pyrightconfig.json \
  nanobot/channels/telegram/social/db/stickers.py \
  nanobot/channels/telegram/social/db/deliveries.py \
  nanobot/channels/telegram/social/context.py \
  nanobot/channels/telegram/social/selection.py \
  nanobot/channels/telegram/social/delivery.py \
  nanobot/channels/telegram/social/learning.py \
  nanobot/channels/telegram/social/runtime.py \
  nanobot/config/schema.py
```

```text
0 errors, 0 warnings, 0 notes
```

临时配置继承原 `pyproject.toml`，仅指定现有 `venvPath`、`venv` 和源码 `extraPaths`，未放宽诊断。未指定该配置及使用 `--pythonpath` 的检查均因依赖解析失败返回：

```text
394 errors, 0 warnings, 0 notes
```

```text
grep -rn "confirm_sticker\|selection_stickers\|selection_sticker_candidates\|stickers.candidates" nanobot scripts
```

无输出，退出码 1。全仓库搜索 `confirm_use()`、`selected_sticker()`、`sent_sticker()` 定义及调用也无结果。`git diff --check` 无输出，退出码 0。

3. 与简报的偏差及原因；被删除/改写的测试清单

无实现设计偏差。类型检查使用上述临时环境配置解决依赖解析问题；未改工程配置或环境补偿插件。

删除：

- `test_usage_requires_real_receipt_and_keeps_human_occurrences_unchanged`：依赖已删除的 `confirm_use()`，共 8 个参数组合。
- `test_understanding_only_cannot_be_attributed_as_natural_use`：依赖已删除的 `confirm_use()`。

改写：

- `test_scope_scene_keyword_pool_caps_and_same_turn_cache`：候选和记录数量仅包含表达、黑话。
- `test_changed_values_reuse_first_selection_without_second_record`：移除贴纸注释参数，配置变更改用表达配置。
- `test_zero_relevance_candidates_are_not_injected`：删除贴纸候选查询。
- `test_inactive_or_changed_candidate_is_filtered`：移除贴纸参数。
- `test_end_to_end_sticker_without_search_receipt_and_transient_prompt`：从完整库选择原检索不命中的贴纸，核对发送回执、最近使用记录及上下文不持久化。
- `test_prompt_character_budget_and_final_caps`：调整两类候选的数量断言。
- `test_retrieval_order_is_truncated_to_each_kind_limit`：仅检查表达、黑话，并断言候选不含贴纸。

新增索引及 live 上下文测试，覆盖 scope 隔离、非空注释、ID 排序、原注释保留、完整注入、学习关闭时注入及注释刷新。未发现需要机械更新的旧 protocol 文本断言。

4. 未解决的问题 / 需要编排者决定的事项

无功能遗留或待决事项。未提交、未推送、未部署，未读取部署实例数据。