# 任务 SEL-a：去掉 V5 选择器的模型调用，改为本地检索直接注入

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景

`nanobot/channels/telegram/social/selection.py` 的 `ContextSelector.select()` 在每个 turn 生成前，先用本地关键词检索取候选（`candidates()`），
再用一次 2 秒限时的模型调用从候选中挑选，结果放进主模型上下文（`SocialContextBuilder.build` 中的 `optional_expression_references`）。
主模型已换成 Gemini，选择器模型调用要么超时、要么依赖 DeepSeek；owner 决定（2026-11-05）**去掉这次模型调用**：
本地检索的结果直接按相关度截断后注入，由主模型自己决定用不用。protocol 已说明这些参考是可选数据，不需要改 protocol。

## 设计（已定，不得改变）

### `social/selection.py`
1. `candidates(scope, messages)`：保持现有检索与 SCENE_HINTS 逻辑，**新增一条**：跳过 `row["relevance"] == 0` 的行（三类都一样；`relevance` 是三个库 `candidates()` SQL 已有的列）。其余不变。
2. `select(scope, turn, model_text="") -> list[Candidate]` 改为**同步函数**（去掉 async、`asyncio.Lock`），流程：
   - 缓存命中 → 返回缓存的 `selected`（缓存 `OrderedDict` 与 100 条上限保留）；
   - `store.learning.has_selection(scope, turn)` 为真 → 返回 `[]`（保留现有语义）；
   - `source = self.input(scope, turn, model_text)`；`learning.enabled` 为假或 `source` 为空 → status `"disabled_or_unavailable"`；
   - 否则 `pool = self.candidates(scope, source)`，按 kind 分组、保持检索顺序，过滤掉 `self.current(scope, c)` 为假的，
     每类取前 `selection_expressions` / `selection_jargon` / `selection_stickers` 个，再用现有 `self.context(selected)` 的字数预算截断（与现在相同：只保留进入 context 的条目）；
     status 为 `"retrieved"`（有条目）或 `"empty"`；
   - 调用 `store.learning.record_selection(...)`，参数：`target_message_id` 同现在、`candidates=len(pool)`、`selected=[(kind, id, revision)...]`、`status`、
     `calls=0`、`input_tokens=0`、`output_tokens=0`、`latency_ms`（本地耗时）、`explanation={"input_messages": [...], "candidates": [{"kind", "id", "selected"}...]}`（去掉 dropped/reason/model/tokens 键）。
   - 去掉包住 record_selection 的 try/except（本地写库失败应当暴露，与其他 store 写入一致）。
3. 删除：`Choice` 类、`self.provider`、`self.model`、`self.prompt`、`configure_provider()`、`self.tasks`、`self.locks`、`cancel()`、超时与模型调用相关代码，
   以及因此不再使用的 import（`asyncio`、`make_expression_provider`、`redacted_error_detail`、`strip_json_fence`、`LLMProvider`、`render_template`、`Config` 等，以 ruff 为准）。
   模块 docstring 改为说明“本地检索的可选表达/黑话/贴纸参考，不调用模型”。
4. `input()`、`current()`、`forget()`、`context()`、`recent_usage()`、`confirm_sticker()` 不变。

### 调用方
5. `social/context.py` `SocialContextBuilder.build`：`selected = self.selector.select(key, turn, transcript)`（去掉 await）。
6. `social/wiring.py`：删除两处 `selector.configure_provider(config)`。
7. `social/runtime.py` `close()`：删除 `self.selector.cancel()`。

### 配置与模板
8. `nanobot/config/schema.py` `TelegramSocialLearningConfig`：删除字段 `selection`、`selection_calls_per_turn`、`selection_timeout_seconds`。其余字段（含 `selection_sticker_candidates`、`selection_chars` 等）保留。
9. 删除模板 `nanobot/templates/telegram/social_expression_selection.md`（先全仓库 grep 确认只有 selection.py 引用）。
10. `scripts/eval/` 中引用已删除名字（`configure_provider`、`Choice`、`selection_timeout_seconds`、`learning.selection` 模型配置、选择器 provider）的地方：
    做最小修改使其能导入运行（`python -c "import ast; ast.parse(open(f).read())"` 级别不够，要求 `.venv/bin/python -m py_compile` 通过且 ruff 通过）；
    如果某脚本的核心功能就是评估选择器模型调用而已无意义，在报告中说明并列出，**不要删除脚本**，由编排者决定。

## 要做的事
1. 按设计修改以上文件。
2. 测试：更新 `nanobot/channels/telegram/tests/test_social_selection.py` 及其他因删除而失败的测试（替身 provider、超时、Choice 解析相关用例删除或改为本地检索断言），新增/保留用例覆盖：
   - relevance 为 0 的候选不注入；每类上限；`current()` 为假的被过滤；字数预算截断；
   - 同一 turn 第二次调用命中缓存且不重复 record；`has_selection` 为真返回空；
   - record 的 status 为 `retrieved`/`empty`、`calls == 0`；
   - `confirm_sticker` 对注入过的贴纸仍能确认使用（原有测试若覆盖则保留）。
   被删除或改写的测试在报告中逐条列出。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不保留任何“可选模型调用”开关或兼容分支。
- 认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：`selection.py` 净减少（预计 60 行以上）；生产代码整体净减少。

## 允许改动的路径
- `nanobot/channels/telegram/social/selection.py`、`context.py`、`wiring.py`、`runtime.py`
- `nanobot/config/schema.py`
- `nanobot/templates/telegram/social_expression_selection.md`（删除）
- `scripts/eval/*.py`（仅第 10 条所述最小修改）
- `nanobot/channels/telegram/tests/` 下受影响的测试与 fixtures（报告中列出）

## 不要做
- 不要改 protocol、检索 SQL、SCENE_HINTS、`context()` 的字段与预算、学习流程（expression/jargon learner）。
- 不要读取 `~/.local/share/nanobot-bridge-tg-*`；不要部署。线上 config 中的 `learning.selection` 键由编排者在部署前删除。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/python -m pytest tests/config -q -p no:cacheprovider`
- `.venv/bin/ruff check` 与 `.venv/bin/basedpyright`（0 errors）覆盖所有改动的 Python 文件；`scripts/eval` 改动文件 `py_compile` + ruff 通过
- `grep -rn "configure_provider\|selection_timeout\|social_expression_selection\|class Choice" nanobot/channels/telegram scripts` 只剩 expression/jargon learner 自己的 `configure_provider`

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件，含净行数）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因；被删除/改写的测试清单
4. 未解决的问题 / 需要编排者决定的事项
