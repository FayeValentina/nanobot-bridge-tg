1. 改动摘要（按文件）

生产代码新增 47 行，净增 45 行。

| 文件 | 改动 |
|---|---|
| `social/db/messages.py` | `judge_facts` 保存 `is_bot`。 |
| `social/judge_models.py` | `JudgeMessage.is_bot` 默认 False，兼容旧行。 |
| `social/judge_runtime.py` | 读取 bot 事实，新增 `local_admission`，在 assess 前分流。 |
| `social/db/judge.py` | 新增 `decide_locally`，沿用输入、状态、时效和水位守卫，原子保存决策并调用 `_handoff`；不写 attempts。 |
| `tests/test_social_judge_runtime.py` | 新增六类测试、参数化后共 9 个用例，包含旧行兼容；调整现有准入预期及 C 层输入。 |
| `tests/test_social_dialogue.py` | 更新普通回复、条件过期及跨 topic 准入预期，保留 C 层观察测试。 |
| `tests/test_social_followups.py` | 普通回复验证 judge 零调用，从持久化批次检查上下文。 |
| `tests/test_social_media.py` | 普通真人媒体消息及成员间回复允许本地 wake。 |
| `tests/test_social_runtime.py` | 更新普通聊天及 all-topics 路由预期。 |
| `tests/test_social_scenarios.py` | 故障场景先建立选择性等待；directed 唤醒不增加 judge 调用。 |
| `tests/test_social_scheduler.py` | 更新混合批次、sleep、跨 scope 预期；保留 C 层晚到曝光复用覆盖。 |

以上路径分别位于 `nanobot/channels/telegram/social/` 和 `nanobot/channels/telegram/tests/`。

行为变化：

- 无显式选择性等待、含真人新消息的批次，以及任何 directed 批次：合并窗口到期后本地 wake，不调用 judge。
- 非 directed 的纯 bot 批次，以及存在 keywords/senders/on_question 选择性等待的非 directed 批次：继续调用 judge。
- directed 的 `fulfills_intent=False`，不消费 one-shot 条件。
- `_handoff`、合并窗口、恢复逻辑、提示词、重试、预算、schema、`judgement` 均保持现有实现。

2. 实际运行的命令及结果

pytest 使用继承环境：`PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`。

```text
$ .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"
967 passed, 13 warnings in 154.82s (0:02:34)

$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider
......................                                                   [100%]
22 passed in 11.27s

$ .venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider
.......s.....                                                            [100%]
83 passed, 2 skipped in 5.70s

$ .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scheduler.py -q -p no:cacheprovider -k 'send_collects_new_messages_once or late_judge_reuses_turn'
....                                                                     [100%]
4 passed, 64 deselected in 1.42s

$ .venv/bin/ruff check nanobot/channels/telegram
All checks passed!

$ .venv/bin/basedpyright -p /tmp/p7a-pyright.json nanobot/channels/telegram/social
0 errors, 0 warnings, 0 notes

$ git diff --check
```

最后一条无输出，退出码 0。类型检查使用的配置：

```json
{
  "typeCheckingMode": "strict",
  "pythonVersion": "3.11",
  "venvPath": "/home/ubuntu/nanobot-bridge-tg",
  "venv": ".venv",
  "extraPaths": ["/home/ubuntu/nanobot-bridge-tg"],
  "exclude": ["**/tests"]
}
```

显式 venv 配置前运行过：

```text
$ .venv/bin/basedpyright --pythonpath .venv/bin/python nanobot/channels/telegram/social
993 errors, 0 warnings, 0 notes
```

该次未解析到 `.venv` 依赖；未修改项目配置处理此问题。

调试过程中运行的其他 pytest 结果如下，后续修改后已由上述全目录通过结果覆盖：

```text
test_social_judge_runtime.py：
1 failed, 50 passed in 10.41s

test_social_judge_runtime.py + test_social_scenarios.py：
3 failed, 79 passed in 26.22s

首轮 Telegram 全目录：
23 failed, 944 passed, 13 warnings in 155.02s (0:02:35)

六个受影响测试文件：
4 failed, 329 passed, 12 warnings in 71.22s (0:01:11)
```

3. 与简报的偏差及原因（含语义性测试修改清单）

实现无设计偏差。未修改 `conftest.py`、fixtures、工程外数据；未提交、推送、构建或部署。

以下行号为修改后位置。文件均位于 `nanobot/channels/telegram/tests/`。

