**1. 问题 1–10 的逐条事实**

**问题 1：OAuth provider 的全部接入点**

以下“必须”区分运行必需和某项功能必需；登录、在线目录、WebUI 编辑并非 `chat()` 的前提。

| 位置 | 当前行为 | 新 provider 是否必须加入，以及遗漏结果 |
|---|---|---|
| `nanobot/providers/registry.py:35`、`:472` | `ProviderSpec` 定义身份、backend、OAuth、目录、模型元数据；xAI 使用独立 backend、`is_oauth=True`、`model_catalog="hybrid"`。 | **内置 provider 必须**。否则无法按内置身份解析；动态 provider 默认采用 OpenAI-compatible backend，见 `registry.py:803`。 |
| `nanobot/config/schema.py:565`、`:574` | 三个 OAuth provider 都有显式配置字段，并设置 `exclude=True`；额外配置字段若与 registry 中的内置名字冲突，会被拒绝。 | **内置 provider 必须**。只登记 registry、不登记字段，不能正常获取对应配置；不能靠同名 extra 字段补足。 |
| `nanobot/providers/factory.py:76`、`:168`、`:223` | 根据解析出的 `spec.backend` 构建具体 provider；未命中独立分支进入 `OpenAICompatProvider`。 | **原生 Gemini backend 必须**。遗漏会走 OpenAI-compatible 实现。 |
| `nanobot/providers/factory.py:100` | 只有 `openai_compat`、`openai_codex`、`xai_grok` backend 允许配置 `proxy`。 | **支持 proxy 时必须**；否则非空 proxy 直接触发 `ValueError`。 |
| `nanobot/providers/__init__.py:10`、`:23`、`:33` | `__all__`、延迟导入表和类型检查导入登记公开 provider 类。factory 自己直接导入实现模块。 | **可选公开导出**。不加不妨碍 factory 直接导入，但不能通过包级名称导入新类。 |
| `nanobot/config/loader.py:157` | 保存配置时，给被 `exclude=True` 排除的 Codex/xAI 单独补回 `proxy`、`extra_body`，不保存凭据。 | **若沿用 exclude 且需要持久化这些设置，则必须**；否则保存后丢失设置。Copilot 当前没有这个补回分支。 |
| `nanobot/cli/provider.py:24`、`:30`、`:105`、`:367` | OAuth 名字由 registry 验证；显示名、默认模型、login/logout handler 是独立显式字典。 | **CLI 登录/退出功能可选**。未登记 handler 会明确报“Login/Logout not implemented”；`--set-main` 未提供模型时还依赖默认模型字典，见 `:133`、`:175`、`:204`。 |
| `nanobot/cli/commands.py:710` | `nanobot status` 遍历 registry；OAuth provider 无条件显示 `✓ (OAuth)`，不读取登录状态。 | **无需新增名字分支**；但现有显示不能证明凭据可用。代码自己说明 status 不验证网络和凭据，见 `:731`。 |
| `nanobot/cli/onboard.py:89`、`:1618` | Quick Start 只接纳名单中的 OAuth provider，目前只有 Codex；登录和认证检查也明确只支持 Codex，见 `:1664`、`:1711`。 | **Quick Start 可选**。不加则不出现在该流程；仅扩名单仍不足以完成登录/状态检查。 |
| `nanobot/providers/oauth_model_catalog.py:186`、`:207` | 在线目录获取、失效操作分别按三个 OAuth provider 名字分发。未知目录获取会抛 `ValueError`；未知失效操作没有动作。 | **hybrid 在线目录必须登记获取分支**；失效分支服务于身份变更后的缓存清除。 |
| `nanobot/webui/settings_models.py:296`、`:373`、`:381` | OAuth 状态按名字读取；未知 OAuth 返回 `configured=False`、`login_supported=False`。 | **WebUI 正确识别已有凭据时必须**。否则显示未配置，并影响模型预设选择/保存校验，见 `:971`。 |
| `nanobot/webui/settings_models.py:430`、`:1489` | backend 决定高级字段；OAuth 设置更新还要求名字位于 `_OAUTH_PROXY_PROVIDERS`。 | **WebUI 编辑 proxy/extra_body 时必须**。遗漏名单会返回“unknown provider”；遗漏字段登记不会正常展示对应编辑项。 |
| `nanobot/webui/settings_models.py:527`、`:646`、`:669` | OAuth 的默认目录种类是 `unsupported`；显式 `builtin` 返回内置列表，`hybrid` 调用 OAuth 目录分发。 | **目录能力可选**。不设目录时允许手输模型；设 hybrid 却不登记分发会失败。 |
| `nanobot/webui/settings_models.py:1535`、`:1636`、`:1650`、`:1706` | 登录、完成登录、退出各有显式 provider 分支；完成登录仅接受 Codex/xAI。 | **应用内登录/退出可选**。新增 OAuth 身份不会自动获得登录流程。 |
| `webui/src/components/settings/models/ProviderSettings.tsx:72`、`:724` | 前端另有 OAuth 高级设置名单；认证区域根据 `auth_type` 渲染，登录按钮根据 `oauth_login_supported` 禁用，见 `:845`。 | **高级设置 UI 需要登记**；通用 OAuth 展示不要求新增 provider 名字。 |
| `webui/src/components/settings/models/ProviderSettings.tsx:109`、`:825` | xAI 另有请求设置预设和远程浏览器登录提示。 | **xAI 专属，可选**，不是所有 OAuth provider 的必需逻辑。 |
| `webui/src/components/settings/models/useModelSettingsActions.ts:373`、`:469` | 保存动作根据后端 `advanced_fields` 生成参数；只有 xAI 本地登录预先打开浏览器窗口。 | **通用保存无需新增名字**；浏览器预开行为仅与登录方式有关。 |
| `webui/src/lib/provider-brand.ts:127`、`:209`、`:215` | 品牌别名/图标独立登记；未知品牌返回 `null`，显示名优先使用后端 label。 | **可选外观登记**，不影响实际 provider 路由。 |
| `webui/src/lib/types.ts:628` | provider 名字为普通 `string`，认证类型只有 `api_key/oauth`，不是 provider 名字枚举。 | **无需为新名字扩类型联合**。 |
| `nanobot/webui/settings_capabilities.py:95` | 图片生成设置根据图片 provider 注册表遍历，并从 LLM registry 获取 OAuth 属性。 | **仅新增聊天 provider 无需改**；此处不会因加入聊天 registry 而自动获得图片生成能力。 |

