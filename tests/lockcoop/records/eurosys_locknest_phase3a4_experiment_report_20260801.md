# Phase 3A-4: LockNest Anchor Maturity 实验报告

日期：2026-08-01  
状态：实现、定向机制实验和正式 MariaDB 性能对照完成。

## 1. 目标与结论

Phase 3A-4 回答的问题是：一个刚出现的 anchor 是否应立刻影响 successor placement，还是应先积累重复且兼容的 handoff evidence。此前的 `handoff_seq` 是 lifetime diagnostic counter，不能表达 anchor 重建、source upgrade 或 phase shift 后的重新 warm-up，因此不适合直接作为 admission policy。

本阶段实现了 per-anchor-lifecycle maturity state，并验证了：

- static `M=4` 在三次新的 60 s MariaDB/Sysbench 256-thread run 中满足 no-regression gate；
- 该机制确实产生 warming fallback 和 mature pass，而不是一个未被执行的参数；
- 在 1/8/64/256 热锁的高压力受控试验和受控 owner CPU shift 中无 `map_update_fail`、无 `counter_underflow`、无 runnable stall；
- 但当前单机实验没有证明 M4 在所有 workload 上具有稳定的端到端增益。因此主论文/默认部署路径仍保持 `maturity=off`，M4 是可复现的可选 profile，而不是未经证实的默认调参。

这一决策保留了 Phase 3A-3 的性能路径，并避免把一个“无退化但收益不稳定”的控制器包装成必要热路径。

## 2. 实现

修改范围：

- `lock-sched/tools/sched_ext/scx_locknest.bpf.c/.c`
- `lock-sched/tools/sched_ext/scx_lockcoop.bpf.c/.c`
- `lock-sched/tools/sched_ext/include/scx/lock_observe.bpf.h`

每个 `lock_nest_info` 新增以下 lifecycle state：

```text
u16 maturity_score;
u16 maturity_threshold;
u32 maturity_rejects;
u64 maturity_last_ns;
```

`handoff_seq` 继续只表示 anchor update 的 lifetime diagnostic sequence。maturity 只描述当前 scheduler-side anchor lifecycle，不是 allocator generation，也不声称发现 mutex free/realloc。

更新规则为：

```text
score_next = min(score_max, score + weight(source))

compatible = (observed_cpu == anchor_cpu)
          && (source has not become weaker)
          && (window == follow-expiry || now - maturity_last <= window)
```

对 `static` profile，`weight=1`；对 `source`/`adaptive` profile，verified mutex/pthread evidence 权重为 2，weak futex-waker hint 权重为 1。新建 anchor、expiry/stale 重建、weak-to-strong source upgrade，以及**强 owner/releaser evidence** 的 fresh CPU change 会 reset score。普通 futex waker 可在 waiter CPU 间正常流动，不能仅凭一次 CPU 变化重锚；这类弱证据变化由既有 freshness/expiry 处理。单次 queue-pressure reject、cooldown 和 waiter timeout 不会 reset，避免 feedback oscillation。

placement 顺序是：freshness -> cpumask -> maturity -> cooldown/queue pressure -> anchor placement。未成熟 anchor 只进入已有 prev-CPU/default fallback，绝不会阻塞 runnable task 或影响 LockBoost confidence/budget。

CLI：

```text
--anchor-maturity-profile off|static|source|adaptive
--anchor-maturity-threshold N
--anchor-maturity-strong-threshold N
--anchor-maturity-weak-threshold N
--anchor-maturity-max-score N
--anchor-maturity-window-ms N
```

`off` 是默认值，并在 BPF 热路径首分支返回；它不做 maturity state lookup，也不执行 gate。

## 3. 运行环境与实验条件

- 内核：Linux 6.17.0-35-generic
- 硬件：当前双路 Xeon 测试机，2 NUMA nodes，128 logical CPUs
- 主调度器：`scx_lockcoop -O 8 -A --boost-profile adaptive`
- owner offset：8；本阶段未改变 Phase 1 的 owner-coverage 边界
- 正式数据库测试：MariaDB/Sysbench OLTP read-write，256 threads，16 tables x 1,000,000 rows，10 s warm-up + 60 s measurement

