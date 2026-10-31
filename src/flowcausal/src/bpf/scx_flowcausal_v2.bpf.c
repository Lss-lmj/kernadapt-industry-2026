/* SPDX-License-Identifier: GPL-2.0 */
/*
 * FlowCausal v2
 *
 * 默认先在已登记工作流之间分配处理器时间，再在工作流内对内核确认的
 * 工具等待目标做有限推进。全局进程队列仅作为可切换对照。
 * 未登记任务走独立后备队列，不受 FlowCausal 策略影响。
 */
#include <scx/common.bpf.h>
#include "flowcausal_v2.h"

/* 由后面复用的 scx_flatcg 实现定义。 */
extern const volatile u64 cgrp_slice_ns;

const volatile __u32 managed_cpu_first = 0;
const volatile __u32 managed_cpu_count = 8;
/* 只把最后一个明确等待目标视为返回尾部，宽汇合层保持正常排序。 */
const volatile __u64 progress_slice_ns = 2ULL * 1000 * 1000;
const volatile __u64 interactive_slice_ns = 1ULL * 1000 * 1000;
/* 默认使用两级工作流路径；全局进程队列仅用于对照。 */
const volatile bool shared_ordinary_path = false;
/* 消融实验可只保留工作流分组和受管处理器范围。 */
const volatile bool flow_policy_enabled = true;
/* 同一份第二版实现中的机制开关，只用于逐项分析。 */
const volatile bool progress_enabled = true;
/* 三类真实工具组合均未显示独立收益，默认关闭但保留完整对照路径。 */
const volatile bool interactive_enabled = false;
const volatile bool background_enabled = true;
const volatile bool locality_enabled = true;
const volatile bool parallel_width_enabled = true;
/* 普通任务睡醒后可以提前的上限；有等待关系证据的推进仍单独核算。
 * 默认保留原来的 20 毫秒，实验选项可启用按实际计算时间调整。 */
const volatile __u64 ordinary_wakeup_credit_ns = 20ULL * 1000 * 1000;
const volatile bool adaptive_wakeup_credit = false;
/* 普通任务不能靠极短的反复睡眠反复取得大量排队提前量。
 * 提前量最多等于这次实际睡眠时间；有因果证据的任务仍走原有预算。 */
const volatile bool sleep_bounded_wakeup_credit = false;
/* 只约束同一进程的线程数超过可用 CPU 的情况，保留独立进程间的唤醒。 */
const volatile bool threaded_wakeup_credit = false;
/* 只限制同一进程内短计算线程反复被 futex 唤醒所得的普通提前量。 */
const volatile bool local_futex_credit = true;
const volatile bool independent_weight_service = true;
const volatile bool observe_weight_service = false;
/* Per-CPU counters: local wake marks/caps [0:2], optional service diagnostics [4:7]. */
__u64 local_schedule_stats[256][8] __attribute__((aligned(64)));
const volatile bool preempt_confirmed_wait = false;
__u64 confirmed_wait_preemptions;
/*
 * scx_flatcg 已经按控制组权重维护虚拟运行时间。V2 额外维护的周期处理器
 * 时间核算用于严格限制短周期内各工作流获得的处理器时间；关闭它只保留虚拟运行时间
 * 公平性，用于判断两套公平机制是否重复。
 */
const volatile bool periodic_fair_accounting = true;
/* 只关闭等权工作流的重复周期核算；异权和动态权重仍使用完整核算。 */
const volatile bool equal_weight_fair_fastpath = true;
/*
 * 默认保留等待关系识别，但不依赖预先标注的并行阶段，
 * 而是根据整个工作流当前可运行进程数启用动态并行度。
 */
const volatile bool session_runnable_width = true;
/*
 * 返回相关任务默认允许在会话预算约束下跨工作流有限提前。只在所属
 * 工作流内部提前的路线保留为对照。
 */
const volatile bool cross_session_role_dispatch = true;
/*
 * 实验路线：等待对象只有在所属工作流当前最多两个可运行进程时，才
 * 跨工作流提前；更宽的工作流仍可在自身队列内提前。这个条件只读取
 * 当前内核计数，不使用程序名称、未来进程数或离线结果。
 */
const volatile bool cross_session_progress_when_few_runnable = false;
/* 实验路线：保护每个可运行会话后，把余量交给积压任务更多的会话。 */
const volatile bool demand_aware_borrow = false;
/* 对可运行任务数取对数，避免超宽工作流独占全部剩余处理器。 */
const volatile bool logarithmic_demand_share = false;
/* 工作流结束后，将释放的处理器时间一半等分、一半按当前需求分配。 */
const volatile bool balanced_demand_share = false;
/*
 * 只在工具入口进程已经确认阻塞、且它等待的进程已经实际排队时，
 * 临时提高该会话的调度权重。自动路线中一次作用不超过
 * automatic_weight_window_ns；阻塞或处理器排队提前结束时立即恢复。
 * 同一次等待不因为进程在“可运行”和“正在运行”之间切换而重复触发。
 * 该规则不读取程序名称，也不预测工作流以后会创建多少进程。
 */
const volatile bool adaptive_session_weight = false;
/*
 * 不使用工具的同步/异步标记。多个会话同时等待时，只有最先观察到
 * “被等待进程实际排队”的会话临时获得额外权重；本轮等待结束后
 * 立即释放，其他仍在等待的会话可以接替。该选择只依赖已经发生的
 * 内核事件，不读取程序名称、任务类别或未来运行时间。
 */
const volatile bool automatic_session_weight = false;
/* 自动权重的一次作用时间独立于工具总时长，默认与 V2 的策略周期相同。 */
const volatile __u64 automatic_weight_window_ns = 100ULL * 1000 * 1000;
/*
 * 实验路线：一次临时权重到期后先恢复同样长的时间；只有等待对象
 * 随后再次经历处理器排队，当前阻塞等待才可重新参加跨会话选择。
 */
const volatile bool automatic_weight_repeat_wait = false;
/* 实验路线：被等待进程先出现实际处理器排队，再允许关键推进。 */
const volatile bool require_runnable_wait = false;
/*
 * wait/waitpid 之外，部分工具通过管道、进程间通信或运行库同步等待。
 * 当同次工具的上层进程已经睡眠、一个后代随后真实排队时，允许该后代
 * 在下一次入队获得一个短时间片；同一次上层睡眠最多选择一个后代。
 */
const volatile bool sleeping_descendant_progress = false;
/*
 * 同一工具的后代多次实际运行且累计排队后，有限增加整个会话获得的
 * 处理器时间；不会直接提高某个后代进程的优先级。
 */
const volatile bool sustained_tool_weight = false;
/* 持续等待候选获得额外处理器时间时使用的权重倍数。 */
const volatile __u32 sustained_tool_weight_multiplier = 2;
/* 采集完全相同的信息，但不让这些信息改变会话权重。 */
const volatile bool sustained_tool_observe_only = false;
/* 只记录同一工具内后代进程对睡眠上层进程的实际唤醒，不改变调度。 */
const volatile bool descendant_wakeup_observe = false;
/* 与正式实现使用同一低开销识别，但只计数、不改变调度。 */
const volatile bool descendant_wakeup_light_observe = false;
/* 让已经实际交付进展且经历排队的进程在所属工作流内提前一次。 */
const volatile bool descendant_wakeup_progress = false;
/* 原始方案中的信息来源对照；默认值是完整的关系判断。 */
const volatile __u32 decision_mode = FLOW_V2_DECISION_RELATION;
/*
 * 正式性能运行关闭逐次调度记账。返回关系、预算和调度动作统计始终
 * 保留；全任务入队、运行和各角色运行时间只在机制核对时打开。
 */
const volatile bool detailed_accounting = false;

struct flow_v2_session_runtime {
	struct bpf_spin_lock lock;
	__u64 window_start_ns;
	__u64 boost_used_ns;
	__u64 quota_period_start_ns;
	__u64 quota_used_ns;
	__u64 quota_reserved_ns;
	__u32 runnable_tasks;
	__u32 running_tasks;
	__u32 reserved_tasks;
	__u32 peak_running_tasks;
	__u32 current_width;
	__u32 quota_throttled;
};

/* 等待关系由跟踪程序写入。该映射不含自旋锁，因而可以在跟踪程序
 * 中安全更新；调度回调读取它并改变实际会话权重。 */
struct flow_v2_session_adaptive_state {
	__u32 active_blocking_waiters;
	__u32 runnable_wait_targets;
	__u32 adaptive_weight_active;
	__u32 effective_weight;
	__u32 runnable_tasks;
	/* 当前工具等待中是否已经观察到明显的处理器排队。 */
	__u32 wait_pressure_seen;
	__u64 adaptive_weight_started_ns;
	/* 每次新的真实阻塞等待观察到排队时递增。 */
	__u64 wait_pressure_generation;
	/* 已经获得过一次额外处理器时间的等待编号。 */
	__u64 granted_wait_generation;
	/* 本次临时权重必须恢复的最晚时间。 */
	__u64 automatic_weight_deadline_ns;
	/* 同一阻塞等待再次参加选择前必须保持基础权重的最晚时间。 */
	__u64 automatic_weight_cooldown_deadline_ns;
	/* 用于多个候选会话之间按历史选择顺序轮换。 */
	__u64 last_grant_order;
	/* 本次自动提高权重最多补回的处理器时间及已经实际取得的时间。 */
	__u64 automatic_weight_service_limit_ns;
	__u64 automatic_weight_service_ns;
	__u32 sustained_descendant_pressure;
	__u32 reserved;
};

struct flow_v2_automatic_weight_state {
	/* 零表示当前没有会话占用额外权重。 */
	__u64 owner_session_id;
	__u64 owner_cgroup_id;
	__u64 grant_sequence;
	__u64 last_owner_session_id;
	/* 只有新的候选或当前拥有者释放后才重新扫描活动会话。 */
	__u64 candidate_epoch;
	__u64 last_scanned_epoch;
};

struct flow_v2_quota_task_state {
	__u64 previous_sleep_exec_runtime;
	__u64 last_burst_ns;
	__u64 sleep_started_ns;
	__u64 last_sleep_ns;
	bool completed_burst;
	bool local_futex_wakeup;
	__u64 running_since_ns;
	__u64 reserved_ns;
	__u64 fair_running_since_ns;
	__u64 fair_reserved_ns;
	__u64 fair_window_id;
	__u64 fair_runnable_epoch;
	__u32 counted_running;
	__u32 counted_runnable;
	__u32 width_reserved;
	__u32 counted_wait_target_runnable;
};

struct flow_v2_fair_runtime {
	struct bpf_spin_lock lock;
	__u64 window_id;
	__u64 runnable_epoch;
	__u64 used_ns;
	__u64 reserved_ns;
};

struct flow_v2_quota_timer_state {
	struct bpf_timer timer;
};

/* dispatch() 先从会话队列取任务，running() 才拿到任务指针。
 * 这份每处理器状态把两者之间已经预留的公平运行时间传递过去。 */
struct flow_v2_dispatch_fair_state {
	__u64 cgroup_id;
	__u64 reserved_ns;
	__u64 window_id;
	__u64 runnable_epoch;
};

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, struct flow_v2_session_config);
} session_config SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_ACTIVE_SESSION_SLOTS);
	__type(key, __u32);
	__type(value, __u64);
} active_session_slots SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u32);
} rescue_cursor SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_TOOLS);
	__type(key, struct flow_v2_tool_key);
	__type(value, struct flow_v2_tool_config);
} tool_config SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_TOOLS);
	__type(key, struct flow_v2_tool_key);
	__type(value, struct flow_v2_tool_runtime);
} tool_runtime SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, struct flow_v2_active_tool);
} active_tool_by_cgroup SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_TASKS);
	__type(key, __u32);
	__type(value, struct flow_v2_task_meta);
} flow_v2_task_meta SEC(".maps");

/* 避免在 512 字节 BPF 栈上构造完整策略状态。 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_task_meta);
} flow_v2_task_meta_scratch SEC(".maps");

/* 只有需要策略判断的返回根和等待目标进入此集合。 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_TASKS);
	__type(key, __u32);
	__type(value, __u8);
} flow_v2_policy_tasks SEC(".maps");

/* 仅登记正在 wait(-1) 中阻塞的父进程，避免普通唤醒扫描完整任务元数据。 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_TASKS);
	__type(key, __u32);
	__type(value, __u8);
} flow_v2_waiting_parents SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, struct flow_v2_session_runtime);
} session_runtime SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, struct flow_v2_session_adaptive_state);
} session_adaptive_state SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_automatic_weight_state);
} automatic_weight_state SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, struct flow_v2_fair_runtime);
} session_fair_runtime SEC(".maps");

/* 独立保存抢占时间，避免扩大调度路径上需要在栈中初始化的会话状态。 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, __u64);
} interactive_preempt_at SEC(".maps");

/*
 * 会话达到并发软上限时，为了继续查找其他会话会临时后移其虚拟
 * 时间。临时增加的量在该会话下次真正取得处理器时退回，不能被
 * 当作已经获得的处理器服务。
 */
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, FLOW_V2_MAX_SESSIONS);
	__type(key, __u64);
	__type(value, __u64);
} session_width_skip_ns SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_width_state);
} width_state SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_control_state);
} control_state SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct flow_v2_quota_task_state);
} quota_task_state SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_quota_timer_state);
} quota_timer SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_dispatch_fair_state);
} dispatch_fair_state SEC(".maps");

static __u64 quota_timer_wakeup_ns;

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_HASH);
	__uint(max_entries, 512);
	__type(key, __u64);
	__type(value, struct flow_v2_session_stats);
} session_stats SEC(".maps");

/* 避免在跟踪程序的 512 字节栈上构造完整统计初值。 */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_session_stats);
} session_stats_zero SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct flow_v2_global_stats);
} global_stats SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 8 * 1024 * 1024);
} events SEC(".maps");

static __always_inline struct flow_v2_global_stats *flow_global(void)
{
	__u32 key = 0;

	return bpf_map_lookup_elem(&global_stats, &key);
}

static __always_inline __u64
flow_effective_runnable_weight(const struct flow_v2_width_state *state)
{
	struct flow_v2_control_state *control;
	__s64 adaptive;
	__u64 raw;
	__u32 zero = 0;

	if (!state)
		return 0;
	raw = state->runnable_weight;
	adaptive = state->adaptive_weight_delta;
	if (adaptive > 0 && (__u64)adaptive <= ~0ULL - raw)
		raw += (__u64)adaptive;
	else if (adaptive < 0) {
		__u64 amount = (__u64)-adaptive;

		raw = raw > amount ? raw - amount : 0;
	}
	control = bpf_map_lookup_elem(&control_state, &zero);
	if (!control)
		return raw;
	return raw > control->disabled_runnable_weight ?
		raw - control->disabled_runnable_weight : 0;
}

static __always_inline __u64
flow_effective_runnable_epoch(const struct flow_v2_width_state *state)
{
	struct flow_v2_control_state *control;
	__u32 zero = 0;

	if (!state)
		return 0;
	control = bpf_map_lookup_elem(&control_state, &zero);
	return state->runnable_epoch + (control ? control->runnable_epoch : 0);
}

static __always_inline __u32 flow_registered_session_count(void)
{
	struct flow_v2_control_state *control;
	__u32 zero = 0;

	control = bpf_map_lookup_elem(&control_state, &zero);
	return control ? control->registered_sessions : 0;
}

static __always_inline __u32
flow_effective_runnable_sessions(const struct flow_v2_width_state *state)
{
	struct flow_v2_control_state *control;
	__u32 raw;
	__u32 zero = 0;

	if (!state)
		return 0;
	raw = state->runnable_sessions;
	control = bpf_map_lookup_elem(&control_state, &zero);
	if (!control)
		return raw;
	return raw > control->disabled_runnable_sessions ?
		raw - control->disabled_runnable_sessions : 0;
}

static __always_inline __u32
flow_effective_runnable_demand(const struct flow_v2_width_state *state)
{
	struct flow_v2_control_state *control;
	__u32 raw;
	__u32 zero = 0;

	if (!state)
		return 0;
	raw = state->runnable_demand;
	control = bpf_map_lookup_elem(&control_state, &zero);
	if (!control)
		return raw;
	return raw > control->disabled_runnable_demand ?
		raw - control->disabled_runnable_demand : 0;
}

static __always_inline __u32
flow_effective_runnable_score(const struct flow_v2_width_state *state)
{
	struct flow_v2_control_state *control;
	__u32 raw;
	__u32 zero = 0;

	if (!state)
		return 0;
	raw = state->runnable_demand_score;
	control = bpf_map_lookup_elem(&control_state, &zero);
	if (!control)
		return raw;
	return raw > control->disabled_runnable_score ?
		raw - control->disabled_runnable_score : 0;
}

static __always_inline __u32
flow_effective_running_tasks(const struct flow_v2_width_state *state)
{
	struct flow_v2_control_state *control;
	__u32 raw;
	__u32 zero = 0;

	if (!state)
		return 0;
	raw = state->running_tasks;
	control = bpf_map_lookup_elem(&control_state, &zero);
	if (!control)
		return raw;
	return raw > control->disabled_running_tasks ?
		raw - control->disabled_running_tasks : 0;
}

static __always_inline bool
flow_registered_weights_are_equal(const struct flow_v2_session_config *cfg)
{
	struct flow_v2_control_state *control;
	__u32 zero = 0;

	if (!cfg || adaptive_session_weight)
		return false;
	control = bpf_map_lookup_elem(&control_state, &zero);
	if (!control || !control->registered_sessions)
		return false;
	return control->registered_weights_equal && control->common_weight &&
		control->common_weight == (cfg->weight ?: 1);
}

static __always_inline bool
flow_periodic_fair_needed(const struct flow_v2_session_config *cfg)
{
	if (!periodic_fair_accounting)
		return false;
	/* 按当前需求分配的路线直接通过会话虚拟运行时间调整处理器时间比例；继续
	 * 进入周期配额路径只会重复限制同一比例，并显著增加校验复杂度。 */
	if (demand_aware_borrow)
		return false;
	/* 动态权重可能临时不同，必须继续按实际处理器时间核算。 */
	if (!equal_weight_fair_fastpath || adaptive_session_weight)
		return true;
	return !flow_registered_weights_are_equal(cfg);
}

static __always_inline bool
flow_has_high_cpu_pressure(const struct flow_v2_width_state *state)
{
	__u64 prospective_running;

	if (!state || !managed_cpu_count)
		return false;
	if (managed_cpu_count == 1)
		return true;
	/* dispatch 当前所在处理器即将运行一个任务，计入判断。 */
	prospective_running = flow_effective_running_tasks(state) + 1;
	return prospective_running * 4 >= (__u64)managed_cpu_count * 3;
}

static __always_inline bool flow_relation_decision(void)
{
	return decision_mode == FLOW_V2_DECISION_RELATION;
}

static __always_inline bool flow_stage_decision(void)
{
	return decision_mode == FLOW_V2_DECISION_STAGE;
}

static __always_inline bool flow_tool_decision(void)
{
	return decision_mode == FLOW_V2_DECISION_TOOL;
}

static __always_inline bool flow_kernel_decision(void)
{
	return decision_mode == FLOW_V2_DECISION_KERNEL;
}

static __always_inline bool
flow_manual_tool_selected(__u64 session_id, __u64 tool_id)
{
	struct flow_v2_tool_config *tool;
	struct flow_v2_tool_key key = {
		.session_id = session_id,
		.tool_id = tool_id,
	};

	if (decision_mode != FLOW_V2_DECISION_MANUAL || !session_id || !tool_id)
		return false;
	tool = bpf_map_lookup_elem(&tool_config, &key);
	return tool && tool->active &&
		(tool->flags & FLOW_V2_TOOL_MANUAL_PROGRESS);
}

static __always_inline void flow_record_map_update_failure(void)
{
	struct flow_v2_global_stats *global = flow_global();

	if (global)
		__sync_fetch_and_add(&global->map_update_failures, 1);
}

static __always_inline struct flow_v2_session_stats *
flow_session_stats(__u64 session_id)
{
	struct flow_v2_session_stats *zero;
	struct flow_v2_session_stats *stats;
	__u32 zero_key = 0;

	if (!session_id)
		return NULL;
	stats = bpf_map_lookup_elem(&session_stats, &session_id);
	if (!stats) {
		zero = bpf_map_lookup_elem(&session_stats_zero, &zero_key);
		if (!zero)
			return NULL;
		if (bpf_map_update_elem(&session_stats, &session_id, zero,
					BPF_NOEXIST))
			flow_record_map_update_failure();
		stats = bpf_map_lookup_elem(&session_stats, &session_id);
	}
	return stats;
}

static __always_inline struct flow_v2_session_runtime *
flow_session_runtime(__u64 session_id)
{
	struct flow_v2_session_runtime *runtime;

	if (!session_id)
		return NULL;
	runtime = bpf_map_lookup_elem(&session_runtime, &session_id);
	if (!runtime)
		flow_record_map_update_failure();
	return runtime;
}

static __always_inline bool
flow_config_enabled(const struct flow_v2_session_config *cfg)
{
	__u64 now;

	if (!cfg || !(cfg->flags & FLOW_V2_SESSION_ENABLED))
		return false;
	if (!(cfg->flags & FLOW_V2_SESSION_LEASED))
		return true;
	now = bpf_ktime_get_ns();
	return cfg->heartbeat_ns && now >= cfg->heartbeat_ns &&
		now - cfg->heartbeat_ns <= FLOW_V2_HEARTBEAT_TIMEOUT_NS;
}

static __always_inline bool flow_session_enabled(__u64 cgid,
				 struct flow_v2_session_config **cfgp)
{
	struct flow_v2_session_config *cfg =
		bpf_map_lookup_elem(&session_config, &cgid);

	if (!flow_config_enabled(cfg))
		return false;
	if (cfgp)
		*cfgp = cfg;
	return true;
}

static __always_inline void flow_mark_policy_task(__u32 pid)
{
	__u8 one = 1;

	if (pid && bpf_map_update_elem(&flow_v2_policy_tasks, &pid, &one,
					BPF_ANY))
		flow_record_map_update_failure();
}

static __always_inline void flow_clear_policy_task(__u32 pid)
{
	if (pid)
		bpf_map_delete_elem(&flow_v2_policy_tasks, &pid);
}

static __always_inline void flow_read_comm(struct task_struct *p,
					   char comm[FLOW_V2_COMM_LEN])
{
	bpf_probe_read_kernel_str(comm, FLOW_V2_COMM_LEN, p->comm);
}

static __always_inline struct flow_v2_tool_runtime *
flow_tool_runtime_lookup(__u64 session_id, __u64 tool_id)
{
	struct flow_v2_tool_key key = {
		.session_id = session_id,
		.tool_id = tool_id,
	};

	if (!session_id || !tool_id)
		return NULL;
	return bpf_map_lookup_elem(&tool_runtime, &key);
}

static __always_inline void
flow_update_tool_peak(struct flow_v2_tool_runtime *runtime, __u32 live)
{
	__u32 observed;

	if (!runtime)
		return;
	observed = runtime->peak_live_tasks;
	bpf_repeat(4) {
		if (live <= observed)
			break;
		observed = __sync_val_compare_and_swap(&runtime->peak_live_tasks,
						       observed, live);
	}
}

static __always_inline void
flow_update_runnable_peak(struct flow_v2_tool_runtime *runtime, __u32 runnable)
{
	__u32 observed;

	if (!runtime)
		return;
	observed = runtime->peak_runnable_tasks;
	bpf_repeat(4) {
		if (runnable <= observed)
			break;
		observed = __sync_val_compare_and_swap(
			&runtime->peak_runnable_tasks, observed, runnable);
	}
}

static __always_inline void
flow_update_wait_target_peak(struct flow_v2_tool_runtime *runtime, __u32 active)
{
	__u32 observed;

	if (!runtime)
		return;
	observed = runtime->peak_wait_targets;
	bpf_repeat(4) {
		if (active <= observed)
			break;
		observed = __sync_val_compare_and_swap(
			&runtime->peak_wait_targets, observed, active);
	}
}

static __always_inline void
flow_update_active_waiter_peak(struct flow_v2_tool_runtime *runtime, __u32 active)
{
	__u32 observed;

	if (!runtime)
		return;
	observed = runtime->peak_active_waiters;
	bpf_repeat(4) {
		if (active <= observed)
			break;
		observed = __sync_val_compare_and_swap(
			&runtime->peak_active_waiters, observed, active);
	}
}

static __always_inline void flow_update_peak_u64(__u64 *value, __u64 candidate)
{
	__u64 observed;

	if (!value)
		return;
	observed = *value;
	bpf_repeat(4) {
		if (candidate <= observed)
			break;
		observed = __sync_val_compare_and_swap(value, observed, candidate);
	}
}

static __always_inline __u32 flow_dec_and_return_old(__u32 *value)
{
	__u32 observed;

	if (!value)
		return 0;
	observed = *value;
	bpf_repeat(4) {
		__u32 previous;

		if (!observed)
			break;
		previous = observed;
		observed = __sync_val_compare_and_swap(value, observed,
						       observed - 1);
		if (observed == previous)
			return previous;
	}
	return 0;
}

static __always_inline void flow_dec_if_positive(__u32 *value)
{
	flow_dec_and_return_old(value);
}

static __always_inline void
flow_change_runnable_demand(__u32 previous, bool add)
{
	struct flow_v2_width_state *state;
	__u32 old_score;
	__u32 new_score;
	__u32 zero = 0;

	state = bpf_map_lookup_elem(&width_state, &zero);
	if (!state)
		return;
	old_score = previous ? 1 + log2_u32(previous) : 0;
	if (add) {
		new_score = 1 + log2_u32(previous + 1);
		__sync_fetch_and_add(&state->runnable_demand, 1);
		if (new_score > old_score)
			__sync_fetch_and_add(&state->runnable_demand_score,
					     new_score - old_score);
		if (!previous)
			__sync_fetch_and_add(&state->runnable_sessions, 1);
	} else if (previous) {
		new_score = previous > 1 ? 1 + log2_u32(previous - 1) : 0;
		flow_dec_if_positive(&state->runnable_demand);
		if (old_score > new_score)
			flow_dec_if_positive(&state->runnable_demand_score);
		if (previous == 1)
			flow_dec_if_positive(&state->runnable_sessions);
	}
}

static __always_inline void flow_change_running_count(bool add)
{
	struct flow_v2_width_state *state;
	__u32 zero = 0;

	state = bpf_map_lookup_elem(&width_state, &zero);
	if (!state)
		return;
	if (add)
		__sync_fetch_and_add(&state->running_tasks, 1);
	else
		flow_dec_if_positive(&state->running_tasks);
}

static __always_inline __u32
flow_adaptive_weight_value(const struct flow_v2_session_config *cfg,
			   const struct flow_v2_session_adaptive_state *adaptive)
{
	__u64 base = cfg && cfg->weight ? cfg->weight : 100;

	if (adaptive_session_weight && adaptive && adaptive->effective_weight)
		base = adaptive->effective_weight;
	if (base > 10000)
		base = 10000;
	return (__u32)base;
}

static __always_inline void flow_adjust_runnable_weight(__s64 delta)
{
	struct flow_v2_width_state *state;
	__u32 zero = 0;

	if (!delta)
		return;
	state = bpf_map_lookup_elem(&width_state, &zero);
	if (!state)
		return;
	if (delta > 0) {
		__sync_fetch_and_add(&state->runnable_weight, (__u64)delta);
	} else {
		__u64 amount = (__u64)-delta;
		__u64 observed = state->runnable_weight;

		bpf_repeat(4) {
			__u64 next = observed > amount ? observed - amount : 0;
			__u64 previous = observed;

			observed = __sync_val_compare_and_swap(
				&state->runnable_weight, observed, next);
			if (observed == previous)
				break;
		}
	}
	__sync_fetch_and_add(&state->runnable_epoch, 1);
}

static __always_inline void flow_adjust_adaptive_weight(__s64 delta)
{
	struct flow_v2_width_state *state;
	__u32 zero = 0;

	if (!delta)
		return;
	state = bpf_map_lookup_elem(&width_state, &zero);
	if (!state)
		return;
	/* 加法在并发升权和降权时可交换；使用独立的有符号字段，避免
	 * 降权先于升权记账时让基础权重发生无符号下溢。 */
	__sync_fetch_and_add(&state->adaptive_weight_delta, delta);
	__sync_fetch_and_add(&state->runnable_epoch, 1);
}

static __attribute__((noinline)) bool
flow_set_adaptive_weight(const struct flow_v2_session_config *cfg,
			 struct flow_v2_session_adaptive_state *adaptive,
			 __u32 desired, __u64 now)
{
	struct flow_v2_session_stats *stats;
	__u64 active_ns = 0;
	__u64 base;
	__u32 desired_active;
	__u32 old_active;
	__u64 started;
	__u32 changed_from = 0;
	__u32 changed_to = 0;
	__u32 observed;
	__u32 raw;

	if (!adaptive_session_weight || !cfg ||
	    !(cfg->flags & FLOW_V2_SESSION_ENABLED) || !adaptive)
		return false;
	base = cfg->weight ?: 100;
	if (!desired)
		desired = (__u32)base;
	if (desired > 10000)
		desired = 10000;
	bpf_repeat(4) {
		raw = adaptive->effective_weight;
		observed = raw;
		if (!observed)
			observed = (__u32)base;
		if (observed == desired)
			break;
		if (__sync_val_compare_and_swap(&adaptive->effective_weight,
				raw, (__u32)desired) == raw) {
			changed_from = observed;
			changed_to = (__u32)desired;
			break;
		}
	}
	if (!changed_to)
		return false;
	if (changed_to > changed_from)
		flow_adjust_adaptive_weight((__s64)(changed_to - changed_from));
	else
		flow_adjust_adaptive_weight(-(__s64)(changed_from - changed_to));

	desired_active = changed_to > base;
	old_active = changed_from > base;
	adaptive->adaptive_weight_active = adaptive->effective_weight > base;
	stats = flow_session_stats(cfg->session_id);
	if (stats) {
		__sync_fetch_and_add(&stats->adaptive_weight_changes, 1);
		flow_update_peak_u64(&stats->adaptive_weight_peak, changed_to);
	}
	if (old_active == desired_active)
		return false;
	if (desired_active) {
		if (adaptive->effective_weight == changed_to)
			__sync_val_compare_and_swap(
				&adaptive->adaptive_weight_started_ns, 0, now);
	} else {
		started = adaptive->adaptive_weight_started_ns;
		if (started && adaptive->effective_weight <= base &&
		    __sync_val_compare_and_swap(
			    &adaptive->adaptive_weight_started_ns, started, 0) ==
		    started && now > started)
			active_ns = now - started;
	}
	if (!stats)
		return old_active && !desired_active;
	if (desired_active)
		__sync_fetch_and_add(&stats->adaptive_weight_activations, 1);
	else {
		__sync_fetch_and_add(&stats->adaptive_weight_deactivations, 1);
		__sync_fetch_and_add(&stats->adaptive_weight_active_ns, active_ns);
		if (automatic_session_weight)
			flow_update_peak_u64(
				&stats->automatic_weight_max_active_ns, active_ns);
	}
	return old_active && !desired_active;
}

static __always_inline bool
flow_automatic_weight_candidate(
	const struct flow_v2_session_adaptive_state *adaptive)
{
	bool runnable_pressure;

	runnable_pressure = adaptive &&
		(adaptive->runnable_wait_targets ||
		 (sustained_tool_weight &&
		  adaptive->sustained_descendant_pressure));
	return adaptive && adaptive->active_blocking_waiters &&
		runnable_pressure && adaptive->wait_pressure_seen &&
		adaptive->wait_pressure_generation &&
		adaptive->granted_wait_generation !=
			adaptive->wait_pressure_generation;
}

static __always_inline __u32
flow_automatic_desired_weight(
	const struct flow_v2_session_config *cfg,
	const struct flow_v2_session_adaptive_state *adaptive)
{
	__u64 base = cfg && cfg->weight ? cfg->weight : 100;
	__u64 multiplier = 2;

	if (sustained_tool_weight && adaptive &&
	    adaptive->sustained_descendant_pressure &&
	    sustained_tool_weight_multiplier > multiplier)
		multiplier = sustained_tool_weight_multiplier;
	if (multiplier > 8)
		multiplier = 8;
	return (__u32)(base * multiplier);
}

/*
 * 自动权重不能仅凭“有人在等”就从其他工作流取得额外处理器时间。
 * 这里读取当前公平核算周期内已经取得和已经预留的处理器时间；选择
 * 会话时只允许此前获得时间较少的候选者补到当前最高值。读取允许有
 * 很短的时序误差，最终补偿量仍由下面的独立计数严格限制。
 */
/* 独立子程序可以避免把带锁读取展开到 64 次有界遍历中，降低内核
 * 验证程序需要探索的分支数量。 */
