# 任务 P2-a：抽出 ScopeBindings；校验不写库；改配置或角色时保留会话历史【行为变更】

你是本工程的实施者，处于 Claude 编排模式（见 AGENTS.md “Orchestrated implementation mode” 和 `.agent/orchestration.md`）。

## 背景

`docs/SOCIAL_ARCHITECTURE_REVIEW.md` P0-7 和 P1-2 第一条（owner 已确认 D4：改配置或角色时必须保留会话历史）。现状如下，都在
`nanobot/channels/telegram/social/runtime.py`：

- `_persona(scope)` 每次调用都会：stat + 读取角色文件；把 `SOCIAL_PROTOCOL_VERSION + binding.model_dump() + 整个 config（除 scopes/learning）`
  序列化成 JSON，交给 `store.setting_revision("scope:<key>", ...)` 得到 version；调用 `store.bind(scope, version)`
  （version 变化时取消 wakes、清空 dialogue、wake_config、judge 批次、排队的 actions）；必要时 `store.pause`；调用 `proactive.sync`。
- `validate_context` 每次都调用 `_persona`。一次 `social_send` 至少触发 3 次，hook 中也会调用，所以**校验带有写库副作用**。
- `policy_for_session` 用 `version + persona 文本的 revision` 拼出 `context_version`。只要不同，就截断会话历史
  （`session.last_archived = len(session.messages)`），并清空 `_last_summary`、provider_state、checkpoint、pending_user_turn、goal。
  所以改任何无关配置（judge 超时、dynamic_wait 秒数……）或编辑角色文件，机器人都会“失忆”，并丢掉正在进行的等待。

场景测试 `test_role_edit_keeps_earlier_group_history_in_context` 目前是 strict xfail，它固定的正是这个问题；本任务完成后它必须通过。

## 设计（已定，不得改变）

### 1. 新文件 `nanobot/channels/telegram/social/bindings.py`

```python
@dataclass(frozen=True)
class ResolvedScope:
    binding: TelegramSocialScopeConfig
    persona: str            # SOCIAL_PROTOCOL + "# Operator persona" + 角色文本 + "# Operator group background" + background，拼接方式与现在 _persona 完全相同
    persona_revision: int   # persona 文本变化时递增（runtime_settings 键 "persona:<scope_key>"）
    version: str            # store 使用的 scope 版本，只由 role_id 决定（runtime_settings 键 "scope:<scope_key>"，值为 json {"role_id": ...}）


class ScopeBindings:
    def __init__(self, store: SocialStore, roles: Path, channel: str) -> None: ...
    def configure(self, config: TelegramSocialConfig) -> None: ...           # 只保存 config
    def binding(self, scope: ConversationScope) -> TelegramSocialScopeConfig | None: ...   # 从 runtime.binding 原样搬来
    def role_file(self, binding: TelegramSocialScopeConfig) -> Path: ...     # (roles / f"{role_id}.md").resolve()
    def resolve(self, scope: ConversationScope) -> ResolvedScope: ...
```

- `resolve`：
  - binding 为 None → `raise PermissionError("group/topic is not authorized for social mode")`（沿用原文案）。
  - 角色文件在 roles 目录之外 → `raise PermissionError("role file is outside the authorized directory")`（沿用原文案；**不再**在这里调用 `store.bind`）。
  - 读取角色文件时按 `(path, st_mtime_ns, st_size)` 做内存缓存：stat 结果不变就复用上次读到的文本，不重新读文件。
    超过 32000 字节、为空、读取失败时，使用与现在完全相同的占位文本（`Role file unavailable: ...`），这些情况不抛异常。
  - `persona_revision` 和 `version` 都通过 `store.setting_revision` 计算。为了避免每次调用都查库，在内存里缓存“上次传入的值 → revision”：
    值相同就直接返回缓存的 revision。只有值变化时才调用 `setting_revision`。
  - `version` 的值 = `str(setting_revision(f"scope:{key}", json.dumps({"role_id": binding.role_id})))`。
    **不再**包含 protocol 版本、binding 的其他字段或 config。
- 把 `runtime.py` 中的 `SOCIAL_PROTOCOL` 字符串和 `SOCIAL_PROTOCOL_VERSION` 原样移到 `bindings.py`（`runtime.py` 中仍从这里 import 并保持对外可用，
  `SOCIAL_TOOLS` 留在 runtime.py）。`SOCIAL_PROTOCOL_VERSION` 改为不再参与任何计算；如果移动后没有使用者，就删除它。

