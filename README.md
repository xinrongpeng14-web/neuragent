# neuragent

在 NeurDB 之上构建一个 Global Agent，用强化学习协调 NQO（学习型查询优化器）与 SELIX（学习型索引）等自学习组件。

## 目录

| 路径 | 内容 |
|---|---|
| `WORKLOG.md` | 工作日志：每次总结的内容、每一项的结果与遗留事项 |
| `GlobalAgent_design.md` | 完整方案，v0.3 已按实测结果修订 |
| `GlobalAgent_prototype.md` | 雏形方案：用一次对比实验判断可行性 |
| `GlobalAgent_round2.md` | 第一轮实验结果分析（NQO 为何净负收益、SELIX 档位为何无差别）与第二轮方案 |
| `GlobalAgent_hierarchical.md` | 分层 GA 实验方案 v0.4：SELIX 与 btree 并存于 NQO 的表上，只读长、短查询两组，以查询延迟评价 |
| `TUTORIAL.md` | 操作教程（第一轮）：在另一台机器上从建容器到跑完对比实验 |
| `TUTORIAL_hierarchical.md` | 操作教程（分层方案）：更新补丁、建 SELIX、可行性验证 F1 到 F4 |
| `TUTORIAL_round2.md` | 操作教程（第二轮）：在已有容器上做两项校准并跑完第二轮实验 |
| `NeuralDB_code_analysis.md` | NeurDB 代码分析，重点是 NQO 与 SELIX |
| `CHMAS_paper_vs_code.md` | CHMAS 论文与其代码的对照 |
| `experiment/` | 实验程序：负载驱动、指标采集、Gym 环境、训练、评估、报告；`STATES_AND_ACTIONS.md` 枚举各组件的状态与动作 |
| `deploy/` | 部署脚本：建容器、编译、装载 IMDB、启停服务、安装检查 |
| `patches/neurdb/` | 对 NeurDB 的改动，以补丁形式保存 |
| `prototype/p1/` | SELIX 通路验证：报告、脚本、原始输出 |
| `prototype/p2_p9/` | 前置改动：报告、测试、原始输出 |
| `prototype/r2/` | 第一轮之后的诊断：NQO 决策扫描与计划计时、SELIX 索引层诊断 |
| `scripts/setup_neurdb.sh` | 克隆上游 NeurDB 并应用补丁 |

## 范围规则

本项目的所有改动只存在于这个文件夹和这个仓库里。

- `NeuralDB/` 与 `CHMAS/` 是别人仓库的克隆，被 `.gitignore` 排除，不属于本仓库。
- 对 NeurDB 的改动以补丁形式保存在 `patches/neurdb/`，从不推送到 NeurDB 的仓库。克隆的推送地址已被设为无效值。
- Python 依赖装在项目内的 `.venv/`。
- NeurDB 的许可证保留所有权利，因此含有其源码的补丁文件不在这个公开仓库中，见 `patches/neurdb/README.md`。

## 当前进度

| 步骤 | 状态 |
|---|---|
| P1 SELIX 通路验证 | 完成 |
| P1a 到 P9 前置改动 | 完成；真实 NQO 模型尚未端到端验证 |
| 实验程序 | 完成，已通过单元测试与端到端冒烟测试 |
| 完整方案按实测结果修订 | 完成 |
| 训练、评估、报告脚本；部署脚本与操作教程 | 完成 |
| 第一轮：原版基线、训练、评估 | 完成（2026-10-04，4 核 16 GB）。GA 学到“关 NQO”，协调收益为零，分析见 `GlobalAgent_round2.md` |
| 第二轮：重新设计负载与对照臂 | 前置工作完成（2026-10-06）：按阶段切换查询集、新档位、校准工具、新对照臂、NQO 决策缓存选项；步骤见 `TUTORIAL_round2.md` |
