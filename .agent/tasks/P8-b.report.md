1. 改动摘要（按文件）

生产 Python 代码净减少 **48 行**。

| 文件 | 改动 |
|---|---|
| `nanobot/agent/tools/telegram_social.py` | 合并为指定的 9 个工具；description 逐字采用提供文件，最长 399 字符。send 支持 text/sticker_id/parts 三选一及混合 parts；wait 增加 until_condition；state 合并 get/read/set_proactive；删除旧工具类及 sticker.send。 |
| `social/waiting.py` | 删除 set_wake，拆成 set_conditions/set_proactive。暂停主动联系不再受 allow_ambient_wake 限制，继续持久保存。 |
| `social/models.py` | 新增指定的 SendPart。 |
| `social/delivery.py` | 抽出静态贴纸解析；单贴纸发送透传 followup_id；parts 支持文本和贴纸，各自检查 sender。 |
| `social/store.py` | enqueue_parts 保存每段媒体引用及 kind。 |
| `social/db/deliveries.py` | insert_part 写入既有 kind/media_ref 列；没有修改 schema 或迁移。 |
| `social/runtime.py` | 更新工具集合，删除 runtime.mark_read；get_state 仅返回指定的 14 个键，使用 until_condition、proactive_paused、quiet_hours_active，删除其余旧字段。 |
| `social/hook.py` | 发布尝试识别改为 social_send 或既有 message 分支。 |
| `templates/telegram/social_protocol.md` | 逐字节复制编排者提供的 protocol。 |
| `templates/telegram/autonomous_activity.md` | 仅将读取消息调用改为 social_state(action="read")。 |

上表 `social/` 均指 `nanobot/channels/telegram/social/`。已核实以下函数没有改变：`_deliver`、`retry_due`、`_confirmed_send`、`_check_send_allowed`、`_collect_new_messages`。依据：

- [delivery.py:207](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/delivery.py:207) 按 kind/media_ref 读取贴纸，215、218 行选择 sticker_sender 并发送 file_id。
- [delivery.py:297](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/delivery.py:297) 的 retry_due 继续调用同一 _deliver。
- [delivery.py:259](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/delivery.py:259) 保留确认贴纸使用的副作用；[store.py:270](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/store.py:270) 保留 uses 计数。

测试逐文件改动如下，除最后一项外均位于 `nanobot/channels/telegram/tests/`：

| 文件 | 改动 |
|---|---|
| `test_reply_reliability.py` | 条件调用迁移；更新三选一 schema 和错误文案断言；补充含 sticker_id 的互斥组合。 |
| `test_social_dialogue.py` | 条件调用迁移；parts 使用 SendPart；水位测试直接调用保留的 store.mark_read。 |
| `test_social_followups.py` | parts、暂停调用迁移；followups 明细改从 store 读取；新增单贴纸 followup 关联及确认测试。 |
| `test_social_judge_runtime.py` | 条件调用迁移；更新精简状态断言；原 on_messages-only 场景改为清除条件后的普通准入。 |
| `test_social_media.py` | get_media 改为 social_media；发送输入检查迁移到 social_send。 |
| `test_social_proactive.py` | 暂停、parts、水位调用迁移；新增 ambient 禁用时暂停并跨重启持久化测试。 |
| `test_social_runtime.py` | get/read 调用及 protocol 文案迁移；删除手动 mark_read 脚本；新增精确工具集合、description 长度、状态键集合及 paused 必填测试。 |
| `test_social_scenarios.py` | 仅修改简报允许的位置：三个等待场景、两个自动已读场景、贴纸发送场景。其余断言未改。 |
| `test_social_scheduler.py` | 条件、read、parts、水位调用迁移；parts schema 增加贴纸对象。 |
| `test_social_selection.py` | 一处旧 sticker.send 改为 social_send(sticker_id=...)。 |
| `test_social_stickers.py` | 发送调用迁移；新增混合 parts 的入队类型、发送顺序、file_id、回执复用、uses +1 和扣除两次用量测试。 |
| `test_social_wake_v2.py` | 条件、水位调用迁移；删除已撤销的模型 coalesce override 检查，保留 operator coalesce 测试；新增 wait 写入及清除条件测试。 |
| `tests/cli/test_gateway_runtime.py` | 注册数量断言从 12 改为 9。 |

依赖旧 get_state 字段的断言逐条处理：

- 删除 `tool_permissions == "shared_with_normal_chat"`；共享工具集合断言保留。
- `judge.intent is None` 改为 `until_condition is None`。
- 删除 `judge.participation.on_question` 断言。
- 删除 `base_wake` 不含 probability 的断言。
- 删除 `dialogue_config` 不含 reply_attention 的断言。
- 删除 `dialogue_config.dynamic_wait` 不含 unfinished_seconds 的断言。
- followups 明细改从 `store.followups.status()` 获取，原事项状态和尝试记录断言保留。

2. 实际运行的命令及结果（原样贴最后几行）

pytest 均使用既有沙箱插件前缀：
`PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env'`。

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
958 passed, 13 warnings in 122.74s (0:02:02)
```

```text
$ .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider
........................................................................ [100%]
144 passed in 5.15s
```

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
......................                                                   [100%]
22 passed in 9.61s
```

另对六项新增行为测试做过定点检查：

```text
......                                                                   [100%]
6 passed in 1.60s
```

```text
$ .venv/bin/ruff check nanobot/
All checks passed!
```

默认类型检查结果：

```text
$ .venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py
790 errors, 0 warnings, 0 notes
```

默认命令无法解析 pydantic、loguru 等 venv 依赖。沿用上次显式配置：

```text
$ .venv/bin/basedpyright -p /tmp/p8-a-pyright.json nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py
0 errors, 0 warnings, 0 notes
```

以下命令均无输出：

```bash
cmp nanobot/templates/telegram/social_protocol.md .agent/tasks/P8-b.social_protocol.md
git diff --check
rg -n 'social_(get_state|read_messages|mark_read|set_wake|get_media)\b' nanobot --glob '!**/tests/**'
rg -n 'social_(get_state|read_messages|mark_read|set_wake|get_media)\b' nanobot/channels/telegram/tests tests --glob '!evaluate_*' --glob '!**/fixtures/**' --glob '!test_social_v5e_replay.py'
```

完整 tests 检索仅剩：

```text
nanobot/channels/telegram/tests/evaluate_social_followups.py:77
nanobot/channels/telegram/tests/evaluate_social_v4.py:252
nanobot/channels/telegram/tests/fixtures/social_v5e_baseline_code.json:3
```

首轮检查发现的测试导入、关联列、漏迁移调用及注册数量问题均已修正，以上为最终复跑结果。

3. 与简报的偏差及原因

无实现设计偏差。仅修改允许路径；未修改冻结基线、评估脚本、schema、judge 或普通 message 工具。未新增兼容层、迁移或防御性分支。

默认 basedpyright 的环境问题通过指定既有 venv 配置核验，未修改项目配置或环境 shim。按编排模式未提交、推送或部署。

4. 未解决的问题 / 需要编排者决定的事项

无待决设计事项。默认 basedpyright 的依赖解析问题仍存在；显式 venv 检查为零错误。评估脚本中的旧工具引用按简报保留，待后续迁移。