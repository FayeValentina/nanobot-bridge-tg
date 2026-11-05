# AGY（Google Antigravity）原生认证：hermes 实现分析与 nanobot 融合方案

状态：分析（2026-11-05），尚未实施。参考来源：`~/agy-hermes-agent`（FayeValentina 的 hermes fork，提交 `463a1562`、`6f2b5776`）。

## 一、hermes fork 的实现

hermes 的下游改动一共约 2,200 行，其中核心是 3 个文件；其余是 CLI/WebUI 登记和 LINE 修复。

| 文件 | 行数 | 作用 |
|---|---|---|
| `agent/antigravity_credentials.py` | 310 | 读取并刷新 agy CLI 的 OAuth 文件 |
| `agent/antigravity_client.py` | 323（+98） | Code Assist 请求包装、模型发现、流式输出、tiered 模型解析 |
| `plugins/model-providers/antigravity/__init__.py` | 43 | provider 注册，把 reasoning 转成 thinking_config |
| 复用上游 `agent/gemini_native_adapter.py` | 714 | OpenAI 消息/工具 ↔ Gemini 原生格式的双向转换（**不是 fork 写的**） |

另有一条独立路线：`extras/agy-provider/agy_provider.py`，每个请求起一个 `agy` 子进程，对外提供 OpenAI 兼容接口。这条路线只返回最终文本，没有结构化工具调用，也没有真正的流式输出，不适合 nanobot 的 social 场景，下文不再讨论。

### 1. 认证（`antigravity_credentials.py`）

- **不自己登录**，直接共用 agy CLI 生成的凭据文件：`~/.gemini/antigravity-cli/antigravity-oauth-token`（路径可用 `ANTIGRAVITY_CREDENTIAL_PATH` 覆盖）。
  文件格式为 `{"auth_method": ..., "token": {"access_token", "refresh_token", "token_type", "expiry"(RFC3339 纳秒)}}`。
- **刷新**：到期前 60 秒，向 `https://oauth2.googleapis.com/token` 发 `grant_type=refresh_token`。
  - client_id/secret 优先取环境变量 `ANTIGRAVITY_OAUTH_CLIENT_ID/SECRET`；没有设置时，**用正则从 agy 二进制里扫出** `*.apps.googleusercontent.com` 和 `GOCSPX-…`。
    以 `1071006060591-` 开头的 client 排在最前，其余候选依次尝试。
  - 刷新成功后，用原子方式写回同一个文件（mkstemp → fsync → replace，权限 0600），这样 agy 自己也能继续使用新 token。
- **并发**：同目录下的 `.hermes-antigravity.lock` 加 `fcntl.flock` 排他锁，锁内重新读取文件。
  401 后强制刷新时会带上被拒的 access_token；如果文件里的 token 已经被别的进程换掉且尚未过期，就直接复用，避免再次轮换 refresh token。
  这一点正对应本机 Codex 遇到过的 “refresh token was already used” 问题。

### 2. 请求与模型发现（`antigravity_client.py`）

- 端点：`https://daily-cloudcode-pa.googleapis.com/v1internal:*`，这是 Cloud Code Assist 的**私有协议**。
  请求头为 `Authorization: Bearer`，`User-Agent: antigravity/1.1.22 linux/arm64`（伪装成 agy，可用 `ANTIGRAVITY_USER_AGENT` 覆盖）。
- **项目 ID**：`POST :loadCodeAssist {"metadata": {"ideType":"ANTIGRAVITY","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}}`，
  从响应中取 `cloudaicompanionProject` 并缓存在进程内。
- **模型发现**：`POST :fetchAvailableModels {"project"}`。解析 `agentModelSorts`（标签含 recommended 的组排在前面）、`defaultAgentModelId` 和 `models`，
  过滤掉 `chat_`、`tab_` 前缀，结果缓存在进程内。
- **tiered 解析**（`6f2b5776`）：用户写 `gemini-3.8-flash-high` 时，如果账号目录里没有这个精确 ID、但有 `gemini-3.8-flash-tiered`，
  就改用 tiered 模型，并把等级写进 `thinkingConfig.thinkingLevel`，同时删除 `thinkingBudget`。目录里有精确 ID 时不改写。
- **请求包装**：先用上游的 `build_gemini_request` 生成原生 Gemini 请求，再包一层：
  `{"project", "model", "user_prompt_id": uuid, "requestType": "CHAT", "userAgent": "antigravity", "requestId": uuid, "request": <gemini 请求>}`。
  非流式用 `:generateContent`；流式用 `:streamGenerateContent?alt=sse`。响应外面套着 `{"response": ...}`，解包后交给上游的转换函数。
