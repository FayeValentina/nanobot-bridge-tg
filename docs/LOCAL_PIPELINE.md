# 本机修改、测试、构建、发布与运行

这台服务器同时承担开发构建和运行。GitHub 只保存代码版本；不需要 GitHub Actions、Secrets、部署密钥或镜像仓库。
GitHub Actions 工作流和社区 Issue 模板已移除，所有测试、构建和部署均在本机执行。
项目仅保留 `deploy/` 下的 Docker 部署方案；上游根目录 Docker、Render 模板和公开发布流程文档已移除。

## 本人使用的本机迭代约定

2026-10-28 用户明确：本迁移工程和本机实际实例仅供本人使用，不是商业生产环境。
优先通过及时部署获得实际反馈；每个可运行的功能修改或修复批次完成必要检查并提交后，
默认直接发布到 `~/.local/share/nanobot-bridge-tg-deploy/`（网页 8765、健康 18790），在本次任务结束前完成部署与健康确认。
这是对常规本机发布及必要重启的持续授权，无需每次再次询问。

不要求专用测试群、专用测试 Bot、独立验证实例先行或长时间观察；现有验证实例按需使用即可。
直接使用本人现有配置和实际使用反馈，同一 Bot Token 仍只运行一个更新接收者。
复用现有自动备份、健康检查和失败回滚，按变更影响安排检查，不额外引入商业发布审批或无实际必要的防御流程。
仅修改文档、记录偏好时无需重建或部署运行实例。

本约定优先于下方历史部署记录中先配置测试群、先部署独立实例等旧安排。
用户后续另行明确要求 [群友与普通助手共用工具权限](SHARED_TOOL_PERMISSIONS.md)，
已据此撤回早期的群友专用权限/上下文隔离；全局工具开关和普通路径、沙箱规则保留。

## 当前生产状态

2026-10-30 当前实际实例为去哈希版本 `6ae4ada01853cc04444dc5fa60abcb7e66230368`，schema 11，`social-judge-v4`。
迁移新增的消息/输入摘要、配置/人格签名、记忆/媒体哈希和自建发布校验和均已移除，
改用原生消息/批次 ID、显式修订号和原值直接比较；模型只接收对话事实。
21 个合成语境真实调用全通过，同组样例输入 Token 比 V3-C 再减少 6.4%；安装 wheel 内 4 例通过。
完整发布通过后端 1080 项、provider/OAuth 155 项、前端 1553 项及 lint/build；严格类型零错误/警告。
实际库升级、完整性、外键和原值比较通过；配置、角色、消息、水位、记忆与证据保留。
容器 healthy、Telegram 已连接。上一镜像 `e9669e5d`，备份 `backups/1793351059291313428/`。
详见 [哈希清理记录](NO_HASH_IMPLEMENTATION.md)。2026-10-30 最新决定：下一步优先 [V4 主动交流还原](MIGRATION_V4_PLAN.md)，不先实施 V2-C；V4 当前仅计划，实际运行版本及配置未因计划更新而改变，M5 暂缓。

此前 V3-C 实际实例为 `e9669e5da367fb7ad1a1accc4dce38b34011181c`，schema 10，`social-judge-v3`。
修复普通观察被伪造成选择性等待、区分参与偏好与显式意图；直接纠正/停止发言等交主模型执行。
消息哈希保留本地校验、不再送模型；相同 25 批历史的本地估算输入减少 17.9%，实际成本继续记录。
后端 1074 项、provider/OAuth 155 项、前端 96 文件/1553 项及 lint/build 通过；严格类型零错误/警告。
21 个真实模型合成语境及安装 wheel 内 4 例通过，没有向真实群发送测试消息。
容器 healthy、Telegram 已连接；schema、102 条旧消息、3 scope 水位、记忆和配置/角色指纹保留。
上一镜像 `c9af722b`，整实例备份 `backups/1793347690084610508/`。详见 [V3-C 记录](V3C_IMPLEMENTATION.md)。
下一批 V2-C，再按 V3 适配 V2-D；V2-E/F 后续，M5 仍暂缓。

以下为历史发布记录。

