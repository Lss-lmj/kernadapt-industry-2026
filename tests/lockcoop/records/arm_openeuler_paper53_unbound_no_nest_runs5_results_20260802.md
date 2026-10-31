# MariaDB/Sysbench OLTP 非绑定 NUMA RUNS=5 测试结果清单

- 报告生成时间：2026-08-02
- 正式结果 tag：`paper53_unbound_no_nest_runs5_20260801_224447`
- 结果目录：`test3-SysbenchOLTP /result/`
- 本报告只统计上述 tag 的正式结果，不包含 smoke/debug 试跑。

## 1. 结论摘要

这轮实验是不做 NUMA 亲和性限制的 RUNS=5 矩阵。结果显示：绝对 TPS 低于经过拓扑控制的口径，且部分调度器存在较高波动；但从相对性能看，`scx_lockcoop` 在 64/128/192/256 四个并发档均为最高 TPS，且在高并发下相对 `default` 和 `scx_simple` 的优势更加明显。

| threads | scx_lockcoop TPS | p95 ms | TPS CV | vs default | vs scx_simple |
|---:|---:|---:|---:|---:|---:|
| 64 | 3314.51 | 46.22 | 0.628 | 2.54x | 1.42x |
| 128 | 3051.76 | 103.48 | 0.685 | 4.41x | 1.92x |
| 192 | 1901.61 | 216.60 | 0.298 | 4.69x | 2.91x |
| 256 | 1889.80 | 281.76 | 0.404 | 5.44x | 5.54x |

## 2. 实验口径

| 项目 | 值 |
|---|---|
| tag | `paper53_unbound_no_nest_runs5_20260801_224447` |
| workload | Sysbench OLTP read/write |
| threads | `64 128 192 256` |
| schedulers | `default scx_simple scx_lcas scx_cfl scx_lockboost scx_locknest scx_lockcoop` |
| runs | 5 per scheduler/thread point |
| measured time | 60 s per run |
| warmup | 10 s before each measured run |
| tables | 16 |
| table size | 1,000,000 rows/table |
| MariaDB data directory | `<workspace>/.mariadb106_data` |
| MariaDB socket | `<workspace>/.mariadb106_run/mariadb106.sock` |
| NUMA policy | 非绑定：DB 重启命令不带 `numactl`；sysbench 使用 `/usr/bin/sysbench` |
| scx_lockcoop args | `-A -m 10 -t 2 -M` |
| result count | 7 schedulers × 4 thread counts × 5 runs = 140 measured runs |

执行入口：

```bash
env \
  TAG='paper53_unbound_no_nest_runs5_20260801_224447' \
  RUNS=5 \
  RUN_TIME=60 \
  WARMUP_TIME=10 \
  THREADS_LIST='64 128 192 256' \
  SCHEDS='default scx_simple scx_lcas scx_cfl scx_lockboost scx_locknest scx_lockcoop' \
  SCX_ARGS_SCX_LOCKCOOP='-A -m 10 -t 2 -M' \
  './test3-SysbenchOLTP /scripts/run_mariadb106_home_unbound_matrix.sh'
```

## 3. 软硬件环境

### 3.1 OS / Kernel

```text
Linux localhost.localdomain 6.17.0-lockcoop-scx617 #1 SMP Fri Jul 31 03:07:36 CST 2026 aarch64 aarch64 aarch64 GNU/Linux
NAME="openEuler"
VERSION="24.03 (LTS)"
ID="openEuler"
VERSION_ID="24.03"
PRETTY_NAME="openEuler 24.03 (LTS)"
ANSI_COLOR="0;31"
sched_ext state after run: disabled
```

### 3.2 CPU / NUMA

