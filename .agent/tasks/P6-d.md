# 任务 P6-d：`social/wiring.py` 成为 gateway 的唯一 social 入口；修 test_commands（小幅装配变更）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景
docs/SOCIAL_ARCHITECTURE_REVIEW.md P2-3；P6-0 清单第 3、4 节（`.agent/tasks/P6-0.report.md`）。P6-a/b/c 已完成，social 组件不再持有 runtime。
当前 `nanobot/cli/gateway_runtime.py` 在两处（首次装配 `G:483–501,508–510,534–542`；WebUI 刷新 `G:750–764`）重复解析 social 配置、推导 OAuth 目录、配置 provider，
并且 Telegram 未启用或 `social.enabled=false` 时仍构造整套 social 对象。

## 设计（已定，不得改变）

### 1. 新文件 `nanobot/channels/telegram/social/wiring.py`
```python
def social_config(config: Config, config_path: Path) -> TelegramSocialConfig:
    """Parse channels.telegram.social; default the Codex judge OAuth directory beside the instance config."""
    # = G:488–493 / G:753–758 的现有逻辑，合并为一处

class SocialWiring:
    """Gateway-facing handle: the runtime plus its refresh/close lifecycle."""
    def __init__(self, runtime: TelegramSocialRuntime, tools: ToolRegistry, config_path: Path) -> None
    runtime: TelegramSocialRuntime
    def connect_agent(self, main_busy: Callable[[], bool]) -> None:
        # = G:540–542：expressions.main_busy = main_busy；jargon.main_busy = main_busy；jargon.tools = self.tools
    def refresh(self, config: Config) -> None:
        # = G:753–764 的 social 部分，顺序不变：
        # runtime.configure(social_config(config, path)) → make_judge_provider(config, runtime.config.judge) → judging.set_provider(...)
        # → expressions/jargon/selector.configure_provider(config)
    def close(self) -> None:
        # runtime.close()

def build_social_runtime(config: Config, config_path: Path, bus: MessageBus, tools: ToolRegistry) -> SocialWiring | None:
    """None unless channels.telegram.enabled and its social.enabled are both true."""
    # 1. telegram = config.channels.model_dump(by_alias=True).get("telegram", {})；enabled 取 telegram.get("enabled") 为真
    #    social = social_config(config, config_path)；两者有一个为假 → return None
    # 2. = G:494–501：make_judge_provider → TelegramSocialRuntime(social, config_path.parent / "telegram-social", bus, judge_provider=..., judge_provider_version=...)
    #    → 三个 configure_provider(config)
    # 3. = G:508–510：for name in sorted(SOCIAL_TOOLS): tools.register(SOCIAL_TOOL_CLASSES[name](runtime))
    # 4. return SocialWiring(runtime, tools, config_path)
```
- wiring 是唯一同时导入 runtime、judge provider factory 和工具类的地方。
- `agent._active_tasks` 的私有访问**留在 gateway**（wiring 只接收 `main_busy` 回调），保留现有的 `# pyright: ignore[reportPrivateUsage]`。

### 2. `nanobot/cli/gateway_runtime.py`
- 删除 `G:483–501` 和 `G:508–510` 的 social 代码；在 `tools = ToolRegistry()` 之后、`MCPProvider.from_config(config, tools)` 之前调用
  `social = build_social_runtime(config, Path(config_path), bus, tools)`（工具注册顺序与现在相同：social 工具先于 MCP）。
- AgentLoop 构造：
  - `hook_factories=[create_file_edit_activity_hook, *([social.runtime.hook_factory] if social else [])]`
  - `session_policy_resolver=social.runtime.policy_for_session if social else None`
  - `request_admission=social.runtime.waiting.admit_request if social else None`
- 构造后：`if social: social.connect_agent(lambda: bool(agent._active_tasks))  # pyright: ignore[reportPrivateUsage]`
- `_webui_refresh_runtime_config`：`agent.refresh_runtime_config()` 与 `load_config()` 保留；social 部分改为 `if social: social.refresh(fresh_config)`。
- ChannelManager：`telegram_social=social.runtime if social else None`。
- 关闭：`telegram_social.close()` 改为 `if social: social.close()`。
- `gateway_runtime.py` 中不再出现 `TelegramSocialConfig`、`make_judge_provider`、`SOCIAL_TOOLS`、`SOCIAL_TOOL_CLASSES`、`TelegramSocialRuntime`。