2026-10-29 当时后续开发顺序：V2-A/B 与 V3-A/B 已完成，下一批优先实施 [V3-C](MIGRATION_V3_PLAN.md)。
实际消息入口已由中间模型统一处理，旧本地语义触发退役。随后重新核对 [V2-C/D](MIGRATION_V2_PLAN.md)，
再补学习与表达；M5 专用管理界面继续暂缓。仅更新计划不构建、不重启实例。

此前 V3-B 实际实例为 `c9af722bdfa54915f74dac234195446e3e250501`，schema 10。
独立持久 OAuth / `gpt-6-luna`（low）已接管新消息批次，主模型保持普通工具权限和沉默/等待能力。
后端 1063 项、provider/OAuth 155 项、前端 96 文件/1553 项及 lint/build 通过；最后边界修复另通过 76 项，严格类型零错误/警告。
9 个真实模型合成用例通过，安装镜像中独立 OAuth 再验证三例通过，未向真实群发送探测消息。
schema 9→10、原消息/水位、配置/角色/记忆保留核对通过，容器 healthy、Telegram 已连接。
上一镜像 `c388ddb9`，整实例备份 `backups/1793262054479148890/`；降级需恢复匹配数据。
详见 [V3-B 记录](V3B_IMPLEMENTATION.md)。

此前 V3-A 实例为 `c388ddb954eb9fda57d4faf6dc33433c903cc8d6`，schema 9。
中间组件采用 Codex OAuth / `gpt-6-luna`（low），77 项新增回归、8 个合成样例真实模型调用通过；
群消息仍走 V2-B。常驻容器 OAuth 和统一消息调度在 V3-B 接入，不因本批发布自动调用中间模型。
完整 release 通过后端 1113 项、provider 116 项、前端 96 文件 / 1553 项、Ruff 和 WebUI lint/build；严格类型零错误/警告。
实际 wheel 临时探针、健康、Telegram 连接、schema 8→9 与 quick_check 核验通过；原消息、水位、scope 版本、配置和角色/工作区指纹保留。
三张 judge 表保持空表，未写入启用预设或持久凭据。上一实际版本 `29579798`，整实例备份 `backups/1793246143370790766/`。
旧代码不支持 schema 9，回滚需连同原库/配置备份恢复，见 [V3-A 实现与发布](V3A_IMPLEMENTATION.md)。

此前 V2-B 实际版本为 `295797983bd5368078188d6716814949b48804d5`。
提问触发、5% 普通消息概率，以及关键词/指定成员/提问潜水中断已启用；概率和普通消息不打断潜水。
支持有限/无限及带明确时区的绝对等待，临时条件仍有 TTL。V2-A、角色、群范围、模型、额度、主动机会和 quietHours 配置保留。
完整原管线通过后端 1036 项、provider 116 项、前端 1553 项、Ruff 与 WebUI lint/build；新增 V2-B 定向验收 102 项，修改模块严格类型零错误/警告。
实际安装 wheel 离线链路、容器健康、Telegram 连接、schema 7→8 与 quick_check、原水位/记忆和角色/工作区指纹已核验。
除三项 scope 增量外配置指纹一致，2 个实际作用域的调度版本已对应启用后的策略；未向真实群发送验收消息。
上一实际版本 `93fcb83f`，发布前整实例备份 `backups/1793239186020715409/`；启用预设前配置备份 `backups/v2b-preset-20261029T020128/`。
旧代码不支持 schema 8 和新增字段，回滚应连同对应数据。详见 [V2-B 实现与发布](V2B_IMPLEMENTATION.md)。

此前 V2-A 版本为 `93fcb83f5cf4fc7027cb82435ca7ed67fc9fa29b`。
动态等待（1.5/4 秒静默、12 秒总上限）、答后关注（30 秒本地检查、120/300 秒窗口）、睡前观察（300 秒）已启用。
原管线通过后端 934 项、provider 116 项、前端 1553 项与 Ruff、WebUI lint/build；新增 56 项 V2-A 定向验收通过，修改模块严格类型零错误/警告。
实际镜像离线探针、容器健康、Telegram 连接、schema 6→7 与 quick_check、原水位/记忆 ID 和角色/工作区指纹均核验通过。
除三项增量参数外配置指纹一致；2 个实际作用域的调度版本已对应启用后的配置。未向真实群发送测试消息。
上一版本 `e714727f`，发布前整实例备份 `backups/1793235968038318132/`；新参数启用前另备份 `backups/v2a-preset-20261029T011240/`。
旧代码不支持 schema 7 和新增配置字段，回滚应由原管线连同对应数据恢复。详见 [V2-A 实现与发布](V2A_IMPLEMENTATION.md)。

