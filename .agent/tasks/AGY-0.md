# 任务 AGY-0：AGY provider 接入前的只读事实清单

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode”）。**本任务只读**：不修改任何文件，只输出报告。

## 背景

计划给 nanobot 新增一个 `antigravity` provider：用 Google Antigravity CLI（agy）的 OAuth 凭据，直接调用 Cloud Code Assist 私有接口
（`https://daily-cloudcode-pa.googleapis.com/v1internal:{loadCodeAssist,fetchAvailableModels,generateContent,streamGenerateContent}`），
请求与响应使用 **Gemini 原生格式**（contents/parts/functionCall/functionResponse/thoughtSignature）。分析与方案见 `docs/AGY_PROVIDER_ANALYSIS.md`。
nanobot 目前没有原生 Gemini 转换层，参考实现在 `/home/ubuntu/agy-hermes-agent`（只读参考，不要修改）：
`agent/antigravity_credentials.py`、`agent/antigravity_client.py`、`agent/gemini_native_adapter.py`、`plugins/model-providers/antigravity/__init__.py`。

Claude 已用真实凭据验证（不需要你再验证网络）：
- 目标模型 `gemini-3.8-flash-medium` 在账号目录中不存在，只有 `gemini-3.8-flash-tiered`；用 tiered 模型并设 `thinkingConfig.thinkingLevel="medium"` 可正常工作。
- 流式事件为 `{"response": {candidates, usageMetadata, modelVersion, responseId}, "metadata", "traceId"}`；functionCall 带 `id`；
  有工具调用时 finishReason 仍是 `STOP`；`includeThoughts: true` 时也没有返回 thought 文本，只有 `thoughtsTokenCount`。
- 回放历史中的 functionCall 缺少 `thoughtSignature` 时返回 400；填 `skip_thought_signature_validator` 可通过。

本任务的目的：在 Claude 设计实施简报之前，从 nanobot 现有代码中穷举接入所需的事实。**每条事实都要给出 `文件:行号`**，并用全仓库搜索（`grep -rn ... nanobot tests webui/src`）核实，不能只看单个模块。

## 需要回答的问题

1. **OAuth provider 的全部接入点**：以 `xai_grok` 为参照（必要时对照 `openai_codex`、`github_copilot`），列出新增一个 OAuth provider 需要改动的每个位置：
   `providers/registry.py`、`providers/factory.py`、`providers/__init__.py`、`config/schema.py`、`config/loader.py`、`cli/provider.py`、`providers/oauth_model_catalog.py`、
   `webui/settings_models.py`、WebUI 前端、以及任何按 provider 名字或 backend 字符串分支的地方（搜索 `"xai_grok"`、`is_oauth`、`backend ==`、`backend in`）。
   每处说明：现在做什么、新 provider 是**必须**加还是**可选**（不加会出什么问题）。
2. **模型字符串的解析**：配置 `antigravity/gemini-3.8-flash-medium` 时，从 config/预设到 provider 的完整路径；provider 收到的 `model` 是否带前缀；
   `strip_model_prefix(es)`、`keywords`、`provider_name` 各起什么作用；模型预设（ModelPresetConfig）和 fallback 预设如何选择 provider；`reasoning_effort` 从配置到 `chat()` 参数的路径和可取值。
3. **provider 收到的消息形态**（runner/context 实际传给 `chat`/`chat_stream` 的 `messages`）：
   - 所有 role；system 消息的个数和位置（是否可能有多条、是否出现在中间）；
   - `content` 的所有形态：str、list 块（列出所有块类型，例如 text、image_url（data URL 还是 http URL）、其他）；tool 消息的 content 能否是 list/含图片；
   - assistant 消息上可能出现的键：`tool_calls`（及其中的 `extra_content`、`provider_specific_fields`）、`reasoning_content`、`thinking_blocks`、以及任何以下划线开头的私有键；
   - 发送前 base 类做了哪些清洗（`_sanitize_request_messages`、`_sanitize_empty_content`、`_enforce_role_alternation`、图片剥离重试等），新 provider 是否自动获得这些处理；
   - 会话持久化与回放时，`ToolCallRequest.extra_content` 是否完整保留（给出写入和读回的位置）；compaction 或历史裁剪后，assistant 的 tool_calls 是否可能失去 extra_content。
4. **工具 schema**：列出全部已注册工具（包括 social 工具和 MCP 工具的转换路径）`to_schema()` 输出中实际出现的 JSON Schema 关键字集合
   （例如 type、properties、required、items、enum、anyOf、oneOf、allOf、$ref、$defs、additionalProperties、default、format、minimum、maxLength、const、nullable、type 为列表等）。
   对照 hermes 的 `sanitize_gemini_tool_parameters`（找到它的定义并概述其规则），指出哪些关键字需要转换或删除。
5. **流式契约**：`chat_stream` 的三个回调（`on_content_delta`、`on_thinking_delta`、`on_tool_call_delta`）分别由谁消费、期望的参数形态（tool_call_delta 字典的键）；
   返回的 `LLMResponse` 中 runner 实际读取的字段；`usage`（`LLMUsage.reported` 的参数）如何被记账（`record_llm_call`/llm_usage）；
   `error_*` 字段与重试策略（哪些字段决定是否重试、等待多久）；流式空闲超时 `resolve_stream_idle_timeout_s` 由谁负责执行。
6. **xai_grok 的实现模式**：HTTP 客户端（httpx.AsyncClient 还是 SDK）、生命周期（每次请求新建还是复用、何时关闭）、proxy 处理、`extra_body` 的用途；
   OAuth token 的获取/刷新在哪里调用、是否在 async 中做阻塞 IO（以及怎么做的）；`get_default_model` 的作用。
7. **fallback 与 provider_state**：`fallback_provider.py` 和 `conversation_state.py` 对一个不支持 provider_state 的新 provider 有什么要求；
   主模型从另一个 provider 切换过来时，历史里的 tool_calls 会是什么样（这关系到 thoughtSignature 占位值）。
8. **测试布局**：provider 相关测试在哪些目录和文件；xai_grok/openai_codex 的测试如何 mock HTTP（respx、httpx.MockTransport 或其他）；工程依赖中是否已有 respx。
9. **部署**：`deploy/compose.local.yml`、`scripts/deploy-local.sh`、实例的 `compose.env`（只看仓库里的模板或脚本如何生成它，**不要读取** `~/.local/share/nanobot-bridge-tg-*` 下的文件）中：
   环境变量如何进入容器；新增一个 bind mount（宿主机 `~/.gemini/antigravity-cli` → 容器内某路径，可读写）需要改哪里；容器内运行用户的 uid 与宿主机文件权限（0600，属主 ubuntu）是否匹配。
10. **疑点**：你在阅读中发现的、会影响设计的任何问题（例如：某处假设所有 OAuth provider 都有登录流程、WebUI 设置页对未知 provider 的行为、模型目录接口的缓存语义）。

## 防御性代码规则
本任务只读，不改代码。

## 允许改动的路径
无。只输出报告。

## 不要做
- 不要修改任何文件，不要运行网络请求，不要读取 `~/.gemini`、`~/.local/share/nanobot-bridge-tg-*`、`~/.codex`。
- 不要给出设计方案或代码草稿；只列事实，疑点写在第 10 节。

## 完成标准
- 10 个问题全部回答，每条事实附 `文件:行号`。

## 报告格式（最终回复只包含以下内容）
1. 问题 1–10 的逐条事实（每条附 `文件:行号`）
2. 实际运行的命令（搜索命令即可）
3. 疑点 / 需要编排者决定的事项
