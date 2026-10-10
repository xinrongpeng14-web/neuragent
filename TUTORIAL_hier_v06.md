# 操作教程：在实验机上运行分层 GA（方案 v0.6.1）

方案见 `GlobalAgent_hierarchical.md` v0.6.1，第 5.6 节是本程序的全部运行设置。代码在 `experiment/gaproto/hier/`，流水线阶段为 `h_baseline`、`h_sweep`、`h_train`、`h_eval`、`h_report`（合起来是 `h_all`）。

**前提**：实验机上的 `neurdb-ga` 容器、`imdb_ori` 数据库、F1 到 F4 时建的 SELIX 索引都还在；`experiment/queries/job_long`（16b、19d、26a、26c）与 `experiment/queries/job_fast`（77 条）是 F1 到 F4 用的那两组。下面所有命令都在实验机的 `neuragent` 目录里执行。

**总用时**：准备约 30 分钟，长查询组约 6.5 到 7.5 小时，短查询组约 1 到 1.5 小时，合计约 **8 到 9 小时**（明细见第 6 节）。两组依次运行，不要同时运行（会互相争抢 CPU，延迟失真）。

---

## 一页命令清单

```bash
# 1 更新代码与补丁（含 SELIX 修复 E7，必须做）
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
wc -l patches/neurdb/0001-*.patch                       # 1337 行
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh

# 2 先停服务，再重新编译 SELIX 扩展，然后启动服务
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh

# 3 检查
docker exec -u neurdb neurdb-ga bash /neuragent/prototype/r2/e7_quick_check.sh     # PASS
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test                  # Ran 68 tests, OK
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh selix_list            # 17 个 nr_ 索引
docker exec neurdb-ga bash -c 'free -g; cat /sys/fs/cgroup/memory.max'              # 内存，见第 3 节

# 4 两组依次运行（后台）
nohup sh -c 'for g in long short; do docker exec -e GROUP=$g neurdb-ga bash /neuragent/experiment/pipeline.sh h_all; done; echo ALL_DONE' > hier_v06.log 2>&1 &

# 5 看进度
grep -E "^=====|\[episode|static-best:|references of|model saved|Traceback|Error|ALL_DONE" hier_v06.log | tail -20

# 6 看结果
cat experiment/runs/hier_long/report.md experiment/runs/hier_short/report.md
```

---

## 1. 更新代码与补丁

**这一步必须做。** 训练中 GA 会尝试"只用 HintPlanSel + SELIX"一类命令；没有 SELIX 修复 E7 时，26a 在这类命令下返回错误的结果并显得"快了 25 倍"，GA 会从错误的加速中学习。

```bash
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
wc -l patches/neurdb/*.patch
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh
grep -c "a NULL key matches no row" NeuralDB/dbengine/nr_kernel/nr_am/src/nrindex.c
```

| 检查点 | 期望 |
|---|---|
| `git log --oneline -1` | 不早于本教程所在的提交 |
| `wc -l` | 0001 为 1337 行 |
| `setup_neurdb.sh` 最后一行 | `NeuralDB/ is at ... plus the prototype patches` |
| `grep -c` | `1` |

如果之前已经按 `TUTORIAL_26a_recheck.md` 做过第 1、2 步，这里和第 2 步可以跳过，直接做第 3 步的检查。

---

## 2. 重新编译 SELIX 扩展

**必须先停服务。** 数据库进程一直使用启动时加载的扩展，不重启，新代码不生效。

```bash
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh
```

| 命令 | 检查点 |
|---|---|
| `stop_services.sh` | `NQO service stopped`、`database stopped` |
| `install_inside.sh` | 只有 nr_am 一步重做，最后是 `install finished` |
| `update_nqo.sh` | 最后一行 `NQO /stats: ...`；数据库与 NQO 服务都已启动，NQO 决策缓存开启 |

---

## 3. 检查

```bash
docker exec -u neurdb neurdb-ga bash /neuragent/prototype/r2/e7_quick_check.sh
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh selix_list
docker exec neurdb-ga bash -c 'free -g; cat /sys/fs/cgroup/memory.max'
```

| 检查 | 期望 | 不符合时 |
|---|---|---|
| E7 快速检查 | 最后一行 `PASS  E7 fixed: ...` | selix 一行第二个数大于 0：数据库还在用旧扩展，回到第 2 步 |
| 单元测试 | `Ran 68 tests`、`OK` | 把输出发给我 |
| SELIX 索引 | 已存在 F4 用过的 17 个 `nr_` 索引 | 已被删除：`$E selix_create` 再 `$E selix_keep`（`E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"`，约 20 到 30 分钟） |
| 内存 | 可用内存（`free -g` 的 available，或 `memory.max`）**不少于 12 GB** | 见下 |

