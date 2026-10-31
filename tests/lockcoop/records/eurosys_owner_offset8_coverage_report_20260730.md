# LockCoop 默认 `owner_offset=8` Coverage-Only 实验报告

日期：2026 年 7 月 30 日

## 1. 实验目的

本实验只回答一个问题：在当前 `scx_lockcoop` 默认 owner resolver 配置
`owner_offset=8` 下，LockBoost 实际能从哪些事件获得 owner 证据，哪些事件会
保守地退化为 LockNest placement 或默认调度。

本实验是 coverage-only，不是性能矩阵，也不用于重新报告 TPS、p95 或端到端
加速比。调度器以 `-D` 启动以打开诊断计数；因此 sysbench 结果只用来确认
负载正常运行，不能与无诊断性能结果直接比较。

## 2. 实验对象与配置

### 2.1 软件与硬件

| 项目 | 配置 |
|---|---|
| 调度器 | 当前 `scx_lockcoop`，LockNest + LockBoost 组合实现 |
| 内核 | Linux `6.17.0-35-generic` |
| CPU | 2-socket Intel Xeon Platinum 8375C |
| CPU 数 | 128 个逻辑 CPU，SMT 开启 |
| NUMA | 2 个 NUMA 节点 |
| libc | glibc `2.39` |
| 数据库 | MariaDB `10.11.14` |
| 负载工具 | sysbench `1.0.20` |

当前机器上用独立的 C 程序检查了 glibc/NPTL 布局：

```text
offsetof(pthread_mutex_t, __data.__owner) = 8
```

因此本实验的 `-O 8` 是当前 x86-64 glibc/NPTL profile 的已验证配置，
不是 futex ABI 对 owner 字段位置的通用规定。

### 2.2 调度器与负载

调度器命令为：

```text
sudo -n /usr/bin/stdbuf -oL \
  lock-sched/tools/sched_ext/build/bin/scx_lockcoop \
  -D -O 8
```

未传入其他调参参数，实际使用当前默认值：

```text
high_contention_threshold = 4
decay_timeout             = 200 ms
nest_expiry               = 2000 ms
weak_anchor_queue_limit   = unlimited
pthread_owner_offset      = 8
```

sysbench 负载为：

```text
oltp_read_write
--tables=16
--table-size=1000000
--threads=32
--time=10
--percentile=95
```

共运行 3 次，未重跑任何完整性能矩阵。

## 3. 原始运行结果

下表中的 TPS 和 p95 只用于确认 sysbench 正常完成；它们不是性能结论。

| run | sysbench TPS | p95 (ms) | private futex wait | private owner hit rate | locality decision hit rate | map fail | underflow guard |
|---|---:|---:|---:|---:|---:|---:|---:|
| `220317` | 3013.09 | 15.83 | 1,424,219 | 17.31% | 34.34% | 0 | 12 |
| `220453` | 4182.48 | 13.46 | 1,513,426 | 22.49% | 27.02% | 0 | 20 |
| `220508` | 4057.90 | 12.52 | 1,670,435 | 20.19% | 29.55% | 0 | 7 |

三次运行均满足：

- scheduler return code 为 0；
- sysbench return code 为 0；
- ignored errors 为 0；
- sched_ext 结束后回到 `disabled`；
- 没有观察到 `scx_bpf_error` 或 scheduler attach failure。

## 4. Coverage-Only 聚合表

以下为三次运行的计数合计。owner hit/miss 是证据路径计数，不是应用侧
ground-truth accuracy。

### 4.1 Owner 与 futex 事件覆盖

| 证据或事件 | 命中/观测次数 | 覆盖率或含义 |
|---|---:|---|
| private futex wait | 4,608,080 | ordinary private futex wait 观测 |
| private futex owner resolver hit | 924,156 | 20.06% 的 owner-resolver observations 产生可验证 owner 候选 |
| private futex owner resolver miss | 3,683,927 | 79.94%；不触发 LockBoost owner relation |
| private futex wake | 5,512,933 | 只作为 LockNest 的 advisory anchor hint，不作为 owner |
| kernel mutex owner hit | 9,437 | 直接来自 kernel mutex owner path |
| kernel mutex owner miss | 10 | kernel mutex contention 中的未解析事件 |
| kernel mutex owner hit rate | 99.89% | 仅针对 kernel mutex owner counters |
| shared futex fallback | 17 | 观测后明确 fallback，不创建 private-futex strong owner |
| PI futex fallback | 0 | 本负载未出现，不能据此声称支持 PI |
| requeue fallback | 0 | 本负载未出现，不能据此声称支持 requeue |

