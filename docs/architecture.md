# Architecture

[Documentation](README.en.md) · [中文概览](../README.md#系统如何工作)

KernAdapt separates resource-configuration decisions from per-task kernel scheduling. The control plane operates on workload state, goals, candidate configurations, and measured outcomes. Kernel mechanisms react to scheduling and synchronization events within bounded state and service budgets.

## Components

| Component | Responsibility | Source |
| --- | --- | --- |
| Workload state | Summarize workload signals and host state | [profile_builder.py](../src/agent/profile_builder.py), [state_ledger.py](../src/agent/state_ledger.py) |
| Policy | Propose and check structured decisions | [llm_policy_agent.py](../src/agent/llm_policy_agent.py), [policy cards](../src/skills/scheduler_skill_cards.json) |
| Execution | Check capabilities and scope, execute allowed scheduler operations, record outcomes | [runtime_guard.py](../src/agent/runtime_guard.py), [scx_executor.py](../src/agent/scx_executor.py) |
| Persistent runtime | Store steps, events, tasks, and checkpoints | [agent_runtime.py](../src/agent/agent_runtime.py) |
| Runtime access | Local service and worker interfaces | [runtime_service.py](../src/agent/runtime_service.py), [remote_worker.py](../src/agent/remote_worker.py) |
| Outcome analysis | Connect observations, actions, and outcomes | [critic.py](../src/agent/critic.py), [provenance.py](../src/agent/provenance.py) |
| LockCoop | Lock-holder service and successor placement | [BPF scheduler](../src/lockcoop/src/sched_ext/scx_lockcoop.bpf.c), [loader](../src/lockcoop/src/sched_ext/scx_lockcoop.c) |
| FlowCausal | Session service accounting and tool-return dependencies | [BPF scheduler](../src/flowcausal/src/bpf/scx_flowcausal_v2.bpf.c), [registration](../src/flowcausal/src/user/flowd_v2.c) |

## Configuration decisions

A workload profile and state record provide the inputs to policy selection. A decision identifies a scheduler or resource action, the evidence used, expected effects, and recovery conditions. The execution path checks the selected action against allowed capabilities and configuration constraints before invoking it. Outcome records support comparing the adjusted and baseline configurations and retaining or restoring the previous state.

The [offline demo](../demo/run_demo.py) shows these interfaces with synthetic inputs. It includes accepted and rejected decisions and capability checks, without entering the kernel execution path.

Model-backed policy is optional. The kernel data path does not perform model inference. The runtime uses local files and SQLite for persistence; service and worker interfaces build on this runtime rather than establishing a distributed consensus service.

## LockCoop

LockCoop maintains relationships among a lock, its waiters, a confirmed holder when available, and recent holder CPU information.

- **LockBoost** extends the currently running holder's timeslice within a budget when the required ownership and waiting evidence are present.
- **LockNest** uses per-lock execution history and the configured resource domain to guide successor placement.
- Shared observations expire or fall back when information is absent, stale, or out of scope. A futex wake alone does not establish lock ownership.

The [shared observation header](../src/lockcoop/src/sched_ext/include/scx/lock_observe.bpf.h) implements the common state. The [user synchronization interface](../src/lockcoop/src/sched_ext/include/scx/lockcoop_user_sync.h) exposes annotations for additional synchronization paths. Separate LockBoost and LockNest implementations support mechanism studies.

[LockCoop report](../papers/E1-LockCoop.pdf) · [Module guide](../src/lockcoop/README.md)

## FlowCausal

FlowCausal groups processes by session using cgroups. Registration associates sessions and tool invocations; kernel events determine when a process actually blocks on a return dependency. Fork, wakeup, exit, and tool-generation changes update the relationship.

The outer scheduler accounts for service across sessions. Within a session, confirmed return dependencies can receive bounded early service. Actual CPU consumption is charged to that session, with quota, concurrency, and advance-service limits still applying. Parent-child ancestry or entry into a wait syscall alone is insufficient evidence.

The current runtime path is **V2**. The [dependency-change prototype](../src/flowcausal/experiments/prototypes/dependency-turn-20261003/) is a separate experiment and has separate ablation results.

[FlowCausal report](../papers/E3-FlowCausal.pdf) · [Shared state definitions](../src/flowcausal/src/include/flowcausal_v2.h)

## Extending the project

An extension should identify its input events, state lifetime, action scope, service budget, fallback behavior, and measurable objective. A new mechanism also needs a clear baseline and a way to distinguish its runtime identity from other schedulers. Configuration-policy changes should document their inputs and decisions separately from kernel timing behavior.

Use the [contribution guide](../CONTRIBUTING.en.md) for review expectations and the [results guide](benchmarks.md) for reporting experimental comparisons.