static __attribute__((noinline)) __u64
flow_automatic_recent_service(__u64 session_id, __u64 window_id,
			      __u64 runnable_epoch)
{
	struct flow_v2_fair_runtime *runtime;
	__u64 service = 0;

	runtime = bpf_map_lookup_elem(&session_fair_runtime, &session_id);
	if (!runtime)
		return 0;
	bpf_spin_lock(&runtime->lock);
	if (runtime->window_id == window_id &&
	    runtime->runnable_epoch == runnable_epoch)
		service = runtime->used_ns + runtime->reserved_ns;
	bpf_spin_unlock(&runtime->lock);
	return service;
}

/*
 * 更新当前会话的权重。跟踪等待关系的程序也会调用本函数，因此这里
 * 不扫描其他会话；跨会话选择只在 sched_ext 的权重读取路径中执行。
 */
static __attribute__((noinline)) void
flow_update_adaptive_weight(const struct flow_v2_session_config *cfg,
			    struct flow_v2_session_adaptive_state *adaptive)
{
	struct flow_v2_automatic_weight_state *automatic;
	struct flow_v2_session_stats *stats;
	__u64 now = bpf_ktime_get_ns();
	__u64 base;
	__u64 desired;
	__u32 zero = 0;
	bool eligible;

	if (!adaptive_session_weight || !cfg ||
	    !(cfg->flags & FLOW_V2_SESSION_ENABLED) || !adaptive)
		return;
	base = cfg->weight ?: 100;
	eligible = adaptive->active_blocking_waiters &&
		adaptive->wait_pressure_seen;
	if (!automatic_session_weight) {
		desired = base * (eligible ? 2 : 1);
		flow_set_adaptive_weight(cfg, adaptive, (__u32)desired, now);
		return;
	}

	automatic = bpf_map_lookup_elem(&automatic_weight_state, &zero);
	if (!automatic)
		return;
	if (automatic->owner_session_id == cfg->session_id) {
		__u64 service_received = adaptive->automatic_weight_service_ns;
		bool timed_out = adaptive->automatic_weight_deadline_ns &&
			now >= adaptive->automatic_weight_deadline_ns;
		bool condition_ended = !adaptive->active_blocking_waiters ||
			(!adaptive->runnable_wait_targets &&
			 (!sustained_tool_weight ||
			  !adaptive->sustained_descendant_pressure)) ||
			!adaptive->wait_pressure_seen;
		bool service_repaid = adaptive->automatic_weight_service_limit_ns &&
			service_received >=
				adaptive->automatic_weight_service_limit_ns;

		if (timed_out || condition_ended || service_repaid) {
			/* 先恢复本会话，再公开“当前无人使用额外权重”，避免
			 * 新会话接替时短暂出现两个双倍权重会话。 */
			flow_set_adaptive_weight(cfg, adaptive, (__u32)base, now);
			stats = flow_session_stats(cfg->session_id);
			if (__sync_val_compare_and_swap(
				    &automatic->owner_session_id,
				    cfg->session_id, 0) == cfg->session_id) {
				__sync_fetch_and_add(&automatic->candidate_epoch, 1);
				if (stats) {
					if (service_repaid)
						__sync_fetch_and_add(
							&stats->automatic_weight_service_releases,
							1);
					else if (timed_out)
						__sync_fetch_and_add(
							&stats->automatic_weight_timeouts,
							1);
					else
						__sync_fetch_and_add(
							&stats->automatic_weight_condition_releases,
							1);
					__sync_fetch_and_add(
						&stats->automatic_weight_service_ns,
						service_received);
				}
			}
			adaptive->automatic_weight_deadline_ns = 0;
			adaptive->automatic_weight_service_limit_ns = 0;
			adaptive->automatic_weight_service_ns = 0;
			if (automatic_weight_repeat_wait &&
			    (timed_out || (sustained_tool_weight && service_repaid)) &&
			    !condition_ended) {
				/* 到期后先回到基础权重。旧的排队事实已经消费，
				 * 恢复期结束后必须观察到新的排队才能再次选择。 */
				__sync_lock_test_and_set(
					&adaptive->wait_pressure_seen, 0);
				adaptive->sustained_descendant_pressure = 0;
				adaptive->automatic_weight_cooldown_deadline_ns =
					now + (automatic_weight_window_ns ?:
					       100ULL * 1000 * 1000);
			} else if (condition_ended) {
				adaptive->automatic_weight_cooldown_deadline_ns = 0;
			}
			return;
		}
		flow_set_adaptive_weight(cfg, adaptive,
			flow_automatic_desired_weight(cfg, adaptive), now);
		return;
	}
	/* 未被选中的会话始终使用基础权重。 */
	if (flow_set_adaptive_weight(cfg, adaptive, (__u32)base, now)) {
		stats = flow_session_stats(cfg->session_id);
		if (stats)
			__sync_fetch_and_add(
				&stats->automatic_weight_owner_change_releases, 1);
	}
}

struct flow_v2_automatic_select_ctx {
	__u64 now;
	__u64 runnable_epoch;
	__u64 best_order;
	__u64 best_session;
	__u64 best_cgid;
	__u64 best_generation;
	__u64 best_service;
	__u64 max_service;
	__u64 best_base_weight;
	__u32 best_demand;
	__u32 active_sessions;
	bool best_sustained;
};

/* 使用 bpf_loop 的回调逐项核算。内核只需验证一次回调，而不是把
 * 带锁的处理器时间读取展开到 64 次遍历中。 */
static long flow_automatic_select_cb(__u32 slot, void *data)
{
	struct flow_v2_automatic_select_ctx *ctx = data;
	struct flow_v2_session_adaptive_state *adaptive;
	struct flow_v2_session_config *cfg;
	__u64 candidate_order;
	__u64 candidate_period;
	__u64 normalized_service;
	__u64 base_weight;
	__u64 service;
	__u64 *cgid;
	__u32 candidate_demand;
	bool candidate_sustained;

	cgid = bpf_map_lookup_elem(&active_session_slots, &slot);
	if (!cgid || !*cgid)
		return 0;
	cfg = bpf_map_lookup_elem(&session_config, cgid);
	if (!cfg || !(cfg->flags & FLOW_V2_SESSION_ENABLED))
		return 0;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (!adaptive || !adaptive->runnable_tasks)
		return 0;
	ctx->active_sessions++;
	candidate_period = cfg->policy_window_ns ?: 100ULL * 1000 * 1000;
	base_weight = cfg->weight ?: 100;
	service = flow_automatic_recent_service(
		cfg->session_id, ctx->now / candidate_period,
		ctx->runnable_epoch);
	normalized_service = service * 100 / base_weight;
	if (normalized_service > ctx->max_service)
		ctx->max_service = normalized_service;
	if (!flow_automatic_weight_candidate(adaptive))
		return 0;
	candidate_sustained = sustained_tool_weight &&
		adaptive->sustained_descendant_pressure;
	candidate_demand = adaptive->runnable_tasks;
	candidate_order = adaptive->last_grant_order;
	if (!ctx->best_session ||
	    (candidate_sustained && !ctx->best_sustained) ||
	    (candidate_sustained == ctx->best_sustained &&
	     ((candidate_sustained &&
	       (candidate_demand > ctx->best_demand ||
	        (candidate_demand == ctx->best_demand &&
	         (normalized_service < ctx->best_service ||
	          (normalized_service == ctx->best_service &&
	           (candidate_order < ctx->best_order ||
	            (candidate_order == ctx->best_order &&
	             cfg->session_id < ctx->best_session))))))) ||
	      (!candidate_sustained &&
	       (normalized_service < ctx->best_service ||
	        (normalized_service == ctx->best_service &&
	         (candidate_order < ctx->best_order ||
	          (candidate_order == ctx->best_order &&
	           cfg->session_id < ctx->best_session)))))))) {
		ctx->best_order = candidate_order;
		ctx->best_session = cfg->session_id;
		ctx->best_cgid = *cgid;
		ctx->best_generation = adaptive->wait_pressure_generation;
		ctx->best_service = normalized_service;
		ctx->best_base_weight = base_weight;
		ctx->best_demand = candidate_demand;
		ctx->best_sustained = candidate_sustained;
	}
	return 0;
}

/*
 * 当前没有会话获得额外处理器时间时，扫描最多 64 个活动会话。
 * 只从真实等待对象正在排队、而且当前公平核算周期内少得到处理器
 * 时间的会话中选择；差距相同时才按历史顺序轮换。增加的权重只用
 * 来补回已经观察到的差距，不会凭等待关系长期扩大处理器占比。
 */
static __attribute__((noinline)) void flow_automatic_select_next(__u64 now)
{
	struct flow_v2_automatic_weight_state *automatic;
	struct flow_v2_session_adaptive_state *adaptive;
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;
	struct flow_v2_automatic_select_ctx ctx = {
		.now = now,
		.best_order = ~0ULL,
		.best_base_weight = 100,
	};
	__u64 service_limit;
	__u64 previous_owner;
	__u64 order;
	__u64 candidate_epoch;
	__u64 scanned_epoch;
	__u32 zero = 0;
	struct flow_v2_width_state *width;

	if (!automatic_session_weight)
		return;
	automatic = bpf_map_lookup_elem(&automatic_weight_state, &zero);
	if (!automatic)
		return;
	if (automatic->owner_session_id) {
		__u64 owner = automatic->owner_session_id;
		__u64 owner_cgid = automatic->owner_cgroup_id;

		/* 任意会话进入权重读取路径时都可推动当前拥有者到期恢复；
		 * 因此拥有者暂时没有被派发，也不会无限延长作用时间。 */
		if (!owner_cgid)
			return;
		cfg = bpf_map_lookup_elem(&session_config, &owner_cgid);
		adaptive = cfg ? bpf_map_lookup_elem(&session_adaptive_state,
						     &cfg->session_id) : NULL;
		if (automatic->owner_session_id != owner)
			return;
		if (!cfg || cfg->session_id != owner || !adaptive) {
			if (__sync_val_compare_and_swap(
				    &automatic->owner_session_id, owner, 0) == owner)
				__sync_fetch_and_add(&automatic->candidate_epoch, 1);
		} else {
			flow_update_adaptive_weight(cfg, adaptive);
		}
		if (automatic->owner_session_id)
			return;
	}
	candidate_epoch = automatic->candidate_epoch;
	scanned_epoch = automatic->last_scanned_epoch;
	if (scanned_epoch == candidate_epoch ||
	    __sync_val_compare_and_swap(&automatic->last_scanned_epoch,
					scanned_epoch, candidate_epoch) !=
				scanned_epoch)
		return;
	width = bpf_map_lookup_elem(&width_state, &zero);
	if (!width)
		return;
	ctx.runnable_epoch = flow_effective_runnable_epoch(width);
	bpf_loop(FLOW_V2_ACTIVE_SESSION_SLOTS, flow_automatic_select_cb, &ctx, 0);
	/* 没有会话竞争，或候选会话并未少得到至少一个会话时间片时，
	 * 普通公平调度已经足够，不改变权重。 */
	if (!ctx.best_session || ctx.active_sessions < 2)
		return;
	if (ctx.best_sustained) {
		/* 不预测工作流以后会创建多少进程。只有工具调用者已经等待、
		 * 其后代持续运行并实际排队时，才按当前可运行进程数选择
		 * 一次；增加的处理器时间只持续一个正常会话时间片。 */
		service_limit = cgrp_slice_ns ?: 5ULL * 1000 * 1000;
	} else {
		if (ctx.max_service <= ctx.best_service ||
		    ctx.max_service - ctx.best_service <
			cgrp_slice_ns * 100 / ctx.best_base_weight)
			return;
		service_limit = (ctx.max_service - ctx.best_service) *
			ctx.best_base_weight / 100;
	}
	if (!service_limit)
		return;
	if (__sync_val_compare_and_swap(&automatic->owner_session_id, 0,
					ctx.best_session) != 0)
		return;
	automatic->owner_cgroup_id = ctx.best_cgid;

	/* 选择后重新核对状态，防止扫描期间等待已经结束。 */
	cfg = bpf_map_lookup_elem(&session_config, &ctx.best_cgid);
	adaptive = cfg ? bpf_map_lookup_elem(&session_adaptive_state,
					     &cfg->session_id) : NULL;
	if (!cfg || cfg->session_id != ctx.best_session ||
	    !flow_automatic_weight_candidate(adaptive) ||
	    adaptive->wait_pressure_generation != ctx.best_generation) {
		if (adaptive) {
			adaptive->automatic_weight_service_limit_ns = 0;
			adaptive->automatic_weight_service_ns = 0;
		}
		__sync_val_compare_and_swap(&automatic->owner_session_id,
					    ctx.best_session, 0);
		__sync_fetch_and_add(&automatic->candidate_epoch, 1);
		return;
	}
	adaptive->granted_wait_generation = ctx.best_generation;
	adaptive->automatic_weight_deadline_ns = now +
		(automatic_weight_window_ns ?: 100ULL * 1000 * 1000);
	adaptive->automatic_weight_cooldown_deadline_ns = 0;
	adaptive->automatic_weight_service_limit_ns = service_limit;
	adaptive->automatic_weight_service_ns = 0;
	order = __sync_add_and_fetch(&automatic->grant_sequence, 1);
	if (!order)
		order = __sync_add_and_fetch(&automatic->grant_sequence, 1);
	adaptive->last_grant_order = order;
	previous_owner = __sync_lock_test_and_set(
		&automatic->last_owner_session_id, ctx.best_session);
	stats = flow_session_stats(ctx.best_session);
	if (stats) {
		__sync_fetch_and_add(&stats->automatic_weight_planned_ns,
				     service_limit);
		if (ctx.best_sustained)
			__sync_fetch_and_add(
				&stats->sustained_pressure_activations, 1);
		if (previous_owner && previous_owner != ctx.best_session)
			__sync_fetch_and_add(&stats->automatic_weight_handoffs, 1);
	}
	flow_set_adaptive_weight(cfg, adaptive,
		flow_automatic_desired_weight(cfg, adaptive), now);
	/* 等待可能在最后一次核对后结束；这种竞争由事件路径立即恢复。 */
	if (automatic->owner_session_id != ctx.best_session &&
	    flow_set_adaptive_weight(cfg, adaptive,
		(__u32)(cfg->weight ?: 100), bpf_ktime_get_ns()) && stats)
		__sync_fetch_and_add(
			&stats->automatic_weight_owner_change_releases, 1);
}

/* 会话的可运行、已派发和正在运行计数会被多个处理器同时修改。
 * 有限次数的无锁重试在高并发下可能放弃一次减计数，使峰值超过
 * 实际处理器数并逐步破坏并行度判断；这些低频计数统一使用会话锁。 */
static __always_inline __u32
flow_session_inc_runnable(struct flow_v2_session_runtime *runtime,
			  const struct flow_v2_session_config *cfg)
{
	__u32 old;

	if (!runtime)
		return 0;
	bpf_spin_lock(&runtime->lock);
	old = runtime->runnable_tasks++;
	bpf_spin_unlock(&runtime->lock);
	flow_change_runnable_demand(old, true);
	if (!old)
		flow_adjust_runnable_weight((__s64)(cfg && cfg->weight ?
						 cfg->weight : 100));
	if (adaptive_session_weight && cfg) {
		struct flow_v2_session_adaptive_state *adaptive =
			bpf_map_lookup_elem(&session_adaptive_state,
					    &cfg->session_id);

		if (adaptive)
			__sync_fetch_and_add(&adaptive->runnable_tasks, 1);
		flow_update_adaptive_weight(cfg, adaptive);
	}
	return old;
}

static __always_inline __u32
flow_session_dec_runnable(struct flow_v2_session_runtime *runtime,
			  const struct flow_v2_session_config *cfg)
{
	__u32 old;

	if (!runtime)
		return 0;
	bpf_spin_lock(&runtime->lock);
	old = runtime->runnable_tasks;
	if (runtime->runnable_tasks)
		runtime->runnable_tasks--;
	bpf_spin_unlock(&runtime->lock);
	flow_change_runnable_demand(old, false);
	if (adaptive_session_weight && cfg) {
		struct flow_v2_session_adaptive_state *adaptive =
			bpf_map_lookup_elem(&session_adaptive_state,
					    &cfg->session_id);

		if (adaptive)
			flow_dec_if_positive(&adaptive->runnable_tasks);
		flow_update_adaptive_weight(cfg, adaptive);
	}
	if (old == 1)
		flow_adjust_runnable_weight(-(__s64)(cfg && cfg->weight ?
						  cfg->weight : 100));
	return old;
}

static __always_inline void
flow_session_dec_running(struct flow_v2_session_runtime *runtime)
{
	bool changed = false;

	if (!runtime)
		return;
	bpf_spin_lock(&runtime->lock);
	if (runtime->running_tasks) {
		runtime->running_tasks--;
		changed = true;
	}
	bpf_spin_unlock(&runtime->lock);
	if (changed)
		flow_change_running_count(false);
}

static __always_inline void
flow_session_dec_reserved(struct flow_v2_session_runtime *runtime)
{
	if (!runtime)
		return;
	bpf_spin_lock(&runtime->lock);
	if (runtime->reserved_tasks)
		runtime->reserved_tasks--;
	bpf_spin_unlock(&runtime->lock);
}

static __always_inline void
flow_session_change_blocking_waiters(const struct flow_v2_session_config *cfg,
				     bool add)
{
	struct flow_v2_session_adaptive_state *adaptive;

	if (!cfg)
		return;
	/* 等待关系由跟踪程序确认；这类程序不能使用 BPF 自旋锁。
	 * 这里只原子更新事实计数，调度回调在下一次分配处理器前统一
	 * 重新核算权重。 */
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (!adaptive)
		return;
	if (add) {
		__sync_fetch_and_add(&adaptive->active_blocking_waiters, 1);
	} else {
		if (automatic_session_weight && adaptive->adaptive_weight_active) {
			struct flow_v2_session_stats *stats =
				flow_session_stats(cfg->session_id);

			if (stats)
				__sync_fetch_and_add(
					&stats->automatic_weight_wait_returns, 1);
		}
		/* 一个工具入口结束本轮等待后先恢复基础权重。若同一会话
		 * 还有另一轮等待，它的等待对象会在后续运行时重新触发。 */
		__sync_lock_test_and_set(&adaptive->wait_pressure_seen, 0);
		adaptive->sustained_descendant_pressure = 0;
		adaptive->automatic_weight_cooldown_deadline_ns = 0;
		flow_dec_if_positive(&adaptive->active_blocking_waiters);
	}
	flow_update_adaptive_weight(cfg, adaptive);
}

static __always_inline bool
flow_waiter_is_synchronous(const struct flow_v2_task_meta *waiter)
{
	struct flow_v2_tool_config *tool;
	struct flow_v2_tool_key key;

	if (!waiter || !waiter->blocking_tool_id)
		return false;
	key.session_id = waiter->session_id;
	key.tool_id = waiter->blocking_tool_id;
	tool = bpf_map_lookup_elem(&tool_config, &key);
	return tool && tool->active &&
		tool->generation == waiter->blocking_sequence &&
		tool->role != FLOW_V2_ROLE_BACKGROUND;
}

static __always_inline void
flow_session_mark_wait_pressure(const struct flow_v2_task_meta *target,
				const struct flow_v2_session_config *cfg)
{
	struct flow_v2_session_adaptive_state *adaptive;
	struct flow_v2_task_meta *waiter;

	if (!adaptive_session_weight || !target ||
	    !target->waiter_pid || !cfg)
		return;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (!adaptive || !adaptive->active_blocking_waiters)
		return;
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &target->waiter_pid);
	if (!waiter || !(waiter->flags & FLOW_V2_TASK_WAIT_BLOCKED) ||
	    waiter->session_id != target->session_id ||
	    (!automatic_session_weight && !flow_waiter_is_synchronous(waiter)))
		return;
	if (automatic_weight_repeat_wait &&
	    adaptive->automatic_weight_cooldown_deadline_ns &&
	    bpf_ktime_get_ns() < adaptive->automatic_weight_cooldown_deadline_ns)
		return;
	if (__sync_val_compare_and_swap(&adaptive->wait_pressure_seen, 0, 1))
		return;
	/* 每次阻塞等待只生成一个编号；同一等待对象反复入队不会再次
	 * 获得额外处理器时间。下一轮真实阻塞等待会生成新编号。 */
	if (!__sync_add_and_fetch(&adaptive->wait_pressure_generation, 1))
		__sync_fetch_and_add(&adaptive->wait_pressure_generation, 1);
	if (automatic_session_weight) {
		struct flow_v2_automatic_weight_state *automatic;
		struct flow_v2_session_stats *stats;
		__u32 zero = 0;

		stats = flow_session_stats(cfg->session_id);
		if (stats)
			__sync_fetch_and_add(
				&stats->automatic_weight_pressure_waits, 1);
		automatic = bpf_map_lookup_elem(&automatic_weight_state, &zero);
		if (automatic)
			__sync_fetch_and_add(&automatic->candidate_epoch, 1);
	}
	flow_update_adaptive_weight(cfg, adaptive);
}

static __always_inline void
flow_session_change_runnable_wait_targets(
	const struct flow_v2_session_config *cfg, bool add)
{
	struct flow_v2_session_adaptive_state *adaptive;

	if (!cfg)
		return;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (!adaptive)
		return;
	if (add)
		__sync_fetch_and_add(&adaptive->runnable_wait_targets, 1);
	else
		flow_dec_if_positive(&adaptive->runnable_wait_targets);
	flow_update_adaptive_weight(cfg, adaptive);
}

static __always_inline __u32
flow_session_effective_weight(const struct flow_v2_session_config *cfg)
{
	struct flow_v2_session_adaptive_state *adaptive;

	if (!cfg)
		return 100;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	return flow_adaptive_weight_value(cfg, adaptive);
}

static __always_inline void
flow_set_wait_target_runnable(struct task_struct *p,
			      const struct flow_v2_task_meta *meta,
			      const struct flow_v2_session_config *cfg,
			      bool runnable)
{
	struct flow_v2_quota_task_state *task_state;
	bool should_count;

	if (!adaptive_session_weight || !p || !meta || !cfg)
		return;
	task_state = bpf_task_storage_get(&quota_task_state, p, 0, 0);
	if (!task_state)
		return;
	should_count = runnable && task_state->counted_runnable &&
		(meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
				FLOW_V2_TASK_PROGRESS_GROUP)) &&
		!(meta->flags & (FLOW_V2_TASK_RETURN_ROOT | FLOW_V2_TASK_EXITED));
	if (should_count == !!task_state->counted_wait_target_runnable)
		return;
	task_state->counted_wait_target_runnable = should_count;
	flow_session_change_runnable_wait_targets(cfg, should_count);
}

static __always_inline __u64 flow_task_start_ns(struct task_struct *p)
{
	__u64 start;

	if (!p)
		return 0;
	start = BPF_CORE_READ(p, start_boottime);
	return start ?: BPF_CORE_READ(p, start_time);
}

static __always_inline void
flow_account_tool_member(struct flow_v2_task_meta *meta)
{
	struct flow_v2_tool_runtime *runtime;
	__u32 old_flags;
	__u32 live;

	if (!meta || !(meta->flags & FLOW_V2_TASK_TOOL_MEMBER) ||
	    (meta->flags & FLOW_V2_TASK_LIVE_ACCOUNTED))
		return;
	runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
	if (!runtime || !runtime->active || runtime->sequence != meta->sequence)
		return;
	old_flags = __sync_fetch_and_or(&meta->flags,
					FLOW_V2_TASK_LIVE_ACCOUNTED);
	if (old_flags & FLOW_V2_TASK_LIVE_ACCOUNTED)
		return;
	live = __sync_add_and_fetch(&runtime->live_tasks, 1);
	flow_update_tool_peak(runtime, live);
}

static __always_inline void
flow_release_tool_member(struct flow_v2_task_meta *meta)
{
	struct flow_v2_tool_runtime *runtime;
	__u32 old_flags;

	if (!meta || !(meta->flags & FLOW_V2_TASK_LIVE_ACCOUNTED))
		return;
	old_flags = __sync_fetch_and_and(&meta->flags,
					 ~FLOW_V2_TASK_LIVE_ACCOUNTED);
	if (!(old_flags & FLOW_V2_TASK_LIVE_ACCOUNTED))
		return;
	runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
	if (runtime && runtime->sequence == meta->sequence)
		flow_dec_if_positive(&runtime->live_tasks);
}

/*
 * 进程号只是散列表索引，start_boottime 才是任务身份。用户态登记的
 * 工具根第一次进入内核路径时补齐身份；进程号复用时先释放旧计数。
 */
static __always_inline struct flow_v2_task_meta *
flow_lookup_task(struct task_struct *p)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *meta;
	__u64 start;
	__u32 pid;

	if (!p)
		return NULL;
	pid = p->pid;
	meta = bpf_map_lookup_elem(&flow_v2_task_meta, &pid);
	if (!meta)
		return NULL;
	start = flow_task_start_ns(p);
	if (meta->task_start_ns && start && meta->task_start_ns != start) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			stats->reused_pid_replacements++;
		flow_release_tool_member(meta);
		bpf_map_delete_elem(&flow_v2_waiting_parents, &pid);
		flow_clear_policy_task(pid);
		bpf_map_delete_elem(&flow_v2_task_meta, &pid);
		return NULL;
	}
	if (!meta->task_start_ns)
		meta->task_start_ns = start;
	flow_account_tool_member(meta);
	return meta;
}

static __always_inline void flow_emit_related(
	__u32 kind, struct task_struct *p,
	const struct flow_v2_task_meta *meta, __u32 related_pid)
{
	struct flow_v2_global_stats *global;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_event *event;

	event = bpf_ringbuf_reserve(&events, sizeof(*event), 0);
	if (!event) {
		global = flow_global();
		if (global)
			__sync_fetch_and_add(&global->dropped_events, 1);
		return;
	}

	__builtin_memset(event, 0, sizeof(*event));
	event->timestamp_ns = bpf_ktime_get_ns();
	event->kind = kind;
	event->pid = p ? p->pid : 0;
	event->related_pid = related_pid;
	if (meta) {
		event->session_id = meta->session_id;
		event->cgroup_id = meta->cgroup_id;
		event->tool_id = meta->tool_id;
		event->sequence = meta->sequence;
		/* 等待关系和返回提权事件归入当前阻塞工具，而非来源代次。 */
		if (meta->blocking_tool_id &&
		    (kind == FLOW_V2_EVENT_WAIT_LINK ||
		     (kind == FLOW_V2_EVENT_ROLE_CHANGE &&
		      meta->role_reason == FLOW_V2_REASON_RETURN_COHORT))) {
			event->tool_id = meta->blocking_tool_id;
			event->sequence = meta->blocking_sequence;
		}
		event->parent_pid = meta->parent_pid;
		event->role = meta->role;
		event->role_source = meta->role_source;
		event->role_reason = meta->role_reason;
		event->stage = meta->stage;
		event->behavior = meta->behavior;
		event->task_flags = meta->flags;
		runtime = flow_tool_runtime_lookup(meta->session_id, event->tool_id);
		if (runtime) {
			event->tool_live_tasks = runtime->live_tasks;
			event->tool_peak_live_tasks = runtime->peak_live_tasks;
			event->tool_active_wait_targets =
				runtime->active_wait_targets;
			event->tool_peak_wait_targets = runtime->peak_wait_targets;
		}
	}
	if (p)
		flow_read_comm(p, event->comm);
	bpf_ringbuf_submit(event, 0);
	global = flow_global();
	if (global)
		__sync_fetch_and_add(&global->emitted_events, 1);
}

static __always_inline void flow_emit(__u32 kind, struct task_struct *p,
				      const struct flow_v2_task_meta *meta)
{
	flow_emit_related(kind, p, meta, 0);
}

static __always_inline struct flow_v2_task_meta *
flow_create_policy_task(struct task_struct *p, __u32 pid, __u64 session_id,
			__u64 cgroup_id,
			__u64 tool_id, __u64 parent_tool_id, __u64 sequence,
			__u32 parent_pid, __u32 role, __u32 role_source,
			__u32 flags)
{
	struct flow_v2_task_meta *initial;
	__u32 zero = 0;

	if (!pid || !session_id || !tool_id)
		return NULL;
	initial = bpf_map_lookup_elem(&flow_v2_task_meta_scratch, &zero);
	if (!initial)
		return NULL;
	__builtin_memset(initial, 0, sizeof(*initial));
	initial->session_id = session_id;
	initial->cgroup_id = cgroup_id;
	initial->tool_id = tool_id;
	initial->parent_tool_id = parent_tool_id;
	initial->sequence = sequence;
	initial->parent_pid = parent_pid;
	initial->role = role;
	initial->role_source = role_source;
	initial->role_reason = FLOW_V2_REASON_NONE;
	initial->flags = flags;
	initial->birth_ns = bpf_ktime_get_ns();
	initial->task_start_ns = flow_task_start_ns(p);
	initial->last_cpu = -1;
	initial->last_role_change_ns = initial->birth_ns;
	if (bpf_map_update_elem(&flow_v2_task_meta, &pid, initial,
				BPF_NOEXIST)) {
		flow_record_map_update_failure();
		return NULL;
	}
	initial = p ? flow_lookup_task(p) :
		bpf_map_lookup_elem(&flow_v2_task_meta, &pid);
	if (initial) {
		struct flow_v2_session_stats *stats =
			flow_session_stats(initial->session_id);

		if (stats)
			stats->provenance_tasks++;
		flow_account_tool_member(initial);
	}
	return initial;
}

static __always_inline __u32 flow_behavior_from_stage(__u32 stage)
{
	if (stage == FLOW_V2_STAGE_BUILD || stage == FLOW_V2_STAGE_TEST ||
	    stage == FLOW_V2_STAGE_DATA || stage == FLOW_V2_STAGE_PACKAGE)
		return FLOW_V2_BEHAVIOR_PARALLEL;
	if (stage == FLOW_V2_STAGE_SERVICE)
		return FLOW_V2_BEHAVIOR_SERVICE;
	if (stage == FLOW_V2_STAGE_PREPARE || stage == FLOW_V2_STAGE_INSPECT ||
	    stage == FLOW_V2_STAGE_EDIT)
		return FLOW_V2_BEHAVIOR_SERIAL;
	return FLOW_V2_BEHAVIOR_UNKNOWN;
}

static __always_inline struct flow_v2_task_meta *
flow_lookup_or_create_task(struct task_struct *p, __u64 cgid,
				   const struct flow_v2_session_config *cfg)
{
	struct flow_v2_active_tool *active;
	struct flow_v2_task_meta *meta = NULL;
	__u32 pid = p->pid;

	meta = flow_lookup_task(p);
	if (meta || !cfg)
		return meta;
	active = bpf_map_lookup_elem(&active_tool_by_cgroup, &cgid);
	if (!active || !active->active || active->session_id != cfg->session_id)
		return NULL;
	return flow_create_policy_task(
		p, pid, cfg->session_id, cgid, active->tool_id,
		active->parent_tool_id, active->sequence,
		BPF_CORE_READ(p, real_parent, pid), FLOW_V2_ROLE_NORMAL,
		FLOW_V2_SOURCE_DEFAULT, 0);
}

