# 任务 P9-b：删除死配置字段与失效的常驻 wake 条件；telegram manifest 不再导入 config.schema

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
`.agent/tasks/P9-0.report.md` 第 6 节（配置面 A/B/C 分类）与第 8 节（manifest 失败）、疑点 5、7、8（必读）。
编排者决定（不得扩大）：**只删除 C 类死字段和已失效的常驻 wake 条件**；B 类字段一律不改为常量；
`TelegramSocialLearningConfig`、`TelegramSocialLearningModelConfig`、`TelegramSocialProactiveConfig`、`TelegramSocialJudgeConfig` **一个字段都不动**
（learning 的 revision 比较整份 model_dump，改动会取消学习批次；proactive 比较整份 dump，会重排调度——P9-0 疑点 5、8）。
线上 `config.json` 中被删字段的键由编排者在部署前删除；SQLite 中已存的 `scopes.wake_config` JSON 可能仍含旧键（P9-0 疑点 7），必须能被读取。

## 设计（已定，不得改变）

### A. `nanobot/config/schema.py`
1. 删除类 `TelegramSocialSleepInterruptConfig`。
2. `TelegramSocialScopeConfig`：删除字段 `wake_keywords`、`wake_senders`、`wake_on_messages`、`wake_on_question`、`wake_probability`、`sleep_interrupts`。
   - 保留 `nicknames` 及其校验；`validate_wake_words` 的 `@field_validator` 目标只剩 `"nicknames"`。
   - `validate_wake_senders`：删除其 `@field_validator("wake_senders")` 装饰，但保留为 `@staticmethod`（返回原值、同样的校验与错误文案），因为 `TelegramSocialWakeConfig.validate_senders` 调用它；`validate_wake_words` 同理须仍可被 `TelegramSocialWakeConfig.validate_keywords` 以 `TelegramSocialScopeConfig.validate_wake_words(values)` 调用（核实当前调用方式在改动后仍成立，必要时改为 staticmethod + 单独的 field_validator 包装，不改错误文案）。
3. `TelegramSocialWakeConfig`：删除 `on_messages`、`probability`、`sleep_interrupts`、`coalesce_seconds`；`model_config = ConfigDict(extra="ignore")`（**唯一允许的兼容措施**：已存 wake_config 可能含旧键）。其余字段与校验不变。
4. `TelegramSocialDynamicWaitConfig`：删除 `enabled`、`unfinished_seconds`；`validate_window` 改为只要求 `quiet_seconds <= max_seconds`，错误文案 `"quietSeconds <= maxSeconds is required"`。
5. 删除类 `TelegramSocialReplyAttentionConfig` 与 `TelegramSocialConfig.reply_attention`。
6. `TelegramSocialConfig.validate_scopes`：`limit` 改为 `self.max_coalesce_seconds`（不再看 dynamic_wait.enabled）；删除 `unfinished_seconds` 那条检查。其余不变。

### B. 读取方
- `social/waiting.py`：删除 `effective()`；`wait()` 与 `keep_reply_intent()` 中对 `self.effective(...)` 的调用改为 `self.conditions(key)`（返回值形状相同：`(conditions, dynamic)`）。若 `TelegramSocialScopeConfig` import 因此不再使用则删除。
- `social/judge.py`：
  - `coalesce_delay`：删除对 `conditions.coalesce_seconds` 的读取，改为 `delay = binding.coalesce_seconds if binding.coalesce_seconds is not None else self.config().coalesce_seconds`，其后的 wait quiet_seconds 覆盖不变；若 `conditions` 不再使用则删去那行。
  - `JudgeIntent` 删除 `on_messages` 字段；`intent()` 的 description JSON 只保留 `"waiting_for_questions"`；构造时不再传 on_messages。
  - 核实 `nanobot/templates/telegram/social_judge.md` 不引用 on_messages / waiting_for_messages（编排者已 grep 为 0 处；若有，停下报告）。
- 其他所有被删字段的生产引用（全仓 `grep -rn` 核实：`wake_keywords|wake_senders|wake_on_messages|wake_on_question|wake_probability|sleep_interrupts|reply_attention|unfinished_seconds|SleepInterruptConfig|ReplyAttentionConfig`，以及 `dynamic_wait.enabled`、`WakeConfig(... on_messages/probability/coalesce_seconds ...)`），删除或改写；逐处列出。

### C. `nanobot/channels/telegram/manifest.py`
- 删除 `from nanobot.config.schema import TelegramSocialConfig`；`"social"` 字段改为 `field("json", default={"enabled": False, "scopes": []})`。
- 目的：`tests/channels/test_channel_setup.py::test_channel_manifests_only_import_contract_modules` 通过。不改该测试。

## 要做的事
1. 按 A–C 实施。
2. 测试：
   - 删除只测试被删字段的测试；依赖它们的断言改写；逐条列出（文件:测试名:处理方式）。
   - 新增：(a) 含旧键 `probability/sleep_interrupts/coalesce_seconds/on_messages` 的 wake_config JSON 仍能被 `waiting.conditions()` 读取，并保留 keywords/senders/on_question/one_shot/expires_at；(b) 线上 config 删除这 4 个键之后的 social 段（编排者提供的形状：见下）能通过 `TelegramSocialConfig.model_validate`；(c) 含 `wakeProbability` 的配置被拒绝（extra_forbid，说明部署前必须删键）。
   - 线上 social 段删键后的形状：以 `/tmp/p9/config.json` 的 `channels.telegram.social` 为准，删除 `scopes[0].wakeOnQuestion`、`scopes[0].wakeProbability`、`scopes[0].sleepInterrupts`、`dynamicWait.unfinishedSeconds`。在测试中**内联**这个删减后的字典（不要读取 /tmp 文件），只保留与校验相关的键即可。
3. 场景测试 `test_social_scenarios.py` 不得修改；若失败，停下报告。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不给 loader 加旧键清理；不给其他配置类加 extra="ignore"。
- 预期规模：生产代码净减少（约 40–70 行）。

## 允许改动的路径
- `nanobot/config/schema.py`、`nanobot/channels/telegram/manifest.py`
- `nanobot/channels/telegram/social/{waiting,judge}.py` 及 grep 中确有引用的其他 social 文件（逐个列出）
- `nanobot/channels/telegram/tests/`（不含 evaluate_*、fixtures、test_social_scenarios.py）、`tests/config/`、`tests/channels/`（仅当已有测试引用被删字段）

## 不要做
- 不动 Learning / LearningModel / Proactive / Judge / QuietHours / SleepObservation 配置类与 `TelegramSocialConfig` 的其他字段（含 `max_coalesce_seconds`）。
- 不改 schema（SQLite）、不写数据迁移、不改 `scopes.wake_config` 已存数据。
- 不改 protocol、工具、模板。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/python -m pytest tests/config tests/channels tests/cli/test_gateway_runtime.py tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider`（沙箱内 socket/可选依赖失败照实贴出并标注，编排者会在沙箱外复跑）
- `.venv/bin/ruff check nanobot/`；`.venv/bin/basedpyright -p <你上次使用的显式 venv 配置> nanobot/channels/telegram/social nanobot/config/schema.py nanobot/channels/telegram/manifest.py` 0 errors。
- `grep -rnE 'wake_keywords|wake_senders|wake_on_messages|wake_on_question|wake_probability|sleep_interrupts|reply_attention|unfinished_seconds|SleepInterruptConfig|ReplyAttentionConfig' nanobot --include=*.py` 在生产代码（排除 tests）中无输出。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