### 2. `runtime.py` 的改动

- 构造函数：`self.bindings = ScopeBindings(self.store, self.roles, channel)`；删除 `self._roles_root`（移进 ScopeBindings）。
  `self.binding(scope)` 保留为一行转发：`return self.bindings.binding(scope)`（调用方太多，不在本任务里改）。
- 新增唯一一个**有副作用**的同步方法，取代 `_persona` 的副作用：
  ```python
  def sync_scope(self, scope: ConversationScope) -> None:
      binding = self.binding(scope)
      if binding is None:
          self.store.bind(scope, "disabled"); self.proactive.sync(scope, None); return
      try:
          resolved = self.bindings.resolve(scope)
      except PermissionError:
          self.store.bind(scope, "unavailable"); return
      self.store.bind(scope, resolved.version)
      if binding.paused:
          self.store.pause(scope.session_key)
      self.proactive.sync(scope, binding)
  ```
- 删除 `_persona`。原调用点按下表处理：

  | 调用点 | 改为 |
  |---|---|
  | `configure` 中两个循环 | 对每个 scope 调用 `self.sync_scope(scope)`（保持原来遍历 `store.scopes()` 和 `config.scopes` 中非 all_topics 绑定的逻辑；`self.bindings.configure(config)` 在循环之前调用） |
  | `run_due` 开头的循环 | 对每个 scope：`self.sync_scope(scope)`；绑定有效时继续调用 `self._wake_conditions(...)`。原来的 try/except 中，`OSError, ValueError` 已经不会由 resolve 抛出（PermissionError 已在 sync_scope 内处理），删除这个 try/except |
  | `observe` | `self._persona(message.scope)` → `self.sync_scope(message.scope)`（新话题首次出现时需要建立 scope 行） |
  | `validate_context` | 删除 `_, version = self._persona(scope)`；`require_claim` 检查使用 `self.store.state(scope.session_key)["version"]` |
  | `reserve_message_send` | `_, version = self._persona(scope)` → `version = self.store.state(scope.session_key)["version"]` |
  | `_deliver` | 删除 `_, current = self._persona(scope)`；比较改为 `version != self.store.state(scope.session_key)["version"]` |
  | `record_message_receipt` | 删除 `self._persona(scope)` 这一行 |
  | `get_state` | `role_file` 使用 `str(self.bindings.role_file(binding))` |
  | `judge_runtime.py::_snapshot` | `persona, _ = self.runtime._persona(scope)` → `persona = self.runtime.bindings.resolve(scope).persona` |

  如果还有其他 `_persona(` 调用点，按同样原则处理（只需要 version 就读 `store.state`，需要 persona 文本就用 `bindings.resolve`），并在报告里列出。

- `validate_context` 改完后应当**不写库**：它只读 `ConversationScope`、`self.binding`、`store.state` 和 `store.active_turn`。

### 3. `policy_for_session` 重写为：

```python
def policy_for_session(self, session: Session) -> SessionPolicy | None:
    scope = ConversationScope.from_session_key(session.key)
    if scope is None or scope.channel != self.channel:
        return None
    binding = self.binding(scope)
    if binding is None and not self.store.owns(session.key):
        return None
    mode = "ordinary"
    policy = SessionPolicy()
    if binding is not None:
        mode = "social"
        try:
            resolved = self.bindings.resolve(scope)
        except PermissionError:
            policy = SessionPolicy(tool_only=True)
        else:
            policy = SessionPolicy(persona=resolved.persona, tool_only=True, context_version=f"social:{scope.session_key}")
            if session.metadata.get("_persona_revision") != resolved.persona_revision:
                # 新 persona 在下一个模型请求生效；provider 侧缓存可能带着旧 system prompt，所以只清 provider_state，不截断历史。
                session.provider_state = None
                session.metadata["_persona_revision"] = resolved.persona_revision
    previous = session.metadata.get("_social_mode")
    if previous is None and "_context_version" in session.metadata:
        # 一次性采用：已有会话视为与当前模式相同，不因本次升级截断历史。
        previous = mode
    session.metadata.pop("_context_version", None)
    if previous != mode:
        <原来的截断代码块，原样保留：last_archived / provider_state / 删除 _last_summary、runtime_checkpoint、pending_user_turn、goal>
        session.metadata["_social_mode"] = mode
    <原来的 _social_runtime_epoch 代码块，原样保留>
    return policy
```

