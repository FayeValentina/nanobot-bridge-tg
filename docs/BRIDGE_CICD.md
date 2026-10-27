# 本机开发、测试、构建与发布

本分支采用：本地源码开发 → GitHub Test Suite → ARM64 镜像和 wheel → 手动触发服务器部署。
运行数据、模型凭据、Telegram Token 均留在服务器，不进入镜像、Git 或 Actions artifact。

## 常用命令

在 `/home/ubuntu/nanobot-bridge-tg` 运行：

```bash
./scripts/pipeline.sh setup        # 首次创建开发环境并安装测试依赖
./scripts/pipeline.sh test         # 发布脚本、Telegram/会话/触发器回归；前端 lint/test/build
git add <本次修改的文件>
git commit -m "描述修改"
./scripts/deploy-local.sh build    # 要求工作区干净；构建镜像/wheel、打包并做无网络冒烟
./scripts/deploy-local.sh status
```

本地 `test` 是迭代时的相关回归；GitHub 上保留原有 Test Suite 的完整 Python、类型检查、WebUI、TUI 和 Docker 检查，发布构建只在其成功后执行。
只修改文档而未触发 Test Suite 时，可在 Actions 手动运行 Test Suite；通过后仍会生成发布包。

发布文件保存在服务器用户目录：

```text
~/.local/share/nanobot-bridge-tg-deploy/
  releases/<完整 Git SHA>/
    release.json       # 源码 SHA、镜像内容 ID、架构和 wheel 文件名
    SHA256SUMS         # 发布内容的 SHA256 校验
    image.tar.gz       # 已构建镜像，不要求服务器登录镜像仓库
    nanobot_ai-*.whl
  shared/data/         # 当前配置、会话、记忆、媒体
  backups/             # 部署前数据快照和历史状态
  state.json           # 当前版本与上一版本
  ssh/                 # GitHub 专用部署密钥，仅本机私有目录
```

## GitHub 工作流

1. 向 `main` 推送/合并：已有 **Test Suite** 自动测试。
2. 测试通过：**Bridge Release** 检出刚刚测试过的精确 SHA，构建 ARM64 镜像，生成 wheel 并启动隔离容器验证。
3. 发布包作为 `bridge-release-linux-arm64` artifact 保存 30 天，构建摘要会显示 `release_run_id`。
4. 打开 **Bridge Deploy → Run workflow**，填入该 ID，部署到服务器。
5. 服务器验证文件校验和、架构和镜像 ID，先冒烟，再停止旧容器、备份数据、启动新容器并验证健康；失败自动恢复旧容器与数据。

PR 的测试结果不能触发发布；部署只接受同仓库、主分支、成功的 Bridge Release 产物。构建运行编号不能填 Test Suite 的编号。
目前只发布本服务器需要的 `linux/arm64` 网关/WebUI，不生成原生 TUI 安装包。镜像中已安装 Telegram 通道依赖。

## 首次服务器准备

需要 Docker Engine、Compose v2 和 Buildx。普通用户能运行 Docker，或具备 `sudo -n docker` 权限。
脚本会优先尝试普通 Docker，必要时使用现有 sudo 权限；不会自动把用户加入 docker 组。

初始化全新实例：

```bash
./scripts/deploy-local.sh init
./scripts/deploy-local.sh deploy <完整 Git SHA>
```

独立验证实例使用独立数据目录、Compose 项目和端口；首次创建时指定，随后会记录到该实例的 `settings.json`：

```bash
export BRIDGE_DEPLOY_ROOT=/home/ubuntu/.local/share/nanobot-bridge-tg-staging
export BRIDGE_WEB_PORT=8766
export BRIDGE_HEALTH_PORT=18791
./scripts/deploy-local.sh init
```

测试实例应保持 Telegram 关闭并使用独立模型配置。运行结束可用同一 `BRIDGE_DEPLOY_ROOT` 执行 `stop`。

从本机已有的 systemd nanobot 迁移：

```bash
./scripts/deploy-local.sh migrate-systemd <完整 Git SHA>
```