此前群回复可靠性版本为 `e714727fc63657db5b7a9b291a401a083c2c3bdf`。
修复漏掉显式发送、等待期间关键词过滤、临时条件消费/过期、已读收尾和普通 message 平台回执/中断恢复。
本机 social 设置 maxRequestsPerHour=120、maxSendsPerHour=60、repairUnsentFinal=true；
整群 allTopics、肥鱼昵称、主动机会开启及 quietHours 关闭保留，其他配置与角色/共享资料指纹不变。
完整 release 通过后端 878 项、provider 116 项、前端 96 文件共 1553 项、Ruff 与 WebUI lint/build。
修改模块严格类型 0 errors / 0 warnings。一次未改动的 xAI 登录前端断言失败后，单文件 8 项复测及
最终单 worker 全量均通过；未修改该前端用例，最终 release 使用 VITEST_MAX_FORKS/VITEST_MIN_FORKS/
VITEST_MAX_THREADS/VITEST_MIN_THREADS=1，避免本机并行争用。
实际容器 healthy、Telegram 已连接、近期无 ERROR；schema 5→6 与 quick_check 正常，原消息/记忆/水位保留。
旧临时唤醒条件已随运行协议/配置版本刷新清空，历史已读不强行追溯，后续成功回合仅确认实际展示的连续前缀。
安装镜像内临时数据验证漏发确认、条件过滤/消费、自动已读和普通发送回执/超时不重试，无真实模型调用或群发送。
上一版本为 `ea734a37`，整实例备份 `backups/1793201575323973806/`；本次额度配置另备份于
`backups/reply-quotas-20261029T003322/`。旧代码不支持 schema 6，回滚需连同发布前数据恢复。
详见 [群回复可靠性修复](REPLY_RELIABILITY.md)。M5 尚未开始。

此前整群仿真发布版本为 `ea734a37be745b8f09a0a5172e84265e34df2789`。
当前群配置 `allTopics: true`，覆盖 General、现有与未来话题；各话题的历史、水位、等待和发送路由保留。
主动机会开启（1800 秒），quietHours 关闭；昵称为“肥鱼/肥魚”。已纠正本机角色、SOUL 和共享记忆的错误身份。
原管线 release 通过后端 826 项、provider 116 项、前端 1553 项、lint/build 和镜像冒烟；修改模块严格类型 0 errors / 0 warnings。
容器 healthy、Telegram 已连接；实际库 quick_check 正常，既有消息与水位保留。
安装镜像中用临时数据验证 General、原话题与未见新话题的观察/唤醒/按原话题发送，没有发送真实测试消息。
整实例发布前备份为 `backups/1793198233984339945/`（上一版本 `c080f626`），
本次配置/身份资料另存 `backups/group-social-20261028T233744/`；旧代码回滚需同时恢复发布前数据，避免不支持的 allTopics 字段。
详见 [按群启用说明](GROUP_SOCIAL.md)。M5 尚未开始。

此前 M4b 发布版本为 `c080f626d64c82057387707cbd4c9966feb89b53`。
M4a/M4b 均已完成。新增主动候选、安静时段、竞争合并与恢复，原工具权限和触发配置保留；
当前群的主动机会/quiet 仍关闭，可在现有 social JSON 设置按 scope 启用。
既有完整管线通过后端 818 项、provider 116 项、前端 1553 项与 lint/build，严格类型为零错误/警告。
schema 4→5、quick_check、原水位与记忆 ID 保留核对通过，配置与角色指纹不变；容器健康、Telegram 已连接。
安装镜像内用临时数据完成主动机会/quiet/恢复和共用权限验证，没有向真实群发测试消息。
上一运行版本 `b121b14c`，数据备份 `backups/1793195590288086496/`，降级需要同时恢复备份数据。
下一阶段 M5 尚未开始；详见 [M4b 实现与发布](M4B_IMPLEMENTATION.md)。

