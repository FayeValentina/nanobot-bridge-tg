# 任务 AGY-b：AntigravityProvider、登记与部署挂载

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景

AGY-a 已完成并提交：`nanobot/providers/antigravity_auth.py`（凭据读取/刷新、`login_status`）和 `nanobot/providers/gemini_wire.py`（`build_request`、`GeminiResponseAccumulator`）。
**先读这两个文件和它们的测试**，本任务只使用它们的公开函数，不修改它们。事实清单见 `.agent/tasks/AGY-0.report.md`（行号以当前代码为准）。

Claude 已用真实账号验证的接口事实（设计以此为准）：
- 基址 `https://daily-cloudcode-pa.googleapis.com/v1internal`。请求头：`Authorization: Bearer <token>`、`Content-Type: application/json`、`User-Agent: antigravity/1.5.0 linux/arm64`；流式另加 `Accept: text/event-stream`。
- `POST :loadCodeAssist`，body `{"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}}` → 响应顶层 `cloudaicompanionProject`（字符串）。
- `POST :fetchAvailableModels`，body `{"project": <project>}` → 响应顶层 `models` 是 **dict**，键为模型 ID（例如含 `gemini-3.8-flash-tiered`，不含 `gemini-3.8-flash-medium`）。
- `POST :streamGenerateContent?alt=sse`，body
  `{"project", "model": <wire model>, "user_prompt_id": uuid4, "requestType": "CHAT", "userAgent": "antigravity", "requestId": uuid4, "request": <gemini_wire.build_request(...) 的返回值>}`；
  每个 SSE 事件是 `{"response": <Gemini 响应对象>, "metadata", "traceId"}`。
- 不存在的模型返回 404；access token 失效返回 401。

## 设计（已定，不得改变）

### 文件 1：`nanobot/providers/antigravity_provider.py`（新增，约 170 行）

```python
BASE_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal"
USER_AGENT = "antigravity/1.5.0 linux/arm64"
CLIENT_METADATA = {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
THINKING_LEVELS = ("low", "medium", "high")
THINKING_MAX_OUTPUT_TOKENS = 65536

_PROJECTS: dict[str, str] = {}          # 键：str(凭据目录)；进程内缓存，不过期
_CATALOGS: dict[str, frozenset[str]] = {}

class AntigravityHTTPError(RuntimeError):
    def __init__(self, status_code: int, error_type: str | None, message: str): ...
    # 属性 status_code、error_type 供 LLMProvider._error_response_from_exception 读取

def resolve_model(requested: str, catalog: frozenset[str], reasoning_effort: str | None) -> tuple[str, str | None]

class AntigravityProvider(LLMProvider):
    def __init__(self, default_model: str, *, provider_name: str = "antigravity"): ...
    async def chat(...) -> LLMResponse          # 与 LLMProvider.chat 同签名，调用 self._call(..., 回调全为 None)
    async def chat_stream(...) -> LLMResponse   # 与 LLMProvider.chat_stream 同签名（不加 on_stream_recover），调用 self._call
    def get_default_model(self) -> str
```

规则：
- `resolve_model`（纯函数）：
  1. `requested` 在 catalog 中 → 返回 `(requested, reasoning_effort if reasoning_effort in THINKING_LEVELS else None)`；
  2. 否则若匹配 `^(gemini-[0-9.]+-flash)-(low|medium|high)$` 且 `f"{family}-tiered"` 在 catalog 中 → 返回 `(f"{family}-tiered", level)`（模型名里的档位优先于 reasoning_effort）；
  3. 否则返回 `(requested, None)`（交给服务端，404 会作为错误返回）。
