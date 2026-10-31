# 控制面演示

在项目根目录运行：

```bash
make build
make demo
```

依赖 Python 3.10+。演示使用合成输入，调用 PolicyEngine、角色能力检查和持久化运行时，验证有效的策略约束、未知调度器、oracle 泄漏、回退条件和角色权限分支，并核算归档 openEuler 实验结果。

成功时输出 `DEMO PASS`。结果保存在 `results/demo/summary.json`、`guard_*.json` 和 `runtime/`。

指定输出目录：

```bash
./demo/run.sh --out /tmp/kernadapt-demo
```

调度器加载与性能复现方法见 [构建与运行](../docs/构建与运行.md)。
