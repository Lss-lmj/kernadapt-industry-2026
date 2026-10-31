# LockNest Phase 3A Adaptive Mechanism Report

Date: 2026-07-30

## Scope

This report records the implemented Phase 3A LockNest adaptive mechanism, the code paths changed, and the targeted experiments run on the current machine.  The implementation deliberately keeps the default scheduler behavior performance-compatible: adaptive LockNest is enabled only with `-A`, and the safe adaptive profile does not impose an anchor queue cap unless `-Q` is explicitly provided.

Phase 3A changes only LockNest placement.  It does not change LockBoost owner evidence, waiter accounting, boost threshold, or `SCX_SLICE_INF` behavior.

## Implemented Code

Files changed:

- `lock-sched/tools/sched_ext/scx_locknest.bpf.c`
- `lock-sched/tools/sched_ext/scx_locknest.c`
- `lock-sched/tools/sched_ext/scx_lockcoop.bpf.c`
- `lock-sched/tools/sched_ext/scx_lockcoop.c`
- `scripts/lockcoop_phase12_smoke.py`
- `scripts/run_lockcoop_phase12_runtime.py`
- `scripts/run_lockcoop_phase12_db_coverage.py`
- `test3-SysbenchOLTP /scripts/formal_mariadb_oltp.py`

New `lock_nest_info` fields:

```c
u64 adaptive_expiry_ns;
u64 cooldown_until_ns;
u32 direct_hits;
u32 pressure_rejects;
```

New runtime controls:

```text
-A            enable adaptive LockNest admission/expiry
-Q <count>    adaptive anchor queue soft limit; default unlimited
-R <count>    pressure rejects before cooldown; default 4
-P <count>    direct hits before expiry extension; default disabled
-C <ms>       cooldown window; default 5 ms
-N <ms>       minimum adaptive expiry; default follows -e
-X <ms>       maximum adaptive expiry; default 5000 ms
```

New diagnostics:

```text
STAT_ANCHOR_PRESSURE_REJECT
STAT_ANCHOR_CPUMASK_REJECT
STAT_ANCHOR_COOLDOWN_REJECT
STAT_ANCHOR_PREVCPU_FALLBACK
STAT_ANCHOR_DEFAULT_FALLBACK
STAT_ANCHOR_EXPIRY_SHORTEN
STAT_ANCHOR_EXPIRY_EXTEND
```

## Mechanism

The final implementation uses a conservative two-level policy.

Default off path:

- `adaptive_nest=false` keeps the original LockNest placement branch.
- `weak_anchor_queue_limit=~0U` still short-circuits before `scx_bpf_test_and_clear_cpu_idle()`, preserving the old direct handoff behavior.
- No adaptive hit/reject writes are performed on the hot path.

Safe adaptive path:

- `-A` enables adaptive checks.
- With default knobs, `adaptive_anchor_queue_limit=~0U`, so the adaptive path does not reject an anchor for queue depth.
- `-P` defaults to 0, so direct handoff hits do not write `direct_hits` on every wakeup.
- `-N` defaults to 0, which means the minimum adaptive expiry follows the current `-e` value.

Pressure fallback path:

- If `-Q` is set and the anchor CPU queue reaches that limit, the scheduler records `STAT_ANCHOR_PRESSURE_REJECT`.
- After repeated rejects, the lock enters a short cooldown and can shorten adaptive expiry.
- During pressure, the scheduler first tries `prev_cpu` if it is allowed and idle, then falls back to `scx_bpf_select_cpu_dfl()`.

This means queue-pressure fallback exists as an experimental/defensive knob, but it is not the MariaDB balanced default because previous and current test3 results show queue caps hurt the primary OLTP workload.

## Regression Found And Fixed

During implementation, an early rewrite changed the default direct-handoff condition from:

```c
weak_anchor_queue_limit == ~0U || scx_bpf_test_and_clear_cpu_idle(anchor) || ...
```

to a helper form that called `scx_bpf_test_and_clear_cpu_idle()` before proving the anchor queue was unlimited.  That helper has side effects, so this was a real default-path regression.  The final code restores an explicit non-adaptive branch and keeps the old short-circuit behavior.

