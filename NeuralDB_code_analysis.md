# NeurDB 代码分析报告

- 分析对象：`/home/zhanhao/neuragent/NeuralDB`，仓库 https://github.com/neurdb/neurdb ，分支 `main`，提交 `fad1bcbd`
- SELIX 子模块：https://github.com/neurdb/selix ，提交 `2569787`
- 分析日期：2026-09-28
- 范围：全局架构概览，重点分析查询优化模块 NQO（NeurQO / MoLQO）与存储模块 SELIX（含 nram 扩展）
- 说明：所有 `文件:行号` 均相对仓库根目录；标注「推断」「未确认」的结论未经实际运行验证

---

## 总体结论

NeurDB 是一个 PostgreSQL 16.3 的 fork，加上五个内核扩展和一个 Python AI 引擎。README 描述的 NeurQO、NeurIndex、NeurCC、NeurStore 等组件在代码中都能找到对应物，但成熟度差异很大。PREDICT 语法与 AI 引擎链路相对完整，而本次重点的两个模块都处于原型阶段。

- **NQO** 只以「改写 SQL 文本」的方式外挂在解析阶段，学习反馈环没有闭合。
- **SELIX** 本体是 ALEX 的衍生库，DRL 调参结果没有任何一条路径落到数据库运行时，且索引只存在于单个后端进程的内存里。

---

## 一、全局架构

### 1.1 三层结构

底层是打了补丁的 PG 内核，中间是 `dbengine/nr_kernel` 下的五个扩展，上层是 `aiengine` 目录中的 Python 服务。SELIX 作为 git 子模块挂在 nr_am 扩展下面，克隆时不会自动下载，需要 `git submodule update --init`。

| 组件 | 位置 | 代码量 | 职责 | README 对应名 |
|---|---|---|---|---|
| PG 内核补丁 | `dbengine/src` | 71 个文件有改动 | PREDICT 语法、CMD_PREDICT 子查询上拉、nr_* GUC、nr_aiengine 系统表 | NeurEngine |
| nr_ext | `dbengine/nr_kernel/nr_ext` | 约 1.2 万行 | PREDICT 的 planner_hook 与 Executor 钩子、Predict 执行节点 | NeurEngine |
| nr_pipeline | `dbengine/nr_kernel/nr_pipeline` | 约 0.8 万行 | 训练/推理数据管道，通过 WebSocket 把批数据推给 AI 引擎，支持多引擎分片 | NeurIDA 通道 |
| nr_molqo | `dbengine/nr_kernel/nr_molqo` | 853 行 | 学习型优化器的 PG 侧接入 | NeurQO |
| nram | `dbengine/nr_kernel/nr_am` | 约 0.9 万行 + SELIX 0.85 万行 | KV 表访问方法、nrindex 索引访问方法、OCC/2PL 混合并发控制 | NeurIndex、NeurCC |
| pg_neurstore | `dbengine/nr_kernel/nr_store` | 约 1.2 万行 | 模型张量压缩存储、HNSW 相似索引、ONNX 推理 | NeurStore |
| runtime | `aiengine/runtime` | 约 1.9 万行 | Quart WebSocket 服务，端口 8090，含 ARMNet、TabPFN、TRAILS、AutoPipeline 等模型 | NeurIDA |
| neurqo_frame | `aiengine/neurqo_frame` | 约 2.1 万行 | 学习型优化器 Python 服务，端口 8666 | NeurQO |
| nr_modelmanager | `aiengine/pgext/nr_modelmanager` | 约 0.2 万行 | 从 pg-model 迁移的模型管理扩展，依赖 libtorch | 未列出 |
| api/python | `api/python/neurdb` | 921 行 | 客户端库，模型序列化与存取 | Python Client |

### 1.2 进程与通信拓扑

```
psql ──► postgres backend
          ├─ nr_ext / nr_pipeline ──WebSocket:8090──► aiengine/runtime server.py  (PREDICT 训练/推理)
          ├─ nr_molqo ──popen(curl) HTTP:8666──► neurqo_frame run.py           (查询优化)
          ├─ nram ──共享内存环形缓冲──► RocksDB bgworker (同一 postmaster 下)   (表数据)
          └─ pg_neurstore ──ipc channel──► NeurStore 微服务                     (模型存储)
```

### 1.3 内核补丁要点

- PREDICT 语句在 `dbengine/src/backend/parser/gram.y:16953` 定义为 `NeurDBPredictStmt`，可嵌套在 FROM 子句中（`gram.y:13317`）。
- `dbengine/src/backend/optimizer/plan/planner.c:576-1112` 新增约 500 行，做 PREDICT 子查询的保守上拉与推迟（`neurdb_pull_up_predict_subqueries`）。
- `dbengine/src/backend/utils/misc/guc_tables.c` 新增 `nr_task_batch_size`、`nr_task_epoch`、`nr_task_num_batches`、`nr_predict_pushdown`、`nr_predict_pullup`、`nr_predict_startup_cost`、`nr_predict_tuple_cost`、`nr_model_name` 等参数。
- 新增系统表 `nr_aiengine`（`dbengine/src/include/catalog/nr_aiengine.h`）记录 AI 引擎地址，nr_pipeline 用它做多引擎调度（`nr_pipeline/src/interface2.c:43-70` 的 `engine_pin`）。
- 新增目录 `dbengine/src/backend/neurdb/guc.c`。

### 1.4 构建与启动

- 顶层 `Makefile`：编译 PG、外部克隆 pg_hint_plan（PG16 分支）、编译五个扩展、pip 安装 runtime。
- 启动时写入 `shared_preload_libraries = 'pg_hint_plan, nr_molqo, nr_ext, nram, pg_neurstore'`，再后台运行 `aiengine/runtime/server.py`。
- 依赖：RocksDB 10.3.0、libwebsockets、libcjson、libpqxx、libopencv。
- `docker-start.sh` / `docker-init.sh` 与 Makefile 流程一致。

### 1.5 空壳与遗留

- `aiengine/network` 只有 README，`doc/ai_dev.md` 和 `external/` 为空。
- nr_pipeline 保留了 1.0 版的 `nr_train`、`nr_inference` 函数和已废弃的 socketio 实现（`.deprecated` 后缀文件）。
- `dbengine/nr_kernel/nr_am/rocksdb_server` 是遗留的独立 RocksDB socket 服务二进制，未被使用。

---

## 二、重点：NQO 学习型查询优化器

### 2.1 核心思路

代码中的名字是 MoQOE，即 Mixture of Query Optimizer Experts（`aiengine/neurqo_frame/src/moqoe.py:103-110`）。它不改 PG 的 planner，而是把 SQL 文本发给外部 Python 服务。服务里一个 Transformer 路由网络从专家池中挑一个学习型优化器，专家产出改写后的 SQL，形式是前置 `SET enable_xxx` 语句或 `/*+ Leading(...) */` 注释。PG 端的 nr_molqo 把它落到会话 GUC 或 pg_hint_plan 上。

README 中的概念在代码中的对应：

- **query-state abstraction / States Retrieval**：Python 侧通过 psycopg2 反连 PG 执行 `EXPLAIN (FORMAT JSON)` 与选择率探测（`src/db/pg_conn.py:147-179, 223-269`），并不是从 PG 内核直接抽取状态。
- **MHSA-based Context Representation + Hybrid Expert Selection**：路由器 `src/expert_router/model.py:9-251`。
- **workload feedback**：`src/exp_buffer/` 的 SQLite 缓冲，`MoQOEController._online_train` 每 10 次选中触发专家重训（`src/moqoe.py:23, 264-272`）。

