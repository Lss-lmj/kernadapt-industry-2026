# RUNS=5 MariaDB/Sysbench OLTP 测试结果清单

本仓库保留原实验报告；下文 `<workspace>` 表示原实验机归档位置。随仓库提供的[逐轮 CSV](../openeuler_node0_runs.csv)、[汇总 JSON](../openeuler_node0_summary.json)和[技术论文](../../../papers/E1-LockCoop.pdf)可直接查阅。

日期：2026-08-01  
报告编号：`paper53_no_nest_runs5_20260801_045744`  
目标：补测论文 MariaDB/Sysbench OLTP 中去除 `scx_nest` 后的 7 个调度器，在 64/128/192/256 并发下的端到端性能。

---

## 1. 结论摘要

本轮 `RUNS=5` 已完整完成：

- scheduler 数量：7；
- thread 档位：64、128、192、256；
- 每个 case：5 次；
- 总有效 run：`7 × 4 × 5 = 140`；
- raw case 目录：28 个；
- 测试结束后 `/sys/kernel/sched_ext/state = disabled`。

最关键的 256-thread 结果：

| scheduler | TPS mean | TPS CV | p95 mean ms |
|---|---:|---:|---:|
| default | 883.42 | 0.031 | 738.61 |
| scx_simple | 892.73 | 0.041 | 754.80 |
| scx_lcas | 917.15 | 0.031 | 730.43 |
| scx_cfl | 884.55 | 0.056 | 728.24 |
| scx_lockboost | 1512.74 | 0.035 | 400.75 |
| scx_locknest | 3454.38 | 0.361 | 262.60 |
| scx_lockcoop | 4304.33 | 0.022 | 133.95 |

观察：

- `scx_lockcoop` 在 256 threads 下达到 `4304.33 TPS / p95 133.95 ms`，p95 落入论文目标区间，TPS 接近 4.5k。
- `scx_lockcoop` 的 256-thread TPS CV 只有 `0.022`，这轮非常稳定。
- `scx_locknest` 的 128/256-thread CV 分别为 `0.247` 和 `0.361`，明显超过 `0.15`，主要由单次低态 outlier 拉低均值。
- `default/scx_simple/scx_lcas/scx_cfl` 在 256 threads 下都只有约 `0.88k–0.92k TPS`，p95 约 `728–755 ms`。
- `scx_lockboost` 有中等改善，但仍远低于 `scx_lockcoop` 和高态下的 `scx_locknest`。

---

## 2. 硬件环境

### 2.1 服务器与固件

| 项目 | 值 |
|---|---|
| 厂商 | Inspur |
| 机型 | `null`，hostnamectl 原样输出 |
| Firmware version | `01.03.02.02.11` |
| Firmware date | 2023-05-10 |
| 架构 | ARM64 / AArch64 |

### 2.2 CPU

| 项目 | 值 |
|---|---|
| CPU 型号 | Ampere Altra Max Processor M128-30 |
| Microarchitecture | Neoverse-N1 |
| Socket | 2 |
| 每 socket core | 128 |
| 总 CPU | 256 |
| SMT | 无，`Thread(s) per core = 1` |
| 频率 | 3.0 GHz |
| Frequency boost | disabled |
| CPU scaling | 100% |
| CPU governor | `performance` |
| cpufreq boost | `0` |
| L1d | 16 MiB，共 256 instances |
| L1i | 16 MiB，共 256 instances |
| L2 | 256 MiB，共 256 instances |

### 2.3 NUMA

| 项目 | 值 |
|---|---|
| NUMA nodes | 2 |
| NUMA domain A CPUs | `0-127` |
| NUMA domain B CPUs | `128-255` |
| NUMA domain A memory | 515450 MB |
| NUMA domain B memory | 513994 MB |
| NUMA distance | local 10，remote 20 |

### 2.4 内存

| 项目 | 值 |
|---|---:|
| MemTotal | 1054152188 kB，约 1.0 TiB |
| MemAvailable | 1044583644 kB |
| Swap | 4.0 GiB |
| HugePages_Total | 0 |

### 2.5 存储与文件系统

| 挂载点 | 文件系统 | 容量 | 已用 | 可用 | 使用率 |
|---|---|---:|---:|---:|---:|
| `/` | ext4 | 747G | 428G | 282G | 61% |
| `/home` | ext4 | 1007G | 19G | 938G | 2% |