其他按 provider 名字分支、与本接入相关的事实：

- social judge 使用通用 factory，但仅 Codex 会注入它专属的 JSON 输出设置；`oauth_storage_dir` 也只接受 `OpenAICodexProvider`。默认 judge OAuth 目录同样只对 Codex设置。新增主模型 provider 不要求复制这些分支。位置：`nanobot/channels/telegram/social/judge.py:141`、`:149`、`:160`；`nanobot/channels/telegram/social/wiring.py:15`。
- OpenAI-compatible provider 中存在 Gemini 签名补齐、Copilot Responses 路由等专属分支；它们不属于 `LLMProvider` 的自动能力。位置：`nanobot/providers/openai_compat_provider.py:726`、`:1128`。
- 运行身份 `provider_name` 必须是非空字符串，并用于调用记录中的 provider 字段。位置：`nanobot/providers/base.py:697`、`:785`。

**问题 2：模型字符串、预设、fallback、reasoning_effort**

- 配置加载先迁移、再由 Pydantic 解析；factory 快照加载还会解析环境变量。有效预设优先取 `agents.defaults.model_preset`，空值或 `"default"` 才使用 defaults 字段。位置：`nanobot/config/loader.py:122`；`nanobot/providers/factory.py:399`；`nanobot/config/schema.py:757`、`:766`。
- `ModelPresetConfig` 包含 `model`、`provider`、生成参数和上下文窗口；provider 默认 `"auto"`。强制指定 provider 时，其优先级高于模型字符串前缀。位置：`nanobot/config/schema.py:375`、`:803`。
- 自动匹配顺序是：内置显式前缀、动态 provider 前缀、按 registry 顺序匹配 keywords、已配置本地 provider、带 key 的普通 provider、已配置动态 provider。OAuth 不参与最后的通用 key fallback。位置：`nanobot/config/schema.py:813`、`:823`、`:832`、`:841`、`:866`、`:885`、`:894`。
- `keywords` 用于模型字符串的包含匹配，并做大小写及连字符/下划线兼容；不是前缀删除规则。`find_by_name()` 对名字做规范化查找。位置：`nanobot/config/schema.py:819`；`nanobot/providers/registry.py:794`。
- 对 `antigravity/gemini-3.8-flash-medium`，在相应内置身份及配置字段存在的前提下，`provider="auto"` 会由第一段前缀选中该身份；factory 将**完整字符串**作为 `default_model` 传入，runner 也将完整 runtime model 传给请求。通用路径没有删除前缀。位置：`nanobot/config/schema.py:815`、`:828`；`nanobot/providers/factory.py:83`、`:162`、`:180`；`nanobot/agent/runner.py:868`。
- `strip_model_prefix=True` 和 `strip_model_prefixes` 的实际执行位于 `OpenAICompatProvider._request_model_name()`：前者取最后一段，后者仅在首段匹配时删除首段。它们不会自动作用于独立 backend。xAI 自己实现前缀删除。位置：`nanobot/providers/openai_compat_provider.py:880`；`nanobot/providers/xai_grok_provider.py:113`、`:282`。
- `provider_name` 表示配置身份，factory 根据它找 spec/backend 并传入实现；它与模型字符串、请求的 wire model 分别存在。位置：`nanobot/providers/factory.py:84`、`:99`、`:125`、`:180`。
- fallback 字符串表示**预设名字**，不是裸模型 ID；它从 `config.model_presets` 取完整配置。inline fallback 必须有 model/provider，缺失的 token、温度、窗口继承主预设，但 `reasoning_effort` 直接取 fallback 值，不继承主值。位置：`nanobot/config/schema.py:361`、`:752`；`nanobot/providers/factory.py:243`、`:263`。
- 每个 fallback 通过相同 `_make_provider_core()` 重新选择 provider；请求时覆盖 model、max_tokens、temperature；reasoning 未配置则删除请求显式值，随后由该 provider 的 generation 默认值解析。位置：`nanobot/providers/factory.py:289`；`nanobot/providers/fallback_provider.py:549`、`:558`、`:583`；`nanobot/providers/base.py:1463`。
- reasoning 路径为：defaults/预设 → `to_generation_settings()` → provider/snapshot → `LLMRuntime` → runner 请求 kwargs → `chat_stream_with_retry()` → `chat_stream()`，默认流式实现再调用 `chat()`。位置：`nanobot/config/schema.py:385`、`:763`；`nanobot/providers/factory.py:239`、`:394`；`nanobot/utils/llm_runtime.py:31`；`nanobot/agent/runner.py:874`；`nanobot/providers/base.py:1467`、`:1485`、`:1324`。
- 主模型、模型预设和 inline fallback 的 reasoning 类型都是 `str | None`，没有固定枚举；schema 注释列出 low/medium/high/xhigh/max/adaptive/none。WebUI 当前使用自由文本输入。social judge 是例外，只允许 low/medium/high。位置：`nanobot/config/schema.py:369`、`:383`、`:418`、`:179`；`webui/src/components/settings/models/ModelsSettings.tsx:967`。
- WebUI 返回的默认建议值是空值、low、medium、high；hybrid 目录还能返回每个模型的 `reasoning_efforts`。这些元数据不构成主配置字段的枚举限制。位置：`nanobot/webui/settings_models.py:679`、`:988`；`nanobot/config/schema.py:383`。
- 通过有 snapshot loader 的预设切换会重建对应 provider；无 loader 的静态 SDK 路径会复用原 provider，只切换 model/generation。位置：`nanobot/agent/model_presets.py:51`、`:66`。

**问题 3：provider 实际收到的消息**