static __always_inline void flow_refresh_tool_role(struct flow_v2_task_meta *meta)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_tool_config *tool;
	struct flow_v2_tool_key key;

	if (!meta || !meta->session_id || !meta->tool_id)
		return;
	/* 短交互角色只覆盖一次实际运行，不能被工具默认角色提前冲掉。 */
	if (meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE)
		return;
	key.session_id = meta->session_id;
	key.tool_id = meta->tool_id;
	tool = bpf_map_lookup_elem(&tool_config, &key);
	if (!tool || !tool->active || tool->generation != meta->sequence) {
		/*
		 * 工具返回只关闭这一代的新成员，不删除仍存活任务的
		 * 来源。明确登记为服务的存活任务继续作为后台任务运行；
		 * 其他旧代次回到普通路径，后续可以由跨工具唤醒关系重新
		 * 关联，而不会被误当作当前工具的等待对象。
		 */
		if (!(meta->flags & FLOW_V2_TASK_STALE_GENERATION)) {
			stats = flow_session_stats(meta->session_id);
			if (stats)
				stats->stale_generation_tasks++;
			meta->flags |= FLOW_V2_TASK_STALE_GENERATION;
			meta->role = tool &&
				tool->generation == meta->sequence &&
				tool->stage == FLOW_V2_STAGE_SERVICE ?
				FLOW_V2_ROLE_BACKGROUND : FLOW_V2_ROLE_NORMAL;
			meta->role_source = meta->role == FLOW_V2_ROLE_BACKGROUND ?
				FLOW_V2_SOURCE_TOOL : FLOW_V2_SOURCE_KERNEL;
			meta->role_reason = FLOW_V2_REASON_STALE_GENERATION;
			meta->flags &= ~FLOW_V2_TASK_TAIL_PROMOTED;
		}
		return;
	}
	if (!(meta->flags & FLOW_V2_TASK_SCOPE_ROOT))
		meta->flags |= FLOW_V2_TASK_TOOL_MEMBER;
	meta->flags &= ~FLOW_V2_TASK_STALE_GENERATION;
	if (tool->role < FLOW_V2_NR_ROLES) {
		meta->stage = tool->stage;
		meta->behavior = flow_behavior_from_stage(tool->stage);
		if (flow_stage_decision()) {
			if (meta->behavior == FLOW_V2_BEHAVIOR_SERVICE)
				meta->role = FLOW_V2_ROLE_BACKGROUND;
			else if (meta->behavior == FLOW_V2_BEHAVIOR_PARALLEL)
				meta->role = FLOW_V2_ROLE_PARALLEL;
			else
				meta->role = FLOW_V2_ROLE_NORMAL;
			meta->role_source = FLOW_V2_SOURCE_TOOL;
			meta->role_reason = FLOW_V2_REASON_TOOL_EXECUTION;
		} else if (flow_tool_decision()) {
			meta->role = tool->role == FLOW_V2_ROLE_BACKGROUND ?
				FLOW_V2_ROLE_BACKGROUND : FLOW_V2_ROLE_NORMAL;
			meta->behavior = FLOW_V2_BEHAVIOR_UNKNOWN;
			meta->role_source = FLOW_V2_SOURCE_TOOL;
			meta->role_reason = FLOW_V2_REASON_TOOL_EXECUTION;
		} else if (flow_kernel_decision()) {
			meta->role = FLOW_V2_ROLE_NORMAL;
			meta->behavior = FLOW_V2_BEHAVIOR_UNKNOWN;
			meta->role_source = FLOW_V2_SOURCE_KERNEL;
			meta->role_reason = FLOW_V2_REASON_NONE;
		} else if ((meta->flags & FLOW_V2_TASK_TAIL_PROMOTED) &&
		    tool->role == FLOW_V2_ROLE_PARALLEL) {
			meta->role = FLOW_V2_ROLE_PROGRESS;
			meta->role_source = FLOW_V2_SOURCE_KERNEL;
			meta->role_reason = FLOW_V2_REASON_RETURN_COHORT;
		} else {
			meta->role = tool->role;
			meta->role_source = FLOW_V2_SOURCE_TOOL;
			meta->role_reason = FLOW_V2_REASON_TOOL_EXECUTION;
		}
		meta->sequence = tool->generation;
	}
}

static __always_inline bool
flow_refresh_stale_delay(struct flow_v2_task_meta *meta,
			 const struct flow_v2_session_config *cfg)
{
	struct flow_v2_active_tool *active;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_session_stats *stats;
	bool should_delay = false;

	if ((!flow_relation_decision() &&
	     decision_mode != FLOW_V2_DECISION_MANUAL) ||
	    !background_enabled || !meta || !cfg || !meta->tool_id ||
	    !meta->sequence)
		return false;
	active = bpf_map_lookup_elem(&active_tool_by_cgroup, &meta->cgroup_id);
	if (decision_mode == FLOW_V2_DECISION_MANUAL &&
	    (!active || !flow_manual_tool_selected(active->session_id,
					       active->tool_id)))
		return false;
	if (active && active->active && active->session_id == meta->session_id &&
	    active->sequence > meta->sequence) {
		runtime = flow_tool_runtime_lookup(active->session_id,
						   active->tool_id);
		if (runtime && runtime->active &&
		    runtime->sequence == active->sequence &&
		    runtime->active_waiters &&
		    !((meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
				       FLOW_V2_TASK_PROGRESS_GROUP)) &&
		      meta->blocking_tool_id == active->tool_id &&
		      meta->blocking_sequence == active->sequence))
			should_delay = true;
	}
	if (should_delay) {
		if (!(meta->flags & FLOW_V2_TASK_STALE_DELAYED)) {
			meta->flags |= FLOW_V2_TASK_STALE_DELAYED;
			meta->stale_delay_started_ns = bpf_ktime_get_ns();
			meta->role = FLOW_V2_ROLE_BACKGROUND;
			meta->role_source = FLOW_V2_SOURCE_KERNEL;
			meta->role_reason = FLOW_V2_REASON_STALE_GENERATION;
			stats = flow_session_stats(meta->session_id);
			if (stats)
				stats->stale_delays++;
		}
		return true;
	}
	if (meta->flags & FLOW_V2_TASK_STALE_DELAYED) {
		meta->flags &= ~(FLOW_V2_TASK_STALE_DELAYED |
				 FLOW_V2_TASK_BACKGROUND_RESCUED);
		meta->stale_delay_started_ns = 0;
		meta->role = FLOW_V2_ROLE_NORMAL;
		meta->role_source = FLOW_V2_SOURCE_KERNEL;
		meta->role_reason = FLOW_V2_REASON_STALE_GENERATION;
		flow_refresh_tool_role(meta);
	}
	return false;
}

/*
 * 容器中的回放程序只能把宿主机可验证的套接字对端作为工具范围根。
 * 工具子进程可能在范围登记前已经 fork。只在同会话的明确父子或等待
 * 关系出现时接管未归属任务，避免按 cgroup 批量扫描和误归属。
 */
static __always_inline void
flow_adopt_task(struct flow_v2_task_meta *meta,
		const struct flow_v2_task_meta *owner)
{
	struct flow_v2_tool_runtime *runtime;

	if (!meta || !owner || meta->tool_id || !owner->tool_id ||
	    meta->session_id != owner->session_id)
		return;
	runtime = flow_tool_runtime_lookup(owner->session_id, owner->tool_id);
	if (!runtime || !runtime->active || runtime->sequence != owner->sequence)
		return;
	meta->tool_id = owner->tool_id;
	meta->parent_tool_id = owner->parent_tool_id;
	meta->sequence = owner->sequence;
	meta->flags |= FLOW_V2_TASK_TOOL_MEMBER;
	meta->flags &= ~FLOW_V2_TASK_STALE_GENERATION;
	flow_refresh_tool_role(meta);
	flow_account_tool_member(meta);
}

static __always_inline void
flow_adopt_from_parent(struct flow_v2_task_meta *meta)
{
	struct flow_v2_task_meta *parent;

	if (!meta || meta->tool_id || !meta->parent_pid)
		return;
	parent = bpf_map_lookup_elem(&flow_v2_task_meta, &meta->parent_pid);
	flow_adopt_task(meta, parent);
}

static __always_inline bool
flow_wait_causal_generation(const struct flow_v2_task_meta *waiter,
			    __u64 *tool_id, __u64 *sequence)
{
	struct flow_v2_tool_runtime *runtime;

	if (!waiter)
		return false;
	if ((waiter->flags & (FLOW_V2_TASK_WAIT_TARGET |
			      FLOW_V2_TASK_PROGRESS_GROUP)) &&
	    waiter->blocking_tool_id && waiter->blocking_sequence) {
		*tool_id = waiter->blocking_tool_id;
		*sequence = waiter->blocking_sequence;
		return true;
	}
	if (!waiter->tool_id || !waiter->sequence)
		return false;
	runtime = flow_tool_runtime_lookup(waiter->session_id, waiter->tool_id);
	if (!runtime || !runtime->active || runtime->sequence != waiter->sequence)
		return false;
	*tool_id = waiter->tool_id;
	*sequence = waiter->sequence;
	return true;
}

static __always_inline void
flow_adopt_causal_task(struct flow_v2_task_meta *target,
		       const struct flow_v2_task_meta *waiter,
		       __u64 tool_id, __u64 sequence)
{
	struct flow_v2_tool_runtime *runtime;

	if (!target || target->tool_id)
		return;
	if (waiter && waiter->tool_id == tool_id && waiter->sequence == sequence) {
		flow_adopt_task(target, waiter);
		if (target->tool_id)
			return;
	}
	runtime = flow_tool_runtime_lookup(target->session_id, tool_id);
	if (!runtime || !runtime->active || runtime->sequence != sequence)
		return;
	target->tool_id = tool_id;
	target->sequence = sequence;
	target->flags |= FLOW_V2_TASK_TOOL_MEMBER;
	target->flags &= ~FLOW_V2_TASK_STALE_GENERATION;
	flow_refresh_tool_role(target);
	flow_account_tool_member(target);
}

static __always_inline bool
flow_reserve_progress_group(struct flow_v2_task_meta *waiter,
			    struct flow_v2_task_meta *target,
			    __u32 waiter_pid, __u64 tool_id, __u64 sequence,
			    __u32 depth)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;
	__u32 observed;
	__u32 limit;
	bool reserved = false;

	if (!waiter || !target || !waiter->wait_round ||
	    !(waiter->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE))
		return false;
	if ((target->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
	    target->waiter_pid == waiter_pid &&
	    target->linked_wait_round == waiter->wait_round &&
	    target->blocking_tool_id == tool_id &&
	    target->blocking_sequence == sequence)
		return true;
	cfg = bpf_map_lookup_elem(&session_config, &waiter->cgroup_id);
	if (!cfg || cfg->session_id != waiter->session_id)
		return false;
	limit = cfg->max_progress_group ?: 1;
	if (depth > cfg->max_progress_depth)
		return false;
	observed = waiter->progress_group_admitted;
	bpf_repeat(4) {
		__u32 previous;

		if (observed >= limit)
			return false;
		previous = observed;
		observed = __sync_val_compare_and_swap(
			&waiter->progress_group_admitted, previous, previous + 1);
		if (observed == previous) {
			reserved = true;
			break;
		}
	}
	if (!reserved)
		return false;
	__sync_fetch_and_add(&waiter->progress_group_members, 1);
	target->flags |= FLOW_V2_TASK_PROGRESS_GROUP;
	target->flags &= ~FLOW_V2_TASK_RELAYED_PROGRESS;
	target->waiter_pid = waiter_pid;
	target->blocking_tool_id = tool_id;
	target->blocking_sequence = sequence;
	target->linked_wait_round = waiter->wait_round;
	target->promoted_wait_round = 0;
	target->progress_group_depth = depth;
	target->promotion_evidence_ns = 0;
	stats = flow_session_stats(waiter->session_id);
	if (stats)
		stats->progress_group_links++;
	return true;
}

static __always_inline void
flow_release_progress_group(struct flow_v2_task_meta *target)
{
	struct flow_v2_task_meta *waiter;

	if (!target || !(target->flags & FLOW_V2_TASK_PROGRESS_GROUP) ||
	    !target->waiter_pid)
		return;
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &target->waiter_pid);
	if (waiter && waiter->wait_round == target->linked_wait_round)
		flow_dec_if_positive(&waiter->progress_group_members);
}

/*
 * 默认一轮等待只保留一个推进名额。当前持有名额的进程如果已经
 * 阻塞等待自己的子进程，继续提前运行它没有意义；把同一个名额
 * 转给它正在等待的子进程。名额数量不增加，关系仍受最外层等待
 * 轮次、配置深度和会话提前量约束。
 */
static __attribute__((noinline)) bool
flow_relay_progress_group(struct flow_v2_task_meta *root,
			  struct flow_v2_task_meta *waiter,
			  struct flow_v2_task_meta *target,
			  __u32 target_pid, __u32 depth)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;
	__u32 limit;

	if (!root || !waiter || !target ||
	    !(waiter->flags & FLOW_V2_TASK_PROGRESS_GROUP) ||
	    waiter->waiter_pid == 0 ||
	    (target->flags & (FLOW_V2_TASK_WAIT_TARGET |
			      FLOW_V2_TASK_PROGRESS_GROUP)))
		return false;
	cfg = bpf_map_lookup_elem(&session_config, &root->cgroup_id);
	if (!cfg || cfg->session_id != root->session_id ||
	    depth > cfg->max_progress_depth)
		return false;
	limit = cfg->max_progress_group ?: 1;
	/* 尚有空位时应使用普通纳入路径；这里只复用已经占满的名额。 */
	if (root->progress_group_admitted < limit)
		return false;
	flow_adopt_causal_task(target, waiter, waiter->blocking_tool_id,
			       waiter->blocking_sequence);
	if (!target->tool_id)
		return false;

	target->flags |= FLOW_V2_TASK_PROGRESS_GROUP |
		FLOW_V2_TASK_RELAYED_PROGRESS;
	target->waiter_pid = waiter->waiter_pid;
	target->blocking_tool_id = waiter->blocking_tool_id;
	target->blocking_sequence = waiter->blocking_sequence;
	target->linked_wait_round = root->wait_round;
	target->promoted_wait_round = 0;
	target->progress_group_depth = depth;
	target->promotion_evidence_ns = 0;

	/* 旧持有者正在睡眠，不再占用本轮唯一的推进位置。 */
	waiter->flags &= ~(FLOW_V2_TASK_WAIT_TARGET |
			   FLOW_V2_TASK_PROGRESS_GROUP |
			   FLOW_V2_TASK_RELAYED_PROGRESS |
			   FLOW_V2_TASK_TAIL_PROMOTED |
			   FLOW_V2_TASK_RETURN_CANDIDATE |
			   FLOW_V2_TASK_REJECTED_NOT_TAIL |
			   FLOW_V2_TASK_REJECTED_NO_WAIT);
	waiter->role = FLOW_V2_ROLE_NORMAL;
	waiter->role_source = FLOW_V2_SOURCE_KERNEL;
	waiter->role_reason = FLOW_V2_REASON_NONE;
	waiter->progress_group_depth = 0;
	waiter->promoted_wait_round = 0;
	waiter->promotion_evidence_ns = 0;
	root->wait_target_pid = target_pid;
	flow_mark_policy_task(target_pid);

	stats = flow_session_stats(root->session_id);
	if (stats) {
		stats->progress_group_relays++;
		flow_update_peak_u64(&stats->max_progress_relay_depth, depth);
	}
	return true;
}

static __always_inline bool
flow_link_wait_target(struct flow_v2_task_meta *waiter,
		      struct flow_v2_task_meta *target,
		      __u32 waiter_pid, __u32 target_pid, __u64 now)
{
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_session_stats *stats;
	struct flow_v2_session_config *cfg;
	struct task_struct *target_task;
	struct task_struct *waiter_task;
	__u64 causal_tool_id = 0;
	__u64 causal_sequence = 0;

	if (!waiter || !target || (target->flags & FLOW_V2_TASK_EXITED) ||
	    waiter->session_id != target->session_id ||
	    !flow_wait_causal_generation(waiter, &causal_tool_id,
					 &causal_sequence))
		return false;
	runtime = flow_tool_runtime_lookup(waiter->session_id, causal_tool_id);
	if (!runtime || !runtime->active ||
	    runtime->sequence != causal_sequence)
		return false;
	flow_adopt_causal_task(target, waiter, causal_tool_id, causal_sequence);
	if (!target->tool_id)
		return false;

	waiter->flags |= FLOW_V2_TASK_WAITING_CHILD;
	waiter->wait_started_ns = now;
	if ((target->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
	    target->blocking_tool_id == causal_tool_id &&
	    target->blocking_sequence == causal_sequence &&
	    target->waiter_pid == waiter_pid &&
	    target->linked_wait_round == waiter->wait_round)
		return true;
	if ((target->flags & (FLOW_V2_TASK_WAIT_TARGET |
			      FLOW_V2_TASK_PROGRESS_GROUP)) &&
	    !(target->flags & FLOW_V2_TASK_RETURN_ROOT)) {
		struct flow_v2_tool_runtime *old_runtime;

		flow_release_progress_group(target);
		old_runtime = flow_tool_runtime_lookup(target->session_id,
						       target->blocking_tool_id);
		if (old_runtime &&
		    old_runtime->sequence == target->blocking_sequence &&
		    old_runtime->active_wait_targets)
			flow_dec_if_positive(&old_runtime->active_wait_targets);
		target->flags &= ~(FLOW_V2_TASK_WAIT_TARGET |
				   FLOW_V2_TASK_PROGRESS_GROUP);
	}
	if (!flow_reserve_progress_group(waiter, target, waiter_pid,
					 causal_tool_id, causal_sequence, 0))
		return false;
	/* wait(-1) 可以由一组正在运行的直接子进程推进，但组宽有上限。 */
	if (!waiter->wait_target_pid ||
	    !(waiter->flags & FLOW_V2_TASK_WAIT_ANY_CHILD))
		waiter->wait_target_pid = target_pid;

	target->flags |= FLOW_V2_TASK_WAIT_TARGET;
	flow_mark_policy_task(target_pid);
	target->role_reason = FLOW_V2_REASON_WAIT_TARGET;
	target->waiter_pid = waiter_pid;
	target->blocking_tool_id = causal_tool_id;
	target->blocking_sequence = causal_sequence;
	{
		__u32 active = __sync_add_and_fetch(&runtime->active_wait_targets, 1);

		flow_update_wait_target_peak(runtime, active);
	}
	stats = flow_session_stats(waiter->session_id);
	if (stats) {
		stats->wait_links++;
		if (waiter->flags & FLOW_V2_TASK_WAIT_ANY_CHILD)
			stats->wait_any_links++;
		if (target->tool_id != causal_tool_id ||
		    target->sequence != causal_sequence)
			stats->cross_generation_wait_links++;
	}
	runtime->last_wait_ns = now;
	__sync_fetch_and_add(&runtime->wait_links, 1);
	if (target->tool_id != causal_tool_id ||
	    target->sequence != causal_sequence)
		__sync_fetch_and_add(&runtime->cross_generation_wait_links, 1);
	cfg = bpf_map_lookup_elem(&session_config, &waiter->cgroup_id);
	target_task = bpf_task_from_pid((__s32)target_pid);
	if (target_task) {
		flow_set_wait_target_runnable(target_task, target, cfg, true);
		bpf_task_release(target_task);
	}

	waiter_task = bpf_task_from_pid((__s32)waiter_pid);
	if (waiter_task) {
		flow_emit_related(FLOW_V2_EVENT_WAIT_LINK, waiter_task, target,
				  target_pid);
		bpf_task_release(waiter_task);
	}
	return true;
}

/*
 * 工具启动进程已经确认的等待目标如果继续阻塞，只沿这条实际等待
 * 关系向下扩展。新成员仍计入最外层等待进程的总上限，不创建另一套
 * 独立推进关系。
 */
static __always_inline bool
flow_link_nested_wait_target(struct flow_v2_task_meta *waiter,
			     struct flow_v2_task_meta *target,
			     __u32 target_pid)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *root;
	struct task_struct *target_task;
	bool relayed = false;
	__u32 depth;
	__u32 root_pid;

	if (!waiter || !target ||
	    !(waiter->flags & FLOW_V2_TASK_PROGRESS_GROUP) ||
	    !waiter->waiter_pid ||
	    waiter->progress_group_depth >= 2)
		return false;
	root_pid = waiter->waiter_pid;
	root = bpf_map_lookup_elem(&flow_v2_task_meta, &root_pid);
	if (!root || !(root->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE) ||
	    !(root->flags & FLOW_V2_TASK_WAITING_CHILD) ||
	    root->wait_round != waiter->linked_wait_round ||
	    root->session_id != waiter->session_id)
		return false;
	if ((target->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
	    target->waiter_pid == root_pid &&
	    target->linked_wait_round == root->wait_round)
		return true;
	if (target->flags & (FLOW_V2_TASK_WAIT_TARGET |
			     FLOW_V2_TASK_PROGRESS_GROUP))
		return false;
	depth = waiter->progress_group_depth + 1;
	flow_adopt_causal_task(target, waiter, waiter->blocking_tool_id,
			       waiter->blocking_sequence);
	if (!target->tool_id)
		return false;
	if (!flow_reserve_progress_group(root, target, root_pid,
					waiter->blocking_tool_id,
					waiter->blocking_sequence, depth)) {
		relayed = flow_relay_progress_group(
			root, waiter, target, target_pid, depth);
		if (!relayed)
			return false;
	}
	target->role_reason = FLOW_V2_REASON_WAIT_TARGET;
	flow_mark_policy_task(target_pid);
	/*
	 * 直接等待对象建立关系时会同步登记“可运行的等待对象”。向下
	 * 传递名额后也必须执行同一状态转移，否则自动权重仍把已经睡眠
	 * 的旧对象当作当前对象，看不到真正继续执行的子进程。
	 */
	cfg = bpf_map_lookup_elem(&session_config, &root->cgroup_id);
	target_task = bpf_task_from_pid((__s32)target_pid);
	if (target_task) {
		flow_set_wait_target_runnable(target_task, target, cfg, true);
		bpf_task_release(target_task);
	}
	stats = flow_session_stats(waiter->session_id);
	if (stats) {
		stats->wait_links++;
		stats->nested_wait_links++;
		flow_update_peak_u64(&stats->max_progress_depth_observed,
				     target->progress_group_depth);
	}
	(void)relayed;
	return true;
}

static __always_inline void
flow_link_from_waiting_parent(struct task_struct *p,
			      struct flow_v2_task_meta *target)
{
	struct flow_v2_task_meta *waiter;
	__u32 target_pid;

	if (!p || !target || !target->parent_pid)
		return;
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &target->parent_pid);
	if (!waiter || !(waiter->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE) ||
	    !(waiter->flags & FLOW_V2_TASK_WAIT_BLOCKED) ||
	    !(waiter->flags & FLOW_V2_TASK_WAITING_CHILD) ||
	    !(waiter->flags & FLOW_V2_TASK_WAIT_ANY_CHILD) ||
	    (waiter->flags & FLOW_V2_TASK_NONBLOCKING_WAIT))
		return;
	target_pid = p->pid;
	if ((waiter->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
	    !(waiter->flags & FLOW_V2_TASK_RETURN_ROOT))
		flow_link_nested_wait_target(waiter, target, target_pid);
	else
		flow_link_wait_target(waiter, target, target->parent_pid,
				      target_pid, bpf_ktime_get_ns());
}

static __always_inline void
flow_link_recent_wait_children(struct flow_v2_task_meta *waiter,
			       __u32 waiter_pid, __u64 now)
{
	__u32 index;

#pragma unroll
	for (index = 0; index < 8; index++) {
		struct flow_v2_task_meta *target;
		__u32 target_pid = waiter->recent_child_pids[index];

		if (!target_pid)
			continue;
		target = bpf_map_lookup_elem(&flow_v2_task_meta, &target_pid);
		if (!target || target->parent_pid != waiter_pid ||
		    target->session_id != waiter->session_id ||
		    (target->flags & FLOW_V2_TASK_EXITED))
			continue;
		/* 等待组成员自身继续等待子进程时，子进程仍属于最外层
		 * 工具等待，不能在这里重新建立一组独立的直接关系。 */
		if ((waiter->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
		    !(waiter->flags & FLOW_V2_TASK_RETURN_ROOT))
			flow_link_nested_wait_target(waiter, target, target_pid);
		else
			flow_link_wait_target(waiter, target, waiter_pid,
					      target_pid, now);
	}
}

static __always_inline void
flow_confirm_blocking_wait(struct task_struct *waiter_task)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_task_meta *target;
	struct flow_v2_task_meta *waiter;
	__u64 now = bpf_ktime_get_ns();
	__u32 waiter_pid;

	if (!waiter_task)
		return;
	waiter_pid = BPF_CORE_READ(waiter_task, pid);
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &waiter_pid);
	if (!waiter || !(waiter->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE) ||
	    (waiter->flags & FLOW_V2_TASK_WAIT_BLOCKED))
		return;
	waiter->flags |= FLOW_V2_TASK_WAIT_BLOCKED;
	stats = flow_session_stats(waiter->session_id);
	if (stats)
		stats->confirmed_blocking_waits++;
	runtime = flow_tool_runtime_lookup(waiter->session_id,
					   waiter->blocking_tool_id);
	if (runtime && runtime->active &&
	    runtime->sequence == waiter->blocking_sequence) {
		__u32 previous_waiters = __sync_fetch_and_add(
			&runtime->active_waiters, 1);
		__u32 active_waiters = previous_waiters + 1;

		if (!previous_waiters) {
			__u64 round = __sync_add_and_fetch(
				&runtime->descendant_wait_round, 1);

			if (!round)
				runtime->descendant_wait_round = 1;
			runtime->sustained_wait_round =
				runtime->descendant_wait_round;
			runtime->sustained_run_events = 0;
			runtime->sustained_queue_ns = 0;
		}

		flow_update_active_waiter_peak(runtime, active_waiters);
		if ((waiter->flags & (FLOW_V2_TASK_RETURN_ROOT |
				      FLOW_V2_TASK_SCOPE_ROOT)) &&
		    !(waiter->flags & FLOW_V2_TASK_ADAPTIVE_WAITER) &&
		    (automatic_session_weight ||
		     flow_waiter_is_synchronous(waiter))) {
			cfg = bpf_map_lookup_elem(&session_config,
						  &waiter->cgroup_id);
			waiter->flags |= FLOW_V2_TASK_ADAPTIVE_WAITER;
			flow_session_change_blocking_waiters(cfg, true);
		}
	}
	flow_emit(FLOW_V2_EVENT_WAIT_BLOCKED, waiter_task, waiter);
	if (!waiter->wait_target_pid) {
		flow_link_recent_wait_children(waiter, waiter_pid, now);
		return;
	}
	target = bpf_map_lookup_elem(&flow_v2_task_meta, &waiter->wait_target_pid);
	if (!target) {
		struct task_struct *target_task =
			bpf_task_from_pid((__s32)waiter->wait_target_pid);

		target = flow_create_policy_task(
			target_task, waiter->wait_target_pid, waiter->session_id,
			waiter->cgroup_id, waiter->tool_id, waiter->parent_tool_id,
			waiter->sequence, waiter_pid, waiter->role,
			waiter->role_source, FLOW_V2_TASK_TOOL_MEMBER);
		if (target_task)
			bpf_task_release(target_task);
	}
	if (target && target->session_id == waiter->session_id) {
		if ((waiter->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
		    !(waiter->flags & FLOW_V2_TASK_RETURN_ROOT))
			flow_link_nested_wait_target(waiter, target,
					     waiter->wait_target_pid);
		else
			flow_link_wait_target(waiter, target, waiter_pid,
					      waiter->wait_target_pid, now);
	}
}

static __always_inline bool
flow_dynamic_wait_target_is_live(struct flow_v2_task_meta *target,
				 __u32 target_pid)
{
	struct flow_v2_task_meta *waiter;

	if (!target || (target->flags & FLOW_V2_TASK_EXITED))
		return false;
	if (!(target->flags & (FLOW_V2_TASK_WAIT_TARGET |
			       FLOW_V2_TASK_PROGRESS_GROUP)) ||
	    (target->flags & FLOW_V2_TASK_RETURN_ROOT))
		return true;
	if (!target->waiter_pid)
		return false;
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &target->waiter_pid);
	if (!waiter || !(waiter->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE) ||
	    !(waiter->flags & FLOW_V2_TASK_WAITING_CHILD) ||
	    (waiter->flags & FLOW_V2_TASK_NONBLOCKING_WAIT) ||
	    waiter->blocking_tool_id != target->blocking_tool_id ||
	    waiter->blocking_sequence != target->blocking_sequence ||
	    waiter->wait_round != target->linked_wait_round)
		return false;
	/* 深层成员由同一最外层等待关系约束，不要求它是根进程的直接子进程。 */
	if ((target->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
	    target->progress_group_depth > 0)
		return true;
	if (waiter->flags & FLOW_V2_TASK_WAIT_ANY_CHILD)
		return target->parent_pid == target->waiter_pid;
	return waiter->wait_target_pid == target_pid;
}

static __always_inline void
flow_clear_expired_wait_target(struct flow_v2_task_meta *meta, __u32 target_pid)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_tool_runtime *runtime;
	struct task_struct *target_task;

	if (!meta || (meta->flags & FLOW_V2_TASK_RETURN_ROOT))
		return;
	cfg = bpf_map_lookup_elem(&session_config, &meta->cgroup_id);
	target_task = bpf_task_from_pid((__s32)target_pid);
	if (target_task) {
		flow_set_wait_target_runnable(target_task, meta, cfg, false);
		bpf_task_release(target_task);
	}
	flow_release_progress_group(meta);
	runtime = flow_tool_runtime_lookup(meta->session_id,
					   meta->blocking_tool_id);
	if (runtime && runtime->sequence == meta->blocking_sequence &&
	    runtime->active_wait_targets)
		flow_dec_if_positive(&runtime->active_wait_targets);
	meta->flags &= ~(FLOW_V2_TASK_WAIT_TARGET |
			 FLOW_V2_TASK_PROGRESS_GROUP |
			 FLOW_V2_TASK_RELAYED_PROGRESS |
			 FLOW_V2_TASK_TAIL_PROMOTED |
			 FLOW_V2_TASK_RETURN_CANDIDATE |
			 FLOW_V2_TASK_REJECTED_NOT_TAIL |
			 FLOW_V2_TASK_REJECTED_NO_WAIT);
	meta->waiter_pid = 0;
	meta->blocking_tool_id = 0;
	meta->blocking_sequence = 0;
	meta->linked_wait_round = 0;
	meta->promoted_wait_round = 0;
	meta->progress_group_depth = 0;
	flow_clear_policy_task(target_pid);
	flow_refresh_tool_role(meta);
}

/*
 * 工具启动进程从已经确认的阻塞等待中恢复，说明它接下来要处理子进程
 * 的返回值并继续工作流。这里只给工具根或执行范围根一次短运行机会；
 * 普通的定时器、管道和网络唤醒不会进入这条路径。
 */
static __always_inline void
flow_mark_interactive_wakeup(struct task_struct *p,
			     struct flow_v2_task_meta *meta)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_tool_config *tool;
	struct flow_v2_tool_key key;
	__u64 tool_id;
	__u64 sequence;

	if ((!flow_relation_decision() &&
	     !flow_manual_tool_selected(meta ? meta->session_id : 0,
					meta ? meta->tool_id : 0)) ||
	    !interactive_enabled || !p || !meta ||
	    !(meta->flags & (FLOW_V2_TASK_RETURN_ROOT |
			     FLOW_V2_TASK_SCOPE_ROOT)) ||
	    !(meta->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE) ||
	    !(meta->flags & FLOW_V2_TASK_WAIT_BLOCKED) ||
	    (meta->flags & FLOW_V2_TASK_NONBLOCKING_WAIT))
		return;
	tool_id = meta->blocking_tool_id ?: meta->tool_id;
	sequence = meta->blocking_sequence ?: meta->sequence;
	if (!tool_id || !sequence)
		return;
	key.session_id = meta->session_id;
	key.tool_id = tool_id;
	tool = bpf_map_lookup_elem(&tool_config, &key);
	if (!tool || !tool->active || tool->generation != sequence ||
	    tool->role == FLOW_V2_ROLE_BACKGROUND)
		return;
	if (meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE)
		return;
	meta->flags |= FLOW_V2_TASK_INTERACTIVE_ONCE;
	meta->role = FLOW_V2_ROLE_INTERACTIVE;
	meta->role_source = FLOW_V2_SOURCE_KERNEL;
	meta->role_reason = FLOW_V2_REASON_WAIT_RESULT_READY;
	meta->last_role_change_ns = bpf_ktime_get_ns();
	flow_mark_policy_task(p->pid);
	stats = flow_session_stats(meta->session_id);
	if (stats)
		stats->interactive_wakeups++;
	flow_emit(FLOW_V2_EVENT_ROLE_CHANGE, p, meta);
}

static __always_inline void
flow_cancel_interactive(struct flow_v2_task_meta *meta)
{
	if (!meta || !(meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE))
		return;
	meta->flags &= ~FLOW_V2_TASK_INTERACTIVE_ONCE;
	meta->role = FLOW_V2_ROLE_NORMAL;
	meta->role_source = FLOW_V2_SOURCE_KERNEL;
	meta->role_reason = FLOW_V2_REASON_WAIT_RESULT_READY;
	meta->last_role_change_ns = bpf_ktime_get_ns();
	flow_refresh_tool_role(meta);
}

/*
 * sched_process_wait 只在父进程真正准备休眠时记录关系。父进程再次
 * 变为可运行，说明这一轮等待已经结束；此时立即撤销对应的等待目标。
 */
