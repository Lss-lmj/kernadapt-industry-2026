# Results and reproduction

[Project overview](../README.en.md) · [中文首页](../README.md) · [Research reports](../papers/README.md)

This page maps the headline results to source files, comparison units, and experiment batches. All numbers below describe archived experiments. Running the offline demo reads these records; it does not repeat their workload measurements.

## LockCoop

The database results use MariaDB/Sysbench with 256 worker threads. Throughput ratios use `scx_simple` as the denominator. A throughput ratio of 5.30 means 5.30 times the baseline throughput; p95 reductions compare latency values in milliseconds.

| Platform / placement | Baseline TPS | LockCoop TPS | Baseline p95 (ms) | LockCoop p95 (ms) | Reported comparison |
| --- | ---: | ---: | ---: | ---: | --- |
| ARM64 / node 0 | 892.73 | 4,731.80 | 754.80 | 114.85 | 5.30× TPS; 84.8% lower p95 |
| x86 / unbound, stable resource domain | 1,623.966667 | 6,724.156667 | 400.056667 | 71.403333 | 4.14× TPS; 82.2% lower p95 |
| RISC-V / unbound | 367.098 | 789.838 | 1,677.476 | 728.097 | 2.15× TPS; 56.6% lower p95 |

### Source files and batches

- **ARM64:** [mariadb_arm64_runs5_cases.csv](../tests/lockcoop/paper-data/mariadb_arm64_runs5_cases.csv), rows with `threads=256`. The `scx_simple` row comes from the August 1 batch; the `scx_lockcoop` row comes from the August 19 refresh. Both report five runs. This is the report's combined summary, not a newly paired single-batch experiment.
- **x86:** the baseline comes from [mariadb_x86_unbound_fixedseed_runs3_cases.csv](../tests/lockcoop/paper-data/mariadb_x86_unbound_fixedseed_runs3_cases.csv). The stable-domain result is `scx_x86best_lockcoop_topo_stable` in [mariadb_resource_anchor_x86_runs3_cases.csv](../tests/lockcoop/paper-data/mariadb_resource_anchor_x86_runs3_cases.csv). Each reports three runs. This headline also combines the reference baseline with a later mechanism experiment.
- **RISC-V:** [mariadb_riscv_unbound_runs10_cases.csv](../tests/lockcoop/paper-data/mariadb_riscv_unbound_runs10_cases.csv), `threads=256`, `scx_simple` and `scx_lockcoop`, ten runs in the recorded September 16 batch. The [per-run measurements](../tests/lockcoop/paper-data/mariadb_riscv_unbound_runs10.csv) and [baseline measurements](../tests/lockcoop/paper-data/mariadb_riscv_baselines_runs10.csv) retain variation and error fields. Include those fields when interpreting the workload, alongside TPS and latency.

The calculations are:

```text
throughput ratio = LockCoop TPS / baseline TPS
p95 reduction (%) = (1 - LockCoop p95 / baseline p95) * 100
```

### Comparisons within an archived batch

The August 1 ARM64 archive contains 7 schedulers × 4 thread counts × 5 runs: **140 samples**. At 256 threads, its LockCoop result is **4,304.33 TPS**, compared with **892.73 TPS** for `scx_simple`: **4.82×** throughput and **82.25%** lower p95. Relative to native Linux in that batch, the throughput ratio is **4.87×**. These are the numbers used by the offline demo.

[Original report](../tests/lockcoop/records/arm_openeulerpaper53_node0all_no_nest_runs5_results_20260801.md) · [140 samples](../tests/lockcoop/openeuler_node0_runs.csv) · [Summary](../tests/lockcoop/openeuler_node0_summary.json)

The unbound x86 fixed-configuration batch contains both `scx_simple` and `scx_lockcoop`: **1,623.97 vs 6,236.12 TPS**, about **3.84×**, with approximately **80.0%** lower p95. This comparison does not use the later stable-domain result.

To reconstruct the archived ARM64 summary from the repository root:

```bash
make verify-results
```

This command checks that archive against its CSV and JSON. It does not recompute every figure in the reports or run a new benchmark.

### Repeating a database experiment

The original ARM64 protocol records openEuler 24.03-LTS, Linux 6.17, CPUs 0–127, five runs per point, 10 seconds of warmup, 60 seconds of measurement, and a fixed MariaDB restart and buffer-pool protocol. Other batches have their own run counts and placement settings.

Use the source report and the [database runner](../src/lockcoop/scripts/mariadb_oltp/formal_mariadb_oltp.py) for the selected experiment. Keep the actual scheduler identity, CPU/NUMA placement, frequency policy, data preparation, warmup, baseline, and per-run measurements with new results. Preserve the report's distinction between pure OLTP, CPU interference, and cross-layer comparisons.

## KernAdapt

The [public experiment summary](../tests/kernadapt/current_e1_public.v4.json) contains two distinct evaluations.

### Configuration selection

Under `composition_final.aggregate`, the `kernadapt_factorized_expert_stacker` result has **560 physical groups** and mean regret **0.0435673591**. The `factor_outcome_gbdt_tuned_strong` baseline has mean regret **0.0630968250**. Their relative reduction is **30.9516%**.

Regret measures the utility lost by the selected configuration relative to the best candidate under the experiment's objective. It is a selection-quality measure, not a throughput percentage. The report describes **2,800 action executions** for these groups; the repository JSON contains aggregate results, paired comparisons, and supporting summaries rather than a raw row for every action execution.

### Independent decision execution

Under `learned_live_final`, **600 decisions** include **147 adjustments** and **453 decisions to retain Linux**. Of the 147 adjustments, **114** have positive utility; **33** do not. Across all decisions, reported mean utility is **0.01398**, with a 95% interval of **[0.01094, 0.01694]**. The summary also records two SLO-violation events.

The prediction and execution evaluations use separately fixed models or policies and answer different questions. Their counts and metrics should remain separate. See the [KernAdapt report](../papers/E2-KernAdapt.pdf) for the objective, candidate actions, split construction, and statistical method.

## FlowCausal

The [workflow summary](../tests/flowcausal/dependency-turn-20261003/summary.json) contains an `hrb.profiles` section for **4 CPUs, 8 concurrent workflows, and 3 interleaved rounds per method**.

| Metric | Native Linux (`linux`) | FlowCausal (`retained`) | Change |
| --- | ---: | ---: | ---: |
| Mean workflow completion time | 25.985710 s | 24.083527 s | −7.32% |
| Whole-workload completion time | 52.200883 s | 53.584032 s | +2.65% |

The `complete` profile is the separate dependency-change prototype: its mean completion time is 24.102995 s and makespan is 53.586987 s. It does not add a gain to the retained mechanism in this workflow experiment. The `formal` and `ablation` sections cover other controlled experiments and should not be merged with `hrb`.

[Runtime plan](../tests/flowcausal/dependency-turn-20261003/hrb-corrected-plan.json) · [Replay implementation](../src/flowcausal/benchmarks/harness-replaybench/) · [FlowCausal report](../papers/E3-FlowCausal.pdf)

The replay workload measures recorded tool-workflow behavior. Its timings do not represent complete interactive agent sessions with live model inference and remote API latency. When repeating it, preserve session count, CPU scope, command set, scheduler identity, and the definition of completion for background work.

## Sharing a reproduction

Include the repository commit, environment, exact baseline, workload command and configuration, per-run results, and the calculation used for each metric. Report non-improving cases and colocated-service effects alongside improvements. Use the [performance report form](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new?template=performance.yml).