- 正常上下文先生成一条根 `system`，随后原样接上 history，再追加当前消息；工具循环追加 `assistant` 和 `tool`。正常支持的角色集合为 system/user/assistant/tool；SDK ingest/restore 也接受这四种角色。位置：`nanobot/agent/context.py:284`；`nanobot/agent/runner.py:489`、`:535`；`nanobot/sdk/clients.py:26`、`:116`。
- **不能假设只有一条 system，也不能假设 system 都在开头**：SDK 可以导入位于历史中间的 system；history 回放不排除该角色；ContextBuilder 的 `current_role` 也允许调用者传入 system。位置：`nanobot/sdk/clients.py:46`；`nanobot/session/manager.py:447`；`nanobot/agent/context.py:320`。
- 输入预算裁剪发生时，会收集所有 system 并放到保留历史前面；未裁剪路径没有这项全局重排。位置：`nanobot/agent/context_governance.py:959`、`:976`、`:999`。
- 常规文字是 `str`；图片消息为 list，原生构造的块类型是 `text` 和 `image_url`。图片 URL 是 `data:<mime>;base64,...`，图片块另带 `_meta.path`。位置：`nanobot/agent/context.py:344`；`nanobot/utils/helpers.py:340`。
- `read_file` 读图片返回同样的图片/文字 list；social media 也返回这种 list，因此 **tool 消息可以包含图片**。runner 并不把工具结果强制转为字符串。位置：`nanobot/agent/tools/filesystem.py:349`；`nanobot/channels/telegram/social/media.py:169`；`nanobot/agent/runner.py:535`；`nanobot/agent/context_governance.py:709`。
- 工具结果规范化会处理 list 中的 text，其他带字符串 `type` 的块被保留；SDK 导入的 content 也没有块类型白名单。因此，“当前内置正常产生的块只有 text/image_url”不等于整个 provider 边界只可能出现这两种块。位置：`nanobot/agent/context_governance.py:734`；`nanobot/sdk/clients.py:54`。
- HTTP API 不直接透传任意 OpenAI transcript：只接受单条 user，识别 text 和 image_url，data URL 保存为本地媒体，远程 HTTP 图片 URL被拒绝。普通上下文随后重新编码为 data URL。SDK 导入历史则可能保留 HTTP URL或其他块。位置：`nanobot/api/server.py:192`；`nanobot/agent/context.py:353`；`nanobot/sdk/clients.py:61`。
- MCP 工具图片当前被保存为 artifact，工具结果返回文字/元数据字符串，不直接作为图片 list 回送；这与 `read_file/social_media` 不同。位置：`nanobot/agent/tools/mcp.py:704`。
- assistant 消息构造字段包括 `role`、`content`，以及可选 `tool_calls`、`reasoning_content`、`thinking_blocks`。空内容由 builder 表示为 `""`；部分 provider 清洗后可变成 `None`。位置：`nanobot/utils/helpers.py:698`；`nanobot/providers/base.py:829`。
- 每个 tool call 包含 `id`、`type="function"`、`function.name`、JSON 字符串 `function.arguments`；可选保留 `extra_content`、调用级 `provider_specific_fields`、函数级 `provider_specific_fields`。位置：`nanobot/providers/base.py:64`、`:86`。
- 当前请求可能携带的私有字段包括消息/块上的 `_meta`，其中可有 runtime context、路径、provider continuation 的 output/boundary 标记；注入 user 消息还可带 `_recovery_followup_id`。位置：`nanobot/agent/context.py:338`、`:368`；`nanobot/providers/conversation_state.py:16`、`:214`、`:308`；`nanobot/agent/loop.py:984`；`nanobot/session/recovery.py:38`。
- `_channel_delivery`、`_recovery_interrupted` 是持久化记录标记；普通 `get_history()` 重建消息时只复制明确列出的字段，不把这些顶层私有字段传回模型。位置：`nanobot/cli/gateway_runtime.py:566`；`nanobot/session/recovery.py:394`；`nanobot/session/manager.py:447`。

清洗与重试：

| 处理 | 实际规则及是否自动获得 |
|---|---|
| runner 上下文治理 | 删除占位 assistant、无效工具调用、孤立 tool result，补齐缺失工具结果，并应用结果预算；这是 runner 请求路径的通用处理。`nanobot/agent/context_governance.py:316`、`:325`；`nanobot/agent/runner.py:891`。 |
| `_sanitize_request_messages` | 根据调用方提供的 allowed_keys 过滤顶层键；assistant 缺 content 时补 `None`。**不是抽象接口自动执行的拦截器**。`nanobot/providers/base.py:909`、`:923`。 |
| `_sanitize_empty_content` | 删除消息和块上的 `_meta`；处理空字符串、空 text/input_text/output_text 块；dict content 包成 list；清除孤立 UTF-16 surrogate。**需要 provider 显式调用**。`nanobot/providers/base.py:814`。 |
| `_enforce_role_alternation` | 合并相邻 user/assistant，保留 tool 分离；相邻 assistant 有工具调用时可能保留其中一个；删除末尾 assistant，并在必要时补 user。**需要 provider 显式调用**。`nanobot/providers/base.py:1124`。 |
| OpenAI-compatible 专属清洗 | 还会规范化工具 ID/arguments、按 spec 删除历史 reasoning；只有 `spec.name=="gemini"` 才补齐 Gemini 签名。独立 provider 不自动获得。`nanobot/providers/openai_compat_provider.py:710`、`:719`、`:726`、`:764`。 |
| 图片剥离重试 | 通过基类 retry 路径调用时，非瞬时错误可额外尝试删除 image_url；成功后也从原消息中剥离，避免后续循环重犯。直接调用 `chat()` 不经过此包装。`nanobot/providers/base.py:1200`、`:1810`、`:1839`。 |
| `_safe_chat/_safe_chat_stream` | 负责上下文分发、异常转换、调用观察；没有自动调用上述三个消息清洗函数。`nanobot/providers/base.py:1262`、`:1360`。 |

持久化与回放：

- 写入链路是 `ToolCallRequest.to_openai_tool_call()` → runner assistant message → loop 复制消息进入 session → JSONL。loop 不删除嵌套 tool call 的 extra 字段；完整保存和增量保存都序列化完整 message。位置：`nanobot/providers/base.py:86`；`nanobot/agent/runner.py:489`；`nanobot/agent/loop.py:2077`、`:2138`；`nanobot/session/manager.py:1369`；`nanobot/session/incremental_log.py:397`。
- 读回时 JSON 对象整体加入 messages；`get_history()` 整体保留 `tool_calls`，不重建其内部结构。因此非空 `extra_content` 可完整回放。空字典在最初 `to_openai_tool_call()` 的 truthy 判断下不写入。位置：`nanobot/session/manager.py:1082`、`:1110`、`:448`；`nanobot/providers/base.py:101`。
- user/tool 的内联图片持久化时会变成文字占位；这个处理不会删除 tool_calls 中的签名。位置：`nanobot/agent/loop.py:1978`、`:2117`。
- 历史裁剪保留整条消息，compaction 用摘要替换旧前缀、保留后续 delta；没有发现“保留某个有效 tool call 却单独删除其 extra_content”的通用裁剪步骤。被摘要或整条删除的调用自然不再回放。位置：`nanobot/agent/context_governance.py:537`、`:554`、`:826`、`:976`；`nanobot/session/manager.py:332`。
- 已有签名往返、跨 provider、并行调用首个签名等测试，但本任务没有运行它们。位置：`tests/agent/test_gemini_thought_signature.py:20`、`:221`、`:271`、`:296`、`:333`。

