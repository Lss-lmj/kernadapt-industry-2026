# LockCoop 三个调度器当前代码修改报告

日期：2026-07-30

本文档说明当前版本三个 lockcoop 调度器的代码状态：

- `scx_locknest`: 只做同锁后继任务的 advisory placement。
- `scx_lockboost`: 只做 verified lock owner 的 owner-progress boost。
- `scx_lockcoop`: 组合 LockNest 与 LockBoost，并把两条证据链显式解耦。

报告重点回答两个问题：我改了什么，以及这些修改在策略和数据结构上意味着什么。

## 1. 总体设计变化

这轮修改的核心不是增加复杂策略，而是修正证据语义：

```text
普通 futex wake        -> 只能作为 LockNest 的近期 anchor hint
kernel mutex owner     -> 可以作为 LockBoost 的 verified owner
glibc pthread owner    -> 经过布局和 mm 校验后，可以作为 verified owner
shared/PI/requeue futex -> 只计数并 fallback
```

因此当前代码把调度动作分成两类：

- LockNest placement 是弱动作，只需要 scoped lock identity 和近期 CPU hint。
- LockBoost boost 是强动作，只能由 verified owner 触发。

这个拆分直接修正了原实现中最危险的问题：把 `FUTEX_WAKE` 调用者当成 owner。当前三个调度器都不再把普通 futex wake 写入 `lock_owners`，也不再用普通 futex wake 创建 waiter-owner 依赖。

## 2. 共享观测层

新增共享 helper：

```text
lock-sched/tools/sched_ext/include/scx/lock_observe.bpf.h
```

### 2.1 新增统一 lock identity

当前所有锁状态都使用：

```c
struct lock_key {
	u64 addr;
	u32 scope_id;
	u32 kind;
};
```

字段含义：

- `addr`: kernel mutex 地址或用户态 futex `uaddr`。
- `scope_id`: kernel mutex 为 0；private futex 为当前 `tgid`。
- `kind`: `LOCK_KEY_KERNEL_MUTEX` 或 `LOCK_KEY_PRIVATE_FUTEX`。

这里选择 `{tgid, uaddr}` 而不是裸 `u64 uaddr`，用于避免普通多进程场景中相同用户虚拟地址互相污染。也没有把 cgroup 放进 identity，因为 cgroup 是调度隔离约束，不是锁对象身份。

### 2.2 Futex 分类

共享 helper 统一解析 futex op：

- wait: `FUTEX_WAIT`, `FUTEX_WAIT_BITSET`
- wake: `FUTEX_WAKE`, `FUTEX_WAKE_BITSET`
- PI: `FUTEX_LOCK_PI`, `FUTEX_LOCK_PI2`, `FUTEX_UNLOCK_PI`, `FUTEX_TRYLOCK_PI`, PI requeue
- requeue: `FUTEX_REQUEUE`, `FUTEX_CMP_REQUEUE`, `FUTEX_WAKE_OP`, PI requeue
- private flag: `FUTEX_PRIVATE_FLAG`

当前只支持 ordinary private futex 的 advisory/owner-resolver 路径。shared futex、PI futex、requeue 只计数并 fallback。

### 2.3 Owner resolver

共享 helper 保留 `lock_current_mm_id()`，用于 glibc pthread owner resolver 校验：

- 从 `pthread_mutex_t.__owner` 读取 owner TID。
- 默认 owner offset 为 8，对应本机 x86-64 glibc/NPTL profile。
- 用 `bpf_task_from_pid()` 找到 owner task。
- 用 CO-RE 读取 owner task 的 `mm`，确认 owner 与 waiter 属于同一地址空间。
- `-O -1` 可以显式关闭该 resolver。

注意：`tgid` 用于 private futex key scope；`mm` 用于 owner TID 校验。二者作用不同。

## 3. `scx_locknest`

代码位置：

- `lock-sched/tools/sched_ext/scx_locknest.bpf.c`
- `lock-sched/tools/sched_ext/scx_locknest.c`

### 3.1 策略定位

`scx_locknest` 现在是纯 LockNest：它只做同锁 handoff placement，不做 owner boost。

策略目标是：

- 发现同一把锁的近期活动 CPU。
- 为该锁建立一个 lock nest anchor。
- 当等待该锁的任务被唤醒时，优先放到 anchor CPU。
- 如果 anchor 过期、不可用、不在任务 cpumask 内，退回默认 `scx_bpf_select_cpu_dfl()`。

