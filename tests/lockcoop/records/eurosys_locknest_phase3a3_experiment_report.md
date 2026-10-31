# Phase 3A-3: LockNest 自适应机制实验报告

结果根目录：`<workspace>/results/phase3a3_locknest_20260731_1435_budget`

计划文档：`<workspace>/eurosys_locknest_phase3a3_evidence_closure_plan_20260731.md`

执行日期：2026-07-31

## 1. 结论摘要

Phase 3A-3 从以下四个方面验证了 LockNest 自适应机制：

1. 256-thread MariaDB/Sysbench 正式重复：8 个关键 profile 全部完成 `3 x 60s` 正式重复，`failures=0`。
2. 机制证据表：记录 locality hit、pressure/cooldown reject、prevcpu/default fallback、stale nest、expiry 调整和 map update failure。
3. 多热点锁压力实验：覆盖 `1/2/4/8/16/64` 个热点锁，验证多热锁下是否出现锚点抖动、map 失败或 zero-progress。
4. 细粒度参数补点：补齐 `Q=24/32/64/128`、`C=2/3/8/10/15ms`、`P=8/12/18/20/24/48`、`E/N` 中间点，避免只靠粗粒度参数得结论。

最终结论如下：

- `safe_adaptive` 即 `scx_lockcoop -O 8 -A` 是当前 LockNest 自适应的论文主配置候选。相对 `baseline_off (-O 8)`，正式重复中 TPS 提升 3.18%，p99 降低 5.32%，且 3 次重复均有效。
- finite queue cap 是最敏感、也最危险的参数。`Q=8/16` 会显著降低 locality，并放大 pressure/cooldown/prevcpu fallback，端到端 TPS 分别降到 baseline 的 70.7% 和 91.7%。
- 多热点锁实验中所有 profile、所有热点数都满足 `map_update_fail=0`、`zero_progress_threads=0`。在 64 个热点锁时，吞吐和尾延迟都回到稳定区间，说明本轮没有观察到 1024-entry map 或单 anchor 策略导致的容量崩溃。
- `P=18` 被单独测试过，但它不是特殊最优点。数据支持的说法是 promotion threshold 存在较宽平台区间，不能把某个整数写成跨工作负载最优。
- 实验前修复了组合调度器中的 owner boost 运行时安全问题：把无界 `SCX_SLICE_INF` 改成有限 boost budget，并保护 per-CPU kworker 进度，保证重复实验能够正常完成。

## 2. 运行时安全前提

前一轮正式重复曾暴露 `kworker/... failed to run for 30s+` 的 runnable task stall。失败同时出现在 `baseline_off (-O 8)` 和 `safe_adaptive (-O 8 -A)`，因此根因不在 LockNest 自适应参数本身，而在组合版 `scx_lockcoop` 的运行时安全边界。

本轮正式实验前完成了两处约束：

| 修改 | 策略含义 | 目的 |
|---|---|---|
| 有限 owner boost budget | `SCX_SLICE_INF` 改为 `owner_boost_slices=1`、`owner_boost_max_slices=2` | 保留 LockBoost 的 owner-progress 思想，同时避免无界时间片压制系统线程 |
| per-CPU kworker 进度保护 | per-CPU kernel thread 不进入 LockNest 路径，enqueue 到 local DSQ | 保证 CPU 绑定的内核线程不会在 shared DSQ 中长期饥饿 |

修复后通过了两层检查：

- attach smoke：`sudo -n scx_lockcoop -D -O 8 -A` 可正常启动和退出。
- 20s MariaDB smoke：`baseline_off` 和 `safe_adaptive` 均完成，未出现 runnable stall。

随后重新运行完整 Phase 3A-3 矩阵。本报告只使用修复后的结果根目录 `results/phase3a3_locknest_20260731_1435_budget`。

## 3. 实验设置

| 项目 | 设置 |
|---|---|
| 机器 | 当前主机 |
| 内核 | Linux `6.17.0-35-generic` |
| 数据库 | MariaDB `10.11.14-MariaDB-0ubuntu0.24.04.1` |
| 负载 | Sysbench OLTP read-write |
| 数据集 | 16 tables, 1,000,000 rows/table |
| 线程数 | 256 |
| owner offset | `-O 8` |
| 调度器 | `scx_lockcoop` |

