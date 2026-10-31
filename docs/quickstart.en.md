# Getting started

[Documentation](README.en.md) · [中文](构建与运行.md)

Choose the path that matches your goal:

| Goal | Requirements |
| --- | --- |
| Explore the control flow | Python 3.10+ |
| Build local microbenchmarks and run logic checks | Python 3.10+, Make, a C compiler on Linux |
| Load a kernel scheduler | A compatible sched_ext kernel, matching full Linux sources, a BPF toolchain, and administrative privileges |
| Repeat a performance experiment | The scheduler, workload software, and the corresponding CPU/NUMA and measurement protocol |

## Offline demo

```bash
git clone https://github.com/Lss-lmj/kernadapt-industry-2026.git kernadapt
cd kernadapt
python3 demo/run_demo.py --out results/demo
```

The demo uses the Python standard library and repository code. It passes synthetic decisions through policy and capability checks, writes persistent runtime state, and reconstructs a summary from archived experiment records. It does not load a scheduler or call a model API.

A successful run prints `DEMO PASS`. Inspect `results/demo/summary.json`, `results/demo/guard_*.json`, and the files under `results/demo/runtime/`. The [archived demo log](../tests/results/demo-output.txt) is an example of the expected stages. Performance figures in that log come from previous measurements.

## Local tools and logic checks

```bash
make build
```

This checks Python syntax and builds `build/bin/owner_progress_bench` and `build/bin/flowbench`. Kernel schedulers are built separately.

```bash
make test-all
make verify-results
```

These commands exercise control-plane and FlowCausal controller logic and reconstruct archived data. They do not substitute for a live kernel performance experiment. The repository's `tests/results/` directory contains archived outputs; new runtime outputs go under `results/`.

## Kernel schedulers

Use a **dedicated full Linux source checkout** that matches the running kernel's sched_ext interfaces. The build script copies scheduler sources into `tools/sched_ext`. A headers-only installation does not provide all required tools.

The build requires Clang/LLVM, a C compiler, Make, pkg-config, libelf, zlib, libbpf, and bpftool as appropriate for the selected kernel tree. The running kernel needs sched_ext, BPF, and BTF support. Archived experiments include Linux 6.17; kernel version alone is not a compatibility guarantee.

```bash
export KERNEL_SRC=/path/to/linux-6.17
./scripts/build-kernel.sh
export KERNADAPT_SCX_BIN_DIR="$KERNEL_SRC/tools/sched_ext/build/bin"
```

This builds the baseline and LockCoop schedulers, the synchronization observation library, and FlowCausal tools, including `scx_flowcausal_v2`, `flowd_v2`, and `flowevent_v2`. It does not load a scheduler.

FlowCausal V2 also has a separate build entry:

```bash
make -C src/flowcausal/src KERNEL_SRC="$KERNEL_SRC" check-flowcausal-v2
```

See the [Makefile](../src/flowcausal/src/Makefile) for `SCHED_EXT`, `SCX_BUILD`, and `TARGET_ARCH` overrides.

## Load and stop a scheduler

On the experiment host, inspect the options before loading:

```bash
"$KERNADAPT_SCX_BIN_DIR/scx_lockcoop" -h
sudo "$KERNADAPT_SCX_BIN_DIR/scx_lockcoop"
```

In a separate terminal:

```bash
cat /sys/kernel/sched_ext/state
cat /sys/kernel/sched_ext/root/ops
```

Stop the loader with Ctrl-C and confirm that `state` returns to `disabled`. Match CPU scope and lock-observation options to the workload protocol.

## Control-plane and workload entry points

```bash
python3 src/agent/run_kernadapt_online.py --help
python3 src/agent/run_mariadb_comparison.py --help
python3 src/lockcoop/scripts/mariadb_oltp/formal_mariadb_oltp.py --help
python3 src/flowcausal/experiments/run_live_sandbox.py --help
```

Optional model-policy settings are defined in [llm_policy_agent.py](../src/agent/llm_policy_agent.py). The offline demo does not require them.

Before repeating a benchmark, choose a row in the [results guide](benchmarks.md) and use its source data, workload setup, CPU placement, and baseline. For architecture and extension points, read [Architecture](architecture.md).