它不声称知道普通 futex 的 owner。

### 3.2 主要数据结构

`lock_owners`

- key: `struct lock_key`
- value: `struct lock_owner_info { tid, cpu, source, observed_ns }`
- 只用于 kernel mutex owner/releaser 路径。

`futex_hints`

- key: `struct lock_key`
- value: `struct lock_owner_info`
- 记录 ordinary private futex wake 的近期 waker CPU。
- 它是 anchor hint，不是 owner map。

`waiters`

- key: waiter TID
- value: `struct waiter_info { key, anchor_cpu, source, observed_ns }`
- 记录某个 waiter 之后 select_cpu 时应该尝试的 anchor。

`lock_nests`

- key: `struct lock_key`
- value: `struct lock_nest_info`
- map 类型: `BPF_MAP_TYPE_LRU_HASH`
- max entries: 1024

`lock_nest_info` 包含：

```c
struct lock_nest_info {
	s32 anchor_cpu;
	s32 last_holder_cpu;
	u32 last_holder_tid;
	u32 source;
	u64 last_update_ns;
	u64 handoff_seq;
};
```

LRU 的意义是 map pressure 下安全退化：丢 anchor 只影响 locality，不影响正确性。

`active_mutex_lock`

- key: tid
- value: `struct lock_key`
- 用于把 `mutex_lock` entry 和 return probe 关联起来。

`stats`

- per-CPU array counters。
- 默认只统计 local/global queue。
- `-D` 诊断模式下输出完整 counters 和 active nests。

### 3.3 事件路径

kernel mutex:

- `mutex_lock` enter: 记录 active mutex key。
- `mutex_lock` return: 当前 task 成为 kernel mutex owner，写入 `lock_owners`。
- `__mutex_lock_slowpath`: 若能找到 owner，则记录 waiter，并用 owner CPU 创建/刷新强 anchor。
- `mutex_unlock`: 若当前 tid 符合 owner，则刷新已有 anchor，并删除 owner。

ordinary private futex:

- `FUTEX_WAKE`: 写入 `futex_hints`，并且只刷新已有 anchor；不会创建 owner。
- `FUTEX_WAIT`: 查找 fresh hint 或已有 fresh nest；如果可用，记录 waiter；如果不可用，直接 miss/fallback。
- shared/PI/requeue: 只记录 fallback counters，不创建 owner/anchor。

placement:

- `select_cpu` 查找 waiter record。
- 如果有 fresh anchor 且 anchor CPU 在 `p->cpus_ptr` 内，则把任务插入 `SCX_DSQ_LOCAL_ON | anchor_cpu`。
- 默认 `weak_anchor_queue_limit = ~0U`，即 direct handoff。
- `-q <count>` 可限制 anchor CPU 本地队列长度，作为显式实验参数。

### 3.4 重要修改点

- 删除裸 `u64 lock_addr` identity，改为 `struct lock_key`。
- 引入 `futex_hints`，把 futex wake 从 owner proof 降级为 anchor hint。
- `lock_nests` 改为 LRU hash，避免 1024-entry 热锁压力下错误扩大。
- anchor 创建需要 admission：裸 wake/unlock 默认只刷新已有 anchor；waiter 看到 fresh hint 或 kernel mutex contention 才创建 anchor。
- `select_cpu` 增加 `bpf_cpumask_test_cpu()`，避免把任务放到不允许的 CPU。
- 默认性能模式不加载 `sys_exit_futex` 和 `sched_wakeup` 诊断 tracepoint；`-D` 模式才打开完整诊断。

## 4. `scx_lockboost`

代码位置：

- `lock-sched/tools/sched_ext/scx_lockboost.bpf.c`
- `lock-sched/tools/sched_ext/scx_lockboost.c`

### 4.1 策略定位

`scx_lockboost` 现在是纯 LockBoost：它不改变同锁后继的 CPU placement，只在 verified owner 有 waiter 压力时延长 owner 的运行机会。

策略目标是：

- 缩短锁持有者到达 unlock 的时间。
- 不依赖 ordinary futex wake 的 owner 猜测。
- owner 证据不足时不 boost。

### 4.2 主要数据结构

`lock_owners`

- key: `struct lock_key`
- value: `struct owner_info { tid, cpu, observed_ns }`
- kernel mutex acquire 后写入，unlock 时删除。

`waiters`

- key: waiter TID
- value: `struct waiter_info { key, owner_tid, source, observed_ns }`
- 一个 TID 最多保留一个 active blocking dependency。

