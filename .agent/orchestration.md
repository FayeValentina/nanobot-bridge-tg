# Claude → Codex 编排实施流程

自 2026-11-03 起，本工程的较大实施任务（首先是 `docs/SOCIAL_ARCHITECTURE_REVIEW.md` 的分阶段重构）按以下方式进行。
用户已确认：**Claude 指挥并验收，Codex 编写代码，用户不逐项验收。**

## 角色

| 角色 | 职责 | 不做 |
|---|---|---|
| Claude（编排者） | 把阶段拆成任务简报；用 `codex exec` 派发；审查 diff；运行检查和测试；提交；按 `LOCAL_PIPELINE.md` 部署；记录进度；遇到需要用户决定的问题时询问用户 | 不在 Codex 完成前替它大段写代码。小修补可以自己做，但要在进度记录里注明 |
| Codex（实施者） | 按简报修改代码、编写测试、在本地运行简报要求的检查，最后输出结构化报告 | **不提交、不推送、不部署、不运行 `pipeline.sh release/deploy/rollback`**，不碰 `~/.local/share/nanobot-bridge-tg-*`、`~/.codex`、`~/.nanobot` 等工程外数据 |
| 用户 | 回答 Claude 提出的决策问题；在实际群里体验行为类变更 | 不需要逐行审查代码 |

## 指挥 Codex 的原则（用户 2026-11-03 总结的 Codex 特性）

Codex 的长处：逻辑推理和找 bug 极强；像手术刀一样精准，严格遵守边界；指令越精确，结果越好。
Codex 的短处：
1. **缺乏发散和整体设计**：不会主动考虑把散落的逻辑合并，也不会考虑可维护性。本工程中分散的 `upgrade_*` 迁移、
   god class、一个功能一个模块却彼此反向引用，都是这样产生的。
2. **过度防御**：会为不可能发生的情况加校验、回退、版本号、日志表和迁移步骤。

因此分工如下：

- **设计由 Claude 完成，写进简报；Codex 不做设计决策。** 涉及结构的任务，简报必须给出：目标文件列表、每个文件负责什么、
  关键类和函数的签名、哪些旧代码删除或合并到哪里。Codex 只负责“按图施工”。简报没写的新文件、新类、新抽象层一律不允许。
- **简报里禁止防御性扩张。** 每份简报都包含“防御性代码规则”一节（见模板）：除非简报明确要求，否则不得新增校验、
  try/except、回退分支、版本或 revision 检查、迁移步骤、配置项。Codex 认为确有必要的，只能写在报告的“建议”里，由 Claude 决定。
- **消除歧义。** 写简报时逐条自问：“这句话有没有第二种理解？”有就改写成可以直接核对的说法，必要时给出示例输入和输出。
  “清理”“优化”“适当处理”这类词不得出现；写明具体删什么、改成什么。
- **用好 Codex 的推理能力。** 高风险阶段（P0-3 状态机、P0-4 曝光、P0-5 投递）动手前，先派**只读分析任务**（`-s read-only`）：
  让 Codex 从现有代码中穷举状态转换、调用点和边界情况，产出事实清单。Claude 据此做设计，再派实施任务。
  实施完成后，也可以再派一次只读任务，让 Codex 找新代码中的逻辑错误，作为验收的补充。
- **验收时专门检查 Codex 的典型问题**：
  - 新增的防御代码：在 diff 中搜索新增的 `if ... raise`、`try:`、`except`、`assert`、`revision`、`version`、`fallback`，
    每一处都要能对应到简报中的要求，对应不上就退回。
  - 结构是否按设计落地：有没有自建 helper、在错误的文件里放逻辑、复制而不是合并。
  - 规模：重构类任务的净行数应该**减少**。简报里写明预期的规模范围，明显超出就要追问原因。

## 一个任务的完整循环

1. **简报**：Claude 在 `.agent/tasks/<id>.md` 写任务简报（模板见下）。一个任务的规模应让 Codex 一次做完：
   宁可把一个 Phase 拆成 2–4 个任务，也不要一次派一个大 Phase。
