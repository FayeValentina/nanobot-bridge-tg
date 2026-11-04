## 1. 改动摘要（按文件）

生产代码净减少 **60 行**：

- `nanobot/config/schema.py`：按 A 删除指定类和字段，调整校验；仅 `TelegramSocialWakeConfig` 使用 `extra="ignore"`。保留两个校验方法及错误文案，已验证直接调用仍成立。六个禁止修改的配置类源码逐字比较一致。
- `nanobot/channels/telegram/social/waiting.py`：删除 `effective()`，两处调用改为 `conditions()`，移除未使用的 import。
- `nanobot/channels/telegram/social/judge.py`：合并延迟只读取 binding/global 配置，保留 wait 覆盖；删除 `JudgeIntent.on_messages` 及 description 中的 `waiting_for_messages`。
- `nanobot/channels/telegram/manifest.py`：删除 schema import，social 默认值改为 `{"enabled": False, "scopes": []}`。
- 没有其他 social 生产文件需要修改。judge 模板相关引用为零。

以下路径均位于 `nanobot/channels/telegram/tests/`；测试逐项处理如下，重命名项列原名：

| 文件:测试名 | 处理 |
|---|---|
| `test_social_dialogue.py:test_dialogue_configuration_rejects_invalid_windows` | 改测 quiet/max 顺序；删除 replyAttention 参数用例 |
| `test_social_dialogue.py:test_dialogue_config_round_trips_in_public_manifest` | 改测最小默认字典及往返校验 |
| `test_social_judge.py:test_judge_defaults_roundtrip_through_existing_json_settings` | 解析 manifest 默认字典后比较 judge 默认值 |
| `test_social_judge.py:test_public_defaults_do_not_advertise_retired_attention_settings` | 删除失效字段断言，检查最小默认字典 |
| `test_social_judge_runtime.py:test_every_human_or_directed_batch_wakes_locally_even_with_legacy_probability_zero` | 删除概率设置并重命名 |
| `test_social_judge_runtime.py:test_local_admission_bypasses_observe_from_judge` | 删除概率设置 |
| `test_social_judge_runtime.py:test_standing_preferences_do_not_invent_a_selective_wait` | 删除常驻条件设置，重命名为 observing/active 模式测试 |
| `test_social_judge_runtime.py:test_expired_temporary_wait_does_not_fall_back_to_question_only_observation` | 删除常驻条件设置，重命名为过期后广泛准入测试 |
| `test_social_judge_runtime.py:test_sleep_observation_is_not_a_selective_question_wait` | 删除常驻条件设置 |
| `test_social_judge_runtime.py:test_state_omits_standing_preferences_from_temporary_condition` | 改为默认无临时条件测试并重命名 |
| `test_social_judge_runtime.py:test_general_reply_wait_never_inherits_standing_question_only_preference` | 删除常驻条件设置并重命名 |
| `test_social_media.py:test_album_items_keep_identity_and_share_one_wake` | 删除 wake_on_messages 设置 |
| `test_social_scheduler.py:test_operator_trigger_conditions` | 删除三个已删字段的参数用例；保留昵称用例并重命名 |
| `test_social_scheduler.py:test_directed_trigger_upgrades_keyword_without_another_job` | 删除常驻关键词设置，重命名为 conversation 升级测试 |
| `test_social_scheduler.py:test_mixed_human_bot_batch_wakes_once_without_idle_model_loops` | 删除 wake_on_messages 设置 |
| `test_social_scheduler.py:test_sleep_with_selective_wait_ignores_ambient_but_direct_address_interrupts` | 删除 wake_on_messages 设置 |
| `test_social_scheduler.py:test_sleep_admits_unseen_human_conversation_without_selective_wait` | 删除 wake_on_messages 设置 |
| `test_social_scheduler.py:test_new_configuration_fields_reject_invalid_values` | 空关键词用例改为空昵称用例 |
| `test_social_wake_v2.py:test_infinite_temporary_wait_expires_back_to_administrator_triggers` | 删除常驻关键词设置，重命名为恢复广泛准入 |
| `test_social_wake_v2.py:test_invalid_policy_configuration_is_rejected` | 删除三个只校验已删字段的参数用例 |
| `test_social_wake_v2.py:test_new_policy_defaults_preserve_legacy_sleeping_and_sampling` | 删除失效默认值断言，保留配置往返校验并重命名 |
| `test_social_wake_v2.py:test_stored_wake_conditions_ignore_retired_keys` | 新增：旧 JSON 可读，五个有效字段保留，原始 JSON 不变 |
| `test_social_wake_v2.py:test_deployed_social_config_validates_after_retired_keys_are_removed` | 新增：内联线上删键后的相关配置并验证 |
| `test_social_wake_v2.py:test_wake_probability_must_be_removed_before_deployment` | 新增：有效数值的 wakeProbability 仍触发 extra_forbidden |

