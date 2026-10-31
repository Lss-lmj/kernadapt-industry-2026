# LockCoop EuroSys 修改期实验记录

本目录包含 LockCoop 的实验报告、排障日志和机制验证记录，涵盖锁观测、锁身份、LockNest、LockBoost、evidence scope，以及 ARM64 实验的环境排查和统计结果。

## 文件索引

| 文件 | 主要内容 | 评阅价值 |
| --- | --- | --- |
| [mariadb_oltp_lockcoop_diagnosis_20260731.md](mariadb_oltp_lockcoop_diagnosis_20260731.md) / [NUMA/NVMe 双稳态汇报](presentations/numa_nvme_bimodality.pdf) | ARM64 MariaDB/OLTP 性能排障与双稳态归因 | 说明 NUMA 放置、数据库版本、数据目录、NVMe/O_DIRECT 路径和 evidence 范围如何影响复现稳定性。 |
| [arm_openeulerpaper53_node0all_no_nest_runs5_results_20260801.md](arm_openeulerpaper53_node0all_no_nest_runs5_results_20260801.md) | ARM64 NUMA 节点 0、RUNS=5 正式复测 | 给出 7 个调度器、4 个并发档、每点 5 次运行的主复测结果。 |
| [arm_openeuler_paper53_unbound_no_nest_runs5_results_20260802.md](arm_openeuler_paper53_unbound_no_nest_runs5_results_20260802.md) | ARM64 非 NUMA 绑定 RUNS=5 复测 | 展示不做拓扑绑定时的性能波动和相对优势。 |
| [lockcoop_three_schedulers_code_report_20260730.md](lockcoop_three_schedulers_code_report_20260730.md) | `scx_locknest`、`scx_lockboost`、`scx_lockcoop` 三个调度器的代码状态 | 说明弱 placement 与强 owner-progress 的证据链拆分。 |
| [eurosys_phase1_observation_semantics_report_20260730.md](eurosys_phase1_observation_semantics_report_20260730.md) / [汇报](presentations/phase1_lock_observation_owner_tracking_beamer.pdf) | Phase 1：锁观测与 owner 追踪正确性 | 覆盖 lock event taxonomy、futex wait/wake 语义、owner evidence 和 waiter dependency。 |
| [eurosys_phase2_lock_identity_report_20260730.md](eurosys_phase2_lock_identity_report_20260730.md) / [汇报](presentations/phase2_lock_identity_state_safety_beamer.pdf) | Phase 2：Lock identity 与状态污染风险 | 覆盖 lock key schema、private futex `{tgid,uaddr}`、地址复用、stale state 和 map pressure。 |
| [eurosys_owner_offset8_coverage_report_20260730.md](eurosys_owner_offset8_coverage_report_20260730.md) | x86 glibc/NPTL owner offset=8 覆盖率实验 | 说明 pthread owner resolver 的覆盖范围和调度用途。 |
| [eurosys_locknest_phase3a_report_20260730.md](eurosys_locknest_phase3a_report_20260730.md) / [汇报](presentations/phase3a_locknest_adaptive_beamer.pdf) | Phase 3A：LockNest 自适应锚点与 fallback | 说明同锁放置机制的主要设计和验证。 |
| [eurosys_locknest_phase3a2_experiment_report.md](eurosys_locknest_phase3a2_experiment_report.md) / [汇报](presentations/phase3a2_locknest_sensitivity_beamer.pdf) | Phase 3A-2：LockNest 参数敏感性 | 说明 queue cap、cooldown、expiry 等参数对吞吐和尾延迟的影响。 |
| [eurosys_locknest_phase3a3_experiment_report.md](eurosys_locknest_phase3a3_experiment_report.md) / [汇报](presentations/phase3a3_locknest_evidence_beamer.pdf) | Phase 3A-3：LockNest 机制验证 | 锚点控制、fallback 和多热点锁实验。 |
| [eurosys_locknest_phase3a4_experiment_report_20260801.md](eurosys_locknest_phase3a4_experiment_report_20260801.md) / [汇报](presentations/phase3a4_locknest_anchor_maturity_beamer.pdf) | Phase 3A-4：anchor maturity 实验 | 说明更窄控制条件下的收益边界。 |
| [eurosys_lockboost_phase3b_experiment_report.md](eurosys_lockboost_phase3b_experiment_report.md) / [汇报](presentations/phase3b_lockboost_adaptive_beamer.pdf) | Phase 3B：LockBoost 自适应 owner-progress 控制 | 说明 bounded/adaptive/inf grant 的性能和公平性权衡。 |
| [eurosys_phase3c_evidence_scope_experiment_report_20260801.md](eurosys_phase3c_evidence_scope_experiment_report_20260801.md) / [汇报](presentations/phase3c_evidence_scope_beamer.pdf) | Phase 3C：通用 evidence scope | 说明 all / TGID / cgroup scope 的正确性、边界和性能影响。 |