| 文件:行号 | 原断言 | 新断言 | 理由 |
|---|---|---|---|
| `test_social_judge_runtime.py:67` | `chat.assert_awaited_once()` | `chat.assert_not_awaited()` | 真人或 directed 走 B。 |
| `test_social_judge_runtime.py:82` | `chat.assert_awaited_once()` | `chat.assert_not_awaited()` | 本地准入绕过 judge。 |
| `test_social_judge_runtime.py:83` | `inbound.empty()` | `inbound.qsize() == 1` | 普通真人和 directed 均产生 wake。 |
| `test_social_judge_runtime.py:123` | `chat.assert_awaited_once()` | `chat.await_count == (0 if directed else 1)` | directed bot 走 B，其余 bot 走 C。 |
| `test_social_judge_runtime.py:146` | `chat.await_count == 2` | `chat.assert_not_awaited()` | 连续 directed bot 消息不调用 judge。 |
| `test_social_judge_runtime.py:147` | `inbound.empty()` | `inbound.qsize() == 1` | 仍受已有 queued wake 和冷却限制。 |
| `test_social_judge_runtime.py:256` | `qsize()==1 and await_count==2` | `qsize()==1 and await_count==0` | 本地交接事务失败后重试也不调用 judge。 |
| `test_social_judge_runtime.py:264` | `chat.await_count == 2` | `chat.assert_not_awaited()` | 两个 topic 的普通真人消息均走 B。 |
| `test_social_dialogue.py:263` | `inbound.empty()` | `inbound.qsize() == 1` | 选择条件过期后恢复普通聊天准入。 |
| `test_social_dialogue.py:310` | `chat.assert_awaited_once()` | `chat.assert_not_awaited()` | 普通回复等待走 B。 |
| `test_social_dialogue.py:323` | `inbound.empty()` | `inbound.qsize() == 1` | B topic 没有选择性等待，可以 wake。 |
| `test_social_media.py:92` | `inbound.empty()` | `inbound.qsize() == 1` | 零合并延迟下普通真人媒体消息本地入队。 |
| `test_social_media.py:205` | `inbound.empty()` | `inbound.qsize() == 1` | 成员间普通回复也走 B。 |
| `test_social_runtime.py:164` | `inbound.empty()` | `inbound.qsize() == 1` | 普通群聊本地 wake。 |
| `test_social_runtime.py:806` | `inbound.empty()` | `inbound.qsize() == 1` | all-topics 普通聊天直接 wake。 |
| `test_social_scenarios.py:89` | `len(wakes()) == 1` | `len(wakes()) == wakes + 1` | 故障前新增建立选择性等待的回合，改为增量核对。 |
| `test_social_scenarios.py:90` | `model_calls() == 1` | `model_calls() == calls + 1` | 同上，继续验证故障只增加一次主模型调用。 |
| `test_social_scenarios.py:181` | `judge_calls() == judged + 1` | `judge_calls() == judged` | directed 唤醒 sleeping bot 走 B。 |
| `test_social_scheduler.py:169` | `inbound.empty()` | `inbound.qsize() == 1` | 真人与 bot 混合批次走 B。 |
| `test_social_scheduler.py:170` | `COUNT(requests) == 0` | `COUNT(requests) == 1` | 产生一次主模型 wake 请求。 |
| `test_social_scheduler.py:171` | `chat.assert_awaited_once()` | `chat.assert_not_awaited()` | 混合批次不调用 judge。 |
| `test_social_scheduler.py:288` | `inbound.qsize() == 1` | `inbound.qsize() == 2` | A 的选择等待命中，B 的普通聊天也准入。 |
| `test_social_scheduler.py:353` | `inbound.empty()` | `inbound.qsize() == 1` | 无条件 sleep 不构成选择性等待。 |

已有测试中新增的语义断言：

| 文件:行号 | 原断言 | 新断言 | 理由 |
|---|---|---|---|
| `test_social_judge_runtime.py:127` | 无 | `snapshot.messages[-1].is_bot` | 验证平台 bot 事实传入快照。 |
| `test_social_judge_runtime.py:148` | 无 | `judge.through(...) == 2` | 连续 directed 批次仍推进水位。 |
| `test_social_judge_runtime.py:177` | 无 | `chat.assert_not_awaited()` | topic-root 真人消息走 B。 |
| `test_social_judge_runtime.py:322` | 无 | `chat.assert_not_awaited()` | standing preferences 不构成选择性等待。 |
| `test_social_judge_runtime.py:380` | 无 | `chat.assert_not_awaited()` | 临时条件过期后走 B。 |
| `test_social_judge_runtime.py:395` | 无 | `chat.assert_not_awaited()` | 普通 sleep observation 走 B。 |
| `test_social_dialogue.py:301` | 无 | `chat.assert_not_awaited()` | 普通回复本地准入。 |
| `test_social_dialogue.py:324` | 无 | 入站 `session_key == B.session_key` | 确认 A 的选择等待仍然隔离。 |
| `test_social_dialogue.py:341` | 无 | `chat.assert_not_awaited()` | 发送期间到达的普通回复走 B。 |
| `test_social_dialogue.py:386` | 无 | `chat.assert_not_awaited()` | 重启后普通回复走 B。 |
| `test_social_followups.py:352` | 无 | `chat.assert_not_awaited()` | 普通 follow-up 回复不调用 judge。 |
| `test_social_scheduler.py:289` | 无 | 两条入站 scope 集合为 `{A, B}` | 验证跨 scope 准入及隔离。 |
| `test_social_scheduler.py:575` | 无 | `chat.assert_awaited_once()` | 明确证明晚到曝光复用仍覆盖 C 层。 |

另有一处删除：`test_social_runtime.py` 旧 815–816 行的第二次 directed update 及其 `pytest.raises(ApplicationHandlerStop)` 检查。该场景现在直接消费第一条普通聊天产生的 wake，继续验证实际 topic 路由与回复。

保留原断言、调整 C 层输入的测试：

- `test_social_judge_runtime.py`：151、183、206、221、269、281、413、443、464、482、496、521、540、554 行对应测试改用非 directed bot 消息，继续覆盖合并、延迟、故障、编辑失效、恢复、图片和预算。
- `test_social_dialogue.py:347`：观察与接受回复的两个输入改为 bot 消息。
- `test_social_scheduler.py:555`：晚到 judge 测试使用非 directed bot 输入和已有 `decide` fake，覆盖 running/completed 两种曝光复用。
- `test_social_scheduler.py:193`：sleep 忽略无关消息的测试先建立 sender 选择性等待。
- `test_social_scenarios.py:80`：故障注入前先建立 sender 选择性等待，确保仍测试 C 层故障回退。

机械改动单独计数：9 处快照读取改为读取冻结批次；1 处 `mentions_bot` 断言仅替换快照变量、含义不变；7 个测试重命名；1 处模块说明更新。

4. 未解决的问题 / 需要编排者决定的事项

无。所列验收检查全部通过，改动留在工作区，待 Claude 审查、提交和部署。