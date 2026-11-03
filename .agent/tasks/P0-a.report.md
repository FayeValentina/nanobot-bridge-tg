1. 改动摘要（按文件）

   - `expressions.py`：三类异常记录批次、kind、错误码；保存脱敏后最多 300 字符的详情，成功时清空。
   - `jargon_learning.py`：同上；搜索、抓取异常增加 debug 日志后继续。
   - `selection.py`：配置失败、选择超时及异常、记录失败增加日志，保持返回值和缓存语义。
   - `judge.py`：增加包含批次及错误码的 warning；消息脱敏、截断，数据库仍只保存固定错误码。
   - `schema.py`：升级至 v18，在本文件内检查并添加 `error_detail TEXT`。
   - `learning.py`：最近批次输出增加最多 120 字符的 `error_detail`。
   - 三个允许的测试文件：新增 12 个测试用例，覆盖异常诊断、脱敏、成功清空、迁移保留数据和日志行为。

2. 实际运行的命令及结果

   指定 pytest 命令执行两次，最终输出：

   ```text
   FAILED nanobot/channels/telegram/tests/test_social_learning.py::test_upgrade_schema_thirteen_preserves_all_existing_source_values
   FAILED nanobot/channels/telegram/tests/test_social_judge.py::test_schema_eight_upgrade_preserves_main_data
   2 failed, 209 passed in 34.03s
   ```

   ```bash
   .venv/bin/ruff check nanobot/channels/telegram/social/
   ```

   ```text
   Found 2 errors.
   [*] 2 fixable with `--fix`.
   ```

   两项均位于未修改的 `store.py`：I001、F401；对 HEAD 原文件运行 Ruff 得到相同结果。

   ```bash
   .venv/bin/ruff check nanobot/channels/telegram/social/{expressions,jargon_learning,selection,judge,schema,learning}.py nanobot/channels/telegram/tests/test_social_{learning,jargon,selection}.py
   ```

   ```text
   All checks passed!
   ```

   ```bash
   .venv/bin/basedpyright nanobot/channels/telegram/social/expressions.py nanobot/channels/telegram/social/jargon_learning.py nanobot/channels/telegram/social/selection.py nanobot/channels/telegram/social/judge.py nanobot/channels/telegram/social/schema.py
   ```

   改动前、改动后均为：

   ```text
   0 errors, 0 warnings, 0 notes
   ```

   `git diff --check`：退出码 0，无输出。

3. 与简报的偏差及原因

   - 指定测试未全部通过：两处现有断言硬编码 schema 为 17，与升级至 18 冲突；遵守“不修改已有断言”要求，保留原样。
   - 目录级 Ruff 未通过：已有问题位于允许范围外，未修改。
   - 凭证模块没有文本脱敏函数，按简报对 `sk-`、`Bearer `、`api_key=`、`token=` 后的值做简单替换。
   - 未提交、推送、部署或操作工程外实例数据。

4. 未解决的问题 / 需要编排者决定的事项

   - 是否允许将上述两个版本断言从 17 更新为 18，其中 `test_social_judge.py` 不在允许路径内。
   - 是否允许修复 `store.py` 的两项已有 lint 问题，以满足目录级 Ruff 验收。