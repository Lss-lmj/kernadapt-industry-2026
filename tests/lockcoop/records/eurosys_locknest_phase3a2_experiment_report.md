# Phase 3A-2: 参数敏感性驱动的 LockNest 启发式自适应设计实验报告

结果根目录：`<workspace>/results/phase3a2_locknest_20260731_030300`

## 1. 目标

Phase 3A-2 的目标不是继续堆新开关，而是用小而有判别力的参数扫描，反推出 LockNest 的下一版启发式：

- 哪些参数真的影响 locality、尾延迟和吞吐；
- 哪些参数只是 stress / fairness knob；
- 哪些参数不值得默认在线自适应；
- 哪些参数组合能作为 balanced profile 的候选边界。

这一步只研究 LockNest 的锚点策略，不改 LockBoost owner evidence，也不改 `SCX_SLICE_INF`。

## 2. 实验设置

- 机器：当前主机，x86-64，Linux 6.17.0-35-generic。
- 数据库：MariaDB 10.11.14。
- 负载：Sysbench OLTP read-write，16 tables，1,000,000 rows/table。
- 线程数：256。
- 性能阶段：5 s warmup，20 s measurement。
- coverage 阶段：8 s。
- owner offset：8。

基线与扫描方式：

- `baseline_off`: `scx_lockcoop -O 8`
- `safe_adaptive`: `scx_lockcoop -O 8 -A`
- Q/R/C/expiry/N/P 扫描：在可控 stress profile 上单参数变化。

说明：

- `safe_adaptive` 是默认自适应路径，`Q` 不设有限制；
- `q16` 及其后续扫描属于 stress profile，用来观察压力、冷却和过期机制；
- `q16` 的第一次尝试触发过一次 `runnable task stall`，第二次重试成功，下面报告使用的是成功样本。

## 3. 性能结果

### 3.1 总体结论

最好的整体点是 `safe_adaptive`，不是任何有限队列 cap 组合。

| 组别 | 最佳 profile | TPS | p95 ms | p99 ms | 结论 |
|---|---|---:|---:|---:|---|
| baseline | `safe_adaptive` | 5493.34 | 90.780 | 118.924 | 默认自适应路径最稳 |
| Q cap | `q16` | 4403.03 | 116.800 | 155.801 | finite cap 会明显伤主线 |
| R threshold | `r8` | 4966.04 | 102.970 | 137.350 | 影响小，4/8 都可 |
| cooldown | `c5` | 4963.65 | 101.130 | 134.899 | 5ms 最平衡 |
| expiry | `e5000` | 5029.44 | 101.130 | 132.492 | 长 expiry 更稳 |
| min expiry | `n1000` | 4976.75 | 102.970 | 134.899 | 低于 base 更有利 |
| promotion | `p32` | 4917.56 | 102.970 | 137.350 | 有一点收益，但不值得默认热路径写 |

### 3.2 关键点

1. `safe_adaptive` 比 `baseline_off` 高 11.6% TPS，p99 下降约 15.0%。
2. `Q=16` 已经比默认自适应差 19.8% TPS，说明有限 queue cap 不是 balanced 默认。
3. `Q=8` 和 `Q=2` 进一步恶化，尾延迟明显拉长。
4. `R` 的敏感性很低，说明“连续压力阈值”不是主导因素。
5. `C=5ms` 最好，`1ms` 太急，`20ms` 太钝。
6. 在有限 `Q=16` 的 stress profile 下，长 expiry 和适度降低 `N` 能恢复一部分性能，但仍追不上默认自适应路径。
7. `P` 只能带来边际收益，更像 stress profile 的辅助 knob，不像默认策略必须项。

### 3.3 逐项观察

#### Q cap

`Q` 是最敏感的参数。

- `safe_adaptive (-A)`：5493.34 TPS，p99 118.924 ms。
- `Q=16`：4403.03 TPS，p99 155.801 ms。
- `Q=8`：3526.35 TPS，p99 211.599 ms。
- `Q=2`：2297.42 TPS，p99 344.078 ms。

结论：queue cap 一旦变小，系统很快从 locality-hint 退化成频繁 fallback。它适合做 stress / fairness 实验，不适合作为 balanced 默认。

#### R / C

在 `Q=16` 的 stress profile 上：

- `R=1/4/8` 的差异不大，TPS 都在 4878~4966 之间；
- `C=5ms` 明显优于 `1ms` 和 `20ms`。

结论：`R` 不是关键敏感参数，`C=5ms` 是更稳妥的默认候选。

#### expiry / N

在 `Q=16` stress profile 上：

- `e=500` 和 `e=2000` 都偏差；
- `e=5000` 恢复得最好，TPS 5029.44，p99 132.492 ms；
- 在 `e=2000` 固定时，`N=500/1000` 比 `N=2000` 更好。