- `__init__`：`super().__init__(api_key=None, api_base=None, provider_name=provider_name)`，保存 `default_model`，`self._directory = antigravity_dir()`。
- `_call(messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice, on_content_delta, on_thinking_delta)`：
  1. `requested = (model or self.default_model)`，去掉开头的 `"antigravity/"`。
  2. `async with httpx.AsyncClient(timeout=resolve_stream_idle_timeout_s()) as client:` 内完成本次调用的所有请求（每次调用新建 client，同 xai_grok）。
  3. token：`await asyncio.to_thread(get_access_token, self._directory)`。
  4. project：`_PROJECTS` 没有时调用 `loadCodeAssist` 并写入缓存；catalog：`_CATALOGS` 没有时调用 `fetchAvailableModels`，取 `frozenset(response["models"])` 写入缓存。
     这两个 JSON 请求共用一个私有方法 `_post_json(client, token, method, body) -> tuple[dict, str]`：返回 401 时 `token = await asyncio.to_thread(get_access_token, self._directory, rejected=token)` 后重发一次；
     非 200 → 抛 `AntigravityHTTPError`；返回 `(响应 JSON, 当前 token)`（token 可能已刷新，后续请求使用它）。
  5. `wire_model, level = resolve_model(requested, catalog, reasoning_effort)`。
  6. `request = build_request(self._sanitize_empty_content(messages), tools, tool_choice, max_output_tokens=max(max_tokens, THINKING_MAX_OUTPUT_TOKENS) if level else max_tokens, temperature=temperature, thinking_level=level)`。
  7. 流式请求：`client.stream("POST", f"{BASE_URL}:streamGenerateContent?alt=sse", ...)`；状态 401 时同样用 `rejected=token` 刷新并重发一次；非 200 → 读完正文后抛 `AntigravityHTTPError`。
     200 → 用 `nanobot.providers.openai_responses.parsing.iter_sse(response)` 逐个事件，`accumulator.feed(event["response"])`，
     对返回的 delta：`"content"` 调 `on_content_delta`、`"thinking"` 调 `on_thinking_delta`（回调为 None 就跳过）。
  8. 返回 `accumulator.response()`。
- `AntigravityHTTPError` 的构造：从错误响应 JSON `{"error": {"code", "message", "status"}}` 取 `status` 作为 `error_type`、`message` 作为消息（取不到时 error_type=None、message 为正文前 300 个字符），
  `str(exc)` 为 `f"Antigravity HTTP {status_code}: {message}"`。**不要**自己生成 `LLMResponse` 错误对象——异常交给基类 `_safe_chat`/`_safe_chat_stream` 统一转换（它会读 `status_code`、`error_type`）。
- 不实现 provider_state、native compaction、on_tool_call_delta、on_stream_recover、proxy、extra_body、模型目录接口。

### 登记（只改以下位置）

1. `nanobot/providers/registry.py`：在 `github_copilot` 的 ProviderSpec 之后新增（写全字段风格与相邻条目一致）：
   ```python
   ProviderSpec(
       name="antigravity",
       keywords=("antigravity",),
       env_key="",
       display_name="Google Antigravity",
       model_catalog="builtin",
       builtin_models=(
           ProviderModelSpec(id="antigravity/gemini-3.8-flash-medium", label="Gemini 3.8 Flash (Medium)", description="Gemini 3.8 Flash via Antigravity CLI OAuth.", context_window=1_048_576),
           ProviderModelSpec(id="antigravity/gemini-3.8-flash-high", label="Gemini 3.8 Flash (High)", description="Gemini 3.8 Flash via Antigravity CLI OAuth.", context_window=1_048_576),
           ProviderModelSpec(id="antigravity/gemini-3.8-flash-low", label="Gemini 3.8 Flash (Low)", description="Gemini 3.8 Flash via Antigravity CLI OAuth.", context_window=1_048_576),
       ),
       backend="antigravity",
       default_api_base="https://daily-cloudcode-pa.googleapis.com",
       is_oauth=True,
   ),
   ```
   并在 `backend` 字段注释的可选值列表里加上 `"antigravity"`。
2. `nanobot/providers/factory.py` `_make_provider_core`：新增 `elif backend == "antigravity":` 分支，`AntigravityProvider(default_model=model, provider_name=provider_name)`（函数内导入，同其他分支）。
3. `nanobot/config/schema.py` `ProvidersConfig`：在 `github_copilot` 之后加 `antigravity: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Google Antigravity (agy OAuth)`。不改 `config/loader.py`。
4. `nanobot/webui/settings_models.py` `oauth_provider_status`：新增 `if spec.name == "antigravity":` 分支，
   返回 `{**login_status(antigravity_dir()), "login_supported": False}`（`login_status` 本身不抛异常，不要套 try/suppress）。不改登录/退出/目录分支，不改前端。
5. `nanobot/security/credentials.py` `protected_runtime_paths`：元组中加入 `antigravity_dir()`（整个目录受保护，文件工具和 exec 沙箱不可访问）。

### 部署

6. `deploy/compose.local.yml` 的 `gateway`：
   - `environment` 加 `ANTIGRAVITY_DIR: /agy`；
   - `volumes` 加一个 bind：`source: ${BRIDGE_AGY_DIR:?Set BRIDGE_AGY_DIR}`、`target: /agy`、可读写（不写 read_only）、`bind: create_host_path: false`（格式照抄现有 checkout bind）。