执行命令：

```bash
python3 scripts/run_phase3a3_locknest_evidence_closure.py \
  --root results/phase3a3_locknest_20260731_1435_budget \
  --only formal --threads 256 --formal-runtime 60 \
  --formal-warmup 10 --formal-runs 3 --resume

python3 scripts/run_phase3a3_locknest_evidence_closure.py \
  --root results/phase3a3_locknest_20260731_1435_budget \
  --only coverage --coverage-runtime 8 --coverage-threads 256 --resume

python3 scripts/run_phase3a3_locknest_evidence_closure.py \
  --root results/phase3a3_locknest_20260731_1435_budget \
  --only micro --micro-duration 6 --micro-runs 2 \
  --micro-threads 256 --micro-locks 1,2,4,8,16,64 --resume

python3 scripts/run_phase3a3_locknest_evidence_closure.py \
  --root results/phase3a3_locknest_20260731_1435_budget \
  --only fine --threads 256 --fine-runtime 20 \
  --fine-warmup 5 --fine-runs 1 --resume
```

一致性检查：

- `formal_repeats.csv`：8 个 profile 全部 `n=3, failures=0`。
- `fine_search.csv`：21 个 profile 全部 `n=1, failures=0`。
- `mechanism_evidence.csv`：所有 profile 的 `map_update_fail=0`，实验后 `sched_ext_state_after=disabled`。
- `multihot_microbench_summary.csv`：所有 profile/lock-count 的 `zero_progress_threads_mean=0`，`map_update_fail=0`。
- 全结果目录扫描未发现 `runnable task stall`、`scx_bpf_error`、`failed to run`、`Traceback`、`ERROR`、`EXIT:`。
- 最终 `/sys/kernel/sched_ext/state` 为 `disabled`。

## 4. WP1: MariaDB 正式重复

正式重复使用 `60s` measurement 和 `10s` warmup，每个关键 profile 取得 3 个有效 run。

| profile | args | n | failures | TPS mean | TPS CV | p95 ms | p99 ms | TPS vs baseline | p99 vs baseline |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `baseline_off` | `-O 8` | 3 | 0 | 5227.74 | 4.83% | 98.32 | 131.99 | 100.0% | 100.0% |
| `safe_adaptive` | `-O 8 -A` | 3 | 0 | 5394.09 | 3.31% | 93.64 | 124.97 | 103.2% | 94.7% |
| `q16` | `-O 8 -A -Q 16` | 3 | 0 | 4794.96 | 2.09% | 106.78 | 142.43 | 91.7% | 107.9% |
| `q8` | `-O 8 -A -Q 8` | 3 | 0 | 3697.65 | 2.32% | 145.02 | 199.28 | 70.7% | 151.0% |
| `c5` | `-O 8 -A -Q 16 -R 4 -C 5` | 3 | 0 | 4762.36 | 1.86% | 106.76 | 144.99 | 91.1% | 109.9% |
| `e5000` | `-O 8 -A -Q 16 -R 4 -C 5 -e 5000 -N 5000` | 3 | 0 | 4597.78 | 1.74% | 111.35 | 150.34 | 87.9% | 113.9% |
| `n1000` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 1000` | 3 | 0 | 4426.28 | 4.87% | 117.66 | 160.76 | 84.7% | 121.8% |
| `p32` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 32` | 3 | 0 | 4463.14 | 4.58% | 115.62 | 156.16 | 85.4% | 118.3% |

正式重复给出三点结论：

1. `safe_adaptive` 满足默认候选标准：TPS 不低于 baseline，p99 不高于 baseline，且没有 runtime failure。
2. 明确设置 finite queue cap 后，性能和尾延迟开始退化。`Q=16` 尚可作为 stress 对照，`Q=8` 已明显不可作为默认路径。
3. `C/e/N/P` 的组合点在本轮没有超过 `safe_adaptive`。因此默认路径应保持简单：保留 direct same-lock anchor，不主动施加小队列上限。

