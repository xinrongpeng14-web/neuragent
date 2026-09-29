# CHMAS 论文与代码对照分析报告

- 论文：Wang, Xu, Zhang, Ren. *CHMAS: A Coupled Hierarchical Framework for Multi-Agent Reinforcement Learning*. arXiv:2607.19555v1, 2026-07-21
- 代码：https://github.com/EricDmWang/Hierarchy_RL ，分支 `main`，提交 `909490f init`，本地路径 `/home/zhanhao/neuragent/CHMAS`
- 分析日期：2026-09-28
- 说明：`文件:行号` 均相对 `CHMAS/` 目录

---

## 总体结论

论文的核心架构已经实现，但**第二个贡献 AHPG 异步更新协议、耦合系数 λ、战术层参数共享这三项没有实现，奖励常数也与论文不一致**。代码是一个「双层 DQN/DDPG 原型」，而不是论文算法 1 的完整实现。

已实现的部分：双层结构、战略层独占全局状态、9×9 区域指导、每 T=5 步生成指导、战术观测含指导、战术奖励向上累加、全局覆盖奖励、每 agent 独立经验缓冲、400 回合 × 5 种子、图 4 的执行轨迹。

未实现或不一致的部分：λ 耦合系数、每 N_f 回合一次的战略异步更新、α/√k 衰减步长、参数共享、战术奖励与全局奖励的数值、战略层的网络类型。

---

## 一、代码结构与可运行性

仓库只有 47 个文件，核心 Python 约 3800 行，单次 `init` 提交，无历史可追溯。远程 `master` 分支与 `main` 仅差 LICENSE、.gitignore 和 README 一行。

| 目录 | 文件 | 行数 | 职责 | 可运行 |
|---|---|---|---|---|
| `lbf_base/` | `gridworld25_env.py` | 831 | 25×25 网格觅食环境，含访问表、9×9 区域、奖励塑形、渲染 | 是 |
| `lbf_base/` | `test_env.py` | 15 | 随机动作冒烟测试 | 是 |
| `hierarchy_RL/` | `train_hierarchy.py` | 1141 | 4 个战术 DQN + 集中式 critic + 战略 actor-critic 的训练主循环 | 是 |
| `hierarchy_RL/` | `run_hierarchy.py` | 150 | CLI 参数解析 | 是 |
| `hierarchy_RL/` | `run_hierarchy.sh` / `run_hierarchy_seeds.sh` | 60 / 73 | 单种子 300 回合 / 多种子 400 回合 × 种子 1–5 | 是 |
| `hierarchy_RL/exec/` | `run_exec.py` / `run_exec.sh` | 182 / 30 | 加载模型确定性执行，输出帧和 GIF | 是，sh 有硬编码路径 |
| `hierarchy_RL/exec/hierarchy_20250930_005110_exec_*/` | 20 帧 PNG、GIF、CSV、JSON | — | 一次执行产物，对应论文图 4 | — |
| `hierarchy_RL/tools/` | `combine_results.py` / `plot_graphs.py` | 177 / 273 | 多种子聚合与论文图绘制 | 依赖硬编码路径 |
| `madqn/` | `train_madqn.py` / `run_madqn.py` / 两个 `.sh` | 829 / 139 / 103 | 无层级的 MADQN 基线 | 否 |
| 根目录 | `README.md` | 0 | 只有标题 | — |

### 可运行性问题

- **madqn 基线无法运行。** `train_madqn.py:21-26` 导入不存在的 `lbf_llm.mlp_model.SmallTabNet` 和 `expert_rl` 包，硬编码 `/home/dongmingwang/project/Expert_RL`。`:378` 按旧接口 `obs_all, _ = env.reset()` 解包，`:482` 按 5 值解包 `env.step()`，与当前环境的 3 值和 6 值返回不匹配。论文没有报告 MADQN 对比，这是上一个项目的遗留。
- **README 引用的目录不存在。** `hierarchy_RL/README.md` 描述的 `scripted_exec/`、`manual_policy/` 及其示例 JSON 都不在仓库中。
- **训练结果全部被 `.gitignore` 排除。** `**/results/` 被忽略，论文图 2、图 3 背后的 `training_logs.csv`、`strategic_training_logs.csv` 不在仓库中。`plot_graphs.py:135-141` 硬编码了 2025-09-30 的五个运行目录，只能在作者机器上复现。
- **多处硬编码路径。** `exec/run_exec.sh:13`、`tools/plot_graphs.py:135-141`、`madqn/run_madqn.sh:11`、`madqn/train_madqn.py:21,346,829` 都写死了 `/home/dongmingwang`。

