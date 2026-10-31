# MariaDB/OLTP LockCoop 性能崩溃排查实验日志

日期：2026-07-31

机器：Ampere Altra Max 双路 ARM 服务器

目标：在当前机器上复现论文中 MariaDB/Sysbench OLTP 的端到端性能效果，尤其 256 threads 下 `scx_lockcoop` 的 4k+ TPS 与低 p95 延迟。

本文中的“崩溃”指性能崩溃，即 TPS 突然跌到 default/scx_simple 同量级或在 60s run 中出现大幅下陷；不是指 MariaDB 或 sched_ext 进程 crash。

---

## 1. 最终结论

这次排查最后确认：

1. `scx_lockcoop` 代码本身不是主要问题。
2. 早期 MariaDB/OLTP 端到端性能差，主要由以下环境变量共同造成。按当前证据强弱排序，NUMA/socket 放置和 lock evidence 范围是强证据；datadir 从 `/root` 到 `/home` 更像是 60s 热态稳定性的辅助证据，不能单独解释全部提升：
   - MariaDB 版本没有对齐论文的 10.6 口径；
   - 当前双路 256 CPU ARM 机器上，未控制 NUMA/socket 放置时，DB 和锁热点会落到不稳定的跨 socket 状态；
   - MariaDB datadir 初始放在 `<workspace-root>/...`，即 root LV，和系统/容器/root 活动共享 `nvme0n1`；
   - 128MB buffer pool 下，Sysbench OLTP 已经明显受到存储路径、buffer-pool 热状态和页淘汰影响；
   - `scx_lockcoop` 采集全系统锁证据时，会把 VSCode/Codex/node 等非 MariaDB 线程的锁事件混进来，造成锚点污染。
3. 最稳复现配置是：
   - MariaDB 10.6.27；
   - 数据目录：`<data-workspace>/.mariadb106_data`；
   - DB 和 sysbench 均绑定到 node0；
   - `scx_lockcoop` 参数：`-A -m 10 -t 2 -M`；
   - 128MB buffer pool，O_DIRECT，durability 设置保持默认强刷语义。
4. 在该配置下，256 threads 已经跑出论文同量级效果：
   - 单次热态 60s：`4559.13 TPS / p95 127.81 ms`；
   - 256-thread 对照矩阵：`scx_lockcoop = 4339.94 TPS / p95 132.49 ms`；
   - 64/128/192/256 正式矩阵中，256 threads：`4077.45 TPS / p95 137.35 ms`；
   - 论文 5.3 口径复跑一轮，256 threads：`4081.91 TPS / p95 142.39 ms`；
   - 相比同轮 `default`：`4.17x`；
   - 相比同轮 `scx_simple`：`5.03x`。

---

## 2. 实验环境记录

### 2.1 内核与 sched_ext 状态

当前内核：

```text
Linux localhost.localdomain 6.17.0-lockcoop-scx617 #1 SMP Fri Jul 31 03:07:36 CST 2026 aarch64 aarch64 aarch64 GNU/Linux
```

sched_ext 当前空闲状态：

```text
disabled
```

### 2.2 CPU / NUMA 拓扑

```text
CPU(s):              256
Thread(s) per core:  1
Core(s) per socket:  128
Socket(s):           2
NUMA node0 CPU(s):   0-127
NUMA node1 CPU(s):   128-255
```

NUMA 距离：

```text
node   0   1
  0:  10  20
  1:  20  10
```

这个拓扑和论文中“two-socket, 128-logical-core Intel Xeon server”的有效 CPU 数不同。当前机器是双路 256 CPU。为了减少跨 socket 随机性，并让有效 CPU 数接近论文主实验口径，本次最后采用 `node0-all`：DB 和 sysbench 都限制在 node0 的 0-127 CPU。

### 2.3 MariaDB 版本与配置

论文原文写的是 MariaDB 10.6：

```text
Sysbench oltp_read_write against MariaDB 10.6.
Database: 16 tables, 10^6 rows/table; run duration: 60s;
thread counts: 64, 128, 192, 256; default InnoDB configuration.
```

当前最终使用版本：

```text
10.6.27-MariaDB-ubu2204
```

关键 InnoDB 配置：

```text
innodb_buffer_pool_size          134217728
innodb_flush_method              O_DIRECT
innodb_flush_log_at_trx_commit   1
sync_binlog                      0
innodb_doublewrite               ON
innodb_adaptive_hash_index       OFF
innodb_change_buffering          none
innodb_io_capacity               200
innodb_io_capacity_max           2000
innodb_lru_scan_depth            1536
innodb_read_io_threads           4
innodb_write_io_threads          4
innodb_flush_neighbors           1
max_connections                  1024
max_prepared_stmt_count          100000
performance_schema               OFF
```

最终运行中的 MariaDB 命令行：

```text
<workspace>/.mariadb106_root/usr/sbin/mariadbd
  --no-defaults
  --basedir=<workspace>/.mariadb106_root/usr
  --datadir=<data-workspace>/.mariadb106_data
  --socket=<workspace>/.mariadb106_run/mariadb106.sock
  --port=3316
  --max-connections=1024
  --max-prepared-stmt-count=100000
  --innodb-buffer-pool-size=134217728
  --innodb-buffer-pool-instances=1
  --skip-log-bin
  --performance-schema=OFF
```

最终 affinity：

```text
pid 307368's current affinity list: 0-127
Cpus_allowed_list: 0-127
Mems_allowed_list: 0-1
```

注意：虽然 CPU 绑到 node0，但 `Mems_allowed_list` 仍显示 0-1；实际 `numastat -p` 显示绝大多数 MariaDB 私有内存在 node0。这是因为进程启动使用了 `numactl --cpunodebind=0 --membind=0`，但 `/proc/$pid/status` 的 allowed list 并不完全等价于当前页分布。

### 2.4 存储布局

root LV：

```text
root openeuler 759.40g linear /dev/nvme0n1p3(0)
```

home LV：

```text
home openeuler 1.00t linear /dev/nvme1n1p1(0)
home openeuler 1.00t linear /dev/nvme0n1p3(194407)
```

解释：

- `<workspace>` 在 `/`，也就是 root LV；
- root LV 只在 `nvme0n1p3` 上；
- `/home` LV 的前段位于 `nvme1n1p1`；
- 当前 `/home` 基本空闲，所以将 MariaDB datadir 放到 `<home-data>/...` 后，实际绕开了 root LV 上系统活动和容器活动的干扰；
- 历史 raw result 路径也在 `<result-workspace>/...`，这提示原实验环境和当前最初的 `/root` datadir 并不一致。

---

## 3. 初始症状

最初的问题是：futex/mutex 微基准显示 `scx_lockcoop` 有明显效果，但 MariaDB/Sysbench OLTP 的 test3 端到端结果经常很差，甚至所有调度器都比 default 更差。

这个现象很容易误判为：

- `scx_lockcoop` 当前源码有 bug；
- 历史 commit 才能跑出论文效果；
- microbenchmark 的结论不能迁移到 MariaDB；
- sched_ext 在当前 6.17/ARM 环境上不可靠。

后续排查表明，这些都不是主因。真正的问题是多个环境变量没有对齐，导致 `scx_lockcoop` 的收益被 DB 版本、NUMA、I/O 路径和热状态掩盖。

---

## 4. 排查时间线与实验记录

### 4.1 第一阶段：确认不要从历史 commit 找代码

用户明确要求不要再从历史 commit 找代码，优先使用：

- 当前源码；
- 论文提到的数据库版本；
- 当前机器环境。

因此后续没有再从历史 commit 回滚 `scx_lockcoop`，而是围绕当前代码和实验环境排查。

当前 `scx_lockcoop` 二进制参数帮助中显示：

```text
Usage: scx_lockcoop [-f] [-H] [-A] [-W] [-M] [-N] [-O] [-U] [-K] [-p] [-v]
                      [-t threshold] [-d decay_ms] [-e expiry_ms]
                      [-s escape_stride] [-m min_seq]

  -A              Force busy-anchor placement
  -M              Collect lock/futex evidence only from mariadbd threads
  -K              Ignore kernel mutex evidence
  -t <count>      Set high contention threshold
  -m <count>      Require at least N lock-nest updates before handoff
```

最终使用参数：

```bash
scx_lockcoop -A -m 10 -t 2 -M
```

---

### 4.2 第二阶段：数据库版本对齐

论文文字明确写 MariaDB 10.6。因此先确认并使用 `.mariadb106_root`：

```text
10.6.27-MariaDB-ubu2204
```

这里需要一个重要限定：后续核对 `LockCoop/results/mariadb_sysbench_oltp/raw_sources/formal_mariadb_oltp_20260630_130448/performance/summary_runs.csv` 时，发现保留下来的历史 raw summary 里，`mariadb_version` 列标的是：

```text
10.11.14-MariaDB-0ubuntu0.24.04.1
```

因此，不能把这一步简单理解成“10.11 从机制上必然跑不起来”。更准确的说法是：

- 论文正文口径写的是 MariaDB 10.6，本次为了降低混杂变量，最终优先切到 10.6；
- 当前机器上我临时搭起来的几套 10.11 路径/配置，在 256-thread OLTP 下明显偏低；
- 历史 raw 中确实存在 MariaDB 10.11.14 的高性能样本，说明“数据库大版本”不是单独充分解释；
- 真正决定端到端结果的是“数据库版本/发行版打包 + datadir 存储路径 + NUMA 放置 + buffer-pool 热状态 + lock evidence 范围”的组合。

之前现场测试过的 MariaDB 10.11，性能明显偏低。典型 256-thread 60s 结果：

| 数据库/实验 | scheduler | TPS | p95 ms | 结论 |
|---|---:|---:|---:|---|
| MariaDB 10.11 smoke | default | 461.02 | 1533.66 | 基线极低 |
| MariaDB 10.11 smoke | scx_simple | 581.56 | 1258.08 | 仍低 |
| MariaDB 10.11 smoke | scx_lockcoop | 518.47 | 1427.08 | 未体现论文效果 |
| Ubuntu MariaDB 10.11 exact | default | 469.74 | 1304.21 | 低 |
| Ubuntu MariaDB 10.11 exact | scx_lockcoop | 1094.32 | 511.33 | 有改善但远低于论文量级 |
| MariaDB 10.6 node0-all | scx_lockcoop `-A -m10 -t2` | 4087.26 / 3989.29 | 147.61 / 150.29 | 回到 4k+ 区间 |

这一阶段结论：

- MariaDB 版本/安装口径是早期重要混杂变量；
- 当前现场测试的 10.11 路径表现和论文口径差距太大，不能作为最终复现路径；
- 按论文文字切到 10.6 后，`scx_lockcoop` 很快回到 4k TPS 附近；
- 但历史 raw summary 显示 10.11.14 也曾跑出高态，因此最终归因不能简化为“只因为 10.11”。

---

### 4.3 第三阶段：NUMA/socket 放置排查

当前机器是 2 socket × 128 core，共 256 CPU。未绑定时，DB 线程、sysbench 线程和 lock anchor 会在两个 socket 间随机扩散。

早期观察：

- 性能差的 run 中，活跃 holder/anchor 很多在 node1，即 CPU 128-255；
- 性能好的 run 中，热点更多集中在 node0；
- split DB/client CPU set，例如 DB 0-95、client 96-127，反而更差；
- DB + client 同在 node0 最稳。

node0-all 256-thread 30s 初步对照：

| scheduler | TPS | p95 ms |
|---|---:|---:|
| default | 1429.99 | 467.30 |
| scx_simple | 1454.14 | 467.30 |
| scx_locknest | 3914.10 | 139.85 |
| scx_lockcoop `-A -m10 -t2` | 4427.06 | 134.90 |

这个结果说明：

- `scx_lockcoop` 在当前代码上是有效的；
- 之前端到端差不是因为调度器完全无效；
- NUMA/socket 放置是重要放大器。

同样是 MariaDB 10.6、256 threads、30s，使用接近相同的 `scx_lockcoop` 参数口径（`threshold=4`、`force_busy_anchor=on`、`min_seq=10`、未启用 `-M`）时，已有日志里可以看到 NUMA 控制带来的阶梯式改善：

| NUMA 放置 | scheduler | runs | TPS | p95 ms | 说明 |
|---|---|---:|---:|---:|---|
| 未绑定 | scx_lockcoop | 3 | 2198.10 | 316.05 | r1 高，r2/r3 掉到 1.4k 左右，稳定性差 |
| 只绑 DB 到 node0 | scx_lockcoop | 2 | 3508.70 | 198.68 | 明显恢复 |
| DB + sysbench 都绑 node0 | scx_lockcoop | 2 | 3978.56 | 158.63 | 更稳，接近 4k |
| 未绑定 | scx_locknest | 3 | 1331.73 | 419.97 | 同样低且不稳 |
| 只绑 DB 到 node0 | scx_locknest | 2 | 3241.28 | 202.29 | 明显恢复 |
| DB + sysbench 都绑 node0 | scx_locknest | 2 | 3797.26 | 158.66 | 更稳 |

这张表比“10.6 一切换就变好”更能说明问题：10.6 是必要对齐项，但在这台双路 256 CPU 机器上，如果不管 NUMA，lock-aware 调度器仍然可能掉到很低；把 DB 和客户端都限制在 node0 后，吞吐和 p95 才一起恢复。

最终采取：

```bash
numactl --cpunodebind=0 --membind=0 ./.mariadb106_control.sh restart
```