数据库 datadir：

```text
<workspace>/.mariadb106_data
```

底层 NVMe：

- `INTEL SSDPF2KX960HZ` × 2；
- `/home` 位于 `openeuler-home` LVM；
- `/` 位于 `openeuler-root` LVM。

---

## 3. 软件环境

### 3.1 OS 与内核

| 项目 | 值 |
|---|---|
| OS | openEuler 24.03 LTS |
| Kernel | `6.17.0-lockcoop-scx617` |
| Kernel build | `#1 SMP Fri Jul 31 03:07:36 CST 2026` |
| Architecture | `aarch64` |
| sched_ext state after test | `disabled` |

内核启动参数：

```text
BOOT_IMAGE=/vmlinuz-6.17.0-lockcoop-scx617 root=/dev/mapper/openeuler-root rd.lvm.lv=openeuler/root rd.lvm.lv=openeuler/swap video=VGA-1:640x480-32@60me cgroup_disable=files apparmor=0 crashkernel=1024M,high smmu.bypassdev=0x1000:0x17 smmu.bypassdev=0x1000:0x15 console=tty0
```

### 3.2 工具链与压测工具

| 工具 | 版本 |
|---|---|
| sysbench | 1.0.20 |
| GCC | 12.3.1，openEuler 12.3.1-38.oe2403 |
| Clang | 17.0.6，openEuler 17.0.6-18.oe2403 |
| Python | 3.11.6 |

### 3.3 MariaDB

本轮目标不是系统 MariaDB，而是仓库内独立 MariaDB 10.6 实例。系统中同时存在 `/usr/libexec/mariadbd --basedir=/var/lib/mysql/`，但 sysbench 使用 socket 指向独立实例：

```text
<workspace>/.mariadb106_run/mariadb106.sock
```

独立 MariaDB 10.6 启动命令关键参数：

```text
<workspace>/.mariadb106_root/usr/sbin/mariadbd
--no-defaults
--basedir=<workspace>/.mariadb106_root/usr
--datadir=<workspace>/.mariadb106_data
--socket=<workspace>/.mariadb106_run/mariadb106.sock
--port=3316
--user=root
--skip-name-resolve
--max-connections=1024
--max-prepared-stmt-count=100000
--innodb-buffer-pool-size=134217728
--innodb-buffer-pool-instances=1
--skip-log-bin
--performance-schema=OFF
```

MariaDB 变量，取自本轮 raw log：

| 变量 | 值 |
|---|---|
| version | `10.6.27-MariaDB-ubu2204` |
| innodb_buffer_pool_size | `134217728`，128 MiB |
| innodb_flush_log_at_trx_commit | `1` |
| sync_binlog | `0` |
| log_bin | `OFF` |
| binlog_format | `MIXED` |
| max_connections | `1024` |
| max_prepared_stmt_count | `100000` |

---

## 4. 实验方法

### 4.1 调度器列表

本轮去除 `scx_nest`，因为前一轮 live rerun 已经确认 `scx_nest` 会将 sched_ext 卡到 `disabling+all`，需要 BMC 强制重启恢复。

本轮调度器：

```text
default
scx_simple
scx_lcas
scx_cfl
scx_lockboost
scx_locknest
scx_lockcoop
```

`scx_lockcoop` 参数：

```text
-A -m 10 -t 2 -M
```

`scx_lockcoop` attach 日志确认：

```text
scx_lockcoop attached.
threshold=2
force_busy_anchor=on
mariadbd_only=on
min_seq=10
owner_progress=on
futex_evidence=on
mutex_evidence=on
```

### 4.2 Sysbench 参数

```text
sysbench oltp_read_write
--mysql-db=sbtest
--mysql-user=sbuser
--mysql-password=password
--tables=16
--table-size=1000000
--threads={64,128,192,256}
--time=60
--report-interval=10
--percentile=95
--mysql-socket=<workspace>/.mariadb106_run/mariadb106.sock
run
```

### 4.3 运行口径