static __always_inline void flow_finish_wait(__u32 waiter_pid)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_task_meta *target;
	struct flow_v2_task_meta *waiter;

	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &waiter_pid);
	if (!waiter)
		return;
	if (waiter->flags & FLOW_V2_TASK_ADAPTIVE_WAITER) {
		cfg = bpf_map_lookup_elem(&session_config, &waiter->cgroup_id);
		waiter->flags &= ~FLOW_V2_TASK_ADAPTIVE_WAITER;
		flow_session_change_blocking_waiters(cfg, false);
	}
	bpf_map_delete_elem(&flow_v2_waiting_parents, &waiter_pid);
	if (waiter->wait_target_pid) {
		__u32 target_pid = waiter->wait_target_pid;

		target = bpf_map_lookup_elem(&flow_v2_task_meta, &target_pid);
		if (target && target->waiter_pid == waiter_pid &&
		    !(target->flags & FLOW_V2_TASK_RETURN_ROOT))
				flow_clear_expired_wait_target(target, target_pid);
	}
	if (waiter->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE) {
		runtime = flow_tool_runtime_lookup(waiter->session_id,
						   waiter->blocking_tool_id);
		if ((waiter->flags & FLOW_V2_TASK_WAIT_BLOCKED) && runtime &&
			runtime->sequence == waiter->blocking_sequence) {
			flow_dec_if_positive(&runtime->active_waiters);
		}
	}
	waiter->flags &= ~(FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE |
			   FLOW_V2_TASK_WAIT_BLOCKED |
			   FLOW_V2_TASK_NONBLOCKING_WAIT |
			   FLOW_V2_TASK_WAITING_CHILD |
			   FLOW_V2_TASK_WAIT_ANY_CHILD);
	waiter->wait_selector = 0;
	waiter->wait_options = 0;
	waiter->wait_target_pid = 0;
	waiter->progress_group_members = 0;
	waiter->progress_group_admitted = 0;
}

/*
 * 部分工具的上层进程不是通过 wait/waitpid 等待，而是睡眠在管道、
 * 进程间通信或运行库内部。这里记录“这个进程现在确实睡眠”这一事实，
 * 供它的同次工具后代在实际排队后使用。普通抢占不属于睡眠。
 */
static __always_inline void
flow_mark_meta_sleeping(struct flow_v2_task_meta *meta)
{
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_width_state *width;
	__u32 zero = 0;

	if (!(sleeping_descendant_progress || sustained_tool_weight ||
	      sustained_tool_observe_only || descendant_wakeup_observe ||
	      descendant_wakeup_light_observe || descendant_wakeup_progress) ||
	    !progress_enabled || !meta)
		return;
	if (!meta || !meta->tool_id || !meta->sequence ||
	    (meta->flags & (FLOW_V2_TASK_EXITED |
			    FLOW_V2_TASK_STALE_GENERATION)))
		return;
	/* 低开销在线路径只记录处理器已经繁忙时开始的睡眠。完整观测
	 * 仍保留全部睡眠，用来分析过滤前后的覆盖差异。 */
	if (!descendant_wakeup_observe &&
	    (descendant_wakeup_light_observe || descendant_wakeup_progress) &&
	    !(sleeping_descendant_progress || sustained_tool_weight ||
	      sustained_tool_observe_only)) {
		width = bpf_map_lookup_elem(&width_state, &zero);
		if (!width || !flow_has_high_cpu_pressure(width))
			return;
	}
	/* 正式唤醒识别只关心仍有子进程的上层进程和工具入口。
	 * 详细观测保留全部任务，用于核对过滤前后的覆盖差异。 */
	if (!descendant_wakeup_observe &&
	    (descendant_wakeup_light_observe || descendant_wakeup_progress) &&
	    !meta->live_children &&
	    !(meta->flags & (FLOW_V2_TASK_RETURN_ROOT |
			     FLOW_V2_TASK_SCOPE_ROOT)))
		return;
	runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
	if (!runtime || !runtime->active || runtime->sequence != meta->sequence)
		return;
	if (meta->flags & FLOW_V2_TASK_SLEEPING)
		return;
	meta->sleep_round++;
	if (!meta->sleep_round)
		meta->sleep_round = 1;
	meta->sleep_started_ns = bpf_ktime_get_ns();
	meta->flags |= FLOW_V2_TASK_SLEEPING;
}

static __always_inline void
flow_mark_task_sleeping(struct task_struct *p)
{
	struct flow_v2_task_meta *meta;

	/* 通用后代唤醒识别在 quiescent() 已有的进程查询上维护状态，
	 * 不在每次上下文切换时重复查表。 */
	if (!(sleeping_descendant_progress || sustained_tool_weight ||
	      sustained_tool_observe_only) || !p)
		return;
	meta = flow_lookup_task(p);
	flow_mark_meta_sleeping(meta);
}

static __always_inline void
flow_mark_meta_awake(struct flow_v2_task_meta *meta)
{
	if (!(sleeping_descendant_progress || sustained_tool_weight ||
	      sustained_tool_observe_only || descendant_wakeup_observe ||
	      descendant_wakeup_light_observe || descendant_wakeup_progress) ||
	    !meta)
		return;
	if (!(meta->flags & FLOW_V2_TASK_SLEEPING))
		return;
	meta->flags &= ~FLOW_V2_TASK_SLEEPING;
	meta->sleep_started_ns = 0;
}

static __always_inline bool
flow_has_other_policy_reason(const struct flow_v2_task_meta *meta)
{
	return meta && (meta->flags &
		(FLOW_V2_TASK_WAIT_TARGET | FLOW_V2_TASK_PROGRESS_GROUP |
		 FLOW_V2_TASK_RETURN_ROOT | FLOW_V2_TASK_TAIL_PROMOTED |
		 FLOW_V2_TASK_INTERACTIVE_ONCE | FLOW_V2_TASK_STALE_DELAYED |
		 FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED |
		 FLOW_V2_TASK_DESCENDANT_ROOT_WAKER_PROMOTED));
}

static __always_inline void
flow_clear_sleeping_descendant(struct flow_v2_task_meta *meta, __u32 pid,
			       bool invalid)
{
	struct flow_v2_session_stats *stats;
	bool was_candidate;

	if (!meta)
		return;
	was_candidate = meta->flags & FLOW_V2_TASK_SLEEPING_DESCENDANT;
	meta->flags &= ~(FLOW_V2_TASK_SLEEPING_DESCENDANT |
			 FLOW_V2_TASK_DESCENDANT_PROMOTED);
	meta->sleeping_ancestor_pid = 0;
	meta->sleeping_ancestor_depth = 0;
	meta->linked_descendant_round = 0;
	if (invalid && was_candidate) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			stats->sleeping_descendant_invalidations++;
	}
	if (!flow_has_other_policy_reason(meta))
		flow_clear_policy_task(pid);
}

/*
 * 每个候选只向上检查有限个父进程，不遍历整棵进程树。离线解析表明，
 * Dask 与 SQLGlot 中需要补充的主要计算位于工具根以下二至三层，四层
 * 已覆盖这两个重点案例，同时把调度路径上的映射查询限制在固定范围。
 */
static __always_inline struct flow_v2_task_meta *
flow_find_sleeping_ancestor(const struct flow_v2_task_meta *meta,
			    __u32 *ancestor_pid, __u32 *depth,
			    __u64 *sleep_started_ns)
{
	struct flow_v2_task_meta *ancestor = NULL;
	__u32 pid;

	if (!meta || !meta->parent_pid)
		return NULL;
	pid = meta->parent_pid;
	bpf_repeat(4) {
		ancestor = bpf_map_lookup_elem(&flow_v2_task_meta, &pid);
		if (!ancestor || ancestor->session_id != meta->session_id ||
		    ancestor->tool_id != meta->tool_id ||
		    ancestor->sequence != meta->sequence)
			break;
		(*depth)++;
		if ((ancestor->flags & FLOW_V2_TASK_SLEEPING) &&
		    ancestor->sleep_started_ns) {
			*sleep_started_ns = ancestor->sleep_started_ns;
			*ancestor_pid = pid;
			return ancestor;
		}
		if (!ancestor->parent_pid || ancestor->parent_pid == pid)
			break;
		pid = ancestor->parent_pid;
	}
	return NULL;
}

/*
 * 一次工具等待中，同一工具的普通后代至少两次实际获得运行机会，且
 * 累计排队达到一个正常会话时间片后，才形成会话候选。两个条件分别
 * 证明后代确实在执行，以及处理器竞争正在妨碍它继续执行。
 */
static __always_inline void
flow_record_sustained_tool_pressure(struct task_struct *p,
				    struct flow_v2_task_meta *meta,
				    const struct flow_v2_session_config *cfg,
				    __u64 now)
{
	struct flow_v2_automatic_weight_state *automatic;
	struct flow_v2_session_adaptive_state *adaptive;
	struct flow_v2_session_stats *stats;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_task_meta *ancestor;
	__u64 queue_threshold = cgrp_slice_ns ?: 5ULL * 1000 * 1000;
	__u64 wait_ns;
	__u64 queue_ns;
	__u64 runs;
	__u64 sleep_started_ns = 0;
	__u32 ancestor_pid = 0;
	__u32 depth = 0;
	__u32 zero = 0;

	if (!(sustained_tool_weight || sustained_tool_observe_only) ||
	    !automatic_session_weight || !p || !meta || !cfg || !meta->tool_id ||
	    !meta->sequence || !meta->enqueue_ns || now <= meta->enqueue_ns ||
	    (meta->flags & (FLOW_V2_TASK_EXITED |
			    FLOW_V2_TASK_STALE_GENERATION)))
		return;
	runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
	if (!runtime || !runtime->active || runtime->sequence != meta->sequence ||
	    !runtime->active_waiters || !runtime->descendant_wait_round)
		return;
	ancestor = flow_find_sleeping_ancestor(meta, &ancestor_pid, &depth,
					      &sleep_started_ns);
	if (!ancestor || meta->enqueue_ns < sleep_started_ns)
		return;
	wait_ns = now - meta->enqueue_ns;
	if (wait_ns > automatic_weight_window_ns)
		wait_ns = automatic_weight_window_ns;
	if (runtime->sustained_wait_round != runtime->descendant_wait_round) {
		runtime->sustained_wait_round = runtime->descendant_wait_round;
		runtime->sustained_run_events = 0;
		runtime->sustained_queue_ns = 0;
	}
	runs = __sync_add_and_fetch(&runtime->sustained_run_events, 1);
	queue_ns = __sync_add_and_fetch(&runtime->sustained_queue_ns, wait_ns);
	stats = flow_session_stats(meta->session_id);
	if (stats) {
		__sync_fetch_and_add(&stats->sustained_descendant_runs, 1);
		__sync_fetch_and_add(&stats->sustained_descendant_queue_ns, wait_ns);
		flow_update_peak_u64(&stats->max_sleeping_descendant_depth, depth);
	}
	if (sustained_tool_observe_only || runs < 2 ||
	    queue_ns < queue_threshold)
		return;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (!adaptive || !adaptive->active_blocking_waiters ||
	    adaptive->sustained_descendant_pressure)
		return;
	if (adaptive->automatic_weight_cooldown_deadline_ns &&
	    now < adaptive->automatic_weight_cooldown_deadline_ns) {
		if (stats)
			__sync_fetch_and_add(&stats->sustained_pressure_cooldowns, 1);
		return;
	}
	if (__sync_val_compare_and_swap(
		    &adaptive->sustained_descendant_pressure, 0, 1))
		return;
	if (!__sync_val_compare_and_swap(&adaptive->wait_pressure_seen, 0, 1)) {
		if (!__sync_add_and_fetch(&adaptive->wait_pressure_generation, 1))
			__sync_fetch_and_add(&adaptive->wait_pressure_generation, 1);
	}
	__sync_lock_test_and_set(&runtime->sustained_run_events, 0);
	__sync_lock_test_and_set(&runtime->sustained_queue_ns, 0);
	if (stats) {
		__sync_fetch_and_add(&stats->sustained_pressure_events, 1);
		__sync_fetch_and_add(&stats->automatic_weight_pressure_waits, 1);
	}
	automatic = bpf_map_lookup_elem(&automatic_weight_state, &zero);
	if (automatic)
		__sync_fetch_and_add(&automatic->candidate_epoch, 1);
	flow_update_adaptive_weight(cfg, adaptive);
}

/*
 * runnable() 在唤醒路径中执行时，当前进程是发起唤醒的一方，p 是被
 * 唤醒的进程。若两者属于同一次工具执行，而且当前进程确实位于被
 * 唤醒进程的后代中，这条关系比“同一进程树”更接近一次结果交付。
 * 本函数目前只采集事实，不改变类别、权重或处理器位置。
 */
static __always_inline void
flow_record_descendant_wakeup(struct task_struct *p,
			      struct flow_v2_task_meta *wakee)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *ancestor;
	struct flow_v2_task_meta *waker;
	struct flow_v2_width_state *width;
	struct task_struct *current;
	__u64 sleep_started_ns = 0;
	__u64 queue_ns;
	__u64 sleep_ns;
	__u64 sleep_threshold;
	__u64 wakee_sleep_started_ns;
	__u32 ancestor_pid = 0;
	__u32 depth = 0;
	__u32 wakee_pid;
	__u32 waker_pid;
	__u32 zero = 0;
	bool cpu_busy = false;
	bool queue_evidence = false;

	if (!(descendant_wakeup_observe || descendant_wakeup_light_observe ||
	      descendant_wakeup_progress) || !p)
		return;
	wakee_pid = BPF_CORE_READ(p, pid);
	if (!wakee || !(wakee->flags & FLOW_V2_TASK_SLEEPING) ||
	    !wakee->tool_id || !wakee->sequence)
		return;
	wakee_sleep_started_ns = wakee->sleep_started_ns;
	if (!wakee_sleep_started_ns)
		return;
	sleep_threshold = progress_slice_ns ?: 2ULL * 1000 * 1000;
	sleep_ns = bpf_ktime_get_ns() - wakee_sleep_started_ns;
	/* 正式路径在父进程查找之前排除短暂睡眠；完整观测模式仍
	 * 保留它们，用来计算过滤比例。 */
	if (!descendant_wakeup_observe && sleep_ns < sleep_threshold)
		return;
	width = bpf_map_lookup_elem(&width_state, &zero);
	if (width && flow_has_high_cpu_pressure(width))
		cpu_busy = true;
	/* 正式路径只在处理器确实繁忙时继续追溯唤醒者。没有竞争时
	 * 改变顺序没有意义，也不应承担父进程查询成本。 */
	if (!descendant_wakeup_observe && !cpu_busy)
		return;
	current = (struct task_struct *)bpf_get_current_task_btf();
	if (!current)
		return;
	waker_pid = BPF_CORE_READ(current, pid);
	if (!waker_pid || waker_pid == wakee_pid)
		return;
	waker = bpf_map_lookup_elem(&flow_v2_task_meta, &waker_pid);
	if (!waker || waker->session_id != wakee->session_id ||
	    waker->tool_id != wakee->tool_id ||
	    waker->sequence != wakee->sequence)
		return;
	ancestor = flow_find_sleeping_ancestor(waker, &ancestor_pid, &depth,
					      &sleep_started_ns);
	if (!ancestor || ancestor_pid != wakee_pid)
		return;
	stats = flow_session_stats(wakee->session_id);
	if (!stats)
		return;
	__sync_fetch_and_add(&stats->descendant_wake_links, 1);
	sleep_ns = bpf_ktime_get_ns() - sleep_started_ns;
	__sync_fetch_and_add(&stats->descendant_wake_sleep_ns, sleep_ns);
	flow_update_peak_u64(&stats->descendant_wake_max_sleep_ns, sleep_ns);
	if (sleep_ns >= sleep_threshold)
		__sync_fetch_and_add(&stats->descendant_wake_long_links, 1);
	if (wakee->flags & (FLOW_V2_TASK_RETURN_ROOT |
			    FLOW_V2_TASK_SCOPE_ROOT)) {
		__sync_fetch_and_add(&stats->descendant_wake_root_links, 1);
	}
	queue_ns = waker->last_runnable_wait_ns;
	if (queue_ns) {
		__sync_fetch_and_add(&stats->descendant_wake_queued_links, 1);
		__sync_fetch_and_add(&stats->descendant_waker_queue_ns, queue_ns);
	}
	if (cpu_busy) {
		__sync_fetch_and_add(&stats->descendant_wake_pressure_links, 1);
	}
	/* 诊断模式使用精确的逐进程排队记录；正式动作只读取调度器已经
	 * 维护的全机可运行进程数，避免给每次普通进程调度增加写操作。 */
	queue_evidence = descendant_wakeup_observe ?
		(queue_ns >= (progress_slice_ns ? progress_slice_ns / 8 :
						250ULL * 1000) &&
		 waker->last_runnable_wait_at_ns &&
		 bpf_ktime_get_ns() - waker->last_runnable_wait_at_ns <=
			250ULL * 1000 * 1000) : cpu_busy;
	if (sleep_ns >= sleep_threshold &&
	    queue_evidence) {
		if (wakee->flags & (FLOW_V2_TASK_RETURN_ROOT |
				    FLOW_V2_TASK_SCOPE_ROOT))
			waker->flags |= FLOW_V2_TASK_DESCENDANT_ROOT_WAKER;
		if (!(waker->flags & FLOW_V2_TASK_DESCENDANT_WAKER)) {
			waker->flags |= FLOW_V2_TASK_DESCENDANT_WAKER;
			__sync_fetch_and_add(&stats->descendant_waker_marks, 1);
			if (waker->flags & FLOW_V2_TASK_DESCENDANT_ROOT_WAKER)
				__sync_fetch_and_add(
					&stats->descendant_root_waker_marks, 1);
		}
	}
}

static __always_inline void
flow_record_descendant_waker_requeue(struct flow_v2_task_meta *meta, __u32 pid)
{
	struct flow_v2_session_stats *stats;
	bool root;

	if (!(descendant_wakeup_observe || descendant_wakeup_light_observe ||
	      descendant_wakeup_progress) || !meta ||
	    !(meta->flags & FLOW_V2_TASK_DESCENDANT_WAKER))
		return;
	root = meta->flags & FLOW_V2_TASK_DESCENDANT_ROOT_WAKER;
	meta->flags &= ~(FLOW_V2_TASK_DESCENDANT_WAKER |
			 FLOW_V2_TASK_DESCENDANT_ROOT_WAKER);
	stats = flow_session_stats(meta->session_id);
	if (!stats)
		return;
	__sync_fetch_and_add(&stats->descendant_waker_requeues, 1);
	if (root)
		__sync_fetch_and_add(&stats->descendant_root_waker_requeues, 1);
	if (descendant_wakeup_progress) {
		meta->flags |= FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED;
		if (root)
			meta->flags |=
				FLOW_V2_TASK_DESCENDANT_ROOT_WAKER_PROMOTED;
		flow_mark_policy_task(pid);
		__sync_fetch_and_add(&stats->descendant_waker_promotions, 1);
		if (root)
			__sync_fetch_and_add(
				&stats->descendant_root_waker_promotions, 1);
	}
}

static __always_inline void
flow_finish_descendant_waker_promotion(struct flow_v2_task_meta *meta,
				       __u32 pid)
{
	struct flow_v2_session_stats *stats;

	if (!meta || !(meta->flags & FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED))
		return;
	meta->flags &= ~(FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED |
			 FLOW_V2_TASK_DESCENDANT_ROOT_WAKER_PROMOTED);
	stats = flow_session_stats(meta->session_id);
	if (stats)
		__sync_fetch_and_add(&stats->descendant_waker_promotion_runs, 1);
	if (!flow_has_other_policy_reason(meta))
		flow_clear_policy_task(pid);
}

/*
 * 先由一次真实排队建立证据，再在上层进程仍处于睡眠状态时给后代一个短
 * 时间片。同一次工具等待用原子比较只选择一个后代；后代运行一片后恢复
 * 普通排序。整个判断不读取命令名称、未来进程数量或离线分析结果。
 */
static __always_inline void
flow_refresh_sleeping_descendant(struct task_struct *p,
				 struct flow_v2_task_meta *meta)
{
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_session_stats *stats;
	struct flow_v2_global_stats *global;
	struct flow_v2_task_meta *ancestor;
	__u64 min_wait_ns = progress_slice_ns ? progress_slice_ns / 8 : 50 * 1000;
	__u64 now = bpf_ktime_get_ns();
	__u64 observed;
	__u64 sleep_started_ns = 0;
	__u32 ancestor_pid = 0;
	__u32 depth = 0;
	bool new_candidate;

	if (!sleeping_descendant_progress || !flow_relation_decision() ||
	    !progress_enabled || !p || !meta || !meta->tool_id ||
	    !meta->sequence ||
	    (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
			    FLOW_V2_TASK_PROGRESS_GROUP |
			    FLOW_V2_TASK_RETURN_ROOT |
			    FLOW_V2_TASK_EXITED |
			    FLOW_V2_TASK_STALE_GENERATION))) {
		if (meta && (meta->flags & FLOW_V2_TASK_SLEEPING_DESCENDANT))
			flow_clear_sleeping_descendant(meta, p ? p->pid : 0, true);
		return;
	}
	runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
	if (!runtime || !runtime->active || runtime->sequence != meta->sequence ||
	    !runtime->active_waiters || !runtime->descendant_wait_round) {
		flow_clear_sleeping_descendant(meta, p->pid, true);
		return;
	}
	ancestor = flow_find_sleeping_ancestor(meta, &ancestor_pid, &depth,
					      &sleep_started_ns);
	if (!ancestor) {
		flow_clear_sleeping_descendant(meta, p->pid, true);
		return;
	}
	new_candidate = !(meta->flags & FLOW_V2_TASK_SLEEPING_DESCENDANT) ||
		meta->sleeping_ancestor_pid != ancestor_pid ||
		meta->linked_descendant_round != runtime->descendant_wait_round;
	meta->flags |= FLOW_V2_TASK_SLEEPING_DESCENDANT;
	meta->sleeping_ancestor_pid = ancestor_pid;
	meta->sleeping_ancestor_depth = depth;
	meta->linked_descendant_round = runtime->descendant_wait_round;
	flow_mark_policy_task(p->pid);
	stats = flow_session_stats(meta->session_id);
	if (new_candidate && stats) {
		stats->sleeping_descendant_candidates++;
		flow_update_peak_u64(&stats->max_sleeping_descendant_depth, depth);
	}
	if ((meta->flags & FLOW_V2_TASK_TAIL_PROMOTED) ||
	    meta->last_runnable_wait_ns < min_wait_ns ||
	    !meta->last_runnable_wait_at_ns ||
	    meta->last_runnable_wait_at_ns < sleep_started_ns ||
	    now - meta->last_runnable_wait_at_ns > 250 * 1000 * 1000)
		return;
	observed = runtime->descendant_granted_round;
	if (observed == runtime->descendant_wait_round ||
	    __sync_val_compare_and_swap(&runtime->descendant_granted_round,
				observed, runtime->descendant_wait_round) != observed)
		return;
	meta->flags |= FLOW_V2_TASK_TAIL_PROMOTED |
		       FLOW_V2_TASK_DESCENDANT_PROMOTED;
	meta->role = FLOW_V2_ROLE_PROGRESS;
	meta->role_source = FLOW_V2_SOURCE_KERNEL;
	meta->role_reason = FLOW_V2_REASON_SLEEPING_ANCESTOR;
	meta->promotion_evidence_ns = meta->last_runnable_wait_at_ns;
	meta->last_role_change_ns = now;
	if (stats) {
		stats->tail_promotions++;
		stats->return_promotions++;
		stats->sleeping_descendant_promotions++;
	}
	global = flow_global();
	if (global)
		global->role_changes++;
	flow_emit_related(FLOW_V2_EVENT_ROLE_CHANGE, p, meta, ancestor_pid);
}

static __always_inline void
flow_refresh_return_role(struct task_struct *p, struct flow_v2_task_meta *meta)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_global_stats *global;
	struct flow_v2_tool_runtime *causal_runtime;
	__u64 min_wait_ns = progress_slice_ns ? progress_slice_ns / 8 : 50 * 1000;
	__u64 now = bpf_ktime_get_ns();
	__u32 old_role;
	bool first_relation_service = false;

	if ((!flow_relation_decision() &&
	     !flow_manual_tool_selected(meta ? meta->session_id : 0,
					meta ? meta->blocking_tool_id : 0)) ||
	    !progress_enabled || !meta || !meta->tool_id ||
	    (meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE))
		return;
	if ((meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
			    FLOW_V2_TASK_PROGRESS_GROUP)) &&
	    !flow_dynamic_wait_target_is_live(meta, p->pid)) {
		flow_clear_expired_wait_target(meta, p->pid);
		return;
	}
	if (!(meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
			     FLOW_V2_TASK_RETURN_ROOT |
			     FLOW_V2_TASK_PROGRESS_GROUP)) ||
	    !meta->blocking_tool_id)
		return;
	causal_runtime = flow_tool_runtime_lookup(meta->session_id,
						  meta->blocking_tool_id);
	if (!causal_runtime || !causal_runtime->active ||
	    causal_runtime->sequence != meta->blocking_sequence) {
		if (meta->flags & FLOW_V2_TASK_TAIL_PROMOTED) {
			meta->flags &= ~FLOW_V2_TASK_TAIL_PROMOTED;
			meta->role = FLOW_V2_ROLE_NORMAL;
			meta->role_source = FLOW_V2_SOURCE_KERNEL;
			meta->role_reason = FLOW_V2_REASON_STALE_GENERATION;
			meta->last_role_change_ns = bpf_ktime_get_ns();
		}
		return;
	}
	if (meta->flags & FLOW_V2_TASK_TAIL_PROMOTED)
		return;
	if (meta->role_source == FLOW_V2_SOURCE_TOOL &&
	    meta->role != FLOW_V2_ROLE_PARALLEL &&
	    meta->role != FLOW_V2_ROLE_NORMAL)
		return;
	if (!causal_runtime->wait_links)
		return;
	stats = flow_session_stats(meta->session_id);
	if (!(meta->flags & FLOW_V2_TASK_RETURN_CANDIDATE)) {
		meta->flags |= FLOW_V2_TASK_RETURN_CANDIDATE;
		if (stats)
			stats->return_candidates++;
	}
	/*
	 * sched_switch 已经确认最外层等待进程确实睡眠，随后建立的等待
	 * 关系本身足以说明目标决定工具返回。首次推进不再要求目标先排队
	 * 一轮；同一等待轮次仍只允许一次，后续推进仍必须有可运行等待。
	 */
	/*
	 * 最外层工具进程已经明确等待直接子进程时，关系建立本身足以
	 * 支持一次有限提前运行。更深层只有在原持有者也确认睡眠并等待
	 * 子进程、且唯一名额已经实际转交时才获得同样的一次机会；普通
	 * 后代仍不进入该路径，因此不会把整个多进程子树一起提前。
	 */
	if (!require_runnable_wait &&
	    (meta->progress_group_depth == 0 ||
	     (meta->flags & FLOW_V2_TASK_RELAYED_PROGRESS)) &&
	    (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
			   FLOW_V2_TASK_PROGRESS_GROUP)) &&
	    meta->linked_wait_round &&
	    meta->promoted_wait_round != meta->linked_wait_round &&
	    causal_runtime->active_waiters)
		first_relation_service = true;
	/*
	 * 等待目标只在最后一个成员时推进。工具根也只有在进程组基本
	 * 排空时才代表最终返回；仍有多个存活成员时，它只是中间协调者。
	 */
	/*
	 * 工具根只有在它从 wait 中恢复并经历实际可运行等待后才会来到
	 * 这里；这个事实比维护整棵进程树的存活计数更直接，也更便宜。
	 */
	/*
	 * 只有已经实际经历过可运行等待的尾部成员才获得提前。
	 * 入队本身证明任务当前可运行；最近一次等待则证明该进程
	 * 在这台机器上确实受到处理器竞争，而不是 I/O 阻塞。
	 */
	if (!first_relation_service && meta->last_runnable_wait_ns < min_wait_ns) {
		if (!(meta->flags & FLOW_V2_TASK_REJECTED_NO_WAIT)) {
			meta->flags |= FLOW_V2_TASK_REJECTED_NO_WAIT;
			if (stats)
				stats->return_rejected_no_wait++;
		}
		return;
	}
	if (!first_relation_service && (!meta->last_runnable_wait_at_ns ||
	    now - meta->last_runnable_wait_at_ns >
		250 * 1000 * 1000)) {
		if (!(meta->flags & FLOW_V2_TASK_REJECTED_NO_WAIT)) {
			meta->flags |= FLOW_V2_TASK_REJECTED_NO_WAIT;
			if (stats)
				stats->return_rejected_no_wait++;
		}
		return;
	}
	/* 同一次可运行等待只使用一次；新的排队等待可以再次提供证据。 */
	if (!first_relation_service &&
	    meta->last_runnable_wait_at_ns <= meta->promotion_evidence_ns)
		return;
	/*
	 * 一轮顶层阻塞等待最多推进组内每个成员一次。反复唤醒只说明
	 * 同一成员继续执行，不应被解释为新的工具返回关系。
	 */
	if (meta->linked_wait_round &&
	    meta->promoted_wait_round == meta->linked_wait_round)
		return;
	old_role = meta->role;
	meta->role = FLOW_V2_ROLE_PROGRESS;
	meta->role_source = FLOW_V2_SOURCE_KERNEL;
	meta->role_reason = FLOW_V2_REASON_RETURN_COHORT;
	meta->flags |= FLOW_V2_TASK_TAIL_PROMOTED;
	meta->flags &= ~(FLOW_V2_TASK_RELAYED_PROGRESS |
			 FLOW_V2_TASK_REJECTED_NOT_TAIL |
			 FLOW_V2_TASK_REJECTED_NO_WAIT);
	meta->promotion_evidence_ns = first_relation_service ? now :
		meta->last_runnable_wait_at_ns;
	if (meta->linked_wait_round)
		meta->promoted_wait_round = meta->linked_wait_round;
	meta->last_role_change_ns = now;
	if (stats) {
		stats->tail_promotions++;
		stats->return_promotions++;
		if (meta->flags & FLOW_V2_TASK_RETURN_ROOT)
			stats->return_root_promotions++;
		else
			stats->wait_target_promotions++;
		if (meta->flags & FLOW_V2_TASK_PROGRESS_GROUP) {
			stats->progress_group_promotions++;
			if (meta->progress_group_depth > 0)
				stats->nested_wait_promotions++;
		}
		if (first_relation_service)
			stats->first_relation_promotions++;
	}
	global = flow_global();
	if (global)
		global->role_changes++;
	if (old_role != FLOW_V2_ROLE_PROGRESS)
		flow_emit(FLOW_V2_EVENT_ROLE_CHANGE, p, meta);
}

static __always_inline __u64
flow_take_bias(const struct flow_v2_session_config *cfg,
	       struct flow_v2_task_meta *meta, __u64 requested)
{
	struct flow_v2_session_runtime *runtime;
	__u64 now, remaining, granted = 0;

	if (!requested || !cfg->boost_budget_ns || !cfg->policy_window_ns)
		return 0;
	runtime = flow_session_runtime(cfg->session_id);
	if (!runtime)
		return 0;

	now = bpf_ktime_get_ns();
	bpf_spin_lock(&runtime->lock);
	if (!runtime->window_start_ns ||
	    now - runtime->window_start_ns >= cfg->policy_window_ns) {
		runtime->window_start_ns = now;
		runtime->boost_used_ns = 0;
	}
	remaining = cfg->boost_budget_ns > runtime->boost_used_ns ?
		cfg->boost_budget_ns - runtime->boost_used_ns : 0;
	granted = requested < remaining ? requested : remaining;
	runtime->boost_used_ns += granted;
	if (meta && granted) {
		meta->reserved_bias_ns = granted;
		meta->bias_window_start_ns = runtime->window_start_ns;
	}
	bpf_spin_unlock(&runtime->lock);
	return granted;
}

static __always_inline __u64
flow_refund_unused_bias(struct flow_v2_task_meta *meta, __u64 runtime_ns)
{
	struct flow_v2_session_runtime *runtime;
	__u64 refund = 0;

	if (!meta || !meta->reserved_bias_ns)
		return 0;
	runtime = bpf_map_lookup_elem(&session_runtime, &meta->session_id);
	if (!runtime)
		goto out;
	bpf_spin_lock(&runtime->lock);
	if (runtime->window_start_ns == meta->bias_window_start_ns &&
	    meta->reserved_bias_ns > runtime_ns) {
		refund = meta->reserved_bias_ns - runtime_ns;
		if (refund > runtime->boost_used_ns)
			refund = runtime->boost_used_ns;
		runtime->boost_used_ns -= refund;
	}
	bpf_spin_unlock(&runtime->lock);
out:
	meta->reserved_bias_ns = 0;
	meta->bias_window_start_ns = 0;
	return refund;
}

static __always_inline __u64
flow_progress_bias(const struct flow_v2_session_config *cfg,
		   const struct flow_v2_task_meta *meta)
{
	(void)meta;
	return progress_enabled && cfg ? cfg->progress_bias_ns : 0;
}

static __always_inline __u64
flow_progress_slice(const struct flow_v2_task_meta *meta)
{
	(void)meta;
	return progress_slice_ns ?: SCX_SLICE_DFL;
}

static __always_inline __u64
flow_interactive_bias(const struct flow_v2_session_config *cfg)
{
	return interactive_enabled && cfg ? cfg->interactive_bias_ns : 0;
}

static __always_inline __u64 flow_interactive_slice(void)
{
	return interactive_slice_ns ?: SCX_SLICE_DFL;
}

