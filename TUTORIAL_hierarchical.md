# 操作教程：在实验机上跑可行性验证 F1 到 F4

本教程从“已有的实验机容器”出发，一步一步跑完分层方案第 4 节的 F1 到 F4。每一步都写了要执行的命令、预计用时、以及怎样确认这一步成功。验证的目的与判定标准见 `GlobalAgent_hierarchical.md` 第 4 节。

**前提**：实验机上已经按 `TUTORIAL.md` 建好容器 `neurdb-ga`、装好 IMDB 数据库 `imdb_ori`。下面所有命令都在实验机的 `neuragent` 目录里执行；以 `docker exec` 开头的命令在容器里运行。

**全部 SQL 都是只读查询。** 两组查询分别验证：

| 组 | 查询集 | 内容 |
|---|---|---|
| long | `experiment/queries/job_long` | 原生计划 10 到 120 秒的 JOB 查询 |
| short | `experiment/queries/job_fast` | 原生计划 1 秒以内的 JOB 查询 |

**总用时**（4 核容器）约 7 到 8 小时，其中长查询组的 F4 最长。

| 步 | 内容 | 用时 |
|---|---|---|
| 1 | 更新代码与补丁，重新编译 SELIX 扩展 | 约 30 分钟 |
| 2 | 检查安装 | 约 10 分钟 |
| 3 | 确认查询集 | 0 到 30 分钟 |
| 4 | 建 SELIX 索引 | 约 15 到 30 分钟 |
| 5 | F1（两组） | 约 30 分钟 |
| 6 | F2、F3、F4（两组，后台） | 约 5 到 6 小时 |
| 7 | 收集结果 | — |

---

## 一页命令清单

熟悉之后，按这个顺序执行即可；每一步的说明与检查点在后面各节。

```bash
# 1 更新
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh

# 2 检查
docker exec neurdb-ga bash /neuragent/deploy/check_deploy.sh
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test
docker exec neurdb-ga su neurdb -c "bash /neuragent/prototype/r2/e1e2_test.sh"

# 3 查询集（长查询下限须为 10 秒）
python3 -c "import json; print(json.load(open('experiment/queries/job_long/latencies.json')).get('long_min_s'))"
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh queries      # 只在上一行不是 10 时执行

# 4 建 SELIX
E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"
$E selix_list
$E selix_create

# 5 F1
docker exec -e GROUP=long  neurdb-ga bash /neuragent/experiment/pipeline.sh f1
docker exec -e GROUP=short neurdb-ga bash /neuragent/experiment/pipeline.sh f1
$E selix_keep

# 6 F2 到 F4（后台）
nohup sh -c 'for c in f2 f3 f4; do for g in short long; do docker exec -e GROUP=$g neurdb-ga bash /neuragent/experiment/pipeline.sh $c; done; done; echo ALL_DONE' > feasibility.log 2>&1 &

# 7 结果
ls experiment/runs/imdb_r2/f*_*.md
```

---

## 1. 更新代码、补丁与扩展

**1.1 拉代码，复制补丁。** SELIX 一侧的改动在补丁 0001 里，补丁不在公开仓库，要从开发机复制：

```bash
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
wc -l patches/neurdb/*.patch
```

检查点：0001 约 1225 行，0002 约 1145 行，0003 约 690 行。

**1.2 重新打补丁。** `setup_neurdb.sh` 要求 `NeuralDB/` 没有未提交的改动，所以先撤掉旧补丁的改动：

```bash
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh
```

检查点：最后一行是 `NeuralDB/ is at ... plus the prototype patches`。

**1.3 重新编译 SELIX 扩展。** 必须先停服务，在服务运行时替换扩展文件会让数据库进程崩溃：

```bash
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
```

只有 nr_am 这一步会重做，其余步骤显示 `skip`。检查点：输出以 `install finished` 结尾。

**1.4 刷新 NQO 服务并启动全部服务：**

```bash
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh
```

检查点：最后一行是 `NQO /stats: ...`，其中有 `"cache_hits": 0`。NQO 的决策缓存默认开启。

---

## 2. 检查安装

```bash
docker exec neurdb-ga bash /neuragent/deploy/check_deploy.sh
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test
docker exec neurdb-ga su neurdb -c "bash /neuragent/prototype/r2/e1e2_test.sh"
```

