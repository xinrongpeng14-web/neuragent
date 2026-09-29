# P1a 到 P9 前置改动报告

- 对应文档：`GlobalAgent_prototype.md` 第 3 节
- 日期：2026-09-29
- 代码位置：`/home/zhanhao/neuragent/NeuralDB`，分支 `ga-prototype`，**改动尚未提交**
- 结论先行：**九项改动全部完成并通过测试。有一项重要的未验证内容：真实的 NQO 模型没有在本机端到端运行过。**

---

## 1. 结论

| 项 | 状态 | 验证方式 |
|---|---|---|
| P1a 键编码补丁 | 完成 | P1 回归：4000 次点查 0 错误，10 万行插入正常 |
| P2 悬垂指针 | 完成 | 连续 300 条查询无崩溃；提示格式生效 |
| P3 SET 提示语句级生效 | 完成 | 语句结束后参数已复位，下一条查询不受影响 |
| P4 超时与防递归 | 完成 | 超时 1 秒时 1.04 秒回退；回连带启动参数时无递归 |
| P5 专家过滤 | 完成 | 三档取值随请求到达服务端；hint、join 两档跳过路由器 |
| P6 冻结与统计接口 | 完成 | 冻结后 50 次推理 0 次重训；`/stats` 计数准确 |
| P7 多进程部署 | 完成 | 3 个进程并行处理；被杀的进程自动补齐 |
| P8 `-O2` 与密度参数 | 完成 | 三档参数在 PG 内产生不同的内存占用；非法组合被拒绝 |
| P9 采样计时与统计函数 | 完成 | 计数器精确；采样率正好 1/8 |

测试总数：

| 测试组 | 通过 / 总数 | 运行位置 |
|---|---|---|
| SELIX 功能测试 T1 到 T5 | 全部通过 | 容器内 NeurDB |
| nr_molqo 功能测试 C1 到 C11 | 17 / 17 | 容器内 NeurDB |
| C 侧与服务端联调 E1 到 E3 | 12 / 12 | 容器内 NeurDB |
| Python 单元测试 | 21 / 21 | 主机，重依赖用桩模块替代 |
| P1 回归 | 通过 | 容器内 NeurDB |

---

## 2. 重要的未验证内容

**真实的 NQO 模型没有端到端运行过。** 运行它需要 IMDB 数据库、PyTorch 环境和比本机更多的内存。当前的验证覆盖范围是：

| 已验证 | 未验证 |
|---|---|
| C 侧 nr_molqo 的全部新逻辑，用测试服务端驱动 | 真实路由器与两个专家在新代码下的推理 |
| 服务端 `run.py` 的多进程、统计、过滤透传，用假控制器驱动 | 多个进程各自加载 PyTorch 模型时的内存占用与启动时间 |
| `moqoe.py` 的过滤与冻结逻辑，用桩模块替代重依赖 | 冻结状态下编码缓存只读是否影响推理结果 |
| 真实专家的**输出格式**与 C 侧解析器匹配：格式取自源码，联调中按该格式生成 | 真实专家回连数据库时启动参数是否在所有连接路径上生效 |

迁移到新机器后，第一件事应当是用真实模型跑一遍 `prototype/p2_p9/it_test.sh` 中 E1 的三条查询。

---

## 3. 各项改动说明

### 3.1 SELIX 一侧（P1a、P8、P9）

| 文件 | 改动 |
|---|---|
| `nr_am/src/nram_storage/indexengine.cpp` | 去掉键的符号位翻转；`put` 与 `get` 内加入计数与采样计时；新增设置密度、设置采样率、读取统计三个方法 |
| `nr_am/src/nram_storage/indexengine.h` | 新增 `IndexEngineStats` 结构与三个 C 接口 |
| `nr_am/src/nrindex_access/nrindex_kv.c` | 定义四个 GUC；参数延迟应用；新增 SQL 函数 `nrindex_stats()` |
| `nr_am/src/nram.c` | `_PG_init` 中注册 GUC |
| `nr_am/sql/nram--1.0.sql` | 声明 `nrindex_stats()` |
| `nr_am/Makefile` | C++ 编译选项加入 `-O2` |

SELIX 子模块本身没有改动。

**新增 GUC：**

| 名称 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `selix.init_density` | 0.70 | 0.01 – 1 | 分裂与装载时新节点的密度 |
| `selix.max_density` | 0.80 | 0.01 – 1 | 触发扩容的密度 |
| `selix.min_density` | 0.60 | 0.01 – 1 | 扩容后的密度 |
| `selix.timing_sample_every` | 8 | 0 – 1000000 | 每多少次操作计时一次，0 表示不计时 |

**`nrindex_stats()` 返回一行，全部为累计值：**

