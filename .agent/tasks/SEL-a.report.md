1. 改动摘要（按文件，净行数）

| 文件 | 改动 | 净行数 |
|---|---|---:|
| `social/selection.py` | 同步本地检索；过滤零相关度；按类限量和预算注入；移除模型调用；记录零调用、零 token | -94 |
| `social/context.py` | 删除 `select()` 的 await | 0 |
| `social/wiring.py` | 删除两处选择器 provider 配置 | -2 |
| `social/runtime.py` | 删除选择器取消调用 | -1 |
| `nanobot/config/schema.py` | 删除指定的三个配置字段 | -3 |
| `social_expression_selection.md` | 删除模板 | -20 |
| `scripts/eval/evaluate_social_selection.py` | 移除选择器 provider，断言调用数为零 | -3 |
| `scripts/eval/evaluate_social_v5.py` | 移除选择器 provider 参数及依赖 | -3 |
| `scripts/eval/evaluate_social_v5_sequences.py` | 同步调整调用参数 | -1 |
| `tests/test_social_selection.py` | 更新为本地检索测试，保留贴纸回执确认用例 | -90 |
| `tests/test_social_learning.py` | 删除已移除配置槽的测试输入 | 0 |
| `fixtures/social_v5e_actual_replay.json` | 删除三个退役配置字段，保留回放证据 | -9 |

共新增 188 行、删除 414 行，净减少 **226 行**；生产代码及模板净减少 **127 行**。改动均在允许路径内，未提交、推送或部署。

2. 实际运行的命令及结果

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
939 passed, 13 warnings in 79.81s (0:01:19)

$ .venv/bin/python -m pytest tests/config -q -p no:cacheprovider
106 passed in 3.19s

$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_selection.py -q -p no:cacheprovider
28 passed in 11.54s

$ .venv/bin/ruff check $(git diff --name-only -- '*.py')
All checks passed!
```

```text
$ .venv/bin/python -m py_compile scripts/eval/evaluate_social_selection.py scripts/eval/evaluate_social_v5.py scripts/eval/evaluate_social_v5_sequences.py
```

退出码 0，无输出。三个评估模块实际导入检查：

```text
3 eval imports passed
```

BasedPyright 使用 `/tmp` 临时配置加载仓库 strict 设置并明确指定现有 `.venv`；没有关闭诊断或修改仓库配置。

```text
$ .venv/bin/basedpyright -p /tmp/sel-a-pyright-current-explicit.json nanobot/channels/telegram/social/selection.py nanobot/channels/telegram/social/context.py nanobot/channels/telegram/social/runtime.py nanobot/channels/telegram/social/wiring.py nanobot/config/schema.py
0 errors, 0 warnings, 0 notes

$ .venv/bin/basedpyright -p /tmp/sel-a-pyright-current-explicit.json $(git diff --name-only -- '*.py')
1025 errors, 0 warnings, 0 notes
```

对 HEAD 源码副本使用相同显式检查范围：

```text
$ .venv/bin/basedpyright -p /tmp/sel-a-pyright-baseline-explicit.json $(git diff --name-only -- '*.py' | sed 's|^|/tmp/sel-a-baseline/|')
1048 errors, 0 warnings, 0 notes
```

```text
$ grep -rn "configure_provider\|selection_timeout\|social_expression_selection\|class Choice" nanobot/channels/telegram scripts
nanobot/channels/telegram/social/jargon_learning.py:62:    def configure_provider(self, config: Config) -> None:
nanobot/channels/telegram/social/expressions.py:285:    def configure_provider(self, config: Config) -> None:
```

其余命中均为这两个 learner 的调用、测试或字节码。`git diff --check` 退出码 0，无输出。

3. 与简报的偏差及原因；被删除/改写的测试清单

实现设计无偏差。验收偏差是：**所有改动 Python 文件的 BasedPyright 尚未达到 0 errors**。生产文件为零；测试与评估脚本的显式 strict 检查仍失败，未扩大范围整理类型标注。

`test_social_selection.py` 改写的测试如下：

- `test_scope_scene_keyword_pool_caps_and_same_turn_cache`：检查本地选择、单次记录和零调用。
- `test_actual_unclaimed_snapshot_selected_before_it_is_marked_seen`：改为同步调用。
- `test_changed_values_reuse_first_selection_without_second_call`：改为缓存不重复记录，删除 `provider_config` 参数。
- `test_no_model_for_empty_library_and_empty_is_valid_for_serious_help`：改为空库和无关检索的 `empty` 断言。
- `test_inflight_inactive_entry_is_dropped`：改为三类失效候选过滤。
- `test_end_to_end_sticker_without_search_receipt_and_transient_prompt`：移除选择器 provider 断言，保留贴纸确认使用。
- `test_timeout_and_provider_error_do_not_block_main_text_generation`：改为空检索仍可生成主回复。
- `test_restart_without_cache_cannot_make_a_second_call`：改为已有记录直接返回空且不重复记录。
- `test_prompt_character_budget_and_final_caps`：检查预算截断与实际记录条目一致。
- `test_valid_choices_are_truncated_to_selection_limit`：改为三类检索顺序和上限断言。

删除的模型专属测试：

- `test_invalid_content_degrades_to_empty_without_retry`
- `test_timeout_is_hard_even_if_adapter_swallows_cancellation`
- `test_inflight_input_changes_keep_selection`
- `test_selector_failure_logs_and_keeps_single_call_cache`
- `test_tolerant_selection_keeps_valid_candidates`
- `test_recorded_validation_errors_keep_sticker_selection`
- `test_invalid_envelope_stays_error`

另改写 `test_social_learning.py::test_config_contract_limits_aliases_and_independent_model_slots`，删除选择器模型配置输入。新增零相关度、禁用/无输入状态、100 条缓存上限和写库异常传播用例。

4. 未解决的问题 / 需要编排者决定的事项

- 完成标准尚有 BasedPyright 全文件零错误这一项未满足。当前错误分布为：`test_social_learning.py` 364、`test_social_selection.py` 481、三个评估脚本合计 180；基线分别为 364、504、180。需编排者决定是否另派类型整理任务。
- 三个评估脚本仍包含主模型回复评估功能，没有仅因选择器模型移除而完全失效的脚本，均已保留。
- 部署前线上 `learning.selection` 配置清理由编排者处理；本次未读取部署实例数据。