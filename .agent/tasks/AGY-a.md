# 任务 AGY-a：Antigravity 凭据模块与 Gemini 原生格式转换（纯函数 + 测试）

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。

## 背景

nanobot 要新增 `antigravity` provider（下一个任务 AGY-b 实现 provider 本身和登记）。它共用宿主机 Antigravity CLI（agy）的 OAuth 凭据，
调用 Cloud Code Assist 私有接口，请求和响应使用 **Gemini 原生格式**。分析见 `docs/AGY_PROVIDER_ANALYSIS.md`，事实清单见 `.agent/tasks/AGY-0.report.md`。
参考实现（只读，不要复制其结构）：`/home/ubuntu/agy-hermes-agent/agent/antigravity_credentials.py`、`agent/gemini_native_adapter.py`。

本任务只做两个**不发生网络请求的**模块及其测试：凭据读取/刷新（刷新用注入的 httpx.Client，测试用 MockTransport），以及 OpenAI 风格消息 ↔ Gemini 原生格式的转换。

Claude 已用真实账号验证、**设计以此为准**的接口事实：
- 凭据文件 `antigravity-oauth-token` 的 JSON：`{"auth_method": "consumer", "id_token_email": "...", "token": {"access_token", "refresh_token", "token_type", "expiry", "expires_in"}}`，
  `expiry` 形如 `2026-11-05T11:35:30.400902766+09:00`（小数部分 9 位）。
- 工具声明用 `functionDeclarations[].parametersJsonSchema` 直接传完整 JSON Schema（类型数组、oneOf、anyOf、exclusiveMinimum、additionalProperties、递归 `$ref/$defs` 全部被接受）。
  **不要**用 `parameters` 字段，**不要**写 schema 清洗器。
- 历史中的 functionCall 必须带 `thoughtSignature`，没有时填 `"skip_thought_signature_validator"`（缺失会 400）。
- tool 结果中的图片放在 `functionResponse.parts: [{"inlineData": {"mimeType", "data"}}]`，用户消息中的图片是普通 part `{"inlineData": {...}}`，两种都已验证可用。
- 响应：`candidates[0].content.parts[]`，part 可能是 `{"text"}`、`{"text", "thought": true}`、`{"functionCall": {"name", "args", "id"}, "thoughtSignature"}`、或 `{"text": "", "thoughtSignature"}`；
  有工具调用时 finishReason 仍是 `"STOP"`；`usageMetadata` 含 `promptTokenCount`、`candidatesTokenCount`、`thoughtsTokenCount`、`totalTokenCount`，可能含 `cachedContentTokenCount`。
  流式（SSE）每个事件就是一个这样的响应对象（provider 负责拆掉外层 `{"response": ...}`，本任务的函数收到的是内层对象）。

## 设计（已定，不得改变）

### 文件 1：`nanobot/providers/antigravity_auth.py`（新增，同步代码，约 130 行）

```python
DEFAULT_DIR = Path.home() / ".gemini" / "antigravity-cli"
TOKEN_FILE = "antigravity-oauth-token"          # agy 拥有，nanobot 只改 token 子字段
CLIENT_FILE = "nanobot-oauth-client.json"        # {"client_id": "...", "client_secret": "..."}，由运维手工放入
LOCK_FILE = ".nanobot-antigravity.lock"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REFRESH_SKEW_SECONDS = 60

class AntigravityAuthError(RuntimeError): ...

@dataclass(frozen=True)
class AntigravityToken:
    access_token: str
    refresh_token: str
    expiry: datetime          # 带时区（UTC）
    email: str | None         # 文档顶层 id_token_email

def antigravity_dir() -> Path                     # 环境变量 ANTIGRAVITY_DIR（expanduser）或 DEFAULT_DIR
def read_token(directory: Path) -> AntigravityToken
def get_access_token(directory: Path, *, rejected: str | None = None, client: httpx.Client | None = None) -> str
def login_status(directory: Path) -> dict[str, Any]   # {"configured": bool, "account": str | None, "expires_at": int | None(毫秒)}
```