### 2.2 架构与调用链

```
psql ─SELECT─► parse_analyze ─► post_parse_analyze_hook          [nr_molqo/src/nr_molqo.c:102]
                                  │ 仅 CMD_SELECT，GUC enable_molqo 默认 off
                                  ▼ 写 /tmp/molqo_<pid>.json，popen("curl -X POST ...")  [nr_molqo.c:234-254]
                    neurqo_frame/run.py:26  HTTP :8666  POST /optimize
                                  ▼ MoQOEController.inference                              [src/moqoe.py:204]
                  ┌── Router: Sql2VecEmbeddingV2 编码 ─► QueryOptMHSASuperNode 打分
                  │       [src/expert_router/encoder.py:414, model.py:158, controller_offline.py:414]
                  ├── HintPlanSel 专家: 5 组 enable_* 组合 ─► 5 次 EXPLAIN ─► TreeCNN 预测延迟
                  │       [src/expert_pool/hint_plan_sel_expert/model.py:344]
                  └── JoinOrder 专家: MCTS 搜索 ─► Leading hint ─► TreeLSTM 预测
                          [src/expert_pool/join_order_expert/mcts_based_expert.py:458]
                                  ▼ JSON {"optimized_sql", "expert_name"}
              含 "/*+" ─► 覆盖 pstate->p_sourcetext，交给 pg_hint_plan                    [nr_molqo.c:162]
              否则     ─► 解析 "SET x TO y" 并 SetConfigOption(PGC_S_SESSION)             [nr_molqo.c:353-418]
                                  ▼ 标准 planner / executor，无任何反馈钩子
离线反馈环: buffer_mngr.run_log_query_exec (EXPLAIN ANALYZE) ─► SQLite plan_buffer ─► expert.train_and_save
```

### 2.3 Python 侧四个子模块（`aiengine/neurqo_frame/src`）

**路由器 `expert_router/`**

- 特征化 `encoder.py:401-525`：psqlparse 解析出 join 条件 `(t1,c1,t2,c2)`、filter 条件 `(t,c,selectivity)` 和 21 张表的大小（log 归一化）。选择率靠反连 PG 跑两次 EXPLAIN 估算。编码结果按 `query_id = base64(zlib(sql))` 缓存到 JSON 文件，每次新查询整文件重写。要求 WHERE 为至少 2 个 AND 谓词的 BoolExpr。
- 模型 `model.py:9-128, 158`：表列 `nn.Embedding(256)` 加 2 层 4 头 `TransformerEncoder`，双头输出。分类头判断「该专家是否够好」，回归头预测延迟。输出维度 5，对应 HintPlanSel、PlanGenSim、PlanGen、PostgreSQL、JoinOrder（`common/config_imdb.py:52`）。
- 选择策略 hypered_2（`controller_offline.py:489-510`）：`score = sigmoid(logit) - reg_time/timeout`，降序排序。损失 `loss.py:76-136` 为 focal-BCE 加归一化 MSE。
- 在线路由 `controller_online.py:19 OnlineRouter`（MC-Dropout Thompson 采样加漂移检测）是实验脚本，未接入服务。
- 权重 `models/router_models/router_model.pth`，8.2MB。

**专家池 `expert_pool/`**

- HintPlanSel（`hint_plan_sel_expert/model.py`）：Bao 的移植。5 个「臂」是不同的 `enable_{nestloop,hashjoin,mergejoin,seqscan,indexscan,indexonlyscan}` 组合（`:49-113`），整查询粒度。每臂 `DISCARD ALL` + SET + EXPLAIN，计划树经 `featurize.py:221-247` 变成 7 维算子 one-hot 加 3 个归一化数值的二叉树，`tree_cnn.py:10-39` 的 TreeCNN 预测延迟，取 argmin。重训 `train_and_save`（`:179-215`）按同 query_hash 组内回归次数决定是否提升 temp 模型，但被拒绝时内存中仍是新模型。
- JoinOrder（`join_order_expert/mcts_based_expert.py`）：HybridQO 的移植。`sql_to_vec.py:32-126` 生成 40×40 别名 join 矩阵加 100 维列选择率，列 id 按首次出现顺序分配，跨进程不稳定。TreeLSTM（`models/mcts_net.py:33`）加 UCT 搜索（`mcts.py:280-317`，固定 `random.seed(113)`），取 3 个候选，每个拼 `/*+Leading(a b)*/`，**只固定前 2 张表**（`:235, 569-574`）。门控条件：预测更快且 KNN 不确定性低于阈值且 PG 预测延迟大于 100ms。
- PlanGen（`plan_gen_expert/`，Balsa 移植，`plan_gen_expert.py` 2531 行）：控制器 `moqoe.py:149-180` 从未加载，且依赖不存在的 `common.workload` 模块，当前无法导入。

**经验缓冲 `exp_buffer/`**

- `sqllite.py:31 ExperienceBuffer`，单表 `plan_buffer(id, query_hash, query, actual_plan_json, actual_plan_hash, actual_latency, plan_time, hint_json, join_order_hint)`。
- `buffer_mngr.py:21 run_log_query_exec`：去重后应用 hint，跑 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 入库。
- 仓库自带 `models/buffer_imdb_ori.db`：107 条记录，33 个不同查询，73 条带 SET hint，1 条带 join_order_hint。

**数据库连接 `db/pg_conn.py`**

- psycopg2 autocommit 连接。连接时执行 `ALTER SYSTEM SET autovacuum TO off; SELECT pg_reload_conf()`（`:47-49`），这是一个全局副作用。
- explain 前统一关并行、设 geqo；`apply_hints` 逐条执行 SET（`:141-145`）。

### 2.4 C 侧 `nr_molqo.c` 详解

全部 446 行，位于 `dbengine/nr_kernel/nr_molqo/src/nr_molqo.c`。

- `_PG_init`（`:66-87`）定义两个 GUC：`enable_molqo`（bool，默认 off）、`molqo.server_url`（默认 `http://localhost:8080/optimize`，而 Python 服务监听 8666）。
- 唯一的钩子是 `post_parse_analyze_hook`（`:83-84`）。没有 planner_hook、set_join_pathlist_hook、get_relation_info_hook 或 Executor 钩子。
- `query_optimizer_post_parse_analyze`（`:102-182`）：先调前一个 hook，仅处理 `CMD_SELECT`，取 `pstate->p_sourcetext` 发送。
- HTTP 客户端 `send_http_request`（`:202-347`）：手工转义 SQL 成 JSON 写入 `/tmp/molqo_<pid>_<time>.json`，`popen("curl -s -X POST ... -d @file")`，无超时，响应缓冲 8192 字节，用 `strstr/strchr` 手工抽取 `optimized_sql` 与 `expert_name`。
- 落地方式（`:138-171`）：含 `/*+` 则覆盖 `pstate->p_sourcetext`；否则 `execute_optimized_sql`（`:353-418`）解析 `SET name TO value;` 并 `SetConfigOption(..., PGC_USERSET, PGC_S_SESSION)`。不构造 Path，不注入基数或代价。
- 所有错误被 `PG_TRY/PG_CATCH + FlushErrorState()` 吞掉，退回原计划。
- 仓库里有一份 libcurl 加 json-c 的 `http_client.c`，但 `CMakeLists.txt:27-30` 未编入。
- 内核补丁：`grep -ri molqo dbengine/src` 仅命中 `postgresql.conf.sample` 的注释示例。pg_hint_plan 源码不在仓库中。

