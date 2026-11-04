**1. 改动摘要（按文件）**

- `learning_worker.py`：统一批次选择、检查、互斥、取消、异常日志及失败记账；供应商错误退款。
- `expressions.py`：生命周期移出，保留 `process` 业务与用量记账。
- `jargon_learning.py`：取消继承及无用 expression library；保留逐次 ask 用量记账、业务处理和 latency。
- `learning.py`：新增指定的 `refund_attempt` SQL。
- `runtime.py`、`wiring.py`：接入统一 worker。
- 四个 evaluate 脚本仅机械替换，共 5 处调用；未单独运行评估脚本。

测试适配计数：

| 文件 | 既有适配 | 新增 |
|---|---|---|
| `test_social_expressions.py` | 4 个用例、9 行入口/引用替换 | 无 |
| `test_social_jargon.py` | 3 个用例及 1 个辅助函数 | 退款 12 例、排序 2 例 |
| `test_social_learning.py` | 1 个参数化测试、2 处调用 | 加强失败计数断言 |
| `test_social_v5e_replay.py` | 1 个用例，mock 改为 process 签名 | 无 |

既有断言的语义调整仅在共享任务测试：

- 原“两份 task 中 jargon.task 为 None”改为检查同一 worker.task、jargon 未调用，以及 expression running / jargon ready。
- 间隔内原“jargon.task 为 None”改为共享 task 保持原值、jargon 未调用。
- 并发场景增加阻塞中的 provider 调用，确认实际运行期间仍只处理一个批次。

timeout / ValueError / 其他异常由 `test_social_learning.py:380` 的六个参数组合继续覆盖。

`wc -l` 对比：

| 文件 | 修改前 | 修改后 |
|---|---:|---:|
| expressions.py | 365 | 312 |
| jargon_learning.py | 237 | 226 |
| learning_worker.py | 0 | 106 |
| runtime.py | 363 | 360 |

**2. 实际运行的命令及结果**

pytest 使用会话已有的 `PYTHONPATH=.agent`、`PYTEST_ADDOPTS="-p codex_pytest_env"`。

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"

933 passed, 13 warnings in 115.09s (0:01:55)
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider

22 passed in 10.30s
```

```text
.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway tests/cli/test_gateway_runtime.py -q -p no:cacheprovider

97 passed, 2 skipped in 6.56s
```

```text
.venv/bin/ruff check nanobot/ scripts/

All checks passed!
```

```text
.venv/bin/basedpyright -p /tmp/p7c-pyrightconfig.json

0 errors, 0 warnings, 0 notes
```

```text
grep -n "peer\|learning_task" nanobot/channels/telegram/social/*.py

无输出；退出码 1，0 命中。
```

`git diff --check` 无输出，通过。

**3. 与简报的偏差及原因**

- 生产代码净增 **41 行**，未达到净减少预期：原文件合计减少 65 行，新增 worker 106 行。增量包含独立模块导入、类型声明及 jargon 显式 provider 配置代码。
- 直接运行 basedpyright 出现依赖解析失败，报告 803 errors。临时配置复制项目类型检查选项，并显式指定现有 `.venv` 和仓库路径后得到 0 errors；未关闭诊断或修改工程配置。
- 未提交、推送、构建或部署；未读写线上库。

**4. 卡住批次的分析与 SQL（只报告）**

依据简报给出的状态，`54c6bbb2aea24b1b8a1acc8e96cdc17d` 为 expression、failed、attempts=2。`prepare` 将 failed 当作活动批次并返回原 ID，而调度器排除已耗尽次数的批次。因此，在 scope/config 不变时，它会持续阻止新 expression 批次创建，重启不能解除。

如决定恢复完整两次尝试预算，可执行以下 SQL，**本次未执行**：

```sql
UPDATE learning_batches
SET status = 'ready',
    attempts = 0,
    updated = unixepoch('now')
WHERE id = '54c6bbb2aea24b1b8a1acc8e96cdc17d'
  AND kind = 'expression'
  AND status = 'failed'
  AND attempts = 2;
```

这只重排原批次：游标、sources、批次窗口、历史失败计数及错误详情均保留。成功后才按原批次推进游标；不会把后来的消息合入此次来源。

**5. 未解决的问题 / 需要编排者决定的事项**

- `last_batch_due` 查询限定 `attempts>0`。首次供应商错误退款至 0 后，该批次虽保留 due，却不再贡献全局间隔，可能立即再次调度。本次按简报保留查询，需要编排者决定后续修正。
- 请编排者确认是否接受生产代码净增 41 行的规模偏差。