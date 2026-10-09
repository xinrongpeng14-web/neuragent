# 操作教程：分层方案的可行性验证 F1 到 F4

适用于已经按 `TUTORIAL.md` 建好容器 `neurdb-ga`、装好 IMDB，并按 `TUTORIAL_round2.md` 划分过查询集的实验机。验证的内容与判定标准见 `GlobalAgent_hierarchical.md` 第 4 节。

全部 SQL 都是只读查询。两组查询分别验证：

| 组 | 查询集 | 来源 |
|---|---|---|
| long | `experiment/queries/job_long`（原生计划 10 到 120 秒） | `pipeline.sh queries`，`LONG_MIN=10` |
| short | `experiment/queries/job_fast`（1 秒以内） | 同上 |

预计用时（4 核容器）：更新与编译 30 分钟；建 SELIX 15 到 30 分钟；F1 到 F4 的长查询组约 4 到 5 小时，短查询组约 1 小时。

---

## 1. 更新代码、补丁与扩展

SELIX 一侧的改动在补丁 0001 里，补丁不在公开仓库。在实验机的 `neuragent` 目录里：

```bash
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
wc -l patches/neurdb/0001-*.patch                 # 约 1225 行
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh
```

重新编译 nram 扩展。**必须先停服务**，在服务运行时替换扩展文件会让数据库进程崩溃：

```bash
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh
```

`install_inside.sh` 只会重做 nr_am 这一步，其余步骤跳过。`update_nqo.sh` 刷新 NQO 服务并启动全部服务；NQO 的决策缓存现在默认开启。

检查：

```bash
docker exec neurdb-ga bash /neuragent/deploy/check_deploy.sh            # 26 项全部 PASS
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh test       # 单元测试 56 项
docker exec neurdb-ga su neurdb -c "bash /neuragent/prototype/r2/e1e2_test.sh"   # 26 项全部 PASS
```

最后一条是 SELIX 一侧改动的正确性测试，在 title 与 movie_keyword 的副本上做，约 5 分钟，结束时自动删除副本。

---

## 2. 建 SELIX 索引

先看候选列（这一步同时在数据库里注册 `nrindex_build_time()` 函数）：

```bash
E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"
$E selix_list
```

候选是 JOB 的外键列与整数主键，共约 30 列；不同值少于 1000 个的类型列（如 `info_type_id`）自动跳过。然后建索引：

```bash
$E selix_create
```

每个索引在单独的连接里建，建完连接就关闭，所以内存只在建索引期间占用。btree 索引不动。`cast_info` 的三列各有 3600 万行，每列约 1 到 2 分钟。

---

## 3. F1：结果是否正确、SELIX 是否被用上

```bash
GROUP=long  ; docker exec -e GROUP=$GROUP neurdb-ga bash /neuragent/experiment/pipeline.sh f1
GROUP=short ; docker exec -e GROUP=$GROUP neurdb-ga bash /neuragent/experiment/pipeline.sh f1
```

每条查询在“只用 btree”与“按代价选择”下各执行一次（NQO 关闭），比较结果，并从执行计划里统计用到了哪些 SELIX 索引。开始前有一遍不计入的预热，用来触发各连接的首次装载。

看 `experiment/runs/imdb_r2/f1_long.md`、`f1_short.md`：

- “结果不同”必须是 0 条。不是 0 条时**停下来**，把报告发给我。
- “计划用到 SELIX 的”条数，说明 SELIX 在多大范围内参与了查询。

然后去掉两组都没用到的 SELIX 索引，减少每个连接的内存：

```bash
$E selix_keep
```

---

## 4. F2、F3、F4

每一项两组各跑一遍。长查询组用时最长，建议放到后台：

```bash
nohup sh -c 'for c in f2 f3 f4; do for g in short long; do docker exec -e GROUP=$g neurdb-ga bash /neuragent/experiment/pipeline.sh $c; done; done' > feasibility.log 2>&1 &
grep -E "^=====|\*\*F[1-4]|not passed|Error|Traceback" feasibility.log | tail -20
```

