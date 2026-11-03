# 任务 P1-a：删除社交模块中的死代码和已退役路径

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode” 和 `.agent/orchestration.md`）。

## 背景

`docs/SOCIAL_ARCHITECTURE_REVIEW.md` P1-1。下列代码要么没有任何生产调用者，要么处理的是生产代码早已不会产生的值。
本任务**只删除，不重写**。除第 8 条（告别语正则）外，行为必须完全不变。

已核实的事实（不需要你再证明，但如果发现与事实不符，就停下并在报告里说明）：
- 生产代码产生的 wake reason 只有 `judge`、`proactive`、`timeout`、`wait_reply`、`wait_messages`。
  `timeout` 只由 `SocialStore.consume_timers` 直接 INSERT，不经过 `_schedule`。
  调用 `_schedule` 的只有 `_handoff_judgement`（judge/proactive）和 `consume_dialogue`（wait_reply/wait_messages）。
- `dialogue_attention` 表从未被 INSERT；`wake_samples` 表从未被读写（只在 schema 和 v11 迁移里出现）。
- `wakes` 中 `status` 不在 scheduled/queued/running 的行会在 86400 秒后删除，所以库里不会存在“待处理且 reason 为旧值”的行。

## 设计（已定，不得改变）

不新增任何文件、类或函数（schema 迁移步骤除外）。

## 要做的事

1. `nanobot/channels/telegram/social/store.py`
   - `_schedule`：`rank` 只保留 `{"judge": 4, "wait_reply": 1, "wait_messages": 1, "timeout": 0, "proactive": -1}`。
     删除遍历 `status='queued' AND turn_id IS NULL` 的整个循环：它的两个 if 只对 reason 为 probability/timeout/directed/nickname/question/keyword/sender 生效，而这些值不会传进 `_schedule`。
     `condition_config = state["wake_config"] if reason in {...} else "{}"` 改为 `condition_config = "{}"`（集合中的值都不会出现），后面 `if chosen != reason` 的分支保留。
     **保留**第一个循环（取消排队中的 proactive 机会）。
   - `note_seen`：删除 `direct = ...` 查询及其 `if direct:` 分支。
   - `wait_for_quiet` 和 `defer`：`reason NOT IN ('directed','nickname','judge')` 改为 `reason <> 'judge'`。
   - 删除所有 `dialogue_attention` 相关代码：`claim`、`_clear_dialogue` 的表元组、`wait_for_quiet`、`consume_dialogue` 中的两条语句、
     `dialogue_status` 中的 `focus` 查询及返回值里的 `"attention"` 键、`defer`、`next_due` 中的 UNION 行。
   - 删除方法 `wake()`、`schedule()`、`delete_observed()`。
   - `observe()`：删除参数 `reason`、`delay`、`quiet_seconds`、`wait_relevant`，以及 `if reason is not None: self._schedule(...)`。
2. `nanobot/channels/telegram/social/runtime.py`
   - `observe()` 调用 `store.observe` 时去掉 `wait_relevant=False`。
   - 删除 `_quiet_seconds` 和 `allows_personal_delivery`。
   - `set_wake`：删除参数 `probability` 及其校验（`if not math.isfinite(probability) or probability != 0`），
     删除 `and probability == 0` 条件；构造 `TelegramSocialWakeConfig` 时不再传 `probability`。
     **注意**：`TelegramSocialWakeConfig.probability` 字段本身保留，它属于 P1-d。
   - `get_state`：删除 `"retired_config"` 键。
   - `SOCIAL_PROTOCOL` 中删除这一句：`Probability sampling has retired; do not request probability>0.`
     （从 "Probability sampling" 开始到 "probability>0." 结束），其余文字一字不改。
3. `nanobot/agent/tools/telegram_social.py`：删除 `social_set_wake` 参数 schema 中的 `probability` 属性，删除调用处的 `probability=kwargs.get("probability", 0)`；
   description 中删除 `Probability must be zero. ` 这一句。