`contended_tasks`

- key: owner TID
- value:

```c
struct contention_info {
	u32 waiter_count;
	u32 boosted;
	u64 last_contention_ns;
};
```

这是 LockBoost 的核心状态：按 owner TID 聚合 waiter pressure。

`active_mutex_lock`

- key: tid
- value: `struct lock_key`
- 关联 mutex entry/return。

`stats`

- per-CPU counters，包括 futex owner hit/miss、fallback、boost applied、counter underflow、map failure 等。

### 4.3 Owner 证据路径

kernel mutex:

- `mutex_lock` return 后当前 task 是 owner，写入 `lock_owners`。
- `__mutex_lock_slowpath` 找到 owner 后，增加 owner 的 waiter count。
- `mutex_unlock` 删除 owner。

glibc pthread mutex/private futex:

- 只在 private futex wait 上尝试 resolver。
- 默认 `pthread_owner_offset = 8`。
- `bpf_probe_read_user(uaddr + 8)` 读取 owner TID。
- owner TID 必须非 0、不是 waiter 自己、存在 task，并且 owner task 与 waiter 同 `mm`。
- 校验通过后才调用 `add_waiter()`。

ordinary futex wake:

- 只计数 `STAT_FUTEX_WAKE_HINT`。
- 不写 owner map，不创建 waiter-owner dependency。

unsupported path:

- shared futex、PI futex、requeue 只计数并 fallback。

### 4.4 Boost 策略

默认参数：

- `high_lockboost_threshold = 4`
- `decay_timeout_ns = 200 ms`
- `pthread_owner_offset = 8`

`tick()` 上执行 boost：

- 如果 owner waiter count 为 0，或距离上次 contention 超过 200 ms，删除 `contended_tasks`。
- 如果 `waiter_count >= 4`，设置 `p->scx.slice = SCX_SLICE_INF`。
- 如果 `0 < waiter_count < 4` 且尚未 boost，给 owner 增加一个 `SCX_SLICE_DFL`。

`stopping()` 对 `SCX_SLICE_INF` 做 vtime 防护：如果 slice 大于默认 slice，只按一个默认 slice 计入 vtime，避免 vtime 反向异常。

### 4.5 重要修改点

- 删除 futex wake owner 推断。
- 增加 glibc/NPTL pthread owner resolver，默认 offset 8，`-O -1` 可关闭。
- `mutex_lock` kretprobe 不再读取不存在的返回值。
- waiter count 使用 atomic add；decrement 使用 verifier-friendly 的固定 4 次 CAS。
- waiter overwrite 时先撤销旧 owner count。
- waiter map update 失败时回滚 owner count。
- `sys_exit_futex` 清理 waiter，避免 timeout/interrupted wait 长时间污染。
- loader 支持 `-O owner_offset`，启动日志打印 `owner_offset`。

## 5. `scx_lockcoop`

代码位置：

- `lock-sched/tools/sched_ext/scx_lockcoop.bpf.c`
- `lock-sched/tools/sched_ext/scx_lockcoop.c`

### 5.1 策略定位

`scx_lockcoop` 是组合调度器。当前代码明确把两条证据链分开：

- LockNest chain: advisory anchor evidence。
- LockBoost chain: verified owner evidence。

组合策略不是“只要看到锁事件就同时 placement + boost”，而是：

```text
有 anchor, 无 owner  -> 只做 LockNest placement
有 owner, 无 anchor  -> 只做 LockBoost owner progress
有 anchor, 有 owner  -> placement + boost
都没有             -> default fallback
```

这是当前版本最关键的策略变化。

### 5.2 主要数据结构

`lock_owners`

- key: `struct lock_key`
- value: `struct owner_info { tid, cpu, source, observed_ns }`
- verified kernel mutex owner。

`futex_hints`

- key: `struct lock_key`
- value: `struct owner_info`
- futex waker CPU hint，不是 owner。

`waiters`

- key: waiter TID
- value:

```c
struct waiter_info {
	struct lock_key key;
	u32 owner_tid;
	s32 anchor_cpu;
	u32 source;
	u32 owner_valid;
	u64 observed_ns;
};
```

这里的 `owner_valid` 是组合 scheduler 的关键字段：

- `owner_valid = true`: 允许更新 `contended_tasks`，即允许 LockBoost。
- `owner_valid = false`: 只允许 LockNest placement，不允许 boost。

`contended_tasks`