所有 runtime case 的验收条件：benchmark return 0、scheduler 没有 `runnable task stall` 或 `scx_bpf_error`、结束后 `/sys/kernel/sched_ext/state=disabled`。多热点压力点还要求 `map_update_fail=0` 和 `counter_underflow=0`。

构建与静态验证：

```text
make -B -C lock-sched/tools/sched_ext scx_lockcoop scx_locknest scx_lockboost
python3 scripts/lockcoop_phase12_smoke.py
```

均通过 `-Werror` 构建和既有 Phase 1/2 smoke。

## 4. 阈值机制扫描

受控 workload 使用 256 threads、1 mutex、100 us critical section、2 s measurement。该扫描的目的不是宣布绝对吞吐冠军，而是检查 profile 是否产生预期的 pass/reject 行为、是否引入错误状态，以及确定正式 gate 的候选区间。

| Profile | ops/s | p99 us | maturity update | pass | reject | 状态 |
|---|---:|---:|---:|---:|---:|---|
| off | 9,831.36 | 1 | 0 | 0 | 0 | baseline |
| static M=0 | 9,873.07 | 1 | 1,789 | 11,794 | 0 | immediate-pass control |
| static M=2 | 9,833.81 | 1 | 901 | 16,092 | 277 | warming active |
| static M=4 | 9,822.26 | 1 | 1,707 | 8,543 | 565 | candidate |
| static M=8 | 9,936.08 | 1 | 3,601 | 1,778 | 1,675 | stronger filtering |
| static M=16 | 9,958.80 | 1 | 3,772 | 1,796 | 776 | sparse maturity |
| source 2/8 | 9,701.00 | 1 | 1,149 | 16,623 | 1,612 | source-aware ablation |
| adaptive T=4 | 9,754.92 | 1 | 1,452 | 12,850 | 533 | online threshold candidate |

M=0 的 zero reject 验证 placement gate 可被严格关闭。M>=2 有可观 reject 和 pass，说明 counters 对应真实调度决定。M4 位于低门槛与过度 filtering 之间，作为正式端到端候选是合理的；但该短试验各 profile 仅一个 repeat，不能以其中的 1-2% 波动主张性能优越性。

原始数据：`results/phase3a4_3c_20260801_afterclaim/maturity_runs.csv`。

## 5. 多热点锁压力

受控 microbench 固定 256 threads、100 us critical section，分别运行 1/8/64/256 个锁。该试验与 Phase 2 map-pressure 语义一致：检查 maturity 在更多 anchor lifecycle 下是否发生异常 state corruption，而不把 LRU 行为伪称为精确 eviction 计数。

| locks | off ops/s | M4 ops/s | off p99 us | M4 p99 us | M4 maturity reject | invariant |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 9,778.49 | 9,793.54 | 1 | 1 | 1,097 | pass |
| 8 | 63,783.15 | 58,101.18 | 65,536 | 131,072 | 3,119 | pass |
| 64 | 348,039.37 | 419,856.58 | 2 | 1,024 | 15,739 | pass |
| 256 | 805,128.11 | 1,246,912.49 | 1 | 1 | 26,408 | pass |

每一格是一个受控 run，因此只用于 safety/shape evidence。尤其 8-lock 尾延迟的粗粒度 histogram 值不应被写入论文定量性能结论。可信结论是：随着热点数增加，M4 的 warming reject 随之增加，且所有 case 在无 map failure、无 counter underflow、无 scheduler stall 下完成。

实现过程中发现 `futex_exit` 与 `select_cpu` 可以同时清理同一个 waiter。修复为 waiter-local `removed` atomic claim；随后将 decay 后不存在/为零的 owner accounting 区分为 `waiter_orphan_cleanup`，不再误报为 unsigned counter underflow。该修复同时适用于 `scx_lockcoop` 和 `scx_locknest`，并在以上多热点验证中将真正 `counter_underflow` 降为 0。

原始数据：`results/phase3a4_3c_20260801_afterclaim/multihot_runs.csv`。

## 6. Weak-Waker CPU Shift 边界