```text
Architecture:                            aarch64
CPU op-mode(s):                          32-bit, 64-bit
Byte Order:                              Little Endian
CPU(s):                                  256
On-line CPU(s) list:                     0-255
Vendor ID:                               ARM
BIOS Vendor ID:                          Ampere(R)
Model name:                              Neoverse-N1
BIOS Model name:                         Ampere(R) Altra(R) Max Processor M128-30 CPU @ 3.0GHz
BIOS CPU family:                         257
Model:                                   1
Thread(s) per core:                      1
Core(s) per socket:                      128
Socket(s):                               2
Stepping:                                r3p1
Frequency boost:                         disabled
CPU(s) scaling MHz:                      100%
CPU max MHz:                             3000.0000
CPU min MHz:                             1000.0000
BogoMIPS:                                50.00
Flags:                                   fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp
L1d cache:                               16 MiB (256 instances)
L1i cache:                               16 MiB (256 instances)
L2 cache:                                256 MiB (256 instances)
NUMA node(s):                            2
NUMA node0 CPU(s):                       0-127
NUMA node1 CPU(s):                       128-255
Vulnerability Gather data sampling:      Not affected
Vulnerability Ghostwrite:                Not affected
Vulnerability Indirect target selection: Not affected
Vulnerability Itlb multihit:             Not affected
Vulnerability L1tf:                      Not affected
Vulnerability Mds:                       Not affected
Vulnerability Meltdown:                  Not affected
Vulnerability Mmio stale data:           Not affected
Vulnerability Old microcode:             Not affected
Vulnerability Reg file data sampling:    Not affected
Vulnerability Retbleed:                  Not affected
Vulnerability Spec rstack overflow:      Not affected
Vulnerability Spec store bypass:         Mitigation; Speculative Store Bypass disabled via prctl
Vulnerability Spectre v1:                Mitigation; __user pointer sanitization
Vulnerability Spectre v2:                Vulnerable: Unprivileged eBPF enabled

available: 2 nodes (0-1)
node 0 cpus: 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 58 59 60 61 62 63 64 65 66 67 68 69 70 71 72 73 74 75 76 77 78 79 80 81 82 83 84 85 86 87 88 89 90 91 92 93 94 95 96 97 98 99 100 101 102 103 104 105 106 107 108 109 110 111 112 113 114 115 116 117 118 119 120 121 122 123 124 125 126 127
node 0 size: 515450 MB
node 0 free: 510131 MB
node 1 cpus: 128 129 130 131 132 133 134 135 136 137 138 139 140 141 142 143 144 145 146 147 148 149 150 151 152 153 154 155 156 157 158 159 160 161 162 163 164 165 166 167 168 169 170 171 172 173 174 175 176 177 178 179 180 181 182 183 184 185 186 187 188 189 190 191 192 193 194 195 196 197 198 199 200 201 202 203 204 205 206 207 208 209 210 211 212 213 214 215 216 217 218 219 220 221 222 223 224 225 226 227 228 229 230 231 232 233 234 235 236 237 238 239 240 241 242 243 244 245 246 247 248 249 250 251 252 253 254 255
node 1 size: 513994 MB
node 1 free: 508905 MB
node distances:
node   0   1 
  0:  10  20 
  1:  20  10
```

### 3.3 Memory / Storage

```text
total        used        free      shared  buff/cache   available
Mem:           1.0Ti       9.3Gi       995Gi        20Mi       4.6Gi       995Gi
Swap:          4.0Gi          0B       4.0Gi

NAME               MODEL                 SIZE TYPE FSTYPE      MOUNTPOINTS
nvme1n1            INTEL SSDPF2KX960HZ 894.3G disk             
├─nvme1n1p1                              100M part vfat        /boot/efi
├─nvme1n1p2                                1G part ext4        /boot
└─nvme1n1p3                            893.2G part LVM2_member 
  ├─openeuler-root                     759.4G lvm  ext4        /var/lib/isulad/storage/overlay
  │                                                            /var/lib/isulad/mnt
  │                                                            /
  ├─openeuler-swap                         4G lvm  swap        [SWAP]
  └─openeuler-home                         1T lvm  ext4        /home
nvme0n1            INTEL SSDPF2KX960HZ 894.3G disk             
└─nvme0n1p1                            894.3G part LVM2_member 
  └─openeuler-home                         1T lvm  ext4        /home
```

### 3.4 数据库与压测工具

| 组件 | 版本/配置 |
|---|---|
| sysbench | `sysbench 1.0.20` |
| MariaDB client/server | `<workspace>/.mariadb106_root/usr/bin/mariadb  Ver 15.1 Distrib 10.6.27-MariaDB, for debian-linux-gnu (aarch64) using  EditLine wrapper` |
| MariaDB root | `.mariadb106_root` |
| MariaDB control script | `arm_openeuler/root_scripts/.mariadb106_control.sh` |
| Buffer pool wait hook | `arm_openeuler/root_scripts/.mariadb106_wait_buffer_pool.sh` |

MariaDB 关键变量：

| variable | value |
|---|---:|
| `binlog_format` | `MIXED` |
| `innodb_buffer_pool_size` | `134217728` |
| `innodb_flush_log_at_trx_commit` | `1` |
| `log_bin` | `OFF` |
| `max_connections` | `1024` |
| `max_prepared_stmt_count` | `100000` |
| `performance_schema` | `OFF` |
| `sync_binlog` | `0` |
| `version` | `10.6.27-MariaDB-ubu2204` |
| `version_comment` | `mariadb.org binary distribution` |

### 3.5 sched_ext 二进制

| scheduler | binary | size bytes | mtime |
|---|---|---:|---|
| `scx_simple` | `lock-sched/tools/sched_ext/build/bin/scx_simple` | 1145992 | 2026-07-31 13:39:33  |
| `scx_lcas` | `lock-sched/tools/sched_ext/build/bin/scx_lcas` | 1146280 | 2026-07-31 15:18:49  |
| `scx_cfl` | `lock-sched/tools/sched_ext/build/bin/scx_cfl` | 1149216 | 2026-07-31 15:18:51  |
| `scx_lockboost` | `lock-sched/tools/sched_ext/build/bin/scx_lockboost` | 1148328 | 2026-07-31 00:22:02  |
| `scx_locknest` | `lock-sched/tools/sched_ext/build/bin/scx_locknest` | 1148312 | 2026-07-31 01:37:14  |
| `scx_lockcoop` | `lock-sched/tools/sched_ext/build/bin/scx_lockcoop` | 1227608 | 2026-07-31 17:50:58  |