| 字段 | 含义 |
|---|---|
| `n_get`、`n_put` | 点查与插入次数 |
| `t_get_ns`、`c_get` | 被采样点查的耗时之和与次数 |
| `t_put_ns`、`c_put` | 被采样插入的耗时之和与次数，含结构调整 |
| `mem_bytes` | 索引内存 |
| `n_smo` | 扩容与分裂总次数 |
| `n_keys`、`n_indexes` | 键数与本后端持有的索引数 |
| `init_density`、`max_density`、`min_density` | 当前实际生效的密度 |

**测试结果：**

| 测试 | 结果 |
|---|---|
| T1 计数器 | 4 万次操作后 `n_get + n_put` 的增量正好 40000；采样率 0.1250；`n_keys` 的增量等于插入次数 |
| T2 采样开关 | 设为 0 时 8000 次操作采样 0 次；设为 1 时采样 8000 次 |
| T3 非法组合 | `init=0.9`、`max=0.8` 时给出告警并保持原值；补齐另两个参数后生效；`max=1.5` 被范围检查拒绝 |
| T4 正确性 | 切换参数并插入后，6000 次点查 0 丢失 |
| T5 三档对比 | 见下表 |

T5 在相同负载下比较三档，各自使用独立会话：30 万键装载后执行 15 万次操作，读比例 30%。

| 档 | 密度 | 索引内存 | 结构调整次数 | 单次耗时 |
|---|---|---|---|---|
| dense | 0.85 / 0.95 / 0.75 | 11.5 MB | 91 | 1043 纳秒 |
| default | 0.70 / 0.80 / 0.60 | 13.2 MB | 92 | 800 纳秒 |
| sparse | 0.50 / 0.60 / 0.40 | 15.5 MB | 92 | 892 纳秒 |

内存随密度单调变化，说明参数在 PG 内确实生效。耗时只测了一次，本机噪声大，不能据此判断快慢。

### 3.2 NQO 的 C 侧（P2、P3、P4、P5）

`nr_molqo/src/nr_molqo.c` 基本重写，`nr_molqo/sql/nr_molqo--1.0.sql` 的状态函数补充了新参数。

**新增 GUC：**

| 名称 | 默认 | 说明 |
|---|---|---|
| `molqo.expert_filter` | all | 取值 all、hint、join，随每个请求发给服务端 |
| `molqo.timeout_ms` | 10000 | 单次请求的时间上限，超时后走原生优化器 |
| `molqo.report_decisions` | on | 是否把每次决策以 INFO 消息发给客户端。实验中应设为 off |

**原版与新版的行为对照**，全部为容器内实测：

| 场景 | 原版 | 新版 |
|---|---|---|
| SET 格式：当条查询的计划 | Nested Loop，生效 | Nested Loop，生效 |
| SET 格式：语句结束后 `enable_hashjoin` | **off，泄漏到整个会话** | on，已复位 |
| 提示格式，标准加载顺序 | **Hash Join，未生效** | Merge Join，生效 |
| 提示格式，标准顺序且开启 `compute_query_id` | **未生效** | 生效 |
| 提示格式，反向加载顺序且开启 `compute_query_id` | **未生效** | 生效 |
| 提示格式，反向加载顺序，默认配置 | 未测 | **未生效**，见 5.1 节 |
| 服务端 3 秒才应答 | 等待 3 秒 | 超时设为 1 秒时 1.04 秒回退 |

原版的提示格式在测试过的所有配置下都没有生效。

**C1 到 C11 的测试内容：**

| 编号 | 内容 | 结果 |
|---|---|---|
| C1 | 关闭 molqo 时为基线计划 | 通过 |
| C2a–d | SET 格式生效；两个开关在语句后复位；同会话下一条查询回到基线 | 通过 |
| C3 | 提示格式生效 | 通过 |
| C4a–c | 超时后回退、给出告警、总耗时小于 2.5 秒 | 通过 |
| C5、C5b | 三档过滤值到达服务端；非法取值被拒绝 | 通过 |
| C6 | 含双引号、单引号、反斜杠、换行、制表符的 SQL 往返一致 | 通过 |
| C7 | 20KB 的长 SQL 往返一致 | 通过 |
| C8 | 查询体内含 `OFFSET` 时不被误认为 SET 语句 | 通过 |
| C9 | 服务端返回未知参数时只告警，其余设置照常生效 | 通过 |
| C10 | 查询执行报错后设置仍已复位 | 通过 |
| C11 | 两种格式交替的 300 条查询后进程存活、设置为默认 | 通过 |

### 3.3 NQO 的 Python 侧（P4、P5、P6、P7）

