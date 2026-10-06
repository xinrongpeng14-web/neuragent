# 操作教程（第二轮）：两项校准与第二轮对比实验

适用于已经按 `TUTORIAL.md` 建好容器 `neurdb-ga`、装好 IMDB 并跑过第一轮的实验机。第二轮改的是负载、档位和对照臂，不需要重建容器，也不需要重新编译数据库。第二轮为什么这样设计，见 `GlobalAgent_round2.md` 第 4 节。

| 第二轮与第一轮的差别 | 第一轮 | 第二轮 |
|---|---|---|
| JOB 查询 | 1 秒内的快查询，两个阶段相同 | 阶段 A 用长查询（基线 1 到 40 秒），阶段 B 用快查询 |
| 步长 / 每阶段步数 | 30 秒 / 30 步 | 60 秒 / 20 步（回合仍为 40 分钟） |
| 阶段 A 的负载 | 8 个 JOB 客户端，YCSB 90% 点查限速 2000/s | 3 个客户端跑长查询，YCSB 70% 点查限速 2000/s（读多阶段索引也在增长） |
| SELIX 第三档 | sparse 0.50/0.60/0.40 | mid 0.80/0.90/0.70 |
| 对照臂 | none / nqo / selix / both / ga | none / nqo / selix / static-best / ga，`static-best` 是两次扫描选出的最优组合 |
| 主判定量 | ga 相对 nqo（原版） | ga 相对 static-best（协调收益），ga 相对 nqo 保留 |
| 新增 | — | 两项校准：专家计划是否真的更快；索引内存是否影响 JOB |
| NQO 服务 | 每条查询都推理 | 可选 `NQO_CACHE=1`：重复的查询直接用缓存的决策 |

时间预算（4 核容器）：校准约 3 到 4 小时；基线 2 小时；两次扫描 4.7 小时；训练约 22 小时（1200 步 × 60 秒）；评估 20 小时（5 臂 × 3 种子 × 2 回合 × 40 分钟）。总计约 2.5 天。训练步数可以用 `TRAIN_STEPS=800` 压到 15 小时。

---

## 1. 更新代码与 NQO 服务

在实验机的 `neuragent` 目录里：

```bash
git pull
ls experiment/config/imdb_r2.json experiment/tools/nqo_plan_gain.py     # 两个文件都应存在
```

NQO 的 Python 侧补丁改了（服务端增加了决策缓存选项），要把补丁重新打到 `NeuralDB/` 并刷新容器里的服务副本。补丁文件不在公开仓库，先从开发机复制最新的：

```bash
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh
grep -c "cache-decisions" NeuralDB/aiengine/neurqo_frame/run.py          # 应输出 2 以上
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh
```

`setup_neurdb.sh` 要求 `NeuralDB/` 没有未提交改动，所以上面先把旧补丁的改动撤掉。`update_nqo.sh` 把 `/opt/nqo/neurqo_frame` 的代码换成新版并重启数据库与 NQO 服务。检查点：最后一行是 `NQO /stats: ...`，其中出现 `"cache_hits": 0`。

然后确认一切正常：

```bash
docker exec neurdb-ga bash /neuragent/deploy/check_deploy.sh      # 26 项全部 PASS
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test  # 单元测试 51 项
```

---

## 2. 划分查询集

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh queries
```

原版优化器下每条 JOB 查询跑 2 次，1 秒内完成的进 `experiment/queries/job_fast`，1 到 40 秒的进 `experiment/queries/job_long`，超过 40 秒或超时的不用。第一轮实验机上的数据预计约 75 条快、约 30 条长。检查点：两行 `... copied to ...`，长查询至少 15 条；少于 15 条时用 `LONG_MAX=60` 重跑。

---

## 3. 校准 1：专家的计划是否真的更快

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh gain
```

对全部 113 条查询：先问 NQO 的两位专家各给什么动作，对改变了计划的查询，交错执行原生计划与专家计划各 3 次取最小值，同时记下规划器对两种计划的代价估计。约 2 到 3 小时（`GAIN_RUNS=2 GAIN_TIMEOUT=60` 可以减半）。

结果：`experiment/runs/imdb_r2/nqo_gain.md`（表格）、`nqo_gain.json`、`nqo_gain_wins.txt`（专家计划快 20% 以上的查询）。最后三行打印总结。