Git 状态：

```text
M lock-sched/tools/sched_ext/Makefile
 M lock-sched/tools/sched_ext/scx_lcas.bpf.c
 M lock-sched/tools/sched_ext/scx_lockcoop.bpf.c
 M lock-sched/tools/sched_ext/scx_lockcoop.c
 M lock-sched/tools/sched_ext/scx_locknest_v9.c
 M lock-sched/tools/sched_ext/scx_simple.c
 M "test3-SysbenchOLTP /scripts/run_full_sweep.sh"
 M "test3-SysbenchOLTP /scripts/run_threads_sweep.sh"
?? "test3-SysbenchOLTP /scripts/run_mariadb106_home_unbound_matrix.sh"
```

## 4. 完成度校验

| threads | default | scx_simple | scx_lcas | scx_cfl | scx_lockboost | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| 128 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| 192 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| 256 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |

所有 28 个 scheduler/thread 点均完成 5/5。

## 5. Case 汇总

说明：TPS CV = TPS 标准差 / TPS 均值；`TPS min/max` 是 5 次 measured run 的吞吐范围。

| threads | scheduler | TPS mean | TPS sd | TPS CV | p95 ms | avg ms | TPS min | TPS max | vs default | vs scx_simple |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | `default` | 1303.16 | 87.43 | 0.067 | 121.99 | 49.27 | 1163.38 | 1383.78 | 1.00x | 0.56x |
| 64 | `scx_simple` | 2330.41 | 1744.66 | 0.749 | 60.38 | 35.36 | 1539.17 | 5451.32 | 1.79x | 1.00x |
| 64 | `scx_lcas` | 1256.78 | 87.00 | 0.069 | 127.45 | 51.09 | 1162.75 | 1389.71 | 0.96x | 0.54x |
| 64 | `scx_cfl` | 3021.67 | 2027.20 | 0.671 | 53.91 | 29.77 | 1539.11 | 5351.83 | 2.32x | 1.30x |
| 64 | `scx_lockboost` | 3022.83 | 2022.40 | 0.669 | 53.96 | 29.71 | 1543.63 | 5325.83 | 2.32x | 1.30x |
| 64 | `scx_locknest` | 2263.68 | 1769.78 | 0.782 | 70.60 | 37.16 | 1382.20 | 5428.02 | 1.74x | 0.97x |
| 64 | `scx_lockcoop` | 3314.51 | 2081.86 | 0.628 | 46.22 | 26.77 | 1509.16 | 5695.67 | 2.54x | 1.42x |
| 128 | `default` | 692.55 | 31.65 | 0.046 | 499.23 | 184.97 | 654.38 | 725.88 | 1.00x | 0.44x |
| 128 | `scx_simple` | 1585.44 | 268.21 | 0.169 | 159.33 | 82.23 | 1437.07 | 2062.95 | 2.29x | 1.00x |
| 128 | `scx_lcas` | 644.21 | 38.37 | 0.060 | 543.49 | 199.11 | 595.46 | 683.03 | 0.93x | 0.41x |
| 128 | `scx_cfl` | 1564.67 | 233.99 | 0.150 | 160.47 | 83.00 | 1420.24 | 1980.77 | 2.26x | 0.99x |
| 128 | `scx_lockboost` | 1831.11 | 521.85 | 0.285 | 151.58 | 74.24 | 1408.33 | 2410.24 | 2.64x | 1.15x |
| 128 | `scx_locknest` | 2147.89 | 1507.06 | 0.702 | 139.03 | 75.28 | 1238.21 | 4832.53 | 3.10x | 1.35x |
| 128 | `scx_lockcoop` | 3051.76 | 2088.99 | 0.685 | 103.48 | 59.43 | 1530.13 | 5758.90 | 4.41x | 1.92x |
| 192 | `default` | 405.64 | 34.34 | 0.085 | 1302.83 | 475.39 | 354.65 | 437.85 | 1.00x | 0.62x |
| 192 | `scx_simple` | 652.83 | 263.17 | 0.403 | 851.18 | 329.51 | 409.61 | 1058.67 | 1.61x | 1.00x |
| 192 | `scx_lcas` | 452.51 | 65.77 | 0.145 | 1202.30 | 430.94 | 363.13 | 546.98 | 1.12x | 0.69x |
| 192 | `scx_cfl` | 581.68 | 110.68 | 0.190 | 839.39 | 338.25 | 473.73 | 766.00 | 1.43x | 0.89x |
| 192 | `scx_lockboost` | 667.16 | 312.25 | 0.468 | 817.57 | 325.71 | 446.75 | 1210.26 | 1.64x | 1.02x |
| 192 | `scx_locknest` | 1356.02 | 408.63 | 0.301 | 359.01 | 151.77 | 893.71 | 1953.76 | 3.34x | 2.08x |
| 192 | `scx_lockcoop` | 1901.61 | 565.97 | 0.298 | 216.60 | 106.97 | 1507.13 | 2828.83 | 4.69x | 2.91x |
| 256 | `default` | 347.09 | 36.18 | 0.104 | 1928.11 | 741.85 | 309.90 | 402.09 | 1.00x | 1.02x |
| 256 | `scx_simple` | 341.24 | 58.53 | 0.172 | 1938.12 | 763.63 | 307.01 | 442.65 | 0.98x | 1.00x |
| 256 | `scx_lcas` | 325.94 | 25.87 | 0.079 | 2084.40 | 787.92 | 287.15 | 352.42 | 0.94x | 0.96x |
| 256 | `scx_cfl` | 443.14 | 160.42 | 0.362 | 1582.11 | 624.61 | 333.38 | 716.15 | 1.28x | 1.30x |
| 256 | `scx_lockboost` | 482.33 | 74.24 | 0.154 | 1517.35 | 539.69 | 390.63 | 596.40 | 1.39x | 1.41x |
| 256 | `scx_locknest` | 1442.67 | 232.75 | 0.161 | 360.76 | 182.09 | 1026.38 | 1551.27 | 4.16x | 4.23x |
| 256 | `scx_lockcoop` | 1889.80 | 764.14 | 0.404 | 281.76 | 147.92 | 1488.67 | 3255.19 | 5.44x | 5.54x |