### 2.5 范围限制

- 只处理 SELECT。
- 只支持 IMDB/JOB 风格的 SPJ 查询：表别名必须在写死的映射表内（`common/config_imdb.py:66-149`），至少 2 表，WHERE 至少 2 个 AND 谓词。
- HintPlanSel 只在整查询粒度切换算子族；JoinOrder 只固定前 2 张表的 Leading。
- 不做基数注入，不构造 Path，不选择单个算子。
- `config_stack.py` 存在但无对应专家和别名配置。

### 2.6 缺陷清单

**C 侧**

- **悬垂指针**：`nr_molqo.c:162` 把 `optimized_sql` 赋给 `pstate->p_sourcetext`，`:174` 随即 `pfree` 它，后续 pg_hint_plan 读到的是已释放内存。
- **递归风险**：Python 侧每次优化要跑 5 到 40 次 EXPLAIN，若其 PG 会话也开启了 `enable_molqo`，会回调单线程 HTTP 服务并死锁。`:118` 注释已承认 EXPLAIN 会递归触发。
- **会话污染**：SET 通过 `PGC_S_SESSION` 落地，语句结束后不复位，影响同会话后续所有查询。
- **hook 顺序**：先调前一个 hook 再改写源文本。按 README 的预载顺序 `pg_hint_plan,nr_molqo`，pg_hint_plan 的解析钩子会在改写之前执行。pg_hint_plan 是否在 planner 阶段重读源文本：未确认。
- **无超时的同步 popen**：每条 SELECT（含 psql 元命令、SPI、EXPLAIN 内部 SELECT）都会阻塞等待 curl。
- 8KB 响应缓冲，长 JOB 查询易截断；手工 JSON 解析遇 SQL 内的 `"` 会截断。

**Python 侧**

- 没有任何测试文件。
- **反馈环未闭合**：PG 端不回传实际延迟，缓冲只由离线脚本填充，「在线学习」只是对静态缓冲的定期重训。
- 路由器 5 类输出中 PostgreSQL、PlanGen、PlanGenSim 永远不可选（`moqoe.py:238-242` 只在已加载专家中取最高分）。
- `RoutingHelper.online_update` 是空占位（`moqoe.py:84-86`）。
- 专家重训被拒绝时内存中仍是新模型，无回滚（`hint_plan_sel_expert/model.py:187-215`）。
- README 给的两条训练命令实际只执行 `_predict`，`_train` 调用被注释（`model.py:512`、`mcts_based_expert.py:940`）。
- `router_offline_pretrain.py:20` 引用不存在的 `cfg.CONFIG`，训练 CSV 缺失，按 README 无法运行。
- 硬编码路径 `/Users/kevin/...`（`model.py:506`、`mcts_based_expert.py:777`、`plan_gen_expert.py:367,2520`）与 `/code/neurdb-dev`（`run_moqoe_server.sh`）。
- `parser/plan_node.py:11` 与整个 plan_gen_expert 包依赖不存在的 `common.workload`。
- `tree_based_expert.py` 与 `model.py` 是重复实现。
- 单线程 `HTTPServer`，每次请求触发 5 到 40 次 EXPLAIN。

---

## 三、重点：SELIX 学习索引与 nram 存储

### 3.1 SELIX 本体

SIGMOD 2026 论文「On Self-Designing Learned Indexes」的实现。README 明示基于 ALEX（`selix/README.md:53`），代码头保留 Microsoft 版权（`lit.h:1-2`）。它是 header-only 的 C++ 模板库，命名空间 `lit`，CMake 只安装头文件。

**核心类**

- `Lit<T, P, Compare, Alloc, allow_duplicates=true>`（`lit.h:51-57`），T 必须是算术类型。`LitMap` / `LitMultimap` 是 std::map 风格包装。
- `LitNode`：公共基类，含 `is_leaf_`、`duplication_factor_`、`level_`、`model_`（LinearModel）、`cost_`（`lit_nodes.h:36-66`）。
- `LitModelNode`：线性模型加 `children_` 指针数组，子节点数必为 2 的幂，`get_child_node = clamp(a*key+b)`（`:68-113`）。
- `LitDataNode`：带位图的 gapped array 加线性模型，`next_leaf_/prev_leaf_` 双链供范围扫描（`:313-332`）。查找先预测位置再指数搜索（`:1596-1616`）。
- 超级根 `superroot_` 幻影节点（`lit.h:76-77`），域外键计数触发 `expand_root`。
- fanout tree（`lit_fanout_tree.h`）：bulk_load 时 `find_best_fanout_bottom_up/top_down` 按代价模型决定每个节点变成数据节点还是以 2^k 扇出继续分区；插入时 `find_best_fanout_existing_node` 决定扩容重训还是分裂。

**公有 API**（`lit.h`）：`bulk_load`(667)、`insert`(1150)、`find`(927)、`lower_bound/upper_bound/equal_range`(960/977/993)、`get_payload`(1005)、`erase_one/erase/clear`(2194-2244)、迭代器(2518-3039)、`get_stats/data_size/model_size`(2411/2379/2393)、setters(364-410)。

**SELIX 相对 ALEX 的增量**

1. `ConflictPolicy {SHIFT, CHAIN}`（`lit_base.h:58-61`），CHAIN 为 LIPP 风格每槽溢出链（`lit_nodes.h:346-473`）。
2. 密度阈值从常量改为可运行时设置的 static 成员（`lit_nodes.h:335-343`）。
3. 四个代价权重从常量改为可写全局 `inline double`（`lit_base.h:202-207`）。
4. 新增 `set_density_params` / `set_conflict_policy`（`lit.h:396-410`）。

**可调参数**

| 参数 | 默认值 | 位置 |
|---|---|---|
| expected_insert_frac / max_node_size / approx_model / approx_cost | 1 / 16MB / true / false | `lit.h:80-98` |
| max_fanout / max_data_node_slots | 2^21 / 16MB÷sizeof(V) | `lit.h:101-105` |
| fanout_selection_method / splitting_policy_method / allow_splitting_upwards | 0 / 1 / false | `lit.h:132-152` |
| kInitDensity / kMaxDensity / kMinDensity | 0.7 / 0.8 / 0.6 | `lit_nodes.h:335-337` |
| kExpSearchIterationsWeight / kShiftsWeight / kNodeLookupsWeight / kModelSizeWeight | 20 / 0.5 / 20 / 5e-7 | `lit_base.h:202-207` |
| kMin/MaxOutOfDomainKeys / ToleranceFactor | 5 / 1000 / 2 | `lit.h:205-213` |
| kAppendMostlyThreshold | 0.9 | `lit_nodes.h:508` |
| conflict_policy | SHIFT | `lit_base.h:58-61` |

### 3.2 DRL 调参机制（`selix/src/drl/`）

完全离线。