/*
 * 固定阶段和工具边界两种对照不读取阻塞等待关系。阶段规则只让串行
 * 阶段的命令根获得有限提前；工具规则把当前同步工具整体视为候选。
 * 两者仍共用会话提前量上限，因此不会形成永久高优先级。
 */
static __always_inline __u64
flow_coarse_policy_bias(const struct flow_v2_session_config *cfg,
			struct flow_v2_task_meta *meta)
{
	struct flow_v2_tool_config *tool;
	struct flow_v2_tool_key key;

	if (!cfg || !meta || !meta->tool_id || !meta->sequence)
		return 0;
	key.session_id = meta->session_id;
	key.tool_id = meta->tool_id;
	tool = bpf_map_lookup_elem(&tool_config, &key);
	if (!tool || !tool->active || tool->generation != meta->sequence)
		return 0;
	if (flow_stage_decision()) {
		if (!(meta->flags & FLOW_V2_TASK_RETURN_ROOT) ||
		    meta->behavior != FLOW_V2_BEHAVIOR_SERIAL)
			return 0;
		return cfg->interactive_bias_ns;
	}
	if (flow_tool_decision() && tool->role != FLOW_V2_ROLE_BACKGROUND)
		return cfg->progress_bias_ns;
	return 0;
}

/*
 * 短交互任务仍可进入专用队列，但繁忙时的主动抢占不能随高频 wait
 * 返回无限发生。同一会话在一个策略统计周期内至多发起一次抢占；
 * 后续短交互等待自然的调度点，从而给普通任务保留连续运行机会。
 */
static __always_inline bool flow_allow_preempt(__u64 session_id, __u64 interval)
{
	__u64 *lastp;
	__u64 initial;
	__u64 now = bpf_ktime_get_ns();
	bool allowed = false;

	lastp = bpf_map_lookup_elem(&interactive_preempt_at, &session_id);
	if (!lastp) {
		initial = now;
		return !bpf_map_update_elem(&interactive_preempt_at, &session_id,
					    &initial, BPF_NOEXIST);
	}
	bpf_repeat(2) {
		__u64 observed = *lastp;

		if (observed && now - observed < interval)
			break;
		if (__sync_val_compare_and_swap(
				lastp, observed, now) == observed) {
			allowed = true;
			break;
		}
	}
	return allowed;
}

static __always_inline bool flow_allow_interactive_preempt(__u64 session_id)
{
	return flow_allow_preempt(session_id, 100ULL * 1000 * 1000);
}

static __always_inline void
flow_record_enqueue(struct flow_v2_task_meta *meta, __u32 role, __u64 bias)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_global_stats *global;
	bool needs_wait_sample;

	/*
	 * 排队等待只参与返回候选判断和后台最长等待恢复。普通并行任务可能
	 * 每秒入队数万次，不应为纯观测数据持续写散列表和每 CPU 统计。
	 */
	needs_wait_sample = detailed_accounting ||
		((sustained_tool_weight || sustained_tool_observe_only) &&
		 meta->tool_id) ||
		(descendant_wakeup_observe && meta->tool_id) ||
		(meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
				FLOW_V2_TASK_RETURN_ROOT |
				FLOW_V2_TASK_PROGRESS_GROUP |
				FLOW_V2_TASK_SLEEPING_DESCENDANT |
				FLOW_V2_TASK_DESCENDANT_PROMOTED |
				FLOW_V2_TASK_TAIL_PROMOTED |
				FLOW_V2_TASK_INTERACTIVE_ONCE)) ||
		role == FLOW_V2_ROLE_BACKGROUND;
	if (!needs_wait_sample && !bias)
		return;
	meta->enqueue_ns = needs_wait_sample ? bpf_ktime_get_ns() : 0;
	stats = flow_session_stats(meta->session_id);
	if (stats) {
		if (detailed_accounting) {
			stats->enqueues++;
			stats->role_enqueues[role]++;
		}
		stats->applied_bias_ns += bias;
	}
	if (detailed_accounting) {
		global = flow_global();
		if (global)
			global->tracked_enqueues++;
	}
}

static __always_inline bool
flow_background_should_delay(struct flow_v2_task_meta *meta,
			     const struct flow_v2_session_config *cfg)
{
	struct flow_v2_session_stats *stats;
	__u64 now, last;

	if (!background_enabled || !meta || !cfg || !cfg->background_delay_ns)
		return false;
	now = bpf_ktime_get_ns();
	if (meta->flags & FLOW_V2_TASK_STALE_DELAYED) {
		last = meta->last_run_at_ns;
		if (last < meta->stale_delay_started_ns)
			last = meta->stale_delay_started_ns;
	} else {
		last = meta->last_run_at_ns ?: meta->birth_ns;
	}
	if (last && now - last >= cfg->background_delay_ns) {
		if (!(meta->flags & FLOW_V2_TASK_BACKGROUND_RESCUED)) {
			meta->flags |= FLOW_V2_TASK_BACKGROUND_RESCUED;
			stats = flow_session_stats(meta->session_id);
			if (stats)
				stats->background_rescues++;
		}
		return false;
	}
	/* 新一轮延后从这里开始；任务真正重新运行时核对实际排队时间。 */
	meta->flags &= ~FLOW_V2_TASK_BACKGROUND_RESCUED;
	return true;
}

/* scx_flatcg 会通过此函数把任务放入会话自己的队列。 */
static __always_inline void
flow_v2_insert_vtime(struct task_struct *p, __u64 dsq_id, __u64 slice_ns,
			    __u64 vtime, __u64 enq_flags)
{
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_task_meta *meta;
	__u32 role = FLOW_V2_ROLE_NORMAL;

	if (!flow_policy_enabled || !flow_session_enabled(dsq_id, &cfg)) {
		scx_bpf_dsq_insert_vtime(p, dsq_id, slice_ns, vtime, enq_flags);
		return;
	}

	meta = flow_lookup_or_create_task(p, dsq_id, cfg);
	if (!meta) {
		scx_bpf_dsq_insert_vtime(p, dsq_id, slice_ns, vtime, enq_flags);
		return;
	}
	flow_adopt_from_parent(meta);
	flow_refresh_tool_role(meta);
	flow_refresh_stale_delay(meta, cfg);
	flow_refresh_return_role(p, meta);
	role = meta->role < FLOW_V2_NR_ROLES ? meta->role : FLOW_V2_ROLE_NORMAL;
	if (role == FLOW_V2_ROLE_BACKGROUND &&
	    flow_background_should_delay(meta, cfg))
		vtime += cfg->background_delay_ns;
	flow_record_enqueue(meta, role, 0);
	scx_bpf_dsq_insert_vtime(p, dsq_id, slice_ns, vtime, enq_flags);
}

/*
 * 复用 scx_flatcg 的层级权重、红黑树和每会话队列实现，只替换最终的
 * struct_ops 定义，并在会话队列插入点加入角色顺序。
 */
#undef scx_bpf_dsq_insert_vtime
#define scx_bpf_dsq_insert_vtime flow_v2_insert_vtime
#undef SCX_OPS_DEFINE
#define SCX_OPS_DEFINE(...)
#include "../../../lock-sched/tools/sched_ext/scx_flatcg.bpf.c"
#undef scx_bpf_dsq_insert_vtime
#undef SCX_OPS_DEFINE
#define SCX_OPS_DEFINE(__name, ...) \
	SEC(".struct_ops.link") struct sched_ext_ops __name = { __VA_ARGS__, }

/* 与控制组编号空间隔离的共享优先队列。 */
#define FLOW_V2_SHARED_DSQ (1ULL << 62)
/* 已由真实阻塞等待确认、且已经取得会话预算的短运行任务。 */
#define FLOW_V2_PROGRESS_DSQ ((1ULL << 62) + 1)
/* 从真实阻塞等待中恢复的工具根只在这里运行一次。 */
#define FLOW_V2_INTERACTIVE_DSQ ((1ULL << 62) + 2)

static __u64 flow_v2_shared_vtime;

static __always_inline bool flow_managed_cpu(__s32 cpu)
{
	return cpu >= managed_cpu_first &&
	       cpu < managed_cpu_first + managed_cpu_count;
}

static int flow_quota_timer_fn(void *map, __u32 *key,
			       struct bpf_timer *timer)
{
	__u32 cpu;

	(void)map;
	(void)key;
	(void)timer;
	quota_timer_wakeup_ns = 0;
	bpf_for(cpu, managed_cpu_first, managed_cpu_first + managed_cpu_count)
		scx_bpf_kick_cpu((__s32)cpu, SCX_KICK_IDLE);
	return 0;
}

static __always_inline void flow_arm_quota_timer(__u64 deadline_ns,
					  __u64 now)
{
	struct flow_v2_quota_timer_state *state;
	__u64 observed;
	__u32 zero = 0;

	if (deadline_ns <= now)
		deadline_ns = now + 1;
	observed = quota_timer_wakeup_ns;
	bpf_repeat(4) {
		__u64 previous;

		if (observed && observed <= deadline_ns)
			return;
		previous = observed;
		observed = __sync_val_compare_and_swap(&quota_timer_wakeup_ns,
						       previous, deadline_ns);
		if (observed == previous)
			break;
	}
	if (quota_timer_wakeup_ns != deadline_ns)
		return;
	state = bpf_map_lookup_elem(&quota_timer, &zero);
	if (!state || bpf_timer_start(&state->timer, deadline_ns - now, 0))
		quota_timer_wakeup_ns = 0;
}

/*
 * 当所有会话权重相同时，每个可运行会话先保留一个处理器周期，剩余
 * 处理器时间按当前可运行任务数分配。它不预测程序名称或剩余时间，
 * 只利用调度器已经维护的即时需求；会话数不少于处理器数时仍使用
 * 原有权重公平规则。
 */
static __attribute__((noinline)) __u64
flow_demand_fair_quota(const struct flow_v2_session_config *cfg,
			       const struct flow_v2_width_state *state,
			       __u64 period)
{
	struct flow_v2_session_runtime *session_runtimep;
	__u64 extra_capacity_ns;
	__u64 quota;
	__u32 active_sessions;
	__u32 total_demand;
	__u32 own_demand;
	__u32 total_excess;
	__u32 own_excess;
	__u32 remaining_cpus;

	if (!demand_aware_borrow || !cfg || !state || !period ||
	    !managed_cpu_count || !flow_registered_weights_are_equal(cfg))
		return 0;
	/* 处理器利用不足四分之三时，原有公平路径已经能够满足需求。 */
	if (!flow_has_high_cpu_pressure(state))
		return 0;
	session_runtimep = flow_session_runtime(cfg->session_id);
	if (!session_runtimep)
		return 0;
	active_sessions = flow_effective_runnable_sessions(state);
	total_demand = logarithmic_demand_share ?
		flow_effective_runnable_score(state) :
		flow_effective_runnable_demand(state);
	if (!active_sessions || active_sessions >= managed_cpu_count ||
	    total_demand <= active_sessions)
		return 0;
	own_demand = logarithmic_demand_share ?
		(session_runtimep->runnable_tasks ?
		 1 + log2_u32(session_runtimep->runnable_tasks) : 0) :
			session_runtimep->runnable_tasks;
	if (!own_demand)
		return 0;
	remaining_cpus = managed_cpu_count - active_sessions;
	total_excess = total_demand - active_sessions;
	own_excess = own_demand > 1 ? own_demand - 1 : 0;
	extra_capacity_ns = period * remaining_cpus;
	quota = period + extra_capacity_ns * own_excess / total_excess;
	if (quota > period * managed_cpu_count)
		quota = period * managed_cpu_count;
	return quota;
}

/*
 * 会话公平性按实际取得的处理器时间核算，而不是用整数个处理器软
 * 上限近似。周期与已有策略统计周期一致；可运行会话集合改变时立即
 * 开始新一轮核算，空闲会话因此不会阻止其他会话使用空闲处理器。
 */
static __attribute__((noinline)) bool
flow_reserve_fair_service_cfg(const struct flow_v2_session_config *cfg,
			      __u64 reserve_ns, bool *reserved)
{
	struct flow_v2_fair_runtime *runtime;
	struct flow_v2_width_state *state;
	__u64 active_weight, epoch, period, quota, window_id;
	__u64 deadline = 0;
	__u64 now = bpf_ktime_get_ns();
	__u32 zero = 0;
	bool allowed = true;

	if (reserved)
		*reserved = false;
	if (!cfg || !reserve_ns || !managed_cpu_count)
		return true;
	if (!flow_periodic_fair_needed(cfg))
		return true;
	state = bpf_map_lookup_elem(&width_state, &zero);
	if (!state)
		return true;
	runtime = bpf_map_lookup_elem(&session_fair_runtime, &cfg->session_id);
	if (!runtime) {
		flow_record_map_update_failure();
		return true;
	}
	active_weight = flow_effective_runnable_weight(state) ?:
		flow_session_effective_weight(cfg);
	epoch = flow_effective_runnable_epoch(state);
	period = cfg->policy_window_ns ?: 100ULL * 1000 * 1000;
	window_id = now / period;
	quota = period * (managed_cpu_count ?: 1) *
		flow_session_effective_weight(cfg) /
		(active_weight ?: 1);
	{
		__u64 demand_quota = flow_demand_fair_quota(cfg, state, period);

		if (demand_quota)
			quota = demand_quota;
	}
	if (quota < reserve_ns)
		quota = reserve_ns;
	bpf_spin_lock(&runtime->lock);
	if (runtime->window_id != window_id ||
	    runtime->runnable_epoch != epoch) {
		runtime->window_id = window_id;
		runtime->runnable_epoch = epoch;
		runtime->used_ns = 0;
		runtime->reserved_ns = 0;
	}
	if (runtime->used_ns >= quota || runtime->reserved_ns >=
	    quota - runtime->used_ns || reserve_ns > quota - runtime->used_ns -
	    runtime->reserved_ns) {
		allowed = false;
		deadline = (window_id + 1) * period;
	} else {
		runtime->reserved_ns += reserve_ns;
	}
	bpf_spin_unlock(&runtime->lock);
	if (!allowed)
		flow_arm_quota_timer(deadline, now);
	else if (reserved)
		*reserved = true;
	return allowed;
}

static __always_inline bool
flow_reserve_fair_service(__u64 cgid, __u64 reserve_ns, bool *reserved)
{
	return flow_reserve_fair_service_cfg(
		bpf_map_lookup_elem(&session_config, &cgid), reserve_ns, reserved);
}

static __always_inline void
flow_release_fair_service(__u64 cgid, __u64 reserve_ns)
{
	struct flow_v2_fair_runtime *runtime;
	struct flow_v2_session_config *cfg;

	if (!reserve_ns)
		return;
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg)
		return;
	runtime = bpf_map_lookup_elem(&session_fair_runtime, &cfg->session_id);
	if (!runtime)
		return;
	bpf_spin_lock(&runtime->lock);
	if (runtime->reserved_ns >= reserve_ns)
		runtime->reserved_ns -= reserve_ns;
	else
		runtime->reserved_ns = 0;
	bpf_spin_unlock(&runtime->lock);
}

static __always_inline void
flow_charge_fair_service(const struct flow_v2_session_config *cfg,
			 struct flow_v2_fair_runtime *runtime,
			 struct flow_v2_quota_task_state *task_state,
			 __u64 now)
{
	struct flow_v2_session_adaptive_state *adaptive;
	struct flow_v2_width_state *state;
	__u64 current_window;
	__u64 current_epoch;
	__u64 period;
	__u64 running_since;
	__u64 charge_start;
	__u64 charged_ns = 0;
	__u32 zero = 0;

	if (!cfg || !runtime || !task_state ||
	    !task_state->fair_running_since_ns ||
	    now <= task_state->fair_running_since_ns)
		return;
	state = bpf_map_lookup_elem(&width_state, &zero);
	if (!state)
		return;
	period = cfg->policy_window_ns ?: 100ULL * 1000 * 1000;
	current_window = now / period;
	current_epoch = flow_effective_runnable_epoch(state);
	running_since = task_state->fair_running_since_ns;
	charge_start = running_since;
	if (charge_start < current_window * period)
		charge_start = current_window * period;
	bpf_spin_lock(&runtime->lock);
	if (runtime->window_id != current_window ||
	    runtime->runnable_epoch != current_epoch) {
		runtime->window_id = current_window;
		runtime->runnable_epoch = current_epoch;
		runtime->used_ns = 0;
		runtime->reserved_ns = 0;
	}
	if (task_state->fair_window_id == current_window &&
	    task_state->fair_runnable_epoch == current_epoch) {
		if (runtime->reserved_ns >= task_state->fair_reserved_ns)
			runtime->reserved_ns -= task_state->fair_reserved_ns;
		else
			runtime->reserved_ns = 0;
	}
	if (task_state->fair_runnable_epoch == current_epoch &&
	    charge_start < now) {
		charged_ns = now - charge_start;
		runtime->used_ns += charged_ns;
	}
	bpf_spin_unlock(&runtime->lock);
	/* 临时加权与公平周期独立计费。这里在任务停止运行时结算；
	 * 其他 CPU 尚在执行的片段仍要等其停止后才能结算。 */
	if (!automatic_session_weight ||
	    (!charged_ns && !independent_weight_service && !observe_weight_service))
		return;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (adaptive && adaptive->adaptive_weight_active &&
	    adaptive->automatic_weight_service_limit_ns) {
		__u64 active_start = adaptive->adaptive_weight_started_ns;
		__u64 lease_start = running_since;
		__u64 lease_service, applied_service = charged_ns;
		__u64 service;

		/* Fair-period rollover or a runnable-set change must not erase
		 * service already received by an active weight grant. A grant
		 * created during this execution cannot charge earlier execution. */
		if (lease_start < active_start)
			lease_start = active_start;
		lease_service = active_start && now > lease_start ? now - lease_start : 0;
		if (independent_weight_service)
			applied_service = lease_service;
		if (observe_weight_service) {
			__u32 cpu = bpf_get_smp_processor_id();

			if (cpu < 256) {
				local_schedule_stats[cpu][4] += lease_service;
				local_schedule_stats[cpu][5] += charged_ns;
				local_schedule_stats[cpu][6] += applied_service;
			}
		}
		if (!applied_service)
			return;
		service = __sync_add_and_fetch(
			&adaptive->automatic_weight_service_ns, applied_service);
		if (service >= adaptive->automatic_weight_service_limit_ns)
			flow_update_adaptive_weight(cfg, adaptive);
	}
}

static __attribute__((noinline)) bool
flow_reserve_fair_for_task(struct task_struct *p, __u64 cgid,
			   __u64 reserve_ns)
{
	struct flow_v2_quota_task_state *task_state;
	bool reserved = false;

	if (!flow_reserve_fair_service(cgid, reserve_ns, &reserved))
		return false;
	if (!reserved)
		return true;
	task_state = bpf_task_storage_get(&quota_task_state, p, 0, 0);
	if (!task_state) {
		flow_release_fair_service(cgid, reserve_ns);
		return false;
	}
	task_state->fair_reserved_ns = reserve_ns;
	{
		struct flow_v2_session_config *cfg =
			bpf_map_lookup_elem(&session_config, &cgid);
		struct flow_v2_fair_runtime *runtime = cfg ?
			bpf_map_lookup_elem(&session_fair_runtime,
					    &cfg->session_id) : NULL;

		if (runtime) {
			task_state->fair_window_id = runtime->window_id;
			task_state->fair_runnable_epoch = runtime->runnable_epoch;
		}
	}
	return true;
}

static __always_inline void
flow_release_fair_for_task(struct task_struct *p, __u64 cgid)
{
	struct flow_v2_quota_task_state *task_state;
	__u64 reserved_ns = 0;

	task_state = bpf_task_storage_get(&quota_task_state, p, 0, 0);
	if (task_state) {
		reserved_ns = task_state->fair_reserved_ns;
		task_state->fair_reserved_ns = 0;
		task_state->fair_running_since_ns = 0;
		task_state->fair_window_id = 0;
		task_state->fair_runnable_epoch = 0;
	}
	flow_release_fair_service(cgid, reserved_ns);
}

static __always_inline void
flow_set_dispatch_fair(__u64 cgid, __u64 reserved_ns)
{
	struct flow_v2_dispatch_fair_state *state;
	struct flow_v2_session_config *cfg;
	struct flow_v2_fair_runtime *runtime;
	__u32 zero = 0;

	if (!periodic_fair_accounting)
		return;
	state = bpf_map_lookup_elem(&dispatch_fair_state, &zero);
	if (!state)
		return;
	state->cgroup_id = cgid;
	state->reserved_ns = reserved_ns;
	state->window_id = 0;
	state->runnable_epoch = 0;
	if (!cgid)
		return;
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	runtime = cfg ? bpf_map_lookup_elem(&session_fair_runtime,
					    &cfg->session_id) : NULL;
	if (runtime) {
		state->window_id = runtime->window_id;
		state->runnable_epoch = runtime->runnable_epoch;
	}
}

static __always_inline void
flow_take_dispatch_fair(__u64 cgid,
			struct flow_v2_quota_task_state *task_state,
			struct flow_v2_fair_runtime *runtime)
{
	struct flow_v2_dispatch_fair_state *state;
	struct flow_v2_session_config *cfg;
	struct flow_v2_width_state *width;
	__u64 period;
	__u64 now;
	__u32 zero = 0;

	if (!task_state)
		return;
	if (!periodic_fair_accounting) {
		task_state->fair_reserved_ns = 0;
		task_state->fair_running_since_ns = 0;
		task_state->fair_window_id = 0;
		task_state->fair_runnable_epoch = 0;
		/* 关闭周期配额时仍要记录自动权重实际得到的处理器时间。它只
		 * 用于判断一次临时增加是否已经偿还服务差额，不会在派发路径
		 * 上拒绝任务，也不会恢复第二套处理器时间限制。 */
		if (!automatic_session_weight || !runtime || !cgid)
			return;
		cfg = bpf_map_lookup_elem(&session_config, &cgid);
		width = bpf_map_lookup_elem(&width_state, &zero);
		if (!cfg || !width)
			return;
		now = bpf_ktime_get_ns();
		period = cfg->policy_window_ns ?: 100ULL * 1000 * 1000;
		task_state->fair_window_id = now / period;
		task_state->fair_runnable_epoch =
			flow_effective_runnable_epoch(width);
		return;
	}
	state = bpf_map_lookup_elem(&dispatch_fair_state, &zero);
	if (state && state->cgroup_id == cgid) {
		task_state->fair_reserved_ns = state->reserved_ns;
		task_state->fair_window_id = state->window_id;
		task_state->fair_runnable_epoch = state->runnable_epoch;
	} else if (runtime) {
		task_state->fair_reserved_ns = 0;
		task_state->fair_window_id = runtime->window_id;
		task_state->fair_runnable_epoch = runtime->runnable_epoch;
	}
	if (state) {
		state->cgroup_id = 0;
		state->reserved_ns = 0;
		state->window_id = 0;
		state->runnable_epoch = 0;
	}
}

static __always_inline bool flow_session_quota_exhausted(__u64 cgid)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_runtime *runtime;
	__u64 deadline = 0;
	__u64 now;
	bool exhausted = false;

	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg || !cfg->cpu_quota_ns || !cfg->cpu_period_ns)
		return false;
	runtime = flow_session_runtime(cfg->session_id);
	if (!runtime)
		return false;
	now = bpf_ktime_get_ns();
	bpf_spin_lock(&runtime->lock);
	if (!runtime->quota_period_start_ns ||
	    now < runtime->quota_period_start_ns ||
	    now - runtime->quota_period_start_ns >= cfg->cpu_period_ns) {
		if (runtime->quota_period_start_ns &&
		    now >= runtime->quota_period_start_ns) {
			__u64 periods = (now - runtime->quota_period_start_ns) /
					cfg->cpu_period_ns;

			runtime->quota_period_start_ns += periods * cfg->cpu_period_ns;
		} else {
			runtime->quota_period_start_ns = now;
		}
		runtime->quota_used_ns = 0;
		runtime->quota_reserved_ns = 0;
		runtime->quota_throttled = 0;
	}
	if (runtime->quota_used_ns + runtime->quota_reserved_ns >=
	    cfg->cpu_quota_ns) {
		exhausted = true;
		runtime->quota_throttled = 1;
		deadline = runtime->quota_period_start_ns + cfg->cpu_period_ns;
	}
	bpf_spin_unlock(&runtime->lock);
	if (exhausted)
		flow_arm_quota_timer(deadline, now);
	return exhausted;
}

/*
 * 多个处理器会同时从同一会话取任务。只在任务停止后累计运行时间，
 * 会让所有处理器在同一时刻都看到“尚有配额”，从而整批越过上限。
 * 因此派发前先预留一个普通时间片，任务停止时再按实际运行时间结算。
 */
static __attribute__((noinline)) bool
flow_reserve_session_quota_cfg(const struct flow_v2_session_config *cfg,
			       __u64 reserve_ns)
{
	struct flow_v2_session_runtime *runtime;
	__u64 deadline = 0;
	__u64 now;
	bool allowed = true;

	if (!cfg || !cfg->cpu_quota_ns || !cfg->cpu_period_ns)
		return true;
	runtime = flow_session_runtime(cfg->session_id);
	if (!runtime)
		return true;
	now = bpf_ktime_get_ns();
	bpf_spin_lock(&runtime->lock);
	if (!runtime->quota_period_start_ns ||
	    now < runtime->quota_period_start_ns ||
	    now - runtime->quota_period_start_ns >= cfg->cpu_period_ns) {
		if (runtime->quota_period_start_ns &&
		    now >= runtime->quota_period_start_ns) {
			__u64 periods = (now - runtime->quota_period_start_ns) /
					cfg->cpu_period_ns;

			runtime->quota_period_start_ns +=
				periods * cfg->cpu_period_ns;
		} else {
			runtime->quota_period_start_ns = now;
		}
		runtime->quota_used_ns = 0;
		runtime->quota_reserved_ns = 0;
		runtime->quota_throttled = 0;
	}
	if (runtime->quota_used_ns >= cfg->cpu_quota_ns ||
	    runtime->quota_reserved_ns >=
		cfg->cpu_quota_ns - runtime->quota_used_ns ||
	    reserve_ns > cfg->cpu_quota_ns - runtime->quota_used_ns -
			 runtime->quota_reserved_ns) {
		allowed = false;
		runtime->quota_throttled = 1;
		deadline = runtime->quota_period_start_ns + cfg->cpu_period_ns;
	} else {
		runtime->quota_reserved_ns += reserve_ns;
	}
	bpf_spin_unlock(&runtime->lock);
	if (allowed)
		return true;
	flow_arm_quota_timer(deadline, now);
	return false;
}

static __always_inline bool
flow_reserve_session_quota(__u64 cgid, __u64 reserve_ns)
{
	return flow_reserve_session_quota_cfg(
		bpf_map_lookup_elem(&session_config, &cgid), reserve_ns);
}

static __always_inline void
flow_record_quota_rejection(__u64 cgid, bool direct)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;

	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg)
		return;
	stats = flow_session_stats(cfg->session_id);
	if (!stats)
		return;
	if (direct)
		stats->quota_direct_rejections++;
	else
		stats->quota_dispatch_rejections++;
}

static __always_inline void
flow_release_session_quota(__u64 cgid, __u64 reserve_ns);

static __attribute__((noinline)) bool
flow_reserve_quota_for_task(struct task_struct *p, __u64 cgid,
			    __u64 reserve_ns, bool direct)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_quota_task_state *task_state;

	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg || !cfg->cpu_quota_ns || !cfg->cpu_period_ns)
		return true;
	if (!flow_reserve_session_quota(cgid, reserve_ns)) {
		flow_record_quota_rejection(cgid, direct);
		return false;
	}
	task_state = bpf_task_storage_get(&quota_task_state, p, 0, 0);
	if (!task_state) {
		flow_release_session_quota(cgid, reserve_ns);
		return false;
	}
	task_state->reserved_ns = reserve_ns;
	return true;
}

static __always_inline void
flow_release_session_quota(__u64 cgid, __u64 reserve_ns)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_runtime *runtime;

	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg || !cfg->cpu_quota_ns || !reserve_ns)
		return;
	runtime = flow_session_runtime(cfg->session_id);
	if (!runtime)
		return;
	bpf_spin_lock(&runtime->lock);
	if (runtime->quota_reserved_ns >= reserve_ns)
		runtime->quota_reserved_ns -= reserve_ns;
	else
		runtime->quota_reserved_ns = 0;
	bpf_spin_unlock(&runtime->lock);
}

static __always_inline void
flow_charge_session_quota(__u64 cgid, const struct flow_v2_session_config *cfg,
			  struct flow_v2_session_runtime *runtime,
			  __u64 runtime_ns, __u64 reserved_ns)
{
	struct flow_v2_session_stats *stats;
	__u64 now = bpf_ktime_get_ns();
	bool newly_throttled = false;

	if (!cfg || !runtime || !runtime_ns || !cfg->cpu_quota_ns ||
	    !cfg->cpu_period_ns)
		return;
	bpf_spin_lock(&runtime->lock);
	if (!runtime->quota_period_start_ns ||
	    now < runtime->quota_period_start_ns ||
	    now - runtime->quota_period_start_ns >= cfg->cpu_period_ns) {
		runtime->quota_period_start_ns = now;
		runtime->quota_used_ns = 0;
		runtime->quota_reserved_ns = 0;
		runtime->quota_throttled = 0;
	}
	if (runtime->quota_reserved_ns >= reserved_ns)
		runtime->quota_reserved_ns -= reserved_ns;
	else
		runtime->quota_reserved_ns = 0;
	runtime->quota_used_ns += runtime_ns;
	if (runtime->quota_used_ns >= cfg->cpu_quota_ns &&
	    !runtime->quota_throttled) {
		runtime->quota_throttled = 1;
		newly_throttled = true;
	}
	bpf_spin_unlock(&runtime->lock);
	stats = flow_session_stats(cfg->session_id);
	if (stats) {
		stats->quota_runtime_ns += runtime_ns;
		if (newly_throttled)
			stats->quota_throttles++;
	}
	(void)cgid;
}

static __always_inline void
flow_update_running_peak(struct flow_v2_session_runtime *runtime, __u32 running)
{
	__u32 observed;

	if (!runtime)
		return;
	observed = runtime->peak_running_tasks;
	bpf_repeat(4) {
		if (running <= observed)
			break;
		observed = __sync_val_compare_and_swap(
			&runtime->peak_running_tasks, observed, running);
	}
}

/*
 * 只用当前可运行进程数计算会话本轮使用的权重。大部分处理器时间仍
 * 在活动会话间等分，只有余量随即时需求变化。该函数不读取程序名，
 * 也不预测未来进程数。单独保留为 BPF 子程序，避免在调度入口的多个
 * 分支中重复展开同一段计算而超过内核校验上限。
 */
static __attribute__((noinline)) __u64
flow_session_scheduling_weight(__u64 cgid,
			       const struct flow_v2_session_config *cfg)
{
	__u64 weight = flow_session_effective_weight(cfg);
	(void)cgid;

	if (balanced_demand_share && cfg &&
	    flow_registered_weights_are_equal(cfg)) {
		struct flow_v2_session_runtime *runtime;
		struct flow_v2_width_state *state;
		__u64 total_units;
		__u64 protected_units;
		__u64 own_units;
		__u64 adjusted;
		__u32 active_sessions;
		__u32 total_demand;
		__u32 zero = 0;
		struct flow_v2_control_state *control;

		state = bpf_map_lookup_elem(&width_state, &zero);
		control = bpf_map_lookup_elem(&control_state, &zero);
		runtime = flow_session_runtime(cfg->session_id);
		active_sessions = flow_effective_runnable_sessions(state);
		total_demand = flow_effective_runnable_demand(state);
		if (state && runtime && runtime->runnable_tasks &&
		    flow_has_high_cpu_pressure(state) && active_sessions &&
		    active_sessions < managed_cpu_count &&
		    control && control->registered_sessions <
			control->peak_registered_sessions &&
		    total_demand > active_sessions) {
			/* 以 1024 为一个处理器时间分配单位。每个活动会话先保留
			 * 最初平均得到的处理器时间；已经结束的会话所释放时间，一半继续
			 * 等分给剩余会话，一半按当前可运行进程数分配。 */
			total_units = (__u64)managed_cpu_count * 1024;
			protected_units = total_units /
				control->peak_registered_sessions;
			protected_units +=
				(total_units - protected_units * active_sessions) /
				2 / active_sessions;
			own_units = protected_units +
				(total_units - protected_units * active_sessions) *
				runtime->runnable_tasks / total_demand;
			adjusted = weight * own_units * active_sessions /
				(total_units ?: 1);
			if (adjusted)
				weight = adjusted;
		}
	} else if (logarithmic_demand_share && cfg) {
		struct flow_v2_session_runtime *runtime;
		struct flow_v2_width_state *state;
		__u64 adjusted;
		__u32 active_sessions;
		__u32 total_score;
		__u32 own_score;
		__u32 zero = 0;

		state = bpf_map_lookup_elem(&width_state, &zero);
		runtime = flow_session_runtime(cfg->session_id);
		active_sessions = flow_effective_runnable_sessions(state);
		total_score = flow_effective_runnable_score(state);
		own_score = runtime && runtime->runnable_tasks ?
			1 + log2_u32(runtime->runnable_tasks) : 0;
		if (state && own_score && active_sessions &&
		    active_sessions < managed_cpu_count &&
		    total_score > active_sessions) {
			/* 对数缩放保留并行需求差异，同时避免进程数极多的会话
			 * 按线性比例占用几乎全部处理器时间。 */
			adjusted = weight * own_score * active_sessions /
				(total_score ?: 1);
			if (adjusted)
				weight = adjusted;
		}
	}
	return weight ?: 1;
}