`private futex owner resolver hit rate` 的分母为
`private_owner_hit + private_owner_miss = 4,608,083`。它与
`private futex wait` 相差 3 次，这是诊断程序按周期读取 per-CPU counters
时的极小并发采样差异，不影响比例判断。

kernel mutex 的 hit/miss 由当前 CLI 输出的 aggregate owner counters 减去
private futex owner counters 得到：

```text
kernel_mutex_hit  = owner_hit - futex_private_owner_hit
kernel_mutex_miss = owner_miss - futex_private_owner_miss
```

### 4.2 LockNest、LockBoost 与状态安全

| 计数 | 合计 | 解释 |
|---|---:|---|
| waiter record hit | 4,544,630 | 成功记录 waiter relation |
| waiter record miss | 11,754,995 | 没有可消费 waiter record 的诊断观察 |
| locality hit | 3,501,797 | 使用可用 anchor 完成目标 CPU placement |
| locality fallback | 8,166,080 | 无 anchor、stale、cpumask 不可用或 admission 失败时回退 |
| locality decision hit rate | 30.01% | `locality_hit / (locality_hit + locality_fallback)` |
| stale nest | 88,474 | stale anchor 被发现并清理 |
| boost applied | 1,730 | LockBoost 实际执行的 boost tick/application 计数 |
| map update failure | 0 | 三次运行没有 BPF map update failure |
| waiter overwrite | 26 | 同一 TID 的旧 waiter record 被覆盖前清理 |
| counter underflow guard | 39 | 防止 contention counter 减到负值的保护分支 |
| waiter cleanup | 4,544,084 | waiter record 清理次数 |
| anchor create | 112,919 | 创建 LockNest anchor |
| anchor refresh | 10,114,149 | 刷新已有 anchor |
| decay cleanup | 40,713 | LockBoost contention state 因 decay 被清理 |

`counter_underflow guard` 非零，但没有伴随 map failure、sched_ext error 或
负计数暴露。它说明当前实现的保护分支有效，但不能把 waiter bookkeeping
写成“绝对没有并发边界事件”。这一项仍应作为后续状态机审计和长时间测试的
残余风险。

## 5. 结果解释

### 5.1 当前 owner 证据链实际覆盖了什么

当前实现有两条故意分离的证据链：

1. kernel mutex path：通过 `mutex_lock`/`mutex_unlock` 和 kernel task
   identity 建立 owner 关系；
2. private pthread-mutex profile：在 ordinary private futex wait 上读取
   `uaddr + 8` 的 glibc/NPTL owner word，并通过 TID、task lookup 和同一
   `mm` 校验后，才标记为 `owner_valid`。

普通 `FUTEX_WAKE` 不进入 `lock_owners`，只刷新 `futex_hints`，因此不会单独
触发 LockBoost。shared futex、PI futex 和 requeue 也不会被伪装成 ordinary
private-futex owner evidence。

三次 MariaDB/sysbench coverage-only 运行显示：

- private futex owner resolver 的 pooled hit rate 为 20.06%；
- private futex miss 为 79.94%，这些事件不会触发 owner-based LockBoost；
- kernel mutex owner path 的 hit rate 为 99.89%，但事件数量远少于 private
  futex path；
- ordinary futex wake 数量很高，但它只支持 LockNest 的 advisory locality；
- 本次负载只观察到少量 shared futex，未观察到 PI 或 requeue，不能将这两条
  路径写成已经验证。

因此，MariaDB 热路径不能被论文简单描述为“已完整追踪 owner”。当前结果
只证明调度器能够在一部分符合当前 glibc/NPTL profile 的 private futex wait
上形成 verified-owner candidate，并对其余事件保守降级。

