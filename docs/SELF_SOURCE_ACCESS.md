# Bot 读取自己的源码

宿主机开发目录是 `/home/ubuntu/nanobot-bridge-tg`。Docker 中不挂载这个宿主机路径；
每次发布现在会把同一提交的仓库快照放进运行镜像，容器内路径为 `/opt/nanobot-source`。

- `/opt/nanobot-source/README.md`：项目说明。
- `/opt/nanobot-source/nanobot/`：Python 实现，包括群聊调度和自主活动。
- `/opt/nanobot-source/docs/`、`tests/`、`scripts/`、`deploy/`：实现记录、测试与发布代码。
- `/opt/nanobot-source/SOURCE_REVISION`：该运行版本的 Git 提交号。

上下文的 Application Source 段会直接提供上述入口，普通和群聊模式共用。
bot可用现有read_file/list_dir/grep等工具按问题阅读，不需要群友逐个贴文件。
源码由现有git archive构建上下文产生，自动随版本升级；不复制宿主机未提交文件、私有配置、Git元数据或虚拟环境。
快照由root拥有，当前非root运行用户只读。实际执行仍从wheel安装目录导入，不从快照运行。
本次不修改普通/social工具权限、工作区范围、模型配置或持久记忆。

发布冒烟检查README、版本标记、源码快照与安装包的关键Python文件逐字节一致，
并检查运行用户不可写源码目录。原来的健康、认证、停机备份和失败回滚流程继续使用。