| 检查 | 做什么 | 轮数 | 报告 |
|---|---|---|---|
| F2 | NQO 关闭，“只用 btree”与“按代价选择”比较 | 3 | `f2_<组>.md` |
| F3 | NQO 关闭，优先用 SELIX，三档密度比较；每次换密度时各连接重建 SELIX | 3 | `f3_<组>.md` |
| F4 | NQO 三种模式 × 7 种索引设置（只用 btree，以及按代价选择、优先 SELIX 各配三档密度），共 21 种组合 | 2 | `f4_<组>.md` |

每个延迟都给出两个值：含重建与不含重建。重建时间是用 `nrindex_build_time()` 在每条查询前后读出的差值。F4 的耦合以不含重建的延迟判定，因为重建只在密度改变后的第一条查询上发生，逐条比较含重建的值主要反映的是执行顺序；重建的代价在 F4 报告最后单独列出。

轮数可以用 `F_RUNS` 改，例如 `-e F_RUNS=1` 先快速看一遍。只测几条查询：在命令末尾加 `--only 26c,19d`。

每份报告最后一行是判定：

| 检查 | 通过的条件 |
|---|---|
| F1 | 全部结果相同，且至少有查询用到 SELIX |
| F2 | 按代价选择的总延迟不比只用 btree 慢 10% 以上（含与不含重建都要满足） |
| F3 | 至少一条用到 SELIX 的查询在两档密度之间相差 10% 以上 |
| F4 | 至少一条查询，其中一个组件的最优选择随另一个组件的选择而改变（相差 10% 以上） |

F4 报告里会逐条列出发生“翻转”的查询，例如“26c（btree 时最好是 hint，prefer/dense 时最好是 off）”。这些就是 GA 有东西可协调的地方。

---

## 5. 结果怎么用

| 结果 | 下一步 |
|---|---|
| 两组 F1 到 F4 都通过 | 进入分层环境与训练（方案第 8 节第 5 步），等训练方法确定 |
| F2 不通过 | SELIX 在这类查询上比 btree 慢；看报告里哪些查询变慢，再讨论是否收窄 SELIX 的列 |
| F3 不通过 | 密度对只读查询没有可见影响；SELIX 子 agent 的动作空间要换成别的（例如只保留“用不用 SELIX”） |
| F4 不通过 | 两个组件在同一批表上也互不影响，分层协调没有对象；方案停下来讨论 |
| 只有一组通过 | 记为“有潜力”，按通过的那一组继续 |

把 `experiment/runs/imdb_r2/f*_*.md` 发给我即可，`.json` 里有每一次执行的原始数据。

---

## 6. 清理与常见问题

验证全部结束、不再需要 SELIX 时：`$E selix_drop`。

| 现象 | 处理 |
|---|---|
| `nrindex_build_time() is missing` | 先执行一次 `$E selix_list` |
| `no SELIX index exists` | 先执行 `$E selix_create` |
| 建 cast_info 的索引时内存不足 | 建索引的连接会临时占用约 2 GB；确认容器内存没有被 `set_memory.sh` 收得太紧 |
| F 检查中途中断 | 重新执行同一个检查即可，报告会覆盖 |
| NQO 模式的组合全部很慢 | 看 `/data/nqo.log`；确认服务在运行、决策缓存开启（`/stats` 里 `cache_hits` 会增长） |
| 某条长查询超时 | 语句超时是 300 秒；超时的执行记为 timeout，不计入最短延迟 |

---

## 7. 在开发机上已经验证过的内容

- SELIX 一侧的正确性测试 26 项全部通过（`prototype/r2/e1e2_test.txt`）。
- 用 6 个小列的 SELIX 与 3 条短查询，把 `selix_list`、`selix_create`、F1 到 F4、`selix_keep`、`selix_drop` 全部跑通：F1 结果全部相同，三条查询的计划都用到 SELIX；F3 每次换密度都重建并记录了时间，索引内存随密度变化（135、143、162 MB）；F4 的 21 种组合全部执行成功。开发机磁盘慢、只跑 1 轮，数字只说明工具能用。