## 5. WP2: 机制证据表

coverage 实验用于回答“TPS/p99 背后是什么机制”。表中 `locality hit %` 是所有 select-cpu locality decision 中命中 anchor 的比例；`waiter locality %` 用 waiter cleanup 作为分母，更接近同锁等待链中的 placement 覆盖率。

| profile | locality hit % | waiter locality % | pressure reject | cooldown reject | prevcpu fallback | default fallback | stale nest | map fail |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `baseline_off` | 31.24 | 69.56 | 0 | 0 | 0 | 2,804,135 | 75,057 | 0 |
| `safe_adaptive` | 31.75 | 70.35 | 0 | 0 | 0 | 3,062,059 | 66,801 | 0 |
| `q64` | 29.85 | 61.57 | 14,961 | 253,866 | 83,334 | 3,304,994 | 102,885 | 0 |
| `q32` | 28.01 | 55.55 | 41,452 | 400,215 | 186,869 | 3,368,961 | 116,823 | 0 |
| `q16` | 27.79 | 47.90 | 111,919 | 707,068 | 430,951 | 3,145,199 | 161,466 | 0 |
| `q12` | 26.82 | 43.24 | 157,509 | 1,011,008 | 640,605 | 3,028,957 | 174,843 | 0 |
| `q8` | 24.63 | 36.33 | 205,896 | 1,436,608 | 985,702 | 2,649,129 | 184,268 | 0 |
| `c5` | 27.79 | 48.25 | 113,587 | 736,290 | 438,924 | 3,168,135 | 158,025 | 0 |
| `e5000` | 27.45 | 47.61 | 113,330 | 739,281 | 445,728 | 3,221,364 | 162,968 | 0 |
| `n1000` | 27.56 | 47.97 | 114,987 | 747,424 | 450,251 | 3,300,660 | 165,492 | 0 |
| `p18` | 29.07 | 49.70 | 248,864 | 549,354 | 402,737 | 3,174,497 | 154,981 | 0 |
| `p32` | 28.80 | 49.67 | 244,495 | 553,611 | 409,396 | 3,291,615 | 160,132 | 0 |

机制解释：

- `safe_adaptive` 没有触发 pressure/cooldown/prevcpu 控制，说明默认 `-A` 路径主要保留原始 same-lock anchor 行为，同时提供额外 counters 和自适应边界。
- `Q` 越小，fallback-heavy 越明显。`q8` 的 waiter locality 从 70.35% 降到 36.33%，cooldown reject 达到 1,436,608，prevcpu fallback 达到 985,702；这与正式重复中 TPS 下降和 p99 上升一致。
- `q64/q32/q24` 属于较宽 admission 区间，短测性能较好，但机制表显示它们仍引入 pressure/cooldown 控制。因此默认不直接使用 finite `Q`，而把它作为 stress profile 或后续在线压力路径候选。
- `p18/p32` 触发的 pressure 与 fallback 不低，端到端没有超过 `safe_adaptive`。promotion threshold 的正确写法是“存在可用平台区间”，不是“某个阈值精确最优”。
- 所有 coverage profile 的 `map_update_fail=0`，可以支撑论文回答：在本轮 256-thread OLTP coverage 中，BPF map 更新失败不是性能变化的主因。

## 6. WP3: 多热点锁压力实验

multi-hot 微基准使用 256 threads、`1/2/4/8/16/64` 个 `pthread_mutex` 热点锁，每个 profile/lock-count 运行 2 次。该实验专门压力测试多热点锁下的 anchor admission 与 fallback 行为。

