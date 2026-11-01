# Bot 读取自己的源码

宿主机开发目录是 `/home/ubuntu/nanobot-bridge-tg`，现在以只读bind挂载到容器内的同一绝对路径。
这是实时工程，包含工作区当前文件、未提交修改、发布脚本、模板及Git状态；宿主机编辑立即可见。
只挂载工程目录，不挂载整个宿主机home或Docker socket。部署显式传递BRIDGE_CHECKOUT_DIR，sudo下同样生效。

另外，每次发布把同一提交的仓库快照放进运行镜像，容器内路径为 `/opt/nanobot-source`。

- `/opt/nanobot-source/README.md`：项目说明。
- `/opt/nanobot-source/nanobot/`：Python 实现，包括群聊调度和自主活动。
- `/opt/nanobot-source/docs/`、`tests/`、`scripts/`、`deploy/`：实现记录、测试与发布代码。
- `/opt/nanobot-source/SOURCE_REVISION`：该运行版本的 Git 提交号。

上下文的 Application Source 与 Live Development Checkout 段会直接提供这两个入口，普通和群聊模式共用。
询问宿主机工程路径时读实时挂载；核对实际运行版本时读快照，不能混淆二者。
bot可用现有read_file/list_dir/grep等工具按问题阅读，不需要群友逐个贴文件。
镜像快照由现有git archive构建上下文产生，自动随版本升级；快照不复制宿主机未提交文件、私有配置、Git元数据或虚拟环境。
快照由root拥有，当前非root运行用户只读。实际执行仍从wheel安装目录导入，不从快照运行。
本次不修改普通/social工具权限、工作区范围、模型配置或持久记忆。

发布冒烟检查README、版本标记、源码快照与安装包的关键Python文件逐字节一致，
并检查运行用户不可写源码目录。原来的健康、认证、停机备份和失败回滚流程继续使用。