以及 sysbench wrapper：

```bash
numactl --cpunodebind=0 --membind=0 /usr/bin/sysbench ...
```

---

### 4.4 第四阶段：调度器参数与证据范围排查

在 256-thread 30s 下扫 `scx_lockcoop` 参数：

| 参数 | TPS | p95 ms | 观察 |
|---|---:|---:|---|
| `-A -m10 -t2` | 1272.01 | 511.33 | 这次起点是冷/不稳状态，性能崩 |
| `-A -m10 -t2 -M` | 4644.09 | 123.28 | 最好 |
| `-A -m10 -t2 -K` | 4565.73 | 132.49 | 接近最好 |
| `-A -m10 -t2 -M -K` | 4290.00 | 142.39 | 下降 |
| `-A -m16 -t2` | 4178.58 | 150.29 | 可用但不优 |
| `-A -m8 -t2` | 4244.26 | 144.97 | 可用但不优 |

解释：

- `-M` 只采集 mariadbd 线程的锁/futex 证据，明显改善；
- `-K` 忽略 kernel mutex evidence 也能高，说明非 MariaDB kernel mutex 噪声确实会污染；
- 但 `-M -K` 比 `-M` 低，说明 MariaDB 自己的 kernel mutex evidence 仍然有价值；
- 所以问题不是“kernel mutex evidence 都错”，而是“全系统证据里有非 MariaDB 噪声”。

scx 日志中也能看到 VSCode/Codex/node 等非 MariaDB 线程参与 lock nest/anchor 状态。这在一个远程开发环境里很正常，但对 MariaDB 单工作负载复现是噪声。

最终选择：

```bash
SCX_ARGS_SCX_LOCKCOOP='-A -m 10 -t 2 -M'
```

---

### 4.5 第五阶段：60s run 中的性能崩溃复现

使用 root datadir、node0-all、`-M` 后，30s 可以很高，但 60s 仍出现崩溃：

结果目录：

```text
test3-SysbenchOLTP /result/mariadb106_node0all_256_mariadbd_only_60s_20260731_194038_scx_lockcoop_t256_d60/
```

两次 60s：

| run | TPS | p95 ms | max ms |
|---:|---:|---:|---:|
| r1 | 4149.09 | 147.61 | 795.64 |
| r2 | 3497.93 | 155.80 | 4189.81 |

r2 每 10 秒曲线：

| 时间 | TPS | p95 ms |
|---:|---:|---:|
| 10s | 3816 | 170 |
| 20s | 3628 | 159 |
| 30s | 2264 | 159 |
| 40s | 3613 | 153 |
| 50s | 3845 | 148 |
| 60s | 3825 | 148 |

现象：

- 崩溃集中在 20-30s 这一桶；
- p95 没有对应暴涨，说明不是大多数事务同时变慢；
- max latency 到 4.19s，说明有少量请求被长时间卡住；
- 60s 平均值被一个短窗口显著拉低。

这不像是调度器持续失效，更像是系统/存储层短停顿。

---

### 4.6 第六阶段：监控验证 CPU 与 I/O

为了判断 60s 崩溃是否来自 CPU，采集了：

- `vmstat -w 1`；
- `/proc/diskstats`；
- sysbench 10s report interval；
- MariaDB status before/after。

监控结果目录：

```text
test3-SysbenchOLTP /result/monitor_mariadb106_m_only_20260731_194518/
```

带监控的一轮 60s：

```text
TPS: 4128.44
p95: 147.61 ms
max: 733.70 ms
```

逐 10s：

| 时间 | TPS | p95 ms |
|---:|---:|---:|
| 10s | 4166 | 156 |
| 20s | 4106 | 145 |
| 30s | 4108 | 153 |
| 40s | 4262 | 140 |
| 50s | 4056 | 148 |
| 60s | 4081 | 150 |

vmstat 观察：

- 整机 CPU idle 大多在 90% 以上；
- user/system CPU 都很低；
- runnable 和 blocked 进程数有波动；
- `bi/bo` 很高。

diskstats 观察：

- 正式运行期间持续出现每秒数百 MB 级别读写；
- `dm-0` / `nvme0n1` 每秒读写量、I/O 时间和 weighted I/O 时间都很高；
- 例如多秒内读写可达到数百 MB/s，`io_ms` 接近 700-1000 ms/s。

结论：

- 不是 CPU 被打满；
- MariaDB 进程实际只消耗大约 6-8 个 CPU；
- 128MB buffer pool 下，OLTP 已经强烈受到存储和页淘汰路径影响；
- `scx_lockcoop` 把锁路径救起来后，剩余瓶颈转向 I/O 长尾。

---

### 4.7 第七阶段：datadir 位置假设

观察到：

- 当前仓库和原始 datadir 在 `<workspace-root>/...`；
- root LV 位于 `nvme0n1p3`；
- `/home` LV 前段位于 `nvme1n1p1`；
- `/home` 几乎空闲；
- 历史实验 raw result 路径也是 `<result-workspace>/...`。

因此提出假设：

> 当前性能崩溃的一部分来自 datadir 放在 root LV，与系统/容器/root 活动共用 `nvme0n1`。将 MariaDB 10.6 datadir 移到 `/home` 后，可以降低 I/O 干扰，改善 60s 稳态。

执行方式：

```bash
./.mariadb106_control.sh stop
mkdir -p <data-workspace>
cp -a .mariadb106_data <data-workspace>/.mariadb106_data
M106_DATA=<data-workspace>/.mariadb106_data \
  numactl --cpunodebind=0 --membind=0 ./.mariadb106_control.sh start
```

原始 `<workspace>/.mariadb106_data` 保留未删。

---

### 4.8 第八阶段：home datadir 验证

第一次 home datadir 双 run：

结果目录：

```text
test3-SysbenchOLTP /result/mariadb106_home_node0_256_mariadbd_only_60s_20260731_194909_scx_lockcoop_t256_d60/
```

| run | TPS | p95 ms | 解释 |
|---:|---:|---:|---|
| r1 | 1226.69 | 530.08 | 刚复制后 buffer pool dump 冷，不能作为稳态 |
| r2 | 4559.13 | 127.81 | 热态后达到论文同量级 |

r2 逐 10s：

| 时间 | TPS | p95 ms |
|---:|---:|---:|
| 10s | 4635 | 135 |
| 20s | 4719 | 121 |
| 30s | 4493 | 128 |
| 40s | 4512 | 128 |
| 50s | 4502 | 128 |
| 60s | 4506 | 130 |

这个结果非常关键：

- 在已经控制 node0 放置、并使用 `-M` 过滤 MariaDB 线程证据的前提下，home datadir 的热态 60s 达到 4559 TPS；
- 与论文正文 `4587 TPS` 极其接近；
- 说明 60s 长 run 的剩余波动更像 I/O 路径和热状态问题，而不是 lockcoop 逻辑失效。

但这里也必须保守表述：这不是一个干净的“只移动 datadir”单变量实验。因为第一次 home run 只有 1226.69 TPS，说明刚复制后的 buffer pool/page-cache 状态很冷；后续变高包含了热态因素。因此 `/home` datadir 的证据应理解为“降低 60s 热态 I/O 长尾风险”，而不是“搬目录本身必然立刻提升”。

随后跑热态三连：

结果目录：

```text
test3-SysbenchOLTP /result/mariadb106_home_node0_256_mariadbd_only_60s_hot3_20260731_195256_scx_lockcoop_t256_d60/
```

| run | TPS | p95 ms |
|---:|---:|---:|
| r1 | 4355.92 | 139.85 |
| r2 | 4056.96 | 144.97 |
| r3 | 4299.79 | 134.90 |
| mean | 4237.56 | 139.91 |

作为对照，恢复出的历史 raw 数据中，256-thread `scx_lockcoop` 全部 run 如下。注意，这批 raw summary 的 `mariadb_version` 标为 `10.11.14-MariaDB-0ubuntu0.24.04.1`，raw 路径位于 `<result-workspace>/...`；因此这里主要把它作为“历史高态分布、outlier 形态、/home 路径实验环境”的参照，而不是作为“10.6 vs 10.11”的单变量证据：

```text
4673.79, 4639.62, 4697.46, 4650.34, 2050.85, 2340.60,
4645.47, 4567.62, 4634.27, 4389.74, 4728.61, 4701.06, 4480.94
```

历史全量均值：

```text
mean TPS = 4246.18
median TPS = 4639.62
mean p95 = 144.78 ms
```

对比：

- 当前 home hot3 均值：`4237.56 TPS / p95 139.91 ms`
- 历史全量均值：`4246.18 TPS / p95 144.78 ms`

两者几乎重合。论文正文中的 `4587 TPS` 更接近历史数据中稳定高态 run 的代表值，而不是包含所有低 outlier 的均值。

---

## 5. 最终正式矩阵

最终脚本：

```text
test3-SysbenchOLTP /scripts/run_mariadb106_home_node0all_matrix.sh
```

最终 64/128/192/256，60s，RUNS=1 矩阵：

结果 tag：

```text
mariadb106_home_node0all_formal_20260731_210407
```

### 5.1 TPS

| threads | default | scx_simple | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|
| 64 | 5247.57 | 5090.35 | 4997.74 | 5717.04 |
| 128 | 2003.40 | 2063.41 | 4511.69 | 4482.54 |
| 192 | 1211.42 | 1186.23 | 4260.67 | 4050.28 |
| 256 | 976.94 | 811.35 | 3588.11 | 4077.45 |

### 5.2 p95 latency

| threads | default | scx_simple | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|
| 64 | 29.19 | 30.81 | 27.17 | 21.89 |
| 128 | 164.45 | 161.51 | 64.47 | 63.32 |
| 192 | 411.96 | 442.73 | 102.97 | 101.13 |
| 256 | 719.92 | 816.63 | 155.80 | 137.35 |

### 5.3 `scx_lockcoop` speedup

| threads | vs default | vs scx_simple |
|---:|---:|---:|
| 64 | 1.09x | 1.12x |
| 128 | 2.24x | 2.17x |
| 192 | 3.34x | 3.41x |
| 256 | 4.17x | 5.03x |

这个趋势和论文核心叙事一致：

- 低并发 64 threads 下，锁竞争不强，收益较小；
- 128 threads 开始出现明显分层；
- 192/256 threads 下，default/scx_simple 崩塌，lock-aware 调度器维持 4k TPS 左右；
- 256 threads 下 `scx_lockcoop` 同时提升 TPS、降低 p95。

### 5.4 论文 5.3 口径复跑：DB + sysbench 绑 node0，tuned `scx_lockcoop`

用户要求按论文 5.3 的 MariaDB/Sysbench OLTP 口径再跑一轮，并采用当前调优后最好的 `scx_lockcoop`。这一轮的配置如下：

```text
TAG: paper53_node0all_tuned_lc_20260731_224532
时间: 2026-07-31 22:45:32 +08:00
MariaDB: 10.6.27-MariaDB-ubu2204
datadir: <data-workspace>/.mariadb106_data
DB 绑定: numactl --cpunodebind=0 --membind=0
sysbench 绑定: .node0bin/sysbench wrapper
threads: 64, 128, 192, 256
RUNS: 1
RUN_TIME: 60s
WARMUP_TIME: 10s
schedulers: default, scx_simple, scx_locknest, scx_lockcoop
scx_lockcoop 参数: -A -m 10 -t 2 -M
```

注意：这轮不是“八个调度器全量 sweep”，而是论文主线的 4 个对照策略：

- `default`
- `scx_simple`
- `scx_locknest`
- `scx_lockcoop`（tuned）

之所以只保留这 4 个，是因为这一轮的目标是复核论文 5.3 的主性能结论，并把 `scx_lockcoop` 与最关键的 baseline / lock-aware 对照放在一起看；不是做全部候选调度器的大扫除。

`scx_lockcoop` attach 日志确认：

```text
threshold=2
force_busy_anchor=on
mariadbd_only=on
same_node=on
escape_stride=8
min_seq=10
owner_progress=on
futex_evidence=on
mutex_evidence=on
```

脚本最终正常完成并写出汇总 CSV；唯一失败的是绘图脚本，因为当前 Python 环境缺少 `matplotlib`：

```text
ModuleNotFoundError: No module named 'matplotlib'
```

这只影响 PNG 图，不影响 sysbench 原始结果或 CSV 汇总。

#### 5.4.1 TPS

| threads | default | scx_simple | scx_locknest | scx_lockcoop tuned |
|---:|---:|---:|---:|---:|
| 64 | 5381.58 | 4986.23 | 5065.71 | 5554.47 |
| 128 | 2098.33 | 2165.06 | 4387.55 | 4972.99 |
| 192 | 1143.82 | 1179.90 | 4249.90 | 5062.18 |
| 256 | 958.03 | 905.22 | 4059.84 | 4081.91 |

#### 5.4.2 p95 latency

| threads | default | scx_simple | scx_locknest | scx_lockcoop tuned |
|---:|---:|---:|---:|---:|
| 64 | 28.16 | 30.81 | 27.17 | 23.10 |
| 128 | 158.63 | 153.02 | 65.65 | 56.84 |
| 192 | 450.77 | 419.45 | 104.84 | 82.96 |
| 256 | 733.00 | 759.88 | 144.97 | 142.39 |

#### 5.4.3 max latency