**内存说明**：每个查询会话各自装载一份 SELIX（只装载本组查询连接列上的那些）。长查询组 1 个会话，约 1.5 GB；短查询组 4 个会话，每个约 2.2 GB，合计约 9 GB，另加数据库与 NQO 服务。可用内存不足 12 GB 时，短查询组改用 2 个会话：在第 4 步的命令里给短查询组加 `-e H_CLIENTS=2`，例如

```bash
nohup sh -c 'docker exec -e GROUP=long neurdb-ga bash /neuragent/experiment/pipeline.sh h_all; docker exec -e GROUP=short -e H_CLIENTS=2 neurdb-ga bash /neuragent/experiment/pipeline.sh h_all; echo ALL_DONE' > hier_v06.log 2>&1 &
```

`H_CLIENTS` 必须在同一组的所有阶段保持一致（`h_all` 会自动做到；单独重跑某个阶段时也要带上）。

---

## 4. 运行

```bash
nohup sh -c 'for g in long short; do docker exec -e GROUP=$g neurdb-ga bash /neuragent/experiment/pipeline.sh h_all; done; echo ALL_DONE' > hier_v06.log 2>&1 &
```

`h_all` 依次执行五个阶段，结果写在 `experiment/runs/hier_<组>/`：

| 阶段 | 做什么 | 产出 |
|---|---|---|
| `h_baseline` | 原版命令（NQO 自动 / 索引按代价）跑 3 个回合，记下每条查询的参考延迟（中位数）和参考结果 | `refs.json`、`baseline/` |
| `h_sweep` | 9 种固定命令各跑 1 个回合，选出总延迟最小的作为 static-best | `static_best.json`、`sweep/` |
| `h_train` | PPO 训练 GA：长组 600 步，短组 1600 步；每个回合结束保存一次模型 | `train/model.zip`、`train/progress.json`、`train/steps.jsonl` |
| `h_eval` | G、O、static-best、none、PG 五组，种子 2001 到 2003 × 2 个回合，组的顺序轮换 | `eval/episodes.json`、`eval/steps.jsonl` |
| `h_report` | 报告与判定（G 对 O） | `report.md` |

每个阶段开始时会先预热：装载 SELIX，并按原版命令把每条查询执行一遍（不计入结果）。输出的第一行类似 `4 queries, 17 SELIX columns (9 used by the queries), 1 session(s) warmed up in 150 s`。

---

## 5. 看进度

```bash
grep -E "^=====|\[episode|static-best:|references of|model saved|Traceback|Error|ALL_DONE" hier_v06.log | tail -20
tail -3 hier_v06.log
```

| 看到的行 | 含义 |
|---|---|
| `===== [...] hierarchical GA (long): ...` | 进入某个阶段 |
| `references of 4 queries written to ...` | 基线完成 |
| `static-best: <命令> (... s)` | 扫描完成 |
| `[episode N] mean reward +0.xxx (last 5: ...)` | 训练中，每个回合一行；奖励大于 0 表示这个回合比原版快 |
| `model saved to ...` | 训练完成 |
| `-> G seed 2001: total ... s` | 评估中，每个回合一行 |
| `WRONG RESULT` | 某次执行结果与原版不同：记下这一行发给我 |
| `ALL_DONE` | 两组全部完成 |

训练过程中也可以随时看最近几个回合：

```bash
python3 -c "import json; d=json.load(open('experiment/runs/hier_long/train/progress.json')); [print(e['episode'], round(e['mean_reward'],3), e['commands']) for e in d['episodes'][-5:]]"
```

---

## 6. 预估运行时间

依据：实验机 F4 实测的各命令延迟（长组 4 条查询在 9 种命令下的总延迟 71 到 148 秒，原版 85.67 秒；短组 77 条 21 到 28 秒），以及开发机上的试运行。

**长查询组**（每回合 12 步，每步 1 条查询）

| 阶段 | 内容 | 预估 |
|---|---|---|
| 每个阶段的预热 | 装载 SELIX 约 1 分钟 + 4 条查询各执行一遍约 1.5 分钟 | 约 2.5 分钟 × 4 个阶段 = 10 分钟 |
| `h_baseline` | 3 个回合 × 12 步 × 平均 21 秒 | 约 13 分钟 |
| `h_sweep` | 9 个回合，按 F4 各命令的延迟合计 | 约 45 分钟 |
| `h_train` | 600 步；开始时命令接近随机（平均每步约 24 秒），后期接近最好的命令（约 18 秒） | 约 3 到 4 小时 |
| `h_eval` | 30 个回合：PG 每回合约 7.4 分钟，其余约 3.5 到 4.3 分钟 | 约 2.4 小时 |
| **合计** | | **约 6.5 到 7.5 小时** |

**短查询组**（每回合 16 步，每步 5 条查询，4 个会话并行）