规则：
- `read_token`：文件不存在或 `token.access_token` 为空 → `AntigravityAuthError`（消息提示先在宿主机运行 `agy` 登录）。
  expiry 解析：把小数部分截到 6 位后用 `datetime.fromisoformat`，转成 UTC。
- `get_access_token`：用 `filelock.FileLock(directory / LOCK_FILE, timeout=15)`（与 `xai_oauth.py` 同一个库）加锁，**锁内**重新 `read_token`，然后：
  - `rejected is None` 且未在 `REFRESH_SKEW_SECONDS` 内到期 → 返回文件中的 access_token；
  - `rejected` 不为空，但文件中的 access_token 已不等于 `rejected` 且未在 skew 内到期 → 返回文件中的值（别的进程已刷新）；
  - 否则刷新：读 `CLIENT_FILE` 取 client_id/client_secret（文件缺失 → `AntigravityAuthError`），
    `POST TOKEN_ENDPOINT`，表单 `client_id, client_secret, refresh_token, grant_type=refresh_token`，timeout 30 秒；
    `client` 为 None 时自己创建并在结束时关闭 `httpx.Client()`。非 200 → `AntigravityAuthError(f"Antigravity token refresh failed: HTTP {status} {error}")`，
    `error` 取响应 JSON 的 `error` 字段（取不到就写 `unknown`），**不得**在消息或日志中包含任何 token 或 secret。
  - 刷新成功：在原文档上只更新 `token.access_token`、`token.expiry`（`now + expires_in` 秒，`isoformat()`）、`token.expires_in`，
    响应里有 `refresh_token` 时也更新 `token.refresh_token`；其他键原样保留。原子写回：同目录 `tempfile.mkstemp` → 写入 → `flush` + `os.fsync` → 权限 0600 → `os.replace`。返回新 access_token。
- `login_status`：`read_token` 成功 → `configured = bool(refresh_token)`，`account = email`，`expires_at = int(expiry.timestamp() * 1000)`；
  `AntigravityAuthError` → `{"configured": False, "account": None, "expires_at": None}`。不发网络请求。

### 文件 2：`nanobot/providers/gemini_wire.py`（新增，纯函数，约 230 行）

```python
SKIP_SIGNATURE = "skip_thought_signature_validator"

def build_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    *,
    max_output_tokens: int,
    temperature: float | None,
    thinking_level: str | None,
) -> dict[str, Any]

class GeminiResponseAccumulator:
    def feed(self, event: dict[str, Any]) -> list[tuple[Literal["content", "thinking"], str]]
    def response(self) -> LLMResponse
```

`build_request` 规则（输入是已经过 `LLMProvider._sanitize_empty_content` 的 OpenAI 风格消息；本模块不再调用它）：
1. `role == "system"`：任何位置的 system 消息，其文本按出现顺序用 `"\n\n"` 连接成 `systemInstruction = {"parts": [{"text": ...}]}`；没有 system 时不输出该键。
2. 内容转 parts（user、assistant、tool 共用一个私有函数）：
   - `str` → `[{"text": s}]`；
   - `list`：`{"type": "text", "text"}` → `{"text"}`；`{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<data>"}}` → `{"inlineData": {"mimeType": mime, "data": data}}`；
     `image_url` 的 url 不是 `data:` 开头 → `{"text": f"[image: {url}]"}`；其他块类型忽略；
   - `None` → `[]`。
3. `role == "user"` → `{"role": "user", "parts": <内容 parts>}`。
4. `role == "assistant"` → `{"role": "model", "parts": <内容 parts> + <每个 tool_call 一个 part>}`，tool_call part 为
   `{"functionCall": {"name": fn.name, "args": <arguments 解析成 dict>, "id": tc.id}, "thoughtSignature": <签名或 SKIP_SIGNATURE>}`。
   arguments 解析用 `nanobot.providers.base.tool_arguments_object_for_replay`。签名取 `tc["extra_content"]["google"]["thought_signature"]`，取不到就用 `SKIP_SIGNATURE`（每个 tool_call 各自判断）。
   `reasoning_content`、`thinking_blocks` 不发送。parts 为空的 assistant 消息不输出。
