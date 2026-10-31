# 测试、实验与数据

## 工程检查入口

```bash
make test-all
make verify-results
make demo
```

`tests/run.sh` 执行 29 个控制面检查脚本、FlowCausal 调度逻辑测试和实验数据核算。

## 测试材料

| 结论或功能 | 材料 | 使用方法 |
| --- | --- | --- |
| KernAdapt 配置预测与执行效果 | [实验汇总](kernadapt/current_e1_public.v4.json) | `composition_final` 对应组合预测；`learned_live_final` 对应 600 次决策与执行实验，环境与统计方法见 [KernAdapt 论文](../papers/E2-KernAdapt.pdf) |
| 控制面、执行权限、异常恢复与结果记录 | `../src/agent/tests/` | `make test`；输出见 `results/control-plane-tests.txt` |
| FlowCausal 调度逻辑 | `../src/flowcausal/src/controller/test_flowctl.py` | `make test-all` |
| openEuler 适配与节点 0 主复测 | `lockcoop/records/arm_openeulerpaper53_node0all_no_nest_runs5_results_20260801.md` | 核查环境、启动参数、逐轮表 |
| 140 个逐轮结果与汇总 | `lockcoop/openeuler_node0_runs.csv`、`openeuler_node0_summary.json` | `python3 tests/verify_results.py` |
| 非绑定环境 | `lockcoop/records/arm_openeuler_paper53_unbound_no_nest_runs5_results_20260802.md` | 非绑定环境的独立结果 |
| NUMA/存储双稳态与故障 | `lockcoop/records/*diagnosis*.md` | 查看失败案例与排障过程 |
| owner、锚点、grant 与 scope | `lockcoop/records/eurosys_*.md` | 对照机制改动、计数及消融 |
| FlowCausal 回放与基准 | `../src/flowcausal/experiments/`、`../src/flowcausal/benchmarks/` | 各脚本 `--help`，配合 FlowCausal 报告 |
| 可复跑控制路径 | `../demo/` | `make demo` |

CSV 来自主报告第 7.1 节中 7 个调度器 × 4 个线程档 × 5 轮样本。核验脚本同时比较报告、CSV 和汇总 JSON，确认结果对应同一份记录。更新来源后运行 `python3 tests/verify_results.py --extract` 重新导出。

## openEuler 实验协议

主复测环境为 openEuler 24.03-LTS、ARM64、Linux 6.17.0-lockcoop-scx617，节点 0 的 CPU 0–127。每点 5 轮；预热 10 秒；测量 60 秒；每轮固定数据库重启和 buffer pool 协议。真实复跑入口见 [构建与运行](../docs/构建与运行.md)。

材料包括实验报告、逐轮数据、失败案例、配置和复现脚本。

## 测试记录

本目录的 `results/` 保存测试、构建、演示输出和源码许可证清单。运行时数据默认写入项目根目录 `results/`。

## 论文实验数据

首页数字的文件来源、统计口径与批次差异见[结果说明](../docs/benchmarks.md)。

LockCoop 的 `lockcoop/paper-data/` 保存论文使用的资源域、跨平台、跨层及 mutex 范围实验 CSV。来源批次见文件名和 data_source 字段；机制设计、图表和实验设置见 [LockCoop 论文](../papers/E1-LockCoop.pdf)。

FlowCausal 的 `flowcausal/dependency-turn-20261003/summary.json` 包含逐轮数值、标准差及关联计数；`formal-plan.json`、`ablation-plan.json`、`hrb-corrected-plan.json` 保存运行配置。计划中的 `<workspace>`、`<home>` 为匿名化的实验机路径，重跑时替换为当前源码和二进制位置。当前调度器与依赖变化原型分别在 `src/flowcausal/src/` 和 `src/flowcausal/experiments/prototypes/dependency-turn-20261003/`。

原生基线身份保护可独立运行：

```bash
python3 src/flowcausal/benchmarks/harness-replaybench/tests/test_scheduler_lifetime.py
```
