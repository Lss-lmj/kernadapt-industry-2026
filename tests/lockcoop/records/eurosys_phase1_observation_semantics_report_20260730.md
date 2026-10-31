# LockCoop Phase 1：锁观测与 Owner 追踪正确性报告

本文件聚焦锁事件观测语义、owner evidence 和 waiter dependency。

## Phase 1.1: Lock Event Taxonomy 与覆盖边界

### 设计

观测事件按语义而不是按“都与锁有关”归类：kernel mutex acquire/release/slowpath、private futex wait/wake、shared futex、PI futex、requeue。LockNest 与 LockBoost 对同一事件可以有不同的消费规则。

### 实现

共享 helper 统一解析 futex command/private flag。三个 scheduler 都有 private wait、wake、shared fallback、PI fallback 和 requeue fallback 计数。普通 futex 与 kernel mutex 使用不同 key kind。

### 实验

静态 smoke 检查三条实现均包含分类和 fallback。runtime 同时触发 private、shared、PI 和 requeue 路径，计数分别非零。

### 结论

Phase 1.1 完成。当前声明覆盖 kernel mutex 和 ordinary private futex；shared/PI/requeue 被显式观测并保守回退，不被包装成已支持的强证据。

## Phase 1.2: Futex Wait/Wake 语义

### 设计

`FUTEX_WAIT` 说明当前线程可能在某个用户地址阻塞，`FUTEX_WAKE` 说明调用者执行了唤醒；二者都不天然证明 owner。waker 对 LockNest 是合理的近期 handoff CPU hint，对 LockBoost 不是 owner proof。

### 实现

所有 `FUTEX_WAKE` block 只刷新 LockNest anchor/计数，不写 `lock_owners`，也不创建 owner waiter relation。`sys_exit_futex` 清理 waiter candidate，覆盖 `-EAGAIN`、timeout、interrupt 和正常返回。

### 实验

静态 smoke 搜索 wake block，禁止其中出现 owner/write waiter 操作。默认 runtime 中 wake=23,383、owner hit=0，但 locality hit=21,026，证明 placement 与 owner boost 已解耦。

### 结论

Phase 1.2 完成。原实现“waker 即 owner”的语义错误已从 LockNest、LockBoost 和组合实现中移除。

## Phase 1.3: Owner Evidence Model

### 设计

LockBoost 只接受可验证 owner：kernel `mutex_lock` 返回后当前 task 是 owner；private pthread mutex 使用经过本机验证的 glibc/NPTL layout resolver。任意 futex wake 不做 owner 推断。

glibc resolver 是部署 profile，不是通用 futex 语义：当前 x86-64 glibc/NPTL profile 默认 owner offset 为 8；只有确认目标对象为 glibc `pthread_mutex_t`、确认 ABI owner offset、并确认读取出的 TID 属于同一地址空间时才作为 verified owner。`-O -1` 可显式关闭。

### 实现

kernel `mutex_lock()` kretprobe 不再读取不存在的返回值。private resolver 使用 `bpf_probe_read_user` 后通过 `bpf_task_from_pid()` 检查 owner task 与 waiter 的地址空间一致。普通 wake 不进入 owner map。CLI 明确显示 `-O` 默认 8，并允许 `-O -1` 关闭。

### 实验

本机 `/usr/include/x86_64-linux-gnu/bits/struct_mutex.h` 验证 glibc 2.39 x86-64 `__owner` offset 为 8。resolver-off controlled run owner hit=0；resolver-on run owner hit/miss=19,661/1,216，并实际触发 937 次 boost。三次默认 `owner_offset=8` MariaDB coverage-only run 的 pooled private futex owner resolver hit rate 为 20.06%，kernel mutex owner hit rate 为 99.89%，shared futex fallback 为 17，PI/requeue 为 0。MariaDB/Sysbench 256 线程默认 resolver-on 单 case 为 `5025.17 TPS`，p95 `102.97 ms`，p99 `137.35 ms`。

### 结论

Phase 1.3 的机制实现完成。默认路径不存在通用 futex owner 猜测；glibc/NPTL resolver 的 path coverage 和 MariaDB 性能回归已验证。当前论文 claim 应写成 selective verified-owner boosting，而不能写成通用用户态 futex owner tracking。将其推广到其它 libc、架构或数据库前，仍需报告目标 lock-family coverage，不能把 owner hit 直接当成 owner accuracy。

## Phase 1.4: Waiter Dependency 与调度动作的触发条件

### 设计

