# 离线控制流程演示

[开始使用](../docs/构建与运行.md) · [English guide](../docs/quickstart.en.md)

在项目根目录运行：

```bash
python3 demo/run_demo.py --out results/demo
```

依赖 Python 3.10+ 和标准库中的 SQLite 支持，无需 root、C 编译器、模型 API 或 sched_ext 内核。安装了 Make 时，也可运行 `make demo`。

## 演示内容

脚本使用合成输入，调用项目的策略检查、角色权限与持久化运行时：

1. 接受符合约束的配置。
2. 拒绝未知调度器、使用结果信息作输入的配置，以及缺少恢复条件的配置。
3. 拒绝观测角色加载调度器的请求。
4. 写入 SQLite 状态、事件和文件索引。
5. 汇总已有 openEuler 实验记录。

成功时输出 `DEMO PASS`。[归档输出示例](../tests/results/demo-output.txt)

## 查看结果

| 文件 | 用途 |
| --- | --- |
| `results/demo/summary.json` | 运行步骤与各项处理结果 |
| `results/demo/guard_valid_contract.json` | 有效配置的决定 |
| `results/demo/guard_unknown_scheduler.json` | 未知调度器的处理结果 |
| `results/demo/guard_missing_rollback.json` | 缺少恢复条件时的处理结果 |
| `results/demo/runtime/` | 持久化状态与事件 |

指定其他输出目录可使用 `--out /tmp/kernadapt-demo`。

脚本不加载真实调度器，也不测量新的性能。末尾的倍数来自 2026 年 8 月 1 日归档实验，数据批次与论文汇总的差异见[实验结果说明](../docs/benchmarks.md#lockcoop)。

下一步可以阅读[架构](../docs/architecture.md)，或按[构建与运行](../docs/构建与运行.md)准备实际内核工具。
