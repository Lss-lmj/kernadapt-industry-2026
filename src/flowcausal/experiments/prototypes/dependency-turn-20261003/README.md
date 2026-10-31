# 跨命令依赖实验原型

这里保存 2026-10-03 同批消融使用的完整 BPF 调度器与 loader。共享头文件、登记程序及事件客户端沿用当前 V2。默认实现仍在 `src/bpf/`、`src/user/`，本目录独立构建。

在 FlowCausal 子工程目录执行：

```bash
prototype_dir=$(mktemp -d /tmp/flowcausal-dependency.XXXXXX)
cp -a src "$prototype_dir/src"
cp experiments/prototypes/dependency-turn-20261003/scx_flowcausal_v2.bpf.c "$prototype_dir/src/bpf/"
cp experiments/prototypes/dependency-turn-20261003/scx_flowcausal_v2.c "$prototype_dir/src/user/"
mkdir "$prototype_dir/bin"
make -C "$prototype_dir/src" KERNEL_SRC=/path/to/linux-6.17 TMPDIR="$prototype_dir/bin" check-flowcausal-v2
```

完整原型参数为 `--sustained-tool-weight --aggregate-background --rebase-background-dependency`；消融通过 `--ignore-cross-generation-dependency` 关闭跨命令关联，或移除 `--aggregate-background` 关闭后台合计份额。CPU 范围由 `--managed-cpus` 指定。

固定工作量基准在 `src/tests/flowcausal_v2_dependency_turn.c`，运行入口为 `experiments/test_flowcausal_v2_dependency_turn.py`。结果与计划位于作品根目录的 `tests/flowcausal/dependency-turn-20261003/`；该原型在完整八工作流回放中未取得额外收益。
