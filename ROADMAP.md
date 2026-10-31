# 维护方向与参与入口

以下方向围绕现有代码和使用场景展开。具体范围与优先级通过 Issue 讨论，不以日历日期承诺交付。

| 方向 | 可以提交的具体工作 | 相关入口 |
| --- | --- | --- |
| 平台兼容性 | 补充独立机器的内核、工具链、构建与运行记录；说明接口差异 | [环境说明](docs/compatibility.md)、[内核工具构建](scripts/build-kernel.sh) |
| 数据库场景 | 固定负载与对照，报告吞吐、尾延迟和后台工作量；分析不获益的配置 | [LockCoop](src/lockcoop/)、[实验数据](tests/lockcoop/) |
| 工作流场景 | 补充工具等待、进程退出和会话生命周期案例；比较平均与整批完成时间 | [FlowCausal](src/flowcausal/)、[回放基准](src/flowcausal/benchmarks/harness-replaybench/) |
| 控制面接口 | 使状态输入、动作参数、范围与恢复行为更易使用和理解 | [运行时](src/agent/)、[架构](docs/architecture.md) |
| 数据与分析 | 补充图表与逐轮数据对应关系、统计计算说明和独立复现 | [结果说明](docs/benchmarks.md)、[分析脚本](src/analysis/) |
| 文档与交流 | 修正初次使用障碍，翻译模块说明，完善示例与演讲材料 | [文档](docs/README.md)、[英文指南](docs/README.en.md)、[演示材料](presentation/) |

小范围文档或数据说明可直接提交 PR。涉及机制与接口的工作，先用[改进建议](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new?template=feature_request.yml)说明遇到的问题、使用场景和评价方法，便于确定实现范围。
