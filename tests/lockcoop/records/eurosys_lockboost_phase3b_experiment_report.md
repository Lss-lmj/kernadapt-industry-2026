# Phase 3B：LockBoost 自适应 Boost 控制实验报告

日期：2026-07-31

最终实现版本：fast-progress guard + active-boost revoke 修复后的版本

最终结果根目录：

`<workspace>/results/phase3b_lockboost_20260731_final`

公平性轮转结果根目录：

`<workspace>/results/phase3b_lockboost_20260731_final_fair_interleaved`

短锁固定拓扑结果根目录：

`<workspace>/results/phase3b_lockboost_20260731_fast_guard`

## 1. 结论

Phase 3B 的代码实现、性能验证、机制证据、参数扫描和混合负载实验已经完成。论文主线推荐使用：

```text
scx_lockcoop -O 8 -A --boost-profile adaptive
```

最终 adaptive 配置在 MariaDB 256 线程端到端测试中达到 `5404.45 TPS`，p99 为 `126.41 ms`。相对 Phase 3A-3 的历史基线 `5394.09 TPS / 124.97 ms`，吞吐提高 `0.19%`，p99 增加 `1.15%`，满足预先设定的 `TPS 不下降超过 3%`、`p99 不上升超过 5%` 验收条件。

在同序位交错的 bounded/adaptive 正式复测中，adaptive 相对 bounded 的吞吐提高 `3.43%`，p99 降低 `4.72%`。在 MariaDB + cyclictest 混合负载中，adaptive 相对 bounded 的 MariaDB 吞吐下降 `0.82%`、数据库 p99 增加 `9.67%`，但 victim p99/p999 分别降低 `11.5%/5.0%`；相对 `SCX_SLICE_INF`，adaptive 的吞吐提高 `4.82%`、数据库 p99 增加 `3.50%`，victim p99/p999 分别降低 `9.5%/3.2%`。

因此，Phase 3B 支持的论文结论是：

1. `SCX_SLICE_INF` 不应作为默认部署策略。
2. confidence gate、bounded grant、task/lock/cgroup budget 和 long-CS guard 可以在不损害 OLTP 主性能的情况下限制无界 boost 的干扰。
3. adaptive 的收益是 workload-dependent 的。它在真实 mixed OLTP 中同时改善主负载尾延迟和 victim 尾延迟，但在合成 owner-preempt 微基准中不能保证所有 percentile 都优于 inf。
4. adaptive 应被描述为“有界 owner-progress 控制器”，而不是对所有工作负载都提供无条件吞吐提升的策略。

## 2. 实现范围

本阶段修改并同步维护以下文件：

- `lock-sched/tools/sched_ext/scx_lockcoop.bpf.c`
- `lock-sched/tools/sched_ext/scx_lockcoop.c`
- `lock-sched/tools/sched_ext/scx_lockboost.bpf.c`
- `lock-sched/tools/sched_ext/scx_lockboost.c`
- `scripts/run_phase3b_lockboost_evidence_closure.py`

`scx_lockcoop` 是论文主调度器，`scx_lockboost` 是 LockBoost-only 消融调度器。两个调度器的 boost-control 逻辑同步实现。`lock-monitor` 没有修改。

最终版本还包含两个在验证过程中发现并修正的控制错误：

- active revoke 修复：unlock 只撤销当前仍处于 `boosted` 状态的 boost，不再因为 `last_boost_ns` 非零而在后续每次 unlock 重复清零 slice。
- fast-progress guard：adaptive 根据 per-lock unlock interval EWMA 识别已经高速自进展的锁；当 unlock interval 小于 `target_delay / 8` 时跳过 boost，避免零长度或极短 critical section 被反复误 boost。

## 3. 控制器设计

### 3.1 Confidence gate

owner confidence 分为四档：

| 等级 | 含义 | 强 boost 权限 |
|---|---|---|
| `CONF_NONE` | unsupported 或没有 owner | 不 boost |
| `CONF_WAITER_ONLY` | 只有 waiter 证据 | 不 boost |
| `CONF_PROBABLE` | owner hint 可用但不够强 | 最多 short boost，且受 gate 限制 |
| `CONF_VERIFIED` | kernel mutex owner 或 validated pthread owner | 可进入 bounded/adaptive |

