# NQO、SELIX 与 Global Agent 的输入状态和输出动作

本文回答实验要验证的第二点：NQO 和 SELIX 各自独立运行时读到的状态是什么、输出的动作是什么；加入 Global Agent（GA）之后，GA 的输入状态和输出动作是什么。前半部分来自代码分析（细节与行号见 `NeuralDB_code_analysis.md` 第五节），后半部分说明这些量在实验中如何被记录下来，报告的哪一张表能看到它们。

---

## 1. NQO 独立运行时

### 1.1 输入状态

NQO 分三层读取状态。数据库只发出第 0 层，其余都是 NQO 服务自己派生的。

| 层 | 状态 | 内容 | 来源 |
|---|---|---|---|
| 0 | SQL 文本 | `{"sql": "...", "expert_filter": "all"}`。原版只有 `sql` 一项；`expert_filter` 是雏形为 GA 加的 | `nr_molqo.c` 在解析完成后发出的 HTTP 请求 |
| 1 | 路由器的查询特征 | `join_conditions`：每条 join 一行 (表1, 列1, 表2, 列2)；`filter_conditions`：每条谓词一行 (表, 列, 选择率)，选择率由回连数据库跑 EXPLAIN 估出；`table_sizes`：21 维，出现的表取归一化后的 log(行数)，没出现的为 0 | `expert_router/encoder.py` |
| 2a | HintPlanSel 专家的状态 | 5 组 `enable_*` 开关组合（臂）各跑一次 EXPLAIN，得到 5 棵计划树；每个节点 = 7 维算子 one-hot + [Buffers, Total Cost, Plan Rows] 的对数归一化 | `hint_plan_sel_expert/model.py`、`featurize.py` |
| 2b | JoinOrder 专家的状态 | 1700 维 SQL 向量（40×40 别名 join 矩阵 + 100 维列选择率）+ MCTS 搜索中每个部分计划的树特征（每节点 9 维） | `join_order_expert/mcts_based_expert.py`、`encoders/` |

没有进入状态的量：执行反馈（真实延迟）、系统负载、其他组件的状态。线上没有反馈回流，模型在服务期间不变（雏形以 `--freeze` 明确固定）。

### 1.2 输出动作

| 学习器 | 动作 | 落地形式 |
|---|---|---|
| 路由器 | 5 选 1：HintPlanSel、PlanGenSim、PlanGen、PostgreSQL、JoinOrder（后三者在当前代码中退化为原生优化器） | 选择由哪个专家处理 |
| HintPlanSel | 5 组开关中的 1 组 | 若干行 `SET enable_xxx TO on/off;` 加原 SQL；`nr_molqo` 在规划前施加、规划后复位（雏形改为语句级） |
| JoinOrder | 一个连接顺序 | `/*+ Leading(a b ...) */` 前缀加原 SQL，由 pg_hint_plan 施加 |
| 回退 | 不改写 | 原 SQL，交给原生优化器 |

JoinOrder 专家有一个内置门槛（`mcts_based_expert.py` 的 `should_try_hint`）：只有当它预测原生计划的延迟超过 100 ms、且搜到的连接顺序预测更快、且 KNN 的不确定性低于阈值时才给出 `Leading` 提示，否则原样返回。在本机的检查里，JOB 的 1a、2a、13a、17a 等快查询都没有得到提示，19d 得到了 `/*+Leading(chn ci)*/`。因此在"快查询子集"上，`join` 模式多数时候等于关闭 NQO 再加一次推理开销；这是 NQO 自身的行为，报告第 3.1 节的 `optimized` 与 `fallbacks` 计数会把它显示出来。HintPlanSel 专家总是返回一组开关（可能是"全开"这一组，等价于不改变计划）。

运行时的直接证据：

```bash
# 把一条 JOB 查询送到 NQO 服务，打印输入 SQL 与两位专家各自的输出
python tools/nqo_probe.py --sql-file queries/job_all/1a.sql --filters hint,join
```

实验日志里的证据：每步记录 `/stats` 计数器的增量（`metrics.nqo_counters`），报告第 3.1 节按臂和阶段汇总 `requests`、`optimized`、`fallbacks`、`expert_HintPlanSel`、`expert_JoinOrder`、`filter_*`。

---

## 2. SELIX 独立运行时

### 2.1 输入状态

SELIX 的离线部分与运行时部分互不相通。

| 层 | 状态 | 内容 | 是否在线 |
|---|---|---|---|
| A | DRL 调参环境（单负载） | 9 维：节点大小、init/max/min 密度、4 个代价权重（均归一化）、吞吐比 | 离线，`selix/src/drl/lit_tuning_env.py` |
| B | DRL 调参环境（多负载） | 6 维：读比例、节点大小、3 个密度、吞吐比 | 离线 |
| C | 基准返回量 | throughput、total_smo、bulk_time、workload_time、键数、节点数、读写次数 | 离线，只有前两项进入奖励 |
| D | 索引内部状态 | 每个数据节点的 shifts、exp_search_iterations、lookups、inserts、resizes 计数，期望代价与经验代价，线性模型系数，全局 `Stats` | 在线，但不暴露给任何学习器，只驱动 ALEX 的规则式结构调整 |
| E | PG 桥接层送进索引的量 | (索引 Oid, 键, 压缩 TID) | 在线，每次操作 |