- key: owner TID
- value: `struct contention_info`
- 只由 verified-owner waiter 更新。

`lock_nests`

- key: `struct lock_key`
- value: `struct lock_nest_info`
- map 类型: `BPF_MAP_TYPE_LRU_HASH`
- max entries: 1024

`active_mutex_lock`

- key: tid
- value: `struct lock_key`

`stats`

- 28 类 counters，包括 owner hit/miss、locality hit/fallback、boost blocked、anchor create/refresh/upgrade、map safety 等。
- 默认只更新 local/global queue counters。
- `-D` 才启用完整诊断。

### 5.3 Event path

kernel mutex:

- `mutex_lock` enter/return 建立 verified owner。
- `__mutex_lock_slowpath` 命中 owner 后：
  - `add_waiter(... owner_valid=true ...)`
  - 创建/刷新 strong anchor
  - 增加 owner hit counter
- `mutex_unlock` 刷新已有 anchor，删除 owner。

private futex wait:

1. 构造 `{PRIVATE_FUTEX, uaddr, tgid}` key。
2. 清理过期 hint/nest。
3. 若有 fresh futex hint，则用 waker CPU 创建/刷新 weak anchor。
4. 尝试 glibc pthread owner resolver。
5. 分三种情况：
   - `owner_tid != 0`: 记录 waiter，`owner_valid=true`，允许 LockBoost。
   - `owner_tid == 0 && anchor >= 0`: 记录 waiter，`owner_valid=false`，只允许 LockNest。
   - `owner_tid == 0 && anchor < 0`: 不记录 waiter，只计 owner miss / boost blocked。

private futex wake:

- 写入 `futex_hints`。
- 只刷新已有 anchor，不创建 owner，不创建 waiter-owner relation。

unsupported futex:

- shared/PI/requeue 只计数并 fallback。

### 5.4 Placement path

`select_cpu()`：

- 查找 waiter record。
- 复制后立即 `remove_waiter()`；如果 `owner_valid=true`，会 decrement owner waiter count。
- 检查 waiter record 是否过期。
- 查找 nest；如果 nest stale，则删除并 fallback。
- 如果 anchor CPU 不在 `p->cpus_ptr`，fallback。
- 默认 direct handoff 到 `SCX_DSQ_LOCAL_ON | anchor_cpu`。
- 如果设置了 `-q`，则只有 anchor queue 未超过 limit 时才 direct handoff。

### 5.5 Boost path

与 `scx_lockboost` 相同：

- `waiter_count >= 4`: `SCX_SLICE_INF`
- `0 < waiter_count < 4`: 增加一个默认 slice
- 200 ms decay 后清理
- vtime stopping 防护避免无限 slice 导致 vtime 异常

### 5.6 Diagnostic path

loader 支持：

- `-D`: 打开完整 counters、active contended owners、active lock nests dump。
- 默认性能模式：
  - 不加载 `sys_exit_futex`
  - 不加载 `sched_wakeup`
  - 只读 local/global queue counters

这个修改是为了减少默认性能路径的 BPF tracepoint 和用户态 map dump 开销。代价是默认模式下 timeout/interrupted futex waiter 的显式 syscall-exit cleanup 不启用，依赖 select_cpu consumption、decay 和 stale expiry 做有界退化。诊断实验需要 `-D`。

### 5.7 重要修改点

- 将 LockNest 与 LockBoost 的证据链解耦。
- `waiter_info.owner_valid` 控制强动作，不让 weak futex evidence 触发 boost。
- `futex_hints` 只服务 placement。
- glibc/NPTL owner resolver 默认启用 offset 8。
- default direct handoff 恢复；`-q` 只作为显式实验参数。
- anchor map 使用 LRU。
- placement 统一检查 cpumask。
- 默认关闭诊断 tracepoint 和大 map dump。

## 6. 三个调度器之间的关系

| 调度器 | 做 placement | 做 boost | 普通 futex wake 是否 owner | private futex key | owner resolver |
|---|---:|---:|---:|---|---|
| `scx_locknest` | 是 | 否 | 否，只是 anchor hint | `{uaddr,tgid,kind}` | 无 |
| `scx_lockboost` | 否 | 是 | 否，只计数 | `{uaddr,tgid,kind}` | 默认 offset 8，可 `-O -1` 关闭 |
| `scx_lockcoop` | 是 | 是 | 否，只是 anchor hint | `{uaddr,tgid,kind}` | 默认 offset 8，可 `-O -1` 关闭 |