- `lit_wrapper.cpp`：pybind11 模块，暴露 `ALEXMemAction`（12 个字段，`:33-55`）和 `run_benchmark(action, keys_path, init_count, op_count, read_ratio)`（`:68-134`）。读 SOSD 格式键文件，写全局权重，新建 `Lit<uint64,uint64>` 并调 setters，bulk_load 后**先全部 insert 再全部 find**（非交错负载，`:190-201`），返回吞吐、SMO 次数、节点数。
- `lit_tuning_env.py`：Gym 环境。状态 9 维 = 8 个归一化参数加吞吐比（`:214-242`）；动作 8 维 Box[0,1]（`:117-121`），反归一化为 node_size 4–64MB、三个密度 0.2–0.9、四个代价权重；奖励 `(thr - baseline)/baseline`，崩溃 -1（`:301-307`）。每步 `terminated=True`（`:324`），本质是 contextual bandit 而非序贯决策。基准在子进程运行以隔离 segfault，120s 超时。不调 conflict_policy 与 insert_frac。
- `lit_multi_workload_env.py`：状态含 read_ratio，动作仅 4 维，每次 reset 随机切换 5 种负载。
- `train.py` / `train_multi_workload.py`：stable-baselines3 PPO MlpPolicy，lr 3e-4、n_steps 16、batch 16。

**关键发现：学习结果没有落地。**

- `SELIXIndexEngine` 构造函数只打印一条日志，从未调用任何 setter（`indexengine.cpp:90-92`），索引一律使用默认参数。
- nrindex 没有 reloptions（`nrindex.c:301-306`）。
- DRL 训练产物只有 stdout 和 PPO 的 `.zip` 文件。
- 密度是 static 类成员、权重是全局变量，同一进程内所有 Lit 实例共享，即便接上也无法按索引调参。

C++ 侧继承自 ALEX 的运行时自适应仍在：数据节点记录 `num_shifts_/num_exp_search_iterations_/num_lookups_/num_inserts_`（`lit_nodes.h:488-493`），`empirical_cost()` 与期望代价比较判定 `significant_cost_deviation` 或 `catastrophic_cost`（`:1805-1815`）。插入失败进入 `lit.h:1171-1265` 循环：fanout tree 返回 depth 0 则 `resize(kMinDensity, force_retrain=true)` 扩容重训，否则 `split_sideways` 或 `split_downwards`。无后台异步重训，代价模型权重是超参数而非学习得到。

### 3.3 nram 存储架构（`dbengine/nr_kernel/nr_am`）

**注册**：`sql/nram--1.0.sql` 创建 `nram_tableam_handler` + `CREATE ACCESS METHOD nram TYPE TABLE`、`nrindex_handler` + `TYPE INDEX`、int4/int8 opclass。`nram.c:762-811` 返回 `TableAmRoutine`；`nrindex.c:623-675` 填 `IndexAmRoutine`（amcanunique/amcanmulticol=true，amcanorder/amcanbackward/amsearchnulls=false）。`_PG_init`（`nram.c:932-945`）挂 shmem hook、Executor hook、xact 回调、注册 bgworker。

**元组到 KV 编码**（`nram_access/kv.[ch]`）：key = `{Oid tableOid, uint64 tid}`，tid = [32 位进程内计数][16 位 pid]（`kv.c:24-28`）；value = `[xact_id][flags][nfields]{attnum,type_oid,len,datum}*`（`kv.h:56-61`）。单版本存储，flags 仅 PRIVATE/DELETED，无 MVCC。

**RocksDB 部署形态**：是 PostgreSQL 后台工作进程，不是独立 OS 服务。`rocks_service.c:791-816` 用 `RegisterBackgroundWorker` 注册 `rocks_service_main`，单线程 `run_rocks_no_thread` 循环（`:212-249`），bgworker 内以 C API 打开 `pg_rocksdb` 目录。后端与 bgworker 通过共享内存环形缓冲通信：`KVChannelShared` = 16MB buffer + LWLock + ConditionVariable（`ipc/msg.h:19-28`），请求通道 `rocks_service_channel`，每后端一个响应通道 `kv_resp_<pid>`（`rocks_handler.c:10-29`），预留 17×16MB 共享内存（`nram.c:918`）。

**并发控制**：OCC 为主、可按策略切 2PL 的混合，即 NeurCC。每事务 `NRAMXactState{read_set, write_set, feature, action}`（`xact.h:40-54`）。INSERT 立即 `RocksClientPut` 并置 PRIVATE 标记（`nram.c:325-334`）；UPDATE 只入 write_set。`XACT_EVENT_PRE_COMMIT` 回调（`xact.c:314-378`）对写集排序加 advisory 排他锁、SERIALIZABLE 下校验读集、flush 写集去 PRIVATE。`before_access`（`xact.c:434-452`）用 5 位特征 `(cur_op, n_access)` 查共享内存策略表得 `{detect_all, priority, timeout}`，detect_all=true 时在访问点即加锁，退化为 2PL。`nram_load_policy('2pl'|'occ'|文件)`（`action.c:58-170`）。`optimizer/` 用 nevergrad 贝叶斯优化在 YCSB 上搜索 32 状态策略表，与 SELIX 无关。

### 3.4 SELIX 与 PG 的桥接层（`nram_storage/indexengine.cpp`，326 行）

- 内部 `std::map<Oid, lit::Lit<int64_t, uint64_t>*>`，按索引 Oid 懒建（`:85-109`）。
- **key**：`nrindex_key_create` 把 INT2/4/8 编成大端加翻符号位（`nrindex_kv.c:58-98`）；`extract_int_from_key` 读回 int64（`:37-62`）；再 `encode_key_64` 翻符号位后强转 int64（`:113`）。等值查询无害，但原非负键落入负区间，日后支持范围扫描会错序。
- **value**：`compress_heap_tid = blk<<32 | off`（`:69-73`）。nram 的 tid 恰好 block=计数、offset=pid，可无损往返。
- **支持**：put（允许重复键，但 get 用 `find` 只返回一条）、get、exists、bulk_load（排序后 `std::unique` 去重，重复键的其他 TID 被丢弃，`:147-171`）、count。
- **不支持**：delete（`:233-235` 仅 WARNING）、clear_range（`:291-293`）、真正范围扫描（`:274-276` WARNING 后返回 0 行）。等值 range_scan 退化为单条 get。
- **限制**：只对单列 INT2/4/8 有效。`nrindex_build` 只取第 0 列且非 INT8 一律 `DatumGetInt32`（`nrindex.c:104-111, 146-150`），TEXT 列会得到指针值。多列拼接后被当作一个整数。唯一性检查只 `return false` 不报错（`nrindex.c:232-243`）。
- **持久化：纯内存且进程私有**。实际调用的是 `nrindex_kv.c:18-27` 的静态 `local_index_engine`（注释称 direct call mode）。bgworker 里的 `index_engine`、IPC 处理器 `handle_kv_index_*`（`rocks_service.c:469-763`）和客户端 `RocksClientIndex*`（`rocks_handler.c:197-462`）**没有任何调用者**（grep 核实）。因此 CREATE INDEX 建出的索引只存在于执行它的会话中，其他会话为空，重启即丢失。`nrindex_buildempty` 为空函数（`nrindex.c:198-202`），无重建逻辑。

### 3.5 端到端链路