**怎么读。** 单条查询的倍数不能直接相加：一条 60 秒的查询快 50 倍能省 59 秒，二十条 3 秒的查询各慢 50% 一共只多花 30 秒。所以要看每个查询集上一遍的总秒数，`gain` 阶段结束时自动打印（也可以单独执行 `pipeline.sh gain_summary`）：

```
set job_long: 30 queries
  one pass, cost-based plans:      280.0 s
  one pass, expert plans:          240.0 s  (-14.3%)
  + inference once per query:      255.0 s  (-8.9%)      <- 这一行是 hint 模式在阶段 A 的净效果
expert wins (>= 20% faster): 3; of these 1 have a cost-based baseline above LONG_MAX=40 s ...
  to include them: LONG_MAX >= 95 and job.statement_timeout_ms >= 237500
```

| `job_long` 上“含推理”的那一行 | 含义 | 下一步 |
|---|---|---|
| 为负（专家更快） | 阶段 A 里 hint 模式有净收益，而快查询集上通常为正（净亏），两个阶段的最优动作相反 | 继续第 4 步 |
| 为正，但 `expert wins` 里有基线超过 LONG_MAX 的查询 | 收益集中在被截掉的最慢查询上，这正是 Bao 原文的收益模式 | 按打印的建议提高 `LONG_MAX`，把 `experiment/config/imdb_r2.json` 里 `job.statement_timeout_ms` 调到建议值，重跑 `queries` 与 `gain_summary`（不必重跑 `gain`） |
| 为正，且没有被截掉的赢家 | 这台机器、这套预训练模型下 NQO 在任何阶段都是净亏，没有可协调的收益 | 见 `GlobalAgent_round2.md` 2.4 节的三个选项；先试 `NQO_CACHE=1` 消掉重复推理再看“不含推理”那一行是否为负 |
| JoinOrder 一条提示都不给 | 预期现象（KNN 门槛依赖原作者机器的延迟记录） | 不影响继续 |

开发机上的结果（1.5 核、磁盘受限，只测了 12 条快查询）见 `GlobalAgent_round2.md` 2.3 节。

---

## 4. 校准 2：索引内存是否影响 JOB

这项校准决定 SELIX 一侧的动作有没有意义。先在现在的 16 GB 容器下测：

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh memcal
```

它用环境本身跑阶段 A 两遍：YCSB 索引 100 万键（约 25 MB）和 2000 万键（约 600 MB），各 6 步，比较 JOB 每步完成数。准备 2000 万键的种子表和索引约需 5 分钟。最后打印一行判定：JOB 吞吐下降 5% 以上记为“有耦合”。

16 GB 下预计没有耦合（9 GB 的 IMDB 全在页缓存里）。这时收紧容器内存，让索引与页缓存争抢，再测一次：

```bash
deploy/set_memory.sh neurdb-ga 6g 1GB 3GB        # 在主机上执行：容器 6 GB，shared_buffers 1 GB
docker exec neurdb-ga bash /neuragent/deploy/start_services.sh
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh memcal
```

仍然没有耦合就再收一档（`5g 1GB 2GB`）。出现耦合后，**后面所有阶段都在这个内存设置下进行**，不要再改。要恢复第一轮的设置：`deploy/set_memory.sh neurdb-ga 16g 4GB 8GB`。

注意：收紧内存后 NQO 服务的 4 个工作进程约占 1.4 GB；容器低于 5 GB 时用 `NQO_WORKERS=2` 启动服务。

---

## 5. 跑第二轮实验

先用缩短配置把整条链路走一遍（约 25 分钟）：

```bash
docker exec -e CONFIG=config/imdb_r2_short.json -e THRESHOLD=2 -e LONG_MAX=40 -e BASELINE_EPISODES=1 \
    -e TRAIN_STEPS=12 -e EVAL_SEEDS=2001 -e EVAL_EPISODES=1 neurdb-ga \
    bash /neuragent/experiment/pipeline.sh all