**问题 4：工具 schema**

这里列的是**仓库代码可注册的完整内置集合**；实际启用集合受配置、scope、social 开关及远端 MCP 返回值影响，不能在不读取实例、不连接 MCP 的限制下声称获得运行实例清单。加载和注册依据：`nanobot/agent/tools/loader.py:92`；`nanobot/channels/telegram/social/wiring.py:50`；`nanobot/agent/tools/mcp.py:1140`。

| 工具 | schema 定义位置 |
|---|---|
| `apply_patch` | `nanobot/agent/tools/apply_patch.py:46` |
| `run_cli_app` | `nanobot/agent/tools/cli_apps.py:36` |
| `cron` | `nanobot/agent/tools/cron.py:22` |
| `exec_session`、`list_exec_sessions` | `nanobot/agent/tools/exec_session.py:499`、`:692` |
| `read_file`、`write_file`、`edit_file`、`list_dir` | `nanobot/agent/tools/filesystem.py:262`、`:544`、`:842`、`:1098` |
| `generate_image` | `nanobot/agent/tools/image_generation.py:60` |
| `create_goal`、`update_goal` | `nanobot/agent/tools/long_task.py:116`、`:239` |
| `message` | `nanobot/agent/tools/message.py:41` |
| `find_files`、`grep` | `nanobot/agent/tools/search.py:229`、`:528` |
| `my` | `nanobot/agent/tools/self.py:185` |
| `list_sessions`、`send_session_message` | `nanobot/agent/tools/session_messages.py:62`、`:102` |
| `search_sessions`、`read_session` | `nanobot/agent/tools/sessions.py:73`、`:141` |
| `exec` | `nanobot/agent/tools/shell.py:118` |
| `spawn` | `nanobot/agent/tools/spawn.py:24` |
| `web_search`、`web_fetch` | `nanobot/agent/tools/web.py:344`、`:1098` |
| `social_activity`、`social_jargon`、`social_expression` | `nanobot/agent/tools/telegram_social.py:59`、`:71`、`:83` |
| `social_state`、`social_send`、`social_wait` | `nanobot/agent/tools/telegram_social.py:95`、`:116`、`:171` |
| `social_media`、`social_sticker`、`social_memory` | `nanobot/agent/tools/telegram_social.py:210`、`:222`、`:266` |

- `to_schema()` 外层是 OpenAI function 包装：`type/function/name/description/parameters`；参数 schema 中的注释型 `title` 会递归删除，properties 中名为 title 的参数仍保留。定义按内置工具、MCP 工具分别排序并缓存至注册表变化。位置：`nanobot/agent/tools/base.py:30`、`:322`；`nanobot/agent/tools/registry.py:86`。
- 经静态 schema 求值核对，以上内置参数 schema 的关键字并集为：  
  `type, properties, required, description, items, enum, additionalProperties, default, minimum, maximum, exclusiveMinimum, minLength, maxLength, minItems, maxItems, pattern, anyOf, oneOf`。来源包括公共 schema 构造器、social 手写 schema 和 Pydantic 模型：`nanobot/agent/tools/schema.py:38`、`:72`、`:136`、`:166`、`:202`；`nanobot/agent/tools/telegram_social.py:122`、`:134`、`:181`；`nanobot/channels/telegram/social/expressions.py:77`；`nanobot/channels/telegram/social/jargon.py:90`。
- `type` **确实会是数组**：nullable 的基础 schema 输出 `["string","null"]` 等；例如 apply_patch、exec、long_task 都实际使用 nullable 参数。位置：`nanobot/agent/tools/schema.py:38`、`:72`、`:136`；`nanobot/agent/tools/apply_patch.py:60`；`nanobot/agent/tools/shell.py:135`；`nanobot/agent/tools/long_task.py:128`。
- Pydantic social 参数使用 `anyOf` 表达可空，正整数 `gt=0` 产生 `exclusiveMinimum`；当前这些内置工具输出没有 `$ref/$defs/allOf/format/const/nullable` 关键字。相关实际模型：`nanobot/channels/telegram/social/activity.py:17`；`nanobot/channels/telegram/social/expressions.py:77`；`nanobot/channels/telegram/social/jargon.py:90`；`nanobot/channels/telegram/social/memory.py:28`；`nanobot/channels/telegram/social/followups.py:16`。
- `my.value` 只有 description，没有 type；空参数工具可以只有 object/properties/additionalProperties，没有 required。位置：`nanobot/agent/tools/self.py:200`；`nanobot/agent/tools/schema.py:208`、`:217`；`nanobot/agent/tools/session_messages.py:62`。
- 外部 entry-point 工具可以自己覆盖 `to_schema()`；兼容包装器直接转发结果，所以内置集合不是整个插件边界的关键字上限。位置：`nanobot/agent/tools/loader.py:68`、`:177`。

MCP 转换路径：

- 远端 `list_tools()` → `MCPToolWrapper` → `inputSchema` → `_normalize_schema_for_openai()` → 公共 `to_schema()` → registry definitions。工具命名为规范化后的 `mcp_<server>_<tool>`。位置：`nanobot/agent/tools/mcp.py:600`、`:609`、`:1140`、`:1160`。
- MCP normalization 将可解析本地引用改写到 `$defs`；把单一非 null 类型加 null 的 type 数组、nullable oneOf/anyOf 转为 `type + nullable`；object 补 properties/required。其他关键字并未统一删除。位置：`nanobot/agent/tools/mcp.py:389`、`:445`、`:496`。
- 因而 MCP 输出能含 `$ref/$defs/nullable/anyOf`，也没有阻止上游携带 allOf、const、format 等关键字；实际内容取决于远端，无法静态穷举。递归引用和保留 `$defs` 已有测试。位置：`nanobot/agent/tools/mcp.py:433`、`:447`；`tests/tools/test_mcp_tool.py:255`、`:283`、`:323`、`:372`。
- MCP resource wrapper 是空 object 参数；prompt wrapper 把参数转换为 string properties/required。两者只在允许全部 MCP tools 时注册。位置：`nanobot/agent/tools/mcp.py:762`、`:883`、`:1182`。