`scx_lockcoop` 不是简单把两个旧实现拼起来，而是在 waiter record 中显式保存 `owner_valid` 与 `anchor_cpu`，让 placement 和 boost 可以独立发生。

## 7. 与原始版本相比的主要代码修改

### 7.1 策略层

- 从“锁事件触发统一策略”改为“证据强度匹配调度动作”。
- 普通 futex wake 不再被视为 owner。
- LockNest 可消费 weak hint；LockBoost 只能消费 verified owner。
- shared/PI/requeue futex 明确 fallback，不伪装成支持。
- 默认性能路径恢复 direct handoff，不再引入 queue limit。
- queue limit、诊断模式、owner offset 都变为显式参数。

### 7.2 数据结构层

- 新增共享 `struct lock_key`。
- 所有 owner、waiter、nest map 都迁移到 typed key。
- 新增 `futex_hints` map，隔离 waker hint 与 owner map。
- `lock_nests` 改成 LRU hash，容量仍是 1024。
- `waiter_info` 在组合调度器中新增 `owner_valid`。
- `contention_info` 使用 `waiter_count / boosted / last_contention_ns`。
- `stats` 扩展为多类 counters，并用 `diagnostic_mode` 控制默认开销。

### 7.3 并发与清理层

- 一个 TID 只保留一个 active waiter dependency。
- waiter overwrite 时先撤销旧 dependency。
- owner waiter count 用 atomic add 和固定上界 CAS decrement。
- map update 失败时回滚 owner count。
- boost decay 防止 waiter count 残留导致永久 boost。
- stale nest 在 wait/select_cpu 时删除并 fallback。

### 7.4 用户态 loader 层

- `scx_locknest` 增加 `-D`、`-q`，默认不加载诊断 tracepoints。
- `scx_lockboost` 增加 `-O`，日志打印 owner offset。
- `scx_lockcoop` 增加 `-D`、`-q`、`-O`，默认不加载诊断 tracepoints。
- loader 输出 key kind/scope/source/seq，方便 artifact 和 reviewer 检查 identity。

## 8. 当前验证结果

已通过：

- `make -C lock-sched/tools/sched_ext scx_locknest scx_lockboost scx_lockcoop`
- `python3 scripts/lockcoop_phase12_smoke.py`
- 针对本次手写文件的局部 `git diff --check`
- 当前 `/sys/kernel/sched_ext/state = disabled`

MariaDB/Sysbench 256 线程单 case 回归：

| 配置 | 结果 |
|---|---|
| resolver-off final stable rerun | `4092.39 TPS`, p95 `130.13 ms`, p99 `176.731 ms` |
| default resolver-on, owner offset 8 | `5025.17 TPS`, p95 `102.97 ms`, p99 `137.35 ms` |

这说明 Phase 1/2 正确性修复本身没有造成当前 MariaDB 主线性能裂化。相反，默认启用经过验证的 glibc/NPTL owner resolver 后，性能恢复并超过前一轮单 case 对照。

## 9. 当前边界

这些不是遗漏，而是当前代码有意选择的边界：

- 地址复用没有 generation tracking。timestamp/expiry 只能提供 freshness，不证明 free/realloc generation。
- shared futex 不重建完整内核 `futex_key`，只 fallback。
- PI futex/requeue 不纳入 ordinary wait/wake 语义。
- glibc owner offset 8 是当前 x86-64 glibc/NPTL profile；跨 libc、跨架构需要重新验证或用 `-O -1` 关闭。
- `scx_locknest` 和 `scx_lockcoop` 默认性能模式关闭 `sys_exit_futex` 诊断 probe；需要完整 cleanup/counter coverage 时使用 `-D`。
- `SCX_SLICE_INF` 仍属于 Phase 3B 要继续处理的 boost policy 问题；Phase 1/2 只保证它由 verified owner 触发。

## 10. 后续进入 Phase 3 前的结论

Phase 1/2 当前代码已经把 reviewer 最核心的两个问题分开处理：

1. owner 追踪正确性：普通 futex wake 不再是 owner；LockBoost 只用 verified owner。
2. lock identity 污染：private futex 不再用裸用户地址，而是 `{uaddr,tgid,kind}`。

下一步 Phase 3 不应该再改证据语义，而应在现有正确性地基上分别做：

- LockNest: anchor overload fallback、dynamic expiry、multi-hot-lock anchor spreading。
- LockBoost: bounded boost、boost budget、dynamic waiter threshold、fairness guard。
