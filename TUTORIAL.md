# 操作教程：从建容器到跑完对比实验

本教程面向另一台（更大的）机器。按顺序执行，每一步都有检查点。所有命令在那台机器的终端执行；`docker exec` 开头的命令在容器内运行。

实验要回答的两个问题、对照臂的定义、各组件的状态与动作，见 `experiment/STATES_AND_ACTIONS.md`；方案本身见 `GlobalAgent_prototype.md`。

---

## 0. 机器要求

| 项 | 要求 | 说明 |
|---|---|---|
| CPU | 至少 8 核 | 容器用 4 核，其余留给主机 |
| 内存 | 至少 16 GB | 容器限 16g：数据库共享缓冲 4 GB，NQO 服务 4 个工作进程各约 350 MB，JOB 查询的工作内存 |
| 磁盘 | 至少 40 GB 空闲 | IMDB 压缩包 1.2 GB、CSV 3.7 GB、数据库约 9 GB、编译产物与 Python 环境约 4 GB |
| 软件 | Docker 20 以上，git，能访问 GitHub、ubuntu 软件源、PyPI、`event.cwi.nl` | 主机**不需要**编译器、Python 包或 PostgreSQL |
| 内核 | cgroup v2（Ubuntu 22.04 及以后默认） | 资源统计从容器内的 `/sys/fs/cgroup` 读取。检查：`stat -fc %T /sys/fs/cgroup` 应输出 `cgroup2fs` |

时间预算（4 核容器）：建容器并编译约 30 分钟；下载并装载 IMDB 约 30 分钟；原版基线 1.5 小时；SELIX 档位扫描 1.5 小时；训练约 11 小时；评估约 15 小时（5 个臂 × 3 个种子 × 2 回合 × 30 分钟）。总计约 1.5 天，各阶段可以分开执行。

---

## 1. 取得代码与补丁

```bash
git clone git@github-pxr:xinrongpeng14-web/neuragent.git     # 或 https://github.com/xinrongpeng14-web/neuragent.git
cd neuragent
git checkout ga-prototype
```

对 NeurDB 的三个补丁文件**不在公开仓库里**（NeurDB 的许可证保留所有权利，见 `patches/neurdb/README.md`）。从开发机复制过来：

```bash
# 在新机器的 neuragent 目录里执行。把 DEV 换成开发机的“用户名@地址”，不要加尖括号
DEV=zhanhao@34.31.210.7
scp "$DEV:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
ls patches/neurdb/*.patch        # 应列出 3 个文件
```

远程路径要放在引号里，`*` 才会由开发机展开。新机器需要能用 SSH 登录开发机；如果不能，就在开发机上反方向推送：`scp /home/zhanhao/neuragent/patches/neurdb/*.patch 用户名@新机器地址:<新机器上 neuragent 的路径>/patches/neurdb/`。

然后取得上游 NeurDB 并打补丁（脚本会 clone 到 `NeuralDB/`、禁用对上游的 push、检出固定提交、初始化 SELIX 子模块、应用补丁）：

```bash
scripts/setup_neurdb.sh
```

检查点：最后一行是 `NeuralDB/ is at fad1bcbd... plus the prototype patches`。`git -C NeuralDB status --short` 应显示 13 个修改文件和 1 个新文件。

---

## 2. 建容器并编译

```bash
CPUS=4 CPUSET=0-3 MEMORY=16g deploy/create_container.sh
```

脚本做的事：建一个名为 `neurdb-ga` 的 ubuntu:22.04 容器（`--init`，项目目录挂载在容器的 `/neuragent`，数据卷 `neurdb-ga-data` 挂载在 `/data`），然后在容器内执行 `deploy/install_inside.sh`：安装编译工具 → 编译 NeurDB 引擎到 `/opt/neurdb` → 编译 auto_explain、pg_hint_plan（PG16 分支）、nram（含 SELIX）、nr_molqo → 建 Python 环境 `/opt/venv`（NQO 的依赖按 `environment_moqoe.yml` 的版本固定，CPU 版 torch 2.2.2，另加 gymnasium 与 stable-baselines3）→ 把 `neurqo_frame` 复制到可写的 `/opt/nqo` → 初始化数据库集群 `/data/pg` 并写入配置。

