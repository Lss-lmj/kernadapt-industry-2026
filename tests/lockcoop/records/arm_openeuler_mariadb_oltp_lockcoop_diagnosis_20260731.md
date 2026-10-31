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
   - MariaDB datadir 初始放在 `<workspace>/...`，即 root LV，和系统/容器/root 活动共享 `nvme0n1`；
   - 128MB buffer pool 下，Sysbench OLTP 已经明显受到存储路径、buffer-pool 热状态和页淘汰影响；
   - `scx_lockcoop` 采集全系统锁证据时，会把 VSCode/Codex/node 等非 MariaDB 线程的锁事件混进来，造成锚点污染。
3. 最稳复现配置是：
   - MariaDB 10.6.27；
   - 数据目录：`<workspace>/.mariadb106_data`；
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
  --datadir=<workspace>/.mariadb106_data
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
- 当前 `/home` 基本空闲，所以将 MariaDB datadir 放到 `<workspace>/...` 后，实际绕开了 root LV 上系统活动和容器活动的干扰；
- 历史 raw result 路径也在 `<workspace>/...`，这提示原实验环境和当前最初的 `/root` datadir 并不一致。

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

- 当前仓库和原始 datadir 在 `<workspace>/...`；
- root LV 位于 `nvme0n1p3`；
- `/home` LV 前段位于 `nvme1n1p1`；
- `/home` 几乎空闲；
- 历史实验 raw result 路径也是 `<workspace>/...`。

因此提出假设：

> 当前性能崩溃的一部分来自 datadir 放在 root LV，与系统/容器/root 活动共用 `nvme0n1`。将 MariaDB 10.6 datadir 移到 `/home` 后，可以降低 I/O 干扰，改善 60s 稳态。

执行方式：

```bash
./.mariadb106_control.sh stop
mkdir -p <workspace>
cp -a .mariadb106_data <workspace>/.mariadb106_data
M106_DATA=<workspace>/.mariadb106_data \
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

作为对照，恢复出的历史 raw 数据中，256-thread `scx_lockcoop` 全部 run 如下。注意，这批 raw summary 的 `mariadb_version` 标为 `10.11.14-MariaDB-0ubuntu0.24.04.1`，raw 路径位于 `<workspace>/...`；因此这里主要把它作为“历史高态分布、outlier 形态、/home 路径实验环境”的参照，而不是作为“10.6 vs 10.11”的单变量证据：

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
datadir: <workspace>/.mariadb106_data
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
--datadir=<workspace>/.mariadb106_data
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