cat experiment/runs/imdb_r2_short/report.md
```

然后按正式配置分阶段执行，放在后台：

```bash
E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"
nohup sh -c "$E seed && $E baseline && $E sweep_nqo && $E sweep_selix" > r2_part1.log 2>&1 &
```

约 7 小时。结束后看 `r2_part1.log` 末尾：

- `baseline` 的 `noise check`：60 秒窗口下阶段 A 的 q_J 变异系数可能高于第一轮（长查询每步只完成几十条），0.15 以内可以接受。
- `best NQO mode: ...` 与 `best SELIX preset: ...`：两次扫描选出的静态最优，评估阶段自动使用。

```bash
nohup sh -c "$E train && $E evaluate && $E report" > r2_part2.log 2>&1 &
```

约 42 小时。查看进度：

```bash
grep -E "^=====|best |\[episode|mid-way|-> return|report written|Error|Traceback" r2_part2.log | tail -20
```

训练中断后续训：`$E train --resume /neuragent/experiment/runs/imdb_r2/train/model.zip`，再单独执行 `$E evaluate` 和 `$E report`。

要把 NQO 的决策缓存作为一个变量考察，用 `NQO_CACHE=1` 重启服务后重跑 `baseline` 到 `report`，并换一个 `RUN_PREFIX=cached_`，两套结果都保留。

---

## 6. 结果在哪里，怎么判定

| 文件 | 内容 |
|---|---|
| `experiment/runs/imdb_r2/report.md` | 结果报告。第 5.1 节是协调收益（`ga` 相对 `static-best`），第 5.2 节是相对原版 |
| `experiment/runs/imdb_r2/nqo_gain.md` | 校准 1 |
| `experiment/runs/imdb_r2/memcal.json` | 校准 2 |
| `experiment/runs/imdb_r2/sweep_nqo/episodes.json`、`sweep_selix/episodes.json` | 两次扫描 |
| `experiment/runs/imdb_r2/<阶段>/steps.jsonl` | 每一步的全部原始数据 |

按 `GlobalAgent_round2.md` 4.4 节判定：

| 结论 | 条件 |
|---|---|
| 协调可行 | `ga` 相对 `static-best` 回报高 5% 以上且各种子一致；训练日志的阶段×动作表（报告第 4 节）显示两个阶段的最优动作确实不同 |
| 有潜力 | 阶段×动作表显示最优动作随阶段变化，但 `ga` 没有学到 |
| 协调无价值 | 某一个固定动作在所有阶段都最优；此时 `ga` 至多等于 `static-best` |

---

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `setup_neurdb.sh` 报 `NeuralDB/ has uncommitted changes` | 第 1 步里 `git checkout -- .` 与 `git clean` 没有执行完；在 `NeuralDB/` 里执行后重试 |
| `update_nqo.sh` 报 `source ... is not the current patched version` | 补丁没有重新打上，检查 `patches/neurdb/0003-*.patch` 是否是新版（约 690 行） |
| `queries` 阶段长查询太少 | `LONG_MAX=60`；注意 `job.statement_timeout_ms` 是 120 秒，长查询上限不要超过它的一半 |
| `gain` 阶段很慢 | `GAIN_RUNS=2 GAIN_TIMEOUT=60`；或用 `--only 17a,19d,26c` 只测几条 |
| `memcal` 的 2000 万键种子表准备失败 | 看 `/data/pg.log`；磁盘空间需要再留 3 GB |
| 收紧内存后数据库起不来 | `shared_buffers` 超过了容器内存的一半；用 `deploy/set_memory.sh` 调小 |
| 阶段 A 的 JOB 查询超时 | `job.statement_timeout_ms` 120 秒；超时的查询计入 `job_errors`，报告里会显示。多时把 `LONG_MAX` 调小重跑 `queries` |
| 训练日志里某个阶段所有动作奖励都相近 | 该阶段没有可学的差别，属于“协调无价值”的情形，不是程序错误 |

---

## 8. 在开发机上已经验证过的内容

开发机（2 核、1.5 核容器）上按本教程跑通：单元测试 51 项；`queries` 阶段生成快、长两个集合；`gain` 阶段（限定 4 条查询）、`memcal` 阶段（20 万与 100 万键）、缩短配置下的 `seed` 到 `report` 全链路，报告第 5.1 节给出 `ga` 相对 `static-best` 的判定。细节见 `WORKLOG.md`。
