# 环境与兼容性

[文档首页](README.md) · [构建与运行](构建与运行.md)

## 按运行方式选择环境

| 运行方式 | 基础要求 | 系统影响 |
| --- | --- | --- |
| 离线控制流程 | Python 3.10+，Python 标准库中的 SQLite 支持 | 在指定目录写入运行记录 |
| 本地微基准构建 | Linux、Python 3.10+、Make、C 编译器和 pthread | 生成 `build/bin/` 下的程序 |
| BPF 调度器构建 | 完整 Linux 源码及匹配的 sched_ext/BPF 工具链 | 向专用内核工具工作树复制源码并构建 |
| BPF 调度器运行 | 提供 sched_ext、BPF、BTF 的运行内核与相应权限 | 在加载期间改变任务调度方式 |
| 数据库性能实验 | MariaDB、Sysbench、数据设备及指定 CPU/NUMA 范围 | 运行数据库与并发负载 |
| 工具工作流实验 | cgroup、会话登记程序和对应回放配置 | 执行指定任务及会话调度 |

## 内核与工具树

源码使用 sched_ext 接口。配置至少涉及 `CONFIG_SCHED_CLASS_EXT`、`CONFIG_BPF`、`CONFIG_BPF_SYSCALL`、`CONFIG_BPF_JIT` 和 `CONFIG_DEBUG_INFO_BTF`，并需满足实际探针与工具的依赖。具体设置以所用内核版本的 [sched_ext 文档](https://docs.kernel.org/6.17/scheduler/sched-ext.html)为准。

归档 openEuler 实验使用 Linux `6.17.0-lockcoop-scx617`。示例构建命令因此以 Linux 6.17 工作树为入口。支持 sched_ext 的内核不等于已兼容项目使用的全部接口；不能仅凭版本号推断兼容 Linux 6.12 或其他版本。

`KERNEL_SRC` 应指向完整源码树，而非 `/usr/include` 或只含 headers 的目录。脚本需要其中的 `tools/sched_ext`、`tools/lib/bpf` 和相关生成文件。

## 实验涉及的平台

| 平台 | 仓库内记录 | 阅读重点 |
| --- | --- | --- |
| ARM64 / openEuler 24.03-LTS | [节点 0 原始批次](../tests/lockcoop/records/arm_openeulerpaper53_node0all_no_nest_runs5_results_20260801.md)与[论文数据](../tests/lockcoop/paper-data/mariadb_arm64_runs5_cases.csv) | CPU 0–127、数据库重启和 buffer pool 协议；区分后续复测 |
| x86-64 | [固定配置数据](../tests/lockcoop/paper-data/mariadb_x86_unbound_fixedseed_runs3_cases.csv)与[资源域实验](../tests/lockcoop/paper-data/mariadb_resource_anchor_x86_runs3_cases.csv) | 全机与节点范围、频率策略、资源域选择 |
| RISC-V | [十轮逐次数据](../tests/lockcoop/paper-data/mariadb_riscv_unbound_runs10.csv)与[汇总](../tests/lockcoop/paper-data/mariadb_riscv_unbound_runs10_cases.csv) | 线程数、重复次数、吞吐、尾延迟和错误记录 |

这些是记录中使用的平台与配置。移植到其他硬件时，应同时确认 CPU 拓扑、可用探针、内核接口、编译目标与负载环境。

openEuler 原始批次记录的工具版本包括 Python 3.11.6、GCC 12.3.1、Clang 17.0.6、Sysbench 1.0.20 和 MariaDB 10.6。它们用于描述该批次，而不是所有场景唯一允许的版本。

## 依赖范围

离线演示不需要模型账户或第三方 Python 包。模型策略、可选框架后端、图表分析和实际数据库实验使用各自依赖，按对应模块说明配置。项目源码、脚本和数据包含在本仓库，完整 Linux 内核与数据库软件由各自上游提供。

[第三方依赖与许可](第三方依赖与工具说明.md) · [集成与测量](integration.md)
