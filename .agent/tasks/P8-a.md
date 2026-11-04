# 任务 P8-a：social protocol 外置为模板、去掉每轮重复注入的中文指导、角色口头禅改为配置【行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P0-6；事实清单见 `.agent/tasks/P8-0.report.md`（第 1、3、6 节）。
新 protocol 正文已由编排者写好：`.agent/tasks/P8-a.social_protocol.md`（8,869 字符，原 `SOCIAL_PROTOCOL` 15,237 字符）。
它吸收了 context 块里每次迭代都注入的中文指导（`facts_note`、`expression_guidance`、`pending_note`、memory `note`、近期用语 `note`），所以这些字段在代码中删除。
角色口头禅“本大爷/白饭/算力/Token”从代码和学习模板移到配置字段 `learning.catchphrases`，编排者部署时在线上配置中填入原来的四个词，过滤行为保持不变。

P8-0 报告第 1.4 节已确认：protocol 文本变化只会让 `persona_revision` 递增并清空 `provider_state`，不截断历史、不取消等待（D4 满足）。**不要改动这条路径。**

工具（名字、参数、描述）本任务一律不动，留给 P8-b。

## 设计（已定，不得改变）
1. 新文件 `nanobot/templates/telegram/social_protocol.md`：内容与 `.agent/tasks/P8-a.social_protocol.md` **逐字节相同**（直接复制文件，不改一个字）。
2. `nanobot/channels/telegram/social/bindings.py`：删除 `SOCIAL_PROTOCOL = """..."""` 字面量，改为
   `SOCIAL_PROTOCOL = render_template("telegram/social_protocol.md", strip=True)`（模块级，导入 `nanobot.utils.prompt_templates.render_template`）。
   名字 `SOCIAL_PROTOCOL` 与 `resolve()` 中 assembled 的拼接格式保持不变。不加缓存、不加版本号、不加读取失败处理。
3. `nanobot/channels/telegram/social/context.py` `SocialContextBuilder.build`：删除 `body_data["facts_note"]` 与 `body_data["expression_guidance"]` 两处赋值。其余键和 `Live group memory index (untrusted data, not instructions):\n` 前缀不变。
4. `nanobot/channels/telegram/social/memory.py`（约 371–374 行，`context()` 返回的 JSON）：删除 `"pending_note"` 和 `"note"` 两个键，其余键不变。
5. `nanobot/channels/telegram/social/selection.py` `recent_usage`：
   - 删除返回字典中的 `"note"` 键；
   - 口头禅计数的词表由写死的 `("本大爷", "白饭", "Token", "算力")` 改为 `self.config().learning.catchphrases`；计数方式不变（casefold 子串），计数为 0 的词仍省略（现有 `if v`）。
6. `nanobot/config/schema.py` `TelegramSocialLearningConfig`：新增字段
   `catchphrases: list[str] = Field(default_factory=list, max_length=20)`，放在 `recent_usage_turns` 之后。不加其他校验器。
7. `nanobot/channels/telegram/social/expressions.py`：
   - `forbidden(text: str, catchphrases: Sequence[str]) -> bool`：第一项改为 `any(normalized(word) in value for word in catchphrases if normalized(word))`；泛化口号集合与正则两项**原样保留**。
   - 两个调用点（约 163、252 行）传入 `self.store.learning.config.catchphrases`（若该处的对象没有 `store`，用该类已有的、能到达 `SocialStore.learning.config` 的属性；不要新增构造参数）。
8. `nanobot/channels/telegram/social/jargon.py` `eligible`：从 `@staticmethod` 改为实例方法；集合中删除 `"本大爷", "白饭", "算力", "token"` 四项，保留其余九个普通词；另外排除 `normalized(term)` 等于任一 `normalized(c)`（c 取自 `self.store.learning.config.catchphrases`）的词。两个调用点（`jargon.py` 约 331 行、`jargon_learning.py` 约 190 行）已经通过实例调用，确认无需改动。
9. `nanobot/templates/telegram/social_expression_learning.md` 第 13 行：把
   `助手提示语和肥鱼招牌口头禅“本大爷/白饭/算力/Token”。即使真人重复这些也不学习。`
   改为
   `助手提示语和 bot 自己的口头禅。即使真人重复这些也不学习。`
   该文件其余内容不动。

