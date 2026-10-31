# Phase 3C: 通用 Evidence Scope 实验报告

日期：2026-08-01  
状态：实现、scope admission/cleanup correctness、定向性能与 MariaDB service-scope 证据完成。

## 1. 问题与结论

Phase 3C 的目标不是将应用名硬编码进 BPF，而是提供一个通用 control plane，决定哪些 producer 可以创建 LockNest/LockBoost evidence。它回答的是“谁可以更新 advisory state”，不是“lock identity 是谁”，更不是 CPU/cgroup 调度隔离。

实现提供 `all|tgid|cgroup` 三种 profile。实验结论：

- `all` 保持默认兼容，BPF 热路径直接返回，不查询 allow map；
- cgroup allow map 能同时观察到 admitted 和 denied event；out-of-scope workload 仍然完成；
- TGID allow map 在独立 two-process workload 中也同时观察到 admitted 和 denied event；
- task 在已有 evidence 后迁出 target cgroup 时，waiter cleanup 继续执行，最终没有 map failure、counter underflow 或 scheduler stall；
- 仅允许 MariaDB service cgroup、排除 Sysbench client 的 service-only scope 在当前 OLTP 拓扑下使 TPS 从约 5,362 降至约 1,306。因此 scope 是正确性/隔离/归因接口，不是默认性能优化开关，论文默认仍为 `scope=all`。

## 2. 实现与边界

修改范围：

- `scx_lockcoop.bpf.c/.c`
- `scx_locknest.bpf.c/.c`
- `lock_observe.bpf.h`

新增两个非 LRU control-plane maps：

```text
HASH<u32, u8> evidence_tgid_allow,    max_entries=64
HASH<u64, u8> evidence_cgroup_allow,  max_entries=64
```

接口：

```text
--evidence-scope all|tgid|cgroup
--evidence-tgid PID                 (repeatable)
--evidence-cgroup-id ID             (repeatable)
--evidence-cgroup-path PATH         (repeatable cgroup v2 path)
--evidence-scope-stats
```

`--evidence-cgroup-path` 先验证 cgroup v2 filesystem，再读取 kernfs directory inode ID；本机 `/sys/fs/cgroup` 解析为 ID 1。最终 admission 由 `bpf_get_current_cgroup_id()` 与 allow-map lookup 决定，受控 cgroup workload 给出了运行期匹配验证。

scope gate 覆盖 mutex enter/slowpath/unlock 和 private-futex wait/wake，且位于新的 owner/hint/waiter/anchor state mutation 之前。`sys_exit_futex` 不接受 scope deny：它始终尝试 cleanup；mutex unlock 对已有 owner state 也允许 revoke/delete。scope 不进入 `{kind, addr, scope_id}` lock key，因此不把同一同步对象因 cgroup migration 拆分成不同 identity。

`all` 的 BPF 逻辑：

```text
if (evidence_scope_mode == ALL)
    return true;
```

因此默认 mode 不增加 allow-map lookup 或 scope counter write。

## 3. 建构与基本运行验证

以下全部成功：

```text
make -B -C lock-sched/tools/sched_ext scx_lockcoop scx_locknest scx_lockboost
python3 scripts/lockcoop_phase12_smoke.py
```

- 空 allow set 被 CLI 拒绝；
- `scope=cgroup` + nonexistent ID attach 成功，计数为 `admitted=0, denied>0`，workload/OS 均继续运行；
- `scope=cgroup` + `/sys/fs/cgroup` path 成功解析为 ID 1；
- 每个 attach/detach 后 `/sys/kernel/sched_ext/state=disabled`。

## 4. Admission/denial 定向试验

256-thread mutex microbenchmark，100 us critical section，2 s measurement：

| profile | ops/s | p99 us | seen | admitted | denied | 完成 |
|---|---:|---:|---:|---:|---:|---|
| all | 9,899.82 | 1 | 0 | 0 | 0 | yes |
| cgroup root (ID 1) | 9,772.88 | 1 | 55,176 | 194 | 54,982 | yes |
| nonexistent cgroup | 9,747.10 | 1 | 53,402 | 0 | 53,402 | yes |

这里的 root profile 不是 target workload profile：测试进程位于 user slice，而 root allow set 只允许少量系统/root task。因此它的价值是证明 map gate 和 reject path 的计数语义，而不是宣称 scope 使该 workload 变快。`all` 的 scope counters 有意为零，因为这是默认 fast path。

原始数据：`results/phase3a4_3c_20260801_afterclaim/scope_runs.csv`。

## 5. 两进程隔离与 cgroup migration

专用 runner `scripts/run_phase3c_scope_isolation.py` 创建两个 cgroup：

```text
target cgroup:     128-thread, 1-lock mutex workload
background cgroup: 128-thread, 64-lock mutex workload
```