Hermes sanitizer 的事实：

- 定义在 `/home/ubuntu/agy-hermes-agent/agent/gemini_schema.py:73`；调用者位于 `/home/ubuntu/agy-hermes-agent/agent/gemini_native_adapter.py:339`。
- 允许集合是 `type, format, title, description, nullable, enum, maxItems, minItems, properties, required, minProperties, maxProperties, minLength, maxLength, pattern, example, anyOf, propertyOrdering, default, items, minimum, maximum`；递归处理 properties/items/anyOf，其他未允许的键删除。位置：`/home/ubuntu/agy-hermes-agent/agent/gemini_schema.py:10`、`:26`。
- 数字/布尔 enum 转为字符串并去重；required 只保留同节点 properties 中存在的名字；清洗结果为空时返回空 object schema。位置：`/home/ubuntu/agy-hermes-agent/agent/gemini_schema.py:47`、`:57`、`:73`。
- 与 nanobot 实际集合相比，该参考实现会删除 `additionalProperties/oneOf/exclusiveMinimum`，也会删除 MCP 的 `$ref/$defs/allOf/const`。它**没有**引用展开、oneOf 转换、exclusiveMinimum 转换、type 数组转换或 null 分支归一化逻辑；这不能被描述为已经完成这些语义转换。位置：`/home/ubuntu/agy-hermes-agent/agent/gemini_schema.py:26`；`nanobot/agent/tools/telegram_social.py:134`；`nanobot/agent/tools/schema.py:40`；`nanobot/agent/tools/mcp.py:389`。

**问题 5：流式契约、usage、错误和超时**

- 三个回调类型分别为异步 `str → None`、异步 `str → None`、异步 `dict → None`。`chat_stream()` 最终仍返回完整 `LLMResponse`；基类默认实现调用非流式 chat，只把完整 content 发一次，不发送 thinking/tool delta。位置：`nanobot/providers/base.py:1298`。
- runner 即使不展示流式，也调用 `chat_stream_with_retry()`，只是不给 UI delta 回调。位置：`nanobot/agent/runner.py:999`、`:1007`。
- content delta 进入 runner `_stream()`，标记已输出并交给 hook；ProgressHook 再发 `StreamDeltaEvent`。thinking delta 进入 `_thinking()`，累计去标签、增量交给 `emit_reasoning()`，由 ProgressHook 发 reasoning ProgressEvent。位置：`nanobot/agent/runner.py:973`、`:980`；`nanobot/agent/progress_hook.py:54`、`:147`。
- 当前 Responses 解析器的普通工具 delta 字典使用 `call_id/name/arguments_delta`；参数完成时使用 `call_id/name/arguments`，其中 arguments 是字符串。位置：`nanobot/providers/openai_responses/parsing.py:399`、`:477`、`:490`。
- runner 当前只消费 `kind=="hosted_tool"` 的工具 delta；其字段为 `kind/phase/call_id/name/arguments/result`，错误还可带 `error`；phase 为 start/end/error，arguments 是 dict。普通函数 delta 被 runner 忽略；执行依赖最终 response.tool_calls。位置：`nanobot/agent/runner.py:956`；`nanobot/providers/openai_responses/parsing.py:156`；`nanobot/agent/progress_hook.py:86`；`nanobot/agent/runner.py:514`。
- runner/上下文控制器实际使用的 response 字段包括 content、tool_calls、finish_reason、usage、reasoning_content、thinking_blocks、generation_ms、ttft_ms，以及 provider_state、provider_compaction_applied/state/scope、preserve_provider_state_on_error。位置：`nanobot/agent/runner.py:467`、`:489`、`:637`、`:1023`、`:1293`；`nanobot/agent/context_governance.py:448`；`nanobot/providers/conversation_state.py:170`。
- 工具有无取决于最终 tool_calls，且 `"stop"` 是允许执行工具的结束原因之一；不要求一定返回 `"tool_calls"`。`"length"` 会触发 runner 的长度恢复路径。位置：`nanobot/providers/base.py:597`、`:603`；`nanobot/agent/runner.py:637`。
- `LLMUsage.reported()` 接受 input_tokens、output_tokens、可选 total_tokens、cache_read_tokens、cache_write_tokens；total 至少为 input+output；cache 的 None 与 0 有区别；没有单独的 thoughts/reasoning token 参数。位置：`nanobot/providers/base.py:268`、`:340`。
- 基类 safe 调用在 observer 已安装时生成 `LLMCallRecord`；缺失有效 usage 的成功响应可估算，错误/取消不会估算 token。gateway 为快照安装 observer，调用 `record_llm_call()`，最终写入 `llm_usage.sqlite3` 的 `llm_calls`。位置：`nanobot/providers/base.py:717`、`:764`；`nanobot/cli/gateway_runtime.py:427`；`nanobot/llm_usage/__init__.py:41`、`:55`；`nanobot/llm_usage/store.py:259`。
- 记录包含 provider/model/source、stream、finish_reason、token/cache/timing、HTTP status/error kind；source 支持 user/api/cron/dream/system。runner 另行聚合本轮 usage，错误无 usage 时使用 empty request。位置：`nanobot/llm_usage/models.py:19`；`nanobot/llm_usage/store.py:270`；`nanobot/agent/runner.py:1285`。
- 自动重试首先要求 `finish_reason=="error"`；瞬时性优先看 `error_should_retry`，再看 HTTP 状态、error_kind、文本。408/409、5xx 可重试；429 按 type/code/文本区分限流与欠费，未知 429 默认重试。位置：`nanobot/providers/base.py:1012`、`:1088`、`:1771`。
- 等待优先级为 `error_retry_after_s` → `retry_after` → content 中的等待提示；有提示加 1 秒，否则按 1/2/4 秒，标准模式最多四次请求。persistent 模式等待上限 60 秒，连续十次同样错误会停。位置：`nanobot/providers/base.py:31`、`:626`、`:1682`、`:1852`、`:1873`、`:1894`。
- 已输出 content 后普通错误不重试；timeout 是例外，可关闭当前 stream segment 后重试，或禁用后续 delta 回调。thinking delta 本身不设置基类的 `has_streamed_content` 标志。位置：`nanobot/providers/base.py:1470`、`:1778`。
- `resolve_stream_idle_timeout_s()` 只是读取并约束超时值：默认 90 秒，上限 3600 秒。**原生流式 provider 负责执行超时**，runner 没有统一外层 idle timer；xAI/Codex 将该值设给 httpx。只有继承默认非流式 fallback 的 chat_stream 才由基类 `asyncio.wait_for()` 包住整次 chat。位置：`nanobot/providers/base.py:28`、`:39`、`:1324`；`nanobot/agent/runner.py:1013`；`nanobot/providers/xai_grok_provider.py:409`；`nanobot/providers/openai_codex_provider.py:483`。
- 通用异常包装会提取 status/type/code并识别 timeout/connection/authentication 等，但没有自动从 HTTP headers 读取 Retry-After 的步骤。位置：`nanobot/providers/base.py:949`。

