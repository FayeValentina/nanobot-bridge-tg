# 任务 STK-a：每轮把完整贴纸库索引给主模型，贴纸退出本地检索选择

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景

SEL-a 之后，每轮只把本地关键词检索命中的最多 5 张贴纸放进主模型上下文；主模型要找别的贴纸只能用 `social_sticker list`（整串子串匹配，命中率低）。
owner 决定（2026-11-05）：**每轮把整个贴纸库的精简索引（sticker_id → note）给主模型，由它从全部贴纸中挑选**；成本可以忽略。
实测线上每个 scope 120 张、注释中位 25 字、最长 49 字，完整索引 JSON 约 8k 字符。表达与黑话仍走 SEL-a 的本地检索，不变。

## 设计（已定，不得改变）

### 贴纸索引
1. `social/db/stickers.py` `StickerLibrary`：新增 `index(self, scope: str) -> dict[str, str]`，
   SQL：`SELECT id, note FROM stickers WHERE scope=? AND trim(note)<>'' ORDER BY id`，返回 `{id: note}`（按 id 排序以保持跨轮稳定，利于缓存）。
   删除 `candidates()`（唯一调用者是 selection.py，下面移除）。
2. `social/context.py` `SocialContextBuilder.build`：`body_data["sticker_library"] = self.store.stickers.index(key)`，放在 `optional_expression_references` 之后。

### 贴纸退出选择器
3. `social/selection.py`：
   - `Candidate.kind` 的 Literal 去掉 `"sticker"`；`candidates()` 的 specs 去掉 sticker 一项，needles 去掉 sticker 专用分支（`scenes if kind != "sticker" else ...` 改为直接 `terms + scenes`）；
   - `current()` 的映射去掉 sticker，并去掉 `candidate.kind != "sticker"` 条件（剩下两类都检查 revision/status）；
   - `select()` 的循环去掉 sticker；`context()` 的 groups 与 fields 去掉 stickers；
   - 删除 `confirm_sticker()`。
4. `social/delivery.py` `_confirmed_send`：删除 `sent_sticker` 查询与 `confirm_sticker` 调用这 3 行。若 `deliveries.sent_sticker` 因此再无调用者（全仓库 grep），一并删除；`ContextSelector` 若因此不再被 delivery 使用，删除其构造参数与 import，并同步 runtime.py 的构造调用。
5. `social/learning.py`：删除因此无调用者的 `confirm_use()`、`selected_sticker()`（全仓库 grep 确认）。`record_selection` 对 kind 的检查保持不变（历史数据可能含 sticker）。
6. `nanobot/config/schema.py` `TelegramSocialLearningConfig`：删除 `selection_stickers`、`selection_sticker_candidates`。

### protocol（文本由编排者撰写，原样替换）
7. `nanobot/templates/telegram/social_protocol.md`：
   - 把以 `- Choose the medium before drafting.` 开头的整行替换为：
     `- Choose the medium before drafting. For banter, teasing, praise or an emotional reaction, first look through sticker_library in the live context (your whole collected library: sticker_id → note); if one fits, send it with social_send(sticker_id=...). A sticker alone is a complete reply; one short line may accompany it. Do not describe the sticker in words. If none fits, use text or stay silent; this is a preference, not a quota.`
   - 把以 `- The live context may offer optional expressions, jargon and stickers.` 开头的行中 `optional expressions, jargon and stickers` 改为 `optional expressions and jargon`，该行其余文字不变。

## 要做的事
1. 按设计修改。
2. 测试：
   - `index()`：只含有注释的贴纸、按 id 排序、只含本 scope；
   - `SocialContextBuilder.build` 的 live 块包含 `sticker_library`，且 `optional_expression_references` 中不再有 `stickers` 键；
   - 选择器不再产生 sticker 候选；
   - 更新因删除而失败的测试（贴纸选择、confirm_sticker/confirm_use 相关用例删除或改写），在报告中逐条列出；
   - protocol 相关测试若断言了旧文字，按新文字机械更新。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不对索引做截断、分页、字数预算或排序以外的处理。
- 认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：生产代码净减少。

## 允许改动的路径
- `nanobot/channels/telegram/social/db/stickers.py`、`db/deliveries.py`（仅第 4 条）、`context.py`、`selection.py`、`delivery.py`、`learning.py`、`runtime.py`（仅第 4 条）
- `nanobot/config/schema.py`
- `nanobot/templates/telegram/social_protocol.md`（仅第 7 条）
- `nanobot/channels/telegram/tests/` 下受影响的测试与 fixtures；`scripts/eval/` 中引用被删名字处的最小修改（py_compile + ruff 通过）

## 不要做
- 不要改 `social_sticker` 工具（import/view/annotate/list/collect）的行为与描述。
- 不要改表达/黑话的检索与注入。
- 不要读取 `~/.local/share/nanobot-bridge-tg-*`；不要部署。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
- `.venv/bin/python -m pytest tests/config -q -p no:cacheprovider`
- `.venv/bin/ruff check` 覆盖所有改动的 Python 文件；`.venv/bin/basedpyright` 对改动的生产模块 0 errors
- `grep -rn "confirm_sticker\|selection_stickers\|selection_sticker_candidates\|stickers.candidates" nanobot scripts` 无结果

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件，含净行数）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因；被删除/改写的测试清单
4. 未解决的问题 / 需要编排者决定的事项
