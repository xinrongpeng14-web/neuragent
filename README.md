# neuragent

在 NeurDB 之上构建一个 Global Agent，用强化学习协调 NQO（学习型查询优化器）与 SELIX（学习型索引）等自学习组件。

## 目录

| 路径 | 内容 |
|---|---|
| `WORKLOG.md` | 工作日志：每次总结的内容、每一项的结果与遗留事项 |
| `GlobalAgent_design.md` | 完整方案，v0.3 已按实测结果修订 |
| `GlobalAgent_prototype.md` | 雏形方案：用一次对比实验判断可行性 |
| `NeuralDB_code_analysis.md` | NeurDB 代码分析，重点是 NQO 与 SELIX |
| `CHMAS_paper_vs_code.md` | CHMAS 论文与其代码的对照 |
| `experiment/` | 实验程序：负载驱动、指标采集、Gym 环境、重置 |
| `patches/neurdb/` | 对 NeurDB 的改动，以补丁形式保存 |
| `prototype/p1/` | SELIX 通路验证：报告、脚本、原始输出 |
| `prototype/p2_p9/` | 前置改动：报告、测试、原始输出 |
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
| 训练与评估脚本 | 未开始 |
| 原版基线、训练、评估 | 未开始，需要至少 8 核 16GB 的机器 |