可调参数（环境变量）：`PG_SHARED_BUFFERS`（默认 4GB）、`PG_WORK_MEM`（64MB）、`PG_EFFECTIVE_CACHE`（8GB）、`JOBS`（编译并行度，默认核数）。内存小于 16 GB 的机器把 `MEMORY` 与 `PG_SHARED_BUFFERS` 按比例调小。

每一步完成后在 `/opt/.ga_stamps/` 留下标记；中途失败时修正后重新执行 `docker exec neurdb-ga bash /neuragent/deploy/install_inside.sh`，已完成的步骤会跳过。第一次运行时给出的 `PG_*` 设置记录在 `/opt/.ga_stamps/settings.env`，重跑时自动沿用；要改某一项，重跑时再次用 `-e` 传入即可（改数据库参数时先删掉标记文件 `cluster`）。

检查点：输出以 `install finished` 结尾，`extensions` 一行列出 `auto_explain.so nr_molqo.so nram.so pg_hint_plan.so`。

数据库配置里与实验直接相关的几项（`/data/pg/postgresql.conf` 的 `ga` 块）：

```
shared_preload_libraries = 'pg_hint_plan, nr_molqo, nram'   # 顺序不能变
molqo.server_url = 'http://127.0.0.1:8666/optimize'
molqo.report_decisions = off
molqo.timeout_ms = 10000
max_parallel_workers_per_gather = 0                          # 便于按后端进程统计 CPU
jit = off
```

---

## 3. 装载 IMDB 数据与 JOB 查询

```bash
docker exec neurdb-ga bash /neuragent/deploy/load_imdb.sh
```

脚本从 `https://event.cwi.nl/da/job/imdb.tgz` 下载（支持断点续传），解压到 `/data/imdb/csv`，建库 `imdb_ori`，按 NeurDB 自带的 `schema.sql`、`fkindexes.sql` 建表建索引，4 张表并行装载，加外键，`ANALYZE`，核对 21 张表的行数，在 `imdb_ori` 里创建扩展 `nram`、`nr_molqo`，最后 clone JOB 查询仓库并把 113 个 `.sql` 放到 `experiment/queries/job_all/`。

注意：这个压缩包里的 CSV **没有表头行**，脚本用 `FORMAT csv, ESCAPE '\'` 装载。NeurDB 自带的装载脚本写的是 `csv header`，会把每张表的第一行当表头丢掉，所以这里没有沿用它。

检查点：`all 21 tables have the expected row counts`，以及 `113 query files in /neuragent/experiment/queries/job_all`。

`PARALLEL=8` 可以加快装载；`IMDB_URL` 可以换成本地镜像。

---

## 4. 启动服务并检查

```bash
docker exec neurdb-ga bash /neuragent/deploy/start_services.sh      # NQO_WORKERS=4 可改
docker exec neurdb-ga bash /neuragent/deploy/check_deploy.sh
```

`start_services.sh` 启动数据库，再以冻结模式启动 NQO 服务（`run_moqoe_prototype.sh`，4 个工作进程，每个加载自己的模型，约 30 秒）。注意 NQO 服务连接数据库时会执行上游代码里的 `ALTER SYSTEM SET autovacuum TO off`，这是 NeurDB 原有行为，对所有对照臂一致。

`check_deploy.sh` 做 26 项检查，全部应为 PASS。E4 要把 JOB 查询 19d 执行两次（NQO 关 / JoinOrder），在 4 核 16 GB 的机器上约 1 分钟，在开发机上用了 5 分钟：

| 组 | 检查 |
|---|---|
| D1–D9 | 服务是 NeurDB 构建、预加载顺序、IMDB 表与行数、扩展、`molqo_status()`、并行与 JIT 已关 |
| S1–S4 | 在一个会话里建 nrindex、1000 次点查正确、插入后可查、`nrindex_stats()` 计数与 dense 档参数生效 |
| N1–N4 | NQO 服务 `/health`、`/stats`；用 `tools/nqo_probe.py` 把查询 1a 送给两位专家，都无错应答；JoinOrder 对查询 19d 给出 `Leading` 提示 |
| E1–E4 | 查询 1a 经 nr_molqo 规划：JoinOrder 的决策被报告、HintPlanSel 的 SET 生效且语句后复位；查询 19d 的 `Leading` 提示经 pg_hint_plan 生效 |
| R1–R3 | cgroup v2 文件可读、进程 PSS 可读 |
| P1–P2 | Python 环境可导入、单元测试通过 |