默认 gate 为 `CONF_VERIFIED`。普通 futex wake 不被解释为 owner；它可以提供 LockNest 的 advisory locality hint，但不能单独触发 LockBoost owner-progress。

### 3.2 五种 profile

| profile | 作用 | 最终用途 |
|---|---|---|
| `none` | 只记录 waiter/owner，不改变 slice | 机制消融 |
| `short` | 短时间片推进 | 低风险对照 |
| `bounded` | grant/cap 有界 | 当前安全静态基线 |
| `adaptive` | confidence、waiter、delay、budget 和 unlock interval 联合控制 | 论文主候选 |
| `inf` | 显式允许 `SCX_SLICE_INF` | 风险对照，不部署 |

`inf` 只有同时指定 `--boost-profile inf --allow-inf` 才能启用。默认路径不会意外使用 `SCX_SLICE_INF`。

adaptive 默认值为：

| 参数 | 值 |
|---|---:|
| 初始 waiter threshold | 4 |
| threshold 范围 | 2--16 |
| grant | 1000 us |
| cap | 4000 us |
| budget window | 100 ms |
| task/lock/cgroup budget | 20%/40%/40% |
| target owner delay | 500 us |
| max continuous boost | 5000 us |
| confidence gate | verified |
| fast-progress cutoff | target delay 的 1/8 |

### 3.3 状态表

`contended_tasks` 按 owner TID 保存：

- `waiter_count`
- `active_key`
- `owner_confidence`
- `adaptive_threshold`
- `last_boost_ns`、`boost_start_ns`
- `cooldown_until_ns`
- `owner_delay_ewma_ns`
- `unlock_interval_ewma_ns`
- task-level budget token
- `boost_revoke_pending`
- 最近 boost 类型和拒绝原因

`boost_lock_states` 按 `lock_key` 保存：

- lock-level token budget
- waiter fan-in EWMA
- unlock interval EWMA
- boost count
- waiter-unblocked count
- boost efficiency EWMA

`boost_cgroup_states` 按 cgroup id 保存 cgroup-level token budget、boost count 和 deny count。Phase 3B 实现了按 cgroup 限制 boost 并记录执行与拒绝次数，用于预算控制实验，尚不具备完整的生产级资源隔离能力。

主要 map 容量为：`contended_tasks/waiters/owner maps=10240`，`lock_nests/boost_lock_states=1024`，`boost_cgroup_states=4096`。

### 3.4 Token budget

对 task、lock 和 cgroup 分别维护 token bucket：

```text
cap_i = window * budget_pct_i / 100
tokens_i(t) = min(cap_i, tokens_i(t0) + cap_i * elapsed / window)

allow_boost =
    task_tokens  >= grant
 && lock_tokens  >= grant
 && cgroup_tokens >= grant
```

grant 后同步扣减三个 bucket。任一 bucket 不足，boost 被拒绝并记录 task/lock/cgroup deny 原因。

### 3.5 Threshold 和 EWMA

所有 EWMA 使用 `alpha=1/8`：

```text
EWMA_new = EWMA_old + (sample - EWMA_old) / 8
```

adaptive threshold 被限制在 `[2, 16]`：

- budget debt 或 long-CS deny：threshold 上调。
- owner runnable delay 高、waiter fan-in 足够、且近期有 waiter 被释放：threshold 下调。
- waiter 少且 owner delay 低：threshold 上调。
- 同一锁的 unlock interval 小于 `target_delay/8`：直接跳过 boost；这是 fast-progress guard。

### 3.6 Long critical-section guard 和 revoke

连续 boost 超过 `max_continuous_boost_ns` 后进入 cooldown；cooldown 期间只允许 normal slice 或 short boost。unlock 或 waiter 清零后设置 revoke 标记，下一次 tick 清除 stale boost。

最终实现中的 revoke 条件是：

```text
if (info->boosted)
    revoke_active_boost()
```

而不是依据历史上是否曾经 boost。这个区别避免一个 owner 在第一次 boost 后，每次后续 unlock 都被重复清零 slice。

## 4. 实验环境和方法

硬件和内核：