2. **派发**（Claude 的 Bash 调用需要 `dangerouslyDisableSandbox: true`，否则 Codex 的 bwrap 嵌套在 Claude 的沙箱里会启动失败；
   同时用后台运行，Codex 可能需要几十分钟）：
   ```bash
   cd /home/ubuntu/nanobot-bridge-tg && PYTHONPATH=.agent PYTEST_ADDOPTS="-p codex_pytest_env" \
     codex exec -C /home/ubuntu/nanobot-bridge-tg \
     -s workspace-write -c approval_policy='"never"' --color never --json \
     -o .agent/tasks/<id>.report.md - < .agent/tasks/<id>.md > .agent/tasks/<id>.events.jsonl 2>&1
   ```
   `PYTHONPATH/PYTEST_ADDOPTS` 加载 `.agent/codex_pytest_env.py`，补偿 Codex 沙箱环境的问题（没有外部 DNS、跨线程唤醒延迟）。
   这些补偿**只**存在于该插件中，不得写进工程的 conftest 或测试（P0-b 教训：Codex 曾把它们写进 conftest，已退回）。
   系统前提：`/etc/apparmor.d/bwrap` 允许 bwrap 使用 user namespace（2026-11-03 经用户同意添加）。
   如果 Codex 报告 sandbox 初始化失败，先检查这个文件是否还在。
3. **验收**（Claude 必须**亲自**完成，不能只读 Codex 的报告）：
   - `git status` / `git diff --stat`：改动范围不能超出简报允许的路径；不能有无关清理。
   - 逐文件阅读 diff，对照简报和 `SOCIAL_ARCHITECTURE_REVIEW.md` §0、§7 的约束。
   - 对改动的 Python 文件运行 `.venv/bin/ruff check` 和 `.venv/bin/basedpyright`，不能新增错误。
   - 运行简报列出的测试，包括场景测试 `nanobot/channels/telegram/tests/test_social_scenarios.py`（Phase 0 建立之后）。
   - 涉及 schema 迁移时，用实际实例 `social.sqlite3` 的**副本**（复制到 scratchpad）跑一次迁移，并核对关键表的行数前后一致。
   - Codex 声称“已测试”的内容，以 Claude 自己的复跑结果为准。
4. **返工**：不合格时，在同一个 Codex 会话里续写具体意见：
   `codex exec resume <session_id> - < 意见文件`。session_id 从 `<id>.events.jsonl` 的首个事件中取得。
   最多返工 2 轮；仍不合格的，Claude 自己修正小问题，或者把阻塞点带着证据报告给用户。
5. **提交与部署**：验收通过后由 Claude 提交（提交信息说明阶段、任务和验收证据），然后执行 `./scripts/pipeline.sh release`。
   部署后确认：`./scripts/deploy-local.sh status` 健康；实例 `state.json` 的 current 等于新提交；
   容器日志中没有新的异常栈。部署失败时管线会自动回滚，Claude 记录原因后返工。
6. **记录**：在 `docs/SOCIAL_REFACTOR_PROGRESS.md` 追加一行，写明任务 id、提交、验收证据（实际运行的命令和结果）、
   偏差或遗留问题。简报和报告文件随提交一并入库，便于追溯。
7. **需要用户介入的情形**（Claude 主动询问或告知，其余情况不打扰用户）：
   - 简报和审查文档都没有覆盖的设计决策。
   - 行为类变更（标注了【行为变更】的任务）部署后，请用户在实际群里体验并反馈。
   - 连续返工后仍失败、部署回滚、可能影响线上数据。

## 简报模板（`.agent/tasks/<id>.md`）

```markdown
# 任务 <id>：<标题>

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
引用 docs/SOCIAL_ARCHITECTURE_REVIEW.md 的相关章节，并写清本任务要解决的问题。

## 设计（已定，不得改变）
- 目标文件和职责、类和函数签名、旧代码的去向。结构类任务必填。

## 要做的事
1. 编号列出具体改动，尽量写到文件和函数。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：<例如“净减少约 X 行”或“新增不超过 Y 行”>。

## 允许改动的路径
- ...（超出范围的改动会被退回）

## 不要做
- 列出容易越界的事项。

## 完成标准
- 必须通过的命令，例如 `.venv/bin/python -m pytest <files> -q`、`.venv/bin/ruff check <paths>`
- **始终包括**整个社交测试目录：`.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  （P0-a 教训：只跑相关文件会漏掉其他文件里的连带失败，例如写死的 schema 版本断言）。
  需要修改已有测试才能通过时，允许做**机械性**修改（例如把写死的版本号改为引用 `SCHEMA_VERSION`），但必须在报告里逐条列出。
- 行为不变的证据，或者【行为变更】的具体说明。

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
```