### 5.2 这张表不能证明什么

本实验没有提供以下证据：

- 没有应用侧 ground truth，因此 `owner resolver hit` 不能直接等价为
  “owner accuracy”；
- 没有证明 MariaDB 中每一个 private futex 都对应
  `pthread_mutex_t`；
- 没有覆盖 PI futex 或 requeue 的真实行为；
- 没有检测 allocator generation，因此不能宣称彻底解决
  free/realloc 后的地址复用；
- 没有测量 BPF CPU overhead、wake-to-run latency 或长期公平性；
- coverage-only 的 `-D` 诊断运行不能用于端到端性能排名。

## 6. 论文 owner claim 建议

### 6.1 推荐写法

建议在论文中使用下面这一类表述：

> For the evaluated x86-64 glibc/NPTL profile, LockCoop resolves the owner
> word of ordinary private pthread-mutex futex waits at a validated
> implementation-specific offset, and enables LockBoost only after the
> resulting TID passes task and address-space checks. Ordinary futex wake,
> shared futex, PI futex, requeue, and unresolved owner-word observations are
> conservatively excluded from owner-based boosting; unresolved observations
> may still provide advisory LockNest placement when a fresh anchor exists.

中文可以写为：

> 对于本文评估的 x86-64 glibc/NPTL 实现配置，LockCoop 在已验证的
> `pthread_mutex_t` owner-word 偏移上解析 ordinary private futex wait，并在
> TID 与地址空间检查通过后才启用 LockBoost。普通 futex wake、shared futex、
> PI futex、requeue 以及 owner word 未解析的观察均不会直接触发
> owner-based boosting；其中仍可利用新鲜 anchor 的事件只作为 LockNest 的
> advisory placement 依据。

### 6.2 不建议写法

不要写：

- “LockCoop tracks the owner of user-space futexes/mutexes”；
- “futex wait/wake directly reveals the current owner”；
- “LockBoost has complete owner coverage for MariaDB”；
- “owner hit rate equals owner accuracy”；
- “PI futex and requeue are supported”。

如果不增加应用侧 ground truth，论文中的指标名称应使用：

```text
verified-owner resolver hit/miss
```

而不是：

```text
owner accuracy
```

## 7. 可复现材料

coverage runner：

```text
scripts/run_lockcoop_phase12_db_coverage.py
```

默认现在是：

```text
--owner-offset 8
--threads 32
--runtime 10
```

如需关闭 owner resolver，可显式运行：

```text
python3 scripts/run_lockcoop_phase12_db_coverage.py --owner-offset -1
```

本次三次原始结果目录：

```text
results/phase12_db_coverage/phase12_db_coverage_20260730_220317
results/phase12_db_coverage/phase12_db_coverage_20260730_220453
results/phase12_db_coverage/phase12_db_coverage_20260730_220508
```

静态 Phase 1/2 invariant smoke：

```text
python3 scripts/lockcoop_phase12_smoke.py
```

结果：

```text
PASS: Phase 1/2 evidence invariants hold
```

## 8. 最终结论

当前 `owner_offset=8` coverage-only 实验支持一个有边界的论文结论：

1. LockBoost 的 owner 证据不是由普通 futex wake 猜测得到，而是来自
   kernel mutex 或经过当前 glibc/NPTL profile 校验的 owner-word resolver；
2. 在 MariaDB/sysbench 负载中，private futex owner resolver 只覆盖约
   20.06% 的 owner-resolution observations，其余事件保守地不进入
   owner-based boosting；
3. LockNest 可以独立消费 futex wake 的 advisory anchor，不要求每个 waiter
   都有 verified owner；
4. shared/PI/requeue 在当前负载中的观察频率不足以支持通用性声明；
5. map update failure 为 0，underflow guard 触发 39 次，实验未出现调度器失败。
   这些结果验证了当前防护的运行情况，尚不构成对所有并发情况的形式化证明。

因此，当前最稳妥的论文主张是“selective verified-owner boosting plus
advisory lock-locality placement”，而不是“通用用户态 futex owner tracking”。