5. `role == "tool"` → `{"role": "user", "parts": [{"functionResponse": {"name": <按 tool_call_id 查前面 assistant 的 tool_call 名字>, "id": tool_call_id, "response": {"output": <内容中所有 text 用 "\n" 连接>}}}]}`；
   内容含图片时，图片的 inlineData parts 放进该 functionResponse 的 `"parts"` 键。
6. 相邻且 role 相同的 contents 合并为一个（parts 依次拼接）。这样连续的多个 tool 结果、以及 tool 结果后紧跟的 user 文本，会落在同一个 user content 中。
7. `tools` 非空 → `"tools": [{"functionDeclarations": [{"name", "description", "parametersJsonSchema": fn["parameters"]}]}]`；无 description 时省略该键，无 parameters 时省略 `parametersJsonSchema`。
8. `tool_choice`：`"auto"`/`"required"`/`"none"` → `"toolConfig": {"functionCallingConfig": {"mode": "AUTO"/"ANY"/"NONE"}}`；
   `{"type": "function", "function": {"name": n}}` → `{"mode": "ANY", "allowedFunctionNames": [n]}`；`None` → 不输出。
9. `generationConfig = {"maxOutputTokens": max_output_tokens}`，`temperature` 不为 None 时加入；`thinking_level` 不为 None 时加入 `"thinkingConfig": {"thinkingLevel": thinking_level}`。
10. 返回 `{"contents": [...], "systemInstruction"?, "tools"?, "toolConfig"?, "generationConfig": {...}}`。

`GeminiResponseAccumulator` 规则（流式和非流式共用：非流式就是只 feed 一次）：
- `feed(event)`：遍历 `event["candidates"][0]["content"]["parts"]`（缺失视为空）：
  - `thought` 为真的 text part → 追加到 reasoning，返回 `("thinking", text)`；
  - 其他非空 text part → 追加到 content，返回 `("content", text)`；
  - `functionCall` part → 追加一个 `ToolCallRequest(id=fc.get("id") or f"call_{uuid4().hex[:24]}", name=fc["name"], arguments=fc.get("args") or {}, extra_content={"google": {"thought_signature": sig}} if sig else None)`，
    sig 是该 part 的 `thoughtSignature`；
  - 只有 thoughtSignature 的空 text part 忽略。
  - `finishReason` 存在时记录；`usageMetadata` 存在时用最新值覆盖。
  - 返回本次事件产生的 delta 列表（按 parts 顺序）。
- `response()` → `LLMResponse(content=拼接的 content 或 None, tool_calls=..., finish_reason=..., usage=..., reasoning_content=拼接的 reasoning 或 None)`：
  - finish_reason：`"MAX_TOKENS"` → `"length"`；`"SAFETY"`、`"RECITATION"`、`"PROHIBITED_CONTENT"`、`"BLOCKLIST"`、`"SPII"` → `"content_filter"`；其他（含 `"STOP"`、缺失）→ `"stop"`。有工具调用也保持 `"stop"`（runner 允许 stop 执行工具）。
  - usage：有 usageMetadata 时 `LLMUsage.reported(input_tokens=promptTokenCount, output_tokens=candidatesTokenCount + thoughtsTokenCount, total_tokens=totalTokenCount, cache_read_tokens=cachedContentTokenCount)`，
    缺失的计数按 0（cachedContentTokenCount 缺失时传 None）；没有 usageMetadata 时 `usage=None`。