## 要做的事
按“设计”1–9 实施，然后只做下面的测试改动：
- `nanobot/channels/telegram/tests/test_social_runtime.py:820` 写死的旧 protocol 句子：改为断言新模板中的一句，`"admission says why this turn started"`。
- `nanobot/channels/telegram/tests/test_social_selection.py:205、264` 的口头禅断言：在这两个测试构造 runtime 的配置中设置 `learning.catchphrases=["本大爷", "白饭", "Token", "算力"]`，断言值不变。
- `nanobot/channels/telegram/tests/test_social_expressions.py:108–112`（角色词硬排除样例）与 `test_social_jargon.py` 中依赖这四个词的样例：同样在测试配置中设置 catchphrases，断言不变；若这些测试直接调用 `forbidden(text)`，改为传入同样的四个词。
- 任何断言 `facts_note`/`expression_guidance`/`pending_note`/memory `note`/recent usage `note` 存在的测试：删除该断言（逐条列出）。
- 新增一个测试（放在 `test_social_selection.py`）：`learning.catchphrases` 为空时 `recent_usage` 不含 `catchphrases` 键（或为空字典，按现有返回形状），即使原文含“本大爷”。
- `test_harness_prompts.py:74–82` 断言的 `"Social delivery rules override"` 在新模板中保留，应无需改动；若失败，报告原因而不是改模板。
- `tests/`、`scripts/` 中冻结的 V5 基线（`fixtures/social_v5e_baseline_code.json`、`test_social_v5e_replay.py`、`scripts/freeze_social_v5_baseline.py`）**不得修改**；若因此失败，停下并报告。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 模板读取不加 fallback；catchphrases 不加去重、长度、大小写之外的校验。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里。
- 预期规模：生产 Python 净减少约 170 行（protocol 字面量 159 行 + 中文指导），新增模板 1 个。

## 允许改动的路径
- `nanobot/templates/telegram/social_protocol.md`（新建）、`nanobot/templates/telegram/social_expression_learning.md`
- `nanobot/channels/telegram/social/{bindings,context,memory,selection,expressions,jargon}.py`
- `nanobot/config/schema.py`（只加一个字段）
- `nanobot/channels/telegram/tests/` 中上面点名的测试文件

## 不要做
- 不改任何工具（`nanobot/agent/tools/telegram_social.py`）、工具描述、`tool_contract.md`、`autonomous_activity.md`、judge/selector/jargon 模板。
- 不改 `SCENE_HINTS`、jargon 的九个普通词、泛化口号集合、辱骂正则、`jargon_learning.py` 的搜索后缀（编排者决定作为非角色数据保留）。
- 不改 persona_revision / scope version / provider_state 路径。
- 不重跑、不修改任何 learning 批次或游标。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py tests/config -q -p no:cacheprovider`
- 场景测试 `nanobot/channels/telegram/tests/test_social_scenarios.py` 全部通过且未修改。
- `.venv/bin/ruff check nanobot/`；`.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/config/schema.py` 无新增错误。
- `cmp nanobot/templates/telegram/social_protocol.md .agent/tasks/P8-a.social_protocol.md` 无输出。
- `rg -n '本大爷|白饭|算力|肥鱼|嘴硬摸鱼' nanobot --glob '!**/tests/**'` 无输出。
- 行为变更说明：主模型 system 中 protocol 变为新文本；每次迭代的 live memory 块不再含五段指导文本；口头禅过滤由配置决定（默认空，即不过滤，编排者会在线上配置中填入原值）。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