A second issue was also fixed: the test3 runner previously did not classify `EXIT: runnable task stall` as a scheduler failure.  It now treats runnable stalls and non-normal `EXIT:` lines as failures while accepting `EXIT: unregistered from user space`.

## Verification

Build and static/BTF smoke:

```text
make -C lock-sched/tools/sched_ext scx_locknest scx_lockcoop scx_lockboost
PASS: LockCoop evidence and LockNest adaptive invariants hold
```

Controlled runtime, safe adaptive:

- Result: `results/phase12_runtime/phase12_runtime_20260730_225628`
- Command: `scx_lockcoop -D -t 1 -d 100 -e 500 -O 8 -A`
- `scheduler_returncode=0`
- `bench_returncode=0`
- `sched_ext_state_after=disabled`
- `adaptive_pressure_reject=0`
- `adaptive_prevcpu_fallback=0`
- `adaptive_expiry_extend=0`

Controlled runtime, pressure path:

- Result: `results/phase12_runtime/phase12_runtime_20260730_225838`
- Command: `scx_lockcoop -D -t 1 -d 100 -e 500 -O 8 -A -Q 2`
- `scheduler_returncode=0`
- `bench_returncode=0`
- `adaptive_pressure_reject=139`
- `adaptive_prevcpu_fallback=87`
- This confirms the pressure fallback path is live, but it is not the default performance profile.

DB coverage, safe adaptive:

- Result: `results/phase12_db_coverage/phase12_db_coverage_20260730_225811`
- Command: `scx_lockcoop -D -A -O 8`, Sysbench 32 threads, 10 s
- `scheduler_returncode=0`
- `sysbench_returncode=0`
- `map_update_fail=0`
- `adaptive_pressure_reject=0`
- `private_futex_owner_hit_pct=36.29%`
- `kernel_mutex_owner_hit_pct=100.00%`
- `locality_hit_pct_waiter_cleanup=59.92%`

## test3 Performance Gate

Configuration:

- MariaDB/Sysbench OLTP read-write
- 16 tables, 1,000,000 rows each
- 256 threads
- 5 s warmup
- 20 s measurement
- one targeted run, not a matrix

Historical comparison:

| Result | Scheduler args | TPS | p95 ms | p99 ms | Status |
|---|---:|---:|---:|---:|---|
| `retest_phase12_default_o8_mariadb_256_20260730_200957` | `-O 8` | 5025.17 | 102.97 | 137.35 | historical gate |
| `phase3a_final_default_retry_mariadb_256_20260730_230025` | `-O 8` | 5464.15 | 90.78 | 121.085 | final default-off pass |
| `phase3a_adaptive_default2_mariadb_256_20260730_225640` | `-O 8 -A` | 5010.87 | 102.97 | 139.846 | final safe-adaptive pass |

Negative pressure-profile observation:

- Earlier queue-cap tests and the current `-A -Q 2` trial show that forcing a low anchor queue limit hurts MariaDB 256-thread OLTP.
- Therefore `-Q` is kept as an explicit stress/fairness knob, not as the balanced default.

One invalid default run after the final build hit a kworker runnable-task stall, then the immediate retry completed normally at 5464.15 TPS.  This is recorded as a residual sched_ext/LockBoost fairness risk to revisit in Phase 6, not as a Phase 3A accepted result.

## Conclusion

Phase 3A now has a real implementation in both standalone LockNest and composed LockCoop:

- Bounded adaptive state exists per lock.
- Safe adaptive defaults preserve the main MariaDB performance gate.
- Pressure fallback can be enabled and measured explicitly.
- Diagnostics expose pressure, cpumask, cooldown, prev-CPU fallback, default fallback, and expiry changes.

The main design conclusion is important: for the current MariaDB/Sysbench workload, the balanced LockNest policy should keep direct same-lock handoff as the default.  Queue-pressure fallback is useful as a defensive mechanism for future multi-hot-lock and fairness experiments, but a small static queue cap is not a good default.