**问题 6：xai_grok 的实现模式**

- xAI 使用原始 `httpx.AsyncClient`，没有使用 xAI/OpenAI SDK 发生成请求；每次 `_request_xai()` 新建 client 和 stream，通过 async context manager 关闭。provider 实例仅保存 model/proxy/extra_body。位置：`nanobot/providers/xai_grok_provider.py:73`、`:365`、`:412`。
- `chat()` 与 `chat_stream()` 都进入 `_call_xai()`，生成请求都使用 Responses SSE；非流式调用并不改用 JSON 非流式端点。位置：`nanobot/providers/xai_grok_provider.py:140`、`:236`、`:250`。
- 显式 proxy 同时传给生成请求、OAuth token 获取/刷新、模型目录；httpx 指定 proxy 时设置 `trust_env=False`，未指定时沿用默认环境代理行为。位置：`nanobot/providers/xai_grok_provider.py:118`、`:178`、`:409`、`:665`；`nanobot/providers/xai_oauth.py:711`。
- extra_body 在默认 body 后覆盖字段；tools 单独处理，能扩展/控制 hosted search；显式 tools 会影响默认 X Search 能力探测，非 list tools 最终可直接覆盖 body.tools。位置：`nanobot/providers/xai_grok_provider.py:119`、`:128`、`:160`。
- 每次请求在 `asyncio.to_thread()` 中调用同步 token 获取；401 时强制刷新并重试一次；目录能力读取也通过 `to_thread()`。位置：`nanobot/providers/xai_grok_provider.py:86`、`:118`、`:184`。
- token 获取内部会读文件、检查到期、获取 FileLock、锁内重读、同步 HTTP refresh、原子写回并设置 0600；这些阻塞操作由生成路径的工作线程承担。位置：`nanobot/providers/xai_oauth.py:351`、`:615`、`:718`、`:724`、`:736`。
- 在线目录获取本身是同步 `httpx.Client`，每次刷新新建并关闭；登录状态查询只读本地 store，不联网。位置：`nanobot/providers/xai_grok_provider.py:663`；`nanobot/providers/xai_oauth.py:261`。
- `get_default_model()` 返回构造时保存的默认字符串，用于没有显式 model 的请求、fallback 主模型显示、用量估算和调用记录；它不负责模型发现或去前缀。位置：`nanobot/providers/xai_grok_provider.py:113`、`:278`；`nanobot/providers/fallback_provider.py:451`；`nanobot/providers/base.py:732`、`:780`。

**问题 7：fallback 与 provider_state**

- 不支持 provider_state 的实现可以继承默认 `can_resume_conversation_state=False`、`supports_native_compaction=False`；默认 context hooks 丢弃上下文后调用普通 chat/chat_stream，不强制实现独立状态方法。位置：`nanobot/providers/base.py:801`、`:1340`。
- conversation controller 初始化及每次 prepare 都检查能否恢复；不支持时丢弃 opaque state；成功响应没有新 state 时继续使用普通 transcript。位置：`nanobot/providers/conversation_state.py:51`、`:127`、`:170`。
- fallback 将原请求 kwargs 复制给候选，仅覆盖候选生成参数；不兼容的 opaque state 清空，非 native-compaction 候选的 context_window_tokens 设为 None，session/events 仍保留。**messages 没有被转换为候选 provider 的私有格式**。位置：`nanobot/providers/fallback_provider.py:558`。
- fallback 每次新建候选 provider，并附上同一个 usage observer；成功 fallback 不永久改变主 provider。主 provider 连续三次可切换错误后熔断 60 秒。位置：`nanobot/providers/fallback_provider.py:27`、`:193`、`:503`、`:549`。
- fallback 判定与“同 provider 重试”不同：认证失败、欠费可以触发 fallback；invalid_request/context_length/refusal/content_filter 不切换；400/404/422 默认不切换。位置：`nanobot/providers/fallback_provider.py:672`。
- 从其他 provider 切来时，历史仍是 OpenAI 风格 tool_calls；Codex 的调用 ID还可能包含 `call_id|item_id`。签名可能存在，也可能完全不存在，核心不会给新独立 provider 自动填 Gemini 占位值。位置：`nanobot/providers/base.py:86`；`nanobot/providers/openai_responses/parsing.py:518`；`nanobot/providers/openai_compat_provider.py:802`、`:726`。
- 现有 Gemini OpenAI-compatible 路径仅为一组调用中的**第一个缺签名调用**补占位，并保留后续调用及顺序；Hermes 原生转换则在每次翻译 functionCall 时取签名或占位。两者现有行为不同。位置：`nanobot/providers/openai_compat_provider.py:822`、`:854`；`/home/ubuntu/agy-hermes-agent/agent/gemini_native_adapter.py:203`、`:225`。

**问题 8：测试布局及 HTTP mock**