| locks | profile | ops/s mean | ops vs baseline | p99 us | p99.9 us | Jain fairness | locality hit % | zero-progress | map fail |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `baseline_off` | 908,523.21 | 100.0% | 0.0 | 0.5 | 0.1030 | 54.97 | 0 | 0 |
| 1 | `safe_adaptive` | 916,548.01 | 100.9% | 0.0 | 0.0 | 0.0892 | 58.96 | 0 | 0 |
| 1 | `q16` | 856,023.17 | 94.2% | 1475.0 | 3564.0 | 0.0671 | 64.10 | 0 | 0 |
| 1 | `q8` | 877,816.95 | 96.6% | 935.0 | 3365.0 | 0.0589 | 54.61 | 0 | 0 |
| 2 | `baseline_off` | 1,631,858.94 | 100.0% | 0.0 | 0.0 | 0.1720 | 58.00 | 0 | 0 |
| 2 | `safe_adaptive` | 1,720,720.33 | 105.4% | 0.0 | 0.0 | 0.1307 | 57.32 | 0 | 0 |
| 2 | `q16` | 1,014,284.38 | 62.2% | 1698.5 | 2743.5 | 0.6442 | 72.95 | 0 | 0 |
| 2 | `q8` | 1,424,083.66 | 87.3% | 1078.0 | 2150.5 | 0.1123 | 59.52 | 0 | 0 |
| 4 | `baseline_off` | 3,281,411.91 | 100.0% | 0.0 | 0.0 | 0.1636 | 58.73 | 0 | 0 |
| 4 | `safe_adaptive` | 3,255,308.37 | 99.2% | 0.0 | 0.0 | 0.1400 | 58.79 | 0 | 0 |
| 4 | `q16` | 1,882,702.48 | 57.4% | 971.5 | 1551.5 | 0.9355 | 69.87 | 0 | 0 |
| 4 | `q8` | 1,545,212.03 | 47.1% | 1342.0 | 2257.5 | 0.9882 | 62.37 | 0 | 0 |
| 8 | `baseline_off` | 6,519,465.88 | 100.0% | 0.0 | 0.0 | 0.1942 | 58.48 | 0 | 0 |
| 8 | `safe_adaptive` | 6,038,742.54 | 92.6% | 0.0 | 0.5 | 0.2720 | 59.91 | 0 | 0 |
| 8 | `q16` | 3,744,104.54 | 57.4% | 585.0 | 1022.5 | 0.9650 | 70.77 | 0 | 0 |
| 8 | `q8` | 2,885,981.39 | 44.3% | 696.0 | 1169.0 | 0.9803 | 62.16 | 0 | 0 |
| 16 | `baseline_off` | 11,413,872.72 | 100.0% | 0.0 | 0.5 | 0.5128 | 60.00 | 0 | 0 |
| 16 | `safe_adaptive` | 12,679,476.25 | 111.1% | 0.0 | 0.0 | 0.5215 | 59.01 | 0 | 0 |
| 16 | `q16` | 12,300,196.79 | 107.8% | 0.0 | 0.0 | 0.5223 | 55.38 | 0 | 0 |
| 16 | `q8` | 6,339,389.20 | 55.5% | 428.5 | 938.5 | 0.9660 | 59.81 | 0 | 0 |
| 64 | `baseline_off` | 42,928,797.05 | 100.0% | 0.0 | 1.0 | 0.9753 | 68.33 | 0 | 0 |
| 64 | `safe_adaptive` | 42,337,930.55 | 98.6% | 0.0 | 2.0 | 0.9755 | 70.28 | 0 | 0 |
| 64 | `q16` | 41,634,960.08 | 97.0% | 0.0 | 4.0 | 0.9748 | 69.90 | 0 | 0 |
| 64 | `q8` | 43,131,498.91 | 100.5% | 0.0 | 1.0 | 0.9785 | 62.12 | 0 | 0 |

多热点实验结论：

1. `safe_adaptive` 在所有热点数下都没有引入 zero-progress 或 map failure，且 p99 基本保持低位。
2. `q8/q16` 在 1-8 个强热点锁下出现 ms 级尾延迟，说明有限 admission 对少数强热点更敏感。
3. 当热点锁数增加到 16/64，竞争被分散，`q16` 和 `safe_adaptive` 回到可接受区间。这说明退化来自 admission 控制和热点强度交互，而不是 map 容量失败。
4. 多热点锁实验需要报告 `map_update_fail=0` 和 `zero-progress=0`，同时承认过小 `Q` 会导致可解释退化，因此不进入默认主配置。

