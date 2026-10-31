/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __FLOWCAUSAL_V2_H
#define __FLOWCAUSAL_V2_H

#include <linux/types.h>

#define FLOW_V2_COMM_LEN 16
#define FLOW_V2_MAX_SESSIONS 4096
#define FLOW_V2_MAX_TASKS 131072
#define FLOW_V2_MAX_TOOLS 16384
#define FLOW_V2_ACTIVE_SESSION_SLOTS 64
#define FLOW_V2_HEARTBEAT_TIMEOUT_NS (2ULL * 1000 * 1000 * 1000)
#define FLOW_V2_PROTOCOL_VERSION 2

/*
 * 原始研究方案规定的几种信息来源对照。完整模式同时使用工具边界和
 * 内核等待关系；其余模式只用于实验，不能与完整系统混写。
 */
enum flow_v2_decision_mode {
	FLOW_V2_DECISION_RELATION = 0,
	FLOW_V2_DECISION_STAGE = 1,
	FLOW_V2_DECISION_TOOL = 2,
	FLOW_V2_DECISION_KERNEL = 3,
	FLOW_V2_DECISION_MANUAL = 4,
};

enum flow_v2_role {
	FLOW_V2_ROLE_NORMAL = 0,
	FLOW_V2_ROLE_PROGRESS = 1,
	FLOW_V2_ROLE_INTERACTIVE = 2,
	FLOW_V2_ROLE_PARALLEL = 3,
	FLOW_V2_ROLE_BACKGROUND = 4,
	FLOW_V2_NR_ROLES = 5,
};

enum flow_v2_role_source {
	FLOW_V2_SOURCE_DEFAULT = 0,
	FLOW_V2_SOURCE_KERNEL = 1,
	FLOW_V2_SOURCE_TOOL = 2,
};

enum flow_v2_stage {
	FLOW_V2_STAGE_UNKNOWN = 0,
	FLOW_V2_STAGE_PREPARE = 1,
	FLOW_V2_STAGE_INSPECT = 2,
	FLOW_V2_STAGE_EDIT = 3,
	FLOW_V2_STAGE_BUILD = 4,
	FLOW_V2_STAGE_TEST = 5,
	FLOW_V2_STAGE_DATA = 6,
	FLOW_V2_STAGE_PACKAGE = 7,
	FLOW_V2_STAGE_SERVICE = 8,
};

/* 执行特征只参与并行度和分析，不单独产生调度优先级。 */
enum flow_v2_behavior {
	FLOW_V2_BEHAVIOR_UNKNOWN = 0,
	FLOW_V2_BEHAVIOR_SERIAL = 1,
	FLOW_V2_BEHAVIOR_PARALLEL = 2,
	FLOW_V2_BEHAVIOR_SERVICE = 3,
};

enum flow_v2_event_kind {
	FLOW_V2_EVENT_SESSION_OPEN = 1,
	FLOW_V2_EVENT_SESSION_CLOSE = 2,
	FLOW_V2_EVENT_TOOL_BEGIN = 3,
	FLOW_V2_EVENT_TOOL_UPDATE = 4,
	FLOW_V2_EVENT_TOOL_END = 5,
	FLOW_V2_EVENT_FORK = 6,
	FLOW_V2_EVENT_EXEC = 7,
	FLOW_V2_EVENT_EXIT = 8,
	FLOW_V2_EVENT_ROLE_CHANGE = 9,
	FLOW_V2_EVENT_WAIT_LINK = 10,
	FLOW_V2_EVENT_WAIT_BLOCKED = 11,
};

enum flow_v2_session_flags {
	FLOW_V2_SESSION_ENABLED = 1U << 0,
	/* 长驻 C 控制程序定期刷新 heartbeat_ns。 */
	FLOW_V2_SESSION_LEASED = 1U << 1,
};

enum flow_v2_tool_flags {
	/* 只用于人工标注对照，不由完整模式读取。 */
	FLOW_V2_TOOL_MANUAL_PROGRESS = 1U << 0,
};

