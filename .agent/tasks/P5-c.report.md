1. 改动摘要

   - `nanobot/channels/telegram/runtime.py`：仅删除指定的 5 处拦截，净减少 10 行。保留 `_on_social_update`、`_observe_social`、豁免逻辑和 handler 注册。
   - 一个测试文件调整了 4 处入口调用，原业务断言未改。
   - 未修改 `test_social_scenarios.py`，未提交、推送或部署。

2. 实际运行的命令及结果

   改动前后均运行：

   ```bash
   .venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py tests/cli/test_gateway_runtime.py tests/channels -q -p no:cacheprovider --ignore-glob="*evaluate_*"
   ```

   改动前末行：

   ```text
   4 failed, 1232 passed, 2 skipped, 13 warnings, 1 error in 153.12s (0:02:33)
   ```

   改动后末尾：

   ```text
   FAILED tests/channels/test_channel_plugins.py::test_enable_optional_feature_refreshes_feishu_identity
   FAILED tests/channels/test_channel_setup.py::test_every_runtime_channel_field_has_a_webui_contract
   FAILED tests/channels/test_channel_setup.py::test_channel_manifests_only_import_contract_modules
   FAILED tests/channels/test_websocket_listener_health.py::test_closed_socket_is_not_accepting
   ERROR tests/channels/test_websocket_listener_health.py::test_real_listening_socket_is_accepting
   4 failed, 1232 passed, 2 skipped, 13 warnings, 1 error in 151.95s (0:02:31)
   ```

   前后失败及错误名单直接比较结果：

   ```text
   Failure/error lists identical: True
   ```

   删除生产拦截后、调整测试入口前运行：

   ```bash
   .venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_runtime.py -q -p no:cacheprovider
   ```

   ```text
   6 failed, 69 passed in 16.94s
   ```

   ```bash
   .venv/bin/ruff check nanobot/
   ```

   ```text
   All checks passed!
   ```

   ```bash
   .venv/bin/basedpyright nanobot/channels/telegram/runtime.py
   ```

   默认依赖发现存在环境问题，前后分别为：

   ```text
   908 errors, 0 warnings, 0 notes
   897 errors, 0 warnings, 0 notes
   ```

   显式指定 Python 路径仍有相同依赖发现问题。使用 `/tmp/p5c-pyrightconfig.json` 继承项目配置，并通过 `extraPaths` 补充仓库及 `.venv/lib/python3.11/site-packages`，未关闭诊断：

   ```bash
   .venv/bin/basedpyright --project /tmp/p5c-pyrightconfig.json nanobot/channels/telegram/runtime.py
   ```

   改动前后均为：

   ```text
   0 errors, 0 warnings, 0 notes
   ```

   `git diff --check` 通过，无输出。

   ```bash
   grep -n "_observe_social" nanobot/channels/telegram/runtime.py
   ```

   ```text
   2035:        if await self._observe_social(update):
   2038:    async def _observe_social(self, update: Update) -> bool:
   ```

3. 修改过的测试（逐条）

   以下均位于 `nanobot/channels/telegram/tests/test_social_runtime.py`，将 `_process_message_update(...)` 改为 `_on_social_update(...)`，并用 `pytest.raises(ApplicationHandlerStop)` 接住真实入口的正常停止信号：

   - 原 `:156` → 现 `:158`：普通群消息观察。
   - 原 `:223` → 现 `:226`：mention/reply，两组参数。
   - 原 `:797` → 现 `:801`：全群绑定下普通话题消息，三组参数。
   - 原 `:805` → 现 `:810`：全群绑定下定向回复，三组参数。

   新增 `ApplicationHandlerStop` 导入，原业务断言全部保留。

4. 与简报的偏差及原因

   - 代码改动无偏差。
   - 完整测试命令未全绿：前后均有相同的 4 个失败、1 个错误，全部位于 `tests/channels`，没有新增失败。
   - 类型检查使用临时依赖路径配置补偿环境问题，未修改项目配置。

5. 未解决的问题 / 建议

   现有基线问题：Feishu 依赖安装遇到只读 uv 缓存、Matrix 缺少 `nh3`、Telegram manifest 导入约束失败，以及沙箱禁止创建 socket。建议编排者在正常依赖和网络权限环境复跑相关检查；这些问题未纳入本次改动。