| 文件 | 改动 |
|---|---|
| `neurqo_frame/src/db/pg_conn.py` | 连接时带启动参数 `-c enable_molqo=off`；服务端没有该参数时退回普通连接；其他连接错误照常抛出 |
| `neurqo_frame/src/moqoe.py` | `inference` 新增 `expert_filter` 参数，默认 all；hint、join 两档直接使用对应专家，不运行路由器；构造函数新增 `enable_online_training`，默认 True |
| `neurqo_frame/src/expert_router/encoder.py` | 环境变量 `MOQOE_READONLY=1` 时不再重写共享的编码缓存文件；补上 `import os` |
| `neurqo_frame/run.py` | 重写：命令行参数、预派生多进程、跨进程共享计数器、`/stats` 与 `/health` 接口 |
| `neurqo_frame/run_moqoe_prototype.sh` | 新增启动脚本，无硬编码路径 |

**`run.py` 的命令行：**

| 参数 | 默认 | 说明 |
|---|---|---|
| `--workers` | 1 | 工作进程数。大于 1 时必须同时指定 `--freeze` |
| `--freeze` | 关 | 冻结模型：不在线重训，不写编码缓存 |
| `--port` | 8666 | |
| `--database` | imdb_ori | |
| `--torch-threads` | 1 | 每个进程的 PyTorch 线程数，避免多进程时线程数超过核数 |
| `--quiet` | 关 | 不打印每个请求 |

不带任何参数运行时，行为与原版一致：单进程、在线重训开启。

**`GET /stats` 返回所有进程的累计值：** `requests`、`optimized`、`fallbacks`、`errors`、`opt_time_ms`、两个专家各自的计数、三档过滤各自的计数、`workers`、`uptime_s`。

**多进程的实现方式：** 父进程打开监听端口后派生工作进程，每个进程在派生之后才加载模型和建立数据库连接，所有进程从同一个端口接收请求。计数器放在共享内存中，任何一个进程都能回答 `/stats`。

### 3.4 端到端联调

用新的 C 侧连接新的 `run.py`。控制器是假的，但输出格式与真实专家一致：hint 档返回 9 条 SET 语句，先全部关闭再打开其中 3 个；join 档返回提示注释。

| 编号 | 内容 | 结果 |
|---|---|---|
| E1a–b | hint 档：9 条 SET 生效；语句后开关复位 | 通过 |
| E1c–d | join 档与 all 档：提示格式生效 | 通过 |
| E1e–h | `/stats` 的请求数、各档计数、各专家计数、进程数 | 通过 |
| E2a–b | 服务端默认开启 molqo 时，带启动参数的连接在 `DISCARD ALL` 与 `RESET ALL` 后仍为关闭 | 通过 |
| E3a | 专家回连数据库时带启动参数：0.1 秒完成，服务端只收到 1 个请求 | 通过 |
| E3b | 专家回连时不带参数：发生递归，3 秒后靠超时回退，服务端收到 4 个请求 | 通过，证实了递归问题存在 |

---

## 4. 与文档描述不同的地方

以下四处的实现与 `GlobalAgent_prototype.md` v0.2 的字面描述不同，原因都来自源码或实测。

### 4.1 P3：设置只在规划期间生效

文档写的是"在 ExecutorEnd 复位"。实际改为规划开始前设置、规划结束后立即复位。

原因：NQO 下发的全是 `enable_*` 这类规划器开关，只在规划期间起作用。把作用范围限制在规划阶段，即使查询没有走到执行阶段也不会泄漏。

代价：如果服务端将来下发 `work_mem` 这类执行期参数，它只会影响规划时的代价估算，不影响执行。

### 4.2 P2：除了修悬垂指针，还改了提示的传递方式

只修悬垂指针不够，因为原版的提示格式本来就不生效。PG16 版的 pg_hint_plan 在默认配置下从传给规划器的查询串读取提示，而不是从解析阶段的文本读取。新代码在规划钩子里把带提示的 SQL 传给下游，同时保留对解析阶段文本的改写，以覆盖开启 `compute_query_id` 的情形。

### 4.3 P8：三个密度参数延迟生效

SELIX 要求 min < init < max，违反时触发断言使进程崩溃。三个参数逐个设置时，中间状态可能违反约束。新代码在 SET 时只做标记，到下一次索引写入、建索引或调用 `nrindex_stats()` 时一次性校验并应用。校验失败时给出告警并保留原值。

对驱动程序的要求：三条 SET 要连续发送，中间不要夹索引操作。

### 4.4 超出清单的改动

| 改动 | 原因 |
|---|---|
| GUC `molqo.report_decisions` | 原版对每条查询向客户端发 4 到 5 条 INFO 消息，会干扰基准测试 |
| GUC `selix.timing_sample_every` | 对应风险表中"提高采样率"的应对措施 |
| 响应缓冲从固定 8KB 改为动态增长 | 响应里同时带原 SQL 与优化后 SQL，长查询会被截断 |
| JSON 解析支持转义字符，且只匹配顶层键 | 原版遇到 SQL 里的双引号会截断，也不还原 `\n` |
| 只解析开头的 SET 语句 | 原版在整个字符串里找 `SET `，查询体内的 `OFFSET` 会被误认 |
| 临时文件改用 `mkstemp` | 原版文件名可预测 |
| 去掉吞掉所有错误的 `PG_CATCH` | 不回滚事务就清除错误状态是不安全的做法。新代码中相关路径只发告警，不抛错误 |