| 范围 | 相关测试位置 |
|---|---|
| xAI 生成/OAuth/模型目录 | `tests/providers/test_xai_grok_provider.py:75`、`:439`；`tests/providers/test_xai_oauth.py:35`；`tests/providers/test_oauth_model_catalog.py:27` |
| Codex 生成/OAuth | `tests/providers/test_openai_codex_provider.py:42`、`:153`；`tests/providers/test_openai_codex_oauth.py:88` |
| provider 通用契约 | `tests/providers/test_conversation_state.py:43`；`tests/providers/test_enforce_role_alternation.py:10`；`tests/providers/test_stream_idle_timeout_config.py:62` |
| Gemini 元数据与模型预设 | `tests/agent/test_gemini_thought_signature.py:20`；`tests/config/test_model_presets.py:388`；`tests/config/test_env_interpolation.py:187` |
| CLI/WebUI/前端 | `tests/cli/test_commands.py:779`；`tests/webui/test_settings_api.py:1721`、`:2150`；`webui/src/tests/settings-providers.test.tsx:21`；`webui/src/tests/settings-models.test.tsx:1281` |
| MCP schema、部署 | `tests/tools/test_mcp_tool.py:255`、`:323`；`tests/deploy/test_bridge_release.py:169` |

- xAI 高层测试 monkeypatch token、目录和 `_request_xai()`；原始 HTTP/SSE 测试 monkeypatch `httpx.AsyncClient`，返回使用 `httpx.MockTransport(handler)` 的真 client。位置：`tests/providers/test_xai_grok_provider.py:40`、`:69`、`:107`、`:441`。
- Codex 同样使用 monkeypatch `_request_codex()` 或 `httpx.AsyncClient + MockTransport`；模型目录测试使用同步 `httpx.Client + MockTransport`。位置：`tests/providers/test_openai_codex_provider.py:55`、`:153`；`tests/providers/test_oauth_model_catalog.py:90`、`:189`。
- xAI refresh 测试直接替换 `_refresh_token()`，并对存储路径使用临时目录；不要求真实账号。位置：`tests/providers/test_xai_oauth.py:35`、`:217`、`:275`。
- 工程已声明 httpx、pytest、pytest-asyncio 等依赖；全仓库依赖文件搜索未发现 respx 声明。位置：`pyproject.toml:32`、`:93`。本任务没有执行测试或网络验证。

**问题 9：部署、环境变量、bind mount、UID**

- `scripts/deploy-local.sh` 只是转交给 `scripts/bridge_release.py`。实际 Compose 参数生成位于 `_compose()`。位置：`scripts/deploy-local.sh:3`；`scripts/bridge_release.py:237`。
- `_compose()` 每次重写实例的 compose.env，内容仅来自固定的 BRIDGE_IMAGE/DATA_DIR/CHECKOUT_DIR/UID/GID/WEB_PORT/HEALTH_PORT，权限 0600；然后传给 `docker compose --env-file`。因此手工添加其他变量不会被该函数保留。位置：`scripts/bridge_release.py:242`。
- `--env-file` 提供 Compose 插值；容器环境由 service.environment 等配置决定。当前 compose 没有 service.env_file，environment 只显式传入 `NANOBOT_CHECKOUT_DIR`；镜像另声明 PATH、Python 和 source 环境变量。位置：`deploy/compose.local.yml:9`；`deploy/Dockerfile:41`。
- 当前 bind mount 是 `${BRIDGE_DATA_DIR}:/data`，以及 checkout 到相同绝对路径的只读 bind。不存在 agy 凭据目录挂载。新增 bind 的 Compose 位置是 `gateway.volumes`；若宿主路径来自新的 BRIDGE 变量，变量生成归 `_compose().values` 管理。位置：`deploy/compose.local.yml:14`；`scripts/bridge_release.py:242`。
- 凭据目录读写挂载不仅涉及现有 token 文件，还涉及创建锁及临时文件并原子替换；参考代码在 token 同目录执行这些操作。位置：`/home/ubuntu/agy-hermes-agent/agent/antigravity_credentials.py:142`、`:249`。
- 镜像创建并默认使用 UID/GID 1001；Compose 默认也是 1001，但部署脚本显式覆盖为执行脚本进程的 `os.getuid()/os.getgid()`。本机 ubuntu 为 1001:1001，本次执行身份也为 1001:1001。故按 ubuntu 身份执行现有部署脚本时，与题设“0600、属主 ubuntu”的文件所有者 UID匹配；没有读取实际凭据或实例配置。位置：`deploy/Dockerfile:29`、`:44`；`deploy/compose.local.yml:5`；`scripts/bridge_release.py:244`；`/etc/passwd:33`。
- smoke 容器也以执行脚本者 UID/GID 运行，但使用单独 `docker run`，不是上述 Compose volumes/environment；它只挂临时 `/data`，且禁网。位置：`scripts/bridge_release.py:333`。

**问题 10：影响设计的疑点事实**

- 分析文档仍写本任务需要网络验证；本次明确任务禁止网络，两者存在范围差异。本次遵循当前任务，未联网。位置：`docs/AGY_PROVIDER_ANALYSIS.md:63`、`:113`。
- 文档说容器“只挂载 /data”，实际还有 checkout 只读挂载。位置：`docs/AGY_PROVIDER_ANALYSIS.md:75`；`deploy/compose.local.yml:16`。
- 文档提出把 OAuth client 信息写入 compose.env，但现有生成器每次覆盖该文件，且当前 service 不会自动把其中所有变量注入容器。位置：`docs/AGY_PROVIDER_ANALYSIS.md:83`、`:108`；`scripts/bridge_release.py:242`；`deploy/compose.local.yml:9`。
- 文档把 status 接入点写成 `cli/provider.py`；该文件实际只有 login/logout 命令，status 位于 `cli/commands.py`，并且当前 OAuth 状态是无条件勾选。位置：`docs/AGY_PROVIDER_ANALYSIS.md:100`；`nanobot/cli/provider.py:152`、`:193`；`nanobot/cli/commands.py:710`。
- `is_oauth` 本身不要求实现登录，但 WebUI 默认把未知 OAuth 视为未配置；模型选择列表主要展示 configured providers，后端也校验 configured。位置：`nanobot/webui/settings_models.py:373`、`:971`；`webui/src/components/settings/models/ModelsSettings.tsx:287`。
- 模型目录是进程内缓存：默认 fresh 5 分钟、stale 24 小时、失败抑制 30 秒、最多八个 key；并发刷新通过 Condition 合并；invalidate 会推进 generation，阻止旧刷新写回。目录失败仍可返回 stale/builtin，WebUI status 仍为 available。位置：`nanobot/providers/oauth_model_catalog.py:44`、`:74`、`:122`、`:148`；`nanobot/webui/settings_models.py:684`。
- xAI 的缓存 key 包含凭据 store 路径、账号标识及 proxy；没有“读取接口就必定刷新目录”的语义。位置：`nanobot/providers/xai_grok_provider.py:652`；`nanobot/providers/oauth_model_catalog.py:130`。
- 参考 sanitizer 的允许集合无法覆盖 nanobot 全部 schema 语义，且 MCP 存在递归引用；直接删除引用会丢失参数结构。位置：`/home/ubuntu/agy-hermes-agent/agent/gemini_schema.py:26`；`nanobot/agent/tools/mcp.py:389`；`tests/tools/test_mcp_tool.py:323`。
- 现有凭据保护名单包含实例 config/auth、social OAuth、oauth-cli-kit 和 Codex auth，没有 agy 凭据路径；文件工具与 shell sandbox 都依赖这套保护。位置：`nanobot/security/credentials.py:8`；`nanobot/agent/tools/path_utils.py:6`；`nanobot/agent/tools/sandbox.py:16`。
- 普通函数工具 delta 当前不进入 runner 的进度展示，只有 hosted_tool 被消费；工具执行仍依赖最终聚合结果。位置：`nanobot/agent/runner.py:956`、`:514`。

