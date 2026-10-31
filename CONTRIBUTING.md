# 参与 KernAdapt

[English](CONTRIBUTING.en.md) · [维护方向](ROADMAP.md) · [问题反馈](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new/choose)

欢迎改进文档、提交平台运行记录、复现实验，以及完善控制面和调度机制。贡献不需要从内核代码开始。

## 第一次参与

| 可以做的事情 | 从哪里开始 | 提交时附什么 |
| --- | --- | --- |
| 修正文档或补充英文说明 | [文档目录](docs/README.md) | 原有问题、修改位置和相关命令 |
| 报告构建与运行问题 | [构建说明](docs/构建与运行.md) | 系统、内核、架构、提交版本和操作记录 |
| 分享独立复现 | [实验结果说明](docs/benchmarks.md) | 对照配置、逐轮数据和计算方法 |
| 改进运行时与接口 | [架构](docs/architecture.md) | 具体行为变化、接口及受影响的调用方 |
| 改进锁或工作流调度 | [LockCoop](src/lockcoop/) / [FlowCausal](src/flowcausal/) | 输入事件、状态变化、执行范围和恢复方式 |

## 报告问题

选择[构建与运行](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new?template=bug_report.yml)、[性能与复现](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new?template=performance.yml)或[改进建议](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new?template=feature_request.yml)表单。尽量给出最小可复现操作，并移除日志中的凭据和非公开业务数据。

性能报告需要明确基线是原生 Linux、`scx_simple` 还是某个机制变体；同时记录 CPU/NUMA 范围、负载、重复次数和共存任务影响。没有收益的结果同样有助于理解适用条件。

## 提交修改

1. Fork 仓库，在自己的分支中完成一个明确问题的修改。
2. 在 PR 中解释问题和改动后的行为。涉及较大接口调整时，先用 Issue 说明场景和方案。
3. 更新受影响的使用文档，保留文件级许可证及上游版权。
4. 记录实际执行的检查及结果；如果未运行某项，直接说明。

文档修改检查链接、命令路径、术语与排版即可。Python 控制面或会话逻辑修改可运行 `make test-all`；内核机制修改需要匹配环境下的构建与运行记录。性能结论需要独立的对照测量，不能由逻辑测试推出。

## 机制与数据修改

调度机制说明观测的事件、状态何时失效、额外服务预算、作用范围和失败后的行为。不要把模型策略的决策时延与内核调度路径混在同一指标中。

数据修订保留原有批次、参数和逐轮数值，另行记录更正原因。图表附单位和计算方式。吞吐倍数、配置选择的 regret、效用与完成时间分别解释，来源见[实验结果](docs/benchmarks.md)。

## 评审与认可

维护者根据行为正确性、接口清晰度、实验依据和来源许可评审。修改通过 PR 讨论与提交记录保留，贡献者按实际工作获得署名。项目维护方式见 [GOVERNANCE.md](GOVERNANCE.md)。