| 命令 | 检查点 |
|---|---|
| `check_deploy.sh` | `26 passed, 0 failed` |
| `pipeline.sh test` | `Ran 56 tests` 与 `OK` |
| `e1e2_test.sh` | `26 passed, 0 failed`。这是 SELIX 一侧改动的正确性测试，在 title 与 movie_keyword 的副本上做，约 5 分钟，结束后自动删除副本 |

任何一项有 FAIL，先停下来，把输出发给我。

---

## 3. 确认查询集

F1 到 F4 用第二轮划分好的两个查询集。长查询集的下限必须是 10 秒：

```bash
python3 -c "import json; print(json.load(open('experiment/queries/job_long/latencies.json')).get('long_min_s'))"
ls experiment/queries/job_long/*.sql | wc -l
ls experiment/queries/job_fast/*.sql | wc -l
```

- 第一行输出 `10.0`：查询集可用，跳到第 4 步。
- 输出 `None` 或其他数字：长查询集是按旧的下限划分的，重新划分（约 30 分钟）：

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh queries
```

检查点：最后两行各有一句 `... copied to queries/job_fast` 和 `... copied to queries/job_long`。长查询预计只有几条（26c、19d、16b 等），少于 8 条时会打印警告，这在预期之中。

---

## 4. 建 SELIX 索引

先看候选列。这一步同时在数据库里注册 F1 到 F4 要用的函数 `nrindex_build_time()`：

```bash
E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"
$E selix_list
```

检查点：`30 columns chosen, 16 skipped`。候选是 JOB 的外键列与整数主键；不同值少于 1000 个的类型列（如 `info_type_id`）跳过，规划器不会对它们用索引。

建索引：

```bash
$E selix_create
```

每个索引在单独的连接里建，建完连接就关闭。btree 索引不动。cast_info 的三列各有 3600 万行，每列约 1 到 2 分钟，建的时候临时占用约 2 GB 内存。检查点：每列一行 `created nr_...`，最后一行 `done in ... s; btree indexes unchanged`。

---

## 5. F1：结果是否正确、SELIX 是否被用上

```bash
docker exec -e GROUP=long  neurdb-ga bash /neuragent/experiment/pipeline.sh f1
docker exec -e GROUP=short neurdb-ga bash /neuragent/experiment/pipeline.sh f1
```

做法：NQO 关闭，每条查询在“只用 btree”与“按代价选择”下各执行一次，比较结果，并从执行计划里统计用到了哪些 SELIX 索引。正式计时前有一遍不计入的预热，用来让连接装载好自己的 SELIX。

结果在 `experiment/runs/imdb_r2/f1_long.md` 与 `f1_short.md`：

```bash
tail -4 experiment/runs/imdb_r2/f1_long.md
tail -4 experiment/runs/imdb_r2/f1_short.md
```

- “结果不同”必须是 0 条。**不是 0 条时停下来**，把报告发给我。
- “计划用到 SELIX 的”条数，说明 SELIX 在多大范围内参与了查询。

然后去掉两组都没用到的 SELIX 索引，减少后面每个连接的内存：

```bash
$E selix_keep
```

检查点：`union of the groups' F1 lists: N indexes`，随后逐行列出 `keep` 与 `dropped`。

---

## 6. F2、F3、F4

**6.1 放到后台运行**（约 5 到 6 小时）：

```bash
nohup sh -c 'for c in f2 f3 f4; do for g in short long; do docker exec -e GROUP=$g neurdb-ga bash /neuragent/experiment/pipeline.sh $c; done; done; echo ALL_DONE' > feasibility.log 2>&1 &
```

**6.2 查看进度**（随时可以执行，不会影响运行）：

```bash
grep -E "^=====|\*\*F[2-4]|not passed|Error|Traceback|ALL_DONE" feasibility.log | tail -20
```

出现 `ALL_DONE` 就是全部完成。

**6.3 三项检查各做什么：**

| 检查 | 做法 | 轮数 | 报告 |
|---|---|---|---|
| F2 | NQO 关闭，“只用 btree”与“按代价选择”比较 | 3 | `f2_long.md`、`f2_short.md` |
| F3 | NQO 关闭，优先用 SELIX，三档密度比较；每换一次密度，连接重建 SELIX | 3 | `f3_long.md`、`f3_short.md` |
| F4 | NQO 三种模式 × 7 种索引设置，共 21 种组合 | 2 | `f4_long.md`、`f4_short.md` |

7 种索引设置是：只用 btree；按代价选择配 dense、mid、default 三档；优先 SELIX 配同样三档。

