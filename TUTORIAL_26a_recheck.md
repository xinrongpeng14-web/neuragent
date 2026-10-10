# 操作教程：在实验机上重测 26a（SELIX 修复 E7 之后）

## 为什么要重测

F4 里 26a 在"NQO 只用 HintPlanSel + SELIX"的 6 格（hint/cost 与 hint/prefer 各 3 档密度）测得约 0.63 秒，但结果哈希与其他配置不同，返回的结果是错的。原因是 SELIX 的缺陷 E7：嵌套循环内层走 SELIX 位图扫描时，连接键为 NULL 会返回上一个键的行（26a 返回 5437 行，正确是 1728 行）。开发机上已修复（补丁 0001 中的 `nrindex.c`，提交 `4198234` 加了回归测试 E7），这 6 格的数据作废。

本教程在实验机上：更新补丁并重新编译 SELIX 扩展 → 确认修复生效 → 只对 26a 重跑一遍完整的 F4（21 种组合 × 2 次），**结果写到新文件，不覆盖原来的 `f4_long.*`**。

**前提**：实验机上的 `neurdb-ga` 容器、`imdb_ori` 数据库和 F1 到 F4 时建的 SELIX 索引都还在（没有执行过 `selix_drop`）。下面所有命令都在实验机的 `neuragent` 目录里执行。

**总用时**：约 40 分钟到 1 小时（不含可选的完整回归测试）。

---

## 一页命令清单

```bash
# 1 更新代码与补丁
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
wc -l patches/neurdb/0001-*.patch                       # 1337 行
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh

# 2 先停服务，再重新编译 SELIX 扩展，然后启动服务
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh

# 3 确认修复生效（1 分钟）
docker exec -u neurdb neurdb-ga bash /neuragent/prototype/r2/e7_quick_check.sh

# 4 确认 SELIX 索引还在
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh selix_list

# 5 只对 26a 重跑 F4，结果写到 recheck_26a_f4_long.*
docker exec -e GROUP=long -e RUN_PREFIX=recheck_26a_ neurdb-ga bash /neuragent/experiment/pipeline.sh f4 --only 26a

# 6 看结果
python3 prototype/r2/recheck_26a_view.py experiment/runs/imdb_r2/recheck_26a_f4_long.json
```

---

## 1. 更新代码与补丁

```bash
git pull
scp "zhanhao@34.31.210.7:/home/zhanhao/neuragent/patches/neurdb/*.patch" patches/neurdb/
wc -l patches/neurdb/*.patch
```

检查点：0001 为 **1337 行**（修复前是 1303 行左右），0002 约 1145 行，0003 约 693 行。`git pull` 之后 `git log --oneline -1` 应不早于 `4198234`。

重新打补丁。`setup_neurdb.sh` 要求 `NeuralDB/` 没有未提交的改动，所以先撤掉旧补丁：

```bash
cd NeuralDB && git checkout -- . && git clean -fdq aiengine dbengine && cd ..
scripts/setup_neurdb.sh
```

检查点：最后一行是 `NeuralDB/ is at ... plus the prototype patches`。再确认修复已在源码里：

```bash
grep -c "a NULL key matches no row" NeuralDB/dbengine/nr_kernel/nr_am/src/nrindex.c
```

检查点：输出 `1`。

---

## 2. 重新编译 SELIX 扩展

**必须先停服务。** 数据库进程会一直使用启动时加载的旧扩展；不重启，新代码不会生效（开发机上就遇到过：装完新扩展后旧进程仍在跑旧代码，问题照旧）。

```bash
docker exec neurdb-ga bash /neuragent/deploy/stop_services.sh
docker exec neurdb-ga bash -c 'rm -f /opt/.ga_stamps/nr_am && bash /neuragent/deploy/install_inside.sh'
docker exec neurdb-ga bash /neuragent/deploy/update_nqo.sh
```

| 命令 | 检查点 |
|---|---|
| `stop_services.sh` | `NQO service stopped` 与 `database stopped` |
| `install_inside.sh` | 只有 nr_am 一步重做，其余显示 `skip`；最后是 `install finished` |
| `update_nqo.sh` | 最后一行是 `NQO /stats: ...`，数据库与 NQO 服务都已启动 |

确认数据库确实是新启动的（启动时间应是刚才）：

```bash
docker exec -u neurdb neurdb-ga /opt/neurdb/bin/psql -X -At -d imdb_ori -c "SELECT pg_postmaster_start_time()"
```

---

## 3. 确认修复生效

```bash
docker exec -u neurdb neurdb-ga bash /neuragent/prototype/r2/e7_quick_check.sh
```

脚本用 cast_info 与 char_name 做一个连接：内层强制走 SELIX 的位图扫描，cast_info.person_role_id 中有大量 NULL。分别在只用 btree 与优先 SELIX 下执行，比较结果。

检查点：

```
plan uses: Bitmap Index Scan on nr_char_name_id
btree: 18993|0
selix: 18993|0
PASS  E7 fixed: SELIX returns the same rows as btree and no row for a NULL key
```