```
INSERT INTO t VALUES(...)   -- t USING nram，含 USING nrindex 索引
  nram_tuple_insert [nram.c:342] ─► nram_insert [:306]
    ─► nram_generate_tid [kv.c:41] + nram_value_serialize_from_tuple [kv.c:89] ─► 置 PRIVATE [nram.c:325]
    ─► [2PL 策略时 nram_lock_for_write xact.c:113]
    ─► RocksClientPut [rocks_handler.c:36] ─► KVChannelPushMsg [msg.c:423]
    ─► bgworker run_rocks_no_thread [rocks_service.c:222] ─► handle_kv_put [:346] ─► rocksengine_put [rocksengine.c:187]
    ─► 响应经 kv_resp_<pid> 返回 ─► add_write_set [xact.c:266]
  ExecInsertIndexTuples ─► nrindex_insert [nrindex.c:207]
    ─► nrindex_key_create [nrindex_kv.c:105] + nrindex_value_create [:287] ─► nrindex_rocks_put [:358]
    ─► indexengine_put [indexengine.cpp:196] ─► SELIXIndexEngine::put [:111]
    ─► Lit::insert [lit.h:1150] ─► get_leaf [:421] ─► LitDataNode::insert [lit_nodes.h:1827] ─► 必要时 resize/split
    （仅本进程内存）
  COMMIT ─► nram_xact_callback(PRE_COMMIT) [xact.c:314] 加锁、校验、清 PRIVATE ─► clear_nram_xact [:180]

SELECT ... WHERE k = 42     -- 走 nrindex
  nrindex_beginscan [nrindex.c:321] ─► nrindex_rescan [:355] 识别 BTEqualStrategy
    ─► nrindex_rocks_point_lookup [nrindex_kv.c:405] ─► indexengine_get [indexengine.cpp:210]
    ─► Lit::find [lit.h:927] ─► LitDataNode::find_key [lit_nodes.h:1596] 预测位置 + 指数搜索
    ─► 解压 heap_tid ─► nrindex_gettuple [nrindex.c:493] 设 xs_heaptid
    ─► nram_index_fetch_tuple [nram.c:244] ─► 命中本地读写集 或 RocksClientGet [rocks_handler.c:81]
    ─► bgworker handle_kv_get [rocks_service.c:325] ─► rocksengine_get [rocksengine.c:164]
    ─► deserialize_nram_value_to_tuple [kv.c:135] ─► nram_mark_tuple_visible_always [nram.c:43] ─► add_read_set
```

注意：PRIMARY KEY 和普通 CREATE INDEX 默认仍是 btree，只有显式 `USING nrindex` 才走 SELIX。nram 表上建 btree 时 `nram_index_build_range_scan` 直接返回 0（`nram.c:675-685`），只有后续 INSERT 进入 btree。

### 3.6 缺陷清单

**SELIX 本体**

- **CHAIN 模式内存不安全**：`resize()`（`lit_nodes.h:1888+`）不迁移链数组，`chain_insert` 扩容后按新容量 `predict_position` 索引旧数组（`:429-436`）会越界写。迭代器、`find_lower`、`erase`、分裂重建均不遍历链，链内键在范围扫描和 SMO 后丢失。`Lit::find` 对链中命中返回 `Iterator(leaf,pos)`，`payload()` 取到的是主槽值。DRL 环境也不调该策略，等于未验证特性。
- 参数为 static/全局，多实例互相污染；无序列化/持久化；单线程无并发控制。
- 只有 `examples/basic_usage.cpp`，无单元测试。

**nr_am**

- **索引后端私有**（见 3.4），IPC 索引路径为死代码。
- **建索引可能直接报错**：`nrindex_build` 用 `heap_getnext`（`nrindex.c:121`），而内核 `heapam.c:1104-1107` 保留「only heap AM is supported」检查，对 nram 表执行 `CREATE INDEX ... USING nrindex` 预期会 ERROR。代码推断，未实际运行。
- **范围查询静默出错**：`<, <=, >, >=` 只设单侧边界（`nrindex.c:428-443`），桥接层遇 NULL 边界直接返回 0 行（`indexengine.cpp:247`）；`nrindex_key_matches_scan` 恒 true（`nrindex_kv.c:274-280`）；NULL 扫描、bulkdelete、vacuumcleanup 均 TODO。
- **表 AM**：`nram_rescan` 新建无用 KVScanDesc、未重置真正 cursor（`nram.c:116-122`），嵌套循环内表重扫返回空；DELETE 不支持（`:445-451`，bgworker `kv_delete` 未实现）；abort 不清除已写入的 PRIVATE 行（`xact.c:381-385`）形成永久隐藏垃圾；tid 依赖 pid 低 16 位，重启或 pid 复用可能碰撞覆盖；WAL 为 TODO（`xact.c:375`）；`nram_estimate_rel_size` 全 0（`nram.c:717-726`）；约 25 个 AM 回调为 `NRAM_UNSUPPORTED()`。
- **bgworker 崩溃点**：`rocksengine_get` 对 `rocksdb_get` 返回的 NULL 直接 `tvalue_deserialize`（`rocksengine.c:172-176`），读不存在的 key 会空指针崩溃（推断）。
- **共享内存泄漏**：每个新后端 pid 用 `ShmemInitStruct` 新建 `kv_resp_<pid>`，PG addin 共享内存不可回收且只预留 17 个，长期运行会耗尽（推断）。
- 热路径上大量 `fopen("/tmp/nrindex_debug.log")` 调试代码（`rocks_service.c:117-160, 299-313, 564-726`）。
- 构建：`indexengine.c` 为 0 字节，Makefile 同时列 `indexengine.o` 与 `%.o: %.cpp` 规则，依赖 make 规则优先级选中 .cpp（未确认）。

**测试覆盖**

- REGRESS = clean/init/unit_tests/table_scan/index_scan/xact_basic（`Makefile:36`）；ISOLATION 6 个 spec 覆盖 OCC/2PL 下 lost update、repeatable read、SSI/RC；`run_nram_tests()` 11 项 C 单测覆盖 KV 序列化、Rocks 服务、通道、策略表。
- **没有任何测试使用 `USING nrindex`**（grep 为空），`index_scan.sql` 的索引均为默认 btree。
- **SELIX 桥接层与 SELIX 本体在本仓库内零测试覆盖。**

---

## 四、两个模块的共性与建议

NQO 和 SELIX 都把学习部分放在离线 Python 脚本里，运行时的 C 代码不读取学习结果，或者只以最粗粒度的方式使用它。README 里「fast-adaptive」「DRL-based index adaptation」描述的闭环，在当前代码中都还没有接通。

如果要基于它们做二次开发，闭环的最小缺口是：

1. **NQO**：在 nr_molqo 中增加 ExecutorEnd 钩子，把实际执行时间和计划回传给 neurqo_frame 的经验缓冲；同时修复 `p_sourcetext` 悬垂指针，并把 SET 落地改为语句级（例如 `PGC_S_SESSION` 后在 ExecutorEnd 复位）。
2. **SELIX**：为 nrindex 增加 reloptions，把 DRL 输出的参数在 `SELIXIndexEngine::getIndex` 创建 Lit 时通过 setter 传入；把索引实例从后端私有迁到 bgworker（IPC 路径已有骨架），并补上持久化或重启重建逻辑。
3. 两者都需要先补最基本的测试：nr_molqo 的 hook 单测、`USING nrindex` 的回归 SQL、SELIX 的 insert/find/range 单测。

---

## 五、NQO 与 SELIX 的输入状态与学习机制

本节回答两个问题：每个学习器的输入状态具体是什么，以及它们是否符合论文"状态 → 动作"的框架。结论先行：**两个模块的学习器都有状态输入，也都输出动作，函数形式与论文一致；断掉的是"线上执行结果 → 训练信号"这一段，以及 SELIX 的"动作 → 系统"这一段。**

### 5.1 NQO 的输入状态（分四层）