- **401 重试一次**：流式请求在拿到状态码后、读取正文前判断；如果是 401，关闭当前流，刷新 token 后重新发起一次。

### 3. 消息与工具调用转换（上游 `gemini_native_adapter.py`，被直接复用）

- system 消息合并为 `systemInstruction`；assistant 映射为 `model`；tool 结果映射为 user 侧的 `functionResponse`（name 必须与对应的 functionCall 一致，通过 call_id 反查）。
- assistant 的 tool_calls 转成 `functionCall`。Gemini 3 要求带 `id`，并要求带 **`thoughtSignature`**：
  签名取自 tool_call 的 `extra_content.google.thought_signature`；没有签名时（例如工具调用来自其他 provider 的历史），填哨兵值 `skip_thought_signature_validator`。
- 相邻同角色的 content 会被合并。如果真人文本紧跟在 tool 结果之后，就在中间插一个占位 model 回合，保证角色交替合法。
- 工具 schema 经 `sanitize_gemini_tool_parameters` 清洗；functionResponse 中的 `$ref` 也会被处理，否则会返回 400。
- `tool_choice` 映射：auto/required/none → `functionCallingConfig.mode` AUTO/ANY/NONE；指定函数名时用 `allowedFunctionNames`。
- 开启 thinking 时 `maxOutputTokens` 会被提高到上限，否则思考过程会耗尽额度、拿不到答案。
- **流式转换**：`thought` 文本部分作为 reasoning delta 输出；`functionCall` 部分以 `(part_index, name, thoughtSignature)` 为键分配 index，
  Gemini 每个事件都会重发完整参数，只输出新增的后缀；finishReason 映射为 stop/length/tool_calls，`usageMetadata` 附在结束 chunk 上。

### 4. 风险

- 使用的是 agy 的 installed-app OAuth client 和私有 `v1internal` 协议，并伪装 User-Agent。这不是 Google 公开支持的 API，
  协议变化或账号风控都可能导致失效，代码里也注明了 “compatibility depends on the private Cloud Code Assist protocol”。
- 2026-11-05 已在宿主机安装 agy 1.5.0（`~/.local/bin/agy`）并以 consumer OAuth 登录；凭据文件 `~/.gemini/antigravity-cli/antigravity-oauth-token`（字段：auth_method、id_token_email、token{access_token, refresh_token, token_type, expiry, expires_in}，与 hermes 的读取方式一致）。
- `agy models` 返回 13 个模型：gemini-4-argon-{low,medium,high}、gemini-3.8-flash-{low,medium,high}、claude-opus-5-5-*、claude-sonnet-5-5-*、gpt-oss-120b-medium。
  hermes 的 tiered 正则只覆盖 gemini-3.x flash；Claude/GPT-OSS 模型是否同样接受 Gemini 格式的请求，需要在 AGY-0 中实际验证。

## 二、nanobot 现状

**不支持。** 仓库中没有 antigravity/cloudcode 相关代码。Gemini 只能通过 `registry.py` 中 `gemini` 这个 openai_compat spec 接入（Google 的 OpenAI 兼容端点，需要 API key）。
nanobot 也**没有原生 Gemini 格式转换层**，这是与 hermes 最大的差别：hermes 只需写一层薄包装，而 nanobot 需要自己补上转换层。

已有、可以直接利用的部分：
- provider 抽象：`LLMProvider.chat` / `chat_stream(on_content_delta, on_thinking_delta, on_tool_call_delta)` → `LLMResponse`（异步）。
- OAuth provider 的接入方式：`xai_grok`（`registry.py` 中的 ProviderSpec 设置 `backend="xai_grok"`、`is_oauth=True`、`model_catalog="hybrid"`；
  `factory.py` 按 backend 分支；`config/schema.py` 的 ProvidersConfig 字段；`oauth_model_catalog.py`；`cli/provider.py`；WebUI 设置页）。
- **thoughtSignature 能在历史中保留**：`ToolCallRequest.extra_content` 会序列化进会话，`openai_compat_provider.py:806` 已经按 `{"google": {"thought_signature"}}` 处理过 Gemini 签名。
- 部署方式：容器只挂载 `${BRIDGE_DATA_DIR}:/data`。

## 三、融合方案

### 新增文件（`nanobot/providers/`）

