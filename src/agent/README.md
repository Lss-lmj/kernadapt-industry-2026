# KernAdapt Agent 运行时

本目录包含 KernAdapt 的在线专家与控制平面实现。

KernAdapt 根据运行指标和用户目标选择资源配置，检查权限与参数后执行，再测量效果并处理异常恢复。每一步的输入、决策和结果都会保存，可供后续检查与重放。调度器通过适配接口接入控制面。

## 运行入口

```bash
python3 src/agent/run_kernadapt_online.py --help
```

`run_lockagent_online.py` 提供兼容入口。运行结果与执行记录保存在根目录 `results/`，策略配置位于 `src/skills/`。

## 当前架构

```text
native KernAdapt runtime     -> 主运行时
optional framework backend   -> 校准 / 兼容后端
mechanism adapters           -> LockCoop、FlowCausal、未来调度器
policy contracts             -> 适用性、动作、指标、回退
provenance ledger            -> 状态 -> 决策 -> 动作 -> 结果
```

## 文件地图

核心运行时：

```text
agent_runtime.py        持久化运行状态、typed events、能力、任务队列、HITL、重放
agent_services.py       memory、HITL、任务租约和 worker 执行的 CLI/服务操作
runtime_service.py      memory、HITL、resume、任务和 worker API 的 HTTP 服务
remote_worker.py        租约命令任务的远程 worker 客户端
```

策略、检查和执行：

```text
llm_policy_agent.py     兼容 OpenAI 的策略决策 agent
runtime_guard.py        RuntimeGuard 上下文构建和 PolicyEngine 入口
scx_executor.py         带前后置检查的 allowlist sched_ext 执行器
```

工作负载运行器：

```text
run_kernadapt_online.py 当前 KernAdapt 在线控制器入口
run_lockagent_online.py 兼容入口
run_mariadb_sysbench.py MariaDB/Sysbench 实验工具
run_mariadb_comparison.py MariaDB 同批次对照实验
```

状态与结果记录：

```text
state_ledger.py             主机、内核和工作负载状态记录
profile_builder.py          工作负载 profile 构建
critic.py                   critic 报告、轨迹状态和失败类别
provenance.py               证据、动作、结果和依赖图构建
causal_attribution.py       失败/依赖归因图报告
environment_compatibility.py 状态记录兼容性检查
same_batch_validator.py     证据提升的同批次验证
```

策略案例管理：

```text
skill_curator.py        技能证据和 candidate -> validated/rejected 转换
self_evolution.py       系统覆盖和 meta-template 规划器
global_memory.py        跨运行 memory 记录、检索和汇总
counterfactual_planner.py
counterfactual_executor.py
```

框架对齐和校准：

```text
framework_adapters.py        OpenAI/LangGraph/AutoGen/CrewAI 兼容 trace 包
runtime_api_adapter.py       映射到原生运行时的框架形状调用 API
native_langgraph_backend.py  可选的真实 LangGraph StateGraph 后端
runtime_parity.py             原生运行时与框架能力对齐审计
```

## 运行时模型

原生运行时按单主机和文件持久化设计，提供：

- `agent_run.json` 中的持久化 checkpoint；
- `durable_runtime.db` 中基于 SQLite 的步骤、事件、任务和 worker 状态；
- `typed_event_bus.jsonl` 中的 typed handoff 和运行时事件；
- `artifact_store.json` 中的输出文件索引；
- 特权动作前的角色能力检查；
- 高风险内核动作的人审批 gate；
- 通过 `RuntimeReplayExecutor` 进行重放；
- 面向框架比较的进程级 trace 导出。