**第 0 层：PG 侧发给服务的内容。** 只有 SQL 文本一项，打包成 `{"sql": "..."}`（`nr_molqo/src/nr_molqo.c:202-254`）。没有计划、统计信息或执行反馈。

**第 1 层：路由器的查询状态。** `Sql2VecEmbeddingV2.encode_query`（`src/expert_router/encoder.py:414-490`）把 SQL 解析成一个字典，含三个数组：

| 字段 | 形状 | 每个元素 | 来源 |
|---|---|---|---|
| `join_conditions` | [n_join, 4] | (表1 id, 列1 id, 表2 id, 列2 id) | 别名经 `table_no_map` 映射到 21 张 IMDB 表，列经 `attr_no_map_list` 映射 |
| `filter_conditions` | [n_filter, 3] | (表 id, 列 id, 选择率) | 选择率由 `pg_conn.get_selectivity` 反连 PG 跑两次 EXPLAIN 估算，取值 [0,1] |
| `table_sizes` | [21] | log1p(行数) 做 min-max 归一化，再乘"该表是否出现在查询中"的掩码 | `models/router_models/ori_table_info.json` 的 `table_size_list` |

进入模型后（`src/expert_router/model.py:49-120`）：每条 join 拼四个 embedding 经线性层变成一个 token，每条 filter 拼两个 embedding 加选择率标量变成一个 token，前面加一个 super token，过 TransformerEncoder。最终特征是 super token 输出拼接 21 维 `table_sizes`。输出是对 5 个方法 HintPlanSel、PlanGenSim、PlanGen、PostgreSQL、JoinOrder 的分数（`src/common/config_imdb.py:10`）。

**第 2a 层：HintPlanSel 专家的状态。** 对 5 个"臂"各跑一次 EXPLAIN 得到 5 棵计划树（`src/expert_pool/hint_plan_sel_expert/model.py:49-113, 331`）。每个臂是一组 `enable_*` 开关组合：

1. 臂 0：nestloop、hashjoin、mergejoin、seqscan、indexscan、indexonlyscan 全开
2. 臂 1：关 nestloop
3. 臂 2：关 mergejoin、indexscan
4. 臂 3：关 nestloop、mergejoin、indexscan
5. 臂 4：关 mergejoin

每棵树的节点向量（`featurize.py:37-47`）= 7 维算子 one-hot ++ 对数归一化的 [Buffers, Total Cost, Plan Rows]。算子集合是 Nested Loop、Hash Join、Merge Join、Seq Scan、Index Scan、Index Only Scan、Bitmap Index Scan。叶节点额外带关系名。单子节点被折叠成二叉树。TreeCNN 对每棵树回归延迟，取 argmin。

**第 2b 层：JoinOrder 专家的状态。** 分两部分（`src/expert_pool/join_order_expert/mcts_based_expert.py:206-251`）：

1. SQL 级向量 `Sql2Vec.to_vec`（`encoders/sql_to_vec.py:32-126`）：40×40 的别名 join 邻接矩阵展平成 1600 维，再拼 100 维按列累加的选择率，共 1700 维。别名表写死在 `config_imdb.py:24-66`，列 id 按首次出现顺序分配。
2. 计划树特征 `TreeBuilder`（`encoders/mcts_encoder.py:42-126`）：每节点 9 维 = 7 维算子 one-hot ++ LatencyNormalizer 编码的 [Total Cost, Plan Rows]；扫描叶节点附带别名 id 供 embedding。TreeLSTM 隐层 64。
3. MCTS 搜索状态：已加入 join 顺序的别名集合，由 ValueNet 以 1700 维 SQL 向量评估。

**第 3 层：经验缓冲的反馈状态。** SQLite 表每行是 (query_hash, sql, actual_plan_json, actual_latency, plan_time, hint_json, join_order_hint)（`src/exp_buffer/sqllite.py:50-60`）。这是重训专家时的输入，但 PG 端不回传，只能离线填充。

### 5.2 NQO 的学习机制：状态 → 动作成立，缺的是在线反馈

NQO 的三个学习器都是从状态到动作的映射，实现方式是 Bao 风格的"学一个代价模型，再对候选取 argmin"，而不是直接学策略。

| 学习器 | 输入状态 | 训练目标 | 训练信号来源 | 输出动作 |
|---|---|---|---|---|
| 路由器 | 查询特征：join 条件、filter 条件加选择率、21 维表大小 | 监督学习。分类头学"哪些专家的延迟在最优的阈值内"，回归头学各专家延迟。损失是 focal BCE 加 MSE（`loss.py:76-136`） | 离线 CSV 数据集，记录每条查询在 5 种方法下的实际执行时间 | 选哪个专家（5 选 1） |
| HintPlanSel 专家 | 5 个臂各一棵 EXPLAIN 计划树 | 监督回归。TreeCNN 拟合 计划树 → 实际延迟，MSE（`model.py:179-215`） | SQLite 缓冲里的 (actual_plan_json, actual_latency) 对 | 5 组 `SET enable_*` 中延迟预测最小的一组 |
| JoinOrder 专家 | 1700 维 SQL 向量加计划树特征 | 监督回归。TreeLSTM 拟合 (SQL, 计划) → 延迟，带 10 个头估不确定性，再用 KNN 修正 | 同一缓冲 | MCTS 搜出的 `Leading(a b)` 前缀 |

- **输入的是什么。** 查询本身派生的状态：谓词结构、PG 估计的选择率、表大小、PG 优化器给出的候选计划树。这对应论文说的 query-state abstraction。
- **训练的是什么。** "状态 → 延迟"的代价模型和"状态 → 最优专家"的分类器，全部是监督学习，没有奖励函数，没有策略梯度。
- **输出是不是动作。** 是。三个输出最终都落成一个可执行的动作：一组 GUC 开关或一个 join 顺序 hint，由 nr_molqo 施加到 PG 上。

与论文相悖的地方只在反馈环。论文描述的 workload feedback 要求执行后的真实延迟回流到学习器。代码里这条通道只在离线脚本 `buffer_mngr.py` 跑 EXPLAIN ANALYZE 时存在，PG 运行时不回传，所以线上是"状态 → 动作"单向的，模型不会因为线上执行结果而变化。

### 5.3 SELIX 的输入状态（分五层）

SELIX 的离线部分与运行时部分完全不相通。

**A. DRL 单负载调参环境的状态。** `selix/src/drl/lit_tuning_env.py:125-129, 214-242` 定义 9 维观测，全部归一化到 [0,1]：

| 序号 | 字段 | 原始参数 | 取值范围 |
|---|---|---|---|
| 1 | `node_size_norm` | `max_node_size` | 4–64 MB |
| 2 | `init_d_norm` | `kInitDensity` | 0.2–0.9 |
| 3 | `max_d_norm` | `kMaxDensity` | 0.2–0.9 |
| 4 | `min_d_norm` | `kMinDensity` | 0.2–0.9 |
| 5 | `exp_search_w_norm` | `kExpSearchIterationsWeight` | 5–50 |
| 6 | `shifts_w_norm` | `kShiftsWeight` | 0.1–2.0 |
| 7 | `node_lookups_w_norm` | `kNodeLookupsWeight` | 5–50 |
| 8 | `model_size_w_norm` | log10(`kModelSizeWeight`) | 10^-8 到 10^-5 |
| 9 | `throughput_norm` | 当前吞吐 / 基线吞吐 / 2，截断到 [0,1] | — |

