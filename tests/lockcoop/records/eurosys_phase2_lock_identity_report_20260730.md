# LockCoop Phase 2：Lock Identity 与状态污染风险报告

本文件聚焦 lock key schema、private futex 身份、地址复用、stale state 和 map pressure。

## Phase 2.1: Lock Key Schema 总体设计

### 设计

key 只包含对象身份所需字段：`{kind, addr, scope_id}`。cgroup 是调度隔离约束，不是锁身份；timestamp 是 freshness，不是 identity generation。

### 实现

共享 `struct lock_key` 被 owner、waiter、nest 三类状态共同使用。`kind` 区分 kernel mutex 与 private futex；结构保持 16 bytes，避免复杂 key 和冗余状态。

### 实验

static/BTF smoke 验证 key 编译进三个实现及组合 object，并禁止共享 header 出现 `cgroup_id`。

### 结论

Phase 2.1 完成。系统不再把所有地址放进一个裸 `u64` 全局命名空间，也没有把调度策略字段混进 identity。

## Phase 2.2: Kernel Mutex 与 Private Futex Keying

### 设计

kernel mutex key 为 `{KERNEL_MUTEX, kaddr, 0}`；private futex key 为 `{PRIVATE_FUTEX, uaddr, tgid}`。private futex 的语义是进程私有同步对象；使用 `tgid` 作为 scope 可以阻断普通多进程同虚拟地址混叠，同时把 hot-path key 压缩到 16 bytes。少见的 `CLONE_VM` 跨 thread-group 共享 private futex 会退化为不共享 anchor，而不是跨地址空间污染。

### 实现

`lock_private_futex_key()` 从 `bpf_get_current_pid_tgid()` 读取 `tgid` 并构造 private futex key。owner resolver 仍通过 CO-RE 读取 current task `mm`，用于验证读取出的 owner TID 与 waiter 属于同一地址空间。

### 实验

same-address parent/child case 在同一 `uaddr` 上产生两个不同 `tgid` scope。static smoke 验证 private key 包含 scope。

### 结论

Phase 2.2 完成。跨进程相同用户虚拟地址不会共享 LockNest/LockBoost 状态。

## Phase 2.3: Shared Futex、PI Futex 与 Requeue 边界

### 设计

shared futex 的真实 key 涉及 inode/offset 或 shared mapping identity，syscall tracepoint 仅靠 `uaddr` 无法等价重建。PI/requeue 也有不同 waiter/owner 语义。与其制造错误“支持”，主线选择计数并 fallback。

### 实现

shared、PI、requeue 各自有计数，不创建 strong owner、不创建跨进程 anchor。当前 map schema只定义 kernel mutex/private futex key kind。

### 实验

默认 runtime 产生 shared/PI/requeue fallback=2,071/4/1；无 map failure、无错误 boost。

### 结论

Phase 2.3 完成。当前选择是明确的安全/开销边界，而不是遗漏：不为极少出现的特殊路径在调度 fast path 重建完整 futex subsystem。

## Phase 2.4: 地址混叠与跨进程污染

### 设计

跨进程地址混叠由 `tgid` scope 解决。cgroup 不进入 key；同一 thread group 的线程即使位于不同 cgroup，仍可能共享一把锁。隔离由 placement 时的 `p->cpus_ptr` 和后续公平性控制承担。

### 实现

LockNest dispatch 前检查 anchor CPU 是否在 task allowed mask，不允许则 default fallback。owner resolver 额外要求 owner/waiter 同 `mm`。

### 实验

runtime 将 parent/child 放入不同 cgroup，仍观察到两个不同 `tgid` scope。static smoke 检查 `cgroup_id` 不属于 key、`bpf_cpumask_test_cpu` 存在。

### 结论

Phase 2.4 完成。identity 与 isolation 分层，避免 cgroup migration 人为切断同一把锁的 handoff stream。