| threads | default | scx_simple | scx_locknest | scx_lockcoop tuned |
|---:|---:|---:|---:|---:|
| 64 | 87.88 | 78.01 | 100.78 | 86.14 |
| 128 | 419.15 | 366.94 | 236.54 | 176.31 |
| 192 | 1035.25 | 921.27 | 386.33 | 413.60 |
| 256 | 2030.84 | 1812.67 | 528.34 | 858.29 |

#### 5.4.4 tuned `scx_lockcoop` speedup

| threads | vs default TPS | vs scx_simple TPS | p95 reduction vs default | p95 reduction vs scx_simple |
|---:|---:|---:|---:|---:|
| 64 | 1.03x | 1.11x | 1.22x lower | 1.33x lower |
| 128 | 2.37x | 2.30x | 2.79x lower | 2.69x lower |
| 192 | 4.43x | 4.29x | 5.43x lower | 5.06x lower |
| 256 | 4.26x | 4.51x | 5.15x lower | 5.34x lower |

#### 5.4.5 本轮观察

- 256 threads 下，tuned `scx_lockcoop` 为 `4081.91 TPS / p95 142.39 ms`，再次稳定落在论文目标的 p95 区间。
- 128/192 threads 这轮特别强：`scx_lockcoop` 分别达到 `4972.99 TPS` 和 `5062.18 TPS`。
- 256 threads 下，`scx_locknest` 与 tuned `scx_lockcoop` TPS 非常接近：`4059.84` vs `4081.91`；但 `scx_lockcoop` p95 更低：`142.39 ms` vs `144.97 ms`。
- 这轮结果和前一版 formal 结果方向一致：64 threads 收益小，128 threads 起收益明显，192/256 threads 下 default/scx_simple 崩塌，而 lock-aware 调度器维持 4k+ TPS。
- 本轮 256-thread max latency 上，`scx_lockcoop` 的 `858.29 ms` 高于 `scx_locknest` 的 `528.34 ms`，但显著低于 default/scx_simple 的 1.8-2.0s 级别。

证据目录：

```text
test3-SysbenchOLTP /result/paper53_node0all_tuned_lc_20260731_224532_*
```

---

## 6. 为什么 microbenchmark 有效果，但 test3 起初没效果？

微基准只验证机制本身：

- owner progress 是否能减少持锁者 off-CPU；
- handoff locality 是否能减少锁交接跨 CPU/NUMA；
- futex/mutex 证据是否能驱动调度器做出正确局部决策。

MariaDB/Sysbench OLTP 端到端多了很多变量：

1. 数据库版本差异；
2. buffer pool 只有 128MB，数据集远大于 buffer pool；
3. O_DIRECT 下真实读写压力很高；
4. redo/fsync/doublewrite/page cleaner 等后台路径会引入长尾；
5. 当前机器是双路 256 CPU，跨 socket 随机性比论文目标更复杂；
6. 远程开发环境里有 VSCode/Codex/node 等进程，它们也会产生锁证据；
7. datadir 在 root LV 时，会与系统活动和容器路径共享同一块 NVMe。

因此，微基准成功并不自动保证 test3 立即成功。微基准说明 lockcoop 机制有效；test3 还要求 DB 版本、NUMA、存储路径和热状态都足够干净。

本次排查最终证明：

- 当这些外部变量没对齐时，lockcoop 的收益被 I/O/NUMA/证据噪声掩盖；
- 当这些变量对齐后，MariaDB/OLTP 端到端结果恢复到论文同量级。

---

## 7. 具体尝试过的方案与结论

### 7.1 有效方案

| 方案 | 是否有效 | 说明 |
|---|---:|---|
| 使用 MariaDB 10.6 | 是 | 对齐论文口径后，性能从 10.11 的低区间回到 4k TPS 附近 |
| DB 绑 node0 | 是 | 减少跨 socket 随机性 |
| DB + sysbench 都绑 node0 | 是 | 最稳的 NUMA 控制方式 |
| `scx_lockcoop -A -m10 -t2` | 是 | 基础高性能参数 |
| `-M` 仅采 mariadbd 证据 | 是 | 过滤非 MariaDB 锁证据，30s 可到 4644 TPS |
| datadir 移到 `/home` | 是 | 60s 热态可到 4559 TPS，最终矩阵 256 为 4077 TPS |
| 等待 buffer pool dump load 完成 | 是 | 避免异步加载干扰，但不能单独解决所有问题 |

### 7.2 无效或较差方案

| 方案 | 结果 | 结论 |
|---|---:|---|
| MariaDB 10.11 | 256 threads 远低于论文 | 版本口径不对，不能作为最终路径 |
| 只调 I/O 参数：`io_capacity=2000`、更多 I/O threads、`flush_neighbors=0` | 3617.98 TPS / p95 170.48 ms | 比默认 InnoDB 更差 |
| DB 0-95、sysbench 96-127 split | 低于 node0-all | 分裂锁交接和 client/DB locality，不如共享 node0 |
| 单纯加长 warmup | 有时仍低 | warmup 不是充分条件；datadir 和热状态更关键 |
| `-M -K` | 4290 TPS，低于 `-M` | MariaDB 自己的 kernel mutex evidence 仍有价值 |
| `-m8` / `-m16` | 可用但不优 | 不如 `-m10 -t2 -M` 稳 |

---

## 8. 用户追问补充：术语、参数和 NUMA 三档性能

这一节专门记录后续追问中澄清过的几个容易混淆点。它的作用是把聊天里的解释固化到实验日志里，避免以后看到目录名或参数名时误读。

### 8.1 `stock_lc` / `stock scx_lockcoop` 是什么意思？

`stock_lc` 是排查过程中使用的实验 tag，不是一个严格的论文术语。

在本次日志里，它的准确含义是：

```text
当前编译出的 scx_lockcoop 二进制，直接使用默认参数运行。
```

它不表示：

- 不是上游 Linux/sched-ext 自带的 stock scheduler；
- 不是论文历史 commit 的原版实现；
- 不是“源码完全未修改”的严格版本标签；
- 也不是 MariaDB stock/default 配置。

更准确的命名应该是：

```text
default-args scx_lockcoop
```

这轮 `stock_lc` 中的 `scx_lockcoop` 日志参数为：

```text
threshold=4
handoff=on
force_busy_anchor=off
mariadbd_only=off
same_node=on
escape_stride=8
min_seq=0
owner_progress=on
futex_evidence=on
mutex_evidence=on
```

也就是说，`stock scx_lockcoop` 没有启用后续高性能路径里用到的：

```text
-A
-m 10
-t 2
-M
```

因此它在当前远程开发环境中的 256-thread MariaDB/Sysbench 结果很差：

| scheduler | 参数口径 | TPS | p95 ms | max ms | 结果目录 |
|---|---|---:|---:|---:|---|
| scx_lockcoop | stock/default args | 558.43 | 1533.66 | 4082.77 | `mariadb106_stock_lc_256_30s_20260731_scx_lockcoop_t256_d30` |

这说明默认参数在当前环境下不稳，但不能据此推出 lockcoop 机制本身失效。

### 8.2 `-A -m10` 是什么意思？

`-A -m10` 是 `scx_lockcoop` 的两个运行参数，完整写法通常是：

```bash
scx_lockcoop -A -m 10
```

含义如下：

| 参数 | 源码变量 | 作用 | 直观解释 |
|---|---|---|---|
| `-A` | `force_busy_anchor=on` | Force busy-anchor placement | 当 lock anchor CPU 忙时，仍然强制把被唤醒线程放到 anchor CPU 上；更激进地维护锁交接局部性 |
| `-m 10` | `min_handoff_seq=10` | 至少观察到 10 次 handoff/lock-nest 更新后才启用定向 handoff | 避免刚出现的偶然锁事件马上影响调度 |

源码 dispatch 逻辑里的关键条件是：

```text
if (min_handoff_seq && n_info->handoff_seq < min_handoff_seq)
    goto fallback;

...

if (force_busy_anchor && cpu is allowed)
    insert task on anchor CPU;
```

所以 `-A -m10` 可以理解成：

> 对已经稳定出现的锁热点，强制维护 lock anchor locality；但对证据还太少的新锁，先回退到默认选择路径。

在 DB + sysbench 都绑 node0 的情况下，`-A -m10` 已经测试过：

| NUMA 放置 | scheduler | 参数 | runs | TPS | p95 ms | 结果目录 |
|---|---|---|---:|---:|---:|---|
| DB + sysbench 都绑 node0 | scx_lockcoop | `-A -m10` | 2 | 3978.56 | 158.63 | `mariadb106_node0all_repro_256_30s_20260731_scx_lockcoop_t256_d30` |

两次 run 分别是：

| run | TPS | p95 ms |
|---:|---:|---:|
| r1 | 3981.10 | 158.63 |
| r2 | 3976.02 | 158.63 |

这说明在 node0-all 控制后，单独 `-A -m10` 就已经把 `scx_lockcoop` 拉到接近 4k TPS。

### 8.3 `-t2 -M` 是什么意思？

`-t2 -M` 通常和 `-A -m10` 一起使用：

```bash
scx_lockcoop -A -m 10 -t 2 -M
```

含义如下：

| 参数 | 源码变量 | 作用 | 直观解释 |
|---|---|---|---|
| `-t 2` | `high_contention_threshold=2` | 将高竞争阈值设为 2 | 更早把某个锁识别为值得介入的热点锁 |
| `-M` | `mariadb_only_evidence=on` | 只从 `mariadbd` 线程采集 lock/futex 证据 | 过滤 VSCode、Codex、node、shell 等非数据库进程的锁证据噪声 |

需要特别强调：`-M` 不是“只调度 mariadbd”。它只是限制 lock/futex evidence 的来源：

```text
调度器仍然运行；
但 lockcoop 的因果判断只看 mariadbd 线程产生的锁/唤醒证据。
```

在 DB + sysbench 都绑 node0、MariaDB 10.6、256 threads、30s 的参数扫描里：

| 参数 | TPS | p95 ms | 解释 |
|---|---:|---:|---|
| `-A -m10` | 3978.56 | 158.63 | node0-all 后的基础高性能口径 |
| `-A -m10 -t2` | 4427.06 | 134.90 | 降低热点阈值后更接近论文目标 |
| `-A -m10 -t2 -M` | 4644.09 | 123.28 | 过滤非 MariaDB 证据后最好 |

因此，本次最终选择 `-A -m10 -t2 -M` 的原因是：

- `-A`：更强地保持 lock anchor locality；
- `-m10`：避免过早相信噪声锁；
- `-t2`：让 MariaDB 热点锁更早触发 lock-aware 路径；
- `-M`：把远程开发环境中的非 MariaDB 锁事件排除掉。

### 8.4 MariaDB 10.6 下，三种 NUMA 绑定状态的性能

这一组表只讨论 MariaDB 10.6、256 threads。需要注意：

- “未绑定”和“只绑 DB 到 node0”是排查阶段样本，不是完整 formal matrix；
- “DB + sysbench 都绑 node0”既有 30s root datadir 排查矩阵，也有 60s home datadir formal 矩阵；
- 不同表里的 run duration、参数和 datadir 不完全一致，所以它们适合判断趋势，不应被当作严格单变量论文图。

#### 8.4.1 未绑定 NUMA

未绑定时，DB 线程、sysbench 线程和 lock anchor 都可能跨两个 socket 扩散。这一档现有样本波动很大，尤其 lock-aware scheduler 的结果非常依赖当次热点所在位置。

| scheduler | 参数口径 | runs | duration | TPS | p95 ms | max ms | 说明 |
|---|---|---:|---:|---:|---:|---:|---|
| default | default | 1 | 60s | 623.42 | 1050.76 | 3160.64 | 未绑定 10.6 baseline |
| scx_simple | default args | 1 | 30s | 660.25 | 995.51 | 2965.16 | 未绑定，仍低 |
| scx_locknest | stock/default | 1 | 30s | 3915.82 | 155.80 | 1764.55 | 单次高态，说明机制可达高性能，但未证明稳定 |
| scx_locknest | default/repro | 3 | 30s | 1331.73 | 419.97 | 1514.19 mean | 三次均值低，说明未绑定时不稳 |
| scx_lockcoop | stock/default args | 1 | 30s | 558.43 | 1533.66 | 4082.77 | 默认参数很差 |
| scx_lockcoop | `-A -m10` | 3 | 30s | 2198.10 | 316.05 | 1063.31 mean | r1 高，r2/r3 掉到约 1.4k |
| scx_lockcoop | `-A -m10` | 1 | 60s | 3130.90 | 240.02 | 2268.18 | 单次 60s 有改善但仍不稳 |
| scx_lockcoop | `-A -m10` warm60 | 2 | 60s | 1458.43 | 386.81 | 1637.37 mean | 另一组 60s 仍低 |

未绑定阶段的核心观察：

- 10.6 能把部分 run 拉起来，但并不能保证稳定接近论文目标；
- `stock scx_lockcoop` 明显很差；
- 同一类 lock-aware 策略会出现从 1.3k 到 3.9k 的巨大波动；
- 这说明 NUMA/socket 随机分布是主要混杂变量。

#### 8.4.2 只绑 DB 到 node0

这一档只测了 `scx_locknest` 和 `scx_lockcoop` 两个策略，没有完整 default/scx_simple 对照。