---

## 二、论文与代码逐项对照

### 2.1 环境与观测（论文 §VI.A）

| 论文内容 | 代码实现 | 状态 |
|---|---|---|
| 25×25 网格、N=4 agent、协作觅食 | `gridworld25_env.py:25-36`，固定 4 agent、4 个食物、等级 1 | 已实现 |
| 全局状态 s_env ∈ {0,1}^625 记录格子访问，战略层独占 | `visitation_table` 25×25，`_env_obs` 展平后拼 12 维 agent 状态，共 637 维（`:261-281`），只喂给战略网络 | 已实现。取值为 0–4 的 agent 编号而非 0/1；每步标记 3×3 块（`:170-189`） |
| 战术观测 o_i ∈ R^20，含位置、附近资源、战略指导 | `_obs_for_agent`（`:205-256`）输出 20 维：自身 3 + 邻居 3 + 食物 12 + 区域中心 (gx, gy) 2 | 已实现。邻居只含 N_i 中第一个 agent，非全体；食物为固定 3/4 可见映射 |
| 通信图 N_i 时不变 | 固定邻居映射 0↔{1,2}、1↔{0,3}、2↔{0,3}、3↔{1,2}（`:151-155`） | 已实现 |
| 战略动作 a_i^str = (gx, gy) ∈ [4,20]² | `CentralStrategicPolicy` 输出 tanh 后映射 `8x+12`（`train_hierarchy.py:263-268`），`act` 中 clamp 并取整（`:293-300`），经 `set_agent_grid_position` 生效（`gridworld25_env.py:157-168`） | 已实现 |
| 战术动作 a_i^tac ∈ {0,…,5} | `spaces.Discrete(6)`，noop/上/下/左/右/collect（`:314-336`） | 已实现 |

### 2.2 奖励与双向耦合（论文 §II.B、§II.D、§VI.A）

| 论文内容 | 代码实现 | 状态 |
|---|---|---|
| 战术奖励 r_i^tac = r_collect − 0.01 − 0.01·1[区域外] | 食物 +1（`gridworld25_env.py:401-407`），区域外 **−0.1**（`:391-399`），**无**每步 −0.01，截断时额外 **−0.5×剩余食物**（`:430-437`） | 数值不一致。论文报告的战术回报 −80 → −10 与代码的 −0.1 一致（200 步 × 4 agent × 0.1 = 80），论文正文的 0.01 疑为笔误 |
| 全局奖励 R_global = 0.01 × 新访问格数 | `global_reward = 0.1 * newly_visited`（`train_hierarchy.py:925-927`） | 数值差 10 倍 |
| R_str = R_global + λ·Σ r^tac，λ = 0.5 | `interval_total_reward = interval_reward_local_sum + global_reward`（`:928`），无 λ，等价 λ=1 | **未实现**。`--lambda_str` 在 `run_hierarchy.py:88-89` 被直接赋给 `gamma_str`，后者只用于日志中的折扣回报（`train_hierarchy.py:1058-1060`），不进入任何损失 |
| 向下耦合：指导进入观测与奖励 | 观测末两维为 (gx, gy)；区域外惩罚依赖 gx, gy | 已实现 |
| 向上耦合：区间内战术奖励累加进战略奖励 | `interval_reward_local_sum += np.sum(rewards_array)`（`:899`），用原始奖励而非归一化奖励 | 已实现 |

### 2.3 AHPG 算法（论文 §IV，算法 1）