## Phase 2.5: 地址复用与状态生命周期

### 设计

地址复用是真实但属于 advisory-state freshness 风险。没有观测 allocator 或 mutex init/destroy 时，timestamp 不能识别 generation；因此系统不声称解决 free/realloc，而是保证旧状态影响有界且只影响性能 hint。

owner state 在 unlock/acquire 更新，waiter state 在 syscall exit/select_cpu/acquire 清理，anchor state按 inactivity expiry 并被新 wake/release 刷新。LockBoost 不从旧 futex waker cache继承 owner。

### 实现

`last_update_ns + nest_expiry_ns` 控制 stale anchor；select_cpu 发现 stale 后删除并 fallback。LRU 进一步限制无人再次访问的旧 entry。没有 epoch/generation、allocator hook 或伪 reuse detector。

### 实验

controlled benchmark 在同地址反复 destroy/reinit 五轮。最终默认 run 观测 4 次 stale nest，程序正确结束；address reuse 没有触发 owner boost，也没有 map failure。

### 结论

Phase 2.5 完成的是“有界 advisory 影响”，不是 generation tracking。该选择保持 fast path 简单，论文应以此准确表述。

## Phase 2.6: Map Pressure 与多热点锁状态安全

### 设计

`lock_nests` 保持 1,024 项 bounded LRU。超过容量允许 anchor miss/warmup 和收益下降，但不能错误复用其他 key。owner/waiter map update failure 只导致相应动作缺失，系统继续 default scheduling。

### 实现

LockNest 与组合实现均使用 `BPF_MAP_TYPE_LRU_HASH`。所有关键 map update 有 failure counter；select_cpu 在 waiter/nest/CPU mask 任一条件失败时 fallback。

### 实验

runtime 创建 1,152 把 contended mutex，超过 nest capacity。最终 `map_update_fail=0`，benchmark 与 scheduler 均正常退出。LRU eviction 使新热点锁仍可进入，不再出现普通 HASH 满后持续失败。

### 结论

Phase 2.6 的状态安全与 graceful degradation 完成。16/64/256/1024/4096 热锁的吞吐/尾延迟曲线属于后续机制证据，不是 identity 正确性的前置代码修复。

## Phase 2.7: Lock Identity 验证与论文呈现

### 设计

论文用一张 key/boundary 表和一张 controlled correctness 表回答 identity 问题，不宣称内核锁管理器级精确性。

### 实现

CLI 可以打印 key kind、address、scope、anchor source 和 sequence；counter 输出覆盖 owner、placement、fallback、stale、map safety。static smoke 将关键边界固化为回归检查。

### 实验

三 scheduler build、BTF、CLI、static smoke、resolver-off runtime、resolver-on runtime 和 MariaDB 256-thread default regression 均通过。针对本次手写源码、脚本和文档补丁的局部 `git diff --check` 通过；runner 生成的 ledger CSV 有历史行尾空字段，不作为手写补丁质量 gate。历史性能矩阵没有重跑，也不用于证明新实现的性能。

### 结论

Phase 2.7 的实现与轻量验证完成。论文修改时应删除裸 `u64 address`、waker-owner、epoch-generation 等旧叙述，并按本报告的证据边界更新设计和 limitations。

## 最终完成判断

Phase 1/2 的代码机制、轻量 controlled 实验、默认 MariaDB 单 case 回归和每个二级小节报告已经完成。剩余的是论文结论所能支持的范围：

1. 如果论文继续声称数据库 owner accuracy，增加应用侧 ground-truth owner TID 对照。
2. 如果不补 ground truth，就把 LockBoost claim 限定为 selective verified-owner boosting，把普通 futex 贡献表述为 LockNest advisory locality。
3. 论文中单独报告 `owner_offset=8` coverage-only 表，并明确 `counter_underflow guard` 是保护分支触发而不是 map failure。

这些事项不要求重跑性能矩阵。