1. **`antigravity_auth.py`**（约 250 行）：移植 hermes 的 credentials 模块。
   - 凭据路径默认取 `/data/antigravity/antigravity-oauth-token`（由部署时挂载），可用环境变量覆盖。
   - client_id/secret **只从环境变量读取**：容器里没有 agy 二进制，也不应该在运行时扫描二进制；部署时一次性提取后写入 compose.env。
   - 保留 flock、锁内重读、“被拒 token 已被替换就复用”和原子写回。在 async 中通过 `asyncio.to_thread` 调用（锁本身是阻塞的）。
2. **`gemini_wire.py`**（约 400 行）：纯函数，移植上游转换层中实际需要的部分：
   messages→contents/systemInstruction（含 functionResponse 名字反查、角色交替修复、签名哨兵）、工具 schema 清洗、tool_choice、
   图片 `image_url`/data URL → `inlineData`（social_media 会给模型看图）、非流式响应和流式事件 → nanobot 的 `LLMResponse`/delta 回调。
   Gemini 的 thought 文本放进 `reasoning_content`；functionCall 的签名放进 `ToolCallRequest.extra_content`。
3. **`antigravity_provider.py`**（约 300 行）：`AntigravityProvider(LLMProvider)`，用 `httpx.AsyncClient` 实现。
   - `loadCodeAssist` 获取项目 ID、`fetchAvailableModels` 获取模型目录（都缓存）；tiered 解析；`reasoning_effort` → `thinkingLevel`；
   - `chat` 走 `:generateContent`，`chat_stream` 走 `:streamGenerateContent?alt=sse` 并调用三个 delta 回调；401 时刷新并重试一次；
   - 把 HTTP 错误映射到 `LLMResponse` 的 error_* 字段（429 设 `error_should_retry`，Retry-After 写入 `error_retry_after_s`），`usageMetadata` → `LLMUsage.reported`。
   - 不接 provider_state 和 native compaction（`can_resume_conversation_state` 保持默认）。

### 修改现有文件（参照 xai_grok 的接入点）

- `registry.py`：新增 `ProviderSpec(name="antigravity", keywords=("antigravity",), backend="antigravity", is_oauth=True, model_catalog="hybrid", strip_model_prefixes=("antigravity",), builtin_models=(gemini-3.8-flash-*, …))`。
- `factory.py`：增加 `backend == "antigravity"` 分支；`config/schema.py`：ProvidersConfig 增加 `antigravity` 字段。
- `oauth_model_catalog.py`：在线目录使用 `fetchAvailableModels`。
- `cli/provider.py`：status 显示凭据状态（是否已登录、过期时间），不显示 token。
- WebUI 设置页：可选，第一阶段不做，用 config.json 手动选择模型。

### 部署

- 在宿主机安装 agy 并登录，生成 `~/.gemini/antigravity-cli/antigravity-oauth-token`。
- 把凭据目录挂载进容器（例如 `~/.gemini/antigravity-cli:/data/antigravity`，**读写**，因为刷新后要写回；锁文件也放在这里，
  与宿主机上的 agy 用 flock 协调，同一文件系统的 bind mount 可以生效）。
- compose.env 设置 `ANTIGRAVITY_OAUTH_CLIENT_ID/SECRET`（从 agy 二进制一次性提取，不提交到仓库）。
- 在 config.json 中把某个模型预设或 fallback 改为 `antigravity/gemini-3.8-flash-medium`。建议先作为 fallback 或辅助模型试用，不要直接替换主模型。

### 建议分期（沿用 Codex 编排流程）

- **AGY-0**（只读）：确认 nanobot 消息格式中图片和 reasoning 的实际形态、xai_grok 的全部接入点、`oauth_model_catalog` 的接口；用宿主机的 agy 凭据手工调用 `loadCodeAssist`/`fetchAvailableModels`，验证协议仍然可用。
- **AGY-a**：`antigravity_auth.py` + `gemini_wire.py`，只写纯函数测试（转换的往返、签名保留、角色交替、流式参数后缀、刷新与锁）。
- **AGY-b**：provider + registry/factory/schema/catalog/CLI status 登记；mock HTTP 测试（401 重试、tiered 解析、错误映射、流式回调）。
- **AGY-c**：部署挂载与环境变量，先作为 fallback 上线，用真实 social turn 验证工具调用、贴纸、多段发送和看图。

预计新增约 950 行生产代码和相应测试，修改 6–8 个现有文件。