N3 的输出同时就是"NQO 读到什么、输出什么"的直接证据。开发机上的实际输出（1 个工作进程）：

```
input state sent by the database: SQL text, 609 characters
expert_filter=hint: expert HintPlanSel, applied True, inference 308.6 ms
  action: SET enable_nestloop TO off; ... SET enable_indexonlyscan TO on;   （12 条开关，这里选的是"全开"一组）
expert_filter=join: expert JoinOrder, applied False, inference 196.7 ms
  action: (unchanged: cost-based optimizer)
```

JoinOrder 专家只在预测原生计划延迟超过 100 ms 且搜到更快顺序时才给提示，所以对快查询多数时候不改写；对 19d 它给出 `/*+Leading(chn ci)*/`。详见 `experiment/STATES_AND_ACTIONS.md` 第 1.2 节。

服务日志在容器内 `/data/pg.log` 与 `/data/nqo.log`。停止服务：`docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh`。

---

## 5. 跑实验

所有阶段通过 `experiment/pipeline.sh` 执行，输出在主机的 `experiment/runs/imdb/`（容器内的 `/neuragent/experiment/runs/imdb/`）。先用缩短配置把整条链路走一遍（约 15 分钟）：

```bash
docker exec -e CONFIG=config/imdb_short.json -e THRESHOLD=2 -e BASELINE_EPISODES=1 \
    -e TRAIN_STEPS=12 -e EVAL_SEEDS=2001 -e EVAL_EPISODES=1 neurdb-ga \
    bash /neuragent/experiment/pipeline.sh all
cat experiment/runs/imdb_short/report.md
```

然后按正式配置分阶段执行（每一步结束再执行下一步；长阶段建议放在 `tmux`/`nohup` 里）：

```bash
E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"
$E test        # 单元测试
$E queries     # 原版优化器下 1 秒内完成的 JOB 查询 -> experiment/queries/job_fast/
$E seed        # YCSB 种子表（100 万键）
$E baseline    # 原版 3 回合 -> runs/imdb/refs.json，并打印噪声检查
$E sweep       # SELIX 三档各 1 回合（NQO 关），选出 SELIX 独立调参会用的档位
$E train       # PPO 1200 步，约 11 小时；中途在第 600 步打印检查
$E evaluate    # none / nqo / selix / both / ga 五个臂交替，3 个种子 × 2 回合
$E report      # runs/imdb/report.md 与 summary.json
```

各阶段的参数（阈值、回合数、训练步数、种子）见 `experiment/pipeline.sh` 文件头，用环境变量传入，例如 `docker exec -e TRAIN_STEPS=600 neurdb-ga bash /neuragent/experiment/pipeline.sh train`。

**各阶段的检查点**

- `queries`：至少 10 条查询入选；打印一遍查询在单客户端上耗时合计。少于 10 条时提高 `THRESHOLD`。
- `baseline`：最后打印 `noise check`，q_J 与 c_idx 的变异系数在 30 秒窗口下应低于 0.10；高于时把配置里的 `step_s` 改为 60 再跑一次（训练时间随之翻倍）。
- `sweep`：输出 `best preset: dense|default|sparse`。
- `train`：每回合打印回报；`runs/imdb/train/progress.json` 记录每回合；中断后可用 `docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh train --resume /neuragent/experiment/runs/imdb/train/model.zip` 续训。
- `evaluate`：`runs/imdb/eval/episodes.json` 每个回合一条摘要。
- `report`：报告第 1、2 节回答"资源与收益"，第 3 节回答"状态与动作"，第 5 节按雏形文档 7.5 节给出判定。

---

## 6. 结果在哪里

| 文件 | 内容 |
|---|---|
| `experiment/runs/imdb/report.md` | 结果报告 |
| `experiment/runs/imdb/summary.json` | 报告中全部数字 |
| `experiment/runs/imdb/<阶段>/steps.jsonl` | 每一步：动作、奖励、9 维观测、JOB/YCSB/索引/NQO 指标、各组件 CPU 与内存 |
| `experiment/runs/imdb/eval/episodes.json` | 评估的每回合摘要 |
| `experiment/runs/imdb/train/model.zip`、`progress.json` | 训练好的策略、每回合回报 |
| `experiment/queries/job_fast/latencies.json` | 全部 JOB 查询在原版下的延迟 |

`runs/` 与 `queries/job_*` 不入 git。要保存结果，把 `experiment/runs/imdb/` 复制出来或另建分支提交。