7. `scripts/bridge_release.py` `_compose()` 的 `values` 加 `BRIDGE_AGY_DIR=str(Path.home() / ".gemini" / "antigravity-cli")`（`Path` 若未导入则导入）。

## 要做的事
1. 新建 `antigravity_provider.py`，完成上面 1–7 处改动。
2. 新建测试 `tests/providers/test_antigravity_provider.py`，照 `tests/providers/test_xai_grok_provider.py` 的方式 monkeypatch `httpx.AsyncClient` 为 `httpx.MockTransport` 客户端，monkeypatch `get_access_token`；每个测试前清空 `_PROJECTS`/`_CATALOGS`：
   - `resolve_model` 三条规则（含 reasoning_effort 被模型档位覆盖、精确 ID 时用 reasoning_effort、非法 effort 时 None）；
   - 完整调用：依次收到 loadCodeAssist、fetchAvailableModels、streamGenerateContent；断言 stream body 的 model 为 `gemini-3.8-flash-tiered`、`request.generationConfig.thinkingConfig.thinkingLevel == "medium"`、
     `maxOutputTokens == 65536`、project 与 `user_prompt_id/requestId` 存在、前缀已去掉；SSE 两个事件 → content delta 回调顺序、最终 tool_calls 与 usage；
   - 第二次调用不再请求 loadCodeAssist/fetchAvailableModels（缓存）；
   - stream 返回 401 → 用 `rejected=<旧 token>` 再取 token 并重发成功；
   - 429 `{"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}` → `chat_stream_with_retry` 或 `_safe_chat_stream` 返回 `finish_reason == "error"`、`error_status_code == 429`、`error_type == "RESOURCE_EXHAUSTED"`（只需验证一次转换，不要等待真实重试间隔——用基类不重试的入口或 monkeypatch 等待）；
   - factory：`antigravity/gemini-3.8-flash-medium` 的预设（provider="auto"）构建出 `AntigravityProvider`；
   - `oauth_provider_status` 对 antigravity：设置 `ANTIGRAVITY_DIR` 指向 tmp 目录（有/无凭据文件）的两种结果；
   - `protected_runtime_paths()` 包含 `ANTIGRAVITY_DIR` 指向的目录。
3. 若已有测试因新增 provider/挂载变量而失败（例如断言 provider 列表、compose 变量、registry 条目数），允许**机械性**修改，在报告里逐条列出。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不要为 catalog/project 缓存加过期、锁或失效逻辑；不要处理 SSE 中缺少 `response` 键的情况；不要给 httpx 异常加包装（基类已转换）。
- 认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：`antigravity_provider.py` 不超过 200 行；其他现有文件合计新增不超过 40 行。

## 允许改动的路径
- `nanobot/providers/antigravity_provider.py`（新增）
- `nanobot/providers/registry.py`、`nanobot/providers/factory.py`、`nanobot/config/schema.py`、`nanobot/webui/settings_models.py`、`nanobot/security/credentials.py`
- `deploy/compose.local.yml`、`scripts/bridge_release.py`
- `tests/providers/test_antigravity_provider.py`（新增）
- 因第 3 条需要机械修改的现有测试文件（报告中列出）

## 不要做
- 不要修改 `antigravity_auth.py`、`gemini_wire.py`（发现问题写进报告）。
- 不要改 WebUI 前端、`cli/`、`config/loader.py`、`oauth_model_catalog.py`、`providers/__init__.py`。
- 不要读取 `~/.gemini`、`~/.local/share/nanobot-bridge-tg-*`；不要发真实网络请求；不要运行部署。

## 完成标准
- `.venv/bin/python -m pytest tests/providers/test_antigravity_provider.py tests/providers/test_antigravity_auth.py tests/providers/test_gemini_wire.py tests/providers/test_xai_grok_provider.py tests/providers/test_providers_init.py tests/deploy tests/webui/test_settings_api.py tests/config -q -p no:cacheprovider`
  （若其中有与本任务无关的既有失败，贴出失败名并用 `git stash` 前后对比证明是既有的）
- `.venv/bin/ruff check` 与 `.venv/bin/basedpyright` 覆盖所有改动的 Python 文件（basedpyright 0 errors）
- 整个社交测试目录：`.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