- 也就是说：**只有在 social ↔ ordinary 模式切换时才截断历史**；改配置、改角色文件、改 role_id 都不截断。
- `_social_runtime_epoch` 的处理保持不变（本任务不改）。

### 4. 其他

- `store.bind` 的语义不变（version 变化时取消在途工作）。现在 version 只在 role_id 变化、或进入 disabled/unavailable 时变化，这正是预期行为。
- **部署后的一次性影响**：线上 scope 的 version 会从旧的“整份配置 revision”变成新的“role_id revision”字符串，`store.bind` 会因此取消一次在途 wake
  并清空当前的 dialogue 等待。这只发生一次，可以接受；不要为此加兼容逻辑。

## 要做的事

1. 按设计实现 `bindings.py` 和 runtime/judge_runtime 的改动。
2. `nanobot/channels/telegram/tests/test_social_scenarios.py`：去掉 `test_role_edit_keeps_earlier_group_history_in_context` 的 xfail 标记，它必须通过。
   新增场景 `test_unrelated_config_change_keeps_waiting_and_history`：模型 `social_wait(purpose="messages", seconds=30)` 进入等待后，修改一个无关配置
   （例如 judge 的 `timeoutSeconds`，通过 harness 新增的 `reconfigure(**overrides)` 方法调用 `runtime.configure`），等待中的会话仍然在：
   随后的群消息唤醒主模型，且主模型能看到修改配置之前的历史消息。
3. 新增单元测试 `nanobot/channels/telegram/tests/test_social_bindings.py`，覆盖：
   - 角色文件 stat 不变时不重新读取（可以用 monkeypatch 统计 `Path.read_text` 的调用次数）；修改文件后读到新内容；
   - 只改 binding 的 background 或 config 的其他字段时，`version` 不变、`persona_revision` 变化（background 是 persona 的一部分）；改 role_id 时 `version` 变化；
   - 角色文件在 roles 目录外时 `resolve` 抛出 PermissionError；
   - `validate_context` 不写库：调用前后 `store.db.total_changes` 不变（validate 前先完成一次 sync）。
4. 已有测试中，断言“改配置或改角色会截断历史 / 取消等待 / 改变 version”的，改为新行为；直接调用 `_persona` 的，改为 `bindings.resolve` 或 `sync_scope`。
   逐条列出每个修改或删除的测试：文件、测试名、旧断言 → 新断言或处理方式、原因。

## 允许改动的路径

- 新建：`nanobot/channels/telegram/social/bindings.py`、`nanobot/channels/telegram/tests/test_social_bindings.py`
- 修改：`nanobot/channels/telegram/social/runtime.py`、`judge_runtime.py`；`nanobot/channels/telegram/tests/` 下的测试与 harness；
  `tests/agent/test_telegram_social_contracts.py`（仅当它引用了 `_persona` 或旧的 context_version 行为时）

## 防御性代码规则

- 除本简报明确要求外，不得新增：校验或断言、try/except、回退分支、版本或 revision 检查、迁移步骤、配置项。
- `_context_version` → `_social_mode` 的一次性采用逻辑只允许设计中那两行，不要扩展。
- 不改 `store.bind`、`store.pause`、`proactive.sync` 的内部实现。不改 `SOCIAL_PROTOCOL` 的文字。
- 预期规模：`bindings.py` 约 80–110 行（其中 SOCIAL_PROTOCOL 约 160 行另计）；runtime.py 净减少。

## 完成标准

- `.venv/bin/python -m pytest nanobot/channels/telegram/tests tests/agent/test_telegram_social_contracts.py -q -p no:cacheprovider --ignore-glob="*evaluate_*"`
  全部通过，且**没有 xfail**（场景套件全部通过）。
- 全仓库 `grep -rn "_persona(" nanobot tests --include=*.py` 无输出。
- `.venv/bin/ruff check nanobot/channels/telegram/` 通过；`.venv/bin/basedpyright nanobot/channels/telegram/social/` 为 0 errors。
- 报告给出 `git diff --stat` 和 runtime.py 改动前后的行数。

## 报告格式（最终回复只包含以下内容）

1. 改动摘要（按文件，含行数）
2. 实际运行的命令及结果（原样贴最后几行）
3. 与简报的偏差及原因；测试改动逐条列出
4. 未解决的问题 / 建议（不要自己实现）