## 6. TPS 矩阵

| threads | default | scx_simple | scx_lcas | scx_cfl | scx_lockboost | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1303.16 | 2330.41 | 1256.78 | 3021.67 | 3022.83 | 2263.68 | 3314.51 |
| 128 | 692.55 | 1585.44 | 644.21 | 1564.67 | 1831.11 | 2147.89 | 3051.76 |
| 192 | 405.64 | 652.83 | 452.51 | 581.68 | 667.16 | 1356.02 | 1901.61 |
| 256 | 347.09 | 341.24 | 325.94 | 443.14 | 482.33 | 1442.67 | 1889.80 |

## 7. p95 latency 矩阵，ms

| threads | default | scx_simple | scx_lcas | scx_cfl | scx_lockboost | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 121.99 | 60.38 | 127.45 | 53.91 | 53.96 | 70.60 | 46.22 |
| 128 | 499.23 | 159.33 | 543.49 | 160.47 | 151.58 | 139.03 | 103.48 |
| 192 | 1302.83 | 851.18 | 1202.30 | 839.39 | 817.57 | 359.01 | 216.60 |
| 256 | 1928.11 | 1938.12 | 2084.40 | 1582.11 | 1517.35 | 360.76 | 281.76 |

## 8. 相对 default 的 TPS 倍数

| threads | default | scx_simple | scx_lcas | scx_cfl | scx_lockboost | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 1.00x | 1.79x | 0.96x | 2.32x | 2.32x | 1.74x | 2.54x |
| 128 | 1.00x | 2.29x | 0.93x | 2.26x | 2.64x | 3.10x | 4.41x |
| 192 | 1.00x | 1.61x | 1.12x | 1.43x | 1.64x | 3.34x | 4.69x |
| 256 | 1.00x | 0.98x | 0.94x | 1.28x | 1.39x | 4.16x | 5.44x |

## 9. 相对 scx_simple 的 TPS 倍数

| threads | default | scx_simple | scx_lcas | scx_cfl | scx_lockboost | scx_locknest | scx_lockcoop |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 64 | 0.56x | 1.00x | 0.54x | 1.30x | 1.30x | 0.97x | 1.42x |
| 128 | 0.44x | 1.00x | 0.41x | 0.99x | 1.15x | 1.35x | 1.92x |
| 192 | 0.62x | 1.00x | 0.69x | 0.89x | 1.02x | 2.08x | 2.91x |
| 256 | 1.02x | 1.00x | 0.96x | 1.30x | 1.41x | 4.23x | 5.54x |

## 10. 逐 run 明细