动作是前 8 项对应的 8 维 [0,1] 连续向量。奖励是 (吞吐 − 基线) / 基线，崩溃记 −1。每步 `terminated=True`，状态里没有历史。负载本身（键文件、初始键数、操作数、读比例）在环境构造时固定，不进入状态。

**B. DRL 多负载环境的状态。** `lit_multi_workload_env.py:23, 78-81` 缩减为 6 维：[read_ratio, node_size_norm, init_d_norm, max_d_norm, min_d_norm, throughput_norm]。动作只有 4 维，不调代价权重。read_ratio 每次 reset 从 5 种负载中随机抽取。

**C. 基准测试返回的反馈量。** `lit_wrapper.cpp:210-229` 返回 throughput、total_smo、bulk_time_ms、workload_time_ms、num_keys、num_data_nodes、num_model_nodes、read_count、write_count。环境只用前两个。

**D. C++ 运行时内部状态。** 这是 ALEX 自适应真正依据的量，不暴露给任何学习器：

1. 每个数据节点的经验计数（`lit_nodes.h:488-493`）：`num_shifts_`、`num_exp_search_iterations_`、`num_lookups_`、`num_inserts_`、`num_resizes_`。
2. 每个数据节点的期望值：`cost_`、`expected_avg_shifts_`、线性模型系数 `a_`、`b_`、`num_keys_`、`data_capacity_`。
3. append-mostly 检测：`max_key_`、`min_key_`、`num_right_out_of_bounds_inserts_`（`lit_nodes.h:496-508`）。
4. 派生量 `empirical_cost()`（`:843-851`），与 `cost_` 比较触发 `significant_cost_deviation`、`catastrophic_cost`（`:1805-1815`）。
5. 全局 `Stats` 结构（`lit.h`）：num_keys、num_model_nodes、num_data_nodes、num_expand_and_scales、num_expand_and_retrains、num_downward_splits、num_sideways_splits、num_model_node_expansions、num_model_node_splits、num_node_lookups、num_lookups、num_inserts、splitting_time、cost_computation_time。

**E. PG 桥接层送进索引的输入。** `indexengine.cpp:37-73`：每次操作只有三个量，索引 Oid 用于选 Lit 实例，key 是从 nrindex 大端编码解回的 int64（仅 INT2/4/8 列），value 是 block 左移 32 位或 offset 的压缩 TID。没有任何负载统计或调参量从 PG 传入。

### 5.4 SELIX 的学习机制：状态是配置而非索引，动作没有落地

SELIX 里有两层"学习"，性质完全不同。

**第一层是 DRL 调参器**（PPO，`train.py:90-103`）。严格是状态到动作：输入 9 维配置加吞吐比，输出新的 8 维配置，奖励是吞吐相对基线的提升。它的问题有两个。第一，状态里只有"配置本身加一个吞吐标量"，没有索引结构信息，所以它学的是"给定当前配置该往哪调"，本质是对配置空间的 contextual bandit。第二，动作从未被施加到数据库：`SELIXIndexEngine` 建索引时不调任何 setter（`indexengine.cpp:90-92`），PPO 的输出只存在 zip 文件里。

**第二层是索引内部的"学习"**（ALEX 继承），含义与 RL 无关：

- 学的函数是每个节点的线性模型 `key → 槽位位置`，用最小二乘拟合（`lit_base.h` 的 `LinearModelBuilder`）。输入是键，输出是位置预测，这是 learned index 原本的含义，不是决策动作。
- 结构调整的决策，即扩容还是分裂、分裂成几路，是规则驱动的：比较节点的经验代价与期望代价，再用手工权重的代价模型搜 fanout tree（`lit.h:1171-1230`）。这套决策没有任何学习成分，权重就是 DRL 要调的那四个超参数。

对 SELIX 的三个问题：

- **输入的是什么。** DRL 层输入的是超参数配置加吞吐比；索引层输入的是键。
- **训练的是什么。** DRL 层训练 PPO 策略网络；索引层训练线性回归。真正决定索引行为的分裂规则不训练。
- **输出是不是动作。** DRL 层输出的是配置，是动作；但这个动作只在离线基准里被执行，线上索引永远用默认配置。

### 5.5 与论文表述的差距

对照的是 NeurDB README 和 SELIX README 的描述，未读 SELIX 的 SIGMOD 论文原文。

| 模块 | 论文/README 描述 | 代码状态 |
|---|---|---|
| NeurQO | query-state abstraction | 已实现，即第 1 层与第 2 层的查询特征与候选计划树 |
| NeurQO | workload feedback、fast-adaptive | 未实现，反馈只能离线灌入 |
| Selix | DRL-based index adaptation | 代码里是"DRL 离线调超参数"，且结果没有接回索引；索引运行时自适应是 ALEX 原有的规则机制 |

按论文的 s → a 框架衡量，两个模块的函数形式都对，训练信号在离线阶段也都有；断掉的是"线上执行结果 → 训练信号"这一段，以及 SELIX 的"动作 → 系统"这一段。

---

## 附录 A：NQO 文件清单