此前 M4a 发布记录：`b121b14c1ebe962941bebe85d426364dc98bae8f`，
新增有来源的群记忆、确认/纠错/删除/过期和有界相关性注入，延续普通工具权限。
完整既有 `pipeline.sh test` 通过后端 767 项、provider 116 项、前端 1553 项与 lint/build，
修改模块严格类型 0 errors / 0 warnings；随后复用原 build/deploy 发布。
实际库 schema 3→4、完整性和原水位保留，配置/角色与发布前指纹一致，容器 healthy、Telegram 已连接。
安装镜像内用临时数据验证记忆生命周期与共用权限；未向真实群发送测试消息。
上一版本 `bbb8853c` 与数据备份 `backups/1793186433861704817/` 可供原管线回滚。
M4b 主动机会/安静时段和 M5 尚未开始，详见 [M4a 说明](M4A_IMPLEMENTATION.md)。

此前共用权限发布记录：`bbb8853c992c078d96b8c5d462ed333460aab63c`，
包含 M0–M3 首批与本人要求的共用工具权限调整。后端 738 项、provider 适配 116 项、
前端全部 1553 项（单 worker）及 lint/build 通过；沿用原构建、备份、部署和健康检查，
容器 healthy、Telegram 已连接。实际镜像内已核对共用权限、旧隔离标记迁移及路由版本。
social 已按本人要求启用，既有配置、群/话题授权和角色与发布前备份完全一致。
实际 social 库已从 schema 2 升级至 3，完整性、水位及运行人格/工具配置检查通过。
上一版本 `e3505c3b` 和数据备份 `backups/1793181451691815164/` 保留，可由原管线回滚。
状态工具保留 role_id、运行环境内 role_file，返回共用工具权限标识，不再固定标记角色不可编辑。
本批新增 11 项共用工具验收，群友可用普通文件/命令修改角色、正常 message 发送文本/附件、
跨会话投递和持续目标续轮；观察、显式发送和潜水继续保留。
范围、限额、媒体降级和验收结果见 [M3 实现说明](M3_IMPLEMENTATION.md)。

以下为此前发布记录：

2026-10-28 当前本人实际使用实例已更新为 `3e2f50ba71d748d63a6e19ce0366e7a4dff9832d`，
包含 M0–M2 源码与 Telegram 群流式发送延迟修复。容器 healthy，Telegram 已连接，
现有模型、Bot 和群配置保持不变，social 仍按原配置关闭。
原 `fbfe7a3c` 镜像和切换前数据已备份，可由现有管线回滚。
原因、验证与备份位置见 [Telegram 流式延迟记录](TELEGRAM_STREAM_LATENCY.md)。

2026-10-28 已完成正式迁移，生产运行本地编译的 Docker 镜像，首次版本为 `fbfe7a3cbb014c3a7222d6e1959d65ca64384c30`。
Telegram、模型和网页登录凭据沿用原配置；网页仍使用宿主 `127.0.0.1:8765`，健康接口为 `127.0.0.1:18790`。
旧的 uv `nanobot-ai` 安装和 `nanobot-gateway.service` 单元已移除。原始 `~/.nanobot` 和迁移备份保留。
备份中的 `legacy-runtime` 可独立运行，首次回滚的服务单元已改为使用这个归档环境，不依赖被卸载的路径。

修改源码后需要提交并运行下面的 `release`，通过检查后才更新生产；保存文件不会自动热更新。

## 日常使用

在 `/home/ubuntu/nanobot-bridge-tg` 下执行：

```bash
./scripts/pipeline.sh setup       # 首次创建 .venv 并安装测试/前端依赖；开发环境独立于当前运行环境
# 修改代码
git add <本次修改的文件>
git commit -m "描述本次修改"
./scripts/pipeline.sh release     # 检查 → 测试 → 编译 WebUI/wheel → 构建镜像 → 备份 → 部署 → 健康检查
git push origin main             # 可自行决定何时同步到 GitHub；不是发布前置条件
```

