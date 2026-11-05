# 任务 AGY-c：AGY 上线后暴露的三个问题（思考档位、思考流、JSON 代码块）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景

AGY provider（`nanobot/providers/antigravity_provider.py`、`gemini_wire.py`）已上线，主模型为 `antigravity/gemini-3.8-flash-medium`。线上日志与 Claude 的实测暴露三个问题：

1. **调用方显式的 reasoning_effort 被忽略**。`resolve_model` 对 tiered 模型总是使用模型名里的档位（medium）。V5 选择器传 `reasoning_effort="none"`、只有 2 秒预算，实际却按 medium 思考。
   实测：`thinkingLevel` 只接受 `low`/`medium`/`high`（`minimal` 返回 400 “Thinking level MINIMAL is not supported”）。
2. **思考期间流式没有任何事件，导致读取超时**。不带 `includeThoughts` 时，medium 档首个事件要 14.8 秒；Dream 两次在 90 秒（流式空闲超时）处 `ReadTimeout`。
   实测带 `"includeThoughts": true` 时，思考摘要以 `{"text", "thought": true}` part 流式返回（high 档 7.9 秒出现首个事件），连接保持活跃；accumulator 已经把 thought part 作为 reasoning 处理。
3. **Gemini 把 JSON 包在 Markdown 代码块里**。jargon 学习连续失败：`JargonDiscovery.model_validate_json` 收到 ```` ```json\n{...}\n``` ````。
   social 中直接解析模型输出 JSON 的位置共 5 处（Claude 已全仓库搜索核实）：
   `social/selection.py:240`（`Choice`）、`social/jargon_learning.py:183`（`JargonDiscovery`）、`social/jargon_learning.py:201`（`ResearchResult`）、
   `social/expressions.py:325`（`ExtractionResult`）、`social/judge.py:198`（`JudgeVerdict`）。

## 设计（已定，不得改变）

1. `antigravity_provider.resolve_model` 改为（签名不变）：
   - 先确定 `effort_level`：`reasoning_effort in THINKING_LEVELS` → 该值；`reasoning_effort in ("none", "minimal")` → `"low"`；其他（含 None）→ None。
   - `requested` 在 catalog 中 → `(requested, effort_level)`；
   - 否则匹配 tiered 正则且 `f"{family}-tiered"` 在 catalog 中 → `(f"{family}-tiered", effort_level or level)`（**调用方显式的 effort 优先于模型名档位**）；
   - 否则 `(requested, None)`。
   不新增常量以外的东西（可以新增 `NO_THINKING_EFFORTS = ("none", "minimal")`）。
2. `gemini_wire.build_request`：`thinking_level` 不为 None 时，`thinkingConfig = {"thinkingLevel": thinking_level, "includeThoughts": True}`。
3. 新增函数 `strip_json_fence(text: str) -> str`，放在 `nanobot/channels/telegram/social/models.py` 末尾：
   去掉首尾空白后，若以 ```` ``` ```` 开头且以 ```` ``` ```` 结尾，则去掉第一行（```` ``` ```` 或 ```` ```json ```` 等）和最后的 ```` ``` ````，再去掉首尾空白后返回；否则返回去掉首尾空白的原文。
   上述 5 处改为 `Model.model_validate_json(strip_json_fence(<原参数>))`。前面已有的长度/空内容检查保持不变。

## 要做的事
1. 按设计修改 `antigravity_provider.py`、`gemini_wire.py`、`social/models.py`、`social/selection.py`、`social/jargon_learning.py`、`social/expressions.py`、`social/judge.py`。
2. 测试：
   - `tests/providers/test_antigravity_provider.py`：更新/新增 `resolve_model` 用例——tiered 下显式 `"low"`/`"high"` 覆盖模型名档位，`"none"`/`"minimal"` → `"low"`，None 时用模型名档位；精确 ID 下 `"none"` → `"low"`、None → None。
   - `tests/providers/test_gemini_wire.py`：thinkingConfig 含 `includeThoughts: True`。
   - 为 `strip_json_fence` 加测试（放进 social 测试目录中最贴近的现有文件或新建 `nanobot/channels/telegram/tests/test_social_json_fence.py`）：```` ```json ```` 包裹、```` ``` ```` 包裹、无包裹、前后有空白四种。
   - 至少一个调用点的集成测试：jargon 或 expression 学习在 provider 返回带代码块的 JSON 时能成功解析（参照该模块已有测试的替身 provider 写法）。
3. 已有测试若因档位规则变化而失败，按新规则机械修改，并在报告中列出。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- `strip_json_fence` 不处理“代码块前后还有说明文字”等其他形态，不做 JSON 修复或提取。
- 认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：生产代码净增不超过 30 行。

## 允许改动的路径
- `nanobot/providers/antigravity_provider.py`、`nanobot/providers/gemini_wire.py`
- `nanobot/channels/telegram/social/models.py`、`selection.py`、`jargon_learning.py`、`expressions.py`、`judge.py`
- `tests/providers/test_antigravity_provider.py`、`tests/providers/test_gemini_wire.py`
- `nanobot/channels/telegram/tests/` 下与上述模块对应的测试文件（新增或修改，报告中列出）

## 不要做
- 不要改选择器超时、学习配置、prompt 模板、流式空闲超时。
- 不要读取 `~/.gemini`、`~/.local/share/nanobot-bridge-tg-*`；不要发真实网络请求；不要部署。

## 完成标准
- `.venv/bin/python -m pytest tests/providers/test_antigravity_provider.py tests/providers/test_antigravity_auth.py tests/providers/test_gemini_wire.py -q -p no:cacheprovider`
- `.venv/bin/ruff check` 与 `.venv/bin/basedpyright`（0 errors）覆盖所有改动的 Python 文件
- 整个社交测试目录：`.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
