# 任务 push-1：把本地 main 推送到 origin

owner 明确要求由你（Codex）执行这次推送。只做推送，不做任何其他改动。

## 要做的事
1. 在 `/home/ubuntu/nanobot-bridge-tg` 运行 `git status -sb`，确认当前在 `main`、工作区干净（没有未提交改动；如有，停下并报告，不要提交或丢弃任何东西）。
2. `git fetch origin main`，确认 `origin/main` 是本地 `main` 的祖先（`git merge-base --is-ancestor origin/main main`）。若不是（远端有本地没有的提交），**停下并报告**，不要 rebase、merge 或强推。
3. `git push -u origin main`。如因网络失败，按 2s、4s、8s、16s 间隔最多重试 4 次。
4. 推送后运行 `git status -sb` 和 `git log --oneline -1 origin/main`，确认本地与远端一致。

## 不要做
- 不要 `--force` / `--force-with-lease`，不要改写历史，不要创建分支或 PR，不要修改任何文件，不要改 git 配置或凭据。

## 报告格式（最终回复只包含以下内容）
1. 每一步实际运行的命令及输出（原样）
2. 结果：推送是否成功，推送了多少个提交，origin/main 当前指向的提交
3. 遇到的问题（如有）