---

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `create_container.sh` 报 `NeuralDB/ does not carry the prototype patches` | 补丁没复制或 `scripts/setup_neurdb.sh` 没执行，回到第 1 步 |
| 初始化集群时打印 `initdb: error: could not stat file "/data/pg/pg_xlog"` | NeurDB 分支的 initdb 多打了一行，初始化照常成功，可忽略。另外这个分支的引导库叫 `neurdb`，没有 `postgres` 库，脚本已按此处理 |
| 编译某一步失败 | 看容器内 `/build/<模块>/make.log`，修正后重跑 `install_inside.sh`，已完成的步骤会跳过 |
| `load_imdb.sh` 下载很慢或中断 | 重跑即可续传；或先用其他方式取得 `imdb.tgz` 放到容器内 `/data/imdb/imdb.tgz` |
| 行数核对出现 WARN | 多半是用了带表头的另一份 CSV；删除库重装（`dropdb imdb_ori`，删掉 `/data/.stamps/imdb_*`） |
| `check_deploy.sh` 的 E1/E2 失败 | 看 `/data/nqo.log`：真实模型第一次推理会回连数据库跑 EXPLAIN，慢时超过 `molqo.timeout_ms`；重跑一次或把该参数调大 |
| NQO 服务起不来、日志里 `ModuleNotFoundError` | 删除 `/opt/.ga_stamps/venv` 后重跑 `install_inside.sh`，并把缺的包加到 `deploy/requirements-container.txt` |
| `baseline` 噪声检查不达标 | 先确认主机上没有其他负载；把 `step_s` 改为 60 |
| 评估中 YCSB 连接断开（回合提前结束） | 看 `/data/pg.log`；索引随后端进程消失，环境会在下一回合重建 |
| 容器重建 | `docker rm -f neurdb-ga` 后重跑第 2 步；数据卷 `neurdb-ga-data` 里的数据库与 IMDB 数据保留，第 3 步会跳过已完成的部分 |

---

## 8. 在开发机上已经验证过的内容

开发机只有 2 核 3.9 GB 内存，容器限 1.5 核 2.5 GB、`shared_buffers` 256 MB、NQO 1 个工作进程。以下内容在这台机器上按本教程的步骤跑通：

| 内容 | 结果 |
|---|---|
| 第 1–2 步：建容器、编译引擎与四个扩展、Python 环境、集群初始化 | 通过，约 26 分钟（引擎编译 19 分钟） |
| 第 3 步：下载 1.2 GB 压缩包、装载 21 张表、外键、`ANALYZE`、行数核对、113 条 JOB 查询 | 通过，库 9.1 GB；21 张表行数全部与基准一致 |
| 第 4 步：`check_deploy.sh` 26 项 | 全部通过；E4 确认 19d 在 JoinOrder 提示下的计划与原生计划不同 |
| 真实 NQO 模型：Python 3.10、torch 2.2.2 CPU、psqlparse 源码编译 | 通过；1 个工作进程驻留 350 MB，启动 5 到 8 秒 |
| 真实 NQO 模型的两位专家 | HintPlanSel 对每条查询返回一组开关；JoinOrder 对快查询不改写、对 19d 给出 `Leading(chn ci)`，与代码中"预测延迟超过 100 ms 才提示"的门槛一致 |
| 第 5 步：缩短配置下的整条流水线（30 条快查询、1 回合基线、3 档扫描、12 步训练、5 个臂评估、报告） | 通过，约 35 分钟；报告各节均有数据 |
| 实验程序单元测试 | 49 项通过 |

过程中修掉的问题（都已反映在脚本里）：NeurDB 的 initdb 建的引导库叫 `neurdb` 而不是 `postgres`；NQO 的 `controller_offline.py` 需要 `tensorboard`；新版 stable-baselines3 会把 torch 升到带 CUDA 的 2.3 以上，固定为 2.4.1；重跑安装脚本时 `PG_*` 设置曾回落到默认的 4 GB，现在记录在 `settings.env`；装载脚本的并行 `\copy` 引号；NQO 服务单线程时推理期间 `/stats` 超时，改为每连接一个线程并用锁串行化推理；环境的 `info["episode"]` 与 stable-baselines3 的保留键冲突，改名 `episode_index`。

在这台小机器上得到的数字（基线 p99 10 秒以上、各臂回报的差异）只说明程序能跑，不说明方案的好坏。