两行的第一个数相同、第二个数为 0 即通过。若 selix 一行的第二个数大于 0（修复前是 `48624|29631`），说明数据库还在用旧扩展：回到第 2 步，确认先停了服务。

**可选：完整回归测试**（约 20 分钟，37 项）：

```bash
docker exec neurdb-ga su neurdb -c "bash /neuragent/prototype/r2/e1e2_test.sh"
```

检查点：`37 passed, 0 failed`。

---

## 4. 确认 SELIX 索引还在

```bash
docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh selix_list
```

检查点：已存在的索引里有 F4 用过的 17 个 `nr_` 索引，其中 26a 会用到的是：`nr_cast_info_movie_id`、`nr_char_name_id`、`nr_complete_cast_movie_id`、`nr_keyword_id`、`nr_movie_info_idx_movie_id`、`nr_movie_keyword_keyword_id`、`nr_movie_keyword_movie_id`、`nr_name_id`、`nr_title_id`。

如果索引已被删除，重新建并只保留 F1 用到的：

```bash
E="docker exec neurdb-ga bash /neuragent/experiment/pipeline.sh"
$E selix_create
$E selix_keep
```

（约 20 到 30 分钟；cast_info 的列每列 1 到 2 分钟。）

SELIX 是每个连接在第一次使用时自己从表里装载的，所以重启数据库后不需要重建索引，F4 开始时的预热会触发装载。

---

## 5. 只对 26a 重跑 F4

```bash
docker exec -e GROUP=long -e RUN_PREFIX=recheck_26a_ neurdb-ga bash /neuragent/experiment/pipeline.sh f4 --only 26a
```

- 与原 F4 完全相同：NQO 三种模式（关、只用 HintPlanSel、自动）× 7 种索引设置（只用 btree；按代价配 dense、mid、default；优先 SELIX 配同样三档），每格 2 次。
- `--only 26a` 只测 26a；`RUN_PREFIX=recheck_26a_` 让结果写到 `experiment/runs/imdb_r2/recheck_26a_f4_long.md` 与 `.json`，**原来的 `f4_long.md`、`f4_long.json` 不会被覆盖**。
- 用时约 20 到 30 分钟。最慢的是只用 btree 且不开 hint 的几格（每次 25 到 40 秒），以及每换一档密度时 SELIX 的重建（每次约 50 秒）。

开头一行应是 `f4 on the long group: 1 queries, 21 variants, 2 run(s)`。结束时最后两行是 F4 的判定（只有一条查询，判定本身不重要）和 `written: ...recheck_26a_f4_long.md, ...json`。

想放到后台运行：

```bash
nohup docker exec -e GROUP=long -e RUN_PREFIX=recheck_26a_ neurdb-ga bash /neuragent/experiment/pipeline.sh f4 --only 26a > recheck_26a.log 2>&1 &
tail -3 recheck_26a.log
```

---

## 6. 看结果

```bash
python3 prototype/r2/recheck_26a_view.py experiment/runs/imdb_r2/recheck_26a_f4_long.json
```

脚本逐格列出两次执行的延迟（不含重建），以及结果是否与"NQO 关 + 只用 btree"相同。

检查点：

| 项 | 期望 |
|---|---|
| 最后一行 | `all 42 executions return the same result as off/btree` |
| hint/cost/* 与 hint/prefer/* 这 6 格 | 结果标记为 `same`（修复前是 `DIFF`）；延迟是这次要拿到的新数据 |
| 其他 15 格 | 延迟与原 F4 相近（例如 auto/cost/dense 约 16 秒，hint/btree 约 0.7 秒） |

如果有任何一格标记为 `DIFF`，先停下来，把输出发给我。

**把这两个文件发给我**：`experiment/runs/imdb_r2/recheck_26a_f4_long.md` 与 `recheck_26a_f4_long.json`（用 scp 传回开发机的同一目录即可）。拿到后我会更新方案 4.1 节与汇报素材中 26a 的数据。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `setup_neurdb.sh` 报 `NeuralDB/ has uncommitted changes` | 第 1 步的 `git checkout` 与 `git clean` 没有执行完，在 `NeuralDB/` 里执行后重试 |
| 第 3 步 selix 一行第二个数大于 0 | 数据库仍在用旧扩展：执行 `stop_services.sh`，确认 `database stopped`，再执行 `update_nqo.sh` |
| 第 3 步报 `relation "nr_char_name_id" does not exist` | SELIX 索引已被删除，先做第 4 步重建，再回到第 3 步 |
| 第 5 步 NQO 相关的组合全部很慢或报错 | `docker exec neurdb-ga tail -50 /data/nqo.log`；确认 NQO 服务在运行（`curl -s http://127.0.0.1:8666/stats`） |
| 第 5 步中途中断 | 重新执行同一条命令即可，会覆盖 `recheck_26a_f4_long.*` |