| 项目 | 配置 |
|---|---|
| CPU | 2-socket Intel Xeon Platinum 8375C |
| Socket/core/thread | 2 / 64 / 128 logical CPUs |
| NUMA | 2 nodes |
| SMT | enabled |
| Kernel | Linux `6.17.0-35-generic` |
| NUMA CPU | node0 `0-31,64-95`；node1 `32-63,96-127` |

性能测量关闭 diagnostic-only futex-exit 和 sched-wakeup probes；机制阶段使用 `--stats-only` 采集 scheduler counters。每个样本都检查：

- benchmark return code 为 0；
- scheduler 无 `runnable task stall` 或 `scx_bpf_error`；
- 最终 `/sys/kernel/sched_ext/state=disabled`；
- 必要时检查 map update、underflow 和 fallback counters。

最终数据集包含：

| 阶段 | 样本数 |
|---|---:|
| build/attach smoke | 10 |
| MariaDB bounded/adaptive 交错正式测试 | 6 |
| 机制 coverage | 5 |
| 参数敏感性 | 50 |
| fairness 轮转测试 | 50 |
| MariaDB + cyclictest mixed | 9 |
| 467-thread 固定 64 CPU 短锁交错 | 15 |
| 短锁 counters | 3 |

此前的大矩阵 `392` 个微基准样本、`24` 个 LockBoost 消融样本、`30` 个初始 fairness 样本和 owner-disabled/PI/requeue/shared runtime smoke 被保留作为历史对照；最终结论优先使用 fast-guard 之后的上述数据集。

## 5. 性能结果

### 5.1 MariaDB 正式交错复测

Phase 3A-3 历史基线：`5394.09 TPS / 124.97 ms p99`。

最终版本按 `bounded, adaptive, adaptive, bounded, bounded, adaptive` 顺序执行，每个 profile 三次，避免 profile 分组导致时间漂移。

| profile | TPS mean | TPS CV | p99 mean | p99 CV |
|---|---:|---:|---:|---:|
| bounded | 5225.16 | 10.10% | 132.67 ms | 15.19% |
| adaptive | **5404.45** | 3.67% | **126.41 ms** | 5.45% |

相对历史基线：

| 比较 | TPS | p99 |
|---|---:|---:|
| adaptive vs Phase 3A-3 | +0.19% | +1.15% |
| adaptive vs bounded | +3.43% | -4.72% |

adaptive 通过性能验收。bounded 的较大 CV 说明同一机器上的数据库运行仍有明显波动，因此不能从一次 bounded 高值推断 adaptive 退化。

### 5.2 短锁和极短 critical section

467 threads 固定到 64 CPU，`cs=0`，profile 按轮转顺序执行五次：

| profile | ops/s | CV | lock-wait p99 |
|---|---:|---:|---:|
| none | 4.002M | 18.09% | 4915 us |
| bounded | 3.418M | 20.45% | 4915 us |
| adaptive | **3.962M** | 14.58% | 4915 us |

adaptive 相对 none 低 `0.99%`，在预设 3% 保护线内；相对 bounded 高 `15.9%`。更重要的是 counters 显示：

| profile | throughput | boost applied | budget deny | owner-delay count |
|---|---:|---:|---:|---:|
| none | 4.282M ops/s | 0 | 0 | 0 |
| bounded | 3.845M ops/s | 143,093 | 0 | 0 |
| adaptive | 4.555M ops/s | 0 | 0 | 1,294,730 |

fast guard 之后的 adaptive `boost_applied=0` 结果表明，极短锁已被识别为自身高速前进路径，不再反复触发 owner boost。100 us critical section 的 adaptive throughput 与 none 相近，说明 guard 没有把一般短锁误判为完全不需要调度推进。

### 5.3 Mixed OLTP + victim

MariaDB 256 threads 预热后，与 8-thread cyclictest 同时测量 30 秒。最终版本结果：

| profile | MariaDB TPS | DB p99 | victim p99 | victim p999 | victim max |
|---|---:|---:|---:|---:|---:|
| bounded | 5641.83 | 224.91 ms | 197.7 us | 382.0 us | 1146.3 us |
| adaptive | 5595.68 | 246.67 ms | **175.0 us** | **363.0 us** | 1668.3 us |
| inf | 5338.26 | 238.32 ms | 193.3 us | 375.0 us | 1112.0 us |