迁移会先验证镜像；随后停止并禁用 `nanobot-gateway.service`，复制 `~/.nanobot` 到独立的 `shared/data`，将容器内工作目录改成 `/data/workspace`，再启动容器。
原始 `~/.nanobot` 不会被修改或 chown。默认只支持工作区位于 `~/.nanobot/workspace` 的实例；其他布局需先明确映射。
迁移失败时恢复原服务。第一次迁移后的 `rollback` 可以回到原 systemd 实例。

WebUI 与健康端口仅发布到本机 `127.0.0.1:8765` 和 `127.0.0.1:18790`；继续使用原 SSH 隧道。
容器内服务绑定 `0.0.0.0`，通过宿主机端口绑定限制外部访问。角色/记忆等以后新增的数据也应位于 `/data` 下。
同一个 Telegram Token 不能同时由旧服务、开发进程和容器轮询。

## 一次性配置 GitHub 部署权限

在服务器运行：

```bash
./scripts/setup-deploy-access.sh
```

它生成专用 Ed25519 密钥，并在当前用户 `authorized_keys` 中新增受限入口，只允许执行 `bridge_receive.py`。该身份不能打开 shell 或做端口转发。
不会修改现有 SSH 公钥，不会把部署私钥打印到日志。服务器上的接收脚本路径应保持稳定；更新部署基础设施时需同步更新服务器 checkout。

在 GitHub 仓库 **Settings → Environments → production** 创建环境，并添加以下 secrets：

| 名称 | 值 |
| --- | --- |
| `DEPLOY_HOST` | 此服务器的公网 IP 或可访问的域名，例如 `168.138.55.150` |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_SSH_KEY` | `~/.local/share/nanobot-bridge-tg-deploy/ssh/github-actions` 的完整内容 |
| `DEPLOY_KNOWN_HOSTS` | 固定服务器 SSH 主机公钥的 known_hosts 行 |

known_hosts 行应从服务器本地 `/etc/ssh/ssh_host_ed25519_key.pub` 取得可信公钥，格式为：

```text
168.138.55.150 ssh-ed25519 <服务器公钥的 base64 字段>
```

私钥只填 GitHub secret，不要发到聊天或提交到仓库。可按需要为 production 环境设置人工审核。
GitHub 的 Git SSH 写权限与 Secrets API 权限不同；仅能 git push 的身份不能替你写入上述 secrets。
SSH 当前使用默认端口 22；如修改端口，需要一并修改 workflow 与 known_hosts。

## 回滚与排障

```bash
./scripts/deploy-local.sh status
./scripts/deploy-local.sh logs     # 对配置中的已知凭据和 Telegram Token 做遮蔽
./scripts/deploy-local.sh rollback
./scripts/deploy-local.sh stop
```

回滚恢复上一版本及其部署前数据快照，因此会回退升级后的数据写入。失败版本的数据会保存在 `failed-data-*` 目录中，不会直接删除。
发布锁可防止本机手动部署与 GitHub 部署同时修改实例。镜像标签与源码 SHA 对应，并校验镜像内容 ID；同一个 SHA 不允许用不同内容覆盖。
历史镜像、发布包和备份不自动删除；确认无需回滚后再由管理员清理，避免自动清理删除唯一可用备份。
校验和用于发现传输损坏；产物来源的信任来自 GitHub 工作流校验、SSH 固定主机公钥和专用部署密钥。

## 验证范围

- 单元测试模拟损坏产物、恶意归档、部署失败、数据恢复和显式回滚，不访问真实 Telegram/LLM。
- 镜像冒烟使用新生成的测试配置、`--network none` 和临时数据，验证 wheel 导入、Telegram 依赖、WebUI、未认证拒绝与认证成功。
- 上线健康检查不发送群消息，也不调用付费模型；实际 Telegram 和模型端到端体验由管理员发送测试消息确认。
- Docker 使用非 root 用户，不挂载 Docker socket，不递归修改宿主数据所有者。自定义 UID/GID 取运行部署脚本的用户。

这套管线只搭建开发发布基础设施；`MIGRATION_PLAN.md` 中的仿真功能尚未开始实现。