| 阶段 | 内容 | 预估 |
|---|---|---|
| 每个阶段的预热 | 4 个会话并行装载 SELIX 约 3 到 4 分钟 + 77 条查询执行一遍约 0.5 分钟 | 约 4 分钟 × 4 个阶段 = 16 分钟 |
| `h_baseline` | 3 个回合，每回合约 15 秒 | 约 1 分钟 |
| `h_sweep` | 9 个回合 | 约 3 分钟 |
| `h_train` | 1600 步（100 个回合） | 约 30 到 45 分钟 |
| `h_eval` | 30 个回合 | 约 8 到 10 分钟 |
| **合计** | | **约 1 到 1.5 小时** |

用 `H_CLIENTS=2` 时，短查询组每回合约慢一倍，合计约 1.5 到 2 小时。

**准备工作**（第 1 到 3 步）约 30 分钟；若要跑可选的完整回归测试 `e1e2_test.sh`（37 项）再加 20 分钟。

---

## 7. 看结果

```bash
cat experiment/runs/hier_long/report.md
cat experiment/runs/hier_short/report.md
```

报告分五节：原版基线（每条查询的参考延迟）、九种固定命令的扫描、训练过程、五组的评估、判定。

| 判定 | 条件（方案第 7 节） |
|---|---|
| 可行 | G 的总延迟比 O 低 10% 以上，3 个种子方向一致，p99 不劣于 O |
| 有潜力 | G 比 O 低，但没有同时满足上面三条 |
| 不可行 | G 不优于 O |
| 结果有误 | G 组有执行结果与原版不同，先排查 |

判定下面还有一行"G 相对 static-best"：低于 0 说明 GA 的按步切换比最好的固定命令还好。

**把这些发给我**（用 scp 传回开发机的同名目录）：

```
experiment/runs/hier_long/report.md   experiment/runs/hier_short/report.md
experiment/runs/hier_long/{refs.json,static_best.json}   experiment/runs/hier_short/{refs.json,static_best.json}
experiment/runs/hier_long/train/progress.json   experiment/runs/hier_short/train/progress.json
experiment/runs/hier_long/eval/episodes.json    experiment/runs/hier_short/eval/episodes.json
hier_v06.log
```

`steps.jsonl`（每一步的完整记录）较大，需要时再传。

---

## 8. 中断后继续、单独重跑

| 情况 | 做法 |
|---|---|
| 训练中断 | `docker exec -e GROUP=long -e H_RESUME=1 neurdb-ga bash /neuragent/experiment/pipeline.sh h_train`，从最后保存的模型继续；然后依次执行 `h_eval`、`h_report` |
| 只重跑某个阶段 | 把 `h_all` 换成该阶段名，例如 `docker exec -e GROUP=short neurdb-ga bash /neuragent/experiment/pipeline.sh h_eval` |
| 改训练步数 | 加 `-e H_TRAIN_STEPS=1000` |
| 改评估规模 | `-e H_SEEDS=2001,2002 -e H_EPISODES=1` |
| 重新开始一组 | `rm -rf experiment/runs/hier_<组>` 后重跑 `h_all` |

基线（`refs.json`）决定奖励和"相对 O"的比较基准；重跑基线后，训练与评估也要重跑。

---

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `no SELIX index on the IMDB tables` | SELIX 索引被删除了，见第 3 节重建 |
| `references ... not found: run the baseline first` | 先执行 `h_baseline` |
| `WRONG RESULT` | 停下来，把那一行和 `steps.jsonl` 发给我；不要用这一组的结果 |
| 进程被系统杀掉、日志中断、`dmesg` 里有 `Out of memory` | 内存不足：短查询组改用 `H_CLIENTS=2`，从头重跑短查询组 |
| NQO 相关的命令全部很慢或报错 | `docker exec neurdb-ga tail -50 /data/nqo.log`；确认服务在运行：`curl -s http://127.0.0.1:8666/stats` |
| `setup_neurdb.sh` 报 `NeuralDB/ has uncommitted changes` | 第 1 步的 `git checkout` 与 `git clean` 没执行完，在 `NeuralDB/` 里执行后重试 |

---

## 10. 在开发机上已经验证过的内容

- 单元测试 68 项通过（新增的分层 GA 测试 12 项：命令编解码、SQL 解析、状态向量、奖励、回合顺序、参考值、报告判定、策略）。
- 用 4 条短查询（6a、32a、11b、21c）、4 个小的 SELIX 索引、2 个会话，把基线、扫描（9 种命令全部执行）、PPO 训练（16 步）、评估（五组）、报告完整跑通；没有结果错误。开发机只有 2 核、4 GB，数字只说明程序可用。
- 缺少基线、缺少模型、缺少 static-best 时，各阶段会给出明确提示并停下或跳过对应的组。