同一个 waiter record 同时携带 lock key、可选 owner 和 anchor。`owner_valid=false` 时仍可进行 LockNest placement，但绝不增加 LockBoost waiter count。强动作与 advisory hint 因而互不绑架。

### 实现

`waiter_info.owner_valid` 控制 `contended_tasks` 更新；LockNest 只需要 `key + anchor_cpu`。组合 scheduler 在 private owner miss 时增加 `BOOST_BLOCKED_NO_OWNER`，仍保留 placement。CPU placement 前检查 `p->cpus_ptr`。

### 实验

resolver-off run 中 21,339 次 private owner miss、0 次 boost，同时产生 21,026 次 locality hit。resolver-on run 产生 937 次 boost。默认 resolver-on MariaDB 单 case 恢复到 `5025.17 TPS`。静态检查确认 `SCX_SLICE_INF` 只在 `contended_tasks` owner path。

### 结论

Phase 1.4 完成。低证据质量只关闭强调度动作，不会无谓关闭可回退的 locality 优化。

## Phase 1.5: BPF 状态机并发语义

### 设计

调度状态允许近似，但不能无限增长或因为并发更新制造无界 boost。一个 TID 只保留一个 active blocking dependency；覆盖旧 waiter 时先撤销旧 owner count。计数错误最终由 decay 清理。

### 实现

waiter count increment 使用 atomic add；decrement 使用 verifier-friendly、固定四次上界的 CAS。失败/underflow 有计数，boost state 有 200 ms decay。waiter 在 syscall exit、mutex acquire 和 select_cpu 消费时清理。

### 实验

真实 attach 首先暴露无界 CAS verifier rejection，修正为固定上界后通过。最终两次 runtime 的 `waiter_overwrite=0`、`counter_underflow=0`、`map_update_fail=0`，sched_ext 均正常卸载。

### 结论

Phase 1.5 完成。状态机采用低开销、有界近似，不引入事务化 map 或高频 task cache。

## Phase 1.6: 观测正确性微基准

### 设计

controlled benchmark 覆盖 private pthread mutex、same-address alias、address reuse、shared futex、PI futex、requeue/wake-only 和 1,152-lock map pressure。它验证路径与不变量，不用于性能排名。

### 实现

runtime runner 短时 attach `scx_lockcoop`，运行一个 C benchmark，解析最终 counters、同地址 key scope 和 scheduler exit state。`--owner-offset` 可单独验证 glibc resolver，或用 `-1` 验证关闭后的保守退化。

### 实验

resolver-off 和 resolver-on 两次 controlled run 均成功。前者证明 owner miss 不妨碍 LockNest；后者证明经验证 resolver 能进入 LockBoost。shared/PI/requeue 均只产生 fallback。

### 结论

Phase 1.6 的轻量机制实验完成。尚未声称 owner precision 的数值结论；若论文保留真实数据库 LockBoost owner claim，还需把 benchmark 增加应用侧 owner TID ground truth 表。

## Phase 1.7: 数据库覆盖率与论文呈现

### 设计

论文必须分别报告 event coverage、LockNest usable-anchor coverage 和 LockBoost verified-owner coverage。三者不能用一个“owner hit rate”混写。

### 实现

loader 已输出 private wait、owner hit/miss、locality hit/fallback、boost、fallback families 和 map safety counters。历史数据库 coverage runner 可复用，但其旧结果来自过度设计 prototype，不作为当前实现完成证据。

### 实验

历史材料显示 MariaDB 热路径以 futex 为主，二进制也链接 glibc/`std::mutex`，但这不足以证明每个热点 futex 都是 `pthread_mutex_t`。当前默认 `owner_offset=8` 的 coverage-only 结果已经单独整理在 `eurosys_owner_offset8_coverage_report_20260730.md`：三次短跑合计 private futex wait 为 4,608,080，private owner resolver hit/miss 为 924,156/3,683,927，hit rate 为 20.06%；private futex wake 为 5,512,933，只作为 LockNest advisory anchor hint；shared/PI/requeue fallback 为 17/0/0；`map_update_fail=0`，`counter_underflow guard=39`。这说明数据库短跑能稳定产出 event coverage 和 fallback 边界，也说明 MariaDB owner claim 必须保持选择性。

### 结论

Phase 1.7 的观测口径和工具接口完成。数据库 coverage-only run 与默认 resolver-on 性能回归已补上；如果论文继续声称数据库 owner accuracy，仍需增加应用侧 ground-truth owner TID 对照。否则把 LockBoost claim 限定为 verified lock families，把普通 futex 贡献表述为 LockNest locality，并把 `counter_underflow guard` 作为已防护但仍需长时间审计的残余状态机风险。