/*
 * V2 的会话统一位于同一个受控父 cgroup 下，权重可以直接比较。
 * 使用 100 作为普通权重的单位，把会话服务时间统一换算为虚拟时间；
 * 这避免把宿主机其他层级的活动 cgroup 误算进会话间比例。
 */
static __always_inline __u64
flow_session_vtime_delta(__u64 cgid, __u64 runtime_ns)
{
	struct flow_v2_session_config *cfg =
		bpf_map_lookup_elem(&session_config, &cgid);
	__u64 weight = flow_session_effective_weight(cfg);

	/* 实验路线关闭时不引入额外 BPF 子程序调用。 */
	if (balanced_demand_share || logarithmic_demand_share)
		weight = flow_session_scheduling_weight(cgid, cfg);

	return runtime_ns * 100 / weight;
}

static __always_inline __u64 flow_session_max_credit(__u64 cgid)
{
	struct flow_v2_session_config *cfg =
		bpf_map_lookup_elem(&session_config, &cgid);
	__u64 weight = flow_session_effective_weight(cfg);
	__u64 credit = cgrp_slice_ns * weight / 100;
	__u64 limit = cgrp_slice_ns * (managed_cpu_count ?: 1);

	/* 权重较大的会话需要足够余额在多个处理器上并行取得相应比例。 */
	return credit > limit ? limit : (credit ?: 1);
}

static __always_inline void
flow_cap_session_budget(struct cgv_node *node, struct fcg_cgrp_ctx *cgc,
			__u64 max_credit)
{
	__s64 delta = __sync_fetch_and_sub(&cgc->cvtime_delta,
					  cgc->cvtime_delta);
	__u64 cvtime = node->cvtime + delta;

	/* 空闲会话最多保留一个本会话时间片的优势。 */
	if (time_before(cvtime, cvtime_now - max_credit))
		cvtime = cvtime_now - max_credit;
	node->cvtime = cvtime;
}

static __always_inline void
flow_session_enqueued(struct cgroup *cgrp, struct fcg_cgrp_ctx *cgc)
{
	struct cgv_node_stash *stash;
	struct cgv_node *node;
	__u64 cgid = cgrp->kn->id;
	__u64 max_credit = flow_session_max_credit(cgid);

	if (__sync_val_compare_and_swap(&cgc->queued, 0, 1))
		return;
	stash = bpf_map_lookup_elem(&cgv_node_stash, &cgid);
	if (!stash) {
		scx_bpf_error("flowcausal cgroup node missing for cgid %llu", cgid);
		return;
	}
	node = bpf_kptr_xchg(&stash->node, NULL);
	if (!node)
		return;
	bpf_spin_lock(&cgv_tree_lock);
	flow_cap_session_budget(node, cgc, max_credit);
	bpf_rbtree_add(&cgv_tree, &node->rb_node, cgv_node_less);
	bpf_spin_unlock(&cgv_tree_lock);
}

static __always_inline bool
flow_parallel_width_has_evidence(__u64 cgid,
				 const struct flow_v2_session_config *cfg)
{
	struct flow_v2_session_runtime *session_runtimep;
	struct flow_v2_tool_runtime *tool_runtimep;
	struct flow_v2_active_tool *active;

	if (!parallel_width_enabled || !flow_policy_enabled || !cfg)
		return false;
	if (flow_tool_decision())
		return false;
	if (flow_kernel_decision() || session_runnable_width) {
		session_runtimep = flow_session_runtime(cfg->session_id);
		return session_runtimep && session_runtimep->runnable_tasks > 1;
	}
	active = bpf_map_lookup_elem(&active_tool_by_cgroup, &cgid);
	if (!active || !active->active || active->session_id != cfg->session_id ||
	    active->behavior != FLOW_V2_BEHAVIOR_PARALLEL)
		return false;
	tool_runtimep = flow_tool_runtime_lookup(active->session_id,
						 active->tool_id);
	return tool_runtimep && tool_runtimep->active &&
	       tool_runtimep->sequence == active->sequence &&
	       tool_runtimep->runnable_tasks > 1;
}

/*
 * 并行工具必须同时满足工具类别和内核观察到的多任务可运行事实。
 * 多会话竞争时按会话权重估计可同时占用的处理器数；单会话运行时
 * 目标自动扩展到全部受管处理器，不降低独立构建的并行度。
 */
static __attribute__((noinline)) __u32
flow_parallel_width_target(__u64 wanted_cgid,
			   const struct flow_v2_session_config *wanted_cfg,
			   struct flow_v2_session_runtime *wanted_runtime)
{
	struct flow_v2_width_state *state;
	__u64 active_weight;
	__u64 wanted_weight;
	__u32 zero = 0;
	__u32 target = 1;

	if (!wanted_cfg || !managed_cpu_count)
		return 1;
	/* 与会话虚拟运行时间使用同一份即时权重。这里用一个名义时间片
	 * 反推权重，不预测后续会创建多少进程。 */
	wanted_weight = flow_session_effective_weight(wanted_cfg);
	if (balanced_demand_share || logarithmic_demand_share) {
		__u64 nominal = 1000000;
		__u64 delta = flow_session_vtime_delta(wanted_cgid, nominal);

		if (delta)
			wanted_weight = nominal * 100 / delta;
	}
	state = bpf_map_lookup_elem(&width_state, &zero);
	active_weight = flow_effective_runnable_weight(state);
	if (demand_aware_borrow && !balanced_demand_share &&
	    !logarithmic_demand_share && state &&
	    flow_registered_weights_are_equal(wanted_cfg)) {
		__u32 active_sessions = flow_effective_runnable_sessions(state);
		__u32 total_demand = logarithmic_demand_share ?
			flow_effective_runnable_score(state) :
			flow_effective_runnable_demand(state);

		if (wanted_runtime && active_sessions &&
		    flow_has_high_cpu_pressure(state) &&
		    active_sessions < managed_cpu_count &&
		    total_demand > active_sessions &&
		    wanted_runtime->runnable_tasks) {
			__u32 remaining = managed_cpu_count - active_sessions;
			__u32 total_excess = total_demand - active_sessions;
			__u32 own_demand = logarithmic_demand_share ?
				1 + log2_u32(wanted_runtime->runnable_tasks) :
				wanted_runtime->runnable_tasks;
			__u32 own_excess = own_demand > 1 ? own_demand - 1 : 0;

			target = 1 + (remaining * own_excess +
				      total_excess - 1) / total_excess;
			if (target > wanted_runtime->runnable_tasks)
				target = wanted_runtime->runnable_tasks;
			if (target > managed_cpu_count)
				target = managed_cpu_count;
			wanted_runtime->current_width = target ?: 1;
			return target ?: 1;
		}
	}
	if (!state || active_weight <= wanted_weight)
		target = managed_cpu_count;
	else
		target = (__u32)(((__u64)managed_cpu_count * wanted_weight +
				active_weight - 1) / active_weight);
	if (!target)
		target = 1;
	if (target > managed_cpu_count)
		target = managed_cpu_count;
	if (wanted_runtime) {
		if (balanced_demand_share && wanted_runtime->runnable_tasks &&
		    target > wanted_runtime->runnable_tasks)
			target = wanted_runtime->runnable_tasks;
		wanted_runtime->current_width = target;
	}
	(void)wanted_cgid;
	return target;
}

/*
 * 多个处理器可能同时从同一会话取任务。取出任务之前先占一个位置，
 * running() 再把它转换成实际运行数，避免并发派发一起越过软上限。
 */
static __attribute__((noinline)) bool
flow_reserve_dispatch_slot_cfg(__u64 cgid,
			       const struct flow_v2_session_config *cfg,
			       bool *parallel_limit, bool *reserved)
{
	struct flow_v2_session_runtime *runtime;
	__u32 target;
	bool allowed = true;

	*reserved = false;
	if (!flow_config_enabled(cfg) || !parallel_width_enabled ||
	    !flow_policy_enabled || flow_tool_decision())
		return true;
	runtime = flow_session_runtime(cfg->session_id);
	if (!runtime)
		return true;
	if (flow_kernel_decision() || session_runnable_width) {
		if (runtime->runnable_tasks <= 1)
			return true;
	} else if (!flow_parallel_width_has_evidence(cgid, cfg)) {
		return true;
	}
	target = flow_parallel_width_target(cgid, cfg, runtime);
	bpf_spin_lock(&runtime->lock);
	if (runtime->running_tasks + runtime->reserved_tasks >= target) {
		allowed = false;
	} else {
		runtime->reserved_tasks++;
		*reserved = true;
	}
	bpf_spin_unlock(&runtime->lock);
	if (parallel_limit)
		*parallel_limit = true;
	return allowed;
}

static __always_inline bool
flow_reserve_dispatch_slot(__u64 cgid, bool *parallel_limit, bool *reserved)
{
	return flow_reserve_dispatch_slot_cfg(cgid,
		bpf_map_lookup_elem(&session_config, &cgid), parallel_limit, reserved);
}

static __always_inline void flow_release_dispatch_slot(__u64 cgid)
{
	struct flow_v2_session_runtime *runtime;
	struct flow_v2_session_config *cfg = NULL;

	if (!flow_session_enabled(cgid, &cfg))
		return;
	runtime = bpf_map_lookup_elem(&session_runtime, &cfg->session_id);
	if (runtime)
		flow_session_dec_reserved(runtime);
}

static __always_inline void
flow_record_width_limit(__u64 cgid, bool borrowed, bool parallel_limit)
{
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_session_stats *stats;

	if (!flow_session_enabled(cgid, &cfg))
		return;
	stats = flow_session_stats(cfg->session_id);
	if (!stats)
		return;
	if (parallel_limit) {
		if (borrowed)
			stats->parallel_width_borrows++;
		else
			stats->parallel_width_limits++;
	} else if (borrowed) {
		stats->session_width_borrows++;
	} else {
		stats->session_width_limits++;
	}
}

static __always_inline void
flow_record_fair_dispatch(__u64 cgid, bool borrowed)
{
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_session_stats *stats;

	if (!flow_session_enabled(cgid, &cfg))
		return;
	stats = flow_session_stats(cfg->session_id);
	if (!stats)
		return;
	if (borrowed)
		stats->fair_dispatch_borrows++;
	else
		stats->fair_dispatch_rejections++;
}

static __always_inline void flow_record_adaptive_weight_dispatch(__u64 cgid)
{
	struct flow_v2_session_adaptive_state *adaptive;
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_stats *stats;

	if (!adaptive_session_weight)
		return;
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg)
		return;
	adaptive = bpf_map_lookup_elem(&session_adaptive_state,
				       &cfg->session_id);
	if (!adaptive || adaptive->effective_weight <= (cfg->weight ?: 100))
		return;
	stats = flow_session_stats(cfg->session_id);
	if (stats)
		__sync_fetch_and_add(&stats->adaptive_weight_dispatches, 1);
}

static __always_inline void flow_record_width_skip(__u64 cgid, __u64 amount)
{
	__u64 *value;

	value = bpf_map_lookup_elem(&session_width_skip_ns, &cgid);
	if (value) {
		__sync_fetch_and_add(value, amount);
	} else {
		bpf_map_update_elem(&session_width_skip_ns, &cgid, &amount,
				    BPF_NOEXIST);
	}
}

static __always_inline __u64 flow_take_width_skip(__u64 cgid)
{
	__u64 *value = bpf_map_lookup_elem(&session_width_skip_ns, &cgid);

	return value ? __sync_lock_test_and_set(value, 0) : 0;
}

/*
 * 只有确有空闲能力时才借用别的会话未用完的运行时间。判断采用会话
 * 当前可运行任务数：若所有会话都能用满按权重分配的处理器时间，就继续按
 * 权重等待；若某个会话任务数不足，才允许需求更高的会话使用余量。
 */
static __attribute__((noinline)) bool
flow_session_can_borrow_unused(__u64 borrower_cgid)
{
	struct flow_v2_session_runtime *borrower_runtime;
	struct flow_v2_session_config *borrower_cfg;
	struct flow_v2_width_state *state;
	__u64 active_weight;
	__u32 borrower_target;
	__u32 demanded_cpus = 0;
	__u32 zero = 0;
	int i;

	state = bpf_map_lookup_elem(&width_state, &zero);
	borrower_cfg = bpf_map_lookup_elem(&session_config, &borrower_cgid);
	if (!state || !borrower_cfg || !managed_cpu_count)
		return false;
	borrower_runtime = bpf_map_lookup_elem(
		&session_runtime, &borrower_cfg->session_id);
	active_weight = flow_effective_runnable_weight(state);
	if (!borrower_runtime || !active_weight)
		return false;
	borrower_target = (managed_cpu_count *
		flow_session_effective_weight(borrower_cfg) + active_weight - 1) /
		active_weight;
	if (borrower_target < 1)
		borrower_target = 1;
	if (borrower_runtime->runnable_tasks <= borrower_target)
		return false;

	bpf_for(i, 0, FLOW_V2_ACTIVE_SESSION_SLOTS) {
		struct flow_v2_session_runtime *runtime;
		struct flow_v2_session_config *cfg;
		__u64 *cgid;
		__u32 target;
		__u32 demand;
		__u32 in_service;

		cgid = bpf_map_lookup_elem(&active_session_slots, &i);
		if (!cgid || !*cgid)
			continue;
		cfg = bpf_map_lookup_elem(&session_config, cgid);
		if (!cfg || !(cfg->flags & FLOW_V2_SESSION_ENABLED))
			continue;
		runtime = bpf_map_lookup_elem(&session_runtime, &cfg->session_id);
		if (!runtime || !runtime->runnable_tasks)
			continue;
		target = (managed_cpu_count * flow_session_effective_weight(cfg) +
			  active_weight - 1) / active_weight;
		if (target < 1)
			target = 1;
		demand = runtime->runnable_tasks;
		if (demand > target)
			demand = target;
		/*
		 * “可以借用”不仅要求总需求小于处理器数，还要求其他会话
		 * 当前已经拿到它能够使用的处理器。否则宽会话会在窄会话
		 * 已经可运行、尚未被派发的短窗口里抢先借走处理器，使单线程
		 * 会话长期只能得到不足一个处理器的时间。
		 */
		in_service = runtime->running_tasks + runtime->reserved_tasks;
		if (*cgid != borrower_cgid && in_service < demand)
			return false;
		demanded_cpus += demand;
		if (demanded_cpus >= managed_cpu_count)
			return false;
	}
	return demanded_cpus < managed_cpu_count;
}

struct flow_v2_backlog_borrow_ctx {
	__u64 borrower_cgid;
	__u64 active_weight;
	__u32 borrower_weight;
	__u32 active_sessions;
	__u32 largest_surplus;
	__u32 blocked;
	__u32 mixed_weights;
};

static long flow_backlog_borrow_cb(__u32 i, void *data)
{
	struct flow_v2_backlog_borrow_ctx *ctx = data;
	struct flow_v2_session_runtime *runtime;
	struct flow_v2_session_config *cfg;
	__u64 *cgid;
	__u32 target;
	__u32 demand;
	__u32 in_service;
	__u32 surplus;

	cgid = bpf_map_lookup_elem(&active_session_slots, &i);
	if (!cgid || !*cgid)
		return 0;
	cfg = bpf_map_lookup_elem(&session_config, cgid);
	if (!cfg || !(cfg->flags & FLOW_V2_SESSION_ENABLED))
		return 0;
	runtime = bpf_map_lookup_elem(&session_runtime, &cfg->session_id);
	if (!runtime || !runtime->runnable_tasks)
		return 0;
	if (flow_session_effective_weight(cfg) != ctx->borrower_weight)
		ctx->mixed_weights = 1;
	ctx->active_sessions++;
	in_service = runtime->running_tasks + runtime->reserved_tasks;
	if (*cgid != ctx->borrower_cgid && !in_service) {
		ctx->blocked = 1;
		return 1;
	}
	target = (managed_cpu_count * flow_session_effective_weight(cfg) +
		  ctx->active_weight - 1) / ctx->active_weight;
	if (target < 1)
		target = 1;
	demand = runtime->runnable_tasks;
	if (demand > managed_cpu_count)
		demand = managed_cpu_count;
	surplus = demand > target ? demand - target : 0;
	if (surplus > ctx->largest_surplus)
		ctx->largest_surplus = surplus;
	return 0;
}

static __attribute__((noinline)) bool
flow_session_can_borrow_backlog(__u64 borrower_cgid)
{
	struct flow_v2_session_runtime *borrower_runtime;
	struct flow_v2_session_config *borrower_cfg;
	struct flow_v2_width_state *state;
	__u64 active_weight;
	__u32 borrower_target;
	__u32 borrower_demand;
	__u32 borrower_surplus;
	struct flow_v2_backlog_borrow_ctx ctx = {};
	__u32 zero = 0;

	state = bpf_map_lookup_elem(&width_state, &zero);
	borrower_cfg = bpf_map_lookup_elem(&session_config, &borrower_cgid);
	if (!state || !borrower_cfg || !managed_cpu_count)
		return false;
	borrower_runtime = bpf_map_lookup_elem(
		&session_runtime, &borrower_cfg->session_id);
	active_weight = flow_effective_runnable_weight(state);
	if (!borrower_runtime || !active_weight)
		return false;
	borrower_target = (managed_cpu_count *
		flow_session_effective_weight(borrower_cfg) + active_weight - 1) /
		active_weight;
	if (borrower_target < 1)
		borrower_target = 1;
	borrower_demand = borrower_runtime->runnable_tasks;
	if (borrower_demand > managed_cpu_count)
		borrower_demand = managed_cpu_count;
	if (borrower_demand <= borrower_target)
		return false;
	borrower_surplus = borrower_demand - borrower_target;
	ctx.borrower_cgid = borrower_cgid;
	ctx.active_weight = active_weight;
	ctx.borrower_weight = flow_session_effective_weight(borrower_cfg);
	bpf_loop(FLOW_V2_ACTIVE_SESSION_SLOTS, flow_backlog_borrow_cb, &ctx, 0);
	if (ctx.mixed_weights)
		return flow_session_can_borrow_unused(borrower_cgid);
	if (ctx.blocked || ctx.active_sessions >= managed_cpu_count)
		return false;
	return borrower_surplus >= ctx.largest_surplus;
}

static __always_inline bool flow_session_can_borrow(__u64 borrower_cgid)
{
	if (demand_aware_borrow)
		return flow_session_can_borrow_backlog(borrower_cgid);
	return flow_session_can_borrow_unused(borrower_cgid);
}

/* scx_flatcg 的会话选择保持不变，只在取任务前检查并行工具的占用数。 */
static __attribute__((noinline)) bool
flow_try_pick_next_cgroup(__u64 *cgidp, bool enforce_width)
{
	struct bpf_rb_node *rb_node;
	struct cgv_node_stash *stash;
	struct cgv_node *cgv_node;
	struct fcg_cgrp_ctx *cgc;
	struct cgroup *cgrp;
	struct flow_v2_session_config *cfg;
	__u64 charge, max_credit;
	__u64 cgid;
	bool parallel_limit = false;
	bool reserved = false;
	bool fair_reserved = false;

	bpf_spin_lock(&cgv_tree_lock);
	rb_node = bpf_rbtree_first(&cgv_tree);
	if (!rb_node) {
		bpf_spin_unlock(&cgv_tree_lock);
		*cgidp = 0;
		return true;
	}
	rb_node = bpf_rbtree_remove(&cgv_tree, rb_node);
	bpf_spin_unlock(&cgv_tree_lock);
	if (!rb_node) {
		scx_bpf_error("flowcausal cgroup node remove failed");
		return true;
	}
	cgv_node = container_of(rb_node, struct cgv_node, rb_node);
	cgid = cgv_node->cgid;
	if (time_before(cvtime_now, cgv_node->cvtime))
		cvtime_now = cgv_node->cvtime;
	cgrp = bpf_cgroup_from_id(cgid);
	if (!cgrp)
		goto out_free;
	cgc = bpf_cgrp_storage_get(&cgrp_ctx, cgrp, 0, 0);
	if (!cgc) {
		bpf_cgroup_release(cgrp);
		goto out_free;
	}
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	charge = flow_session_vtime_delta(cgid, cgrp_slice_ns);
	max_credit = flow_session_max_credit(cgid);
	if (!enforce_width && !flow_session_can_borrow(cgid)) {
		bpf_cgroup_release(cgrp);
		flow_record_width_skip(cgid, charge);
		bpf_spin_lock(&cgv_tree_lock);
		flow_cap_session_budget(cgv_node, cgc, max_credit);
		cgv_node->cvtime += charge;
		bpf_rbtree_add(&cgv_tree, &cgv_node->rb_node, cgv_node_less);
		bpf_spin_unlock(&cgv_tree_lock);
		return false;
	}
	if (!enforce_width)
		flow_record_fair_dispatch(cgid, true);
	if (enforce_width &&
	    !flow_reserve_fair_service_cfg(cfg, cgrp_slice_ns,
				       &fair_reserved)) {
		bpf_cgroup_release(cgrp);
		flow_record_width_skip(cgid, charge);
		bpf_spin_lock(&cgv_tree_lock);
		flow_cap_session_budget(cgv_node, cgc, max_credit);
		cgv_node->cvtime += charge;
		bpf_rbtree_add(&cgv_tree, &cgv_node->rb_node, cgv_node_less);
		bpf_spin_unlock(&cgv_tree_lock);
		flow_record_fair_dispatch(cgid, false);
		return false;
	}
	if (!flow_reserve_session_quota_cfg(cfg, cgrp_slice_ns)) {
		bpf_cgroup_release(cgrp);
		bpf_spin_lock(&cgv_tree_lock);
		cgv_node->cvtime += charge;
		flow_cap_session_budget(cgv_node, cgc, max_credit);
		bpf_rbtree_add(&cgv_tree, &cgv_node->rb_node, cgv_node_less);
		bpf_spin_unlock(&cgv_tree_lock);
		if (fair_reserved)
			flow_release_fair_service(cgid, cgrp_slice_ns);
		flow_record_quota_rejection(cgid, false);
		return false;
	}
	if (enforce_width &&
	    !flow_reserve_dispatch_slot_cfg(cgid, cfg, &parallel_limit, &reserved)) {
		bpf_cgroup_release(cgrp);
		flow_record_width_skip(cgid, charge);
		bpf_spin_lock(&cgv_tree_lock);
		flow_cap_session_budget(cgv_node, cgc, max_credit);
		cgv_node->cvtime += charge;
		bpf_rbtree_add(&cgv_tree, &cgv_node->rb_node, cgv_node_less);
		bpf_spin_unlock(&cgv_tree_lock);
		flow_release_session_quota(cgid, cgrp_slice_ns);
		if (fair_reserved)
			flow_release_fair_service(cgid, cgrp_slice_ns);
		flow_record_width_limit(cgid, false, parallel_limit);
		return false;
	}
	if (!scx_bpf_dsq_move_to_local(cgid)) {
		if (reserved)
			flow_release_dispatch_slot(cgid);
		flow_release_session_quota(cgid, cgrp_slice_ns);
		if (fair_reserved)
			flow_release_fair_service(cgid, cgrp_slice_ns);
		bpf_cgroup_release(cgrp);
		goto out_stash;
	}
	{
		__u64 skip = flow_take_width_skip(cgid);

	bpf_cgroup_release(cgrp);
	bpf_spin_lock(&cgv_tree_lock);
	flow_cap_session_budget(cgv_node, cgc, max_credit);
	cgv_node->cvtime = cgv_node->cvtime > skip ?
		cgv_node->cvtime - skip : 0;
	cgv_node->cvtime += charge;
	bpf_rbtree_add(&cgv_tree, &cgv_node->rb_node, cgv_node_less);
	bpf_spin_unlock(&cgv_tree_lock);
	}
	*cgidp = cgid;
	flow_record_adaptive_weight_dispatch(cgid);
	flow_set_dispatch_fair(cgid,
		fair_reserved ? cgrp_slice_ns : 0);
	return true;

out_stash:
	stash = bpf_map_lookup_elem(&cgv_node_stash, &cgid);
	if (!stash)
		goto out_free;
	__sync_val_compare_and_swap(&cgc->queued, 1, 0);
	if (scx_bpf_dsq_nr_queued(cgid)) {
		bpf_spin_lock(&cgv_tree_lock);
		bpf_rbtree_add(&cgv_tree, &cgv_node->rb_node, cgv_node_less);
		bpf_spin_unlock(&cgv_tree_lock);
	} else {
		cgv_node = bpf_kptr_xchg(&stash->node, cgv_node);
		if (cgv_node)
			goto out_free;
	}
	return false;

out_free:
	bpf_obj_drop(cgv_node);
	return false;
}

/* This hook sees a selected futex waiter, not unsuccessful FUTEX_WAKE
 * calls. A cross-process notification, a main thread, and a long CPU burst
 * keep their original credit. Real waiting time still bounds any reduction. */
SEC("fentry/futex_wake_mark")
int BPF_PROG(flow_v2_local_futex_wake, struct wake_q_head *wake_q,
	     struct futex_q *q)
{
	struct flow_v2_quota_task_state *state;
	struct task_struct *raw, *task;
	__u32 pid, tgid, cpu;

	(void)wake_q;
	if (!local_futex_credit || !q || BPF_CORE_READ(q, pi_state) ||
	    BPF_CORE_READ(q, rt_waiter))
		return 0;
	raw = BPF_CORE_READ(q, task);
	pid = BPF_CORE_READ(raw, pid);
	tgid = BPF_CORE_READ(raw, tgid);
	if (!pid || pid == tgid || tgid != (__u32)(bpf_get_current_pid_tgid() >> 32) ||
	    BPF_CORE_READ(raw, signal, nr_threads) <= managed_cpu_count)
		return 0;
	task = bpf_task_from_pid(pid);
	if (!task)
		return 0;
	state = bpf_task_storage_get(&quota_task_state, task, 0, 0);
	if (state && state->completed_burst && state->sleep_started_ns &&
	    state->last_burst_ns <= (progress_slice_ns ?: 1)) {
		state->local_futex_wakeup = true;
		cpu = bpf_get_smp_processor_id();
		if (cpu < 256)
			local_schedule_stats[cpu][0]++;
	}
	bpf_task_release(task);
	return 0;
}

/* 统计两次真实睡眠之间使用的 CPU 时间，包含中间被抢占、重新运行的
 * 各个片段。没有样本时保留旧上限。短计算最多提前一个短时间片；
 * 较长计算可以获得更多提前量，但不能超过旧上限。
 * CPU 时间只是局部判断依据，不表示这些计算必然有用。 */
static __always_inline __u64
flow_ordinary_wakeup_credit(struct task_struct *p)
{
	struct flow_v2_quota_task_state *task;
	__u64 credit;

	if (local_futex_credit) {
		task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
		if (task && task->local_futex_wakeup &&
		    task->last_sleep_ns < ordinary_wakeup_credit_ns) {
			__u32 cpu = bpf_get_smp_processor_id();

			if (cpu < 256)
				local_schedule_stats[cpu][1]++;
			return task->last_sleep_ns;
		}
	}
	if (!adaptive_wakeup_credit && !sleep_bounded_wakeup_credit)
		return ordinary_wakeup_credit_ns;
	if (threaded_wakeup_credit &&
	    BPF_CORE_READ(p, signal, nr_threads) <= managed_cpu_count)
		return ordinary_wakeup_credit_ns;
	task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
	if (!task || !task->completed_burst)
		return ordinary_wakeup_credit_ns;
	credit = ordinary_wakeup_credit_ns;
	if (adaptive_wakeup_credit) {
		credit = task->last_burst_ns;
		if (credit < progress_slice_ns)
			credit = progress_slice_ns;
		if (credit > ordinary_wakeup_credit_ns)
			credit = ordinary_wakeup_credit_ns;
	}
	if (sleep_bounded_wakeup_credit && credit > task->last_sleep_ns)
		credit = task->last_sleep_ns;
	return credit;
}

static __always_inline __s32
flow_locality_preference(struct task_struct *p,
			 const struct flow_v2_task_meta *meta)
{
	__u64 now;
	__s32 cpu;

	if ((!flow_relation_decision() &&
	     decision_mode != FLOW_V2_DECISION_MANUAL) ||
	    !locality_enabled || !p || !meta ||
	    (meta->flags & FLOW_V2_TASK_STALE_DELAYED) ||
	    !meta->promotion_evidence_ns)
		return -1;
	now = bpf_ktime_get_ns();
	/* 只在这个进程最近确实因等待关系获得过推进时
	 * 复用它自己上次运行的处理器，不为整个进程组绑定位置。 */
	if (now < meta->promotion_evidence_ns ||
	    now - meta->promotion_evidence_ns > 250 * 1000 * 1000)
		return -1;
	cpu = meta->last_cpu;
	if (flow_managed_cpu(cpu) && bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
		return cpu;
	return -1;
}

static __always_inline void
flow_record_running_cpu(struct flow_v2_task_meta *meta, __s32 cpu)
{
	if (!meta || !flow_managed_cpu(cpu))
		return;
	meta->last_cpu = cpu;
}

static __always_inline bool flow_rescue_session_dsq(__u64 *cgidp)
{
	struct flow_v2_global_stats *global;
	__u32 zero = 0;
	__u32 *cursor;
	__u32 start;
	int i;

	cursor = bpf_map_lookup_elem(&rescue_cursor, &zero);
	start = cursor ? *cursor : 0;
	bpf_for(i, 0, FLOW_V2_ACTIVE_SESSION_SLOTS) {
		__u32 slot = (start + i) % FLOW_V2_ACTIVE_SESSION_SLOTS;
		__u64 *cgid = bpf_map_lookup_elem(&active_session_slots, &slot);
		struct cgroup *cgrp;
		__u64 value;
		bool fair_reserved;

		if (!cgid || !*cgid)
			continue;
		value = *cgid;
		if (!scx_bpf_dsq_nr_queued(value))
			continue;
		bool fair_allowed = flow_reserve_fair_service(
			value, cgrp_slice_ns, &fair_reserved);

		if (!fair_allowed && !flow_session_can_borrow(value)) {
			flow_record_fair_dispatch(value, false);
			continue;
		}
		if (!fair_allowed)
			flow_record_fair_dispatch(value, true);
		if (!flow_reserve_session_quota(value, cgrp_slice_ns)) {
			if (fair_reserved)
				flow_release_fair_service(value, cgrp_slice_ns);
			flow_record_quota_rejection(value, false);
			continue;
		}
		cgrp = bpf_cgroup_from_id(value);
		if (!cgrp) {
			flow_release_session_quota(value, cgrp_slice_ns);
			if (fair_reserved)
				flow_release_fair_service(value, cgrp_slice_ns);
			bpf_map_delete_elem(&active_session_slots, &slot);
			continue;
		}
		bpf_cgroup_release(cgrp);
		if (!scx_bpf_dsq_move_to_local(value)) {
			flow_release_session_quota(value, cgrp_slice_ns);
			if (fair_reserved)
				flow_release_fair_service(value, cgrp_slice_ns);
			continue;
		}
		*cgidp = value;
		flow_set_dispatch_fair(value,
			fair_reserved ? cgrp_slice_ns : 0);
		if (cursor)
			*cursor = (slot + 1) % FLOW_V2_ACTIVE_SESSION_SLOTS;
		global = flow_global();
		if (global)
			__sync_fetch_and_add(&global->session_rescues, 1);
		return true;
	}
	return false;
}

s32 BPF_STRUCT_OPS(flow_v2_select_cpu, struct task_struct *p, s32 prev_cpu,
			   __u64 wake_flags)
{
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_session_stats *session_statsp;
	struct flow_v2_task_meta *meta = NULL;
	struct flow_v2_global_stats *global;
	struct fcg_task_ctx *taskc;
	struct cgroup *cgrp;
	bool idle = false;
	bool quota_exhausted = false;
	bool width_reserved = false;
	bool parallel_limit = false;
	__u64 cgid;
	__u32 pid = p->pid;
	__s32 cpu;
	__s32 preferred_cpu;
	__u8 *policy_task;
	bool interactive = false;

	policy_task = flow_policy_enabled ?
		bpf_map_lookup_elem(&flow_v2_policy_tasks, &pid) : NULL;
	if (policy_task)
		meta = flow_lookup_task(p);
	interactive = interactive_enabled && meta &&
		(meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE);
	preferred_cpu = flow_locality_preference(p, meta);
	cpu = scx_bpf_select_cpu_dfl(
		p, preferred_cpu >= 0 ? preferred_cpu : prev_cpu,
		wake_flags, &idle);
	if (preferred_cpu >= 0 && meta) {
		session_statsp = flow_session_stats(meta->session_id);
		if (session_statsp) {
			if (idle && cpu == preferred_cpu)
				session_statsp->locality_hits++;
			else
				session_statsp->locality_fallbacks++;
		}
	}
	if (idle) {
		/* 空闲处理器直派也必须服从会话的 cpu.max。 */
		cgrp = __COMPAT_scx_bpf_task_cgroup(p);
		cgid = cgrp ? cgrp->kn->id : 0;
		if (cgrp)
			bpf_cgroup_release(cgrp);
		if (cgid && flow_session_enabled(cgid, &cfg)) {
			struct flow_v2_quota_task_state *quota_task;

			if (!flow_reserve_fair_for_task(p, cgid, cgrp_slice_ns))
				return cpu;
			if (!flow_reserve_dispatch_slot(
					cgid, &parallel_limit, &width_reserved)) {
				flow_record_width_limit(cgid, false, parallel_limit);
				flow_release_fair_for_task(p, cgid);
				return cpu;
			}
			quota_exhausted = !flow_reserve_quota_for_task(
				p, cgid, cgrp_slice_ns, true);
			if (quota_exhausted) {
				if (width_reserved)
					flow_release_dispatch_slot(cgid);
				flow_release_fair_for_task(p, cgid);
				return cpu;
			}
			quota_task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
			if (quota_task)
				quota_task->width_reserved = width_reserved;
		}
		/* 与 scx_flatcg 一样直接使用空闲处理器并在 stopping 结算。 */
		if (!shared_ordinary_path) {
			taskc = bpf_task_storage_get(&task_ctx, p, 0, 0);
			if (taskc)
				set_bypassed_at(p, taskc);
		}
		/*
		 * 空闲处理器不存在可运行排队，不能消耗一条“等待后提权”
		 * 证据。详细核对模式才补记这次直接派发。
		 */
		if (detailed_accounting && flow_session_enabled(cgid, &cfg)) {
			__u32 role;

			meta = flow_lookup_or_create_task(p, cgid, cfg);
			if (meta) {
				role = meta->role < FLOW_V2_NR_ROLES ?
					meta->role : FLOW_V2_ROLE_NORMAL;

				flow_record_enqueue(meta, role, 0);
			}
		} else if (detailed_accounting) {
			global = flow_global();
			if (global)
				__sync_fetch_and_add(&global->untracked_enqueues, 1);
		}
		if (interactive && !detailed_accounting)
			flow_record_enqueue(meta, FLOW_V2_ROLE_INTERACTIVE, 0);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL,
				   interactive ? flow_interactive_slice() :
				   SCX_SLICE_DFL, 0);
	}
	return cpu;
}