| scheduler | 参数口径 | runs | duration | TPS | p95 ms | max ms | 结果目录 |
|---|---|---:|---:|---:|---:|---:|---|
| default | default | - | - | 未测 | 未测 | 未测 | - |
| scx_simple | default args | - | - | 未测 | 未测 | 未测 | - |
| scx_locknest | default | 2 | 30s | 3241.28 | 202.29 | 3075.41 mean | `mariadb106_node0db_repro_256_30s_20260731_scx_locknest_t256_d30` |
| scx_lockcoop | `-A -m10` | 2 | 30s | 3508.70 | 198.68 | 3013.42 mean | `mariadb106_node0db_repro_256_30s_20260731_scx_lockcoop_t256_d30` |

相比未绑定的同口径均值：

| scheduler | 未绑定 TPS / p95 | 只绑 DB TPS / p95 | 变化 |
|---|---:|---:|---|
| scx_locknest | 1331.73 / 419.97 ms | 3241.28 / 202.29 ms | TPS 约 2.43x，p95 减半 |
| scx_lockcoop `-A -m10` | 2198.10 / 316.05 ms | 3508.70 / 198.68 ms | TPS 约 1.60x，p95 明显下降 |

结论：

- 只绑 DB 到 node0 已经能显著减少跨 socket 干扰；
- 但 sysbench/client 仍可能在另一个 socket 上，与 DB 交互产生额外 NUMA 成本；
- 因此它比未绑定好，但不是最终最稳配置。

#### 8.4.3 DB + sysbench 都绑 node0

这一档是最终最稳的 NUMA 控制方式。它有两类数据：30s root datadir 排查矩阵，以及 60s home datadir formal 矩阵。

30s root datadir，node0-all：

| scheduler | 参数口径 | runs | duration | TPS | p95 ms | max ms | 结果目录 |
|---|---|---:|---:|---:|---:|---:|---|
| default | default | 1 | 30s | 1429.99 | 467.30 | 1286.15 | `mariadb106_node0all_256_short_20260731_193039_default_t256_d30` |
| scx_simple | default args | 1 | 30s | 1454.14 | 467.30 | 1286.36 | `mariadb106_node0all_256_short_20260731_193039_scx_simple_t256_d30` |
| scx_locknest | default | 1 | 30s | 3914.10 | 139.85 | 2335.28 | `mariadb106_node0all_256_short_20260731_193039_scx_locknest_t256_d30` |
| scx_lockcoop | `-A -m10 -t2` | 1 | 30s | 4427.06 | 134.90 | 528.11 | `mariadb106_node0all_256_short_20260731_193039_scx_lockcoop_t256_d30` |

同一 node0-all 条件下，`scx_lockcoop` 参数扫描：

| 参数 | runs | duration | TPS | p95 ms | 说明 |
|---|---:|---:|---:|---:|---|
| `-A -m10` | 2 | 30s | 3978.56 | 158.63 | 基础高性能口径 |
| `-A -m10 -t2` | 1 | 30s | 4427.06 | 134.90 | 降低热点阈值后更好 |
| `-A -m10 -t2 -M` | 1 | 30s | 4644.09 | 123.28 | 最好，只采 mariadbd 证据 |
| `-A -m10 -t2 -K` | 1 | 30s | 4565.73 | 132.49 | 忽略 kernel mutex evidence，仍然很高 |
| `-A -m16 -t2` | 1 | 30s | 4178.58 | 150.29 | 可用但不如 `-m10` |
| `-A -m8 -t2` | 1 | 30s | 4244.26 | 144.97 | 可用但不如 `-m10` |

60s home datadir，node0-all formal matrix：

| scheduler | 参数口径 | runs | duration | TPS | p95 ms | max ms | 结果目录 |
|---|---|---:|---:|---:|---:|---:|---|
| default | default | 1 | 60s | 976.94 | 719.92 | 2002.98 | `mariadb106_home_node0all_formal_20260731_210407_default_t256_d60` |
| scx_simple | default args | 1 | 60s | 811.35 | 816.63 | 2250.45 | `mariadb106_home_node0all_formal_20260731_210407_scx_simple_t256_d60` |
| scx_locknest | default | 1 | 60s | 3588.11 | 155.80 | 4221.83 | `mariadb106_home_node0all_formal_20260731_210407_scx_locknest_t256_d60` |
| scx_lockcoop | `-A -m10 -t2 -M` | 1 | 60s | 4077.45 | 137.35 | 2170.04 | `mariadb106_home_node0all_formal_20260731_210407_scx_lockcoop_t256_d60` |

node0-all 阶段的核心观察：

- default/scx_simple 在 256 threads 下仍然只有约 0.8k-1.4k TPS；
- lock-aware 策略稳定进入 3.5k-4.6k TPS；
- `scx_lockcoop -A -m10 -t2 -M` 是当前最稳口径；
- 这一步才真正把端到端性能拉到论文同量级。

### 8.5 关于 `/root` datadir 到 `/home` datadir 的归因修正

后续追问中指出：“没看到 DB 数据目录从 `/root` 挪到 `/home` 有带来什么效果。”这个质疑是合理的。

基于现有日志，应该这样表述：

- NUMA 绑定带来的提升是强证据；
- `-M` 过滤 MariaDB evidence 带来的提升也是强证据；
- `/root -> /home` 不是一个干净的单变量实验，因为迁移后第一轮 home run 很冷，只有 `1226.69 TPS / p95 530.08 ms`；
- home datadir 第二轮热态达到 `4559.13 TPS / p95 127.81 ms`，但这个结果同时包含 datadir 路径、buffer pool/page-cache 热状态、I/O 长尾减弱等因素；
- 因此，不能把“搬到 `/home`”写成主要性能跃迁原因。更准确的说法是：它可能改善 60s 长跑的 I/O 长尾和热态稳定性，但主因仍是 NUMA 绑定和 evidence 过滤。

对照数据如下：

| 条件 | scheduler/参数 | run | TPS | p95 ms | max ms | 解释 |
|---|---|---:|---:|---:|---:|---|
| root datadir，node0-all，`-M` | scx_lockcoop `-A -m10 -t2 -M` | r1 | 4149.09 | 147.61 | 795.64 | 60s 正常高态 |
| root datadir，node0-all，`-M` | scx_lockcoop `-A -m10 -t2 -M` | r2 | 3497.93 | 155.80 | 4189.81 | 60s 中 20-30s 桶掉到 2264 TPS |
| home datadir，node0-all，`-M` | scx_lockcoop `-A -m10 -t2 -M` | r1 | 1226.69 | 530.08 | 1264.26 | 刚复制后冷态，不能作为稳态 |
| home datadir，node0-all，`-M` | scx_lockcoop `-A -m10 -t2 -M` | r2 | 4559.13 | 127.81 | 1058.61 | 热态高态 |
| home datadir，node0-all formal | scx_lockcoop `-A -m10 -t2 -M` | r1 | 4077.45 | 137.35 | 2170.04 | 最终正式 60s 矩阵 |

最终归因排序因此调整为：

1. MariaDB 10.6 对齐论文口径：必要前提；
2. DB + sysbench 都绑 node0：最大、最清晰的性能阶梯；
3. `-A -m10 -t2`：使 lockcoop 更积极、但不过早相信噪声锁；
4. `-M`：过滤非 MariaDB 锁证据，进一步提升；
5. `/home` datadir：可能改善 60s 热态稳定性，但不是主要因果链。

---

## 9. 最终复现实验命令

### 9.1 一次性确认当前 MariaDB 状态

```bash
cd <workspace>

cat /sys/kernel/sched_ext/state
./.mariadb106_control.sh status
pid=$(./.mariadb106_control.sh pid)
taskset -pc "$pid"
tr '\0' ' ' </proc/$pid/cmdline
```

确认输出中应包含：

```text
--datadir=<data-workspace>/.mariadb106_data
pid ... current affinity list: 0-127
```

### 9.2 正式矩阵

```bash
cd <workspace>

TAG=mariadb106_home_node0all_formal_$(date +%Y%m%d_%H%M%S) \
RUNS=3 RUN_TIME=60 WARMUP_TIME=10 \
THREADS_LIST='64 128 192 256' \
SCHEDS='default scx_simple scx_locknest scx_lockcoop' \
SCX_ARGS_SCX_LOCKCOOP='-A -m 10 -t 2 -M' \
'./test3-SysbenchOLTP /scripts/run_mariadb106_home_node0all_matrix.sh'
```

### 9.3 只跑 256-thread 快速确认

```bash
cd <workspace>

TAG=mariadb106_home_node0all_256_check_$(date +%Y%m%d_%H%M%S) \
RUNS=1 RUN_TIME=60 WARMUP_TIME=10 \
THREADS_LIST='256' \
SCHEDS='default scx_simple scx_locknest scx_lockcoop' \
SCX_ARGS_SCX_LOCKCOOP='-A -m 10 -t 2 -M' \
'./test3-SysbenchOLTP /scripts/run_mariadb106_home_node0all_matrix.sh'
```

### 9.4 新 datadir 第一次复制后的注意事项

如果重新复制 datadir 到 `/home`，第一次 run 可能会非常低，因为 buffer pool dump 或页缓存状态是冷的。本次实验中第一次 home datadir run 只有：

```text
1226.69 TPS / p95 530.08 ms
```

第二次热态立即到：

```text
4559.13 TPS / p95 127.81 ms
```

所以 freshly copied datadir 后建议先跑一次 256-thread warm-up/seed run，再跑正式矩阵。

---

## 10. 证据索引

### 10.1 最终脚本

```text
test3-SysbenchOLTP /scripts/run_mariadb106_home_node0all_matrix.sh
```

### 10.2 关键结果目录

root datadir，node0-all，30s 对照：

```text
test3-SysbenchOLTP /result/mariadb106_node0all_256_short_20260731_193039_*
```

参数扫描：

```text
test3-SysbenchOLTP /result/mariadb106_node0all_256_param_*
```

root datadir，`-M`，60s 崩溃样例：

```text
test3-SysbenchOLTP /result/mariadb106_node0all_256_mariadbd_only_60s_20260731_194038_scx_lockcoop_t256_d60/
```

I/O 监控：

```text
test3-SysbenchOLTP /result/monitor_mariadb106_m_only_20260731_194518/
```

home datadir，首次冷/热对比：

```text
test3-SysbenchOLTP /result/mariadb106_home_node0_256_mariadbd_only_60s_20260731_194909_scx_lockcoop_t256_d60/
```

home datadir，热态三连：

```text
test3-SysbenchOLTP /result/mariadb106_home_node0_256_mariadbd_only_60s_hot3_20260731_195256_scx_lockcoop_t256_d60/
```

home datadir，256-thread baseline 对照：

```text
test3-SysbenchOLTP /result/mariadb106_home_node0_256_baselines_60s_20260731_195751_*
```

home datadir，最终 4×4 矩阵：

```text
test3-SysbenchOLTP /result/mariadb106_home_node0all_formal_20260731_210407_*
```

---

## 11. 可以写进论文/答辩的归因表述

本次复现失败的直接表现是 MariaDB/Sysbench OLTP 中 `scx_lockcoop` 吞吐偶发跌落，而 futex/mutex 微基准仍表现正常。排查表明，这不是 lockcoop 调度机制失效，而是端到端 OLTP 将锁路径、NUMA 放置、buffer-pool 热状态和存储 I/O 混合在一起后产生的系统级遮蔽效应。

在当前双路 256 CPU ARM 机器上，未控制 socket 放置时，MariaDB 线程和锁交接锚点会跨 NUMA 节点扩散，使锁交接局部性收益不稳定。进一步地，128MB buffer pool 加 O_DIRECT 的配置会使 Sysbench OLTP 持续产生大量读写；当 datadir 位于 root LV 时，该 I/O 路径与系统活动共享同一 NVMe，导致 60s run 中出现短窗口吞吐坍塌和秒级最大延迟。通过将数据库版本对齐到论文的 MariaDB 10.6，将 DB 与客户端限制在 node0，并将 datadir 移至 `/home` 所在的更空闲存储路径，同时让 `scx_lockcoop` 只采集 MariaDB 线程锁证据，端到端结果恢复到论文同量级。

最终 256-thread 下，`scx_lockcoop` 达到 4077.45 TPS / p95 137.35 ms，相对 default 提升 4.17 倍，相对 `scx_simple` 提升 5.03 倍；单次热态 60s 可达到 4559.13 TPS / p95 127.81 ms，接近论文报告的 4587 TPS。这说明微基准观察到的 lockcoop 机制确实能够迁移到真实 MariaDB/OLTP，但前提是数据库版本、NUMA 拓扑、锁证据范围和存储热状态不能成为混杂变量。

---

## 12. 2026-08-01 补测四个调度器时的事故记录

### 12.1 发生了什么

用户要求补测论文里剩下的四个调度器：

- `scx_lcas`
- `scx_cfl`
- `scx_nest`
- `scx_lockboost`

本轮仍沿用同一套 MariaDB 10.6 / Sysbench OLTP 口径：

- DB 与 sysbench 都绑 node0；
- `THREADS_LIST='64 128 192 256'`；
- `RUNS=1`；
- `RUN_TIME=60`；
- `WARMUP_TIME=10`；
- `scx_lockcoop` 仍保留 `-A -m 10 -t 2 -M` 作为对照默认参数。

实际运行从 `64 threads` 开始，`scx_lcas` 和 `scx_cfl` 都先跑完了 64-thread 这一个点：

| scheduler | TPS | p95 ms |
|---|---:|---:|
| scx_lcas | 5111.76 | 31.37 |
| scx_cfl | 5181.95 | 29.19 |

随后轮到 `scx_nest@64`。这一步没有正常结束，反而把 sched_ext 的退出路径卡住了。

### 12.2 具体卡在哪里

现场证据显示：

