# FlowCausal 源码

本目录保存 FlowCausal 的 `sched_ext` 原型源码。主要组件如下：

| 路径 | 用途 |
| --- | --- |
| `bpf/` | BPF 调度器数据路径，消费 frontier hints、宽度预算和 fallback 状态。 |
| `user/` | 调度器 loader、`flowd` session registrar、`flowmon` 事件监控器。 |
| `controller/flowctl.py` | Python 原型控制器，可用于 dry-run、oracle 标注和受控实验。 |
| `include/flowcausal.h` | BPF 与用户态共享的 map key、value、flag 和 event 定义。 |
| `Makefile` | 与本地 `sched_ext` 构建环境集成的辅助入口。 |

常用查看命令：

```bash
python3 controller/flowctl.py --help
make --dry-run
```

真实加载需要兼容 `sched_ext` 的内核、BPF 工具链和特权权限；评阅源码时可先从 `controller/flowctl.py`、`user/flowd.c`、`user/flowmon.c` 和 `bpf/` 下的 BPF 调度器版本进入。
