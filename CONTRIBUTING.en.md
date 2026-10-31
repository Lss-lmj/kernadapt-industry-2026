# Contributing to KernAdapt

[中文](CONTRIBUTING.md) · [Issue forms](https://github.com/Lss-lmj/kernadapt-industry-2026/issues/new/choose)

Documentation fixes, platform reports, independent reproductions, and improvements to the runtime or schedulers are welcome. You do not need to start with kernel code.

## Find an entry point

| Contribution | Start with | Include |
| --- | --- | --- |
| Documentation or translation | [Documentation](docs/README.en.md) | The confusing section and the corrected explanation |
| Build or runtime report | [Getting started](docs/quickstart.en.md) | OS, kernel, architecture, commit, and reproduction steps |
| Independent performance study | [Results guide](docs/benchmarks.md) | Baseline, configuration, per-run data, and calculations |
| Runtime or interface improvement | [Architecture](docs/architecture.md) | Behavior changes and affected callers |
| Scheduling mechanism | [LockCoop](src/lockcoop/) or [FlowCausal](src/flowcausal/) | Input events, state transitions, scope, and recovery |

## Report an issue

Use the build/runtime, performance/reproduction, or feature-request form. Include the smallest reproduction you can provide and remove credentials and private workload data from logs.

For performance, identify whether the baseline is native Linux, `scx_simple`, or another mechanism. Record CPU/NUMA scope, workload parameters, repetitions, and colocated-service effects. Non-improving cases are useful too.

## Submit a pull request

1. Fork the repository and use a branch for a focused change.
2. Explain the concrete problem and the resulting behavior. Discuss substantial interface changes in an issue first.
3. Update affected documentation and retain source licenses and upstream attribution.
4. State which checks you actually ran and their results. State explicitly when a relevant check was not run.

For documentation, check links, command paths, terminology, and rendering. For Python runtime or controller logic changes, `make test-all` provides the existing checks. Kernel changes need build and runtime evidence from a compatible environment. Performance claims require separate controlled measurements.

## Mechanisms and data

Explain observed events, state expiration, service budgets, scope, and failure behavior. Keep model-policy latency separate from the kernel scheduling path.

Preserve the original experiment batch and per-run values when correcting data, and explain the correction separately. Include units and calculation methods. Throughput ratios, selection regret, utility, and completion time measure different outcomes; use the [results guide](docs/benchmarks.md) to select the appropriate comparison.

Maintainers review behavior, interfaces, experimental support, and licensing. Contributions remain attributable through commits and pull-request discussions.