- `cat /sys/kernel/sched_ext/state` 变成了 `disabling`；
- `scx_nest` 进程进入不可杀的 `D` 状态；
- `sysbench` 的 64-thread 正式阶段在 30s 左右后开始掉到 0 TPS；
- 内核日志出现 `Sched_ext: nest (disabling+all)`；
- 还伴随 `scx_disable_workfn`、`bpf_scx_unreg`、`kthread_flush_work`、`bpf_prog_free_deferred` 相关的 soft lockup。

这说明问题不是“调度器性能太差”，而是 `scx_nest` 在卸载/反注册链路里把整个 sched_ext 关闭过程卡死了。

### 12.3 为什么 BMC 重启后看起来“恢复”了，但实验环境变了

BMC 重启把服务器救回来了，但它没有回到 sched_ext 实验内核，而是启动到了：

```text
Linux localhost.localdomain 6.6.0-28.0.0.34.oe2403.aarch64
```

当前机器上：

- `/sys/kernel/sched_ext/state` 不存在；
- 现在跑的是系统自带的 `/var/lib/mysql` `mariadbd`；
- 不是实验用的 `6.17.0-lockcoop-scx617`；
- 也不是 `6.14.11-lockcoop-scx`。

所以这次 BMC 重启只说明机器“活了”，不代表可以马上继续 sched_ext 补测。

### 12.4 这次事故对后续补测的影响

结论很直接：

1. `scx_nest` 这次 live rerun 仍然不稳定，而且是内核态卡死级别的不稳定；
2. 在当前 6.6 启动状态下，sched_ext 相关补测不能继续；
3. 要想把剩下三个调度器补完，先得回到带 sched_ext 的实验内核，再重新启动 MariaDB 10.6 口径；
4. 最稳妥的策略是：
   - 继续保留 `scx_nest` 为已知失败样本；
   - 先补 `scx_lockboost`、必要时补 `scx_lcas/scx_cfl` 的 128/192/256 点；
   - `scx_nest` 如果要留 live 结果，只能等更稳的内核/修复版，不建议在当前状态反复硬跑。

### 12.5 当前可复用的证据

已成功落盘的 64-thread 原始目录：

- `test3-SysbenchOLTP /result/paper53_node0all_rest4_20260731_233520_scx_lcas_t64_d60/`
- `test3-SysbenchOLTP /result/paper53_node0all_rest4_20260731_233520_scx_cfl_t64_d60/`

`scx_nest` 的原始目录也在，但缺少正常结束后的 `mariadb_status.after`，并且对应内核日志已经证明是 `disabling+all` 方向的卡死：

- `test3-SysbenchOLTP /result/paper53_node0all_rest4_20260731_233520_scx_nest_t64_d60/`

### 12.6 去除 `scx_nest` 后的 7-scheduler 完整矩阵

BMC 重启后，机器重新进入 sched_ext 实验内核：

```text
Linux 6.17.0-lockcoop-scx617
sched_ext state: disabled
```

随后按用户要求去掉 `scx_nest`，补测论文图中剩余 7 个调度器：

```text
default, scx_simple, scx_lcas, scx_cfl, scx_lockboost, scx_locknest, scx_lockcoop
```

实验条件：

```text
tag: paper53_node0all_no_nest_20260801_032247
MariaDB: 10.6.27
datadir: <data-workspace>/.mariadb106_data
binding: DB + sysbench 都绑 node0
threads: 64, 128, 192, 256
runs: 1
run time: 60s
warmup: 10s
scx_lockcoop 参数: -A -m 10 -t 2 -M
```

#### 12.6.1 TPS

| scheduler | 64 | 128 | 192 | 256 |
|---|---:|---:|---:|---:|
| default | 5236.80 | 2017.86 | 1206.42 | 1035.20 |
| scx_simple | 5246.85 | 2361.13 | 1317.29 | 950.64 |
| scx_lcas | 4734.10 | 2199.36 | 1165.77 | 916.99 |
| scx_cfl | 5373.40 | 2130.27 | 1424.74 | 977.91 |
| scx_lockboost | 5086.69 | 2713.92 | 2008.52 | 1311.10 |
| scx_locknest | 5423.04 | 4999.31 | 4289.82 | 4228.90 |
| scx_lockcoop | 5943.37 | 4746.06 | 4828.19 | 4582.08 |

#### 12.6.2 p95 latency, ms

| scheduler | 64 | 128 | 192 | 256 |
|---|---:|---:|---:|---:|
| default | 30.26 | 167.44 | 419.45 | 707.07 |
| scx_simple | 29.19 | 137.35 | 363.18 | 719.92 |
| scx_lcas | 35.59 | 153.02 | 434.83 | 719.92 |
| scx_cfl | 27.66 | 155.80 | 344.08 | 694.45 |
| scx_lockboost | 29.72 | 116.80 | 235.74 | 484.44 |
| scx_locknest | 24.38 | 56.84 | 97.55 | 144.97 |
| scx_lockcoop | 20.37 | 58.92 | 87.56 | 127.81 |

#### 12.6.3 max latency, ms

| scheduler | 64 | 128 | 192 | 256 |
|---|---:|---:|---:|---:|
| default | 90.06 | 420.73 | 1085.30 | 1750.86 |
| scx_simple | 80.90 | 400.70 | 899.34 | 1881.84 |
| scx_lcas | 98.76 | 441.78 | 1074.98 | 2108.36 |
| scx_cfl | 80.90 | 453.56 | 1090.84 | 1824.80 |
| scx_lockboost | 104.87 | 424.34 | 772.20 | 1244.65 |
| scx_locknest | 85.79 | 183.28 | 2181.97 | 666.75 |
| scx_lockcoop | 85.59 | 229.41 | 283.23 | 963.93 |

#### 12.6.4 观察

- 256 threads 下，`scx_lockcoop` 达到 `4582.08 TPS / p95 127.81 ms`，已经非常接近论文目标中的 `4.5k TPS / p95 110–160 ms`。
- `scx_locknest` 在 128/192/256 threads 下也维持高态，256 threads 为 `4228.90 TPS / p95 144.97 ms`。
- `default/scx_simple/scx_lcas/scx_cfl` 在高并发下仍明显崩塌，256 threads 基本只有 `0.9k–1.0k TPS`，p95 接近或超过 `700 ms`。
- `scx_lockboost` 有改善，但 256 threads 只有 `1311.10 TPS / p95 484.44 ms`，没有达到 locknest/lockcoop 的量级。
- 去掉 `scx_nest` 后，整轮 7-scheduler 矩阵能正常完成，结束后 sched_ext 回到 `disabled`。

## 13. 本地脚本说明与归档记录

本节记录 2026-08-01 对本机 ARM/openEuler 实验脚本的整理。目标是把正式复现实验入口和临时排查脚本分开，避免后续复跑 test3 时误用旧入口。

### 13.1 正式复现实验脚本与依赖

这些文件后续还会直接用于 MariaDB 10.6 / Sysbench OLTP 复现实验：

```text
arm_openeuler/root_scripts/.mariadb106_control.sh
arm_openeuler/root_scripts/.mariadb106_wait_buffer_pool.sh
.node0bin/sysbench
test3-SysbenchOLTP /scripts/run_full_sweep.sh
test3-SysbenchOLTP /scripts/run_threads_sweep.sh
test3-SysbenchOLTP /scripts/run_mariadb106_home_node0all_matrix.sh
```

- `arm_openeuler/root_scripts/.mariadb106_control.sh`：管理隔离安装的 MariaDB 10.6 实例，支持 `start|stop|restart|status|pid`，正式 test3 结果依赖它重启数据库。
- `arm_openeuler/root_scripts/.mariadb106_wait_buffer_pool.sh`：数据库重启后等待/检查 InnoDB buffer pool load 状态，并打印关键 buffer pool 计数。
- `.node0bin/sysbench`：sysbench wrapper，让 test3 正式脚本调用 sysbench 时自动使用当前实验指定的 CPU/memory 放置策略。
- `run_full_sweep.sh`：test3 单线程数通用 runner，负责切换 `scx_*`、重启数据库、warmup、正式 sysbench 计时和采集结果。
- `run_threads_sweep.sh`：对多个并发线程数循环调用 `run_full_sweep.sh`。
- `run_mariadb106_home_node0all_matrix.sh`：这次 RUNS=5 正式矩阵使用的高层入口，固定 MariaDB 10.6、数据目录、DB restart hook、buffer pool wait hook 和 `scx_lockcoop` 调优参数。2026-08-01 已同步改为从 `arm_openeuler/root_scripts/` 调用 MariaDB 10.6 控制脚本。

### 13.2 已归档目录

按用户要求，临时/旧版/排查脚本已从原位置移动到：

```text
arm_openeuler/
```

并新增说明文件：

```text
arm_openeuler/README.md
```

当前归档结构：

```text
arm_openeuler/
├── README.md
├── root_scripts/
│   ├── .mariadb1011_control.sh
│   ├── .mariadb1011_restart_iotuned.sh
│   ├── .mariadb106_control.sh
│   ├── .mariadb106_restart_node0_db0_95.sh
│   ├── .mariadb106_restart_node0_iomid.sh
│   ├── .mariadb106_wait_buffer_pool.sh
│   ├── .mariadb_ubuntu1011_control.sh
│   ├── .mariadb_ubuntu1011_restart_bp45g.sh
│   ├── .mariadb_ubuntu1011_restart_iomid.sh
│   ├── .run_lockcoop_confirm_60s.sh
│   ├── .run_lockcoop_diag_30s.sh
│   ├── .set_mariadb_relaxed_flush.sh
│   ├── .set_sched_ext
│   └── .set_sched_ext.c
└── test3_scripts/
    ├── run_lockcoop_matrix_tmux.sh
    └── run_mariadb106_node0all_matrix.sh
```

### 13.3 已归档脚本用途

- `arm_openeuler/root_scripts/.mariadb1011_control.sh`：管理隔离 MariaDB 10.11 实例，用于数据库版本对照。
- `arm_openeuler/root_scripts/.mariadb1011_restart_iotuned.sh`：用更激进的 InnoDB I/O 参数重启 MariaDB 10.11，用于 I/O 参数排查。
- `arm_openeuler/root_scripts/.mariadb106_control.sh`：当前正式 MariaDB 10.6 控制脚本，已从仓库根目录移入归档目录，但仍被正式 test3 入口调用。
- `arm_openeuler/root_scripts/.mariadb106_wait_buffer_pool.sh`：当前正式 MariaDB 10.6 buffer pool 状态检查脚本，已从仓库根目录移入归档目录，但仍被正式 test3 入口调用。
- `arm_openeuler/root_scripts/.mariadb_ubuntu1011_control.sh`：管理 Ubuntu 包拆出的 MariaDB 10.11 实例，用于排除 openEuler 包差异。
- `arm_openeuler/root_scripts/.mariadb_ubuntu1011_restart_bp45g.sh`：以约 45 GiB buffer pool 重启 Ubuntu MariaDB 10.11，用于排查 buffer pool 大小影响。
- `arm_openeuler/root_scripts/.mariadb_ubuntu1011_restart_iomid.sh`：以中等 I/O 参数重启 Ubuntu MariaDB 10.11，用于 I/O 参数对照。
- `arm_openeuler/root_scripts/.mariadb106_restart_node0_db0_95.sh`：早期 MariaDB 10.6 放置方式排查脚本。
- `arm_openeuler/root_scripts/.mariadb106_restart_node0_iomid.sh`：MariaDB 10.6 + I/O 参数 + 放置方式组合排查脚本。
- `arm_openeuler/root_scripts/.set_mariadb_relaxed_flush.sh`：临时设置 `innodb_flush_log_at_trx_commit=2`，用于确认刷盘策略是否影响性能。
- `arm_openeuler/root_scripts/.run_lockcoop_diag_30s.sh`：256 线程、30 秒 lockcoop 功能开关短跑诊断脚本。
- `arm_openeuler/root_scripts/.run_lockcoop_confirm_60s.sh`：256 线程、60 秒关键组合确认脚本。
- `arm_openeuler/root_scripts/.set_sched_ext.c`：把指定 PID 的所有线程切到 `SCHED_EXT` 的辅助工具源码。
- `arm_openeuler/root_scripts/.set_sched_ext`：上述源码编译出的 aarch64 ELF 二进制，不建议提交到正式仓库。
- `arm_openeuler/test3_scripts/run_lockcoop_matrix_tmux.sh`：早期通用 test3 矩阵 wrapper，当前正式矩阵已不用它。
- `arm_openeuler/test3_scripts/run_mariadb106_node0all_matrix.sh`：早期 MariaDB 10.6 矩阵入口，当前已被 `run_mariadb106_home_node0all_matrix.sh` 替代。

### 13.4 恢复方式

如果后续需要恢复某个脚本，直接移回原路径即可。例如：

```bash
mv arm_openeuler/root_scripts/.run_lockcoop_diag_30s.sh ./
mv arm_openeuler/test3_scripts/run_mariadb106_node0all_matrix.sh "test3-SysbenchOLTP /scripts/"
```

## 14. 非绑定矩阵 5k/1.5k 双峰根因专项排查

日期：2026-08-02

目标：解释 `paper53_unbound_no_nest_runs5_20260801_224447` 中，为什么同一个
`scx_lockcoop` 配置有些 run 能达到约 5k TPS，而多数 run 停在约 1.5k TPS。

### 14.1 最终结论