enum flow_v2_task_flags {
	FLOW_V2_TASK_TAIL_PROMOTED = 1U << 0,
	FLOW_V2_TASK_TOOL_MEMBER = 1U << 1,
	FLOW_V2_TASK_WAIT_TARGET = 1U << 2,
	FLOW_V2_TASK_WAITING_CHILD = 1U << 3,
	FLOW_V2_TASK_STALE_GENERATION = 1U << 4,
	FLOW_V2_TASK_RUNNABLE = 1U << 5,
	FLOW_V2_TASK_RETURN_ROOT = 1U << 6,
	FLOW_V2_TASK_RETURN_CANDIDATE = 1U << 7,
	FLOW_V2_TASK_REJECTED_NOT_TAIL = 1U << 8,
	FLOW_V2_TASK_REJECTED_NO_WAIT = 1U << 9,
	FLOW_V2_TASK_BACKGROUND_RESCUED = 1U << 10,
	FLOW_V2_TASK_WAIT_SYSCALL_ACTIVE = 1U << 11,
	FLOW_V2_TASK_NONBLOCKING_WAIT = 1U << 12,
	FLOW_V2_TASK_SCOPE_ROOT = 1U << 13,
	FLOW_V2_TASK_WAIT_ANY_CHILD = 1U << 14,
	FLOW_V2_TASK_EXITED = 1U << 15,
	/* live_tasks 由内核认领并且只释放一次。 */
	FLOW_V2_TASK_LIVE_ACCOUNTED = 1U << 16,
	/* 同一轮阻塞等待中被有界纳入推进范围的成员。 */
	FLOW_V2_TASK_PROGRESS_GROUP = 1U << 17,
	/* 旧工具留下的任务正在给当前阻塞工具让出处理器。 */
	FLOW_V2_TASK_STALE_DELAYED = 1U << 18,
	/* 已观察到等待进程以睡眠状态让出处理器。 */
	FLOW_V2_TASK_WAIT_BLOCKED = 1U << 19,
	/* 从一次已经确认的阻塞等待中恢复，只允许提前运行一次。 */
	FLOW_V2_TASK_INTERACTIVE_ONCE = 1U << 20,
	/* 该等待进程已计入动态会话权重，只能计入和释放一次。 */
	FLOW_V2_TASK_ADAPTIVE_WAITER = 1U << 21,
	/* 唯一推进名额由已睡眠的父进程转交，允许一次有限的首次运行。 */
	FLOW_V2_TASK_RELAYED_PROGRESS = 1U << 22,
	/* 当前任务已经睡眠，可作为同次工具后代的运行依据。 */
	FLOW_V2_TASK_SLEEPING = 1U << 23,
	/* 同次工具的上层进程睡眠时，该后代已经观察到处理器排队。 */
	FLOW_V2_TASK_SLEEPING_DESCENDANT = 1U << 24,
	/* 本次短时提前来自“睡眠上层进程+后代排队”规则。 */
	FLOW_V2_TASK_DESCENDANT_PROMOTED = 1U << 25,
	/* 本进程刚刚唤醒同一工具内的睡眠上层进程，等待再次入队。 */
	FLOW_V2_TASK_DESCENDANT_WAKER = 1U << 26,
	/* 被唤醒的是工具入口或工具执行范围入口。 */
	FLOW_V2_TASK_DESCENDANT_ROOT_WAKER = 1U << 27,
	/* 实际交付过进展的进程再次入队时，在所属工作流内提前一次。 */
	FLOW_V2_TASK_DESCENDANT_WAKER_PROMOTED = 1U << 28,
	/* 本次提前来自直接唤醒工具入口，可参加公共关键进程队列。 */
	FLOW_V2_TASK_DESCENDANT_ROOT_WAKER_PROMOTED = 1U << 29,
};

enum flow_v2_role_reason {
	FLOW_V2_REASON_NONE = 0,
	FLOW_V2_REASON_TOOL_EXECUTION = 1,
	FLOW_V2_REASON_UNUSED = 2,
	FLOW_V2_REASON_WAIT_TARGET = 3,
	FLOW_V2_REASON_RETURN_COHORT = 4,
	FLOW_V2_REASON_STALE_GENERATION = 5,
	FLOW_V2_REASON_WAIT_RESULT_READY = 6,
	FLOW_V2_REASON_SLEEPING_ANCESTOR = 7,
};

struct flow_v2_tool_key {
	__u64 session_id;
	__u64 tool_id;
};