## 7. WP4: 细粒度参数搜索

fine search 使用 `20s` measurement 和 `5s` warmup，每个 profile 1 个有效 run。它只用于补趋势，不替代 WP1 的正式重复。

| kind | profile | args | TPS | p99 ms | failures |
|---|---|---|---:|---:|---:|
| Q | `q128` | `-O 8 -A -Q 128` | 4449.89 | 158.63 | 0 |
| Q | `q64` | `-O 8 -A -Q 64` | 5201.28 | 123.28 | 0 |
| Q | `q32` | `-O 8 -A -Q 32` | 5104.71 | 127.81 | 0 |
| Q | `q24` | `-O 8 -A -Q 24` | 5255.63 | 127.81 | 0 |
| Q | `q12` | `-O 8 -A -Q 12` | 4411.82 | 161.51 | 0 |
| C | `c2` | `-O 8 -A -Q 16 -R 4 -C 2` | 4392.36 | 158.63 | 0 |
| C | `c3` | `-O 8 -A -Q 16 -R 4 -C 3` | 4449.93 | 164.45 | 0 |
| C | `c8` | `-O 8 -A -Q 16 -R 4 -C 8` | 4518.70 | 153.02 | 0 |
| C | `c10` | `-O 8 -A -Q 16 -R 4 -C 10` | 4812.70 | 142.39 | 0 |
| C | `c15` | `-O 8 -A -Q 16 -R 4 -C 15` | 4328.77 | 158.63 | 0 |
| expiry | `e3000` | `-O 8 -A -Q 16 -R 4 -C 5 -e 3000 -N 3000` | 4693.13 | 144.97 | 0 |
| expiry | `e4000` | `-O 8 -A -Q 16 -R 4 -C 5 -e 4000 -N 4000` | 4353.06 | 161.51 | 0 |
| expiry | `e8000` | `-O 8 -A -Q 16 -R 4 -C 5 -e 8000 -N 8000` | 4455.21 | 158.63 | 0 |
| N | `n750` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 750` | 4387.49 | 164.45 | 0 |
| N | `n1500` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 1500` | 4603.66 | 147.61 | 0 |
| P | `p8` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 8` | 4837.93 | 139.85 | 0 |
| P | `p12` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 12` | 4749.17 | 142.39 | 0 |
| P | `p18` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 18` | 4200.17 | 170.48 | 0 |
| P | `p20` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 20` | 4520.24 | 155.80 | 0 |
| P | `p24` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 24` | 4805.34 | 139.85 | 0 |
| P | `p48` | `-O 8 -A -Q 16 -R 4 -C 5 -e 2000 -N 2000 -P 48` | 4280.78 | 167.44 | 0 |

细粒度搜索结论：

- `Q=24/32/64` 的短测性能较好，但 formal 中 `Q=16/8` 已显示 finite queue cap 风险，coverage 也显示 `Q` 越小 fallback 越重。因此下一版启发式可以把 `Q` 作为压力路径的有界候选，而不是默认常开参数。
- `Q=128` 表现反而较差，说明 admission window 不是越大越好；过宽窗口可能保留过时/拥塞 anchor。
- `C` 在 `2-10ms` 内没有决定性差异，`C=15ms` 开始偏低。cooldown 更适合做低频阻尼，不值得变成复杂在线优化目标。
- `E/N` 中间点没有超过 `safe_adaptive`。有效期调整应保持保守：缩短用于防 stale，延长用于稳定局部性，但不能频繁追逐短期性能。
- `P=18` 在本轮不是最优，且低于 `P=8/12/24`。这正好说明论文不应声称精确阈值，而应写成 promotion 只是 stress profile 的辅助机制。

## 8. 自适应参数设计收敛

基于 Phase 3A-3，当前 LockNest 自适应参数应这样收敛：

| 参数 | 当前范围/默认 | 结论 | 论文写法 |
|---|---|---|---|
| `-A` | 默认候选打开 | 保留 same-lock anchor 主路径，不施加小队列上限 | 主配置，可写强 |
| `Q` | 默认 unlimited；stress 用 24/32/64 | 小 `Q` 明确退化，大 `Q` 不保证更好 | 压力路径候选，不作为默认性能优化 |
| `R` | 4 附近 | 作为 pressure reject 计数阈值，避免短 burst 误判 | 低频保护参数 |
| `C` | 2-10ms 可接受 | 对性能非高敏，但过长不必要 | 简单阻尼，不做复杂调参 |
| `E/N` | 默认 2s；stress 可 3-8s/0.75-1.5s | 不能激进缩短；需要保留局部性历史 | 有界生命周期 hint |
| `P` | 默认关闭；stress 扫 8-48 | 无稳定收益，不进入主路径 | 可选 stress knob |
| map size | 1024 entries | 本轮无 map failure | 不作为 Phase 3A 自适应对象 |

推荐默认策略：

```text
default LockNest adaptive:
  keep direct same-lock anchor handoff
  keep anchor queue cap unlimited
  collect pressure/cooldown/fallback/stale counters
  use expiry adjustment only as bounded safety hint
  keep direct-hit promotion disabled by default
