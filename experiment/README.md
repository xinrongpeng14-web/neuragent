# 实验程序

Global Agent 雏形的实验程序，对应 `GlobalAgent_prototype.md` 第 4 到 7 节。从建容器到跑完实验的操作步骤见仓库根目录的 `TUTORIAL.md`；NQO、SELIX、GA 各自的状态与动作见 `STATES_AND_ACTIONS.md`。

| 模块 | 作用 |
|---|---|
| `gaproto/job_driver.py` | JOB 负载驱动：多个并发客户端循环执行分析查询 |
| `gaproto/ycsb_driver.py` | YCSB 负载驱动：单连接，在 SELIX 索引上做点查与插入 |
| `gaproto/cgroup.py` | 从 cgroup 读取容器的 CPU 与内存占用；容器内外均可 |
| `gaproto/procstat.py` | 从 `/proc` 按组件（NQO 服务、各类数据库后端、实验程序）统计 CPU 与内存 |
| `gaproto/nqo_client.py` | 读取 NQO 服务的 `/stats` |
| `gaproto/reset.py` | 种子表的准备与回合之间的重置 |
| `gaproto/env.py` | Gymnasium 环境：12 个离散动作，9 维观测 |
| `gaproto/metrics.py` | 每步指标、原版参考值、奖励、观测向量 |
| `gaproto/baseline.py` | 测量原版系统并生成奖励所需的参考值 |
| `gaproto/policies.py` | 策略：固定动作、按阶段的固定动作、训练好的 PPO |
| `gaproto/train.py` | 用 stable-baselines3 的 PPO 训练 GA |
| `gaproto/evaluate.py` | 多个对照臂交替运行，记录每一步 |
| `gaproto/report.py` | 从步日志生成结果报告（Markdown + JSON） |
| `gaproto/actions.py`、`keys.py`、`config.py`、`db.py` | 动作表、键生成、配置、数据库连接 |
| `tools/select_job_queries.py` | 选出原版系统在阈值内完成的 JOB 查询子集 |
| `tools/nqo_probe.py` | 把一条查询送到 NQO 服务，显示它的输入与输出 |
| `pipeline.sh` | 按阶段执行整个实验 |

## 在实验容器内运行

实验程序和数据库在同一个容器里运行（`deploy/` 下的脚本建好容器后，项目目录挂载在 `/neuragent`）。`pipeline.sh` 把各阶段串起来：

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test       # 单元测试
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh queries    # JOB 快查询子集
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh seed       # YCSB 种子表
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh baseline   # 原版 3 回合 -> refs.json
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh sweep      # SELIX 三档各 1 回合
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh train      # PPO 训练 1200 步
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh evaluate   # 五个臂交替评估
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh report     # 报告
```

`CONFIG=config/imdb_short.json` 可以用缩短的配置（每阶段 3 步、步长 10 秒）先把整条链路跑通。各阶段的其他参数见 `pipeline.sh` 文件头。

每个阶段的输出在 `<log_dir>/<run_name>/`：`steps.jsonl`（每一步的动作、奖励、观测和全部原始指标）、`config.json`，评估另有 `episodes.json`，训练另有 `model.zip` 与 `progress.json`。报告在 `<log_dir>/report.md` 与 `summary.json`。

## 在主机上运行（开发用）

依赖装在项目文件夹内的虚拟环境里，不写入系统或用户目录。

```bash
cd /path/to/neuragent
python3 -m venv --without-pip .venv
pip --python .venv/bin/python install --no-cache-dir -r experiment/requirements.txt
cd experiment && ../.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

从主机运行时配置里要写容器名（`container.name`），CPU 与内存从主机的 cgroup 目录读取；按组件的 `/proc` 统计只在容器内可用。

## 端到端冒烟测试

```bash
bash smoke/run_smoke.sh neurdb-p1
```

它用一个小型数据集和一个 NQO 替身服务检查整条链路，大约 3 分钟。替身服务的输出格式与真实专家一致，所以这项测试检查的是程序的连通性与正确性，**不检查优化效果**。

## 配置

配置文件是 JSON，只需要写与默认值不同的项，未知的键会报错。全部字段见 `gaproto/config.py`。

| 字段 | 默认 | 说明 |
|---|---|---|
| `step_s` | 30 | GA 步长，秒 |
| `phases` | A、B 各 30 步 | 每个阶段的步数、JOB 客户端数、YCSB 读比例与限速 |
| `warmup_steps` | 1 | 每个回合开始时以原版动作运行的步数，不计入回合 |
| `container.name` | 空 | 空表示实验程序在容器内运行；在主机上运行时填容器名 |
| `container.ncpus` | 0 | 容器可用的核数，是 CPU 利用率的分母；0 表示从 `cpu.max` 或 cpuset 读取 |
| `container.proc_stats` | true | 按组件统计 CPU 与内存（只在容器内有效） |
| `ycsb.initial_keys` | 1000000 | 每个回合开始时索引中的键数 |
| `job.query_dir` | `queries` | 每个 `.sql` 文件一条查询，文件名即模板名 |
| `reward.*` | 0.5、0.5、0.5、0.05 | JOB 权重、SELIX 权重、内存系数、切换惩罚 |

## 三类连接

| 连接 | 是否关闭 molqo | 原因 |
|---|---|---|
| JOB 客户端 | 否 | 它们必须跟随 GA 写入服务器配置的 NQO 模式 |
| YCSB 连接 | 是 | 服务器全局开启 molqo 后，未关闭的连接上每条 SELECT 都会被发给优化服务，包括 YCSB 的点查 |
| 控制连接 | 是 | 同上 |

关闭是通过连接的启动参数 `-c enable_molqo=off` 实现的，`RESET ALL` 之后仍然有效。

## 资源统计的分组

`procstat.py` 把容器里的每个进程归入一组：`nqo_server`（NQO 服务的进程）、`pg_nqo_client`（NQO 回连数据库做 EXPLAIN 的后端）、`pg_job`、`pg_ycsb`、`pg_admin`（实验的三类连接对应的后端，按 `application_name` 识别）、`pg_background`（postmaster 与后台进程）、`harness`（实验程序主进程：GA 推理、指标采集、训练更新）、`drivers`（YCSB 与 JOB 负载发生器，代表客户端）、`other`。CPU 按 `/proc/<pid>/stat` 的用户态加内核态时间计，内存取 `smaps_rollup` 的 PSS。为了让按后端的统计可解释，部署脚本关闭了并行查询与 JIT。

## 在 2 核主机上的已知现象

开发用的主机只有 2 个核，数据库容器、实验程序和主机上的其他进程共用它们。同样的动作在相邻两轮之间，YCSB 吞吐可以相差 3 到 4 倍。这台主机只适合检查程序是否正确。性能结论必须在满足 `GlobalAgent_prototype.md` 4.1 节要求的机器上得出。