struct flow_v2_session_config {
	__u64 session_id;
	__u64 cgroup_id;
	__u64 opened_ns;
	__u64 policy_window_ns;
	__u64 boost_budget_ns;
	__u64 progress_bias_ns;
	__u64 interactive_bias_ns;
	__u64 background_delay_ns;
	/* cpu.max 中的配额和周期；quota 为零表示不限制。 */
	__u64 cpu_quota_ns;
	__u64 cpu_period_ns;
	__u64 heartbeat_ns;
	__u64 last_sequence;
	__u32 weight;
	__u32 flags;
	__u32 max_progress_group;
	__u32 max_progress_depth;
	__u32 controller_pid;
	__u32 reserved;
};

/* 只由 eBPF 在可运行、开始运行和停止运行路径中更新。 */
struct flow_v2_width_state {
	__u64 total_weight;
	/* 当前至少有一个可运行任务的会话权重之和。 */
	__u64 runnable_weight;
	/* 动态权重相对基础权重的有符号增量，独立保存以避免并发减法下溢。 */
	__s64 adaptive_weight_delta;
	/* 可运行会话集合改变时递增，使服务周期及时重新开始。 */
	__u64 runnable_epoch;
	__u32 registered_sessions;
	/* 当前至少有一个可运行任务的会话数。 */
	__u32 runnable_sessions;
	/* 所有会话当前可运行任务数之和。 */
	__u32 runnable_demand;
	/* 各会话 1 + floor(log2(可运行任务数)) 之和。 */
	__u32 runnable_demand_score;
	/* 当前在受管处理器上实际运行的会话任务数。 */
	__u32 running_tasks;
	__u32 reserved;
};

/*
 * 只由 C 控制程序修改的登记状态。它与内核在 width_state 中更新的
 * 可运行状态分开保存，避免用户态整项写回覆盖并发的调度事件。
 */
struct flow_v2_control_state {
	__u64 total_weight;
	/* 会话异常结束时，内核原始计数中需要排除的可运行权重。 */
	__u64 disabled_runnable_weight;
	/* 排除项变化也必须使公平运行周期重新开始。 */
	__u64 runnable_epoch;
	__u32 registered_sessions;
	__u32 disabled_runnable_sessions;
	__u32 disabled_runnable_demand;
	__u32 disabled_runnable_score;
	__u32 disabled_running_tasks;
	__u32 managed_cpu_count;
	/* C 控制程序在会话登记变化时核对全部基础权重。 */
	__u32 common_weight;
	__u32 registered_weights_equal;
	/* 本次调度器运行期间同时登记过的会话数上限。会话关闭后保留，
	 * 用于判断是否已经有工作流释放了处理器。 */
	__u32 peak_registered_sessions;
};

struct flow_v2_tool_config {
	__u64 parent_tool_id;
	__u64 started_ns;
	__u64 updated_ns;
	__u64 sequence;
	__u64 generation;
	__u32 role;
	__u32 stage;
	__u32 active;
	__u32 flags;
};

/* BPF-owned state for one tool generation. */
struct flow_v2_tool_runtime {
	__u64 started_ns;
	__u64 ended_ns;
	__u64 last_wait_ns;
	__u64 last_exit_ns;
	__u64 sequence;
	__u32 root_pid;
	__u32 live_tasks;
	__u32 peak_live_tasks;
	__u32 wait_links;
	__u32 cross_generation_wait_links;
	__u32 active_wait_targets;
	__u32 peak_wait_targets;
	__u32 exit_events;
	__u32 runnable_tasks;
	__u32 peak_runnable_tasks;
	__u32 active_waiters;
	__u32 peak_active_waiters;
	/* 工具从无等待者变为有等待者时递增。 */
	__u64 descendant_wait_round;
	/* 同一轮工具等待只允许一个普通后代获得一次短时间片。 */
	__u64 descendant_granted_round;
	/* 工具入口等待期间，普通后代实际运行和排队形成的会话级证据。 */
	__u64 sustained_wait_round;
	__u64 sustained_run_events;
	__u64 sustained_queue_ns;
	__s32 preferred_cpu;
	__u32 preferred_cpu_valid;
	__u32 active;
	__u32 reserved;
};

/* 当前会话正在执行的工具，用于按需关联实际进入 wait 的进程。 */
struct flow_v2_active_tool {
	__u64 session_id;
	__u64 cgroup_id;
	__u64 tool_id;
	__u64 parent_tool_id;
	__u64 sequence;
	__u32 role;
	__u32 role_source;
	__u32 stage;
	__u32 behavior;
	__u32 active;
	__u32 reserved;
};

