# M2 本机独立验证实例部署记录

日期：2026-10-28。按用户选择先完成部署，测试 Bot、模型和真实测试群稍后配置。

## 部署结果

| 项目 | 当前值 |
| --- | --- |
| 已部署源码 | `798d172d54f6ca0ca6386f965d8462605e861e57` |
| 镜像 | `nanobot-bridge-tg:sha-798d172d54f6ca0ca6386f965d8462605e861e57` |
| 镜像内容 ID | `sha256:5985dc3697f12682ceb85d36ebfddbc717f50ae78a8f95556d09cc7f87ed22e4` |
| 架构 / wheel | arm64 / `nanobot_ai-0.3.5-py3-none-any.whl` |
| 验证实例根目录 | `/home/ubuntu/.local/share/nanobot-bridge-tg-staging/` |
| Compose 项目 | `nanobot-bridge-tg-e156cbab` |
| 网页 | `http://127.0.0.1:8766` |
| 健康检查 | `http://127.0.0.1:18791/health`，HTTP 200，ready=true |
| 容器状态 | healthy |
| social 数据库 | 挂载数据目录中的 schema 2，授权 scope 数为 0 |
| Telegram / 模型 / 仿真 | 未配置测试凭据，Telegram 与 social 均未开启 |

源码提交后通过以下命令执行完整管线，使用该提交的 Git 快照构建与部署：

```bash
BRIDGE_DEPLOY_ROOT=/home/ubuntu/.local/share/nanobot-bridge-tg-staging ./scripts/pipeline.sh release
```

源码版本与这份部署记录的文档提交不同；实例版本以 `state.json` 与镜像 revision 为准。
网页登录沿用验证实例原有凭据。上面的地址只绑定服务器本机，远程访问需转发对应端口。

## 验证范围

- 发布管线通过：后端 581 项、WebUI 96 个文件共 1553 项、Ruff、WebUI lint/build。
- Docker 镜像隔离冒烟通过：安装 wheel 导入、Telegram 依赖、网页、未登录拒绝访问、
  授权 bootstrap 和健康检查；构建与实际部署均完成检查。
- 在独立的无网络、只读根文件系统容器中验证安装包：消息合并、连续已读水位、
  引用发送回执、同回合发送去重、等待和无新消息时不重复超时唤醒，全部通过。
- 同一安装包在临时库验证 schema 1→2，保留水位、消息与发送回执，升级前备份仍可读取。
- 实际 gateway 在挂载的 `/data/telegram-social/` 创建 schema 2；正式实例无 social 库被升级。
- 正式实例仍为 `fbfe7a3cbb014c3a7222d6e1959d65ca64384c30`，端口 8765 / 18790；
  健康正常，部署状态与配置文件的前后校验值一致。

未进行真实模型请求、Bot 轮询或真实群发送。日志中的模型未配置提示与当前验证实例配置一致，
不影响 WebUI 和配置入口就绪。群友自然程度和真实 Telegram 下的参与行为仍待专用测试群验收。

## 备份、证据与回滚

升级前验证实例的数据和状态保存在：

```text
/home/ubuntu/.local/share/nanobot-bridge-tg-staging/backups/1793163037725563119/
```

管线日志、安装包验证脚本与结果、镜像/备份/健康核对记录保存在 Git 外部：

```text
/home/ubuntu/.local/share/nanobot-bridge-tg-staging/verification/798d172d54f6ca0ca6386f965d8462605e861e57/
  release.log
  package-probe.py
  package-probe-result.json
  deployment-verification.json
```

查看状态和脱敏日志：

```bash
./scripts/deploy-local.sh --root /home/ubuntu/.local/share/nanobot-bridge-tg-staging status
./scripts/deploy-local.sh --root /home/ubuntu/.local/share/nanobot-bridge-tg-staging logs
```

如后续需要回滚，现有管线会同时恢复前一镜像与升级前数据，并保留被替换的新数据。
本次未执行回滚，当前继续运行 M2。

```bash
./scripts/deploy-local.sh --root /home/ubuntu/.local/share/nanobot-bridge-tg-staging rollback
```

## 后续配置

在验证实例 WebUI 配置模型和专用测试 Bot，然后按 [M2 实现说明](M2_IMPLEMENTATION.md)
配置精确群/话题授权、角色文件及预算。验证实例的角色宿主路径为
`/home/ubuntu/.local/share/nanobot-bridge-tg-staging/shared/data/telegram-social/roles/`，
容器内对应 `/data/telegram-social/roles/`。

专用测试 Bot 不能与正式实例共用 Token。完成真实群文字闭环验收后，再决定正式实例更新。
