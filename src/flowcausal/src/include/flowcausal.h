/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Shared FlowCausal data structures.
 *
 * FlowCausal is a sched_ext prototype for sandbox workflow scheduling.  The
 * target runtime is no-Python: BPF/sched_ext owns online task/session
 * accounting and scheduling actions, while a small C/libbpf daemon owns
 * loading, configuration, and audit.
 */
#ifndef __FLOWCAUSAL_H
#define __FLOWCAUSAL_H

#include <linux/types.h>

#define FLOWCAUSAL_MAX_COMM          16
#define FLOWCAUSAL_MAX_STAGE_NAME    32

#define FLOWCAUSAL_SHARED_DSQ        0
#define FLOWCAUSAL_FRONTIER_DSQ      1
#define FLOWCAUSAL_LATENCY_DSQ       2
#define FLOWCAUSAL_BACKGROUND_DSQ    3
#define FLOWCAUSAL_CONVERGE_DSQ      4
#define FLOWCAUSAL_CLEANUP_DSQ       5
#define FLOWCAUSAL_ROOT_COPY_DSQ     6
#define FLOWCAUSAL_CPU_SHARED_DSQ_BASE 1024
#define FLOWCAUSAL_SESSION_FRONTIER_DSQ_BASE 4096
#define FLOWCAUSAL_SESSION_FRONTIER_DSQ_SLOTS 16
#define FLOWCAUSAL_MAX_CPUS          1024

#define FLOWCAUSAL_PARENT_EXIT_SESSION_ID 0x9e3779b97f4a7c15ULL

#define FLOWCAUSAL_TASK_FRONTIER     (1U << 0)
#define FLOWCAUSAL_TASK_COHORT_TAIL  (1U << 1)
#define FLOWCAUSAL_TASK_LOCALITY     (1U << 2)
#define FLOWCAUSAL_TASK_WIDTH_HELD   (1U << 3)
#define FLOWCAUSAL_TASK_PROPAGATE    (1U << 4)
#define FLOWCAUSAL_TASK_PRESSURE_HELD (1U << 5)
#define FLOWCAUSAL_TASK_OBSERVE_ONLY (1U << 6)
#define FLOWCAUSAL_TASK_BACKGROUND   (1U << 7)

#define FLOWCAUSAL_TASK_MODE_SHIFT    8
#define FLOWCAUSAL_TASK_MODE_MASK     (7U << FLOWCAUSAL_TASK_MODE_SHIFT)
#define FLOWCAUSAL_TASK_MODE_UNKNOWN  (0U << FLOWCAUSAL_TASK_MODE_SHIFT)
#define FLOWCAUSAL_TASK_MODE_INTERACTIVE (1U << FLOWCAUSAL_TASK_MODE_SHIFT)
#define FLOWCAUSAL_TASK_MODE_COMPUTE   (2U << FLOWCAUSAL_TASK_MODE_SHIFT)
#define FLOWCAUSAL_TASK_MODE_TAIL      (3U << FLOWCAUSAL_TASK_MODE_SHIFT)
#define FLOWCAUSAL_TASK_MODE_BACKGROUND (4U << FLOWCAUSAL_TASK_MODE_SHIFT)

#define FLOWCAUSAL_TASK_BPF_AUTO       (1U << 11)
#define FLOWCAUSAL_TASK_C_ASSIST       (1U << 12)
#define FLOWCAUSAL_TASK_CONVERGE_TAIL  (1U << 13)
#define FLOWCAUSAL_TASK_STAGE_BUILD_WIDE (1U << 14)
#define FLOWCAUSAL_TASK_STAGE_TEST_TAIL  (1U << 15)
#define FLOWCAUSAL_TASK_STAGE_PACKAGE_BG (1U << 16)
#define FLOWCAUSAL_TASK_STAGE_INSPECT    (1U << 17)
#define FLOWCAUSAL_TASK_PARENT_LATENCY   (1U << 18)
#define FLOWCAUSAL_TASK_ROOT             (1U << 19)
#define FLOWCAUSAL_TASK_ROOT_STAGE_STARTED (1U << 20)
#define FLOWCAUSAL_TASK_PACKAGE_BG_MARK  (1U << 21)
#define FLOWCAUSAL_TASK_ROOT_DIRECT_THIRD (1U << 22)
#define FLOWCAUSAL_TASK_ROOT_DIRECT_SECOND (1U << 23)
#define FLOWCAUSAL_TASK_STAGE_MASK \
	(FLOWCAUSAL_TASK_STAGE_BUILD_WIDE | \
	 FLOWCAUSAL_TASK_STAGE_TEST_TAIL | \
	 FLOWCAUSAL_TASK_STAGE_PACKAGE_BG | \
	 FLOWCAUSAL_TASK_STAGE_INSPECT)

#define FLOWCAUSAL_POLICY_ENABLE     (1U << 0)
#define FLOWCAUSAL_POLICY_LOCALITY   (1U << 1)
#define FLOWCAUSAL_POLICY_WIDTH_CTL  (1U << 2)
#define FLOWCAUSAL_POLICY_FALLBACK   (1U << 4)

enum flowcausal_stage_kind {
	FLOW_STAGE_UNKNOWN = 0,
	FLOW_STAGE_MODEL_WAIT = 1,
	FLOW_STAGE_SHELL = 2,
	FLOW_STAGE_SEARCH = 3,
	FLOW_STAGE_BUILD = 4,
	FLOW_STAGE_TEST = 5,
	FLOW_STAGE_RETRY = 6,
	FLOW_STAGE_IO_WAIT = 7,
};