`release` 要求工作区干净，记录开始时的 Git SHA；测试失败或测试期间 HEAD 改变即中止，不切换运行实例。
Docker 构建使用该提交的 `git archive` 快照，避免把未提交文件、私有配置或构建期间编辑器的新改动混进产物。
Python 部分打包为 wheel，WebUI 由前端构建阶段编译并打入 wheel，再安装到运行镜像。
本流程面向网关/WebUI，不生成原生 TUI 发布安装包。

`scripts/` 仅保留本机管线入口 `pipeline.sh`、部署入口 `deploy-local.sh`、发布实现 `bridge_release.py`，
以及开发环境和 Docker 构建必需的 `install_channel_dependencies.py`。
上游的通用安装脚本、TUI 平台 wheel 打包器和贡献者列表更新脚本已移除。

可以拆开执行：

```bash
./scripts/pipeline.sh test
./scripts/deploy-local.sh build
./scripts/deploy-local.sh smoke <完整 Git SHA>
./scripts/deploy-local.sh deploy <完整 Git SHA>
./scripts/deploy-local.sh status
./scripts/deploy-local.sh logs
./scripts/deploy-local.sh rollback
./scripts/deploy-local.sh stop
```

本地 `test` 覆盖发布脚本、Telegram、上下文、会话策略和触发器回归，以及完整 WebUI lint/test/build。
修改其他模块时，增加相应模块的测试；需要全量检查时可在开发环境安装相应通道依赖后运行 `pytest` 和 `basedpyright`。
遵守项目约定，不运行 `ruff format`。

## 本机实例与数据

默认正式实例目录为 `~/.local/share/nanobot-bridge-tg-deploy/`：

```text
releases/<完整 Git SHA>/
  release.json           # 源码 SHA、镜像内容 ID、架构、wheel 文件名
  image.tar.gz           # 已构建的镜像；回滚不依赖外部仓库
  nanobot_ai-*.whl
shared/data/             # 配置、会话、记忆、媒体
backups/                 # 切换前的数据快照和历史状态
state.json               # 当前版本、上一版本
settings.json            # 此实例的 Compose 项目名与宿主端口
compose.env              # 非机密的本地 Compose 参数；兼容 sudo 的环境清理
deploy.lock              # 防止同时执行两个发布/回滚
```

数据目录、备份、真实 Token 和模型凭据均在 Git 外部，不进入镜像。容器运行用户为非 root，只挂载本实例的 `/data`，不挂载 Docker socket。
镜像名称使用完整 Git 提交号，核对发布文件清单、提交标签、架构、安装包和健康状态，不再生成自建校验和或比较镜像内容指纹。相同提交的现有产物会复用并重新冒烟，不用 `latest` 覆盖历史版本。
新实例的端口只发布到宿主 `127.0.0.1`。容器内服务绑定 `0.0.0.0`，方便 Docker 转发，但不会因此在宿主公开端口。

### 独立验证实例

该实例保留为可选排障工具，不是后续本机发布的前置步骤。以下为首次 M2 部署的历史记录。

2026-10-28 已将 M0–M2 的提交 `798d172d54f6ca0ca6386f965d8462605e861e57` 部署到该实例，
容器 healthy，网页、鉴权和健康检查通过；Telegram、模型和真实测试群按用户选择稍后配置。
完整镜像、备份与验收记录见 [M2 本机部署记录](M2_DEPLOYMENT.md)。该次部署未切换实际实例；当前实际版本以本页上方记录为准。

本次验证使用 `~/.local/share/nanobot-bridge-tg-staging/`，网页 `8766`、健康接口 `18791`。
它没有真实 Telegram 或模型凭据，不与生产实例竞争 Token。

首次初始化独立实例：

```bash
export BRIDGE_DEPLOY_ROOT=/home/ubuntu/.local/share/nanobot-bridge-tg-staging
export BRIDGE_WEB_PORT=8766
export BRIDGE_HEALTH_PORT=18791
./scripts/deploy-local.sh init
```

端口和项目名首次创建后保存在 `settings.json`。后续可在不设置端口变量时操作同一实例：

```bash
BRIDGE_DEPLOY_ROOT=/home/ubuntu/.local/share/nanobot-bridge-tg-staging ./scripts/pipeline.sh release
./scripts/deploy-local.sh --root /home/ubuntu/.local/share/nanobot-bridge-tg-staging status
./scripts/deploy-local.sh --root /home/ubuntu/.local/share/nanobot-bridge-tg-staging rollback
```