| 项目 | 值 |
|---|---|
| RUNS | 5 |
| RUN_TIME | 60s |
| WARMUP_TIME | 10s |
| THREADS_LIST | `64 128 192 256` |
| 每个 run 前 | 重启 MariaDB 10.6 |
| 每个 run 前 | 设定 `innodb_buffer_pool_size=134217728` |
| 每个 run 前 | 等待/记录 buffer pool 状态 |
| 每个 scheduler run 前后 | 启停对应 sched_ext scheduler |
| 结果汇总 | `summarize_results.py --expected-runs 5` |

实际启动命令：

```bash
cd <workspace>

TAG=paper53_no_nest_runs5_$(date +%Y%m%d_%H%M%S)
LOG="test3-SysbenchOLTP /result/${TAG}.log"

nohup env \
TAG="$TAG" \
RUNS=5 RUN_TIME=60 WARMUP_TIME=10 \
THREADS_LIST='64 128 192 256' \
SCHEDS='default scx_simple scx_lcas scx_cfl scx_lockboost scx_locknest scx_lockcoop' \
SCX_ARGS_SCX_LOCKCOOP='-A -m 10 -t 2 -M' \
stdbuf -oL -eL '<仓库中的 MariaDB 10.6 复现实验封装脚本>' \
> "$LOG" 2>&1 &
```

实际日志开头确认：

```text
TAG=paper53_no_nest_runs5_20260801_045744
RUNS=5 RUN_TIME=60 WARMUP_TIME=10
THREADS_LIST=64 128 192 256
SCHEDS=default scx_simple scx_lcas scx_cfl scx_lockboost scx_locknest scx_lockcoop
HOME_DATA=<workspace>/.mariadb106_data
DB_SOCKET=<workspace>/.mariadb106_run/mariadb106.sock
DB_AFTER_RESTART_CMD=<workspace>/.mariadb106_wait_buffer_pool.sh
SCX_ARGS_SCX_LOCKCOOP=-A -m 10 -t 2 -M
```

---

## 5. 结果文件

主日志：

```text
test3-SysbenchOLTP /result/<本轮运行日志>.log
```

CSV 汇总：

```text
test3-SysbenchOLTP /result/summary_runs.csv
test3-SysbenchOLTP /result/summary_cases.csv
test3-SysbenchOLTP /result/summary_coverage.csv
```

SVG 图：

- 组合预览 HTML（历史路径：`<workspace>/test3-SysbenchOLTP /result/plots_svg/paper53_no_nest_runs5_20260801_045744/index.html`）
- TPS 曲线 SVG（历史路径：`<workspace>/test3-SysbenchOLTP /result/plots_svg/paper53_no_nest_runs5_20260801_045744/tps_mean.svg`）
- p95 延迟曲线 SVG（历史路径：`<workspace>/test3-SysbenchOLTP /result/plots_svg/paper53_no_nest_runs5_20260801_045744/p95_latency.svg`）

### 5.1 图表预览

TPS 曲线：

TPS mean（历史路径：`<workspace>/test3-SysbenchOLTP /result/plots_svg/paper53_no_nest_runs5_20260801_045744/tps_mean.svg`）

p95 延迟曲线：

p95 latency（历史路径：`<workspace>/test3-SysbenchOLTP /result/plots_svg/paper53_no_nest_runs5_20260801_045744/p95_latency.svg`）

raw case 目录数：

```text
28
```

run-level row 数：

```text
140
```

---

## 6. 汇总结果

### 6.1 TPS mean

| scheduler | 64 | 128 | 192 | 256 |
|---|---:|---:|---:|---:|
| default | 5441.44 | 2221.00 | 1171.82 | 883.42 |
| scx_simple | 5237.62 | 2379.05 | 1257.58 | 892.73 |
| scx_lcas | 5122.49 | 2034.18 | 1202.85 | 917.15 |
| scx_cfl | 5316.02 | 2171.62 | 1317.90 | 884.55 |
| scx_lockboost | 5143.08 | 2700.02 | 1948.41 | 1512.74 |
| scx_locknest | 5510.50 | 4243.19 | 4304.39 | 3454.38 |
| scx_lockcoop | 5468.94 | 5015.37 | 4414.30 | 4304.33 |

### 6.2 TPS CV