## 要做的事
1. 新建上述两个文件，签名与规则严格按“设计”。
2. 新建测试 `tests/providers/test_antigravity_auth.py`：
   - 用 `tmp_path` 写凭据文件；未到期 → 不发请求直接返回；到期 → 用 `httpx.Client(transport=httpx.MockTransport(handler))` 刷新，断言表单字段、写回后的文件（其他键保留、权限 0600、新 expiry 可再次被 `read_token` 解析）；
   - `rejected` 等于文件值 → 刷新；`rejected` 不等于文件值且未到期 → 不刷新、返回文件值；
   - 刷新返回 400 `{"error": "invalid_grant"}` → `AntigravityAuthError`，消息含 `invalid_grant`，不含 refresh_token 和 client_secret 的值；
   - 缺 CLIENT_FILE 时到期 → `AntigravityAuthError`；9 位小数 expiry 解析；`login_status` 的两种结果。
3. 新建测试 `tests/providers/test_gemini_wire.py`：
   - 多个 system（含中间位置）合并；data URL 图片 → inlineData；非 data URL → text 占位；
   - assistant tool_calls：有签名的保留原签名，无签名的用 SKIP_SIGNATURE，arguments 为 JSON 字符串时解析成 dict；
   - tool 结果：name 按 tool_call_id 反查；含图片时进入 functionResponse.parts；两个连续 tool 结果 + 一条 user 文本合并为一个 user content；
   - tools 使用 parametersJsonSchema 原样传递（含 `"type": ["string", "null"]` 和 `$defs` 的 schema 不被修改）；tool_choice 四种形态；generationConfig 三个键的有无；
   - accumulator：用验证过的真实事件形态（上面“背景”中的 part 形态）测试多事件流式：content/thinking delta 顺序、functionCall → ToolCallRequest（id、args、extra_content 签名）、
     MAX_TOKENS → length、usage 计算（output = candidates + thoughts）、无 usageMetadata → None；
   - 往返：accumulator 产出的 tool_call 经 `ToolCallRequest.to_openai_tool_call()` 放进 assistant 消息，再经 `build_request` 后签名与 id 不变。

## 防御性代码规则
- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项、日志表。
- 不要为“理论上可能”的输入形态（未知块类型之外的其他情况、非 dict 的消息、非 list 的 parts 等）加分支；设计里写了“忽略”的才忽略。
- 认为确有必要的，写在报告的“建议”里，不要自己实现。
- 预期规模：两个生产文件合计不超过 400 行；测试不限。

## 允许改动的路径
- `nanobot/providers/antigravity_auth.py`（新增）
- `nanobot/providers/gemini_wire.py`（新增）
- `tests/providers/test_antigravity_auth.py`（新增）
- `tests/providers/test_gemini_wire.py`（新增）

## 不要做
- 不要改 registry/factory/schema/WebUI 等任何现有文件（AGY-b 做）。
- 不要实现 provider 类、HTTP 生成请求、模型目录、loadCodeAssist。
- 不要移植 hermes 的 schema 清洗、角色交替占位回合、流式参数后缀去重逻辑。
- 不要读取 `~/.gemini`、不要发真实网络请求。

## 完成标准
- `.venv/bin/python -m pytest tests/providers/test_antigravity_auth.py tests/providers/test_gemini_wire.py -q -p no:cacheprovider`
- `.venv/bin/ruff check nanobot/providers/antigravity_auth.py nanobot/providers/gemini_wire.py tests/providers/test_antigravity_auth.py tests/providers/test_gemini_wire.py`
- `.venv/bin/basedpyright nanobot/providers/antigravity_auth.py nanobot/providers/gemini_wire.py`（0 errors）
- 整个社交测试目录（确认无连带影响）：`.venv/bin/python -m pytest nanobot/channels/telegram/tests -q -p no:cacheprovider --ignore-glob="*evaluate_*"`

## 报告格式（最终回复只包含以下内容）
1. 改动摘要（按文件）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因
4. 未解决的问题 / 需要编排者决定的事项
