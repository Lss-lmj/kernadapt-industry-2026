# KernAdapt 控制面与运行时

[项目首页](../../README.md) · [Architecture in English](../../docs/architecture.md) · [开始使用](../../docs/构建与运行.md)

本模块根据运行状态与应用目标选择资源配置，检查参数、权限和作用范围后执行，并保存结果与恢复记录。调度器通过执行接口接入；逐任务调度由内核机制完成。

## 从哪里开始

体验控制流程：

```bash
python3 demo/run_demo.py --out results/demo
```

该命令在仓库根目录运行，使用合成输入，不需要加载调度器或配置模型 API。[输出说明](../../demo/README.md)

查看在线控制器参数：

```bash
python3 src/agent/run_kernadapt_online.py --help
```

`run_lockagent_online.py` 为兼容入口。策略配置见 [scheduler_skill_cards.json](../skills/scheduler_skill_cards.json)，示例输入见 [examples](examples/)。

## 按功能找代码

| 功能 | 入口 | 主要职责 |
| --- | --- | --- |
| 运行状态 | [agent_runtime.py](agent_runtime.py) | 持久化状态、事件、能力权限、任务队列和重放 |
| 本地操作 | [agent_services.py](agent_services.py) | 记忆记录、人工确认、任务与 worker 操作 |
| 服务接口 | [runtime_service.py](runtime_service.py)、[remote_worker.py](remote_worker.py) | HTTP 服务和远程任务客户端 |
| 工作负载描述 | [profile_builder.py](profile_builder.py)、[state_ledger.py](state_ledger.py) | 组织负载信号、主机与内核状态 |
| 模型策略 | [llm_policy_agent.py](llm_policy_agent.py) | 请求结构化决策并检查返回字段 |
| 执行管理 | [runtime_guard.py](runtime_guard.py)、[scx_executor.py](scx_executor.py) | 适用条件、允许的动作、前后置检查与执行 |
| 结果分析 | [critic.py](critic.py)、[provenance.py](provenance.py) | 效果评价及输入、动作、输出的关联记录 |
| 经验管理 | [global_memory.py](global_memory.py)、[skill_curator.py](skill_curator.py) | 查询案例、记录结果和更新策略经验 |
| 框架适配 | [framework_adapters.py](framework_adapters.py)、[native_langgraph_backend.py](native_langgraph_backend.py) | 外部接口适配与可选执行后端 |
| 数据库对照 | [run_mariadb_comparison.py](run_mariadb_comparison.py) | 按配置组织 MariaDB 对照实验 |

## 状态与输出

运行时以单主机和本地持久化为基础，主要记录包括：

- `agent_run.json`：运行检查点；
- `durable_runtime.db`：SQLite 中的步骤、事件、任务和 worker 状态；
- `typed_event_bus.jsonl`：结构化运行事件；
- `artifact_store.json`：输出文件索引。

执行前按角色检查所需能力，部分高影响动作支持人工确认。`RuntimeReplayExecutor` 使用记录重放运行过程。服务和远程 worker 复用该状态模型。

## 研究与贡献

[控制面测试](tests/) · [配置预测与决策结果](../../docs/benchmarks.md#kernadapt) · [专题报告](../../papers/E2-KernAdapt.pdf) · [贡献指南](../../CONTRIBUTING.md)