adaptive 与 bounded 的差异是一个明确的控制取舍：主负载 TPS 低 `0.82%`、DB p99 高 `9.68%`，但 victim p99/p999 分别低 `11.5%/5.0%`。相对 inf，adaptive 主负载 TPS 高 `4.82%`，DB p99 高 `3.50%`，victim p99/p999 低 `9.5%/3.2%`。

victim max 对少数极端样本很敏感，不能作为唯一公平性指标。adaptive max 高于 inf，说明有界控制并不提供最坏情况下的严格 max-latency 保证。

MariaDB 和 victim 的 cgroup 证据：

```text
MariaDB: /system.slice/mariadb.service
victim:  /user.slice/user-1000.slice/session-4142.scope
```

三种 profile 的 cgroup path 均不同，`cpu.stat` 的 `nr_throttled` 均为 0。该结果证明了 cgroup identity 和 accounting 可观测，但不等于完整的生产级多租户隔离保证。

## 6. 机制证据

最终 adaptive 的 20 秒 MariaDB stats-only 样本：

| 指标 | 值 |
|---|---:|
| futex private wait | 5,277,736 |
| futex owner hit / miss | 2,102,506 / 3,175,230 |
| verified confidence | 2,104,979 |
| waiter-only confidence | 3,101,880 |
| waiter hit / miss | 5,206,858 / 572 |
| locality hit / fallback | 4,009,340 / 9,584,043 |
| anchor create / refresh | 306,013 / 10,257,770 |
| stale nest | 162,039 |
| map update fail | 23,397 |
| counter underflow | 19 |
| boost applied | 524 |
| short / bounded / adaptive boost | 17 / 489 / 18 |
| active unlock revoke | 511 |
| waiters unblocked after boost | 10,737 |
| budget denied | 0 |
| long-CS denied | 21 |
| owner delay samples / average | 28,020 / 446 us |
| threshold up / down | 14,600 / 48 |

几个结论：

1. 普通 private futex 中只有约 `39.8%` 的观测事件能得到 verified owner；其余事件仍可供 LockNest 做 locality hint，但不会被 LockBoost 强 boost。
2. adaptive 实际只施加 `524` 次 boost，却释放 `10,737` 个 waiter，约 `20.5 waiters/boost`，说明控制器不是对每个 waiter 都执行 boost。
3. `511` 次 active revoke 与 `524` 次 boost 同量级，验证了 stale revoke 修复；修复前曾出现 `5,970 boost : 175,674 revoke` 的异常比例。
4. threshold 主要上调，说明 MariaDB workload 下 adaptive 会主动回到保守侧；这也是它在短锁路径不持续 boost 的原因。
5. stats-only diagnostic sample 有 `23,397` 次 map update fail 和 `19` 次 counter underflow。它们占高频 waiter 事件的比例不高，但说明机制 counters 不是零丢失的完整 trace。论文应将其写成 evidence lower bound，并把 map-level drop attribution 放入 Phase 4 instrumentation 工作，而不能声称所有观测事件都被精确记录。

## 7. 公平性和风险边界

公平性使用 5 轮轮转顺序，避免 profile 顺序和机器漂移混淆。结果如下：

| critical section | profile | throughput | Jain | victim p99 | victim p999 |
|---:|---|---:|---:|---:|---:|
| 2 ms | bounded | 326.2 ops/s | 0.9672 | 29.2 us | 48.6 us |
| 2 ms | adaptive | 358.2 ops/s | **0.9805** | 28.6 us | 54.4 us |
| 2 ms | inf | 683.1 ops/s | 0.8906 | 27.0 us | 48.8 us |
| 100 ms | bounded | 119.1 ops/s | 0.9919 | 15.0 us | 22.4 us |
| 100 ms | adaptive | 118.5 ops/s | **0.9928** | 14.8 us | 30.2 us |
| 100 ms | inf | 122.2 ops/s | 0.9923 | 15.2 us | 27.2 us |

这张表不支持“adaptive 在合成 workload 的每个 percentile 上都优于 inf”。它支持更窄但更可信的结论：

