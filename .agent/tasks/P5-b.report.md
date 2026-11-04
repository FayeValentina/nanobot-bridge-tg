1. 改动摘要（按文件）

| 文件 | 改动 | 生产代码净行数 |
|---|---|---:|
| `social/runtime.py` | 新增 `_check_send_allowed`，三个入口统一调用，保留各自重复/幂等语义 | +2 |
| `social/store.py` | 删除指定许可检查及两个 `limit` 参数，保留写入和多段专有前提 | −12 |
| `social/db/deliveries.py` | 删除重复查询 `unknown_part` | −3 |

以上路径均位于 `nanobot/channels/telegram/`。生产代码合计新增 28 行、删除 41 行，**净减少 13 行**。测试侧仅作下述机械修改。未提交、推送或部署。

2. 三项行为变化的落点

| 行为变化 | 落点 |
|---|---|
| 三组旧文案统一为指定文案 | [runtime.py:598](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:598)、[runtime.py:601](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:601)、[runtime.py:603](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:603) |
| `begin_message` 同 turn 已尝试检查提前到统一检查之前 | [runtime.py:612](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:612) |
| `send`/`send_parts` 接入统一暂停检查；原 `validate_context` 暂停拒绝保留 | [runtime.py:632](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:632)、[runtime.py:680](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:680)，检查位于 [runtime.py:591](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/runtime.py:591) |

3. 实际运行的命令及结果

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
984 passed, 13 warnings in 145.58s (0:02:25)
```

运行时继承编排环境：`PYTHONPATH=.agent`、`PYTEST_ADDOPTS=-p codex_pytest_env`。

```bash
.venv/bin/ruff check nanobot/
```

```text
All checks passed!
```

```bash
.venv/bin/basedpyright nanobot/channels/telegram/social
```

```text
0 errors, 0 warnings, 0 notes
```

`git diff --check`：退出码 0，无输出。

```bash
grep -n "send cooldown is active\|awaiting\|blindly repeat\|hourly send budget\|social conversation is paused" -r nanobot/channels/telegram/social --include=*.py
```

```text
nanobot/channels/telegram/social/runtime.py:305:            raise PermissionError("social conversation is paused")
nanobot/channels/telegram/social/runtime.py:592:            raise PermissionError("social conversation is paused")
nanobot/channels/telegram/social/runtime.py:596:            raise PermissionError("send cooldown is active")
nanobot/channels/telegram/social/runtime.py:598:            raise PermissionError("an earlier send is awaiting delivery")
nanobot/channels/telegram/social/runtime.py:601:                raise PermissionError("delivery of this text is unknown; do not blindly repeat it")
nanobot/channels/telegram/social/runtime.py:603:            raise PermissionError("hourly send budget exhausted")
```

305 行是 `validate_context` 原有暂停文案；其余均在 `_check_send_allowed` 内，各出现一次。

4. 修改过的测试（逐条）

以下路径均位于 `nanobot/channels/telegram/tests/`：

| 文件:行号 | 机械修改 |
|---|---|
| `test_social_scheduler.py:559` | `match="all parts"` 改为 `match="hourly send budget exhausted"` |
| `test_social_runtime.py:567` | 删除 `reserve_send` 的 `limit=6` 位置实参 |
| `test_social_proactive.py:130` | 删除 `limit=60` 位置实参 |
| `test_social_media.py:500` | 删除 `limit=6` 位置实参 |
| `test_social_followups.py:445` | 删除 `limit=60` 位置实参 |
| `social_harness.py:276` | 删除 `self._config.max_sends_per_hour` 位置实参 |
| `evaluate_social_followups.py:91` | 删除 `limit=60` 位置实参 |
| `evaluate_social_v4.py:157` | 删除 `limit=60` 位置实参 |

`test_social_scenarios.py` 未修改，已随指定测试集运行；`evaluate_*` 按指定命令排除。

5. 与简报的偏差及原因

无设计或范围偏差。实际净减少 13 行，低于预估的约 20 行；保留了简报要求的方法签名、文档字符串和原有调用格式。

`_deliver`、`_retry_sends`、`claim_retry`、`queue_retry`、`validate_context` 及 Telegram channel 均通过源码直接比较确认未改动。

6. 未解决的问题 / 建议

无本任务阻塞项。测试产生 13 条弃用及配置提示，本次未处理。