### 正式实例首次切换

下面仅适用于仍由 systemd 启动旧 Python 包的实例；当前服务器已完成迁移，无需重复执行。
首次正式切换会造成短暂停机，必须在明确决定切换后执行：

```bash
./scripts/deploy-local.sh migrate-systemd <已构建的完整 Git SHA>
```

该操作先验证镜像，随后停止并禁用 `nanobot-gateway.service`，备份并复制 `~/.nanobot` 到独立数据目录，修正容器工作区路径后启动 Docker 实例。
原始 `~/.nanobot` 不修改、不 chown；迁移失败恢复原服务；第一次迁移后的回滚可回到原 systemd 实例。
当前服务器另外归档了旧运行环境，并将备份服务的解释器路径指向归档，然后卸载旧包、移除旧服务单元；首次回滚仍可恢复服务。
默认要求原工作区位于 `~/.nanobot/workspace`，其他布局需先明确映射。正式实例默认使用 `8765` 和 `18790`，原有 SSH 隧道可以继续使用。
同一 Telegram Token 只能由一个网关接收更新，不能让开发进程与生产实例同时轮询。

如需全新正式实例而非迁移现有配置，可运行 `init` 后再 `deploy`，进入网页自行配置模型；不要与原服务占用相同端口。

## 失败处理与回滚

- 测试、构建或隔离冒烟失败：不停止正在运行的实例。
- 发布时先停止本实例，再备份数据，启动新容器；健康验证失败会恢复旧数据并重新启动旧容器。
- 手动 `rollback` 同时恢复旧版本和它对应的数据快照。升级后的新数据会被回退，因此仍保留 `failed-data-*` 目录供人工检查。
- 如果创建备份失败，恢复当前容器，不继续升级/回滚。
- 锁防止重复发布；失败版本数据与历史镜像不会自动删除。确认不需回滚后再清理旧产物与备份。
- `logs` 会遮蔽配置中的已知凭据与 Telegram Token；不要把原始配置或备份上传到 GitHub。

## 环境与验证范围

需要 Python、uv、Git、Node/npm、Docker Engine、Compose v2、Buildx。用户需能运行 Docker，或已有 `sudo -n docker` 权限。
脚本优先尝试普通 Docker，必要时使用现有 sudo 权限；不会自动把用户加入 Docker 组。

构建镜像已预装 Telegram 通道依赖。未来增加 ffmpeg 等系统依赖时修改 `deploy/Dockerfile`，重新构建即可，不在运行容器中手动修改。

- 单元测试验证产物损坏、备份失败、升级失败、回滚失败、实例隔离、源码快照和 sudo 参数传递。
- 隔离冒烟使用新配置和 `--network none`，验证 wheel 导入、Telegram 依赖、网页、登录认证与健康状态，不调用模型。
- 独立测试实例已验证真实升级、手动回滚，以及通过预检后故意失败的升级能自动恢复旧容器和数据。
- 模型与 Telegram 的最终业务体验由管理员发送测试消息确认，管线不会向真实群自动发送测试消息。

本机开发发布基础设施、M0–M3 首批、完整 M4、V2-A/B 和 V3-A/B 已完成，验收随 `test` 执行；详见 [M0](M0_CONTRACTS.md)、[M1](M1_IMPLEMENTATION.md)、[M2](M2_IMPLEMENTATION.md)、[M3](M3_IMPLEMENTATION.md)、[M4a](M4A_IMPLEMENTATION.md)、[M4b](M4B_IMPLEMENTATION.md)、[V2-A](V2A_IMPLEMENTATION.md)、[V2-B](V2B_IMPLEMENTATION.md) 与 [V3-B](V3B_IMPLEMENTATION.md)。`telegram-social/` 数据位于实例配置目录，包含角色、SQLite、独立 OAuth 与有界媒体缓存，随整实例备份保存，不替换 JSONL 会话。当前实际库 schema 11；旧程序不接受新版本，降级需同时恢复对应数据备份。软件默认关闭仿真；本人实际实例已启用整群仿真、主动机会及 V3 统一判断，quietHours 仍关闭，M5 暂缓。实际部署版本以实例 `state.json` 和上方发布记录为准。
