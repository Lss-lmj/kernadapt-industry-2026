# 技术报告与研究资料 / Research materials

[项目首页](../README.md) · [English overview](../README.en.md) · [Results and reproduction](../docs/benchmarks.md)

本目录收录项目的专题技术报告及补充材料。软件引用使用根目录的 [CITATION.cff](../CITATION.cff)，并记录实际使用的提交版本。

These technical reports describe the mechanisms and experiments included in this repository. Use the software citation for code reuse, and identify the report, experiment configuration, and commit when discussing specific results. The reports are primarily in Chinese.

| 主题 / Topic | 报告 / Report | 代码 / Code | 数据 / Data |
| --- | --- | --- | --- |
| LockCoop：锁持有者推进与同锁放置 | [报告](E1-LockCoop.pdf) · [补充附录](E1-LockCoop-补充附录.pdf) | [src/lockcoop](../src/lockcoop/) | [论文数据](../tests/lockcoop/paper-data/) · [实验记录](../tests/lockcoop/records/) |
| KernAdapt：配置选择、执行与恢复 | [报告](E2-KernAdapt.pdf) | [src/agent](../src/agent/) | [配置预测与独立决策汇总](../tests/kernadapt/current_e1_public.v4.json) |
| FlowCausal：工具返回依赖与会话调度 | [报告](E3-FlowCausal.pdf) | [src/flowcausal](../src/flowcausal/) | [配置与逐轮汇总](../tests/flowcausal/dependency-turn-20261003/) |

[完整项目报告与章节导航](../docs/项目文档.md)连接项目背景、总体方法和各机制分析；[57 页演示文稿](../presentation/)适合技术交流。

## 引用软件 / Cite the software

```bibtex
@software{kernadapt,
  author = {{OSKernel}},
  title = {KernAdapt: Adaptive Linux Resource Control for Mixed Workloads},
  url = {https://github.com/Lss-lmj/kernadapt-industry-2026}
}
```

在实验记录中补充所用提交标识和访问日期；讨论具体机制或实验结果时，同时注明相应技术报告。

Record the commit and access date used in your experiment, and identify the relevant technical report when discussing a mechanism or result.
