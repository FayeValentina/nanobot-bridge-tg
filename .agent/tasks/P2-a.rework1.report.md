1. 本次改动（按文件）

- `social/bindings.py`：scope version 改为比较 `role_id + paused`，使用 `sort_keys=True`。
- `social/runtime.py`：将 `_social_mode` 赋值移到模式切换代码块之后，无条件保存。
- `tests/test_social_bindings.py`：增加暂停与取消暂停改变 version 的断言；新增旧会话连续解析两次均保留历史的测试。暂停恢复测试原断言未改。

2. 实际运行的命令及结果

```text
.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"

971 passed, 13 warnings in 146.14s (0:02:26)
```

零 xfail，场景套件全部通过。

```text
.venv/bin/ruff check nanobot/channels/telegram/

All checks passed!
```

```text
.venv/bin/basedpyright -p /tmp/p2a-pyrightconfig.json nanobot/channels/telegram/social/

0 errors, 0 warnings, 0 notes
```

```text
grep -rn '_persona(' nanobot tests --include='*.py'
```

无输出，退出码 1，即零命中。

```text
git diff --check
```

无输出，退出码 0。

3. 偏差

无。仅修改上述三个文件，未提交、推送或部署。

4. 未解决问题

无。此前报告的两处设计问题均已修复并通过回归测试。