void BPF_STRUCT_OPS(flow_v2_enqueue, struct task_struct *p, __u64 enq_flags)
{
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_global_stats *global;
	struct flow_v2_task_meta *meta = NULL;
	struct flow_v2_session_stats *stats;
	struct fcg_cgrp_ctx *cgc;
	struct fcg_task_ctx *taskc;
	struct cgroup *cgrp;
	__u64 requested_bias = 0;
	__u64 granted_bias = 0;
	__u64 cgid;
	__u64 tvtime;
	__u64 slice_ns;
	__u32 pid = p->pid;
	__u32 role = FLOW_V2_ROLE_NORMAL;
	__u8 *policy_task;
	bool cross_session_dispatch = false;
	bool interactive_dispatch = false;

	/* ENQ_LAST means the CPU is going idle after its sole task was put
	 * back in our queue. The core requires an explicit scheduling event;
	 * otherwise a ready task can stall until an unrelated wakeup arrives.
	 * Dispatch still checks quota and arms its existing timer if blocked. */
	if (enq_flags & SCX_ENQ_LAST)
		scx_bpf_kick_cpu(scx_bpf_task_cpu(p), 0);

	if (shared_ordinary_path) {
		/*
		 * 所有已知工具成员都做一次来源查询，才能在新工具开始后识别
		 * 仍存活的旧任务。没有来源记录的系统任务仍直接走共享队列。
		 */
		meta = flow_lookup_task(p);
		if (meta && flow_session_enabled(meta->cgroup_id, &cfg)) {
			flow_record_descendant_waker_requeue(meta, pid);
			if (!meta->tool_id)
				flow_adopt_from_parent(meta);
			flow_refresh_tool_role(meta);
			if (flow_refresh_stale_delay(meta, cfg))
				flow_mark_policy_task(pid);
			if (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
					   FLOW_V2_TASK_RETURN_ROOT |
					   FLOW_V2_TASK_PROGRESS_GROUP |
					   FLOW_V2_TASK_TAIL_PROMOTED))
				flow_refresh_return_role(p, meta);
			flow_refresh_sleeping_descendant(p, meta);
			role = meta->role < FLOW_V2_NR_ROLES ?
				meta->role : FLOW_V2_ROLE_NORMAL;
			interactive_dispatch = interactive_enabled &&
				(meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE);
			if (interactive_dispatch)
				requested_bias = flow_interactive_bias(cfg);
			else if (role == FLOW_V2_ROLE_PROGRESS &&
			    (meta->flags & FLOW_V2_TASK_TAIL_PROMOTED))
				requested_bias = flow_progress_bias(cfg, meta);
			else if (meta->flags &
				 FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED)
				requested_bias = flow_progress_bias(cfg, meta);
			else
				requested_bias = flow_coarse_policy_bias(cfg, meta);
			if (requested_bias &&
			    flow_session_quota_exhausted(meta->cgroup_id))
				requested_bias = 0;
			granted_bias = flow_take_bias(cfg, meta, requested_bias);
			if (requested_bias && !granted_bias) {
				stats = flow_session_stats(meta->session_id);
				if (stats)
					stats->budget_limited++;
			}
		} else if (meta) {
			/* 已关闭会话的残留任务不能改变普通工作守恒顺序。 */
			flow_clear_policy_task(pid);
			meta = NULL;
			cfg = NULL;
		}
		policy_task = bpf_map_lookup_elem(&flow_v2_policy_tasks, &pid);

		slice_ns = granted_bias ?
			(interactive_dispatch ? flow_interactive_slice() :
			 flow_progress_slice(meta)) : SCX_SLICE_DFL;
		if (granted_bias) {
			taskc = bpf_task_storage_get(&task_ctx, p, 0, 0);
			if (taskc) {
				if (flow_reserve_fair_for_task(
						p, meta->cgroup_id, slice_ns) &&
				    flow_reserve_quota_for_task(
						p, meta->cgroup_id, slice_ns, false)) {
					set_bypassed_at(p, taskc);
					flow_record_enqueue(meta, role, granted_bias);
					scx_bpf_dsq_insert(p,
						interactive_dispatch ?
						FLOW_V2_INTERACTIVE_DSQ :
						FLOW_V2_PROGRESS_DSQ,
							slice_ns, enq_flags);
					if (interactive_dispatch &&
					    flow_allow_interactive_preempt(meta->session_id)) {
						stats = flow_session_stats(meta->session_id);
						if (stats)
							stats->interactive_preemptions++;
						scx_bpf_kick_cpu(scx_bpf_task_cpu(p),
								 SCX_KICK_PREEMPT);
					}
					return;
				}
				flow_release_fair_for_task(p, meta->cgroup_id);
				flow_refund_unused_bias(meta, 0);
				granted_bias = 0;
				slice_ns = SCX_SLICE_DFL;
			}
		}
		/* 一纳秒只作为 running 回调中的候选标记，不改变有效时间片。 */
		if (!granted_bias && policy_task && slice_ns > 1)
			slice_ns--;
		if (fifo_sched) {
			if (meta)
				flow_record_enqueue(meta, role, granted_bias);
			scx_bpf_dsq_insert(p, FLOW_V2_SHARED_DSQ, slice_ns,
					   enq_flags);
			return;
		}
		{
			__u64 credit = granted_bias ? SCX_SLICE_DFL :
				flow_ordinary_wakeup_credit(p);

			tvtime = p->scx.dsq_vtime;
			if (time_before(tvtime, flow_v2_shared_vtime - credit))
				tvtime = flow_v2_shared_vtime - credit;
		}
		if (granted_bias) {
			__u64 weight = p->scx.weight ?: 1;
			__u64 vtime_bias = granted_bias * 100 / weight;

			tvtime = tvtime > vtime_bias ? tvtime - vtime_bias : 0;
		} else if (meta && cfg && role == FLOW_V2_ROLE_BACKGROUND &&
			   flow_background_should_delay(meta, cfg)) {
			tvtime += cfg->background_delay_ns;
		}
		if (meta)
			flow_record_enqueue(meta, role, granted_bias);
		scx_bpf_dsq_insert_vtime(p, FLOW_V2_SHARED_DSQ, slice_ns,
					 tvtime, enq_flags);
		return;
	}

	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (!cgrp)
		goto fallback;
	cgid = cgrp->kn->id;
	if (!flow_session_enabled(cgid, &cfg))
		goto release_fallback;
	cgc = find_cgrp_ctx(cgrp);
	if (!cgc)
		goto release_fallback;
	meta = flow_policy_enabled ? flow_lookup_or_create_task(p, cgid, cfg) : NULL;
	if (meta) {
		flow_record_descendant_waker_requeue(meta, pid);
		if (!meta->tool_id)
			flow_adopt_from_parent(meta);
		/* runnable() 已经负责等待任意子进程的增量关联。 */
		flow_refresh_tool_role(meta);
		if (flow_refresh_stale_delay(meta, cfg))
			flow_mark_policy_task(pid);
		if (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
				   FLOW_V2_TASK_RETURN_ROOT |
				   FLOW_V2_TASK_PROGRESS_GROUP |
				   FLOW_V2_TASK_TAIL_PROMOTED))
			flow_refresh_return_role(p, meta);
		flow_refresh_sleeping_descendant(p, meta);
		role = meta->role < FLOW_V2_NR_ROLES ?
			meta->role : FLOW_V2_ROLE_NORMAL;
		interactive_dispatch = interactive_enabled &&
			(meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE);
		if (interactive_dispatch)
			requested_bias = flow_interactive_bias(cfg);
		else if (role == FLOW_V2_ROLE_PROGRESS &&
		    (meta->flags & FLOW_V2_TASK_TAIL_PROMOTED))
			requested_bias = flow_progress_bias(cfg, meta);
		else if (meta->flags & FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED)
			requested_bias = flow_progress_bias(cfg, meta);
		else
			requested_bias = flow_coarse_policy_bias(cfg, meta);
		if (requested_bias && flow_session_quota_exhausted(cgid))
			requested_bias = 0;
		granted_bias = flow_take_bias(cfg, meta, requested_bias);
		if (requested_bias && !granted_bias) {
			stats = flow_session_stats(meta->session_id);
			if (stats)
				stats->budget_limited++;
		}
	}
	if (meta && granted_bias && !interactive_dispatch) {
		cross_session_dispatch = cross_session_role_dispatch &&
			(flow_stage_decision() || flow_tool_decision() ||
			 (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
					 FLOW_V2_TASK_PROGRESS_GROUP |
					 FLOW_V2_TASK_DESCENDANT_PROMOTED |
					 FLOW_V2_TASK_DESCENDANT_ROOT_WAKER_PROMOTED)));
		if (cross_session_dispatch &&
		    cross_session_progress_when_few_runnable) {
			struct flow_v2_session_runtime *session_runtimep =
				flow_session_runtime(cfg->session_id);

			if (session_runtimep && session_runtimep->runnable_tasks > 2)
				cross_session_dispatch = false;
		}
	}
	if (meta && granted_bias && interactive_dispatch &&
	    cross_session_role_dispatch) {
		slice_ns = flow_interactive_slice();
		taskc = bpf_task_storage_get(&task_ctx, p, 0, 0);
		if (taskc) {
			if (flow_reserve_fair_for_task(p, cgid, slice_ns) &&
			    flow_reserve_quota_for_task(p, cgid, slice_ns, false)) {
				set_bypassed_at(p, taskc);
				flow_record_enqueue(meta, role, granted_bias);
				scx_bpf_dsq_insert(p, FLOW_V2_INTERACTIVE_DSQ,
						   slice_ns, enq_flags);
				if (flow_allow_interactive_preempt(meta->session_id)) {
					stats = flow_session_stats(meta->session_id);
					if (stats)
						stats->interactive_preemptions++;
					scx_bpf_kick_cpu(scx_bpf_task_cpu(p),
							 SCX_KICK_PREEMPT);
				}
				bpf_cgroup_release(cgrp);
				return;
			}
			flow_release_fair_for_task(p, cgid);
			flow_refund_unused_bias(meta, 0);
			granted_bias = 0;
		}
	}
	if (meta && granted_bias && cross_session_dispatch) {
		/*
		 * 默认允许已有因果证据且取得预算的任务跨工作流有限提前。
		 * 关闭 cross_session_role_dispatch 后，仅在本工作流内提前。
		 */
		slice_ns = flow_progress_slice(meta);
		taskc = bpf_task_storage_get(&task_ctx, p, 0, 0);
		if (taskc) {
			if (flow_reserve_fair_for_task(p, cgid, slice_ns) &&
			    flow_reserve_quota_for_task(p, cgid, slice_ns, false)) {
				set_bypassed_at(p, taskc);
				flow_record_enqueue(meta, role, granted_bias);
				scx_bpf_dsq_insert(p, FLOW_V2_PROGRESS_DSQ,
						   slice_ns, enq_flags);
				/* 已确认被等待、取得两层预算且刚被唤醒的任务，
				 * 可以请求重新选择；同会话至少间隔一个推进时间片。
				 * 仅排在队首不足以打断正在运行的长时间片。 */
				if (preempt_confirmed_wait &&
				    (enq_flags & SCX_ENQ_WAKEUP) &&
				    (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
						    FLOW_V2_TASK_PROGRESS_GROUP))) {
					__s32 target_cpu = scx_bpf_task_cpu(p);

					if (flow_managed_cpu(target_cpu) &&
					    flow_allow_preempt(meta->session_id, progress_slice_ns)) {
						__sync_fetch_and_add(&confirmed_wait_preemptions, 1);
						scx_bpf_kick_cpu(target_cpu, SCX_KICK_PREEMPT);
					}
				}
				bpf_cgroup_release(cgrp);
				return;
			}
			flow_release_fair_for_task(p, cgid);
			flow_refund_unused_bias(meta, 0);
			granted_bias = 0;
		}
	}
	if (meta && granted_bias && !fifo_sched) {
		__u64 weight = p->scx.weight ?: 1;
		__u64 vtime_bias = granted_bias * 100 / weight;

		/* 返回相关任务只在本工作流队列内提前一个短运行片。 */
		tvtime = p->scx.dsq_vtime;
		if (time_before(tvtime, cgc->tvtime_now - SCX_SLICE_DFL))
			tvtime = cgc->tvtime_now - SCX_SLICE_DFL;
		tvtime = tvtime > vtime_bias ? tvtime - vtime_bias : 0;
		flow_record_enqueue(meta, role, granted_bias);
		scx_bpf_dsq_insert_vtime(p, cgid,
				 interactive_dispatch ? flow_interactive_slice() :
				 flow_progress_slice(meta),
					 tvtime, enq_flags);
	} else if (meta && fifo_sched) {
		meta->enqueue_ns = bpf_ktime_get_ns();
		scx_bpf_dsq_insert(p, cgid, SCX_SLICE_DFL, enq_flags);
	} else {
		__u64 credit = flow_ordinary_wakeup_credit(p);

		tvtime = p->scx.dsq_vtime;
		if (time_before(tvtime, cgc->tvtime_now - credit))
			tvtime = cgc->tvtime_now - credit;
		if (meta && cfg && role == FLOW_V2_ROLE_BACKGROUND &&
		    flow_background_should_delay(meta, cfg))
			tvtime += cfg->background_delay_ns;
		if (meta)
			flow_record_enqueue(meta, role, 0);
		/* 本函数已经完成来源、类别和后台判断，不能再次进入包装层
		 * 重复查询同一组映射。 */
		scx_bpf_dsq_insert_vtime(p, cgid, SCX_SLICE_DFL, tvtime,
					 enq_flags);
	}
	flow_session_enqueued(cgrp, cgc);
	bpf_cgroup_release(cgrp);
	return;

release_fallback:
	bpf_cgroup_release(cgrp);
fallback:
	global = flow_global();
	if (global) {
		__sync_fetch_and_add(&global->untracked_enqueues, 1);
		__sync_fetch_and_add(&global->fallback_enqueues, 1);
	}
	scx_bpf_dsq_insert(p, FALLBACK_DSQ, SCX_SLICE_DFL, enq_flags);
}

void BPF_STRUCT_OPS(flow_v2_runnable, struct task_struct *p, __u64 enq_flags)
{
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_session_runtime *session_runtimep;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_task_meta *meta;
	struct cgroup *cgrp;
	__u64 cgid;
	__u32 pid = p->pid;
	__u32 parent_pid;
	__u8 *active_waiter;
	__u8 *waiting_parent;

	(void)enq_flags;
	if (sleep_bounded_wakeup_credit || local_futex_credit) {
		struct flow_v2_quota_task_state *task =
			bpf_task_storage_get(&quota_task_state, p, 0, 0);

		if (task && task->sleep_started_ns) {
			__u64 now = bpf_ktime_get_ns();

			task->last_sleep_ns = now >= task->sleep_started_ns ?
				now - task->sleep_started_ns : 0;
			task->sleep_started_ns = 0;
		}
	}
	if (!flow_policy_enabled) {
		cgrp = __COMPAT_scx_bpf_task_cgroup(p);
		if (!cgrp)
			return;
		cgid = cgrp->kn->id;
		if (flow_session_enabled(cgid, &cfg)) {
			session_runtimep = flow_session_runtime(cfg->session_id);
			if (session_runtimep) {
				struct flow_v2_quota_task_state *quota_task =
					bpf_task_storage_get(&quota_task_state, p, 0, 0);

				if (quota_task && !quota_task->counted_runnable)
					flow_session_inc_runnable(session_runtimep, cfg);
				if (quota_task)
					quota_task->counted_runnable = 1;
			}
		}
		if (!shared_ordinary_path)
			update_active_weight_sums(cgrp, true);
		bpf_cgroup_release(cgrp);
		return;
	}
	if (shared_ordinary_path && !detailed_accounting &&
	    !(descendant_wakeup_observe || descendant_wakeup_light_observe ||
	      descendant_wakeup_progress)) {
		/* 被等待者唤醒父进程后，这一轮等待关系已经完成。 */
		active_waiter = bpf_map_lookup_elem(&flow_v2_waiting_parents,
						    &pid);
		if (active_waiter) {
			meta = flow_lookup_task(p);
			flow_mark_interactive_wakeup(p, meta);
			flow_finish_wait(pid);
		}
		parent_pid = BPF_CORE_READ(p, real_parent, pid);
		waiting_parent = bpf_map_lookup_elem(&flow_v2_waiting_parents,
						     &parent_pid);
		if (!waiting_parent)
			return;
	}
	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (!cgrp)
		return;
	cgid = cgrp->kn->id;
	if (flow_session_enabled(cgid, &cfg)) {
		session_runtimep = flow_session_runtime(cfg->session_id);
		if (session_runtimep) {
			struct flow_v2_quota_task_state *quota_task =
				bpf_task_storage_get(&quota_task_state, p, 0, 0);

			if (quota_task && !quota_task->counted_runnable)
				flow_session_inc_runnable(session_runtimep, cfg);
			if (quota_task)
				quota_task->counted_runnable = 1;
		}
		meta = flow_lookup_or_create_task(p, cgid, cfg);
		if (meta && !meta->tool_id)
			flow_adopt_from_parent(meta);
		flow_record_descendant_wakeup(p, meta);
		flow_mark_meta_awake(meta);
		active_waiter = bpf_map_lookup_elem(&flow_v2_waiting_parents,
						    &pid);
		if (active_waiter) {
			flow_mark_interactive_wakeup(p, meta);
			flow_finish_wait(pid);
		}
		flow_link_from_waiting_parent(p, meta);
		flow_set_wait_target_runnable(p, meta, cfg, true);
		if (meta && (detailed_accounting || sustained_tool_weight ||
		    sustained_tool_observe_only ||
		    meta->behavior == FLOW_V2_BEHAVIOR_PARALLEL) &&
		    !(meta->flags & FLOW_V2_TASK_RUNNABLE)) {
			meta->flags |= FLOW_V2_TASK_RUNNABLE;
			runtime = flow_tool_runtime_lookup(meta->session_id,
							   meta->tool_id);
			if (runtime && runtime->active &&
			    runtime->sequence == meta->sequence) {
				__u32 runnable = __sync_add_and_fetch(
					&runtime->runnable_tasks, 1);

				flow_update_runnable_peak(runtime, runnable);
			}
		}
	}
	/*
	 * 会话是在容器进程启动后登记的。这里必须始终维护 flatcg 的
	 * 活跃任务计数，否则登记前已经可运行的会话根进程不会被计入，
	 * 后续休眠又会执行减计数，最终破坏控制组选择树并造成任务停滞。
	 */
	if (!shared_ordinary_path)
		update_active_weight_sums(cgrp, true);
	bpf_cgroup_release(cgrp);
}

void BPF_STRUCT_OPS(flow_v2_quiescent, struct task_struct *p, __u64 deq_flags)
{
	struct flow_v2_session_config *cfg;
	struct flow_v2_fair_runtime *fair_runtime;
	struct flow_v2_session_runtime *session_runtimep;
	struct flow_v2_quota_task_state *quota_task;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_task_meta *meta;
	struct cgroup *cgrp;
	__u64 cgid;
	__u32 pid = p->pid;

	meta = flow_policy_enabled ?
		bpf_map_lookup_elem(&flow_v2_task_meta, &pid) : NULL;
	if (meta && (descendant_wakeup_observe ||
		     descendant_wakeup_light_observe ||
		     descendant_wakeup_progress))
		flow_mark_meta_sleeping(meta);
	if (meta && (meta->flags & FLOW_V2_TASK_RUNNABLE)) {
		meta->flags &= ~FLOW_V2_TASK_RUNNABLE;
		runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
		if (runtime && runtime->runnable_tasks)
			__sync_fetch_and_sub(&runtime->runnable_tasks, 1);
	}
	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (!cgrp)
		return;
	cgid = cgrp->kn->id;
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (cfg) {
		session_runtimep = bpf_map_lookup_elem(&session_runtime,
						 &cfg->session_id);
		fair_runtime = bpf_map_lookup_elem(&session_fair_runtime,
					       &cfg->session_id);
		if (session_runtimep) {
			/* 进程退出时 stopping 可能不会再次出现，由 quiescent
			 * 清除仍保留的运行标记，避免并行度统计逐步增大。 */
			quota_task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
			if ((adaptive_wakeup_credit || sleep_bounded_wakeup_credit ||
			     local_futex_credit) && quota_task &&
			    (deq_flags & SCX_DEQ_SLEEP)) {
				__u64 executed = p->se.sum_exec_runtime;

				quota_task->local_futex_wakeup = false;

				if (executed >= quota_task->previous_sleep_exec_runtime) {
					quota_task->last_burst_ns = executed -
						quota_task->previous_sleep_exec_runtime;
					quota_task->completed_burst = true;
				}
				quota_task->previous_sleep_exec_runtime = executed;
				if (sleep_bounded_wakeup_credit || local_futex_credit)
					quota_task->sleep_started_ns = bpf_ktime_get_ns();
			}
			if (quota_task && quota_task->counted_wait_target_runnable)
				flow_set_wait_target_runnable(p, meta, cfg, false);
			if (quota_task && quota_task->counted_runnable) {
				flow_session_dec_runnable(session_runtimep, cfg);
				quota_task->counted_runnable = 0;
			}
			if (fair_runtime && quota_task &&
			    quota_task->fair_running_since_ns) {
				flow_charge_fair_service(cfg, fair_runtime,
					quota_task, bpf_ktime_get_ns());
				quota_task->fair_running_since_ns = 0;
				quota_task->fair_reserved_ns = 0;
				quota_task->fair_window_id = 0;
				quota_task->fair_runnable_epoch = 0;
			}
			if (quota_task && quota_task->counted_running) {
				flow_session_dec_running(session_runtimep);
				quota_task->counted_running = 0;
			}
		}
	}
	if (!shared_ordinary_path)
		update_active_weight_sums(cgrp, false);
	bpf_cgroup_release(cgrp);
}

void BPF_STRUCT_OPS(flow_v2_running, struct task_struct *p)
{
	struct flow_v2_quota_task_state *quota_task;
	struct flow_v2_fair_runtime *fair_runtime;
	struct flow_v2_session_config *cfg = NULL;
	struct flow_v2_session_runtime *session_runtimep;
	struct flow_v2_session_stats *stats;
	struct flow_v2_global_stats *global;
	struct flow_v2_task_meta *meta;
	struct fcg_cgrp_ctx *cgc;
	struct cgroup *cgrp;
	__u64 now = bpf_ktime_get_ns();
	__u64 cgid;
	__u32 pid = p->pid;
	__u32 role;
	__u8 *policy_task;
	__u32 running;

	if (!fifo_sched && shared_ordinary_path) {
		if (time_before(flow_v2_shared_vtime, p->scx.dsq_vtime))
			flow_v2_shared_vtime = p->scx.dsq_vtime;
	} else if (!fifo_sched) {
		cgrp = __COMPAT_scx_bpf_task_cgroup(p);
		cgc = find_cgrp_ctx(cgrp);
		if (cgc && time_before(cgc->tvtime_now, p->scx.dsq_vtime))
			cgc->tvtime_now = p->scx.dsq_vtime;
		bpf_cgroup_release(cgrp);
	}
	if (flow_managed_cpu((__s32)bpf_get_smp_processor_id())) {
		cgrp = __COMPAT_scx_bpf_task_cgroup(p);
		if (cgrp) {
			cgid = cgrp->kn->id;
			cfg = bpf_map_lookup_elem(&session_config, &cgid);
		} else {
			cfg = NULL;
		}
		if (cfg) {
			quota_task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
			session_runtimep = flow_session_runtime(cfg->session_id);
			fair_runtime = bpf_map_lookup_elem(&session_fair_runtime,
						       &cfg->session_id);
			if (session_runtimep && quota_task &&
			    !quota_task->counted_running) {
				bpf_spin_lock(&session_runtimep->lock);
				if (session_runtimep->reserved_tasks)
					session_runtimep->reserved_tasks--;
				running = ++session_runtimep->running_tasks;
				bpf_spin_unlock(&session_runtimep->lock);
				quota_task->counted_running = 1;
				quota_task->width_reserved = 0;
				flow_change_running_count(true);
				flow_update_running_peak(session_runtimep, running);
			}
			if (quota_task && cfg->cpu_quota_ns) {
				if (!quota_task->reserved_ns)
					quota_task->reserved_ns = cgrp_slice_ns;
				quota_task->running_since_ns = now;
			}
			if (quota_task && fair_runtime) {
				if (!quota_task->fair_reserved_ns)
					flow_take_dispatch_fair(cgid, quota_task,
							fair_runtime);
				quota_task->fair_running_since_ns = now;
			}
		}
		if (cgrp)
			bpf_cgroup_release(cgrp);
	}
	if (!flow_policy_enabled)
		return;

	policy_task = bpf_map_lookup_elem(&flow_v2_policy_tasks, &pid);
	if (!detailed_accounting && !sustained_tool_weight &&
	    !sustained_tool_observe_only && !descendant_wakeup_observe &&
	    !policy_task &&
	    p->scx.slice == SCX_SLICE_DFL)
		return;
	meta = flow_lookup_task(p);
	if (!meta)
		return;
	flow_record_sustained_tool_pressure(p, meta, cfg, now);
	flow_record_running_cpu(meta, (__s32)bpf_get_smp_processor_id());
	flow_finish_descendant_waker_promotion(meta, pid);
	/*
	 * 普通任务只更新一次实际运行处理器，不做逐次时间记账。成组推进
	 * 成员即使仍使用默认时间片，也必须结算本次排队等待；否则只有
	 * 直接等待目标能够获得推进，后代成员永远缺少调度证据。
	 */
	if (!detailed_accounting && !descendant_wakeup_observe && !policy_task &&
	    p->scx.slice == SCX_SLICE_DFL)
		return;
	role = meta->role < FLOW_V2_NR_ROLES ? meta->role : FLOW_V2_ROLE_NORMAL;
	if (meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			stats->interactive_runs++;
	}
	if (meta->enqueue_ns && now > meta->enqueue_ns) {
		__u64 wait = now - meta->enqueue_ns;
		__u64 pressure_ns = progress_slice_ns ?
			progress_slice_ns / 8 : 250 * 1000;

		meta->runnable_wait_ns += wait;
		meta->last_runnable_wait_ns = wait;
		meta->last_runnable_wait_at_ns = now;
		/* 与关键推进采用同一排队下限。只有当前工具真正等待的
		 * 进程在处理器队列中等待过，才改变整个工作流的处理器时间比例。 */
		if (cfg && wait >= pressure_ns &&
		    (meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
				    FLOW_V2_TASK_PROGRESS_GROUP)))
			flow_session_mark_wait_pressure(meta, cfg);
		/*
		 * 深层等待对象的本次排队时间到 running() 才能准确结算。
		 * 过去只在 enqueue() 中判断，此时 last_runnable_wait_ns 仍是
		 * 上一次的值，导致已经识别到的深层等待关系很少真正改变
		 * 调度顺序。这里在取得本次排队证据后完成判断；当前进程已经
		 * 获得处理器，若它还需继续运行，下一次入队即可使用有限提前。
		 */
		if ((meta->flags & FLOW_V2_TASK_PROGRESS_GROUP) &&
		    meta->progress_group_depth > 0 &&
		    !(meta->flags & FLOW_V2_TASK_TAIL_PROMOTED))
			flow_refresh_return_role(p, meta);
		if (detailed_accounting) {
			stats = flow_session_stats(meta->session_id);
			if (stats)
				__sync_fetch_and_add(&stats->runnable_wait_ns, wait);
		}
		/*
		 * 后台任务入队时用有限的虚拟运行时间偏移暂时让路。任务在
		 * 同一会话队列中重新获得处理器，且实际等待达到设定上限时，
		 * 记录这次恢复。原先只在再次入队时检查，无法观察任务已经在
		 * 队列中等待后重新运行的事实。
		 */
		if (background_enabled && cfg && role == FLOW_V2_ROLE_BACKGROUND &&
		    cfg->background_delay_ns &&
		    wait >= cfg->background_delay_ns &&
		    !(meta->flags & FLOW_V2_TASK_BACKGROUND_RESCUED)) {
			meta->flags |= FLOW_V2_TASK_BACKGROUND_RESCUED;
			stats = flow_session_stats(meta->session_id);
			if (stats)
				stats->background_rescues++;
		}
	}
	if (detailed_accounting ||
	    (meta->flags & FLOW_V2_TASK_TAIL_PROMOTED) ||
	    (meta->flags & FLOW_V2_TASK_INTERACTIVE_ONCE) ||
	    meta->reserved_bias_ns) {
		meta->running_ns = now;
		meta->run_slice_start_ns = p->scx.slice;
	}
	if (detailed_accounting || role == FLOW_V2_ROLE_BACKGROUND) {
		meta->last_run_at_ns = now;
	}
	if (detailed_accounting) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			__sync_fetch_and_add(&stats->runs, 1);
		global = flow_global();
		if (global)
			__sync_fetch_and_add(&global->tracked_runs, 1);
	}
}

