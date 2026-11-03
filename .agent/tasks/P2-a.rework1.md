# P2-a 返工 1：修正简报中的两处设计缺陷

你在报告第 4 项指出的两个问题都成立，是简报设计的错误。按以下方式修正，其余部分保持不变。

## 1. 取消暂停后无法恢复

`bindings.py` 中 `version` 的来源从 `json.dumps({"role_id": binding.role_id})` 改为
`json.dumps({"role_id": binding.role_id, "paused": binding.paused}, sort_keys=True)`。

这样暂停和取消暂停都会改变 version，`store.bind` 会像改动前一样重置 scope 状态（`sync_scope` 中 paused 时继续调用 `store.pause`）。
这与审查文档 P0-7 的原意一致：binding 版本只在禁用、暂停、改绑角色时变化。不要新增其他恢复逻辑。

`test_social_bindings.py::test_revisions_only_follow_persona_text_and_role_id` 增加一个断言：切换 `paused` 会改变 version。
`test_social_scheduler.py::test_pause_invalidates_running_tools_timers_and_pending_addresses` 不改断言，必须通过。

## 2. 旧会话采用后第二次解析仍截断历史

`policy_for_session` 中，在 `if previous != mode:` 代码块**之后**，无条件执行 `session.metadata["_social_mode"] = mode`
（并删除代码块内部原来的那一行赋值）。采用分支（`previous is None and "_context_version" in metadata` → `previous = mode`）保持不变。

在 `test_social_bindings.py` 中增加一个测试：只带 `_context_version` 的旧会话连续解析两次，两次都不截断历史，且第一次之后 metadata 中已有 `_social_mode`。

## 完成标准与报告

与原简报相同（全部社交测试通过、零 xfail；`_persona(` 全仓 0 命中；ruff；basedpyright）。
basedpyright 照你的做法使用临时 pyrightconfig 即可，编排者会在正常环境中复跑原命令。
报告只需写：本次改动（按文件）、实际运行的命令及结果、偏差、未解决问题。
