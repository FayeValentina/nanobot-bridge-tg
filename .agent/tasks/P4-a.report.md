1. 改动摘要（按文件）

- `social/models.py`：`Snapshot` 最后增加 `view_end`。
- `social/store.py`：快照字典最后写入 `view_end`；`note_seen` 按完整首尾标记识别 view_id。
- `social/db/views.py`：新增容许冒号两侧空白的 `complete_view_ids`；`unseen` 按 ID 查询，空集合直接返回；`turn_bodies` 首列增加 ID；曝光排序增加 rowid。
- `social/selection.py`：按完整 view_id 选择快照，使用具名列。
- `social/db/wakes.py`：提取 `_enter_running`，统一 claim 与曝光收取后的 scope／等待清理。
- `tests/test_social_runtime.py`：新增一个默认分隔符重编码测试。

生产代码净增 **24 行**。未修改 hook、登记时机、schema、`SCHEMA_VERSION`、`memory.context` 或 `latest_body`。

2. 三项行为变化各自的落点（文件:行号）

- 快照末尾增加 `view_end`：[models.py:120](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/models.py:120)、[store.py:177](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/store.py:177)。
- 缺结尾仍不登记；完整识别只依赖首尾标记，兼容默认 JSON 空白：[views.py:14](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/views.py:14)、[store.py:189](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/store.py:189)、[selection.py:126](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/selection.py:126)。
- 曝光收取 wake 后激活 scope、清空 `wake_at`／`wait_infinite` 并删除旧等待：[wakes.py:76](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/db/wakes.py:76)，调用点为同文件第 89、434、452 行。

3. 实际运行的命令及结果（原样贴最后几行）

```bash
.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
983 passed, 13 warnings in 143.85s (0:02:23)
```

```bash
.venv/bin/ruff check nanobot/ scripts/
```

首次报告 I001，删除多余空行后复跑：

```text
All checks passed!
```

```bash
.venv/bin/basedpyright nanobot/channels/telegram/social
```

```text
0 errors, 0 warnings, 0 notes
```

```bash
grep -rn "not in model_text\|in model_text" nanobot/channels/telegram/social --include=*.py
```

无输出；退出码 1，表示 **0 匹配**。

`git diff --check` 无输出，退出码 0。

4. 修改过的测试（逐条）

仅新增 [test_social_runtime.py:510](/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/tests/test_social_runtime.py:510) 的 `test_reencoded_snapshot_grants_read_ack`：

- 原内容：无此测试。
- 新内容：使用 `json.dumps(view, ensure_ascii=False)` 默认分隔符作为 `model_text`，调用 `note_seen` 后断言 `mark_read(ctx, 1) == 1`。
- 理由：验证首尾标记识别不依赖整份 JSON 逐字一致。

已有测试未修改；`test_social_scenarios.py` 未修改。

5. 与简报的偏差及原因

识别函数采用返工 1 确认的修订。原简报称 `admit_shown` 有两处 RUNNING 迁移，当前代码实际只有一处；实现累加该调用在循环中的 rowcount，总数大于 0 时统一清理一次。

未提交、推送或部署；工作区其他文件的已有或并发改动未触碰。

6. 未解决的问题 / 建议

本任务无未解决项。P4-0 §6 的其他疑点保持未处理，尤其 max-iterations 收尾请求漏登记、`accepted_messages` 残留注入块，建议后续任务单独处理。