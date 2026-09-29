# 实验程序

Global Agent 雏形的实验程序，对应 `GlobalAgent_prototype.md` 第 4 到 7 节。

| 模块 | 作用 |
|---|---|
| `gaproto/job_driver.py` | JOB 负载驱动：多个并发客户端循环执行分析查询 |
| `gaproto/ycsb_driver.py` | YCSB 负载驱动：单连接，在 SELIX 索引上做点查与插入 |
| `gaproto/cgroup.py` | 从 cgroup 读取数据库容器的 CPU 与内存占用 |
| `gaproto/nqo_client.py` | 读取 NQO 服务的 `/stats` |
| `gaproto/reset.py` | 种子表的准备与回合之间的重置 |
| `gaproto/env.py` | Gymnasium 环境：12 个离散动作，9 维观测 |
| `gaproto/metrics.py` | 每步指标、原版参考值、奖励、观测向量 |
| `gaproto/baseline.py` | 测量原版系统并生成奖励所需的参考值 |
| `gaproto/actions.py`、`keys.py`、`config.py`、`db.py` | 动作表、键生成、配置、数据库连接 |

## 环境准备

依赖装在项目文件夹内的虚拟环境里，不写入系统或用户目录。

```bash
cd /path/to/neuragent
python3 -m venv --without-pip .venv
pip --python .venv/bin/python install --no-cache-dir -r experiment/requirements.txt
```

数据库一侧需要打过补丁的 NeurDB，见 `patches/neurdb/README.md`。

## 运行

所有命令在 `experiment/` 目录下执行。

```bash
PY=../.venv/bin/python

# 1. 单元测试，不需要数据库
$PY tests/test_units.py

# 2. 准备种子表。回合之间的重置由环境自动完成，也可以单独执行
$PY -m gaproto.reset --config config/default.json --prepare

# 3. 测量原版系统，生成参考值 refs.json
$PY -m gaproto.baseline --config config/default.json --episodes 3
```

环境在代码中这样使用：

```python
from gaproto.config import load_config
from gaproto.env import make_env

cfg = load_config("config/default.json")
env = make_env(cfg, refs_path=cfg.refs_path, run_name="train")
obs, info = env.reset(seed=1)
obs, reward, terminated, truncated, info = env.step(env.action_space.sample())
env.close()
```

每一步的动作、奖励和全部原始指标写入 `<log_dir>/<run_name>/steps.jsonl`。

## 端到端冒烟测试

```bash
bash smoke/run_smoke.sh neurdb-p1
```

它用一个小型数据集和一个 NQO 替身服务检查整条链路，大约 3 分钟。替身服务的输出格式与真实专家一致，所以这项测试检查的是程序的连通性与正确性，**不检查优化效果**。脚本依次执行：配置并启动容器内的数据库、启动替身服务、装载数据、准备种子表、跑 1 个回合的原版基线、做接口检查、跑 1 个回合的随机动作并在每一步核对测量值。

## 配置

配置文件是 JSON，只需要写与默认值不同的项，未知的键会报错。全部字段见 `gaproto/config.py`。

| 字段 | 默认 | 说明 |
|---|---|---|
| `step_s` | 30 | GA 步长，秒 |
| `phases` | A、B 各 30 步 | 每个阶段的步数、JOB 客户端数、YCSB 读比例与限速 |
| `warmup_steps` | 1 | 每个回合开始时以原版动作运行的步数，不计入回合 |
| `container.ncpus` | 4 | 容器可用的核数，是 CPU 利用率的分母 |
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

## 在 2 核主机上的已知现象

开发用的主机只有 2 个核，数据库容器、实验程序和主机上的其他进程共用它们。同样的动作在相邻两轮之间，YCSB 吞吐可以相差 3 到 4 倍。经过逐进程采样确认，这来自主机 CPU 被占满，回合结束后服务器上没有残留会话。

这台主机只适合检查程序是否正确。性能结论必须在满足 `GlobalAgent_prototype.md` 4.1 节要求的机器上得出。