4. `nanobot/channels/telegram/social/proactive.py::status`：删除 `result["retired_config"] = ["intervalSeconds"]` 这一行。
5. `nanobot/channels/telegram/social/judge.py`：删除未使用的 `SocialJudge.from_config`。
6. `nanobot/channels/telegram/social/schema.py`：`SCHEMA_VERSION` 19 → 20。在 `if version < 19` 之后新增：
   `DROP TABLE IF EXISTS dialogue_attention`、`DROP TABLE IF EXISTS wake_samples`、`PRAGMA user_version=20`，写法与前面两步一致。
   不要改动 v0–v19 的迁移代码。
7. 删除后如果有 import 变为未使用（例如 `math`），一并删除。
8. 【行为变更，轻微】`runtime.py::wait` 中删除告别语正则 `farewell = re.search(...)` 及其在条件中的使用（`and not farewell`）。
   效果：模型在任何情况下选择 sleeping，只要 `sleep_observation` 启用，都会先进入观察窗口。
   如果 `re` 因此变为未使用，也删除 import。

## 测试的处理

- 只测试已删除代码的测试（例如专门测试 `store.wake`、`delete_observed`、`allows_personal_delivery`、retired probability 拒绝、
  `dialogue_attention`、旧 reason 优先级的测试）：删除。
- 借用已删除方法搭建场景的测试辅助函数（例如 `test_social_runtime.py::context()` 用 `social.store.wake(...)` 创建 wake）：
  在**测试文件内部**用等价的 SQL 插入替换，测试本身的断言不变。
- 迁移测试如果断言旧表在升级后存在，改为断言升级到最新版本后这些表不存在；写死的版本号改为引用 `SCHEMA_VERSION`。
- 依赖告别语行为的测试：改为新行为，或删除。
- 每一处删除或修改的测试都要在报告里逐条列出：文件、测试名、处理方式（删除 / 替换辅助函数 / 改断言）、原因。
- `test_social_scenarios.py` 和 `social_harness.py` 不应受影响；如果受影响，停下并报告。
- `nanobot/channels/telegram/tests/evaluate_*.py` 是评估脚本，不在测试收集范围内。如果它们引用了已删除的方法，做最小修改让 `ruff check` 通过即可。

## 允许改动的路径

- `nanobot/channels/telegram/social/store.py`、`runtime.py`、`proactive.py`、`judge.py`、`schema.py`
- `nanobot/agent/tools/telegram_social.py`
- `nanobot/channels/telegram/tests/` 下的测试文件和评估脚本（按“测试的处理”一节）
- `tests/agent/test_telegram_social_contracts.py`（仅当它引用了已删除内容时）

## 防御性代码规则

- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不要为“万一库里还有旧 reason 的行”加兼容处理，背景中已说明不存在。
- 不做本简报以外的清理，即使你看到其他死代码（写进报告的“建议”）。
- 预期规模：生产代码净减少约 100–200 行；生产代码不应有净增加的函数。

## 完成标准

- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过（场景测试中仍有 1 个 xfail）。
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider` 通过。
- `grep -rnE "dialogue_attention|wake_samples|'nickname'|_quiet_seconds|allows_personal_delivery|from_config|retired_config" nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py`
  只允许命中 `schema.py` 中旧版本的建表或迁移语句和新增的 DROP 语句。
- `.venv/bin/ruff check nanobot/channels/telegram/ nanobot/agent/tools/telegram_social.py` 通过；
  `.venv/bin/basedpyright nanobot/channels/telegram/social/ nanobot/agent/tools/telegram_social.py` 为 0 errors。
- 报告中给出 `git diff --stat` 和生产代码的净行数变化。

## 报告格式（最终回复只包含以下内容）

1. 改动摘要（按文件），包括净行数变化
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因；测试改动逐条列出
4. 未解决的问题 / 建议（不要自己实现）