| 文件（相对 `aiengine/neurqo_frame/` 或 `dbengine/nr_kernel/nr_molqo/`） | 行数 | 职责 |
|---|---|---|
| README.md | 118 | 运行示例、环境、离线训练命令 |
| run.py | 130 | HTTP 推理服务入口（:8666 /optimize） |
| run_moqoe.sh / run_moqoe_server.sh | 2 / 11 | 设 PYTHONPATH 启动服务（硬编码路径） |
| configs/postgres.cfg | 6 | PG 连接账号/超时 |
| environment_moqoe.yml | 174 | conda 依赖 |
| src/moqoe.py | 357 | MoQOEController/RoutingHelper：路由、专家池、在线重训触发 |
| src/common/{__init__,base_config,config_imdb,config_stack}.py | 13/29/121/46 | 数据集配置、方法列表、别名映射 |
| src/db/pg_conn.py | 363 | psycopg2 连接、EXPLAIN/EXPLAIN ANALYZE、选择率、SET hint |
| src/exp_buffer/buffer_mngr.py | 129 | 执行并记录 (计划,延迟,hint) 到缓冲 |
| src/exp_buffer/sqllite.py | 329 | SQLite plan_buffer 存取 |
| src/parser/{entiy,plan_parser,plan_node,sql_parser,table_parser}.py | 34/134/285/552/339 | 表信息结构、计划/SQL 解析（plan_node 依赖缺失模块） |
| src/expert_router/model.py | 251 | Transformer 路由网络（分类+回归双头） |
| src/expert_router/encoder.py | 670 | 查询特征化 Sql2VecEmbeddingV2 + JSON 缓存 |
| src/expert_router/controller_offline.py | 827 | ModelBuilder：训练/评估/单条推理(hypered_2) |
| src/expert_router/controller_online.py | 311 | OnlineRouter（Thompson 采样在线微调，未接入服务） |
| src/expert_router/{router_offline_pretrain,router_online_update,router_inference}.py | 180/368/181 | 离线/在线/评估脚本（cfg.CONFIG 等错误，不可直接运行） |
| src/expert_router/{loss,dataset,workloads,workloads_stack,preprocess_query_feature,logger}.py | 136/334/497/558/62/50 | 损失、数据集、工作负载切分、特征预生成、日志 |
| src/expert_pool/hint_plan_sel_expert/model.py | 513 | HintPlanSel 专家（Bao，5 臂 SET hint） |
| src/expert_pool/hint_plan_sel_expert/featurize.py | 268 | Bao TreeFeaturizer 计划树特征 |
| src/expert_pool/hint_plan_sel_expert/tree_cnn.py | 268 | TreeCNN 网络 |
| src/expert_pool/hint_plan_sel_expert/{tree_based_expert,plan_encoder}.py | 458/344 | 旧版重复实现/编码器（未被导出） |
| src/expert_pool/join_order_expert/mcts_based_expert.py | 943 | JoinOrder 专家（HybridQO：TreeSQLNet+MCTS+KNN，Leading hint） |
| src/expert_pool/join_order_expert/mcts.py | 396 | UCT 搜索 join 顺序 |
| src/expert_pool/join_order_expert/encoders/{sql_to_vec,mcts_encoder,job_parser}.py | 126/126/391 | SQL 向量化、计划树编码、psqlparse 包装 |
| src/expert_pool/join_order_expert/models/{mcts_net,tree_lstm,torchfold}.py | 479/42/264 | TreeSQLNet/ValueNet/TreeLSTM |
| src/expert_pool/join_order_expert/{KNN.py,tools/normalize.py} | 63/70 | KNN 误差修正、延迟归一化 |
| src/expert_pool/plan_gen_expert/**（16 文件） | 约 8400 | Balsa 移植 PlanGen 专家，未接入且不可导入 |
| src/utils/{plan_utils,io,file_utils,array,date_utils,sql_utils}.py | 189/266/106/73/17/10 | 计划哈希/IO 工具 |
| script/load_to_db/imdb/load_job_postgres.sh (+3 sql) | 56 | 装载 IMDB/JOB |
| models/router_models/router_model.pth | 8.2MB | 路由网络权重 |
| models/router_models/ori_table_info*.json, query_encodings_embedding_v2_*.json | 20KB/235KB | 表元信息、149 条 JOB 查询编码缓存 |
| models/tree_expert_models/current/* | 536KB | HintPlanSel 权重（Bao 格式） |
| models/join_order_exp_models/{model.pt,knn.pkl} | 1.9MB/226KB | JoinOrder 权重 |
| models/buffer_imdb_ori.db | 2MB | 经验缓冲（107 条） |
| nr_molqo/src/nr_molqo.c | 446 | 扩展主体：GUC、post_parse_analyze_hook、curl 通信、SET/hint 落地 |
| nr_molqo/src/http_client.{c,h} | 182/17 | libcurl+json-c 客户端（未编译） |
| nr_molqo/sql/nr_molqo--1.0.sql | 31 | optimize_query()/molqo_status() |
| nr_molqo/nr_molqo.control / Makefile / CMakeLists.txt | 7/33/49 | 扩展元数据与构建 |
| nr_molqo/README.md / simple_server.py | 150/208 | 安装测试说明、规则式测试服务器（:8080） |

## 附录 B：SELIX 与 nr_am 文件清单

| 文件（相对 `dbengine/nr_kernel/nr_am/`） | 行数 | 职责 |
|---|---|---|
| src/nram_storage/selix/README.md | 55 | SELIX 简介、DRL 训练步骤 |
| .../selix/CMakeLists.txt | 16 | header-only INTERFACE 库，仅安装头 |
| .../selix/include/lit/lit.h | 3039 | `Lit` 主类：RMI、bulk_load、insert/SMO、查找、迭代器 |
| .../selix/include/lit/lit_base.h | 403 | 线性模型/构建器、ConflictPolicy、代价权重全局、统计累加器 |
| .../selix/include/lit/lit_nodes.h | 2487 | `LitModelNode`/`LitDataNode`（gapped array、CHAIN 链、resize、erase） |
| .../selix/include/lit/lit_fanout_tree.h | 456 | 扇出树代价搜索（bulk_load 与分裂决策） |
| .../selix/include/lit/lit_map.h / lit_multimap.h | 248 / 223 | std::map/multimap 风格包装 |
| .../selix/src/drl/lit_wrapper.cpp | 269 | pybind11 绑定：`ALEXMemAction`、`run_benchmark` |
| .../selix/src/drl/lit_tuning_env.py | 416 | 单负载 Gym 环境（8 维动作/9 维状态） |
| .../selix/src/drl/lit_multi_workload_env.py | 300 | 多负载 Gym 环境（4 维动作，含 read_ratio） |
| .../selix/src/drl/train.py / train_multi_workload.py | 192 / 185 | PPO 训练脚本 |
| .../selix/src/drl/test_model.py / config.py / Makefile | 159 / 18 / 22 | 模型评估 / 数据配置 / pybind 编译 |
| .../selix/examples/basic_usage.cpp | 77 | API 用例（唯一「测试」） |
| README.md / Makefile / CMakeLists.txt / nram.control | 33/100/33/5 | 扩展构建（PGXS，链接 librocksdb、libstdc++） |
| sql/nram--1.0.sql | 52 | 注册 nram 表 AM、nrindex 索引 AM、int4/int8 opclass |
| src/nram.c / nram.h | 971 / 16 | 表 AM 全部回调、`_PG_init` hooks、`run_nram_tests`、`nram_load_policy` |
| src/nrindex.c / nrindex.h | 675 / 26 | 索引 AM：build(bulk)/insert/scan 回调、handler |
| src/nrindex_access/nrindex_kv.c / .h | 430 / 152 | 索引 key/value 编码；`local_index_engine` 直连 SELIX |
| src/nram_storage/indexengine.cpp / .h / .c | 326 / 58 / 0 | `SELIXIndexEngine` C 胶水层；`.c` 为空占位 |
| src/nram_storage/rocksengine.c / .h | 570 / 68 | RocksDB C API 封装（get/put/iterator/range/index_*） |
| src/nram_storage/rocks_service.c / .h | 826 / 66 | bgworker 主循环、请求分发、结果队列、bgworker 注册 |
| src/nram_storage/rocks_handler.c / .h | 462 / 16 | 后端侧 IPC 客户端（RocksClient*；Index 版无人调用） |
| src/nram_storage/thread.c / .h | 109 / 31 | pthread 线程池（默认单线程未用） |
| src/nram_storage/rocksdb.c / .h | 57 / 8 | 遗留 Unix-socket 客户端，未编译 |
| src/nram_access/kv.c / .h | 282 / 153 | 表 KV 结构、tid 生成、元组序列化、KVEngine 接口 |
| src/nram_xact/xact.c / .h | 452 / 78 | 事务状态、读写集、OCC 校验/提交回调、锁封装 |
| src/nram_xact/action.c / .h | 197 / 61 | NeurCC 策略表（共享内存）、策略文件加载、特征编码 |
| src/ipc/msg.c / .h | 524 / 117 | 共享内存环形通道、KVMsg 编解码 |
| src/nram_utils/config.h / .c | 17 / 2 | 路径/通道名/调试宏 |
| src/test/kv_test.c / channel_test.c / action_test.c | 398/312/173 | C 单测（由 `run_nram_tests()` 驱动） |
| optimizer/cc_optimizer.py / ng.py / train.py / utils.py | 340/112/173/125 | 并发控制策略的 nevergrad BO 学习器 |
| benchmark/ycsb.py | 362 | asyncpg YCSB 负载生成（CC 优化目标） |
| sql/*.sql(8) / expected/*.out(12) / specs/*.spec(7) | — | 回归与隔离测试（均不涉及 nrindex） |
| rocksdb_server | ELF | 遗留独立 RocksDB socket 服务，未使用 |