这不是普通随机波动，也不是少数 run 性能崩溃。它是一个由 MariaDB 初始 NUMA
放置位置触发、再被 `scx_lockcoop -A` 固化的双稳态：

```text
MariaDB 锁热点落在 node0（CPU 0-127）
  -> futex anchor 主要或全部位于 node0
  -> node0 对 /home 数据盘的 O_DIRECT 路径快
  -> 约 5k TPS 高态

MariaDB 锁热点落在 node1（CPU 128-255）
  -> futex anchor 主要或全部位于 node1
  -> node1 对同一数据盘的 O_DIRECT 路径极慢
  -> 约 1.5k TPS 低态
```

在 20 个非绑定 `scx_lockcoop` run 中：

- TPS 与测量阶段“本轮 MariaDB futex anchor 位于 node0 的比例”的 Pearson 相关系数为 `0.881`；
- 4 个 `TPS > 4000` 的高态 run，当前 MariaDB futex anchor 均为 `100% node0`；
- 12 个 `TPS < 2000` 的低态 run，node0 anchor 平均仅 `2.8%`；
- 受控 2x2 实验中，DB 在 node0 的 4 个 run 平均 `5647.26 TPS`，DB 在 node1 的 4 个 run 平均 `1575.93 TPS`，相差 `3.58x`；
- 改变 sysbench 所在节点不会改变档位，改变 DB 所在节点会直接改变档位。

因此，在自然未绑定 run 中，5k/1.5k 的可观测档位开关是实验 MariaDB 的 CPU
执行位置、首次触碰内存位置和热锁 anchor 共同落在哪个 NUMA node。anchor node 与
TPS 高度相关，但后续 CPU x MEM 反事实证明：anchor 是整体放置状态的代理和强化器，
不能把 anchor CPU 本身解释为单独、充分的硬件根因。

### 14.2 现有 RUNS=5 日志的逐 run 证据

解析方法：

1. 按 `lock=futex:<tgid>:` 统计每个 `scx.rN.log` 中最活跃的 TGID，识别本轮隔离 MariaDB；
2. 只统计测量阶段最后 60 帧 `Active Lock Nests` 中该 TGID 的 futex anchor；
3. CPU `0-127` 计为 node0，CPU `128-255` 计为 node1；
4. 不把系统 MariaDB PID 12583 的记录混入比例。

| threads | run | warmup TPS | measure TPS | 实验 DB TGID | measure anchor 在 node0 | 判定 |
|---:|---:|---:|---:|---:|---:|---|
| 64 | 1 | 4458.71 | 5413.87 | 323356 | 100.0% | 高态 |
| 64 | 2 | 2648.47 | 2436.23 | 323982 | 97.7% | 中间态，存在次级局部性变量 |
| 64 | 3 | 1482.30 | 1509.16 | 324557 | 0.0% | 低态 |
| 64 | 4 | 5745.77 | 5695.67 | 325121 | 100.0% | 高态 |
| 64 | 5 | 1504.56 | 1517.64 | 325673 | 0.0% | 低态 |
| 128 | 1 | 4930.42 | 4869.12 | 354586 | 100.0% | 高态 |
| 128 | 2 | 1522.59 | 1545.96 | 355346 | 0.0% | 低态 |
| 128 | 3 | 1534.10 | 1554.67 | 356106 | 0.0% | 低态 |
| 128 | 4 | 5710.95 | 5758.90 | 356866 | 100.0% | 高态 |
| 128 | 5 | 1510.55 | 1530.13 | 357618 | 0.0% | 低态 |
| 192 | 1 | 1237.67 | 1559.81 | 386451 | 0.0% | 低态 |
| 192 | 2 | 5012.57 | 2828.83 | 387438 | 64.7% | 高态向低态迁移 |
| 192 | 3 | 5386.42 | 2061.98 | 388417 | 58.2% | 混合/迁移态 |
| 192 | 4 | 1537.77 | 1550.31 | 389462 | 0.0% | 低态 |
| 192 | 5 | 1508.37 | 1507.13 | 390465 | 13.6% | 低态 |
| 256 | 1 | 4381.23 | 3255.19 | 424601 | 78.7% | 高态向低态迁移 |
| 256 | 2 | 1568.20 | 1574.93 | 426255 | 0.0% | 低态 |
| 256 | 3 | 1568.59 | 1580.43 | 427868 | 0.0% | 低态 |
| 256 | 4 | 1512.19 | 1549.77 | 429473 | 0.9% | 低态 |
| 256 | 5 | 1402.34 | 1488.67 | 430945 | 19.3% | 低态 |

64/128 threads 下，warmup TPS 与 measure TPS 的相关系数分别约为 `0.990` 和
`0.998`。也就是说，档位通常在 10 秒 warmup 时已经形成，不是 60 秒统计阶段末尾
才随机分叉。

### 14.3 高并发下的状态迁移

192/256 threads 下，部分 run 从 node0 高态逐渐扩散到 node1，TPS 与 anchor 比例
同步下降。

`192 threads / run 2`：

| 10 秒桶 | 1 | 2 | 3 | 4 | 5 | 6 |
|---:|---:|---:|---:|---:|---:|---:|
| TPS | 4451.61 | 4841.72 | 2837.50 | 1704.18 | 1591.92 | 1554.28 |
| node0 anchor | 97.9% | 94.1% | 78.9% | 59.1% | 34.8% | 24.7% |

`256 threads / run 1`：

| 10 秒桶 | 1 | 2 | 3 | 4 | 5 | 6 |
|---:|---:|---:|---:|---:|---:|---:|
| TPS | 4551.23 | 4726.21 | 4724.95 | 1719.18 | 1482.48 | 2354.70 |
| node0 anchor | 99.8% | 100.0% | 99.7% | 61.1% | 44.3% | 66.4% |

这解释了为什么部分高并发 run 的 60 秒平均值落在 2k-3k：它们不是一个稳定的
第三档，而是前半段处于 node0 高态、后半段部分转入 node1 低态。

### 14.4 测试脚本如何随机选择初始 node

非绑定入口明确做了两件事：

- 把 `PATH` 恢复到系统 `/usr/bin/sysbench`，不使用 `.node0bin/sysbench`；
- DB restart 命令不带 `numactl`。

通用 runner 每个 run 的顺序为：

```text
stop old scheduler
attach new scheduler
restart MariaDB without affinity
wait buffer-pool load
10s warmup
60s measure
```

因此 MariaDB 的进程创建、线程创建和内存首次触碰都发生在新 attach 的 sched_ext
调度器之下。启动 shell/新进程最初在哪个 CPU 是非确定的，而默认 CPU 选择又偏好
`prev_cpu` 及其 LLC/NUMA node，所以每次 restart 很容易落入 node0 或 node1 中的
某一个局部 basin。

这也解释了为什么同类双峰不只出现在 `scx_lockcoop`：`scx_simple`、`scx_cfl`、
`scx_lockboost` 和 `scx_locknest` 的个别 run 也出现过高态。初始 NUMA 随机性是共享
条件；`scx_lockcoop` 会进一步把它固化。

### 14.5 `scx_lockcoop` 如何把初始选择固化

当前源码中的关键路径如下。

1. `init_lock_nest()` 直接把第一次观察到的 holder/waker 当前 CPU 保存为 `anchor_cpu`：

```c
n_info->anchor_cpu = holder_cpu;
```

2. `update_lock_nest()` 只有两种情况会更换 anchor：

- 超过 `nest_expiry_ns=2s` 没有更新；
- 从 futex weak source 升级到 mutex strong source。

热 futex 每次活动都会刷新 `last_update_ts`，所以 2 秒 expiry 不会发生。mutex 与
futex 又使用不同的 `lock_key.type`，同一个 futex entry 实际不会被 mutex source
升级。因此，繁忙 futex 的首次 anchor 会长期保持。

3. `-m 10` 检查的是 `handoff_seq >= 10`。这个序号是 lock-state 更新次数，不是经过
真实性验证的十次锁交接；高争用下会在很短时间内越过阈值。

4. 默认 `require_same_node=true`，允许的 prev-CPU escape 不能跨 NUMA node。

5. `-A` 设置 `force_busy_anchor`。即使 anchor 已经忙，仍执行：

```c
scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | cpu, SCX_SLICE_DFL, 0);
```

正式日志中 `busy_dfl=0`，每个 run 有大量 `forced` 事件，说明该路径实际生效。这构成
正反馈：首次 anchor 在哪个 node，后续相关 waiter 就不断被拉回哪个 node。

### 14.6 为什么 node0 是高态：独立 O_DIRECT 实验

硬件拓扑：

```text
NUMA nodes: 2 (node0, node1)
node0 CPUs: 0-127, memory: 515450 MiB
node1 CPUs: 128-255, memory: 513994 MiB
NUMA distance: local=10, remote=20
nvme0 numa_node=0
nvme1 numa_node=0
/home LV 使用 nvme0 与 nvme1
```

这里的 `nvme0 numa_node=0` 和 `nvme1 numa_node=0` 不能解释成“系统没有
node1”。当前系统明确有两个完整的 NUMA 计算/内存节点：node0 对应 CPU 0-127，
node1 对应 CPU 128-255，两侧各有约 500 GiB 本地内存。`numa_node=0` 描述的是
PCIe 设备的**拓扑归属或最近节点**：两块 NVMe 的 PCIe 控制器都位于 node0 一侧，
由 node0 CPU 访问属于本地 I/O 路径；由 node1 CPU 发起访问时，仍然可以使用这两块
盘，但请求需要经过双路处理器之间的互连到达 node0 一侧的 PCIe root complex。

可以把当前拓扑简化为：

```text
node0 / socket0                         node1 / socket1
CPU 0-127 + local memory                CPU 128-255 + local memory
       |                                        |
       +-- PCIe -- nvme0                        |
       +-- PCIe -- nvme1                        |
       |                                        |
       +========= inter-socket link ============+
```

因此，node1 并非不存在，也不是不能访问 NVMe；它只是没有这两块 NVMe 的本地
PCIe 端点。一般的远端 I/O 会有额外开销，但本机实测的 `11.6x` O_DIRECT 差距远大于
普通 NUMA 延迟差，说明当前 NVMe 路径还存在显著的平台级非对称。现有实验已经把
异常范围缩小到这条路径，但尚不能仅凭 `numa_node=0` 判断具体是跨 socket PCIe、
中断/队列亲和性、IOMMU/SMMU 还是固件中的哪一层造成全部差距。

数据库口径：

```text
MariaDB 10.6.27
innodb_buffer_pool_size=134217728 (128 MiB)
innodb_flush_method=O_DIRECT
sbtest 数据约 4.5 GiB
```

在相同 SQL 工作量下，受控 node0/node1 run 的每事务物理读页数都约为 `8.6`，但：

| DB node | Innodb physical reads/s | 约合 TPS |
|---:|---:|---:|
| node0 | 43.7k-48.3k | 5.5k-5.7k |
| node1 | 13.36k-13.42k | 1.56k-1.57k |

为避免把这个差异误归因于 LockCoop，额外停止隔离 MariaDB，在 `/home` 临时创建
2 GiB 文件，使用系统调度器运行 `sysbench fileio rndrd`：128 threads、16 KiB block、
sync I/O、O_DIRECT、20 秒，按 `node0,node1,node0,node1` 交替测试。

| CPU/memory node | run 1 read | run 2 read | IOPS | p95 |
|---:|---:|---:|---:|---:|
| node0 | 2651.44 MiB/s | 2651.22 MiB/s | 约 169.7k | 2.03-2.07 ms |
| node1 | 229.13 MiB/s | 229.10 MiB/s | 约 14.66k | 16.41-16.71 ms |

node0 的独立随机读吞吐是 node1 的约 `11.6x`。临时文件在测试后已通过 sysbench
cleanup 删除，临时目录也已移除。

同时做了 CPU/内存排除实验：

| microbenchmark | node0 | node1 | 结论 |
|---|---:|---:|---|
| sysbench CPU, 128 threads | 164664 events/s | 162204 events/s | node1 仅慢约 1.5% |
| sysbench memory write | 490293 MiB/s | 572671 MiB/s | node1 并不慢 |

所以第二颗 socket 不是整体算力下降；异常集中在 `/home` 所在 NVMe/O_DIRECT 数据路径。

### 14.7 受控 `DB node x client node` 2x2 实验

协议：

```text
scheduler: scx_lockcoop -A -m 10 -t 2 -M
threads: 128
warmup: 10s
measure: 30s
runs: 2 per cell
DB: --cpunodebind=N --membind=N
sysbench: --cpunodebind=N --membind=N
```

| DB node | sysbench node | run 1 TPS | run 2 TPS | mean TPS | mean p95 ms | DB 内存分布 |
|---:|---:|---:|---:|---:|---:|---|
| 0 | 0 | 5679.26 | 5544.23 | 5611.74 | 49.25 | 约 99% node0 |
| 0 | 1 | 5510.10 | 5855.45 | 5682.77 | 44.58 | 约 99% node0 |
| 1 | 0 | 1583.53 | 1585.82 | 1584.67 | 134.90 | 约 97% node1 |
| 1 | 1 | 1566.32 | 1568.04 | 1567.18 | 136.12 | 约 97% node1 |

关键观察：

- DB 固定 node0 后，客户端放 node0 或 node1 都是 5.6k 左右；
- DB 固定 node1 后，客户端放 node0 或 node1 都是 1.57k 左右；
- 因变量几乎完全跟 DB node 走，而不是跟 sysbench node 走。