---

## 5. 已知限制

### 5.1 加载顺序

提示格式要求 pg_hint_plan 在 nr_molqo **之前**加载：

```
shared_preload_libraries = 'pg_hint_plan, nr_molqo, nram'
```

这也是 NeurDB 的标准配置。顺序颠倒时，提示格式只有在开启 `compute_query_id` 后才生效。SET 格式不受加载顺序影响。

### 5.2 其他限制

| 限制 | 说明 |
|---|---|
| 预备语句 | 优化只在第一次规划时应用。无参数的预备语句会缓存该计划，后续执行沿用；带参数且每次重新规划的语句，第二次起不再有优化。JOB 客户端应使用简单查询协议 |
| EXPLAIN | `EXPLAIN SELECT ...` 同样会触发优化请求 |
| pg_stat_statements | 同时加载时，带提示的查询的归一化文本可能不准确，因为解析阶段的文本被加了前缀 |
| 密度参数的作用范围 | 在进程内对所有 SELIX 索引生效。雏形中一个连接只有一个索引，不受影响 |
| 范围查询 | `BETWEEN` 现在报错 `invalid typLen: 0`，此前是静默返回空。原因是原代码把扫描键个数当作索引列数，越界读取了列描述。该文件未改动，症状变化来自内存布局不同。范围查询本来就不在支持范围内 |
| `pg_conn.py` 的全局副作用 | 每次连接仍会执行 `ALTER SYSTEM SET autovacuum TO off`。未改动，对两个对照组的影响相同 |

### 5.3 测试过程中的一个假象

服务运行时直接覆盖 `nr_molqo.so`，会使已经映射该文件的进程段错误。日志里的 9 次段错误都来自这个操作，时间点一一对应。按"停服务、换文件、启动"的顺序重跑后，17 项测试通过，新增日志中没有任何异常终止。

**安装新版扩展前必须先停数据库。**

---

## 6. 仓库状态

```
分支:     ga-prototype（由 main 创建）
已修改:   13 个文件，+1370 行，−424 行
新增:     aiengine/neurqo_frame/run_moqoe_prototype.sh
未提交
selix 子模块: 未改动
```

查看与提交：

```bash
cd /home/zhanhao/neuragent/NeuralDB
git diff --stat
git add -A && git commit -m "..."      # 需要时再提交
git checkout main                       # 回到原版；未提交的改动会跟随，需先提交或 stash
```

---

## 7. 复现方法

目录 `/home/zhanhao/neuragent/prototype/p2_p9/`：

| 文件 | 内容 |
|---|---|
| `P2_P9_report.md` | 本报告 |
| `selix_setup.sql`、`selix_t1_stats.sql`、`selix_t5_preset.sql` | SELIX 功能测试 |
| `molqo_test_server.py` | nr_molqo 的测试服务端 |
| `molqo_test.sh` | nr_molqo 功能测试 C1 到 C13 |
| `molqo_orig_check.sh` | 原版与新版的行为对照 |
| `it_test.sh` | 端到端联调 E1 到 E3 |
| `pytests/` | Python 单元测试与假控制器 |
| `results_*.txt` | 各次运行的原始输出 |

容器 `neurdb-p1` 保留，数据库服务已停止。在容器内重新编译并测试：

```bash
# 1. 启动前先确认数据库已停止，再编译安装
docker exec neurdb-p1 bash -c '
  for m in nr_am nr_molqo; do
    rm -rf /build/$m && cp -r /src/dbengine/nr_kernel/$m /build/$m &&
    cd /build/$m && make PG_CONFIG=/opt/neurdb/bin/pg_config &&
    make PG_CONFIG=/opt/neurdb/bin/pg_config install
  done'

# 2. 启动数据库与测试服务端，运行测试
docker exec neurdb-p1 su neurdb -c '
  /opt/neurdb/bin/pg_ctl -D /data/pg -l /data/logfile -w start
  (nohup python3 /data/t/molqo_test_server.py 8080 /data/t/molqo_requests.log > /dev/null 2>&1 &)
  sleep 1; bash /data/t/molqo_test.sh rerun; bash /data/t/it_test.sh'

# 3. Python 单元测试在主机上运行
cd /home/zhanhao/neuragent/prototype/p2_p9/pytests
for t in test_moqoe_logic test_pg_conn test_run_server; do python3 $t.py; done
```

容器内已额外构建了 pg_hint_plan 的 PG16 分支和 auto_explain，测试依赖它们。