| 论文内容 | 代码实现 | 状态 |
|---|---|---|
| 指导每 T 步生成并在区间内持续 | `if step % k_update == 0: strategic_agent.act(obs_env)`，`k_update=5`（`train_hierarchy.py:860-872`） | 已实现 |
| 算法 1 第 4 行：指导在每个 N_f 回合的 epoch 开始时生成一次 | 指导每 5 步重新生成 | 未实现。论文 §VI.A 又说「epoch 为 T=5 个时间步」，图 3 标 N_f=5，与算法 1 的「N_f 个回合」自相矛盾；代码的 5 步区间对应的是 T |
| 算法 1 第 23–28 行：战略参数每 N_f 回合更新一次 | 每个 5 步区间结束即 `strategic_agent.learn(batch)`（`:922-941`），只要缓冲 ≥ 128 条 | **未实现** |
| 战略步长衰减 η_k^str = α/√k | 战略 actor/critic 用 AdamW 固定学习率 `lr` 与 `2*lr`（`:284-285, 624`），无 scheduler；只有战术 agent 与集中式 critic 有 `ExponentialLR`（`:615-619`） | **未实现** |
| 战术参数每回合更新 | 每 3 步更新一次（`update_every=3`，`:956`） | 更频繁，可接受 |
| 战略层用集中式策略梯度（PPO），实验称集中式 DQN | 确定性 actor-critic（DDPG 风格）：critic 估计 Q(s_env, positions)，actor 最大化 Q（`:227-346`），目标网络软更新 | 结构不同。论文图 3 有「actor loss」，与代码一致，与正文「DQN」不一致 |
| 战术层 MA-DQN，**参数共享**，dueling，double，PER | dueling（`:174-201`）、double（`:402-406`）、PER（`:46-87`）都有；但 4 个 agent 各自独立 `DQNAgent`（`:580-597`） | 参数共享**未实现** |
| 每个战术 agent 独立经验缓冲 D_i，只用局部梯度 | 每 agent 一个 `PrioritizedReplayBuffer`（`:593-597`） | 已实现 |
| 热启动、轮询更新、PL 条件、收敛定理 | 理论假设，代码无对应 | 不适用 |

### 2.4 实验设置与结果（论文 §VI.B、§VI.C）

| 论文内容 | 代码实现 | 状态 |
|---|---|---|
| 400 回合、5 个独立种子 | `run_hierarchy_seeds.sh` 设 `TOTAL_EPISODES=400`、`SEEDS=(1 2 3 4 5)` | 已实现。`run_hierarchy.sh` 默认 300 回合 |
| 图 2：战术 Q 值、回报、critic loss、policy loss | `training_logs.csv` 记录 `avg_q_value`、`return_total`、`critic_loss`、`policy_loss_mean`（`:631-639`），`plot_graphs.py` 绘制 | 已实现，但原始数据不在仓库 |
| 图 3：战略 Q 值、回报、critic loss、actor loss，N_f=5 | `strategic_training_logs.csv` 记录（`:640-646, 1066-1077`） | 已实现，但原始数据不在仓库；N_f=5 实为 k_update=5 步 |
| 图 4：执行轨迹 t=0, 6, 9, 16, 19 | `run_exec.py` 输出 20 帧；`exec/.../frames/step0000.png` 四个苹果在场，`step0019.png` 全部收集且四色区域铺满 | 已实现 |
| 「前 10–20 回合学习延迟反映异步协议」 | 代码无异步协议。延迟来自缓冲阈值：战略缓冲需 128 条区间样本（约 3 回合），战术缓冲需 `min_buffer_before_training=1000` 步（5 回合） | 归因不成立 |
| 「学习到不重叠区域、初始置于角落」 | 由训练结果决定，无显式约束。`step0000.png` 显示初始区域来自 agent 出生位置，非角落 | 未验证 |

---

## 三、其他代码问题