为检查 maturity 是否会把普通 futex waker 的 CPU 流动误判为 topology phase shift，新增受控 phased benchmark：64 个线程竞争一把 pthread mutex，owner 在 8 s 测量中点从 CPU 0 迁到 CPU 64。每个 profile 各运行两次；该试验的目标是验证 reset policy 和安全不变量，不是宣布微基准性能胜者。

| Profile | Mean ops/s | Mean p99 | owner shift applied | CPU reset | stale reset | zero progress | invariant |
|---|---:|---:|---:|---:|---:|---:|---|
| off | 122,606.26 | 768 us | 1/1 | 0 | 0 | 0 | pass |
| static M=4 | 148,193.92 | 512 us | 1/1 | 132 | 67,220.5 | 0 | pass |

CPU reset/stale reset 是系统级 aggregate counter。M4 的少量 CPU reset 来自同机的强 kernel-mutex owner/releaser evidence；target 的 pthread mutex 只产生 weak futex-waker anchor。这里关键的判据是数量级：在一个被否决的初版策略中，任意 waker CPU change 都会触发重锚，两个 8 s M4 run 分别产生 1,242,239 和 1,100,379 次 CPU reset。最终策略将 CPU-change reset 限制为 strong evidence 后，相同受控形状下的计数降为 162 和 102，且所有 run 正常退出。

因此，Phase 3A-4 的 phase-shift 结论不是“futex waker 可精确检测 holder topology phase shift”，而是：弱 waker 的迁移不能被当成成熟度生命周期重置依据；强 owner/releaser evidence 才允许 fresh CPU-change reset；弱证据的失效仍由 bounded expiry/fallback 处理。该边界避免了 re-anchor oscillation，也不增加默认 `maturity=off` 路径的工作。

原始数据：`results/phase3a4_phase_shift_20260801_final3/phase_shift_runs.csv`。初版被否决的诊断数据保留在 `results/phase3a4_3c_20260801_afterclaim/phase_shift`，不进入定量性能结论。

## 7. MariaDB 正式性能对照

为了避免复用短 smoke output，本测试使用独立目录 `formal_baseline60`，按 `off, M4, M4, off, off, M4` 交错执行，均为新的 60 s measurement。

| Run | Profile | TPS | p95 ms | p99 ms |
|---:|---|---:|---:|---:|
| 1 | off | 5,421.22 | 94.10 | 125.53 |
| 2 | M4 | 5,422.48 | 90.78 | 118.92 |
| 3 | M4 | 5,117.73 | 97.55 | 132.49 |
| 4 | off | 5,131.78 | 99.33 | 137.35 |
| 5 | off | 5,532.31 | 90.78 | 123.29 |
| 6 | M4 | 5,273.75 | 94.10 | 123.29 |
| mean | off | **5,361.77** | **94.74** | **128.72** |
| mean | M4 | **5,271.32** | **94.14** | **124.90** |

相对 off：M4 TPS 为 -1.69%，p99 为 -2.97%。预注册 gate 为 TPS 不低于 -3%、p99 不高于 +5%，因此 M4 通过。所有 six runs `failures=0`、最终 sched_ext state 为 disabled。

这证明 M4 在主端到端 workload 上不会破坏此前 safe-adaptive baseline；它并不证明 M4 应无条件替换 off，因为收益未在参数扫描和多热点的所有点稳定出现。

原始数据：`results/phase3a4_3c_20260801_afterclaim/formal_baseline60_runs.csv`。

## 8. 最终决策与论文口径

| 项目 | 决策 |
|---|---|
| 默认 profile | `maturity=off`，完全保留 Phase 3A-3 默认行为 |
| 可选 profile | `static M=4`，具备正式 no-regression 证据 |
| source/adaptive maturity | 实现和定向证据完成；需要跨 workload/平台重复后才作为默认候选 |
| 论文 claim | LockNest 可以在 repeated compatible evidence 前延迟 anchor-directed placement；M4 在正式 DB gate 内保持性能稳定；weak futex-waker CPU drift 不触发 re-anchor |
| 不可 claim | maturity 检测 allocator generation/address reuse；M4 在所有 workload 上提升性能；多热点 LRU 的精确 eviction 数 |

本阶段完成了 anchor-maturity 的设计、实现和实验验证。多平台、不同 NUMA 拓扑及长期生产部署尚待验证。
