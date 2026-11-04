1. 改动摘要

全部 11 个脚本已移入 `scripts/eval/`，文件名不变。`git mv` 因 `.git` 只读失败，实际使用普通移动，待编排者提交时登记。

| 原位置 | 文件 | 适配内容 |
|---|---|---|
| `scripts/` | `evaluate_social_expressions.py` | 仓库根目录绝对路径 |
| `scripts/` | `evaluate_social_jargon.py` | 当前独立 JargonLibrary 接口 |
| `scripts/` | `evaluate_social_selection.py` | `Wakes.schedule/ready` |
| `scripts/` | `evaluate_social_v5.py` | 冻结 hook 显式适配、wake、投递、回执查询、transcript 签名、SOUL 路径 |
| `scripts/` | `evaluate_social_v5_sequences.py` | 新导入路径及数据目录说明 |
| `scripts/` | `freeze_social_v5_baseline.py` | 查询 `actions.message_ids` |
| `scripts/` | `summarize_social_v5.py` | 新导入路径，强制 `--output`，取消默认写回 fixtures |
| Telegram tests | `evaluate_social_followups.py` | 新工具名、投递接口、历史回执及当前上下文 hook |
| Telegram tests | `evaluate_social_judge.py` | 条件等待、wake、配置与 fixtures 路径 |
| Telegram tests | `evaluate_social_stickers.py` | 仓库路径、动态贴纸数量、显式指定包 |
| Telegram tests | `evaluate_social_v4.py` | 保留并修复旧配置、工具、投递及查询接口 |

另新增空的 `scripts/eval/__init__.py`；确认 `scripts/` 已是可导入的 namespace package。更新 `scripts/pipeline.sh` 和 `test_social_v5e_replay.py`；`LOCAL_PIPELINE.md` 无旧路径命令需要更新。

2. 实际运行的命令及结果

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env' .venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider
```

```text
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
944 passed, 13 warnings in 80.11s (0:01:20)
```

```bash
.venv/bin/ruff check scripts/ nanobot/
```

```text
All checks passed!
```

以下命令均退出 0、无输出：

```bash
bash -n scripts/pipeline.sh
git status --porcelain -- nanobot ':!nanobot/channels/telegram/tests'
git diff --stat -- nanobot/channels/telegram/tests/fixtures
git diff --check
```

在 `/tmp/p9/work/p9d/` 执行验证驱动：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/ubuntu/nanobot-bridge-tg /home/ubuntu/nanobot-bridge-tg/.venv/bin/python check_all.py
```

末尾输出：

```text
v5 offline exit=0
v5: exact frozen hook + replay_one returned; fake calls = 2 contexts = 1 receipt = sent
stickers offline exit=0
stickers: synthetic 2-sticker import/view/annotate/send returned; fake calls = 6
sequences offline exit=0
sequences: topic_switch/sticker_exchange/empty_library returned; turns = 9 fake calls = 18
summarize: missing --output exit=2; no summary.md created = True
```

对 `/tmp/p9/social.sqlite3` 的 SQLite backup 副本执行冻结导出：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/home/ubuntu/nanobot-bridge-tg /home/ubuntu/nanobot-bridge-tg/.venv/bin/python -m scripts.eval.freeze_social_v5_baseline --database social.sqlite3 --main-scope telegram:-1003760551468 --topic-scope telegram:-1003760551468:topic:53 --human-id 2041312857 --human-id 5161465425 --peer-bot-id 8671295753 --output frozen-export.json
```

```text
{"extraction": 39, "evaluation": 61}
```

执行旧接口检查：

```bash
grep -En 'store\.wake\(|message_deliveries|actions\.message_id\b|record_message_receipt|social_get_state|social_read_messages|social_mark_read|social_set_wake|social_get_media|waiting\.set_wake|include_skills=' scripts/eval/*.py
```

退出 1，无匹配。另直接逐字节比较全部 fixtures：

```text
fixtures direct byte comparison: 9 unchanged
eval __init__ bytes: 0
```

3. 逐脚本验证结论表

| 脚本 | help | 离线运行到哪一步 | 未验证范围／原因 |
|---|---|---|---|
| `evaluate_social_expressions` | 0 | 两个提取批次返回，假 provider 调用 2 次 | 实际模型提取质量未测 |
| `evaluate_social_jargon` | 0 | 研究、发现、显式纠错及覆盖旧释义返回，调用 4 次 | 公共释义质量未测；网络工具均为替身 |
| `evaluate_social_selection` | 0 | 三例选择及生成返回；主调用 6 次、选择 3 次；首例 stub 投递成功 | 实际选择质量未测 |
| `evaluate_social_v5` | 0 | 冻结 hook 原文执行、记忆曝光、`replay_one`、普通消息 sent 回执；调用 2 次 | 未跑真实三臂评估 |
| `evaluate_social_v5_sequences` | 0 | baseline 三组连续场景共 9 轮返回，调用 18 次 | 其他臂及实际模型质量未测 |
| `freeze_social_v5_baseline` | 0 | 数据库副本完整导出 39／61 条 | 无执行阻塞 |
| `summarize_social_v5` | 0 | 成本函数通过；缺少 `--output` 时退出 2，未写文件 | 完整汇总在第 78 行读取 `v5/replay.json` 时失败，缺原始输入 |
| `evaluate_social_followups` | 0 | 两轮调用及记忆工具返回，场景匹配 | 其余实际模型语义场景未测 |
| `evaluate_social_judge` | 0 | 条件等待设置、模型返回及 admission 完成，调用 1 次 | 历史预期不代表当前行为或自然度验收 |
| `evaluate_social_stickers` | 0 | 假包 2 张贴纸完成导入、看图、标注及发送，调用 6 次 | 未连接 Telegram |
| `evaluate_social_v4` | 0 | `emotion_share` 主模型两轮及 stub 投递返回，结构检查通过 | 其余实际模型语义场景未测 |

4. 与简报的偏差及原因

- 按简报允许的替代方式使用普通移动，原因是 `.git` 只读。
- V5 改从 `--data/baseline-SOUL.md` 读取保留的历史 SOUL，摆脱旧实例旁备份路径；离线验证使用明确标注的占位内容。
- 实跑额外发现并移除了 Judge 已不存在的 `wake_on_question` 配置调用。
- 未提交、推送或部署；生产代码及 fixtures 均未修改。

5. 未解决的问题 / 需要编排者决定的事项

- 完整汇总验证仍需原始 72 轮 `replay.json` 及配套学习、连续回放、黑话和故障报告。
- 实际基线回放需提供原始历史 SOUL 至上述数据路径。
- 本次未调用付费模型或 Telegram；不构成 V5 自然度全面验收。