void BPF_STRUCT_OPS(flow_v2_stopping, struct task_struct *p, bool runnable)
{
	struct flow_v2_quota_task_state *quota_task;
	struct flow_v2_fair_runtime *fair_runtime;
	struct flow_v2_session_config *cfg;
	struct flow_v2_session_runtime *session_runtimep;
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *meta;
	struct fcg_task_ctx *taskc;
	struct fcg_cgrp_ctx *cgc;
	struct cgroup *cgrp;
	__u64 now = bpf_ktime_get_ns();
	__u64 cgid;
	__u64 refunded = 0;
	__u64 runtime = 0;
	__u32 role;
	bool promoted;

	if (flow_managed_cpu((__s32)bpf_get_smp_processor_id())) {
		cgrp = __COMPAT_scx_bpf_task_cgroup(p);
		if (cgrp) {
			cgid = cgrp->kn->id;
			cfg = bpf_map_lookup_elem(&session_config, &cgid);
		} else {
			cfg = NULL;
		}
		if (cfg) {
			session_runtimep = bpf_map_lookup_elem(&session_runtime,
							 &cfg->session_id);
			fair_runtime = bpf_map_lookup_elem(&session_fair_runtime,
						       &cfg->session_id);
			quota_task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
			if (fair_runtime && quota_task &&
			    quota_task->fair_running_since_ns &&
			    now > quota_task->fair_running_since_ns) {
				flow_charge_fair_service(cfg, fair_runtime,
					quota_task, now);
				quota_task->fair_running_since_ns = 0;
				quota_task->fair_reserved_ns = 0;
				quota_task->fair_window_id = 0;
				quota_task->fair_runnable_epoch = 0;
			}
			if (session_runtimep && quota_task &&
			    quota_task->running_since_ns &&
			    now > quota_task->running_since_ns) {
				flow_charge_session_quota(
					cgid, cfg, session_runtimep,
					now - quota_task->running_since_ns,
					quota_task->reserved_ns);
				quota_task->running_since_ns = 0;
				quota_task->reserved_ns = 0;
			}
			if (session_runtimep && quota_task &&
			    quota_task->counted_running) {
				flow_session_dec_running(session_runtimep);
				quota_task->counted_running = 0;
			}
		}
		if (cgrp)
			bpf_cgroup_release(cgrp);
	}

	taskc = shared_ordinary_path ? NULL :
		bpf_task_storage_get(&task_ctx, p, 0, 0);
	if (taskc && taskc->bypassed_at) {
		cgrp = __COMPAT_scx_bpf_task_cgroup(p);
		cgc = find_cgrp_ctx(cgrp);
		if (cgc) {
			__u64 used = p->se.sum_exec_runtime - taskc->bypassed_at;
			__u64 bypass_cgid = cgrp->kn->id;

			__sync_fetch_and_add(&cgc->cvtime_delta,
					     flow_session_vtime_delta(bypass_cgid,
								      used));
			taskc->bypassed_at = 0;
		}
		bpf_cgroup_release(cgrp);
	}
	if (!flow_policy_enabled) {
		if (!fifo_sched)
			p->scx.dsq_vtime +=
				(SCX_SLICE_DFL - p->scx.slice) * 100 /
				(p->scx.weight ?: 1);
		return;
	}
	/* 只有短时间片返回任务和接近耗尽默认时间片的任务需要慢路径核对。 */
	if (!detailed_accounting &&
	    p->scx.slice > (progress_slice_ns ?: 1) &&
	    p->scx.slice != SCX_SLICE_DFL - 1) {
		if (!fifo_sched)
			p->scx.dsq_vtime +=
				(SCX_SLICE_DFL - p->scx.slice) * 100 /
				(p->scx.weight ?: 1);
		return;
	}

	meta = flow_lookup_task(p);
	if (!meta) {
		if (!fifo_sched)
			p->scx.dsq_vtime +=
				(SCX_SLICE_DFL - p->scx.slice) * 100 /
				(p->scx.weight ?: 1);
		return;
	}
	promoted = (meta->flags & (FLOW_V2_TASK_TAIL_PROMOTED |
				  FLOW_V2_TASK_INTERACTIVE_ONCE)) ||
		meta->reserved_bias_ns;
	/*
	 * 普通任务完全沿用 scx_flatcg 的结算。正式模式在这里结束，
	 * 不再为每次短运行写任务元数据或查询会话统计。
	 */
	if (!promoted) {
		if (!fifo_sched)
			p->scx.dsq_vtime +=
				(SCX_SLICE_DFL - p->scx.slice) * 100 /
				(p->scx.weight ?: 1);
		if (!detailed_accounting)
			return;
	}
	if (meta->running_ns && now > meta->running_ns)
		runtime = now - meta->running_ns;
	if (!fifo_sched && promoted) {
		__u64 slice_used;

		/* 提权任务必须按本次真实起始片长结算。 */
		slice_used = runtime;
		if (meta->run_slice_start_ns >= p->scx.slice)
			slice_used = meta->run_slice_start_ns - p->scx.slice;
		p->scx.dsq_vtime += slice_used * 100 / (p->scx.weight ?: 1);
	}
	if (meta->reserved_bias_ns)
		refunded = flow_refund_unused_bias(meta, runtime);
	if (detailed_accounting)
		meta->runtime_ns += runtime;
	meta->running_ns = 0;
	meta->run_slice_start_ns = 0;
	role = meta->role < FLOW_V2_NR_ROLES ? meta->role : FLOW_V2_ROLE_NORMAL;
	if (detailed_accounting || refunded) {
		stats = flow_session_stats(meta->session_id);
		if (stats) {
			if (detailed_accounting) {
				__sync_fetch_and_add(&stats->runtime_ns, runtime);
				__sync_fetch_and_add(&stats->role_runtime_ns[role], runtime);
			}
			__sync_fetch_and_add(&stats->refunded_bias_ns, refunded);
		}
	}
	/* 返回候选的提前只覆盖一个实际运行片。 */
	if (meta->flags & FLOW_V2_TASK_TAIL_PROMOTED) {
		bool descendant = meta->flags & FLOW_V2_TASK_DESCENDANT_PROMOTED;

		meta->flags &= ~(FLOW_V2_TASK_TAIL_PROMOTED |
				 FLOW_V2_TASK_DESCENDANT_PROMOTED);
		if (descendant)
			flow_clear_sleeping_descendant(meta, p->pid, false);
		flow_refresh_tool_role(meta);
	}
	/* 短交互只覆盖从阻塞等待中恢复后的第一次实际运行。 */
	flow_cancel_interactive(meta);
	(void)runnable;
}

/*
 * 不为所有任务强制重新排队。只有明确设置了处理器时间上限的
 * 会话在用完本周期时间后结束当前运行片，使 dispatch() 能将它
 * 留到下一周期。普通会话不进入这条路径。
 */
void BPF_STRUCT_OPS(flow_v2_tick, struct task_struct *p)
{
	struct flow_v2_session_config *cfg;
	struct cgroup *cgrp;
	__u64 cgid;
	__s32 cpu;

	cpu = (__s32)bpf_get_smp_processor_id();
	if (!flow_managed_cpu(cpu))
		return;
	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (!cgrp)
		return;
	cgid = cgrp->kn->id;
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (cfg && cfg->cpu_quota_ns && cfg->cpu_period_ns &&
	    flow_session_quota_exhausted(cgid)) {
		p->scx.slice = 0;
		scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
	}
	bpf_cgroup_release(cgrp);
}

void BPF_STRUCT_OPS(flow_v2_dispatch, __s32 cpu, struct task_struct *prev)
{
	struct fcg_cpu_ctx *cpuc;
	struct fcg_cgrp_ctx *cgc;
	struct cgroup *cgrp;
	__u64 now = scx_bpf_now();
	__u32 attempts = 1;
	int attempt;
	bool picked_next = false;
	bool current_at_width = false;
	bool current_parallel_limit = false;
	bool current_reserved = false;
	bool current_fair_exhausted = false;
	bool current_fair_reserved = false;
	bool current_quota_exhausted = false;
	bool current_quota_reserved = false;

	(void)prev;
	flow_set_dispatch_fair(0, 0);
	if (shared_ordinary_path) {
		/*
		 * 共享队列包含系统普通任务，必须由所有处理器共同消费；任务
		 * 自身的亲和性仍由 sched_ext 核心约束，基准任务只会落在 0-7。
		 * 等待结束后被唤醒的进程先于其他推进任务：它通常只需很短
		 * 的处理器时间就能将工具结果返回给上层。
		 */
		if (scx_bpf_dsq_move_to_local(FLOW_V2_INTERACTIVE_DSQ))
			return;
		if (scx_bpf_dsq_move_to_local(FLOW_V2_PROGRESS_DSQ))
			return;
		if (scx_bpf_dsq_move_to_local(FALLBACK_DSQ))
			return;
		scx_bpf_dsq_move_to_local(FLOW_V2_SHARED_DSQ);
		return;
	}
	if (!flow_managed_cpu(cpu)) {
		scx_bpf_dsq_move_to_local(FALLBACK_DSQ);
		return;
	}
	/* 自动权重的到期恢复和下一会话选择只在调度入口执行一次，
	 * 不扩大每次读取会话权重的热路径。这里必须与等待事件和权重
	 * 统计统一使用 bpf_ktime_get_ns()，不能混用调度框架缓存时间。 */
	if (automatic_session_weight)
		flow_automatic_select_next(bpf_ktime_get_ns());
	cpuc = find_cpu_ctx();
	if (!cpuc)
		return;
	/*
	 * 每次只取一个已经获得会话预算的短运行任务。它结束后继续原有
	 * 控制组轮转，避免把整个工作流提升为高优先级。已唤醒的结果返回
	 * 进程先于其他推进任务，以免完成了子进程却仍迟迟不返回。
	 */
	if (scx_bpf_dsq_move_to_local(FLOW_V2_INTERACTIVE_DSQ))
		return;
	if (scx_bpf_dsq_move_to_local(FLOW_V2_PROGRESS_DSQ))
		return;

	if (!cpuc->cur_cgid)
		goto pick_next;
	if (time_before(now, cpuc->cur_at + cgrp_slice_ns)) {
		struct flow_v2_session_config *cfg =
			bpf_map_lookup_elem(&session_config, &cpuc->cur_cgid);

		current_fair_exhausted = !flow_reserve_fair_service_cfg(
			cfg, cgrp_slice_ns, &current_fair_reserved);
		if (current_fair_exhausted)
			flow_record_fair_dispatch(cpuc->cur_cgid, false);
		current_at_width = current_fair_exhausted;
		if (!current_at_width) {
			current_quota_exhausted = !flow_reserve_session_quota_cfg(
				cfg, cgrp_slice_ns);
			if (current_quota_exhausted)
				flow_record_quota_rejection(cpuc->cur_cgid, false);
			current_quota_reserved = !current_quota_exhausted;
			current_at_width = current_quota_exhausted;
		}
		if (!current_at_width) {
			current_at_width = !flow_reserve_dispatch_slot_cfg(
				cpuc->cur_cgid, cfg, &current_parallel_limit,
				&current_reserved);
		}
		if (!current_at_width) {
			if (scx_bpf_dsq_move_to_local(cpuc->cur_cgid)) {
				flow_record_adaptive_weight_dispatch(cpuc->cur_cgid);
				flow_set_dispatch_fair(cpuc->cur_cgid,
					current_fair_reserved ? cgrp_slice_ns : 0);
				return;
			}
			if (current_reserved)
				flow_release_dispatch_slot(cpuc->cur_cgid);
			if (current_quota_reserved)
				flow_release_session_quota(cpuc->cur_cgid,
							   cgrp_slice_ns);
			if (current_fair_reserved)
				flow_release_fair_service(cpuc->cur_cgid,
							 cgrp_slice_ns);
		} else {
			if (current_quota_reserved)
				flow_release_session_quota(cpuc->cur_cgid,
							   cgrp_slice_ns);
			if (current_fair_reserved)
				flow_release_fair_service(cpuc->cur_cgid,
							 cgrp_slice_ns);
			if (!current_fair_exhausted)
				flow_record_width_limit(cpuc->cur_cgid, false,
							current_parallel_limit);
		}
	}

	cgrp = bpf_cgroup_from_id(cpuc->cur_cgid);
	if (cgrp) {
		cgc = bpf_cgrp_storage_get(&cgrp_ctx, cgrp, 0, 0);
		if (cgc) {
			__s64 remaining = (__s64)(cpuc->cur_at + cgrp_slice_ns) -
					  (__s64)now;
			__s64 correction;

			/*
			 * 选中会话时已经预收一个完整时间片。提前结束要退还
			 * 未使用部分，超时运行则补收；此前符号写反会系统性
			 * 压低高权重会话的实际处理器时间。
			 */
			if (remaining >= 0)
				correction = -(__s64)flow_session_vtime_delta(
					cpuc->cur_cgid, (__u64)remaining);
			else
				correction = flow_session_vtime_delta(
					cpuc->cur_cgid, (__u64)(-remaining));

			bpf_spin_lock(&cgv_tree_lock);
			__sync_fetch_and_add(&cgc->cvtime_delta, correction);
			bpf_spin_unlock(&cgv_tree_lock);
		}
		bpf_cgroup_release(cgrp);
	}

pick_next:
	cpuc->cur_at = now;
	/*
	 * 未注册任务使用 FALLBACK_DSQ。每次会话时间片结束或
	 * 当前会话队列变空时，先服务一个普通任务，保证它们不会
	 * 因持续活跃的会话而长时间等待。这与 scx_flatcg 的有限
	 * 等待路径保持一致。
	 */
	if (scx_bpf_dsq_move_to_local(FALLBACK_DSQ)) {
		cpuc->cur_cgid = 0;
		return;
	}
	{
		__u32 registered_sessions = flow_registered_session_count();

		/*
		 * 被软上限挡住的会话会重新进入树中。每个已登记会话允许两次
		 * 查找，避免连续两次取到同一高权重会话后过早进入借用路径。
		 */
		if (registered_sessions)
			attempts = registered_sessions * 2;
		if (attempts > FLOW_V2_ACTIVE_SESSION_SLOTS)
			attempts = FLOW_V2_ACTIVE_SESSION_SLOTS;
	}
	bpf_for(attempt, 0, attempts) {
		if (flow_try_pick_next_cgroup(&cpuc->cur_cgid, true)) {
			picked_next = true;
			break;
		}
	}
	if (picked_next && cpuc->cur_cgid)
		return;
	/* 只有确认存在未使用能力时才借用，并遍历到真正能使用余量的会话。 */
	if (!picked_next) {
		bpf_for(attempt, 0, attempts) {
			if (!flow_try_pick_next_cgroup(&cpuc->cur_cgid, false))
				continue;
			if (cpuc->cur_cgid) {
				bool parallel_limit = flow_parallel_width_has_evidence(
					cpuc->cur_cgid, bpf_map_lookup_elem(
						&session_config, &cpuc->cur_cgid));

				flow_record_width_limit(cpuc->cur_cgid, true,
							parallel_limit);
				return;
			}
			break;
		}
	}
	if (flow_rescue_session_dsq(&cpuc->cur_cgid))
		return;
	cpuc->cur_cgid = 0;
	scx_bpf_dsq_move_to_local(FALLBACK_DSQ);
}

SEC("tp_btf/sched_process_fork")
int BPF_PROG(flow_v2_fork, struct task_struct *parent,
	     struct task_struct *child)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *parent_meta;
	struct flow_v2_task_meta *child_meta;
	__u32 parent_pid, child_pid;

	if (!flow_policy_enabled || !parent || !child)
		return 0;
	parent_pid = parent->pid;
	child_pid = child->pid;
	parent_meta = flow_lookup_task(parent);
	if (!parent_meta)
		return 0;
	child_meta = flow_create_policy_task(
		child, child_pid, parent_meta->session_id, parent_meta->cgroup_id,
		parent_meta->tool_id, parent_meta->parent_tool_id,
		parent_meta->sequence, parent_pid, parent_meta->role,
		parent_meta->role_source, FLOW_V2_TASK_TOOL_MEMBER);
	if (child_meta)
		child_meta->last_cpu = parent_meta->last_cpu;
	if (child_meta) {
		child_meta->stage = parent_meta->stage;
		child_meta->behavior = parent_meta->behavior;
	}
	if (child_meta) {
		__u32 slot = parent_meta->recent_child_cursor++ & 7U;

		parent_meta->recent_child_pids[slot] = child_pid;
	}
	if (detailed_accounting) {
		stats = flow_session_stats(parent_meta->session_id);
		if (stats)
			__sync_fetch_and_add(&stats->forks, 1);
		flow_emit(FLOW_V2_EVENT_FORK, child, child_meta);
	}
	return 0;
}

/*
 * do_wait 同时覆盖 wait4 和 waitid。只处理没有 WNOHANG 的调用；这类
 * 调用会在没有可回收子进程时阻塞，正是工具返回所依赖的同步关系。
 * 具体目标由紧接着发生的 sched_process_wait 事件提供。
 */
SEC("fentry/do_wait")
int BPF_PROG(flow_v2_wait_event, struct wait_opts *wo)
{
	struct flow_v2_active_tool *active;
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *waiter;
	__u8 one = 1;
	__u64 cgid;
	__u64 causal_tool_id = 0;
	__u64 causal_sequence = 0;
	__u32 waiter_pid = (__u32)bpf_get_current_pid_tgid();
	__u32 options;

	if (!flow_policy_enabled || !wo)
		return 0;
	options = BPF_CORE_READ(wo, wo_flags);
	/* WNOHANG 轮询不会卡住工具返回，不建立调度关系。 */
	if (options & 1U) {
		waiter = flow_lookup_task(
			(struct task_struct *)bpf_get_current_task_btf());
		if (waiter) {
			stats = flow_session_stats(waiter->session_id);
			if (stats)
				stats->ignored_nonblocking_waits++;
		}
		if (bpf_map_lookup_elem(&flow_v2_waiting_parents, &waiter_pid))
			flow_finish_wait(waiter_pid);
		return 0;
	}

	waiter = flow_lookup_task(
		(struct task_struct *)bpf_get_current_task_btf());
	if (!waiter) {
		cgid = bpf_get_current_cgroup_id();
		active = bpf_map_lookup_elem(&active_tool_by_cgroup, &cgid);
		if (active && active->active)
			waiter = flow_create_policy_task(
				(struct task_struct *)bpf_get_current_task_btf(),
				waiter_pid, active->session_id, active->cgroup_id,
				active->tool_id, active->parent_tool_id,
				active->sequence, 0, active->role,
				active->role_source, FLOW_V2_TASK_TOOL_MEMBER);
	}
	if (!waiter || !waiter->session_id || !waiter->tool_id)
		return 0;
	/*
	 * 只从 flowd 已确认的工具启动进程建立阻塞关系。工具内部库和
	 * 工作进程自己的 wait 由既有父子关系覆盖，不能各自创建新的
	 * 推进组，否则大量嵌套等待会反复改变同一工作流的队列顺序。
	 */
	if (!(waiter->flags & (FLOW_V2_TASK_RETURN_ROOT |
			       FLOW_V2_TASK_SCOPE_ROOT |
			       FLOW_V2_TASK_PROGRESS_GROUP))) {
		stats = flow_session_stats(waiter->session_id);
		if (stats)
			stats->ignored_group_waits++;
		return 0;
	}
	flow_mark_policy_task(waiter_pid);
	/* 同一进程开始下一轮等待前，先结束可能残留的上一轮关系。 */
	if (bpf_map_lookup_elem(&flow_v2_waiting_parents, &waiter_pid))
		flow_finish_wait(waiter_pid);
	waiter->wait_selector = 0;
	waiter->wait_options = 0;
	waiter->wait_target_pid = 0;
	waiter->progress_group_members = 0;
	waiter->progress_group_admitted = 0;
	waiter->wait_round++;
	if (!waiter->wait_round)
		waiter->wait_round = 1;
	waiter->flags &= ~(FLOW_V2_TASK_WAITING_CHILD |
			  FLOW_V2_TASK_WAIT_ANY_CHILD |
			  FLOW_V2_TASK_WAIT_BLOCKED |
			  FLOW_V2_TASK_NONBLOCKING_WAIT);
	waiter->flags |= FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE |
			 FLOW_V2_TASK_WAITING_CHILD |
			 FLOW_V2_TASK_WAIT_ANY_CHILD;
	waiter->wait_started_ns = bpf_ktime_get_ns();
	if (flow_wait_causal_generation(waiter, &causal_tool_id,
					&causal_sequence)) {
		waiter->blocking_tool_id = causal_tool_id;
		waiter->blocking_sequence = causal_sequence;
	}
	if (bpf_map_update_elem(&flow_v2_waiting_parents, &waiter_pid, &one,
				BPF_ANY)) {
		flow_record_map_update_failure();
		waiter->flags &= ~(FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE |
				   FLOW_V2_TASK_WAIT_BLOCKED |
				   FLOW_V2_TASK_NONBLOCKING_WAIT |
				   FLOW_V2_TASK_WAITING_CHILD |
				   FLOW_V2_TASK_WAIT_ANY_CHILD);
		waiter->wait_started_ns = 0;
		waiter->blocking_tool_id = 0;
		waiter->blocking_sequence = 0;
		return 0;
	}
	stats = flow_session_stats(waiter->session_id);
	if (stats)
		stats->wait_events++;
	return 0;
}

/*
 * tracepoint 提供宿主机 pid。先查找上一钩子留下的阻塞标记，因此
 * WNOHANG 轮询在这里只做一次散列表查询，不会建立或更新任务关系。
 */
SEC("tp/sched/sched_process_wait")
int flow_v2_wait_target_event(struct trace_event_raw_sched_process_wait *ctx)
{
	struct flow_v2_task_meta *waiter;
	__u32 waiter_pid = (__u32)bpf_get_current_pid_tgid();
	__u32 target_pid;

	if (!flow_policy_enabled ||
	    !bpf_map_lookup_elem(&flow_v2_waiting_parents, &waiter_pid))
		return 0;
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &waiter_pid);
	if (!waiter || !(waiter->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE))
		return 0;
	target_pid = BPF_CORE_READ(ctx, pid);
	/* 没有具体 pid 时，等直接子进程进入可运行状态后增量登记。 */
	if (!target_pid)
		return 0;
	waiter->flags &= ~FLOW_V2_TASK_WAIT_ANY_CHILD;
	waiter->wait_target_pid = target_pid;
	return 0;
}

SEC("fexit/do_wait")
int BPF_PROG(flow_v2_wait_event_end, struct wait_opts *wo, long ret)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *waiter;
	__u32 waiter_pid = (__u32)bpf_get_current_pid_tgid();

	(void)wo;
	(void)ret;
	if (!flow_policy_enabled ||
	    !bpf_map_lookup_elem(&flow_v2_waiting_parents, &waiter_pid))
		return 0;
	waiter = bpf_map_lookup_elem(&flow_v2_task_meta, &waiter_pid);
	if (!waiter)
		return 0;
	if (!(waiter->flags & FLOW_V2_TASK_WAIT_BLOCKED)) {
		stats = flow_session_stats(waiter->session_id);
		if (stats)
			stats->ignored_immediate_waits++;
	}
	flow_finish_wait(waiter_pid);
	return 0;
}

SEC("tp_btf/sched_switch")
int BPF_PROG(flow_v2_wait_switch, bool preempt, struct task_struct *prev,
	     struct task_struct *next, unsigned int prev_state)
{
	(void)next;
	if (!flow_policy_enabled || !prev || preempt || prev_state == 0)
		return 0;
	flow_mark_task_sleeping(prev);
	flow_confirm_blocking_wait(prev);
	return 0;
}

SEC("tp/sched/sched_process_exec")
int flow_v2_exec(void *ctx)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_task_meta *meta;
	__u32 old_behavior;
	__u32 pid = (__u32)bpf_get_current_pid_tgid();

	(void)ctx;
	if (!flow_policy_enabled)
		return 0;
	meta = flow_lookup_task(
		(struct task_struct *)bpf_get_current_task_btf());
	if (!meta)
		return 0;
	old_behavior = meta->behavior;
	flow_refresh_tool_role(meta);
	if (meta->behavior != old_behavior) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			stats->exec_reclassifications++;
	}
	if (meta->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE)
		flow_finish_wait(pid);
	if (detailed_accounting) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			__sync_fetch_and_add(&stats->execs, 1);
		flow_emit(FLOW_V2_EVENT_EXEC,
			  (struct task_struct *)bpf_get_current_task_btf(), meta);
	}
	return 0;
}

SEC("tp/sched/sched_process_exit")
int flow_v2_exit_event(void *ctx)
{
	struct flow_v2_session_stats *stats;
	struct flow_v2_tool_runtime *runtime;
	struct flow_v2_task_meta *meta;
	struct task_struct *current;
	__u32 pid = (__u32)bpf_get_current_pid_tgid();

	(void)ctx;
	if (!flow_policy_enabled)
		return 0;
	current = (struct task_struct *)bpf_get_current_task_btf();
	meta = flow_lookup_task(current);
	if (!meta)
		return 0;
	if (meta->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE)
		flow_finish_wait(pid);
	flow_clear_policy_task(pid);
	meta->flags |= FLOW_V2_TASK_EXITED;
	flow_release_tool_member(meta);
	if ((meta->flags & (FLOW_V2_TASK_WAIT_TARGET |
			    FLOW_V2_TASK_PROGRESS_GROUP)) &&
	    !(meta->flags & FLOW_V2_TASK_RETURN_ROOT))
		flow_clear_expired_wait_target(meta, pid);
	runtime = flow_tool_runtime_lookup(meta->session_id, meta->tool_id);
	if (runtime && runtime->sequence == meta->sequence) {
		if (detailed_accounting &&
		    (meta->flags & FLOW_V2_TASK_RUNNABLE) &&
		    runtime->runnable_tasks)
			__sync_fetch_and_sub(&runtime->runnable_tasks, 1);
		runtime->last_exit_ns = bpf_ktime_get_ns();
		__sync_fetch_and_add(&runtime->exit_events, 1);
	}
	if (detailed_accounting) {
		stats = flow_session_stats(meta->session_id);
		if (stats)
			__sync_fetch_and_add(&stats->exits, 1);
		flow_emit(FLOW_V2_EVENT_EXIT, current, meta);
	}
	return 0;
}

SEC("tp_btf/sched_process_free")
int BPF_PROG(flow_v2_free, struct task_struct *p)
{
	__u32 pid;

	if (!flow_policy_enabled || !p)
		return 0;
	pid = p->pid;
	{
		struct flow_v2_task_meta *meta =
			bpf_map_lookup_elem(&flow_v2_task_meta, &pid);

		if (!meta)
			return 0;
		if (meta->flags & FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE)
			flow_finish_wait(pid);
		flow_release_progress_group(meta);
		flow_release_tool_member(meta);
	}
	bpf_map_delete_elem(&flow_v2_waiting_parents, &pid);
	flow_clear_policy_task(pid);
	bpf_map_delete_elem(&flow_v2_task_meta, &pid);
	return 0;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(flow_v2_init)
{
	struct flow_v2_quota_timer_state *timer_state;
	__u32 zero = 0;
	int err;

	err = ____fcg_init(ctx);
	if (err)
		return err;
	err = scx_bpf_create_dsq(FLOW_V2_PROGRESS_DSQ, -1);
	if (err)
		return err;
	err = scx_bpf_create_dsq(FLOW_V2_INTERACTIVE_DSQ, -1);
	if (err)
		return err;
	timer_state = bpf_map_lookup_elem(&quota_timer, &zero);
	if (!timer_state)
		return -ENOENT;
	err = bpf_timer_init(&timer_state->timer, &quota_timer, CLOCK_MONOTONIC);
	if (err)
		return err;
	err = bpf_timer_set_callback(&timer_state->timer, flow_quota_timer_fn);
	if (err || !shared_ordinary_path)
		return err;
	return scx_bpf_create_dsq(FLOW_V2_SHARED_DSQ, -1);
}

s32 BPF_STRUCT_OPS(flow_v2_init_task, struct task_struct *p,
			   struct scx_init_task_args *args)
{
	struct flow_v2_quota_task_state *quota_task;
	int err;

	quota_task = bpf_task_storage_get(&quota_task_state, p, 0,
					  BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!quota_task)
		return -ENOMEM;
	quota_task->previous_sleep_exec_runtime = p->se.sum_exec_runtime;
	quota_task->last_burst_ns = 0;
	quota_task->sleep_started_ns = 0;
	quota_task->last_sleep_ns = 0;
	quota_task->completed_burst = false;
	quota_task->local_futex_wakeup = false;
	quota_task->running_since_ns = 0;
	quota_task->reserved_ns = 0;
	quota_task->fair_running_since_ns = 0;
	quota_task->fair_reserved_ns = 0;
	quota_task->fair_window_id = 0;
	quota_task->fair_runnable_epoch = 0;
	quota_task->counted_running = 0;
	quota_task->counted_runnable = 0;
	quota_task->width_reserved = 0;
	quota_task->counted_wait_target_runnable = 0;
	if (shared_ordinary_path) {
		p->scx.dsq_vtime = flow_v2_shared_vtime;
		return 0;
	}
	err = ____fcg_init_task(ctx, p, args);
	return err;
}

void BPF_STRUCT_OPS(flow_v2_exit_task, struct task_struct *p,
			    struct scx_exit_task_args *args)
{
	struct flow_v2_quota_task_state *quota_task;
	struct flow_v2_session_runtime *runtime;
	struct flow_v2_fair_runtime *fair_runtime;
	struct flow_v2_session_config *cfg;
	struct cgroup *cgrp;
	__u64 now = bpf_ktime_get_ns();
	__u64 cgid;

	(void)args;
	quota_task = bpf_task_storage_get(&quota_task_state, p, 0, 0);
	if (!quota_task)
		return;
	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (!cgrp)
		return;
	cgid = cgrp->kn->id;
	cfg = bpf_map_lookup_elem(&session_config, &cgid);
	if (!cfg)
		goto out;
	runtime = bpf_map_lookup_elem(&session_runtime, &cfg->session_id);
	fair_runtime = bpf_map_lookup_elem(&session_fair_runtime,
					   &cfg->session_id);
	if (fair_runtime && quota_task->fair_running_since_ns &&
	    now > quota_task->fair_running_since_ns) {
		flow_charge_fair_service(cfg, fair_runtime, quota_task, now);
		quota_task->fair_reserved_ns = 0;
	} else if (quota_task->fair_reserved_ns) {
		flow_release_fair_service(cgid, quota_task->fair_reserved_ns);
		quota_task->fair_reserved_ns = 0;
	}
	quota_task->fair_running_since_ns = 0;
	quota_task->fair_window_id = 0;
	quota_task->fair_runnable_epoch = 0;
	if (runtime && quota_task->running_since_ns &&
	    now > quota_task->running_since_ns) {
		flow_charge_session_quota(cgid, cfg, runtime,
			now - quota_task->running_since_ns,
			quota_task->reserved_ns);
		quota_task->reserved_ns = 0;
	} else if (quota_task->reserved_ns) {
		flow_release_session_quota(cgid, quota_task->reserved_ns);
		quota_task->reserved_ns = 0;
	}
	quota_task->running_since_ns = 0;
	if (runtime && quota_task->counted_running) {
		flow_session_dec_running(runtime);
		quota_task->counted_running = 0;
	}
	if (runtime && quota_task->width_reserved) {
		flow_session_dec_reserved(runtime);
		quota_task->width_reserved = 0;
	}
	if (runtime && quota_task->counted_wait_target_runnable) {
		struct flow_v2_task_meta *meta = flow_lookup_task(p);

		flow_set_wait_target_runnable(p, meta, cfg, false);
	}
	if (runtime && quota_task->counted_runnable) {
		flow_session_dec_runnable(runtime, cfg);
		quota_task->counted_runnable = 0;
	}
out:
	bpf_cgroup_release(cgrp);
}

void BPF_STRUCT_OPS(flow_v2_cgroup_exit, struct cgroup *cgrp)
{
	struct task_struct *p;
	__u64 cgid = cgrp->kn->id;

	/*
	 * 短生命周期容器退出时，队列中可能仍有一个已经进入退出路径的
	 * 任务。直接销毁非空队列会使整个 sched_ext 调度器退出。先把
	 * 这些任务移到后备队列，让它们完成退出；控制组离线后不会再有
	 * 新任务进入原队列，因此此后可以安全销毁。
	 */
	bpf_for_each(scx_dsq, p, cgid, 0)
		__COMPAT_scx_bpf_dsq_move(BPF_FOR_EACH_ITER, p,
					  FALLBACK_DSQ, 0);
	bpf_map_delete_elem(&cgv_node_stash, &cgid);
	if (scx_bpf_dsq_nr_queued(cgid) == 0)
		scx_bpf_destroy_dsq(cgid);
}

SCX_OPS_DEFINE(flowcausal_v2_ops,
	.select_cpu		= (void *)flow_v2_select_cpu,
	.enqueue		= (void *)flow_v2_enqueue,
	.dispatch		= (void *)flow_v2_dispatch,
	.runnable		= (void *)flow_v2_runnable,
	.running		= (void *)flow_v2_running,
	.stopping		= (void *)flow_v2_stopping,
	.tick			= (void *)flow_v2_tick,
	.quiescent		= (void *)flow_v2_quiescent,
	.init_task		= (void *)flow_v2_init_task,
	.exit_task		= (void *)flow_v2_exit_task,
	.cgroup_set_weight	= (void *)fcg_cgroup_set_weight,
	.cgroup_init		= (void *)fcg_cgroup_init,
	.cgroup_exit		= (void *)flow_v2_cgroup_exit,
	.cgroup_move		= (void *)fcg_cgroup_move,
	.init			= (void *)flow_v2_init,
	.exit			= (void *)fcg_exit,
	.flags			= SCX_OPS_HAS_CGROUP_WEIGHT | SCX_OPS_ENQ_EXITING,
	.name			= "flowcausal_v2");
