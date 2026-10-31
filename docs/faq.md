# 常见问题

[文档首页](README.md) · [开始使用](构建与运行.md)

## 需要更换内核才能体验吗？

离线控制流程不需要，只需 Python 3.10+。加载实际 BPF 调度器则需要兼容的 sched_ext、BPF、BTF 和相应权限，详见[环境与兼容性](compatibility.md)。

## 是否必须配置大模型 API？

离线演示不需要。模型策略是控制面的可选输入方式，逐任务调度由内核机制执行。运行模型策略时按对应模块配置本地环境参数。

## `make build` 为什么没有生成 BPF 调度器？

它只检查 Python 语法并编译两个普通 C 微基准。内核调度器使用 `KERNEL_SRC=/path/to/linux-6.17 ./scripts/build-kernel.sh`，需要完整且匹配的内核工具工作树。

## 已安装 kernel headers，为什么仍提示缺少 tools/sched_ext？

构建脚本依赖完整 Linux 源码中的工具目录。请将 `KERNEL_SRC` 指向包含 `tools/sched_ext/Makefile` 和 `tools/lib/bpf` 的专用源码工作树。

## 演示里的 4.82 倍与首页 5.30 倍为什么不同？

演示核算的是 2026 年 8 月 1 日 openEuler 原始批次，256 线程时相对 `scx_simple` 为约 4.82 倍。首页沿用论文汇总的 5.30 倍，使用后续 LockCoop 复测结果。两者的数据入口和批次不同，具体数值及计算见[结果说明](benchmarks.md#lockcoop)。

## FlowCausal 为什么同时报告一项改善和一项增加？

平均完成时间衡量各工作流的平均等待；全部完成时间衡量整批工作结束的时刻。八工作流实验中，平均值改善 7.32%，整批完成时间增加 2.65%。应按自己的目标权衡这两项结果。

## 是否有独立、可直接阅读的代码？

代码、脚本、配置和实验记录均在本仓库。控制面位于 `src/agent/`，两种内核机制位于 `src/lockcoop/` 和 `src/flowcausal/`。外部依赖是 Linux、编译工具链、数据库等上游软件，见[代码索引](代码与脚本索引.md)。

## 报告、PPT 和视频在哪里？

[完整报告](项目文档.md)、[专题报告](../papers/README.md)、[PPT 与讲稿](../presentation/)和[配音视频](../videos/)都有独立入口。视频使用现有演示文稿配音，介绍机制与结果；实际控制流程可以运行离线演示查看。

## 怎样报告问题或贡献平台结果？

打开[问题表单](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new/choose)。构建与运行问题附环境及操作；性能问题附基线、参数和逐轮结果。文档修订可以直接提交 PR。
