# 部署脚本

在另一台机器上从零建起实验环境的脚本。完整步骤与检查点见仓库根目录的 `TUTORIAL.md`。

| 脚本 | 在哪里运行 | 作用 |
|---|---|---|
| `create_container.sh` | 主机 | 建容器（项目目录挂载为 `/neuragent`，数据卷挂载为 `/data`），然后在容器内执行 `install_inside.sh` |
| `install_inside.sh` | 容器 | 编译打过补丁的 NeurDB、pg_hint_plan、auto_explain、nram、nr_molqo；建 Python 环境 `/opt/venv`；复制 NQO 服务到 `/opt/nqo`；初始化并配置数据库集群。分步打标记，可重跑 |
| `requirements-container.txt` | 容器 | `/opt/venv` 的 Python 包版本 |
| `load_imdb.sh` | 容器 | 下载并装载 IMDB（JOB 数据集）到 `imdb_ori`，取得 113 条 JOB 查询 |
| `start_services.sh` / `stop_services.sh` | 容器 | 启停数据库与 NQO 服务 |
| `check_deploy.sh` | 容器 | 二十多项安装检查，含 SELIX 索引、NQO 两位专家、经 nr_molqo 的端到端规划、资源统计文件 |

与 `prototype/p1/setup_container.sh`（P1 验证时的手工步骤）相比，这里的脚本是可重跑的，并且加入了 NQO 服务的 Python 环境与真实模型。
