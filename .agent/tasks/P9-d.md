# 任务 P9-d：评估脚本移到 scripts/eval/ 并适配当前代码

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
`.agent/tasks/P9-0.report.md` 第 7 节（必读，逐脚本失效点带行号）；docs/SOCIAL_ARCHITECTURE_REVIEW.md 中“评估脚本和夹具移到 scripts/eval/、不作为单元测试”的建议。
Phase 3–9 的重构让这些脚本多处失效（`store.wake`、`message_deliveries`、`actions.message_id`、`record_message_receipt`、旧工具名、`waiting.set_wake`、冻结 hook 依赖的旧结构、`build_transcript(include_skills=...)`）。
**冻结证据不得改写**：V5/V5-E 基线与历史实际模型结果（fixtures 中的 `social_v5*`、`social_judge_*` 等）是固定对照（docs/V5E_IMPLEMENTATION.md），只能适配执行环境，不能修改其内容，也不能借此宣称自然度已验收。

## 设计（已定，不得改变）
1. 新目录 `scripts/eval/`（含空的 `__init__.py`；确认 `scripts/` 本身已是可导入的包，若不是，报告而不是新建）。用 `git mv` 移入（Codex 沙箱内 .git 只读时改为普通移动并在报告中列出，编排者提交时登记）：
   - `scripts/evaluate_social_expressions.py`、`evaluate_social_jargon.py`、`evaluate_social_selection.py`、`evaluate_social_v5.py`、`evaluate_social_v5_sequences.py`、`freeze_social_v5_baseline.py`、`summarize_social_v5.py`
   - `nanobot/channels/telegram/tests/evaluate_social_followups.py`、`evaluate_social_judge.py`、`evaluate_social_stickers.py`、`evaluate_social_v4.py`
   文件名不变。
2. **fixtures 留在原处** `nanobot/channels/telegram/tests/fixtures/`（单元测试 `test_social_v5e_replay.py`、`test_harness_prompts.py` 也在用）；脚本中的 fixtures 路径改为基于仓库根目录计算的绝对路径（不依赖 cwd）。
3. 逐脚本修复 P9-0 §7 列出的每一个失效点，改用当前接口（当前接口以代码为准，例如：wake 创建与移交用现有 `Wakes`/`store` 公开方法；投递记录用 `actions.message_ids` 与 `delivery.begin_message/finish_message`；工具名用 P8-b 的 9 个新名字；条件等待用 `waiting.set_conditions`；`build_transcript` 用当前签名）。
   - `evaluate_social_v5.py` 的冻结 hook：冻结源码的**内容不改**；在脚本内为它提供显式的适配命名空间/对象（它需要的 `current_request_context`、`self.runtime`、`self._last_memory_body`、`runtime.store.owns` 等），让冻结源码在当前运行时可执行。适配代码只放在 `scripts/eval/evaluate_social_v5.py`。
   - `summarize_social_v5.py` 不得默认写回工程 fixtures：改为必须显式传 `--output` 路径。
   - `evaluate_social_v4.py` 保留并修复（不删除）。
4. 更新引用：`scripts/pipeline.sh`（ruff 列表中的新路径；同时删除已不存在的 `nanobot/channels/telegram/tests/test_social_revisions.py` 一行）、`test_social_v5e_replay.py` 的导入与 monkeypatch 目标（改为 `scripts.eval.evaluate_social_v5`）、文档中仅 `docs/LOCAL_PIPELINE.md` 若有命令示例引用旧路径则更新（其他历史实施记录不改）。

## 要做的事
1. 按设计实施。
2. **验证（每个脚本都要有结论）**，在 `/tmp/p9/work/p9d/` 下进行，不调用付费模型、不连 Telegram：
   - 全部 11 个脚本 `python -m scripts.eval.<name> --help` 成功；
   - 能离线跑的部分实际跑：至少 `freeze_social_v5_baseline`（对 `/tmp/p9/social.sqlite3` 的副本，输出到 /tmp）、`evaluate_social_v5` 的冻结 hook + `replay_one` 用假 provider 跑通一条样本、`evaluate_social_selection`/`evaluate_social_jargon`/`evaluate_social_followups`/`evaluate_social_v4`/`evaluate_social_judge` 用假 provider（monkeypatch，写在 /tmp 的驱动脚本里，不改脚本本身）跑到第一次模型调用之后并返回；做不到的写明卡在哪一行、为什么。
   - 用 grep 证明脚本中不再出现：`store.wake(`、`message_deliveries`、`actions.message_id\b`、`record_message_receipt`、`social_get_state|social_read_messages|social_mark_read|social_set_wake|social_get_media`、`waiting.set_wake`、`include_skills=`。
3. 单元测试与场景测试照常通过。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不修改任何 fixtures 文件内容；不修改生产代码（`nanobot/` 下除 tests 以外不得有改动）。

## 允许改动的路径
- `scripts/eval/`（新建）、`scripts/`（被移出的文件、`pipeline.sh`）
- `nanobot/channels/telegram/tests/`（被移出的 evaluate_*、`test_social_v5e_replay.py`），fixtures 只读
- `docs/LOCAL_PIPELINE.md`（仅路径）

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider`（注意：不再需要 `--ignore-glob`，因为 evaluate_* 已移出）
- `.venv/bin/ruff check scripts/ nanobot/`
- `bash -n scripts/pipeline.sh`
- `git status --porcelain -- nanobot ':!nanobot/channels/telegram/tests'` 无输出（生产代码未改）；`git diff --stat -- nanobot/channels/telegram/tests/fixtures` 无输出。
- 第 2 步每个脚本的验证结论表。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件；列出移动清单，及是否用了 git mv）
2. 实际运行的命令及结果（原样贴最后几行）
3. 逐脚本验证结论表（help / 离线运行到哪一步 / 未能验证的原因）
4. 与简报的偏差及原因
5. 未解决的问题 / 需要编排者决定的事项