有效原始结果目录：

```text
test3-SysbenchOLTP /result/diag_lc_node0local_t128_r2_20260802_scx_lockcoop_t128_d30/
test3-SysbenchOLTP /result/diag_lc_node1local_t128_r2_20260802_scx_lockcoop_t128_d30/
test3-SysbenchOLTP /result/diag_lc_db0_client1_clean_t128_r2_20260802_scx_lockcoop_t128_d30/
test3-SysbenchOLTP /result/diag_lc_db1_client0_t128_r2_20260802_scx_lockcoop_t128_d30/
```

曾有一轮交叉实验与另一诊断进程同时 restart 同一个 MariaDB，sysbench 报
`Lost connection to server during query`。该冲突样本明确作废，没有进入上表；之后在
sched_ext 回到 disabled、残留 runner 全部退出后，用 `*_clean_*` tag 串行重跑。

### 14.8 已排除的主要假设

| 假设 | 证据 | 判定 |
|---|---|---|
| scheduler attach 偶发失败 | 高低 run 的 scx 日志均成功 attach，并正常产生数百万 locality/forced 事件 | 排除 |
| SQL 工作量不同 | 各 run 均无 ignored errors；每事务 queries/physical reads 基本一致 | 排除 |
| buffer pool 命中差异是档位开关 | 高低态 pages_data 接近，每事务物理读约 8.5-9.3；node0 冷启动受控 run 仍达 5679 TPS | 排除为主开关 |
| MariaDB table-cache 自适应导致双峰 | 第 2/3 个 table-cache instance 在高、低 run 中都可能出现，也都可能不出现 | 排除为主开关 |
| node1 CPU 降频/算力异常 | 两侧均 3.0 GHz performance governor；CPU microbenchmark 只差 1.5% | 排除 |
| sysbench 所在 node 决定档位 | 2x2 中换 client node 几乎不改变 TPS | 排除 |
| 5k 只是单个异常大值 | 受控 node0 四次全部 5.5k-5.9k，node0-all RUNS=5 也无 1.5k 档 | 排除 |

### 14.9 次要实验卫生问题

这些问题不是本次双峰的主因，但正式确认实验应处理：

1. 系统 MariaDB 10.5 PID 12583 同时运行，约 116 MiB 内存几乎都在 node1；
2. `-M` 只按 `comm == "mariadbd"` 过滤，不能区分系统实例和实验实例；
3. 新 scheduler 在旧实验 DB 关闭前已经 attach，会短暂观察旧实例 shutdown 锁事件；
4. 当前机器的 `perf` 因 `undefined symbol: tep_set_loglevel` 无法采集 PMU 数据；本次使用 numastat、scx 日志、InnoDB status 和独立 fileio 完成归因；
5. 之后的正式实验应停止系统 MariaDB，并显式记录实验 DB PID/TGID，而不只按 comm 过滤。

### 14.10 对结果解释与后续实验口径的修正

非绑定 RUNS=5 的算术均值把两个硬件路径不同的状态混在了一起，不能把高态当异常值
删除，也不能把低态简单称为 LockCoop 崩溃。更准确的表述是：

- `scx_lockcoop` 确实能形成高性能锁交接状态；
- 当前实现把首次 anchor 固定得过强，导致它也会稳定维持一个不利的 node1 状态；
- 当前服务器的数据盘全部属于 node0；未绑定启动时 DB CPU、首次触碰内存和 anchor
  通常一起落入同一 node，这个联合放置被放大为约 5k/1.5k 两档；
- 对比调度器算法时必须控制 DB 的 NUMA/I/O 路径，或把 DB-node 状态作为实验因子分层报告；
- 若研究目标是“真正无需绑定也能稳定选对节点”，则需要让 anchor 选择考虑设备/内存 NUMA 成本，并允许热锁在观测到持续低吞吐或远端 I/O 时重新锚定，而不是依赖剔除异常值。

### 14.11 4 GiB O_DIRECT 复核与 CPU x MEM 四象限

#### 14.11.1 目的与边界

14.6 的 2 GiB 独立 fileio 已经证明 node0/node1 的 O_DIRECT 路径存在约 `11.6x`
差距。本节使用 4 GiB 临时文件重新做两轮本地放置复核，并将 CPU node 与用户缓冲页
所在 memory node 解耦，以判断差异主要跟 CPU 算力、CPU 到设备路径，还是 O_DIRECT
DMA 缓冲页的位置有关。

本轮不是新的正式论文矩阵，边界如下：

- `sched_ext` 状态为 `disabled`，使用系统默认调度器；
- 没有并发 sysbench/MariaDB 压测；隔离 MariaDB 10.6 进程在第一组 fileio 期间为空闲
  状态，系统 MariaDB 服务也处于空闲状态；
- fileio 使用独立临时文件，不读取或写入任何 MariaDB 数据文件；
- 该次命令输出没有写成持久 raw log，表中数值来自当次终端/tool session 的完整输出；
  因此它是补强机制判断的诊断证据，正式引用仍应优先保留 14.6 已记录的 2 GiB 结果；
- 两个临时目录及全部 4 GiB 文件均已在测试结束时删除，无法从文件系统恢复 raw 输出。

#### 14.11.2 命令与协议

本地 node0/node1 两轮复核使用：

```bash
tmpdir="$(mktemp -d <home-data>/lockcoop_fileio.XXXXXX)"
cd "$tmpdir"

/usr/bin/sysbench fileio \
  --file-num=16 \
  --file-total-size=4G \
  prepare

numactl --cpunodebind="$NODE" --membind="$NODE" \
  /usr/bin/sysbench fileio \
    --file-num=16 \
    --file-total-size=4G \
    --file-block-size=16K \
    --file-test-mode=rndrd \
    --file-io-mode=sync \
    --file-extra-flags=direct \
    --threads=128 \
    --time=15 \
    --events=0 \
    --report-interval=5 \
    run

/usr/bin/sysbench fileio --file-num=16 --file-total-size=4G cleanup
rmdir "$tmpdir"
```

运行顺序为 `node0, node1, node1, node0`，避免把固定顺序的设备热状态误认为 node
差异。实际第一组临时目录为 `<home-data>/lockcoop_fileio.8yJwnX`，测试后已删除。

CPU x MEM 四象限使用相同的 4 GiB、16 files、16 KiB、128 threads、sync、direct
配置，每格运行 10 秒，唯一变化是：

```bash
numactl --cpunodebind="$CPU_NODE" --membind="$MEM_NODE" \
  /usr/bin/sysbench fileio ... --time=10 run
```

四象限临时目录使用
`<home-data>/lockcoop_fileio_cross.XXXXXX` 模板；随机后缀没有写入持久日志，目录和文件
已由 trap 中的 `sysbench fileio cleanup` 与 `rmdir` 删除。

#### 14.11.3 本地放置两轮 raw 值

| 顺序 | CPU node | MEM node | total events | reads/s | read MiB/s | avg ms | p95 ms |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0 | 0 | 2,775,205 | 184,942.69 | 2,889.73 | 0.69 | 1.86 |
| 2 | 1 | 1 | 220,081 | 14,660.75 | 229.07 | 8.73 | 16.41 |
| 3 | 1 | 1 | 220,085 | 14,660.96 | 229.08 | 8.73 | 16.41 |
| 4 | 0 | 0 | 2,771,985 | 184,740.39 | 2,886.57 | 0.69 | 1.89 |

两轮均值：

| 放置 | mean reads/s | mean MiB/s | node0 / node1 |
|---|---:|---:|---:|
| CPU0 + MEM0 | 184,841.54 | 2,888.15 | 12.61x |
| CPU1 + MEM1 | 14,660.86 | 229.08 | 1.00x |

这次 4 GiB 复核的 `12.61x` 与 14.6 的 2 GiB `11.6x` 方向和量级一致，且交换运行
顺序后没有反转。

#### 14.11.4 CPU x MEM 四象限 raw 值

| CPU node | MEM node | total events | reads/s | read MiB/s | avg ms | p95 ms | 相对 CPU0/MEM0 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 1,836,173 | 183,530.30 | 2,867.66 | 0.70 | 1.93 | 1.000x |
| 0 | 1 | 147,196 | 14,694.89 | 229.61 | 8.70 | 16.71 | 0.080x |
| 1 | 0 | 1,413,402 | 141,180.87 | 2,205.95 | 0.57 | 0.97 | 0.769x |
| 1 | 1 | 146,754 | 14,658.13 | 229.03 | 8.72 | 16.71 | 0.080x |

解释：

1. 只把 memory node 从 0 改为 1，即使 CPU 仍在 node0，read IOPS 也从 `183.5k`
   降到 `14.7k`。因此对这个纯读 fileio，O_DIRECT 用户缓冲页或 DMA 目标内存的位置是
   主要开关。
2. CPU1 + MEM0 仍可达到 `141.2k IOPS`，说明 node1 CPU 并非只有 14.7k IOPS 的
   固有算力上限；但它比 CPU0 + MEM0 低约 `23.1%`，CPU/提交路径仍有次要影响。
3. `CPU1/MEM0` 的 p95 小于 `CPU0/MEM0`，但总 IOPS 更低。这并不矛盾：sysbench
   的事件 latency 只统计单次 fileio event，CPU1 访问远端 MEM0 的用户态开销不会全部
   体现为块设备 event latency。因此不能只用该 p95 推导端到端数据库延迟。
4. 这个实验将异常范围缩小到“CPU、内存、PCIe/NVMe、DMA/中断/队列之间的联合
   路径”。它不能仅凭 `/sys/class/nvme/*/device/numa_node=0` 再细分出跨 socket
   interconnect、IOMMU/SMMU、固件或 block-mq 中哪一层贡献了多少。

### 14.12 MariaDB CPU x MEM 反事实实验

#### 14.12.1 目的

自然未绑定 run 中，MariaDB 的 CPU 执行位置、首次触碰内存和 futex anchor 通常一起
落在同一 node。因此，正式日志里 `anchor-node0%` 与 TPS 的 `r=0.881` 只能证明它是
优秀的整体放置状态指标，不能单独证明“只要 anchor 在 node0 就一定有 5k TPS”。

本节固定 anchor/holder 的 CPU node，同时把 MariaDB memory node 强制放到相反节点，
构造两个反事实：

- `DB CPU0 + MEM1`：anchor/holder 全在 node0，但 DB 私有内存几乎全在 node1；
- `DB CPU1 + MEM0`：anchor/holder 全在 node1，但 DB 私有内存几乎全在 node0。

#### 14.12.2 命令与协议

共同协议：

```text
scheduler: scx_lockcoop -A -m 10 -t 2 -M
MariaDB: 10.6.27, 128 MiB buffer pool, O_DIRECT
sysbench: oltp_read_write, 128 threads, 16 tables x 1,000,000 rows
warmup: 10 s
measure: 30 s
runs: 2 per cell
ignored errors: 0 in all runs
reconnects: 0 in all runs
scheduler attach: successful in all runs
```

关键启动形式：

| tag | DB_CPU_NODE | DB_MEM_NODE | CLIENT_NODE |
|---|---:|---:|---:|
| `diag_lc_db_cpu0_mem1_client0_t128_r2_20260802` | 0 | 1 | 0 |
| `diag_lc_db_cpu1_mem0_client1_t128_r2_20260802` | 1 | 0 | 1 |

```bash
DB_RESTART_CMD="env M106_DATA=<data-workspace>/.mariadb106_data \
  numactl --cpunodebind=$DB_CPU_NODE --membind=$DB_MEM_NODE \
  <workspace>/arm_openeuler/root_scripts/.mariadb106_control.sh restart"

sysbench() {
  numactl --cpunodebind="$CLIENT_NODE" --membind="$CLIENT_NODE" \
    /usr/bin/sysbench "$@"
}
export -f sysbench

env \
  CLIENT_NODE="$CLIENT_NODE" \
  TAG="$TAG" \
  RUNS=2 RUN_TIME=30 WARMUP_TIME=10 THREADS=128 \
  SCHEDS=scx_lockcoop \
  DB_SOCKET=<workspace>/.mariadb106_run/mariadb106.sock \
  DB_USE_SUDO_ADMIN=0 DB_ADMIN_USER=root DB_PASS=password \
  MYSQL_BIN=<workspace>/.mariadb106_root/usr/bin/mariadb \
  MYSQL_LD_LIBRARY_PATH=<workspace>/.mariadb106_root/usr/lib/aarch64-linux-gnu:<workspace>/.mariadb106_root/lib/aarch64-linux-gnu:/usr/lib64 \
  SCX_ARGS_SCX_LOCKCOOP='-A -m 10 -t 2 -M' \
  SCX_BIN_DIR=<workspace>/lock-sched/tools/sched_ext/build/bin \
  DB_RESTART_CMD="$DB_RESTART_CMD" \
  DB_AFTER_RESTART_CMD=<workspace>/arm_openeuler/root_scripts/.mariadb106_wait_buffer_pool.sh \
  DB_PID_FILE=<workspace>/.mariadb106_run/mariadb106.pid \
  TARGET_BUFFER_POOL_SIZE=134217728 \
  METRICS=1 \
  '<workspace>/test3-SysbenchOLTP /scripts/run_full_sweep.sh'
```

两组新 tag：

```text
diag_lc_db_cpu0_mem1_client0_t128_r2_20260802
diag_lc_db_cpu1_mem0_client1_t128_r2_20260802
```

#### 14.12.3 物理读计算口径

