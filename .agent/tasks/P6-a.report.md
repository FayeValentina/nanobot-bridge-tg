1. 改动摘要（按文件）

- `runtime.py`：按指定顺序装配组件；删除 12 个迁移方法及媒体私有字段，无转发方法；更新内部和 hook 调用。
- `bindings.py`：原样接收 `validate_context`。
- `media.py`：新增 `SocialMedia`，承接媒体获取、准备、共享锁、loader 和 preview 保存；迁入 loader 类型别名。
- `waiting.py`：新增 `SocialWaiting`，承接指定的 7 个方法。
- `stickers.py`：移除 runtime 引用，注入窄依赖；接收 collect/search 和 loader；通过 media 保存 preview。
- `judge_runtime.py`：移除 runtime 引用，注入指定依赖；删除无调用者的 `enqueue`。
- `jargon.py`：删除过时注释和私有访问抑制。
- `telegram_social.py`：7 处调用路径改名。
- `gateway_runtime.py`：仅修改 admission 注册 1 处。
- `nanobot/channels/telegram/runtime.py`：无迁移方法引用，未修改。

测试及评估文件仅机械改名，数量如下；已通过 AST 比较确认没有其他修改：

| 文件 | 改名次数 |
|---|---:|
| `social_harness.py` | 1 |
| `test_reply_reliability.py` | 9 |
| `test_social_bindings.py` | 2 |
| `test_social_dialogue.py` | 26 |
| `test_social_followups.py` | 1 |
| `test_social_judge_runtime.py` | 8 |
| `test_social_media.py` | 33 |
| `test_social_proactive.py` | 4 |
| `test_social_runtime.py` | 2 |
| `test_social_scheduler.py` | 20 |
| `test_social_stickers.py` | 6 |
| `test_social_wake_v2.py` | 13 |
| `evaluate_social_followups.py` | 2 |
| `evaluate_social_judge.py` | 4 |
| `evaluate_social_v4.py` | 2 |
| `scripts/evaluate_social_selection.py` | 1 |
| `scripts/evaluate_social_v5.py` | 2 |

未修改断言语义、fixtures 或 `test_social_scenarios.py`；未执行评估脚本。

`wc -l` 前后对比：

| 文件 | 修改前 | 修改后 | 差值 |
|---|---:|---:|---:|
| runtime.py | 960 | 727 | −233 |
| waiting.py | 不存在 | 182 | +182 |
| media.py | 94 | 174 | +80 |
| stickers.py | 152 | 165 | +13 |
| judge_runtime.py | 356 | 355 | −1 |
| bindings.py | 248 | 267 | +19 |

2. 实际运行的命令及结果（原样贴最后几行）

pytest 均使用现有沙箱插件环境：`PYTHONPATH=.agent PYTEST_ADDOPTS='-p codex_pytest_env'`。

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob='*evaluate_*'

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
957 passed, 13 warnings in 147.83s (0:02:27)
```

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider

......................                                                   [100%]
22 passed in 11.23s
```

```text
.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway -q -p no:cacheprovider

.......s.....                                                            [100%]
83 passed, 2 skipped in 5.68s
```

```text
.venv/bin/ruff check nanobot/ scripts/

All checks passed!
```

```text
.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/agent/tools/telegram_social.py

/home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/media.py
  /home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/media.py:173:20 - error: "_save_pair" is protected and used outside of the class in which it is declared (reportPrivateUsage)
  /home/ubuntu/nanobot-bridge-tg/nanobot/channels/telegram/social/media.py:174:31 - error: "_path" is protected and used outside of the class in which it is declared (reportPrivateUsage)
2 errors, 0 warnings, 0 notes
```

```text
grep -rn "self.runtime\|runtime\._" nanobot/channels/telegram/social/judge_runtime.py nanobot/channels/telegram/social/stickers.py
```

无输出，退出码 1，0 命中。

```text
grep -rn "reportPrivateUsage" nanobot/channels/telegram/social
```

无输出，退出码 1，0 命中。

`git diff --check` 无输出，通过。

3. 与简报的偏差及原因

- basedpyright 未达到 0 errors：指定的 `SocialMedia.save_preview` 直接调用另一类的 `_save_pair`、`_path`，触发上述两条错误。保留指定实现，未添加抑制或绕过代码；`jargon.py` 无报错。
- 生产代码净增 **58 行**，超过预期上限 40 行；增量来自新组件构造、依赖导入及签名等开销。
- 未提交、推送或部署，符合编排模式要求。

4. 未解决的问题 / 需要编排者决定的事项

- 需决定如何解决 preview 保存设计与私有访问检查的冲突。建议授权将 cache 的这两个方法改为公开名称并机械更新调用点；本轮未擅自修改。
- 需决定是否接受净增 58 行的规模偏差。

当前三组测试全部通过，但尚未满足全部完成标准。