**结论**：在线运行的 SELIX 没有从数据库读取任何负载状态；它的自适应是 ALEX 继承的规则（比较节点的经验代价与期望代价决定扩容或分裂），调参只在离线完成。

### 2.2 输出动作

| 层 | 动作 | 落地 |
|---|---|---|
| DRL 调参器 | 8 维（单负载）或 4 维（多负载）配置向量：节点大小、三个密度、代价权重 | **原版从未落地**：`SELIXIndexEngine` 建索引时不调任何 setter，PPO 的输出只存在 zip 文件里 |
| 索引内部 | 扩容、分裂（向下/横向）、重训线性模型 | 在线，由规则触发，不受学习器控制 |

雏形为 GA 补的通道：三个 GUC `selix.init_density`、`selix.max_density`、`selix.min_density`，在下一次索引写入时一起校验并生效（见 `patches/neurdb/0001-*.patch`）。"SELIX 独立运行"在实验中对应**静态配置**：`none`/`nqo` 臂用默认 (0.70, 0.80, 0.60)；`selix`/`both` 臂用 `sweep` 阶段选出的单一档位，这相当于 SELIX 自己的离线调参结果。

运行时证据：`nrindex_stats()` 的 13 列，每步由 YCSB 连接读取；报告第 3.2 节列出每个臂在每个阶段生效过的密度、每步结构调整次数、单次操作耗时与索引内存。

---

## 3. 加入 Global Agent 之后

GA 不替换 NQO 和 SELIX 的内部学习器，而是在它们之上选择"用哪个专家"和"用哪组密度"。

### 3.1 GA 的输入状态（9 维）

| 序号 | 字段 | 定义 | 来源 |
|---|---|---|---|
| 1 | cpu_util | 上一步容器 CPU 使用量 / 容器核数 | cgroup `cpu.stat` |
| 2 | olap_share | JOB 总耗时 / (JOB 总耗时 + YCSB 总耗时) | 两个负载驱动 |
| 3 | ycsb_insert_ratio | 插入次数 / (插入 + 点查) | `nrindex_stats()` |
| 4 | nqo_overhead | NQO 推理耗时 / JOB 总耗时 | NQO `/stats` |
| 5 | idx_cost | log(c_idx / c_ref)，SELIX 单次操作耗时相对原版 | `nrindex_stats()` + 参考值 |
| 6 | idx_mem | log(mem / mem_ref)，索引内存相对原版同一步序号 | 同上 |
| 7 | smo_rate | 结构调整次数 / 索引操作数 × 1000 | `nrindex_stats()` |
| 8 | prev_nqo | 当前 NQO 模式编号 / 3 | GA 自己记录 |
| 9 | prev_selix | 当前 SELIX 档位编号 / 2 | GA 自己记录 |

所有分量截断到 [−3, 3]。每一步的观测向量写入步日志的 `obs` 字段；报告第 3.4 节给出各分量在两个阶段的均值与范围，以及若干"观测 → 动作"的示例行。

### 3.2 GA 的输出动作（12 个离散动作）

动作 = NQO 模式 × SELIX 档位。

| NQO 模式 | 实现 | | SELIX 档位 | init / max / min |
|---|---|---|---|---|
| off | `enable_molqo = off` | | dense | 0.85 / 0.95 / 0.75 |
| auto | `molqo.expert_filter = all`（原版） | | default | 0.70 / 0.80 / 0.60（原版） |
| hint | `molqo.expert_filter = hint` | | sparse | 0.50 / 0.60 / 0.40 |
| join | `molqo.expert_filter = join` | | | |

下发方式：NQO 模式通过 `ALTER SYSTEM` + `pg_reload_conf()`，对所有 JOB 客户端的下一条查询生效；SELIX 档位交给 YCSB 连接执行三条 `SET selix.*`。编号见 `gaproto/actions.py`：`action = 模式编号 × 3 + 档位编号`，原版对应 `auto/default` = 4。

### 3.3 奖励

```
r = 0.5·log(q_J / q_J_ref) + 0.5·[log(c_ref / c_idx) − 0.5·log(mem / mem_ref)] − 0.05·switched
```

参考值来自 `baseline` 阶段的原版运行。奖励为 0 表示与原版相同。

---

## 4. 对照臂与"独立运行"的对应关系

| 臂 | NQO | SELIX | 含义 |
|---|---|---|---|
| `none` | off | default | 两个学习组件都不起作用：原生优化器 + 静态默认索引参数 |
| `nqo` | auto | default | NQO 独立运行（= 原版 NeurDB；SELIX 在原版里就是静态默认值） |
| `selix` | off | sweep 选出的档位 | SELIX 独立运行：按它自己的离线调参方式为整个负载选一组参数 |
| `both` | auto | 同上 | 两者各自独立、无协调 |
| `ga` | 每步由策略决定 | 每步由策略决定 | GA 协调两者 |

`nqo`、`selix`、`both` 相对 `none` 的差异回答"各自独立运行用多少资源、有多少收益"；`ga` 相对 `both` 的差异回答"协调是否比各自独立调好再叠加更好"。资源按组件分组（`gaproto/procstat.py`）：NQO 服务进程、NQO 回连数据库的后端、JOB 后端、YCSB 后端、数据库后台进程、实验程序本身。