struct flow_v2_task_meta {
	__u64 session_id;
	__u64 cgroup_id;
	__u64 tool_id;
	__u64 parent_tool_id;
	__u64 enqueue_ns;
	__u64 running_ns;
	__u64 run_slice_start_ns;
	__u64 runnable_wait_ns;
	__u64 runtime_ns;
	__u64 last_role_change_ns;
	__u64 birth_ns;
	__u64 last_runnable_wait_ns;
	__u64 last_runnable_wait_at_ns;
	__u64 promotion_evidence_ns;
	__u64 last_run_at_ns;
	__u64 wait_started_ns;
	/* task_struct::start_boottime，避免进程号复用继承旧关系。 */
	__u64 task_start_ns;
	__u64 wait_round;
	__u64 linked_wait_round;
	/* 同一顶层阻塞等待中，每个成组成员只允许推进一次。 */
	__u64 promoted_wait_round;
	/* 每次从运行转为睡眠时递增，用于区分相邻两次阻塞。 */
	__u64 sleep_round;
	__u64 sleep_started_ns;
	/* 后代保存建立候选关系时观察到的工具等待编号。 */
	__u64 linked_descendant_round;
	__u64 stale_delay_started_ns;
	__u64 reserved_bias_ns;
	__u64 bias_window_start_ns;
	__u64 blocking_tool_id;
	__u64 sequence;
	__u32 parent_pid;
	__u32 waiter_pid;
	__u32 wait_target_pid;
	__u32 live_children;
	__u32 peak_children;
	__u32 progress_group_members;
	/* 当前等待轮次累计接纳的成员数，成员退出后也不释放名额。 */
	__u32 progress_group_admitted;
	__u32 progress_group_depth;
	__u32 sleeping_ancestor_pid;
	__u32 sleeping_ancestor_depth;
	/* 最近创建的直接子进程，用于确认阻塞后补齐已在运行的成员。 */
	__u32 recent_child_pids[8];
	__u32 recent_child_cursor;
	__s32 last_cpu;
	__u32 role;
	__u32 role_source;
	__u32 role_reason;
	__u32 stage;
	__u32 behavior;
	__u64 blocking_sequence;
	__s32 wait_selector;
	__u32 wait_options;
	__u32 flags;
};

