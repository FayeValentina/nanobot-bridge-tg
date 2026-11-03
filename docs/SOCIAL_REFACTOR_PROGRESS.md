# Social 重构实施进度

计划见 [SOCIAL_ARCHITECTURE_REVIEW.md](SOCIAL_ARCHITECTURE_REVIEW.md)，实施流程见 [`.agent/orchestration.md`](../.agent/orchestration.md)。
每个任务验收并部署后追加一行。“验收证据”只写实际运行的命令和结果。

| 任务 | 阶段 | 提交 | 部署 | 验收证据 | 偏差 / 遗留 |
|---|---|---|---|---|---|
| P0-a 后台调用失败可诊断 | 0 | `8a0e9338` | 2026-11-03 healthy，实例 schema 18 | 社交测试目录 916 passed；ruff 干净；basedpyright social/ 0 errors；线上库副本迁移 17→18，各表行数不变；部署后容器 healthy，日志无新异常 | Claude 补修：13 处写死的 schema=17 断言改为引用 `SCHEMA_VERSION`；store.py 两个历史遗留 lint 问题。线上库已无 failed 批次，此前失败的黑话批次原因无法追溯 |
| P0-b 黑盒场景测试套件 | 0 | 见本行提交 | 无需部署（仅测试） | 场景 16 passed + 2 xfailed（strict，对应 §5.2 bug 和 P0-7）；社交测试目录 932 passed 2 xfailed；场景文件无 db/store/私有访问；ruff 干净 | 退回 Codex 写进 conftest 的两个沙箱补丁，改为 `.agent/codex_pytest_env.py`（只在派发 Codex 时加载）。第 8 阶段工具合并时需同步更新场景中的工具名 |