结论：稳定 anchor 应该允许更长生命周期；`N` 主要是防御性下限，不应轻易抬高到和 base expiry 等高。

#### P

`P` 的表现是边际改善：

- `P=0`: 4655.68 TPS
- `P=16`: 4784.39 TPS
- `P=32`: 4917.56 TPS

结论：promotion 能补一点，但不足以改变默认策略。它更像 stress profile 下的可选增强，而不是 balanced 默认的核心机制。

## 4. 机制覆盖结果

### 4.1 总体观察

coverage 结果确认：这轮扫描没有破坏 owner tracking，也没有出现 map 更新失败。

- `map_update_fail = 0` for all valid profiles。
- `kernel_mutex_owner_hit_pct` 基本都在 99%+。
- `private_futex_owner_hit_pct` 维持在 40%~48% 区间。

### 4.2 关键 profile

| profile | locality hit % | pressure rej | cooldown rej | prevcpu fb | default fb | 说明 |
|---|---:|---:|---:|---:|---:|---|
| `baseline_off` | 29.72 | 0 | 0 | 0 | 2,984,390 | 没有自适应压力路径 |
| `safe_adaptive` | 30.01 | 0 | 0 | 0 | 3,185,260 | 默认自适应不触发压力逻辑 |
| `q16` | 28.57 | 97,563 | 587,818 | 340,198 | 2,756,635 | finite cap 开始明显触发 fallback |
| `q8` | 23.51 | 235,766 | 1,584,545 | 1,107,101 | 2,984,203 | 压力和 cooldown 急剧放大 |
| `q2` | 10.59 | 451,049 | 3,468,666 | 3,241,089 | 2,185,072 | 过小 cap 直接把 locality 打散 |
| `c5` | 25.91 | 98,429 | 618,195 | 344,337 | 2,864,787 | 中等 cooldown 较稳 |
| `e5000` | 26.43 | 96,026 | 646,606 | 371,203 | 3,013,863 | 长 expiry 下更像稳定 anchor |
| `n1000` | 27.63 | 97,405 | 556,530 | 311,116 | 2,750,119 | 低 min expiry 对恢复更友好 |
| `p32` | 28.16 | 197,635 | 460,766 | 317,564 | 2,805,444 | promotion 有收益，但不够改变默认结论 |

### 4.3 机制解释

coverage 支持下面的解释：

1. `safe_adaptive` 之所以好，是因为它保留了 direct same-lock handoff，没有把 anchor 强行压进有限队列。
2. `Q` 变小后，pressure/cooldown/prevcpu fallback 都上升，说明系统在用更多控制动作弥补过窄的队列门槛。
3. `C=5ms` 提供了比 `1ms` 更稳的恢复窗口。
4. `e=5000` / `N=500~1000` 说明 anchor 需要更长寿命，但也需要一个能在必要时缩短的下限。
5. `P` 的价值存在，但主要是 stress profile 里的补偿，不是 balanced 主路径。

## 5. 由实验反推的启发式

从这组数据里，下一版 LockNest 启发式可以先收敛成一个很朴素、但更合理的版本：

1. 默认保留 `-A` 的无界自适应路径，不要给 balanced profile 加小 `Q`。
2. `R=4` 或 `8` 都可以，差异小，不必过度调。
3. `C=5ms` 是当前最好的短窗口候选。
4. anchor expiry 应该偏长，短 expiry 更像防御，不像默认优化。
5. `N` 作为下限可以比 base expiry 低一些，保留相变时的收缩空间。
6. `P` 默认关闭；只有在 stress profile 或后续更严格的实验里再打开。

更直接地说：

- balanced/default：`-A`，不加 finite queue cap；
- stress/fairness：再加 `Q/R/C/P` 去观察退化边界。

## 6. 结论

这轮 Phase 3A-2 的核心结论是：

1. LockNest 的真正主轴是 same-lock locality，不是有限队列 cap。
2. 默认自适应路径已经足够好，甚至优于 baseline。
3. 小 `Q` 会迅速把系统推到压力区，导致吞吐和尾延迟一起恶化。
4. `R/C/expiry/N/P` 更像是在 stress profile 下修补边界，而不是应当默认开启的一组强优化。
5. 当前数据支持一个简洁的 heuristic：默认保留无界 anchor、用短而克制的 cooldown、让 expiry 偏长、把 promotion 留作可选增强。

## 7. 备注

- 该实验集是单机单工作负载扫描，不是多机器泛化实验。
- q16 的第一次运行触发过 runnable-task stall，第二次运行成功；这说明 finite cap 16 已经接近当前机器上的稳定性边界。
- 完整原始表和 JSON 见结果根目录下的 `summary.md`、`manifest.json`、`results.json`。