调度器只允许 target cgroup 产生 evidence。target 运行 2 s 后，其 process group 被迁移到 background cgroup，并继续运行直到自然结束。结果：

| 指标 | 值 |
|---|---:|
| target cgroup id | 687,870 |
| target producer admitted | 55,105 |
| out-of-scope producer denied | 21,641,058 |
| target return code | 0 |
| background return code | 0 |
| map failure | 0 |
| counter underflow | 0 |
| final sched_ext state | disabled |

迁移前 target cgroup 包含 PID 3,684,991；迁移后 `target/cgroup.procs` 为空。`cleanup_out` 记录迁出后成功清理的 transient dependency，数值随高频 futex activity 增长，不能解释为 persistent stale-entry 数量。正确性判据是：cleanup 未被 deny 阻塞、两个 workload 均结束、map/invariant 计数为零、scheduler 正常 detach。

原始数据：`results/phase3c_scope_isolation_20260801_051759/result.json`。

## 6. TGID Scope Isolation

同一 runner 以目标 benchmark 的 runtime TGID 建立 allow entry，再启动 scheduler 与 64-lock background cgroup。target 在 2 s delay 后 exec 为 128-thread/1-lock workload；background 不在 allow set 内。TGID mode 不以 cgroup migration 作为 deny 条件，因而本组验证 TGID admission 本身，而不是复测 cgroup migration cleanup。

| 指标 | 值 |
|---|---:|
| target TGID | 3,698,985 |
| target producer admitted | 364,034 |
| out-of-scope producer denied | 40,118,587 |
| target/background return code | 0 / 0 |
| map failure / counter underflow | 0 / 0 |
| final sched_ext state | disabled |

admitted/denied 同时非零，且两个 workload 均自然结束，证明 TGID allow set 不是仅存在于 loader 的配置参数。该 profile 仍只适合 controlled 或短期单进程 deployment：TGID 可被复用，长期服务应优先使用稳定的 cgroup identity。

原始数据：`results/phase3c_scope_tgid_isolation_20260801_054546/result.json`。

## 7. MariaDB service-scope 边界

MariaDB main PID 位于 `/system.slice/mariadb.service`，cgroup ID 为 686,408。运行 `scope=cgroup` 仅允许该 service cgroup，而 Sysbench clients 保持 out-of-scope。三次新的 60 s run：

| Run | service-scope TPS | p95 ms | p99 ms |
|---:|---:|---:|---:|
| 1 | 1,308.60 | 475.79 | 590.56 |
| 2 | 1,267.32 | 493.24 | 612.21 |
| 3 | 1,340.77 | 475.79 | 590.56 |
| mean | **1,305.56** | **481.61** | **597.77** |

同一机器、同一数据库配置下的 `scope=all` 正式 off baseline 是 5,361.77 TPS、p99 128.72 ms。因此 service-only cgroup scope 在该 server/client 交互中明显退化。

这不是 map key collision 或 attach failure：scope isolation test 已表明 admission/denial/migration cleanup 正确。根因边界是 profile 只保留服务器侧 evidence，却移除了发起请求、等待/唤醒链中 client-side 的 lock evidence；而 sched_ext 仍调度全部 task。正确部署含义是：scope allow set 必须包含完整相关 workload closure，或保持 `all`。本阶段不在 BPF 中猜测“哪些 cgroup 应自动加入”，也不通过 TPS 在线调整 scope。

因此：

| profile | 部署决策 |
|---|---|
| all | 默认；保留当前端到端性能路径 |
| tgid | controlled experiment / 临时单进程部署接口 |
| cgroup | 正确性、隔离、归因和经过完整 workload closure 验证的部署接口 |
| service-only cgroup | 本机 MariaDB/Sysbench 不作为性能候选 |

原始数据：`results/phase3a4_3c_20260801_afterclaim/formal_runs.csv`（`formal_scope_service` 三行）。

## 8. 论文口径与未做的事情

可以主张：LockCoop 支持通用 TGID/cgroup evidence admission，且 scope 不改变 lock identity；out-of-scope producer 不创建新的 evidence；已有 state 的 cleanup 能跨 migration 完成。

不能主张：任意 service-only scope 会提升数据库性能；scope 是安全沙箱；scope 自动推断完整 request path；cgroup ID 取代 private-futex 的 TGID identity；本结果代表 ARM/VM/多 NUMA 平台。

Phase 3C 实现了通用 evidence boundary，并验证了准入、拒绝与状态清理的正确性。background lock-churn 的收益、mixed-workload SLA 和 TGID reuse 防护尚未验证；生产级 workload closure discovery、动态 allow-set 更新和完整多租户 fairness policy 留待后续部署与公平性实验。