| scheduler | 64 | 128 | 192 | 256 |
|---|---:|---:|---:|---:|
| default | 0.034 | 0.067 | 0.025 | 0.031 |
| scx_simple | 0.017 | 0.047 | 0.033 | 0.041 |
| scx_lcas | 0.016 | 0.030 | 0.060 | 0.031 |
| scx_cfl | 0.005 | 0.044 | 0.017 | 0.056 |
| scx_lockboost | 0.007 | 0.017 | 0.023 | 0.035 |
| scx_locknest | 0.019 | 0.247 | 0.035 | 0.361 |
| scx_lockcoop | 0.036 | 0.064 | 0.057 | 0.022 |

CV 超过 0.15 的 case：

| scheduler | threads | TPS CV | 原因 |
|---|---:|---:|---|
| scx_locknest | 128 | 0.247 | r2 明显低态 |
| scx_locknest | 256 | 0.361 | r1 明显低态 |

### 6.3 p95 latency mean，ms

| scheduler | 64 | 128 | 192 | 256 |
|---|---:|---:|---:|---:|
| default | 28.12 | 150.70 | 424.13 | 738.61 |
| scx_simple | 29.31 | 138.50 | 384.95 | 754.80 |
| scx_lcas | 31.05 | 164.00 | 420.16 | 730.43 |
| scx_cfl | 27.96 | 151.06 | 367.18 | 728.24 |
| scx_lockboost | 29.51 | 117.68 | 236.67 | 400.75 |
| scx_locknest | 23.87 | 78.94 | 98.27 | 262.60 |
| scx_lockcoop | 22.14 | 54.22 | 94.26 | 133.95 |

## 7. 逐轮结果

### 7.1 r1-r5 TPS 与 p95

| scheduler | threads | TPS runs r1-r5 | p95 runs r1-r5 |
|---|---:|---|---|
| default | 64 | 5734.49, 5579.02, 5275.19, 5297.56, 5320.92 | 25.28, 26.68, 29.72, 29.72, 29.19 |
| default | 128 | 2431.02, 2373.86, 2100.95, 2079.17, 2119.98 | 134.90, 139.85, 158.63, 161.51, 158.63 |
| default | 192 | 1132.51, 1209.15, 1143.51, 1193.62, 1180.33 | 434.83, 411.96, 434.83, 411.96, 427.07 |
| default | 256 | 853.18, 921.94, 862.50, 909.97, 869.49 | 746.32, 707.07, 733.00, 733.00, 773.68 |
| scx_simple | 64 | 5311.70, 5285.39, 5164.71, 5326.43, 5099.88 | 28.16, 28.67, 30.26, 28.67, 30.81 |
| scx_simple | 128 | 2521.06, 2435.99, 2343.46, 2188.01, 2406.73 | 130.13, 134.90, 139.85, 150.29, 137.35 |
| scx_simple | 192 | 1224.45, 1320.29, 1288.09, 1204.87, 1250.22 | 397.39, 376.49, 369.77, 404.61, 376.49 |
| scx_simple | 256 | 894.49, 947.63, 848.12, 914.72, 858.70 | 746.32, 719.92, 787.74, 746.32, 773.68 |
| scx_lcas | 64 | 5179.99, 4990.98, 5129.67, 5231.27, 5080.54 | 30.26, 32.53, 30.81, 29.72, 31.94 |
| scx_lcas | 128 | 2040.38, 2044.95, 1934.06, 2123.73, 2027.78 | 164.45, 161.51, 176.73, 155.80, 161.51 |
| scx_lcas | 192 | 1171.91, 1111.68, 1251.44, 1162.17, 1317.04 | 427.07, 458.96, 397.39, 427.07, 390.30 |
| scx_lcas | 256 | 933.64, 922.53, 925.27, 862.56, 941.74 | 719.92, 733.00, 746.32, 733.00, 719.92 |
| scx_cfl | 64 | 5348.49, 5327.06, 5321.64, 5262.09, 5320.80 | 27.66, 27.66, 28.16, 28.67, 27.66 |
| scx_cfl | 128 | 2196.69, 2317.59, 2109.85, 2030.50, 2203.46 | 147.61, 139.85, 155.80, 164.45, 147.61 |
| scx_cfl | 192 | 1339.36, 1290.92, 1309.08, 1347.63, 1302.52 | 356.70, 369.77, 376.49, 363.18, 369.77 |
| scx_cfl | 256 | 969.47, 900.53, 868.51, 864.30, 819.94 | 694.45, 707.07, 719.92, 759.88, 759.88 |
| scx_lockboost | 64 | 5126.19, 5181.99, 5077.02, 5165.00, 5165.19 | 29.72, 29.19, 29.72, 29.72, 29.19 |
| scx_lockboost | 128 | 2615.47, 2696.33, 2744.83, 2726.89, 2716.60 | 123.28, 116.80, 114.72, 116.80, 116.80 |
| scx_lockboost | 192 | 1915.31, 1982.22, 1875.80, 1996.72, 1971.99 | 244.38, 231.53, 244.38, 231.53, 231.53 |
| scx_lockboost | 256 | 1509.32, 1548.11, 1579.60, 1420.85, 1505.81 | 390.30, 383.33, 383.33, 434.83, 411.96 |
| scx_locknest | 64 | 5525.00, 5391.25, 5406.52, 5671.95, 5557.79 | 23.95, 24.38, 24.38, 22.69, 23.95 |
| scx_locknest | 128 | 4815.75, 2151.69, 4691.48, 4802.43, 4754.62 | 58.92, 155.80, 61.08, 58.92, 59.99 |
| scx_locknest | 192 | 4233.81, 4496.87, 4181.55, 4472.02, 4137.69 | 95.81, 97.55, 101.13, 97.55, 99.33 |
| scx_locknest | 256 | 964.73, 4228.14, 3964.56, 4012.95, 4101.54 | 733.00, 142.39, 147.61, 147.61, 142.39 |
| scx_lockcoop | 64 | 5640.34, 5482.44, 5353.51, 5160.68, 5707.72 | 23.10, 22.28, 20.74, 22.28, 22.28 |
| scx_lockcoop | 128 | 4725.98, 4565.69, 5145.50, 5206.59, 5433.11 | 58.92, 58.92, 53.85, 51.94, 47.47 |
| scx_lockcoop | 192 | 4237.49, 4186.41, 4206.64, 4733.77, 4707.18 | 99.33, 101.13, 94.10, 87.56, 89.16 |
| scx_lockcoop | 256 | 4292.23, 4275.51, 4478.35, 4187.69, 4287.87 | 134.90, 137.35, 130.13, 132.49, 134.90 |