struct flow_v2_session_stats {
	__u64 enqueues;
	__u64 runs;
	__u64 runnable_wait_ns;
	__u64 runtime_ns;
	__u64 applied_bias_ns;
	__u64 budget_limited;
	__u64 forks;
	__u64 execs;
	__u64 exits;
	__u64 tail_promotions;
	__u64 wait_events;
	__u64 wait_links;
	__u64 cross_generation_wait_links;
	__u64 return_candidates;
	__u64 return_promotions;
	__u64 return_root_promotions;
	__u64 wait_target_promotions;
	__u64 wait_any_links;
	__u64 return_rejected_no_wait;
	__u64 return_rejected_not_tail;
	__u64 stale_generation_tasks;
	__u64 refunded_bias_ns;
	__u64 background_rescues;
	__u64 ignored_nonblocking_waits;
	__u64 confirmed_blocking_waits;
	__u64 ignored_immediate_waits;
	__u64 ignored_group_waits;
	__u64 provenance_tasks;
	__u64 reused_pid_replacements;
	__u64 progress_group_links;
	__u64 progress_group_promotions;
	/* 由已经确认的等待关系继续向下连接的进程及其实际推进。 */
	__u64 nested_wait_links;
	__u64 nested_wait_promotions;
	__u64 max_progress_depth_observed;
	/* 有界推进名额从阻塞父进程转给其等待子进程。 */
	__u64 progress_group_relays;
	__u64 max_progress_relay_depth;
	/* 已确认阻塞关系后、无需等待第二次入队采样的首次推进。 */
	__u64 first_relation_promotions;
	/* 上层进程睡眠期间，普通后代排队形成的候选及实际短时提前。 */
	__u64 sleeping_descendant_candidates;
	__u64 sleeping_descendant_promotions;
	__u64 sleeping_descendant_invalidations;
	__u64 max_sleeping_descendant_depth;
	/* 同一工具后代持续运行、持续排队及其形成的会话权重候选。 */
	__u64 sustained_descendant_runs;
	__u64 sustained_descendant_queue_ns;
	__u64 sustained_pressure_events;
	__u64 sustained_pressure_activations;
	__u64 sustained_pressure_cooldowns;
	/* 同一工具的后代唤醒其睡眠上层进程，用于识别管道等等待。 */
	__u64 descendant_wake_links;
	__u64 descendant_wake_root_links;
	__u64 descendant_wake_queued_links;
	__u64 descendant_waker_queue_ns;
	__u64 descendant_wake_long_links;
	__u64 descendant_wake_pressure_links;
	__u64 descendant_wake_sleep_ns;
	__u64 descendant_wake_max_sleep_ns;
	__u64 descendant_waker_marks;
	__u64 descendant_waker_requeues;
	__u64 descendant_root_waker_marks;
	__u64 descendant_root_waker_requeues;
	__u64 descendant_waker_promotions;
	__u64 descendant_waker_promotion_runs;
	__u64 descendant_root_waker_promotions;
	__u64 stale_delays;
	__u64 locality_hits;
	__u64 locality_fallbacks;
	__u64 interactive_wakeups;
	__u64 interactive_preemptions;
	__u64 interactive_runs;
	__u64 exec_reclassifications;
	/* 会话实际处理器时间达到权重目标后的等待与空闲能力借用。 */
	__u64 fair_dispatch_rejections;
	__u64 fair_dispatch_borrows;
	__u64 session_width_limits;
	__u64 session_width_borrows;
	/* 由真实阻塞等待和可运行等待目标共同触发的临时会话权重。 */
	__u64 adaptive_weight_activations;
	__u64 adaptive_weight_deactivations;
	__u64 adaptive_weight_active_ns;
	__u64 adaptive_weight_dispatches;
	__u64 adaptive_weight_changes;
	__u64 adaptive_weight_peak;
	/* 完全自动权重停止的原因和跨会话轮换次数。 */
	__u64 automatic_weight_timeouts;
	__u64 automatic_weight_condition_releases;
	__u64 automatic_weight_wait_returns;
	__u64 automatic_weight_handoffs;
	/* 并发更新中发现本会话已不再是临时加权对象时的恢复次数。 */
	__u64 automatic_weight_owner_change_releases;
	__u64 automatic_weight_pressure_waits;
	__u64 automatic_weight_max_active_ns;
	/* 自动权重只补回会话在当前公平周期内少得到的处理器时间。 */
	__u64 automatic_weight_service_releases;
	__u64 automatic_weight_planned_ns;
	__u64 automatic_weight_service_ns;
	__u64 quota_throttles;
	__u64 quota_runtime_ns;
	__u64 quota_dispatch_rejections;
	__u64 quota_direct_rejections;
	__u64 parallel_width_limits;
	__u64 parallel_width_borrows;
	__u64 role_enqueues[FLOW_V2_NR_ROLES];
	__u64 role_runtime_ns[FLOW_V2_NR_ROLES];
};

struct flow_v2_global_stats {
	__u64 tracked_enqueues;
	__u64 untracked_enqueues;
	__u64 fallback_enqueues;
	__u64 tracked_runs;
	__u64 role_changes;
	__u64 emitted_events;
	__u64 dropped_events;
	__u64 stale_tool_updates;
	__u64 missing_session;
	__u64 session_rescues;
	/* 内核状态空间不足时记录并回到普通调度。 */
	__u64 map_update_failures;
};

struct flow_v2_event {
	__u64 timestamp_ns;
	__u64 session_id;
	__u64 cgroup_id;
	__u64 tool_id;
	__u64 sequence;
	__u32 kind;
	__u32 pid;
	__u32 related_pid;
	__u32 parent_pid;
	__u32 role;
	__u32 role_source;
	__u32 role_reason;
	__u32 stage;
	__u32 behavior;
	__u32 tool_live_tasks;
	__u32 tool_peak_live_tasks;
	__u32 tool_active_wait_targets;
	__u32 tool_peak_wait_targets;
	__u32 task_flags;
	char comm[FLOW_V2_COMM_LEN];
};

#endif /* __FLOWCAUSAL_V2_H */