每条查询的延迟都给出两个值，含重建与不含重建。重建时间是在每条查询前后读 `nrindex_build_time()` 得到的差值。F4 判断两个组件是否相互影响时只用不含重建的值，因为重建只落在换密度后的第一条查询上；重建的总代价在 F4 报告最后单独列出。

**6.4 需要时的调整：**

| 需要 | 做法 |
|---|---|
| 先快速看一遍 | 加 `-e F_RUNS=1` |
| 只测几条查询 | 在命令末尾加 `--only 26c,19d` |
| 中途中断后重跑某一项 | 重新执行那一项即可，报告会覆盖，例如 `docker exec -e GROUP=long neurdb-ga bash /neuragent/experiment/pipeline.sh f4` |

---

## 7. 看结果

每份报告的最后一行是判定：

```bash
for f in experiment/runs/imdb_r2/f*_*.md; do echo "== $f"; tail -1 "$f"; done
```

| 检查 | 通过的条件 |
|---|---|
| F1 | 全部结果相同，且至少有查询用到 SELIX |
| F2 | 按代价选择的总延迟不比只用 btree 慢 10% 以上（含与不含重建都要满足） |
| F3 | 至少一条用到 SELIX 的查询在两档密度之间相差 10% 以上 |
| F4 | 至少一条查询，其中一个组件的最优选择随另一个组件的选择而改变（相差 10% 以上） |

F4 报告会逐条列出发生“翻转”的查询，例如“26c（btree 时最好是 hint，prefer/dense 时最好是 off）”。这些就是 GA 有东西可协调的地方。

| 结果 | 下一步 |
|---|---|
| 两组 F1 到 F4 都通过 | 进入分层环境与训练（方案第 8 节第 5 步），等训练方法确定 |
| F2 不通过 | SELIX 在这类查询上比 btree 慢；看报告里哪些查询变慢，再讨论是否收窄 SELIX 的列 |
| F3 不通过 | 密度对只读查询没有可见影响；SELIX 子 agent 的动作要换成别的，例如只保留“用不用 SELIX” |
| F4 不通过 | 两个组件在同一批表上也互不影响，分层协调没有对象；方案停下来讨论 |
| 只有一组通过 | 记为“有潜力”，按通过的那一组继续 |

把 `experiment/runs/imdb_r2/` 下的 8 份 `f*_*.md` 发给我即可；同名的 `.json` 里有每一次执行的原始数据，需要时再发。

---

## 8. 清理

验证全部结束、不再需要 SELIX 索引时：

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh selix_drop
```

---

## 9. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `setup_neurdb.sh` 报 `NeuralDB/ has uncommitted changes` | 1.2 的 `git checkout` 与 `git clean` 没有执行完；在 `NeuralDB/` 里执行后重试 |
| `check_deploy.sh` 的 S1 到 S4 失败 | nram 扩展没有重新编译或没有重启数据库；重做 1.3 与 1.4 |
| `nrindex_build_time() is missing` | 先执行一次 `$E selix_list` |
| `no SELIX index exists` | 先执行 `$E selix_create` |
| 建 cast_info 的索引时内存不足 | 建索引的连接临时占用约 2 GB；确认容器内存没有被 `set_memory.sh` 收得太紧（`docker exec neurdb-ga cat /sys/fs/cgroup/memory.max`） |
| F1 报告“结果不同”不为 0 | 停下来，把报告发给我；这说明 SELIX 返回的行与 btree 不一致 |
| NQO 模式的组合全部很慢 | 看 `docker exec neurdb-ga tail -50 /data/nqo.log`；确认服务在运行、决策缓存开启（`curl -s http://127.0.0.1:8666/stats` 里 `cache_hits` 会增长） |
| 某条长查询超时 | 语句超时是 300 秒；超时的执行记为 timeout，不计入最短延迟 |
| 想看某一次执行的细节 | 在对应的 `.json` 里按 `query` 与 `variant` 查找，包括用到的索引、NQO 是否改写、含与不含重建的时间 |

---

## 10. 在开发机上已经验证过的内容

- SELIX 一侧的正确性测试 26 项全部通过（`prototype/r2/e1e2_test.txt`）。
- 用 6 个小列的 SELIX 与 3 条短查询，把 `selix_list`、`selix_create`、F1 到 F4、`selix_keep`、`selix_drop` 全部跑通：F1 结果全部相同，三条查询的计划都用到 SELIX；F3 每次换密度都重建并记录了时间，索引内存随密度变化（135、143、162 MB）；F4 的 21 种组合全部执行成功。开发机磁盘慢、只跑 1 轮，数字只说明工具能用。