- **集中式 critic 是死组件。** `CentralValueNet` 在 `train_hierarchy.py:600-609` 创建、`:971-996` 训练，但没有任何 agent 读取它的输出，不影响策略。论文图 2(c) 的「tactical critic loss」就是这个不参与决策的网络的损失。
- **战略 actor 无探索。** `act` 输出确定性位置直接执行（`:293-300`），没有动作噪声。策略空间的探索完全依赖战术层 ε-greedy 带来的状态变化。
- **战略日志格式混乱。** `strategic_training_logs.csv` 表头 6 列（`:643-646`），但 `:946-951` 按区间写 10 列，`:1071` 按回合写 6 列，两种行混在同一文件。区间行的 `a_loss_val`、`c_loss_val` 在 `:932-933` 设为 NaN 后从未更新，恒为 NaN。`combine_results.py:61-65` 和 `plot_graphs.py:62` 只按 6 列格式读取。
- **`gamma_str` 只影响日志。** 战略学习用的折扣是 `gamma=0.97`（`:624`），`gamma_str=0.95` 仅用于 `:1059` 计算记录用的折扣回报。
- **exec 产物自相矛盾。** `episode_returns.csv` 三个回合都是 −87.5，对应 200 步全程区域外且未收集食物；但同目录 20 帧显示第 19 步已全部收集。帧文件名 `stepXXXX.png` 与当前脚本的 `epXXX_stepXXXX.png`（`run_exec.py:159`）不同，两者来自不同版本。
- **战术奖励归一化后再裁剪到 [−1, 1]**（`:892-907`），食物 +1 与区域外 −0.1 经运行均值方差归一化后尺度关系会变化。论文未提及。
- **环境 `mode_2` 固定初始状态。** `train_hierarchy.py:511` 用 `mode="mode_2"`，同一 seed 下每回合的 agent 与食物初始位置完全相同，泛化性未测试。
- **无任何单元测试。** 仅 `lbf_base/test_env.py` 一个随机动作冒烟脚本。

---

## 四、结论与建议

若要让代码与论文算法 1 一致，需要补的最小改动：

1. **λ**：在 `train_hierarchy.py:928` 改为 `interval_reward_local_sum * lambda_coef + global_reward`，并新增 `--lambda_coef` 参数，与 `gamma_str` 区分。
2. **N_f 异步更新**：把 `:934-941` 的 `learn()` 从每区间调用改为每 N_f 回合调用一次，指导生成也改为 epoch 级别或明确文档说明 T 与 N_f 的关系。
3. **α/√k 步长**：给 `opt_actor`、`opt_critic` 加 `LambdaLR(lambda k: 1/sqrt(k+1))`，按战略更新次数 k 步进。
4. **参数共享**：4 个 `DQNAgent` 改为共享一个 `policy_net`，或在论文中改为「独立参数」。
5. **奖励常数**：统一论文与代码的 0.01 / 0.1。
6. 删除或接入 `CentralValueNet`；修复战略 CSV 双格式；移除硬编码路径；修复或删除 `madqn/`。

---

## 附录：论文关键符号与代码变量对应表

| 论文符号 | 含义 | 代码变量 | 位置 |
|---|---|---|---|
| T | 指导持续步数 | `k_update` | `run_hierarchy.py:36` |
| N_f | 战略更新间隔（回合） | 无 | — |
| λ | 耦合系数 | 无（`lambda_str` 实为 `gamma_str`） | `run_hierarchy.py:40, 88` |
| γ_str | 战略折扣 | `gamma`（学习）/ `gamma_str`（日志） | `train_hierarchy.py:624, 1059` |
| γ_tac | 战术折扣 | `gamma` = 0.97 | `train_hierarchy.py:461` |
| η_k^str = α/√k | 战略步长 | 固定 `lr`、`2*lr` | `train_hierarchy.py:624` |
| s_env | 全局访问表 | `visitation_table` → `obs_env` | `gridworld25_env.py:62, 261` |
| a_i^str = (gx, gy) | 区域中心 | `agent_grid_positions[i]` | `gridworld25_env.py:59, 157` |
| o_i^tac | 战术观测 | `_obs_for_agent(i)` | `gridworld25_env.py:205` |
| R_global | 覆盖奖励 | `global_reward` | `train_hierarchy.py:927` |
| Σ r^tac | 区间战术奖励和 | `interval_reward_local_sum` | `train_hierarchy.py:899` |
| D_i^tac | 战术缓冲 | `buffers[i]` | `train_hierarchy.py:597` |
| D^str | 战略缓冲 | `strategic_buffer` | `train_hierarchy.py:625` |