### 行为变化（本任务唯一允许的）
- Telegram 未启用或 `social.enabled=false` 时不再创建 social runtime、数据库、judge provider、工具和 hook。
- 已知后果：启动时 social 关闭、之后通过 WebUI 刷新开启，需要重启才生效（此前热刷新可以开启）。不要为此加代码；写进报告。
- social 启用时，对象构造顺序、配置、provider、工具注册顺序、刷新步骤与现在完全相同。

### 3. `tests/cli/test_commands.py` 的 9 个既有失败
- 先运行 `.venv/bin/python -m pytest tests/cli/test_commands.py -q -p no:cacheprovider` 记录改动前结果。
- 改动后再运行。P6-0 的分析是：这些测试的配置未启用 Telegram social，失败来自 gateway 无条件装配 judge provider；新装配下应全部通过。
- 若仍有失败：**只允许**修改测试中的替身（例如让伪造 `make_provider` 接受 `preset` 参数），不得修改生产代码去迁就替身；
  若修替身也不够，停下并在报告中给出失败原因。

## 要做的事
1. 新建 `wiring.py`，修改 `gateway_runtime.py`。
2. 测试：
   - 在 `tests/cli/test_gateway_runtime.py`（若该文件不存在或不合适，放进 `nanobot/channels/telegram/tests/test_social_runtime.py`）新增 4 条测试，只测 wiring 的公开函数：
     (a) telegram 未启用 → `build_social_runtime` 返回 None 且 registry 中没有 social 工具；
     (b) telegram 启用但 social.enabled=false → None；
     (c) 两者启用 → 返回 SocialWiring，registry 中恰好有 `SOCIAL_TOOLS` 全部 12 个工具（judge provider 用测试中已有的伪造方式，不访问网络）；
     (d) `social_config` 在 judge provider 为 openai_codex 且未设置 oauth 目录时，返回 `<config 目录>/telegram-social/oauth`。
   - 现有引用 gateway social 装配细节的测试若失败，只做机械适配并逐条列出。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不得为“理论上可能”的情况加代码。认为确有必要的，写在报告的“建议”里，不要自己实现。
- 不得新增简报未列出的类、helper、文件。
- 预期规模：wiring.py 约 60–80 行；gateway_runtime.py 净减少约 25 行。

## 允许改动的路径
- `nanobot/channels/telegram/social/wiring.py`（新建）
- `nanobot/cli/gateway_runtime.py`
- `tests/cli/`（替身与新增测试）
- `nanobot/channels/telegram/tests/test_social_runtime.py`（仅当新增测试放这里）

## 不要做
- 不改 Telegram channel、ChannelManager、AgentLoop、social 其他模块。
- 不修 P6-0 第 3 节列出的其他装配疑点（文件 watcher 不刷新 social、channel 构造时用自身 social config 覆盖、热刷新 judge 配置两次）；在报告中确认它们仍保持原状即可。

## 完成标准
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"` 全部通过
- `.venv/bin/python -m pytest nanobot/channels/telegram/tests/test_social_scenarios.py -q -p no:cacheprovider` 全部通过
- `.venv/bin/python -m pytest tests/agent/test_telegram_social_contracts.py tests/gateway tests/cli -q -p no:cacheprovider` 全部通过（贴出 tests/cli 改动前后的失败名单）
- `.venv/bin/ruff check nanobot/ tests/cli` 干净
- `.venv/bin/basedpyright nanobot/channels/telegram/social nanobot/cli/gateway_runtime.py` 0 errors（若需显式指定 venv，同时贴出改动前的同一命令结果作对比）
- `grep -n "TelegramSocialConfig\|make_judge_provider\|SOCIAL_TOOL\|TelegramSocialRuntime" nanobot/cli/gateway_runtime.py` 0 命中

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
