# KernAdapt 文档

[项目首页](../README.md) · [English documentation](README.en.md)

## 开始使用

| 文档 | 可以解决的问题 |
| --- | --- |
| [构建与运行](构建与运行.md) | 体验离线控制流程、编译工具和加载实际调度器 |
| [环境与兼容性](compatibility.md) | Python、Linux 内核、CPU 架构和权限要求 |
| [集成与测量](integration.md) | 按自己的负载确定目标、安排对照并记录结果 |
| [常见问题](faq.md) | 演示输出、内核源码、平台支持与数据差异 |

## 理解实现

- [系统架构（English）](architecture.md)：控制面、内核机制、状态与恢复。
- [代码与脚本索引](代码与脚本索引.md)：从机制找到源码、配置和实验入口。
- [控制面](../src/agent/) · [LockCoop](../src/lockcoop/) · [FlowCausal](../src/flowcausal/)。
- [依赖与许可](第三方依赖与工具说明.md)：外部工具、许可范围及文件级标识。

## 研究与复现

- [结果、数据与统计方法（English）](benchmarks.md)：首页数字的来源与计算口径。
- [实验结果来源](实验结果来源.md)：各专题报告和数据目录。
- [测试与实验说明](../tests/README.md)：检查入口、逐轮结果和实验协议。
- [技术报告索引](../papers/README.md)：KernAdapt、LockCoop、FlowCausal 专题资料。
- [作品说明书](项目文档.md)：90 页 PDF 的章节导航。

## 演示与协作

[离线演示](../demo/README.md) · [PPT、PDF 与讲稿](../presentation/) · [配音视频](../videos/) · [贡献指南](../CONTRIBUTING.md) · [维护方向](../ROADMAP.md)

## 项目材料

| 材料 | 入口 |
| --- | --- |
| PPT 与对应视频 | [项目演示](../presentation/README.md) · [视频](../videos/README.md) |
| 作品说明书 | [90 页 PDF](作品说明书.pdf) · [章节导航](项目文档.md) |
| 开发设计文档 | [14 页 PDF](开发设计文档.pdf) · [内容导航](开发设计文档.md) |

[OpenHarmony 接入设计](openharmony.md) · [部署信息记录表](部署信息记录表.md) · [中文实验结果说明](实验结果说明.md)