enum flowcausal_event_kind {
	FLOW_EVT_FORK = 1,
	FLOW_EVT_EXEC = 2,
	FLOW_EVT_EXIT = 3,
	FLOW_EVT_WAKEUP = 4,
	FLOW_EVT_SWITCH_IN = 5,
	FLOW_EVT_SWITCH_OUT = 6,
	FLOW_EVT_WAIT = 7,
	FLOW_EVT_FRONTIER_ENQUEUE = 8,
	FLOW_EVT_FALLBACK = 9,
	FLOW_EVT_CLEANUP = 10,
};

struct flow_task_key {
	__u32 pid;
};

struct flow_task_ctx {
	__u64 session_id;
	__u64 stage_id;
	__u64 cohort_id;
	__u64 enqueue_ts_ns;
	__u64 running_since_ns;
	__u64 runnable_wait_ns;
	__u64 runtime_ns;
	__u64 last_update_ns;
	__u32 tgid;
	__u32 parent_pid;
	__u32 flags;
	__u32 confidence;       /* 0..100 */
	__s32 anchor_cpu;       /* -1 means no placement hint */
	__u32 propagation_depth;
};

struct flow_session_policy {
	__u32 flags;
	__u32 min_confidence;       /* 0..100 */
	__u32 max_frontier_width;   /* advisory */
	__u32 max_vtime_boost;      /* ns in scheduler virtual time units */
	__u64 window_ns;
	__u64 boost_budget_ns;
	__u64 budget_used_ns;
	__u64 budget_window_start_ns;
	__u64 expires_ns;
	__s32 anchor_cpu;
	__u32 anchor_span;
};

struct flow_session_stats {
	__u64 seen_enqueues;
	__u64 frontier_enqueues;
	__u64 fallback_enqueues;
	__u64 width_held_enqueues;
	__u64 frontier_inflight;
	__u64 frontier_width_releases;
	__u64 total_runnable_wait_ns;
	__u64 frontier_runnable_wait_ns;
	__u64 total_runtime_ns;
	__u64 frontier_runtime_ns;
	__u64 last_event_ns;
};

struct flow_global_stats {
	__u64 frontier_dispatches;
	__u64 shared_dispatches;
	__u64 shared_first_dispatches;
	__u64 frontier_guard_trips;
	__u64 frontier_guard_misses;
	__u64 shared_cooldown_entries;
	__u64 shared_cooldown_dispatches;
	__u64 shared_cooldown_misses;
	__u64 pressure_fallback_entries;
	__u64 pressure_fallback_enqueues;
	__u64 frontier_budget_windows;
	__u64 frontier_budget_trips;
	__u64 frontier_budget_shared_dispatches;
	__u64 frontier_budget_misses;
	__u64 shared_wait_pressure_events;
	__u64 shared_wait_pressure_wait_ns;
	__u64 pressure_dispatch_entries;
	__u64 pressure_dispatches;
	__u64 pressure_dispatch_misses;
	__u64 shared_wakeup_preempt_entries;
	__u64 shared_wakeup_preempt_dispatches;
	__u64 shared_wakeup_preempt_misses;
	__u64 shared_local_enqueues;
	__u64 shared_percpu_enqueues;
	__u64 shared_percpu_dispatches;
	__u64 shared_percpu_misses;
	__u64 frontier_wakeup_preempt_entries;
	__u64 background_enqueues;
	__u64 background_dispatches;
	__u64 frontier_dsq_enqueues;
	__u64 tracked_shared_path_enqueues;
	__u64 untracked_shared_path_enqueues;
	__u64 shared_global_enqueues;
	__u64 shared_global_tracked_enqueues;
	__u64 shared_global_untracked_enqueues;
	__u64 latency_dsq_enqueues;
	__u64 shared_local_tracked_enqueues;
	__u64 shared_local_untracked_enqueues;
	__u64 shared_percpu_tracked_enqueues;
	__u64 shared_percpu_untracked_enqueues;
	__u64 shared_local_pick_fallbacks;
	__u64 shared_percpu_pick_fallbacks;
	__u64 shared_local_frontier_fallback_enqueues;
	__u64 mode_interactive_enqueues;
	__u64 mode_compute_enqueues;
	__u64 mode_tail_enqueues;
	__u64 mode_background_enqueues;
	__u64 mode_interactive_dispatches;
	__u64 mode_compute_dispatches;
	__u64 mode_tail_dispatches;
	__u64 mode_background_dispatches;
	__u64 converge_dsq_enqueues;
	__u64 converge_dispatches;
	__u64 session_frontier_dsq_enqueues;
	__u64 session_frontier_dispatches;
	__u64 adaptive_width_tail_admissions;
	__u64 adaptive_width_wide_admissions;
	__u64 adaptive_width_held_enqueues;
	__u64 root_copy_dsq_enqueues;
	__u64 root_copy_dispatches;
};

struct flow_event {
	__u64 ts_ns;
	__u64 session_id;
	__u64 stage_id;
	__u64 cohort_id;
	__u32 pid;
	__u32 tgid;
	__u32 ppid;
	__u32 kind;
	__u32 flags;
	__u32 cpu;
	char comm[FLOWCAUSAL_MAX_COMM];
};

#endif /* __FLOWCAUSAL_H */