- 2 ms 下 adaptive 的 Jain fairness 从 inf 的 `0.8906` 提升到 `0.9805`，并把 owner-progress 吞吐限制在有界范围内。
- 100 ms 下各 profile 的 Jain 接近，但 adaptive 的 max/p999 仍有长尾波动。
- 真实 mixed OLTP 中 adaptive 同时优于 inf 的主负载和 victim p99/p999；这应作为部署意义上的主要公平性证据。

Linux default 在该合成 owner-preempt workload 上吞吐远高于所有 LockCoop profile，但它改变了 workload 的调度语义，不能被用作 LockBoost profile 间公平性的同等服务水平 baseline。报告和论文都不应声称 LockCoop 在该微基准上全面击败 Linux default。

## 8. 参数敏感性

最终扫描包含 25 个一因子点、每点两次重复：

- threshold 粗扫：`2,4,8,16`
- grant 粗扫：`250,500,1000,2000 us`
- cap 粗扫：`1000,2000,5000,10000 us`
- budget：`5%,10%,20%`
- threshold 细扫：`3,5,6,10,12`
- grant 细扫：`750,1500 us`
- cap 细扫：`3000,4000,8000 us`

代表性结果：

| 轴 | 值 | throughput | CV |
|---|---:|---:|---:|
| threshold | 2 | 950.1 | 7.39% |
| threshold | 4 | 648.8 | 24.90% |
| threshold | 8 | 689.6 | 1.66% |
| threshold | 16 | 673.1 | 1.26% |
| threshold fine | 3 | 737.9 | 6.81% |
| threshold fine | 5 | 843.2 | 10.20% |
| grant | 1000 us | 755.6 | 25.84% |
| grant | 2000 us | 719.2 | 2.68% |
| grant fine | 1500 us | 796.4 | 3.08% |
| cap | 4000 us | 772.3 | 4.31% |
| budget | 20% | 790.1 | 0.27% |

结果说明：

1. 参数空间存在 workload interaction，不能从两次重复的最高点自动选择默认值。
2. `T=4` 的静态点本身不是所有微基准中的最佳点，但它处于控制器可以向上或向下调整的中间位置，并且端到端正式结果通过门槛。
3. `grant=2000 us`、`threshold=5` 等表面高点伴随较高 CV，不应作为论文默认。
4. `cap=4000 us` 和 task/lock/cgroup `20/40/40%` 在性能、稳定性和 fairness 之间提供了可解释折中。
5. 当前 threshold 的合法范围是 `[2,16]`；自动调优不会产生 `18`，这是有意的 bounded search space，而不是连续参数优化器。

## 9. 代码和实验完整性

最终 build：

```text
make -C lock-sched/tools/sched_ext scx_lockcoop scx_lockboost
```

结果：成功，`-Wall -Werror` 通过。

最终 smoke：

- `scx_lockcoop`: none/short/bounded/adaptive/inf 全部 attach/detach 成功。
- `scx_lockboost`: none/short/bounded/adaptive/inf 全部 attach/detach 成功。
- inf without `--allow-inf` 的拒绝路径已验证。
- 所有最终 runner 结束时 `/sys/kernel/sched_ext/state=disabled`。
- `lock-monitor` 未修改。

## 10. 论文 claim 边界

可以写：

> LockBoost uses verified-owner confidence, bounded time-slice grants, and online task/lock/cgroup budgets to provide owner progress without relying on an unbounded `SCX_SLICE_INF` grant. The adaptive controller also suppresses boosting for locks whose observed unlock interval already indicates rapid self-progress.

可以写：

> On the evaluated MariaDB mixed workload, adaptive boosting improves throughput and reduces the unrelated victim's p99/p999 latency relative to the unbounded baseline, at the cost of a small increase in database p99 latency.

不能写：

- adaptive 在所有 workload 和所有 percentile 上都优于 inf；
- LockBoost 提供严格的最坏等待时间或 starvation-free 保证；
- cgroup budget 已经构成完整生产级多租户隔离；
- stats-only counters 是无丢失、逐事件精确 trace；
- 仅凭 owner hit/miss 可以证明所有 pthread mutex owner 都被准确识别。

Phase 3B 的后续残余工作不再是核心实现阻塞项，而是论文和 Phase 4 的增强项：granular map-drop attribution、更多 victim class、长期部署和跨硬件验证，以及对 RW lock/spinlock 等未覆盖锁族的扩展。