| threads | scheduler | run | transactions | TPS | QPS | avg ms | p95 ms | total time s |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 64 | `default` | 1 | 79401 | 1322.60 | 26452.00 | 48.36 | 116.80 | 60.0321 |
| 64 | `default` | 2 | 83100 | 1383.78 | 27675.67 | 46.23 | 130.13 | 60.0518 |
| 64 | `default` | 3 | 76980 | 1281.94 | 25638.86 | 49.90 | 118.92 | 60.0466 |
| 64 | `default` | 4 | 81900 | 1364.08 | 27281.54 | 46.89 | 106.75 | 60.0396 |
| 64 | `default` | 5 | 69846 | 1163.38 | 23267.54 | 54.98 | 137.35 | 60.0360 |
| 64 | `scx_simple` | 1 | 92384 | 1539.17 | 30783.43 | 41.57 | 70.55 | 60.0210 |
| 64 | `scx_simple` | 2 | 92973 | 1548.60 | 30971.92 | 41.31 | 69.29 | 60.0360 |
| 64 | `scx_simple` | 3 | 327235 | 5451.32 | 109026.44 | 11.73 | 27.17 | 60.0276 |
| 64 | `scx_simple` | 4 | 93512 | 1557.64 | 31152.89 | 41.07 | 66.84 | 60.0333 |
| 64 | `scx_simple` | 5 | 93381 | 1555.33 | 31106.54 | 41.13 | 68.05 | 60.0385 |
| 64 | `scx_lcas` | 1 | 75460 | 1257.06 | 25141.18 | 50.89 | 123.28 | 60.0277 |
| 64 | `scx_lcas` | 2 | 76616 | 1275.91 | 25518.19 | 50.14 | 134.90 | 60.0461 |
| 64 | `scx_lcas` | 3 | 71963 | 1198.46 | 23969.11 | 53.38 | 132.49 | 60.0443 |
| 64 | `scx_lcas` | 4 | 69802 | 1162.75 | 23254.92 | 55.01 | 139.85 | 60.0310 |
| 64 | `scx_lcas` | 5 | 83445 | 1389.71 | 27794.23 | 46.03 | 106.75 | 60.0430 |
| 64 | `scx_cfl` | 1 | 321237 | 5351.83 | 107036.68 | 11.95 | 28.16 | 60.0228 |
| 64 | `scx_cfl` | 2 | 92587 | 1542.50 | 30850.03 | 41.48 | 70.55 | 60.0230 |
| 64 | `scx_cfl` | 3 | 92769 | 1545.37 | 30907.36 | 41.40 | 70.55 | 60.0295 |
| 64 | `scx_cfl` | 4 | 307914 | 5129.55 | 102591.08 | 12.47 | 29.72 | 60.0265 |
| 64 | `scx_cfl` | 5 | 92419 | 1539.11 | 30782.24 | 41.56 | 70.55 | 60.0459 |
| 64 | `scx_lockboost` | 1 | 92981 | 1548.92 | 30978.39 | 41.30 | 70.55 | 60.0287 |
| 64 | `scx_lockboost` | 2 | 319680 | 5325.83 | 106516.57 | 12.01 | 27.66 | 60.0233 |
| 64 | `scx_lockboost` | 3 | 92668 | 1543.63 | 30872.53 | 41.45 | 70.55 | 60.0316 |
| 64 | `scx_lockboost` | 4 | 92870 | 1547.22 | 30944.38 | 41.35 | 71.83 | 60.0229 |
| 64 | `scx_lockboost` | 5 | 309049 | 5148.55 | 102970.99 | 12.42 | 29.19 | 60.0255 |
| 64 | `scx_locknest` | 1 | 88439 | 1473.08 | 29461.57 | 43.43 | 87.56 | 60.0356 |
| 64 | `scx_locknest` | 2 | 91101 | 1517.50 | 30349.98 | 42.16 | 75.82 | 60.0326 |
| 64 | `scx_locknest` | 3 | 325801 | 5428.02 | 108560.32 | 11.78 | 24.38 | 60.0210 |
| 64 | `scx_locknest` | 4 | 91116 | 1517.62 | 30352.33 | 42.15 | 74.46 | 60.0378 |
| 64 | `scx_locknest` | 5 | 82984 | 1382.20 | 27644.05 | 46.28 | 90.78 | 60.0364 |
| 64 | `scx_lockcoop` | 1 | 324962 | 5413.87 | 108277.31 | 11.82 | 23.10 | 60.0231 |
| 64 | `scx_lockcoop` | 2 | 146252 | 2436.23 | 48724.60 | 26.25 | 44.98 | 60.0285 |
| 64 | `scx_lockcoop` | 3 | 90603 | 1509.16 | 30183.21 | 42.39 | 71.83 | 60.0344 |
| 64 | `scx_lockcoop` | 4 | 341875 | 5695.67 | 113913.31 | 11.23 | 21.89 | 60.0228 |
| 64 | `scx_lockcoop` | 5 | 91114 | 1517.64 | 30352.76 | 42.16 | 69.29 | 60.0356 |
| 128 | `default` | 1 | 40924 | 680.46 | 13609.29 | 187.94 | 511.33 | 60.1402 |
| 128 | `default` | 2 | 40694 | 677.03 | 13540.51 | 188.90 | 530.08 | 60.1061 |
| 128 | `default` | 3 | 39333 | 654.38 | 13087.68 | 195.45 | 511.33 | 60.1049 |
| 128 | `default` | 4 | 43599 | 725.00 | 14499.93 | 176.40 | 458.96 | 60.1359 |
| 128 | `default` | 5 | 43638 | 725.88 | 14517.56 | 176.18 | 484.44 | 60.1158 |
| 128 | `scx_simple` | 1 | 87242 | 1452.43 | 29048.59 | 88.08 | 161.51 | 60.0653 |
| 128 | `scx_simple` | 2 | 86310 | 1437.07 | 28741.37 | 89.02 | 164.45 | 60.0586 |
| 128 | `scx_simple` | 3 | 123892 | 2062.95 | 41259.00 | 62.00 | 164.45 | 60.0548 |
| 128 | `scx_simple` | 4 | 88139 | 1467.52 | 29350.40 | 87.17 | 158.63 | 60.0589 |
| 128 | `scx_simple` | 5 | 90550 | 1507.23 | 30144.62 | 84.86 | 147.61 | 60.0761 |
| 128 | `scx_lcas` | 1 | 41094 | 683.03 | 13660.62 | 187.21 | 484.44 | 60.1625 |
| 128 | `scx_lcas` | 2 | 39008 | 648.97 | 12979.47 | 197.09 | 549.52 | 60.1062 |
| 128 | `scx_lcas` | 3 | 40746 | 678.16 | 13563.22 | 188.60 | 502.20 | 60.0815 |
| 128 | `scx_lcas` | 4 | 35773 | 595.46 | 11909.25 | 214.83 | 601.29 | 60.0750 |
| 128 | `scx_lcas` | 5 | 36987 | 615.41 | 12308.17 | 207.84 | 580.02 | 60.1005 |
| 128 | `scx_cfl` | 1 | 87788 | 1461.32 | 29226.30 | 87.54 | 158.63 | 60.0737 |
| 128 | `scx_cfl` | 2 | 89474 | 1489.58 | 29791.60 | 85.89 | 153.02 | 60.0657 |
| 128 | `scx_cfl` | 3 | 88396 | 1471.43 | 29428.67 | 86.93 | 155.80 | 60.0738 |
| 128 | `scx_cfl` | 4 | 118990 | 1980.77 | 39615.31 | 64.58 | 167.44 | 60.0718 |
| 128 | `scx_cfl` | 5 | 85320 | 1420.24 | 28404.86 | 90.05 | 167.44 | 60.0733 |
| 128 | `scx_lockboost` | 1 | 144738 | 2410.24 | 48204.84 | 53.07 | 134.90 | 60.0503 |
| 128 | `scx_lockboost` | 2 | 89610 | 1491.71 | 29834.10 | 85.75 | 153.02 | 60.0713 |
| 128 | `scx_lockboost` | 3 | 87210 | 1451.86 | 29037.18 | 88.11 | 161.51 | 60.0669 |
| 128 | `scx_lockboost` | 4 | 84598 | 1408.33 | 28166.70 | 90.83 | 173.58 | 60.0686 |
| 128 | `scx_lockboost` | 5 | 143714 | 2393.40 | 47868.01 | 53.45 | 134.90 | 60.0450 |
| 128 | `scx_locknest` | 1 | 93455 | 1555.96 | 31119.30 | 82.22 | 142.39 | 60.0615 |
| 128 | `scx_locknest` | 2 | 74364 | 1238.21 | 24764.18 | 103.30 | 211.60 | 60.0568 |
| 128 | `scx_locknest` | 3 | 93498 | 1556.50 | 31129.97 | 82.19 | 139.85 | 60.0685 |
| 128 | `scx_locknest` | 4 | 290171 | 4832.53 | 96650.65 | 26.47 | 58.92 | 60.0444 |
| 128 | `scx_locknest` | 5 | 93470 | 1556.27 | 31125.36 | 82.21 | 142.39 | 60.0594 |
| 128 | `scx_lockcoop` | 1 | 292345 | 4869.12 | 97382.44 | 26.27 | 57.87 | 60.0397 |
| 128 | `scx_lockcoop` | 2 | 92867 | 1545.96 | 30919.17 | 82.75 | 137.35 | 60.0697 |
| 128 | `scx_lockcoop` | 3 | 93367 | 1554.67 | 31093.31 | 82.30 | 137.35 | 60.0551 |
| 128 | `scx_lockcoop` | 4 | 345787 | 5758.90 | 115178.01 | 22.21 | 44.98 | 60.0430 |
| 128 | `scx_lockcoop` | 5 | 91897 | 1530.13 | 30602.50 | 83.61 | 139.85 | 60.0576 |
| 192 | `default` | 1 | 21442 | 354.65 | 7093.03 | 540.20 | 1427.08 | 60.4583 |
| 192 | `default` | 2 | 26451 | 437.85 | 8756.99 | 437.57 | 1235.62 | 60.4102 |
| 192 | `default` | 3 | 24661 | 409.75 | 8195.08 | 467.97 | 1304.21 | 60.1839 |
| 192 | `default` | 4 | 23514 | 391.11 | 7822.13 | 490.22 | 1376.60 | 60.1201 |
| 192 | `default` | 5 | 26191 | 434.82 | 8696.34 | 440.99 | 1170.65 | 60.2331 |
| 192 | `scx_simple` | 1 | 29030 | 482.53 | 9650.53 | 397.45 | 995.51 | 60.1615 |
| 192 | `scx_simple` | 2 | 63618 | 1058.67 | 21173.38 | 181.20 | 390.30 | 60.0915 |
| 192 | `scx_simple` | 3 | 46063 | 766.54 | 15330.75 | 250.29 | 580.02 | 60.0914 |
| 192 | `scx_simple` | 4 | 24741 | 409.61 | 8192.15 | 467.82 | 1258.08 | 60.4006 |
| 192 | `scx_simple` | 5 | 32873 | 546.78 | 10935.67 | 350.79 | 1032.01 | 60.1197 |
| 192 | `scx_lcas` | 1 | 21967 | 363.13 | 7262.57 | 527.47 | 1427.08 | 60.4920 |
| 192 | `scx_lcas` | 2 | 26587 | 439.83 | 8796.63 | 435.58 | 1213.57 | 60.4472 |
| 192 | `scx_lcas` | 3 | 26858 | 446.36 | 8927.11 | 429.63 | 1191.92 | 60.1695 |
| 192 | `scx_lcas` | 4 | 28043 | 466.24 | 9324.82 | 411.41 | 1109.09 | 60.1446 |
| 192 | `scx_lcas` | 5 | 32920 | 546.98 | 10939.68 | 350.62 | 1069.86 | 60.1827 |
| 192 | `scx_cfl` | 1 | 32239 | 535.17 | 10703.31 | 358.24 | 877.61 | 60.2403 |
| 192 | `scx_cfl` | 2 | 46107 | 766.00 | 15320.08 | 250.38 | 623.33 | 60.1907 |
| 192 | `scx_cfl` | 3 | 35252 | 586.25 | 11724.95 | 327.26 | 802.05 | 60.1306 |
| 192 | `scx_cfl` | 4 | 28494 | 473.73 | 9474.66 | 404.86 | 1032.01 | 60.1469 |
| 192 | `scx_cfl` | 5 | 32889 | 547.24 | 10944.89 | 350.49 | 861.95 | 60.0983 |
| 192 | `scx_lockboost` | 1 | 32944 | 547.82 | 10956.31 | 350.05 | 943.16 | 60.1361 |
| 192 | `scx_lockboost` | 2 | 72728 | 1210.26 | 24205.22 | 158.50 | 320.17 | 60.0919 |
| 192 | `scx_lockboost` | 3 | 29465 | 489.49 | 9789.81 | 391.78 | 995.51 | 60.1941 |
| 192 | `scx_lockboost` | 4 | 38685 | 641.48 | 12829.60 | 298.84 | 719.92 | 60.3049 |
| 192 | `scx_lockboost` | 5 | 26869 | 446.75 | 8934.98 | 429.36 | 1109.09 | 60.1424 |
| 192 | `scx_locknest` | 1 | 71033 | 1181.78 | 23635.65 | 162.31 | 383.33 | 60.1050 |
| 192 | `scx_locknest` | 2 | 71789 | 1194.09 | 23881.73 | 160.60 | 390.30 | 60.1194 |
| 192 | `scx_locknest` | 3 | 117520 | 1953.76 | 39075.13 | 98.14 | 308.84 | 60.1497 |
| 192 | `scx_locknest` | 4 | 53752 | 893.71 | 17874.29 | 214.57 | 493.24 | 60.1435 |
| 192 | `scx_locknest` | 5 | 93560 | 1556.77 | 31135.35 | 123.22 | 219.36 | 60.0980 |
| 192 | `scx_lockcoop` | 1 | 93732 | 1559.81 | 31196.20 | 122.99 | 211.60 | 60.0910 |
| 192 | `scx_lockcoop` | 2 | 170011 | 2828.83 | 56576.57 | 67.80 | 196.89 | 60.0983 |
| 192 | `scx_lockcoop` | 3 | 123932 | 2061.98 | 41239.56 | 93.02 | 235.74 | 60.1023 |
| 192 | `scx_lockcoop` | 4 | 93125 | 1550.31 | 31006.12 | 123.76 | 215.44 | 60.0679 |
| 192 | `scx_lockcoop` | 5 | 90586 | 1507.13 | 30142.58 | 127.28 | 223.34 | 60.1040 |
| 256 | `default` | 1 | 18765 | 309.90 | 6198.08 | 823.88 | 2045.74 | 60.5500 |
| 256 | `default` | 2 | 21589 | 358.54 | 7170.86 | 713.00 | 1903.57 | 60.2112 |
| 256 | `default` | 3 | 20869 | 344.07 | 6881.39 | 741.88 | 1973.38 | 60.6510 |
| 256 | `default` | 4 | 19497 | 320.87 | 6417.43 | 794.94 | 2009.23 | 60.7609 |
| 256 | `default` | 5 | 24238 | 402.09 | 8041.71 | 635.53 | 1708.63 | 60.2798 |
| 256 | `scx_simple` | 1 | 18744 | 308.30 | 6166.09 | 827.54 | 2279.14 | 60.7961 |
| 256 | `scx_simple` | 2 | 18517 | 307.01 | 6140.29 | 832.26 | 2082.91 | 60.3122 |
| 256 | `scx_simple` | 3 | 18617 | 307.16 | 6143.25 | 831.36 | 1903.57 | 60.6087 |
| 256 | `scx_simple` | 4 | 20567 | 341.07 | 6821.44 | 749.38 | 2120.76 | 60.2997 |
| 256 | `scx_simple` | 5 | 26632 | 442.65 | 8853.07 | 577.61 | 1304.21 | 60.1635 |
| 256 | `scx_lcas` | 1 | 17326 | 287.15 | 5743.09 | 889.99 | 2198.52 | 60.3358 |
| 256 | `scx_lcas` | 2 | 21234 | 352.42 | 7048.49 | 725.22 | 2009.23 | 60.2493 |
| 256 | `scx_lcas` | 3 | 19041 | 314.66 | 6293.21 | 811.70 | 2159.29 | 60.5119 |
| 256 | `scx_lcas` | 4 | 20829 | 343.47 | 6869.44 | 743.09 | 2045.74 | 60.6416 |
| 256 | `scx_lcas` | 5 | 20038 | 331.98 | 6639.66 | 769.58 | 2009.23 | 60.3569 |
| 256 | `scx_cfl` | 1 | 22404 | 370.03 | 7400.68 | 690.37 | 1739.68 | 60.5448 |
| 256 | `scx_cfl` | 2 | 20270 | 333.38 | 6667.56 | 765.30 | 1803.47 | 60.8009 |
| 256 | `scx_cfl` | 3 | 20585 | 339.11 | 6782.15 | 752.32 | 1771.29 | 60.7025 |
| 256 | `scx_cfl` | 4 | 27748 | 457.04 | 9140.83 | 558.04 | 1836.24 | 60.7112 |
| 256 | `scx_cfl` | 5 | 43118 | 716.15 | 14323.09 | 357.04 | 759.88 | 60.2068 |
| 256 | `scx_lockboost` | 1 | 27814 | 461.64 | 9232.74 | 553.55 | 1678.14 | 60.2499 |
| 256 | `scx_lockboost` | 2 | 29662 | 491.39 | 9827.83 | 519.83 | 1678.14 | 60.3623 |
| 256 | `scx_lockboost` | 3 | 23560 | 390.63 | 7812.54 | 654.34 | 1561.52 | 60.3122 |
| 256 | `scx_lockboost` | 4 | 35884 | 596.40 | 11928.04 | 428.65 | 960.30 | 60.1665 |
| 256 | `scx_lockboost` | 5 | 28376 | 471.61 | 9432.23 | 542.08 | 1708.63 | 60.1672 |
| 256 | `scx_locknest` | 1 | 93275 | 1551.27 | 31025.49 | 164.87 | 297.92 | 60.1271 |
| 256 | `scx_locknest` | 2 | 61725 | 1026.38 | 20527.51 | 249.09 | 601.29 | 60.1378 |
| 256 | `scx_locknest` | 3 | 93128 | 1548.72 | 30974.41 | 165.15 | 297.92 | 60.1313 |
| 256 | `scx_locknest` | 4 | 93029 | 1546.97 | 30939.38 | 165.28 | 303.33 | 60.1354 |
| 256 | `scx_locknest` | 5 | 92557 | 1540.02 | 30800.38 | 166.08 | 303.33 | 60.1003 |
| 256 | `scx_lockcoop` | 1 | 195848 | 3255.19 | 65103.75 | 78.54 | 231.53 | 60.1637 |
| 256 | `scx_lockcoop` | 2 | 94664 | 1574.93 | 31498.60 | 162.39 | 282.25 | 60.1059 |
| 256 | `scx_lockcoop` | 3 | 95008 | 1580.43 | 31608.61 | 161.82 | 282.25 | 60.1141 |
| 256 | `scx_lockcoop` | 4 | 93135 | 1549.77 | 30995.46 | 165.04 | 292.60 | 60.0950 |
| 256 | `scx_lockcoop` | 5 | 89489 | 1488.67 | 29773.39 | 171.79 | 320.17 | 60.1124 |

## 11. 观察

- `scx_lockcoop` 在所有并发档的 TPS 均值均为第一。
- 随着并发从 64 提高到 256，`default` 和 `scx_simple` 吞吐显著下降，p95 延迟快速上升。
- 不做 NUMA 亲和性限制时，多个调度器出现较大 run-to-run 波动，尤其 64/128 下的 lock-aware 调度器和 `scx_simple` 存在明显双峰。
- 256 threads 下，`scx_lockcoop` 达到 `1889.80 TPS / p95 281.76 ms`，相对 `default` 为 `5.44x`，相对 `scx_simple` 为 `5.54x`。
- 因此，如果评价标准是相对 `default` / `scx_simple` 的端到端性能提升，而不是追求跨硬件绝对 TPS，一轮非绑定 NUMA 的 RUNS=5 结果已经能支持 `scx_lockcoop` 的相对优势。