**2. 实际运行的命令**

以下为实际执行的主要搜索命令；另使用 `nl -ba … | sed -n …` 核对行号，并用只读 Python AST/schema 求值核对内置参数关键字。没有运行构建、测试、部署或网络命令。

```bash
rg --files -g 'AGENTS.md' -g '.agent/**' -g '*AGY*' \
  -g '*provider*' -g '*oauth*' -g '*conversation_state*' \
  -g '*fallback*' -g '*compose*' -g '*deploy-local*' \
  -g '*runner*' -g '*context*' -g '*base.py'

rg -n 'xai_grok|is_oauth|backend ==|backend in' nanobot tests webui/src

grep -rnE 'xai_grok|is_oauth|backend ==|backend in' \
  nanobot tests webui/src \
  --include='*.py' --include='*.ts' --include='*.tsx' \
  | cut -d: -f1 | sort -u

grep -rnE 'strip_model_prefix(es)?|reasoning_effort|provider_name' \
  nanobot tests webui/src \
  --include='*.py' --include='*.ts' --include='*.tsx' \
  | cut -d: -f1 | sort -u | head -100

grep -rnE 'extra_content|provider_specific_fields|thinking_blocks|_sanitize_request_messages|_sanitize_empty_content|_enforce_role_alternation|resolve_stream_idle_timeout_s|on_tool_call_delta|record_llm_call' \
  nanobot tests webui/src \
  --include='*.py' --include='*.ts' --include='*.tsx' \
  | cut -d: -f1 | sort -u

grep -rnE 'to_schema\(|inputSchema|\$ref|\$defs|oneOf|allOf|exclusiveMinimum|nullable' \
  nanobot tests webui/src \
  --include='*.py' --include='*.ts' --include='*.tsx' \
  | cut -d: -f1 | sort -u | head -85

rg -n 'openai_codex|github_copilot|openai-codex|github-copilot' \
  nanobot --glob '*.py'

rg -n 'sanitize_gemini_tool_parameters' \
  /home/ubuntu/agy-hermes-agent/agent \
  /home/ubuntu/agy-hermes-agent/plugins/model-providers/antigravity

rg --files tests/providers | sort

rg -n 'respx|MockTransport|AsyncClient|monkeypatch.setattr|fake_client' \
  tests/providers/test_xai_grok_provider.py \
  tests/providers/test_openai_codex_provider.py \
  tests/providers/test_xai_oauth.py \
  tests/providers/test_openai_codex_oauth.py \
  tests/providers/test_oauth_model_catalog.py

rg -n 'compose.env|BRIDGE_UID|BRIDGE_GID|BRIDGE_CHECKOUT_DIR|env_file|compose.*--env-file|id -u|os.getuid|local.yml|docker compose' \
  scripts deploy nanobot tests webui/src

rg -n 'antigravity|cloudcode|gemini_wire|gemini_native' nanobot tests webui/src
rg -n '^ubuntu:' /etc/passwd
id
```

**3. 疑点 / 需要编排者决定的事项**

- **应用内认证范围**：只识别 CLI 已有凭据，还是包含登录/退出；现有 CLI/WebUI 的实现入口并不统一。依据：`nanobot/cli/provider.py:175`；`nanobot/webui/settings_models.py:373`、`:1636`。
- **WebUI 接受程度**：若暂不实现设置页，是否接受 provider 显示未配置及预设操作受阻。依据：`nanobot/webui/settings_models.py:971`；`webui/src/components/settings/models/ModelsSettings.tsx:287`。
- **schema 语义范围**：对实际存在的 type 数组、oneOf、exclusiveMinimum，以及 MCP 递归 `$ref/$defs`，验收覆盖到什么程度；参考 sanitizer 没有实现相应语义转换。依据：`nanobot/agent/tools/schema.py:40`；`nanobot/agent/tools/telegram_social.py:134`；`/home/ubuntu/agy-hermes-agent/agent/gemini_schema.py:26`。
- **目录刷新与模型映射依据**：模型选择是否接受 stale/builtin 目录，以及目录身份/失效范围；当前 hybrid “available”不能证明在线刷新成功。依据：`nanobot/providers/oauth_model_catalog.py:148`；`nanobot/webui/settings_models.py:684`。
- **部署环境变量的持久来源**：现有 compose.env 会重写，且不是 service.env_file；后续简报需要明确这部分文件范围。依据：`scripts/bridge_release.py:242`；`deploy/compose.local.yml:9`。
- **凭据挂载位置与现有工具保护的关系**：目前保护名单没有 agy 路径。依据：`nanobot/security/credentials.py:8`。
- **签名回放验收**：现有 Gemini-compatible 与 Hermes-native 的占位填充粒度不同；跨 provider 历史确实可能完全无签名。依据：`nanobot/providers/openai_compat_provider.py:854`；`/home/ubuntu/agy-hermes-agent/agent/gemini_native_adapter.py:225`。
- **运行时 MCP 清单的证据边界**：本报告穷举仓库内置工具及转换路径，没有读取实例或连接远端；运行时插件/MCP schema 的最终集合仍未知。依据：`nanobot/agent/tools/loader.py:68`；`nanobot/agent/tools/mcp.py:1140`。