---

## 8. 与 baseline 的 256-thread 对比

以 256-thread `default` 为 baseline：

| scheduler | TPS mean | speedup vs default | p95 mean ms | p95 reduction vs default |
|---|---:|---:|---:|---:|
| default | 883.42 | 1.00× | 738.61 | 0.0% |
| scx_simple | 892.73 | 1.01× | 754.80 | -2.2% |
| scx_lcas | 917.15 | 1.04× | 730.43 | 1.1% |
| scx_cfl | 884.55 | 1.00× | 728.24 | 1.4% |
| scx_lockboost | 1512.74 | 1.71× | 400.75 | 45.7% |
| scx_locknest | 3454.38 | 3.91× | 262.60 | 64.4% |
| scx_lockcoop | 4304.33 | 4.87× | 133.95 | 81.9% |

以 256-thread `scx_simple` 为 baseline：

| scheduler | TPS mean | speedup vs scx_simple | p95 mean ms | p95 reduction vs scx_simple |
|---|---:|---:|---:|---:|
| scx_simple | 892.73 | 1.00× | 754.80 | 0.0% |
| scx_lockcoop | 4304.33 | 4.82× | 133.95 | 82.3% |
| scx_locknest | 3454.38 | 3.87× | 262.60 | 65.2% |
| scx_lockboost | 1512.74 | 1.69× | 400.75 | 46.9% |

---

## 9. 注意事项

1. 本轮是 `RUNS=5` 固定次数，不是 `min=5/max=8` 自动补跑版本。
2. `summary_cases.csv` 已经提供 `tps_cv`，因此可以看到哪些 case 波动偏大。
3. 如果严格采用 `CV > 0.15` 自动补跑口径，本轮中只有 `scx_locknest@128` 和 `scx_locknest@256` 需要补跑；`scx_lockcoop` 全部线程档位都低于 0.15。
4. 原 PNG 脚本因当前 Python 环境缺少 `matplotlib` 无法运行；本轮图已用无依赖 SVG 方式生成。
5. `scx_nest` 没有进入本轮，是因为前一轮已经触发 sched_ext `disabling+all` 卡死，并导致服务器需要 BMC 强制重启。