夹具同步：

- `test_social_dialogue.py:dialogue`、`test_social_wake_v2.py:v2b`：删除失效字段设置。
- `conftest.py:offline_intermediary`：删除对已移除 `on_messages` 的读取。
- `social_harness.py:SocialHarness`：删除 `wakeOnMessages` 配置及对应说明。
- `test_social_scenarios.py` 未修改。

## 2. 实际运行的命令及结果

pytest 命令均使用已有沙箱插件环境：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env'
```

场景测试：

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
22 passed in 8.89s
```

社交目录首轮发现遗漏的 manifest 默认值断言，修正后完整复跑：

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob='*evaluate_*'

首轮：
FAILED nanobot/channels/telegram/tests/test_social_judge.py::test_judge_defaults_roundtrip_through_existing_json_settings
1 failed, 958 passed, 13 warnings in 120.48s (0:02:00)

最终：
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
959 passed, 13 warnings in 120.41s (0:02:00)
```

完整第二组：

```text
$ .venv/bin/python -m pytest tests/config tests/channels tests/cli/test_gateway_runtime.py tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider
=========================== short test summary info ============================
FAILED tests/channels/test_channel_setup.py::test_every_runtime_channel_field_has_a_webui_contract
FAILED tests/channels/test_websocket_listener_health.py::test_closed_socket_is_not_accepting
ERROR tests/channels/test_websocket_listener_health.py::test_real_listening_socket_is_accepting
2 failed, 394 passed, 2 skipped, 1 error in 10.63s
```

真实错误分别为：

```text
E       ModuleNotFoundError: No module named 'nh3'
E       ImportError: Matrix dependencies not installed. Run: nanobot plugins enable matrix
E       PermissionError: [Errno 1] Operation not permitted
```

manifest 专项复跑：

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_judge.py::test_judge_defaults_roundtrip_through_existing_json_settings tests/channels/test_channel_setup.py::test_channel_manifests_only_import_contract_modules -q -p no:cacheprovider
2 passed in 0.53s
```

静态检查：

```text
$ .venv/bin/ruff check nanobot/
All checks passed!

$ .venv/bin/basedpyright -p /tmp/p8-a-pyright.json nanobot/channels/telegram/social nanobot/config/schema.py nanobot/channels/telegram/manifest.py
0 errors, 0 warnings, 0 notes

$ git diff --check
```

最后一项无输出，退出码 0。保护类源码比较输出：

```text
6 protected config classes unchanged
```

生产引用检查：

```text
$ grep -rnE 'wake_keywords|wake_senders|wake_on_messages|wake_on_question|wake_probability|sleep_interrupts|reply_attention|unfinished_seconds|SleepInterruptConfig|ReplyAttentionConfig' nanobot --include='*.py' --exclude-dir=tests
nanobot/config/schema.py:121:    def validate_wake_senders(values: list[str]) -> list[str]:
nanobot/config/schema.py:152:        return TelegramSocialScopeConfig.validate_wake_senders(values)
```

其他被删属性读取、`dynamic_wait.enabled`、禁用 WakeConfig 构造参数及 judge 模板相关引用均未发现。

完整日志：[社交测试](/tmp/p9/p9b-social-final.log)、[第二组检查](/tmp/p9/p9b-contracts.log)、[类型检查](/tmp/p9/p9b-pyright.log)。

## 3. 与简报的偏差及原因

实现无设计偏差；没有修改禁改路径、线上配置或已存 SQLite 数据，没有提交、推送或部署。

验收存在两项未完全达标：

- 第二组受缺失可选依赖及沙箱 socket 权限限制。
- grep 无输出要求与 A.2 明确保留 `validate_wake_senders` 冲突；按 A.2 保留方法，剩余两处命中均为该方法名。

## 4. 未解决的问题 / 需要编排者决定的事项

- 在沙箱外补齐 Matrix 依赖并复跑第二组检查。
- grep 验收需允许上述两处方法名命中，或调整过滤条件。
- 部署前仍须由编排者删除线上指定四个旧键。