```

推荐 stress/ablation 策略：

```text
stress profile:
  Q in {24, 32, 64}
  C in {5, 10} ms
  E/N in {3000/3000, 5000/5000}
  P in {8, 12, 24}
```

不建议的策略：

```text
avoid:
  Q <= 16 as default
  Q=8 under strong hot-lock workloads
  frequent online tuning of C
  treating P=18 or any single integer as a universal optimum
  using map-size adaptation before observing map_update_fail
```

## 9. 对论文证据链的影响

Phase 3A-3 现在可以支撑论文中更强但仍克制的表述：

1. LockNest 自适应不是黑箱调参，而是围绕 anchor lifecycle、admission pressure 和 fallback path 的有界控制策略。
2. 默认 `-A` 配置在 256-thread MariaDB OLTP 正式重复中没有性能裂化，TPS 高于 `-O 8` baseline，p99 更低。
3. 过小 admission queue cap 会导致可解释退化：locality 下降、pressure/cooldown reject 增加、prevcpu/default fallback 增加，最终表现为 TPS 降低和 p99 上升。
4. 多热点锁下没有观察到 BPF map failure 或 zero-progress，说明 1024-entry map 与单 anchor 机制在本轮压力范围内不是新瓶颈。
5. 参数敏感性结果支持“稳定区间”而不是“精确最优整数”。论文中应避免写 `P=18`、`Q=24` 这类点是通用最优。

论文中仍应保守写的边界：

1. 本轮只覆盖当前机器，硬件拓扑泛化仍放到后续 Phase。
2. fine search 是 20s 单样本，用于趋势，不替代正式重复。
3. `Q=24/32/64` 需要作为 stress/ablation，而不是默认主路径。
4. 本轮 runtime safety 修复解决了 kworker progress 问题，但公平性和多租户隔离仍属于 LockBoost/Phase 3B 的主问题。

## 10. 最终验收判定

| 验收项 | 结果 | 依据 |
|---|---|---|
| 正式重复完整性 | 通过 | 8 个关键 profile 全部 `n=3, failures=0` |
| 默认自适应不劣化 | 通过 | `safe_adaptive` TPS 为 baseline 的 103.2%，p99 为 baseline 的 94.7% |
| 小 `Q` 退化可解释 | 通过 | `q8/q16` 的 pressure/cooldown/prevcpu fallback 明显增加 |
| 多热点锁压力 | 通过 | `map_update_fail=0`，`zero_progress_threads=0` |
| 细粒度参数补点 | 通过 | 21 个 profile 全部有效，覆盖中间参数点 |
| runtime safety | 通过 | 全结果目录未发现 runnable stall / BPF error，最终 `sched_ext=disabled` |

Phase 3A-3 已完成 LockNest 的性能、机制、压力与稳定性验证。Phase 3B 将研究 LockBoost 自适应 boost budget、公平性和混合负载隔离。