`run_full_sweep.sh` 的 `mariadb_status.before.rN.tsv` 在 warmup **之前**采集，
`mariadb_status.after.rN.tsv` 在 measured run 之后采集。因此差值覆盖完整的
`10 s warmup + 30 s measure`，不能错误地只除以 30 秒，也不能只除 measured
transactions。

本节统一采用：

```text
physical_reads_delta = Innodb_buffer_pool_reads_after
                     - Innodb_buffer_pool_reads_before

total_transactions = warmup_transactions + measure_transactions

physical_reads_per_second = physical_reads_delta / (10 + 30)

physical_reads_per_transaction = physical_reads_delta / total_transactions
```

`physical_reads_per_second` 使用名义 40 秒，适合相同协议之间的相对比较；若需要亚百分比
绝对精度，应改用两个 sysbench 输出中的实际 `total time` 之和。

#### 14.12.4 每次 raw 值

| DB CPU | DB MEM | client | run | warmup TPS | measure TPS | p95 ms | private MB node0/node1 | anchor0 | holder0 | reads delta | reads/s, 40s | reads/tx |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 0 | 1 | 3315.30 | 5679.26 | 47.47 | 679.95 / 4.65 | 100% | 100% | 1,749,940 | 43,748.50 | 8.5816 |
| 0 | 0 | 0 | 2 | 5658.07 | 5544.23 | 51.02 | 684.09 / 4.65 | 100% | 100% | 1,931,457 | 48,286.43 | 8.6458 |
| 0 | 1 | 0 | 1 | 1240.90 | 1334.92 | 179.94 | 16.61 / 645.93 | 100% | 100% | 455,436 | 11,385.90 | 8.6417 |
| 0 | 1 | 0 | 2 | 1347.11 | 1332.45 | 183.21 | 16.61 / 672.49 | 100% | 100% | 457,384 | 11,434.60 | 8.5331 |
| 1 | 0 | 1 | 1 | 1713.03 | 1656.52 | 167.44 | 690.87 / 4.65 | 0% | 0% | 593,867 | 14,846.67 | 8.8580 |
| 1 | 0 | 1 | 2 | 1776.55 | 1747.74 | 158.63 | 687.59 / 4.65 | 0% | 0% | 613,875 | 15,346.88 | 8.7198 |
| 1 | 1 | 1 | 1 | 1518.76 | 1566.32 | 137.35 | 16.61 / 672.49 | 0% | 0% | 534,420 | 13,360.50 | 8.5690 |
| 1 | 1 | 1 | 2 | 1550.49 | 1568.04 | 134.90 | 16.61 / 667.11 | 0% | 0% | 536,997 | 13,424.92 | 8.5602 |

`private MB node0/node1` 来自对应 `numastat.after.rN.txt` 的 `Private` 行。anchor/holder
比例先从同一文件头识别本轮实验 DB PID，再只统计 `scx.rN.log` 中
`lock=futex:<本轮 PID>:` 的记录；系统 MariaDB PID 12583 不计入。每轮均核对到
`scx_lockcoop attached` 和正常的 `EXIT: unregistered from user space`。

两轮均值：

| DB CPU | DB MEM | mean TPS | mean p95 ms | mean physical reads/s | 相对 CPU0/MEM0 TPS |
|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 5611.74 | 49.25 | 46,017.47 | 1.000x |
| 0 | 1 | 1333.69 | 181.58 | 11,410.25 | 0.238x |
| 1 | 0 | 1702.13 | 163.04 | 15,096.78 | 0.303x |
| 1 | 1 | 1567.18 | 136.13 | 13,392.71 | 0.279x |

#### 14.12.5 有效原始结果目录

```text
# CPU0 + MEM0 baseline
test3-SysbenchOLTP /result/diag_lc_node0local_t128_r2_20260802_scx_lockcoop_t128_d30/

# CPU0 + MEM1 counterfactual
test3-SysbenchOLTP /result/diag_lc_db_cpu0_mem1_client0_t128_r2_20260802_scx_lockcoop_t128_d30/

# CPU1 + MEM0 counterfactual
test3-SysbenchOLTP /result/diag_lc_db_cpu1_mem0_client1_t128_r2_20260802_scx_lockcoop_t128_d30/

# CPU1 + MEM1 baseline
test3-SysbenchOLTP /result/diag_lc_node1local_t128_r2_20260802_scx_lockcoop_t128_d30/
```

每个目录均保存：

```text
sysbench.warmup.rN.txt
sysbench.rN.txt
scx.rN.log
mariadb_vars.rN.tsv
mariadb_status.before.rN.tsv
mariadb_status.after.rN.tsv
numastat.before.rN.txt
numastat.after.rN.txt
numa_maps.before.rN.txt
numa_maps.after.rN.txt
```

#### 14.12.6 结论边界：联合局部性，而非单一 `numa_node=0`

1. `CPU0 + MEM1` 中 anchor/holder 仍是 `100% node0`，TPS 却只有 `1.33k`。
   因此“anchor 在 node0”不是高态的充分条件。
2. `CPU1 + MEM0` 的独立 fileio 可恢复到 `141k IOPS`，但 MariaDB 只有
   `1.70k TPS`。MariaDB 不只是搬运 O_DIRECT 数据，它还会频繁访问 buffer pool、锁结构、页
   元数据和日志状态；CPU1 访问 MEM0 的远端工作集成本不会在纯 fileio 中完整体现。
3. `CPU0 + MEM0` 是唯一同时满足 DB compute、锁交接、工作内存和 node0 PCIe/NVMe
   路径局部性的组合，稳定达到 `5.6k TPS`。
4. 自然未绑定 run 中 CPU 与 first-touch memory 通常同 node，因此 anchor-node 比例仍是
   判断高低态的可靠观测指标；`scx_lockcoop -A` 又会强化该状态。但更准确的因果链是
   “初始联合放置 -> anchor 正反馈固化 -> NUMA/I/O 路径决定可达到的吞吐上限”，而不是
   “anchor CPU 单独决定 TPS”。
5. `/sys/class/nvme/nvme{0,1}/device/numa_node=0` 只证明设备拓扑最近 node。它与
   12.6x fileio 差距共同证明 node0/node1 I/O 路径显著不对称，但仍不能单独定位到底是
   inter-socket、DMA、IOMMU/SMMU、block-mq、中断亲和性还是固件层造成全部差距。

本轮结束后，隔离 MariaDB 10.6 已停止，`/sys/kernel/sched_ext/state` 为 `disabled`；
没有修改 sched_ext 源码或 presentation 目录。

### 14.13 正式非绑定矩阵中 `scx_lcas` attach 失败与数据有效性修正

#### 14.13.1 修正结论

对正式 tag
`paper53_unbound_no_nest_runs5_20260801_224447` 进行逐 scheduler、逐 run 的
`scx.rN.log` 审计后，确认该 tag 中 `scx_lcas` 的 20 次尝试全部无效：

- 4 个并发点：`64, 128, 192, 256`；
- 每个并发点 5 runs，共 20 份 `scx.rN.log`；
- 20/20 都在 BPF verifier 阶段因 `-E2BIG` 失败；
- 20/20 都没有 attach 到 sched_ext；
- 后续 warmup 和 measured sysbench 运行时，`sched_ext` 仍为 `disabled`，
  所以这些数字实际是系统默认调度 fallback 的工作负载输出，不是
  LCAS 性能。

因此，该 tag 不能被表述为“7 个调度器的 140 个有效 runs”。正确计数是：

| 类别 | scheduler 口径 | 计数 | 有效性 |
|---|---|---:|---|
| 有效性能 runs | `default`, `scx_simple`, `scx_cfl`, `scx_lockboost`, `scx_locknest`, `scx_lockcoop` | 6 x 4 x 5 = 120 | 可用于性能比较 |
| 无效 LCAS fallback | `scx_lcas` label | 1 x 4 x 5 = 20 | 仅供失败路径审计 |
| 工作负载输出总数 | 上述两类 | 140 | 输出完整不等于 scheduler attach 有效 |

这一修正要求所有主图、排名和结论只使用 6 个有效系列。LCAS 原始 TPS/
p95 不删除，但必须在表格中标为 `INVALID / audit-only fallback`，不得当作
LCAS 的算法性能。

#### 14.13.2 verifier 直接证据

四个结果目录分别为：

```text
test3-SysbenchOLTP /result/paper53_unbound_no_nest_runs5_20260801_224447_scx_lcas_t64_d60/
test3-SysbenchOLTP /result/paper53_unbound_no_nest_runs5_20260801_224447_scx_lcas_t128_d60/
test3-SysbenchOLTP /result/paper53_unbound_no_nest_runs5_20260801_224447_scx_lcas_t192_d60/
test3-SysbenchOLTP /result/paper53_unbound_no_nest_runs5_20260801_224447_scx_lcas_t256_d60/
```

每个目录中的 `scx.r1.log` 到 `scx.r5.log` 都包含同样的终止证据：

```text
libbpf: prog 'ops_init': BPF program load failed: -E2BIG
...
BPF program is too large. Processed 1000001 insn
processed 1000001 insns (limit 1000000)
...
libbpf: prog 'ops_init': failed to load: -E2BIG
libbpf: failed to load object 'scx_lcas'
libbpf: failed to load BPF skeleton 'scx_lcas': -E2BIG
[SCX_BUG] scx_lcas.c:46 (Argument list too long)
Failed to load skel
```

`-E2BIG` 在这里不是 sysbench 参数错误，而是 verifier 已处理
`1,000,001` 条指令，超过 `1,000,000` 的上限后拒绝加载 `ops_init`。用户态
`scx_lcas` 进程随即退出，因此无法仅凭 pidfile 存在、sysbench 输出存在或整个
shell 流程返回 0 来证明 attach 成功。

#### 14.13.3 runner 为什么没有阻断实验

`test3-SysbenchOLTP /scripts/run_full_sweep.sh` 中的启动链路是：

```text
nohup scx_lcas ... & -> 先写 pidfile
                         |
                         +-> BPF verifier -E2BIG，进程退出
                         |
wait_scx_enabled() ------+-> 轮询 5 s，sched_ext 一直 disabled
                         |
                         +-> 超时后仍 return 0
                         |
start_scx() -------------+-> wait_scx_enabled || true
                         |
                         +-> runner 继续 DB restart/warmup/measure
```

具体控制流缺陷有两层：

1. `wait_scx_enabled()` 轮询 50 次、每次 0.1 s。如果一直是 `disabled`，
   函数末尾仍执行 `return 0`，把超时报成功。
2. `start_scx()` 调用端又写成 `wait_scx_enabled || true`，即使等待函数未来
   修正为非零返回值，这一层仍会吞掉失败。

因为 Linux 在 sched_ext 为 `disabled` 时会继续使用系统默认调度类，所以 DB 和
sysbench 仍能正常运行并产生形式上完整的 TPS/p95 文件。这是一个“工作负载
成功，实验处理失败”的 fail-open runner 错误，而不是 LCAS 运行后性能低。

#### 14.13.4 影响边界

1. 直接作废的只是正式非绑定 tag 中标记为 `scx_lcas` 的 20 runs。
2. 同一 tag 中其他五个 SCX 系列的 100 份日志未发现 `-E2BIG` 或其他加载
   失败；`scx_simple` 日志有持续运行计数与正常 unregister，其他四个有
   attach 和正常退出证据。加上 `default` 后，有效比较共 6 个系列、120 runs。
3. 第 14 节的 5k/1.5k 双稳态分析只使用 20 个 `scx_lockcoop` runs；这些
   run 均有 LockCoop attach、持续 locality/forced 计数和正常退出证据。因此
   LCAS 的作废不影响 LockCoop 双峰根因与 CPU x MEM 反事实结论。
4. 本节只审计了 tag `paper53_unbound_no_nest_runs5_20260801_224447`。它不能
   自动证明其他历史 tag 中的 LCAS 数据也无效，也不能自动证明其有效；
   任何历史 LCAS 行在引用前都必须独立核对对应 run 的 attach 日志。

#### 14.13.5 修复与 LCAS 补测准入条件

补测 LCAS 之前必须同时满足以下条件，不能只重跑 sysbench：

1. 修正 `scx_lcas.bpf.c` 的 verifier 指令状态爆炸，用目标内核的 BTF/
   headers 重新编译，先在当前 256-CPU 机器上独立运行 attach smoke test。
2. 将 `wait_scx_enabled()` 的超时返回值改为非零，删除调用处的 `|| true`；
   超时时立即中止该 case，不允许进入 DB restart、warmup 或 measured run。
3. 启动后同时验证：用户态 scheduler 进程仍存活、
   `/sys/kernel/sched_ext/state == enabled`、日志中没有 libbpf/verifier 失败。最好再记录
   sched_ext ops 名称或唯一的 LCAS 运行计数，避免只验证“某个 SCX 已开启”。
4. 每个 measured run 前后都保存 scheduler PID、sched_ext state/ops、attach 证据和
   退出原因；完成度检查必须同时统计“workload output”和“attach valid”。
5. 在新 tag 下按相同 MariaDB 10.6、数据集、warmup、60 s measure、非绑定口径
   重跑 `64/128/192/256 x RUNS=5` 的 20 个 LCAS cases。新结果必须与旧的
   audit-only fallback 分开存放，不得合并求均值或计算 CV。

只有在 20/20 个补测 run 都通过 attach 有效性检查后，才能恢复“7 个有效
scheduler 系列、140 个有效 runs”的表述，并将 LCAS 放回性能主图。
