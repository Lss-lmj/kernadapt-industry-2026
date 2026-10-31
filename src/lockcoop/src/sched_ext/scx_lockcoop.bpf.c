/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_lockcoop: composed LockBoost + LockNest scheduler.
 *
 * LockNest consumes advisory waiter/anchor evidence.  LockBoost consumes
 * only verified kernel-mutex owners, or a validated pthread owner-word
 * resolver for private futex mutexes.  An ordinary futex waker is never an
 * owner.
 */
#include <scx/common.bpf.h>
#include <scx/lock_observe.bpf.h>

#ifndef LOCKCOOP_OPS
#define LOCKCOOP_OPS lockcoop_ops
#endif

#ifndef LOCKCOOP_SCHED_NAME
#define LOCKCOOP_SCHED_NAME "lockcoop"
#endif

#ifndef LOCKCOOP_DEFAULT_NEST_ENABLED
#define LOCKCOOP_DEFAULT_NEST_ENABLED true
#endif

#ifndef LOCKCOOP_DEFAULT_BOOST_ENABLED
#define LOCKCOOP_DEFAULT_BOOST_ENABLED true
#endif

#ifndef LOCKCOOP_DEFAULT_BOOST_PROFILE
#define LOCKCOOP_DEFAULT_BOOST_PROFILE BOOST_PROFILE_BOUNDED
#endif

char _license[] SEC("license") = "GPL";

#define SHARED_DSQ 0
#define ANCHOR_CPU_DISABLED (-2)

enum stat_idx {
	STAT_LOCAL = 0,
	STAT_GLOBAL,
	STAT_MUTEX_OWNER_HIT,
	STAT_MUTEX_OWNER_MISS,
	STAT_FUTEX_PRIVATE_WAIT,
	STAT_FUTEX_PRIVATE_OWNER_HIT,
	STAT_FUTEX_PRIVATE_OWNER_MISS,
	STAT_FUTEX_WAKE,
	STAT_FUTEX_SHARED_FALLBACK,
	STAT_FUTEX_PI_FALLBACK,
	STAT_FUTEX_REQUEUE_FALLBACK,
	STAT_OWNER_HIT,
	STAT_OWNER_MISS,
	STAT_WAITER_HIT,
	STAT_WAITER_MISS,
	STAT_LOCALITY_HIT,
	STAT_LOCALITY_FALLBACK,
	STAT_STALE_NEST,
	STAT_BOOST_APPLIED,
	STAT_BOOST_BLOCKED_NO_OWNER,
	STAT_MAP_UPDATE_FAIL,
	STAT_WAITER_OVERWRITE,
	STAT_WAITER_CLEANUP,
	STAT_COUNTER_UNDERFLOW,
	STAT_WAITER_ORPHAN_CLEANUP,
	STAT_ANCHOR_CREATE,
	STAT_ANCHOR_REFRESH,
	STAT_ANCHOR_UPGRADE,
	STAT_DECAY_CLEANUP,
	STAT_ANCHOR_PRESSURE_REJECT,
	STAT_ANCHOR_CPUMASK_REJECT,
	STAT_ANCHOR_COOLDOWN_REJECT,
	STAT_ANCHOR_PREVCPU_FALLBACK,
	STAT_ANCHOR_DEFAULT_FALLBACK,
	STAT_ANCHOR_EXPIRY_SHORTEN,
	STAT_ANCHOR_EXPIRY_EXTEND,
	STAT_BOOST_CONF_DENIED,
	STAT_BOOST_BUDGET_DENIED,
	STAT_BOOST_LONGCS_DENIED,
	STAT_BOOST_REVOKE_UNLOCK,
	STAT_OWNER_DELAY_SUM_NS,
	STAT_OWNER_DELAY_COUNT,
	STAT_WAITERS_UNBLOCKED_AFTER_BOOST,
	STAT_BOOST_INF_APPLIED,
	STAT_BOOST_SHORT_APPLIED,
	STAT_BOOST_BOUNDED_APPLIED,
	STAT_BOOST_ADAPTIVE_APPLIED,
	STAT_BOOST_TASK_BUDGET_DENIED,
	STAT_BOOST_LOCK_BUDGET_DENIED,
	STAT_BOOST_CGROUP_BUDGET_DENIED,
	STAT_CONF_NONE,
	STAT_CONF_WAITER_ONLY,
	STAT_CONF_PROBABLE,
	STAT_CONF_VERIFIED,
	STAT_THRESHOLD_UP,
	STAT_THRESHOLD_DOWN,
	STAT_ANCHOR_MATURITY_UPDATE,
	STAT_ANCHOR_MATURITY_COMPATIBLE_UPDATE,
	STAT_ANCHOR_MATURITY_RESET_CPU,
	STAT_ANCHOR_MATURITY_RESET_SOURCE,
	STAT_ANCHOR_MATURITY_RESET_STALE,
	STAT_ANCHOR_MATURITY_REJECT,
	STAT_ANCHOR_MATURITY_PASS,
	STAT_ANCHOR_MATURITY_STRONG_PASS,
	STAT_ANCHOR_MATURITY_WEAK_PASS,
	STAT_ANCHOR_MATURITY_PHASE_RESET,
	STAT_EVIDENCE_SEEN,
	STAT_EVIDENCE_ADMITTED,
	STAT_EVIDENCE_SCOPE_DENIED,
	STAT_EVIDENCE_CLEANUP_OUT_OF_SCOPE,
	STAT_SCOPE_ADMIT_MUTEX_ENTER,
	STAT_SCOPE_ADMIT_MUTEX_SLOWPATH,
	STAT_SCOPE_ADMIT_MUTEX_UNLOCK,
	STAT_SCOPE_ADMIT_FUTEX_WAIT,
	STAT_SCOPE_ADMIT_FUTEX_WAKE,
	STAT_SCOPE_DENY_MUTEX_ENTER,
	STAT_SCOPE_DENY_MUTEX_SLOWPATH,
	STAT_SCOPE_DENY_MUTEX_UNLOCK,
	STAT_SCOPE_DENY_FUTEX_WAIT,
	STAT_SCOPE_DENY_FUTEX_WAKE,
	STAT_BOOST_RUNNING_APPLIED,
	STAT_BOOST_TICK_APPLIED,
	STAT_OWNER_OBS_RUNNING,
	STAT_OWNER_OBS_RUNNABLE,
	STAT_OWNER_OBS_SLEEPING,
	STAT_URGENT_OWNER_MARK,
	STAT_URGENT_OWNER_DISPATCH,
	STAT_URGENT_OWNER_SCAN_TASK,
	STAT_OWNER_WORD_REJECT,
	STAT_OWNER_TASK_MISS,
	STAT_OWNER_MM_MISMATCH,
	STAT_OWNER_VALIDATED,
	STAT_SYNC_EVENT_WAIT,
	STAT_SYNC_EVENT_ACQUIRE,
	STAT_SYNC_EVENT_RELEASE,
	STAT_RWSEM_READ_ACQUIRE,
	STAT_RWSEM_WRITE_ACQUIRE,
	STAT_RWSEM_READ_WAIT,
	STAT_RWSEM_WRITE_WAIT,
	STAT_RWSEM_RELEASE,
	STAT_SHARED_FUTEX_WAIT,
	STAT_SHARED_FUTEX_WAKE,
	STAT_SHARED_FUTEX_KEY_MISS,
	STAT_PI_FUTEX_WAIT,
	STAT_PI_FUTEX_ACQUIRE,
	STAT_PI_FUTEX_RELEASE,
	STAT_PI_OWNER_HIT,
	STAT_PI_OWNER_MISS,
	STAT_REQUEUE_OP,
	STAT_REQUEUE_TRANSFER,
	STAT_REQUEUE_KEY_MISS,
	STAT_PTHREAD_RWLOCK_READ,
	STAT_PTHREAD_RWLOCK_WRITE,
	STAT_PTHREAD_RWLOCK_WAIT,
	STAT_PTHREAD_RWLOCK_RELEASE,
	STAT_USER_SPIN_WAIT,
	STAT_USER_SPIN_ACQUIRE,
	STAT_USER_SPIN_RELEASE,
	STAT_USER_CAS_WAIT,
	STAT_USER_CAS_ACQUIRE,
	STAT_USER_CAS_RELEASE,
	STAT_PATH_NEST_ACTION,
	STAT_PATH_SHORT_BOOST_ACTION,
	STAT_PATH_STRONG_BOOST_ACTION,
	STAT_PATH_BOOST_DENIED,
	STAT_PI_KEY_MISS,
	STAT_DIAG_ANCHOR_IDLE,
	STAT_DIAG_ANCHOR_BUSY,
	STAT_DIAG_ANCHOR_OCCUPANT_UNKNOWN,
	STAT_DIAG_ANCHOR_SAME_TGID,
	STAT_DIAG_ANCHOR_SAME_CGROUP,
	STAT_DIAG_ANCHOR_DIFFERENT_TGID,
	STAT_DIAG_DIRECT_Q_0,
	STAT_DIAG_DIRECT_Q_1,
	STAT_DIAG_DIRECT_Q_2_3,
	STAT_DIAG_DIRECT_Q_4_7,
	STAT_DIAG_DIRECT_Q_8_15,
	STAT_DIAG_DIRECT_Q_16_31,
	STAT_DIAG_DIRECT_Q_GE_32,
	STAT_DIAG_ANCHOR_WAIT_SUM_NS,
	STAT_DIAG_ANCHOR_WAIT_COUNT,
	STAT_DIAG_ANCHOR_WAIT_LE_10US,
	STAT_DIAG_ANCHOR_WAIT_LE_100US,
	STAT_DIAG_ANCHOR_WAIT_LE_1MS,
	STAT_DIAG_ANCHOR_WAIT_LE_10MS,
	STAT_DIAG_ANCHOR_WAIT_GT_10MS,
	STAT_DIAG_ANCHOR_RUN_CPU_MISMATCH,
	STAT_DIAG_FALLBACK_WAIT_SUM_NS,
	STAT_DIAG_FALLBACK_WAIT_COUNT,
	STAT_DIAG_FALLBACK_SELECTED_MATCH,
	STAT_DIAG_FALLBACK_SELECTED_MISMATCH,
	STAT_DIAG_MAP_UPDATE_FAIL,
	STAT_MAX,
};

enum scope_event {
	SCOPE_EVENT_MUTEX_ENTER = 0,
	SCOPE_EVENT_MUTEX_SLOWPATH,
	SCOPE_EVENT_MUTEX_UNLOCK,
	SCOPE_EVENT_FUTEX_WAIT,
	SCOPE_EVENT_FUTEX_WAKE,
};

enum owner_confidence {
	CONF_NONE = 0,
	CONF_WAITER_ONLY = 1,
	CONF_PROBABLE = 2,
	CONF_VERIFIED = 3,
};

enum owner_observed_state {
	OWNER_STATE_UNKNOWN = 0,
	OWNER_STATE_RUNNING = 1,
	OWNER_STATE_RUNNABLE = 2,
	OWNER_STATE_SLEEPING = 3,
};

enum boost_profile {
	BOOST_PROFILE_NONE = 0,
	BOOST_PROFILE_SHORT = 1,
	BOOST_PROFILE_BOUNDED = 2,
	BOOST_PROFILE_ADAPTIVE = 3,
	BOOST_PROFILE_INF = 4,
};

enum boost_kind {
	BOOST_KIND_NONE = 0,
	BOOST_KIND_SHORT = 1,
	BOOST_KIND_BOUNDED = 2,
	BOOST_KIND_ADAPTIVE = 3,
	BOOST_KIND_INF = 4,
};

struct owner_info {
	u32 tid;
	s32 cpu;
	u32 source;
	u64 observed_ns;
};

struct waiter_info {
	struct lock_key key;
	u32 owner_tid;
	s32 anchor_cpu;
	u32 source;
	u32 owner_valid;
	u32 removed;
	u64 observed_ns;
	u64 requeue_bypass_ns;
};

struct contention_info {
	u32 waiter_count;
	u32 boosted;
	u64 last_contention_ns;
	struct lock_key active_key;
	u32 owner_confidence;
	u32 owner_observed_state;
	u32 adaptive_threshold;
	u32 last_waiter_count;
	u32 unblocked_since_boost;
	u64 last_boost_ns;
	u64 boost_start_ns;
	u64 cooldown_until_ns;
	u64 last_runnable_ns;
	u64 owner_delay_ewma_ns;
	u64 unlock_interval_ewma_ns;
	u64 last_unlock_ns;
	u64 task_budget_tokens_ns;
	u64 task_budget_refill_ns;
	u64 last_owner_kick_ns;
	u32 boost_revoke_pending;
	u32 budget_denied;
	u32 longcs_denied;
	u32 last_boost_kind;
};

struct lock_nest_info {
	s32 anchor_cpu;
	s32 last_holder_cpu;
	u32 last_holder_tid;
	u32 source;
	u64 last_update_ns;
	u64 handoff_seq;
	u64 adaptive_expiry_ns;
	u64 cooldown_until_ns;
	u32 direct_hits;
	u32 pressure_rejects;
	u16 maturity_score;
	u16 maturity_threshold;
	u32 maturity_rejects;
	u64 maturity_last_ns;
};

struct boost_lock_state {
	u64 budget_tokens_ns;
	u64 budget_refill_ns;
	u64 fanin_ewma_x8;
	u64 efficiency_ewma_x8;
	u64 last_unlock_ns;
	u64 unlock_interval_ewma_ns;
	u32 boost_count;
	u32 budget_deny_count;
	u32 waiters_unblocked;
	u32 reserved;
};

struct boost_cgroup_state {
	u64 budget_tokens_ns;
	u64 budget_refill_ns;
	u64 boost_count;
	u64 budget_deny_count;
};

struct owner_validation_state {
	u64 mm_id;
	u64 validated_ns;
};

struct recent_owner_validation {
	u32 tid;
	u32 reserved;
	u64 mm_id;
	u64 validated_ns;
};

struct urgent_owner_slot {
	u32 tid;
	u32 reserved;
	u64 observed_ns;
};

struct held_sync_key {
	struct lock_key key;
	u32 tid;
	u32 mode;
};

struct futex_call_state {
	u64 uaddr;
	u64 uaddr2;
	struct lock_key canonical_key;
	struct lock_key canonical_key2;
	u32 raw_op;
	u32 cmd;
	u32 key_valid;
	u32 key2_valid;
	u32 path_handled;
	u32 nr_wake;
	u32 nr_requeue;
	u32 pi_owner_resolved;
};

struct requeue_redirect {
	struct lock_key destination;
	u64 observed_ns;
	u32 remaining;
	u32 source_cmd;
};

struct pi_atomic_call {
	u64 key_ptr;
	u64 pi_state_ptr_ptr;
};

enum diag_fallback_reason {
	DIAG_FALLBACK_CPUMASK = 1,
	DIAG_FALLBACK_MATURITY = 2,
	DIAG_FALLBACK_COOLDOWN = 3,
	DIAG_FALLBACK_PRESSURE = 4,
};

struct diag_cpu_runtime {
	u32 tid;
	u32 tgid;
	u64 cgroup_id;
	u64 running_since_ns;
};

struct diag_anchor_pending {
	u64 enqueue_ns;
	s32 anchor_cpu;
	u32 tgid;
};

struct diag_fallback_pending {
	u64 enqueue_ns;
	s32 anchor_cpu;
	s32 selected_cpu;
	u32 reason;
};

struct diag_anchor_relation_key {
	u64 wake_cgroup_id;
	u64 occupant_cgroup_id;
	u32 anchor_cpu;
	u32 wake_tgid;
	u32 occupant_tid;
	u32 occupant_tgid;
	u32 idle;
	u32 reserved;
};

struct diag_fallback_path_key {
	s32 anchor_cpu;
	s32 selected_cpu;
	s32 actual_cpu;
	u32 reason;
};

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct lock_key);
	__type(value, struct owner_info);
	__uint(max_entries, 10240);
} lock_owners SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, u32);
	__type(value, struct owner_validation_state);
	__uint(max_entries, 1024);
} owner_validation_cache SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct recent_owner_validation);
} recent_owner_cache SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1024);
	__type(key, u32);
	__type(value, struct urgent_owner_slot);
} urgent_owner_slots SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct lock_key);
	__type(value, struct owner_info);
	__uint(max_entries, 10240);
} futex_hints SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct waiter_info);
	__uint(max_entries, 10240);
} waiters SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct contention_info);
	__uint(max_entries, 10240);
} contended_tasks SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct lock_key);
	__type(value, struct lock_nest_info);
	__uint(max_entries, 1024);
} lock_nests SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct lock_key);
	__type(value, struct boost_lock_state);
	__uint(max_entries, 1024);
} boost_lock_states SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, u64);
	__type(value, struct boost_cgroup_state);
	__uint(max_entries, 4096);
} boost_cgroup_states SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct lock_key);
	__uint(max_entries, 10240);
} active_mutex_lock SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, struct lock_key);
	__type(value, struct lock_state);
	__uint(max_entries, 16384);
} sync_states SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct lock_event);
	__uint(max_entries, 16384);
} pending_sync_events SEC(".maps");

/* Keep typed user-space calls separate from futex syscalls nested inside libc. */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct lock_event);
	__uint(max_entries, 16384);
} pending_rwlock_events SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct held_sync_key);
	__type(value, u64);
	__uint(max_entries, 32768);
} held_sync_objects SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct futex_call_state);
	__uint(max_entries, 16384);
} futex_calls SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, struct lock_key);
	__type(value, struct requeue_redirect);
	__uint(max_entries, 4096);
} requeue_redirects SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct pi_atomic_call);
	__uint(max_entries, 16384);
} pi_atomic_calls SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 64);
	__type(key, u32);
	__type(value, u8);
} evidence_tgid_allow SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 64);
	__type(key, u64);
	__type(value, u8);
} evidence_cgroup_allow SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1024);
	__type(key, u32);
	__type(value, struct diag_cpu_runtime);
} diag_cpu_runtime SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 16384);
	__type(key, u32);
	__type(value, struct diag_anchor_pending);
} diag_anchor_waits SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 16384);
	__type(key, u32);
	__type(value, struct diag_fallback_pending);
} diag_fallback_waits SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 4096);
	__type(key, struct diag_anchor_relation_key);
	__type(value, u64);
} diag_anchor_relations SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 4096);
	__type(key, struct diag_fallback_path_key);
	__type(value, u64);
} diag_fallback_paths SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(u32));
	__uint(value_size, sizeof(u64));
	__uint(max_entries, STAT_MAX);
} stats SEC(".maps");

const volatile u32 high_contention_threshold = 4;
const volatile u64 decay_timeout_ns = 200 * 1000 * 1000;
const volatile u64 nest_expiry_ns = 2 * 1000 * 1000 * 1000;
const volatile u32 weak_anchor_queue_limit = ~0U;
const volatile bool adaptive_nest;
const volatile u32 adaptive_anchor_queue_limit = ~0U;
const volatile u32 adaptive_anchor_reject_threshold = 4;
const volatile u32 adaptive_anchor_promote_threshold;
const volatile u64 adaptive_anchor_cooldown_ns = 5 * 1000 * 1000;
const volatile u64 adaptive_min_nest_expiry_ns;
const volatile u64 adaptive_max_nest_expiry_ns = 5ULL * 1000 * 1000 * 1000;
const volatile u32 anchor_maturity_profile;
const volatile u32 anchor_maturity_threshold = 4;
const volatile u32 anchor_maturity_strong_threshold = 2;
const volatile u32 anchor_maturity_weak_threshold = 8;
const volatile u32 anchor_maturity_max_score = 32;
const volatile u64 anchor_maturity_window_ns;
const volatile u32 evidence_scope_mode = LOCK_EVIDENCE_SCOPE_ALL;
const volatile bool evidence_scope_stats;
const volatile s32 pthread_owner_offset = -1;
const volatile bool diagnostic_mode;
const volatile bool stats_enabled;
const volatile bool fifo_sched;
const volatile bool nest_enabled = LOCKCOOP_DEFAULT_NEST_ENABLED;
const volatile bool boost_enabled = LOCKCOOP_DEFAULT_BOOST_ENABLED;
const volatile bool sensor_only_mode;
const volatile u32 owner_boost_slices = 1;
const volatile u32 owner_boost_max_slices = 2;
const volatile u32 boost_profile = LOCKCOOP_DEFAULT_BOOST_PROFILE;
const volatile bool boost_allow_inf;
const volatile u64 boost_grant_ns;
const volatile u64 boost_cap_ns;
const volatile u64 boost_budget_window_ns = 100 * 1000 * 1000;
const volatile u32 boost_task_budget_pct;
const volatile u32 boost_lock_budget_pct;
const volatile u32 boost_cgroup_budget_pct;
const volatile u64 boost_target_delay_ns = 500 * 1000;
const volatile u32 boost_confidence_gate = CONF_VERIFIED;
const volatile u64 boost_max_continuous_ns;

static u64 vtime_now;
UEI_DEFINE(uei);

static __always_inline void stat_inc(u32 idx)
{
	u64 *value;

	if (idx >= STAT_MAX ||
	    (!stats_enabled && idx != STAT_LOCAL && idx != STAT_GLOBAL))
		return;
	value = bpf_map_lookup_elem(&stats, &idx);
	if (value)
		(*value)++;
}

static __always_inline void stat_add(u32 idx, u64 delta)
{
	u64 *value;

	if (idx >= STAT_MAX ||
	    (!stats_enabled && idx != STAT_LOCAL && idx != STAT_GLOBAL))
		return;
	value = bpf_map_lookup_elem(&stats, &idx);
	if (value)
		(*value) += delta;
}

static __always_inline u64 diag_task_cgroup_id(struct task_struct *p)
{
	struct cgroup *cgrp;
	u64 id = 0;

	cgrp = __COMPAT_scx_bpf_task_cgroup(p);
	if (cgrp) {
		id = cgrp->kn->id;
		bpf_cgroup_release(cgrp);
	}
	return id;
}

static __always_inline void diag_count_relation(
					const struct diag_anchor_relation_key *key)
{
	u64 initial = 1;
	u64 *count;

	count = bpf_map_lookup_elem(&diag_anchor_relations, key);
	if (count) {
		__sync_fetch_and_add(count, 1);
		return;
	}
	if (bpf_map_update_elem(&diag_anchor_relations, key, &initial,
				BPF_NOEXIST)) {
		count = bpf_map_lookup_elem(&diag_anchor_relations, key);
		if (count)
			__sync_fetch_and_add(count, 1);
		else
			stat_inc(STAT_DIAG_MAP_UPDATE_FAIL);
	}
}

static __always_inline void diag_count_fallback_path(
					const struct diag_fallback_path_key *key)
{
	u64 initial = 1;
	u64 *count;

	count = bpf_map_lookup_elem(&diag_fallback_paths, key);
	if (count) {
		__sync_fetch_and_add(count, 1);
		return;
	}
	if (bpf_map_update_elem(&diag_fallback_paths, key, &initial,
				BPF_NOEXIST)) {
		count = bpf_map_lookup_elem(&diag_fallback_paths, key);
		if (count)
			__sync_fetch_and_add(count, 1);
		else
			stat_inc(STAT_DIAG_MAP_UPDATE_FAIL);
	}
}

static __always_inline bool diag_anchor_is_idle(s32 anchor_cpu)
{
	const struct cpumask *idle;
	bool is_idle;

	bpf_rcu_read_lock();
	idle = scx_bpf_get_idle_cpumask();
	is_idle = bpf_cpumask_test_cpu(anchor_cpu, idle);
	bpf_rcu_read_unlock();
	scx_bpf_put_idle_cpumask(idle);
	return is_idle;
}

static __always_inline void diag_observe_anchor_candidate(
					struct task_struct *p, s32 anchor_cpu)
{
	struct diag_anchor_relation_key key = {
		.anchor_cpu = anchor_cpu,
		.wake_tgid = p->tgid,
	};
	struct diag_cpu_runtime *occupant;
	u32 cpu_key = (u32)anchor_cpu;
	bool idle;

	if (!diagnostic_mode)
		return;
	idle = diag_anchor_is_idle(anchor_cpu);
	key.idle = idle;
	key.wake_cgroup_id = diag_task_cgroup_id(p);
	if (idle) {
		stat_inc(STAT_DIAG_ANCHOR_IDLE);
		diag_count_relation(&key);
		return;
	}
	stat_inc(STAT_DIAG_ANCHOR_BUSY);
	occupant = bpf_map_lookup_elem(&diag_cpu_runtime, &cpu_key);
	if (!occupant || !occupant->tid) {
		stat_inc(STAT_DIAG_ANCHOR_OCCUPANT_UNKNOWN);
		diag_count_relation(&key);
		return;
	}
	key.occupant_tid = occupant->tid;
	key.occupant_tgid = occupant->tgid;
	key.occupant_cgroup_id = occupant->cgroup_id;
	if (key.wake_tgid == occupant->tgid)
		stat_inc(STAT_DIAG_ANCHOR_SAME_TGID);
	else
		stat_inc(STAT_DIAG_ANCHOR_DIFFERENT_TGID);
	if (key.wake_cgroup_id &&
	    key.wake_cgroup_id == occupant->cgroup_id)
		stat_inc(STAT_DIAG_ANCHOR_SAME_CGROUP);
	diag_count_relation(&key);
}

static __always_inline void diag_direct_queue_depth(s32 anchor_cpu)
{
	s32 queued;

	queued = scx_bpf_dsq_nr_queued(SCX_DSQ_LOCAL_ON | (u64)anchor_cpu);
	if (queued < 0) {
		stat_inc(STAT_DIAG_MAP_UPDATE_FAIL);
		return;
	}
	if (!queued)
		stat_inc(STAT_DIAG_DIRECT_Q_0);
	else if (queued == 1)
		stat_inc(STAT_DIAG_DIRECT_Q_1);
	else if (queued < 4)
		stat_inc(STAT_DIAG_DIRECT_Q_2_3);
	else if (queued < 8)
		stat_inc(STAT_DIAG_DIRECT_Q_4_7);
	else if (queued < 16)
		stat_inc(STAT_DIAG_DIRECT_Q_8_15);
	else if (queued < 32)
		stat_inc(STAT_DIAG_DIRECT_Q_16_31);
	else
		stat_inc(STAT_DIAG_DIRECT_Q_GE_32);
}

static __always_inline void diag_record_anchor_direct(struct task_struct *p,
						       s32 anchor_cpu)
{
	struct diag_anchor_pending pending = {
		.enqueue_ns = bpf_ktime_get_ns(),
		.anchor_cpu = anchor_cpu,
		.tgid = p->tgid,
	};
	u32 tid = p->pid;

	if (!diagnostic_mode)
		return;
	diag_direct_queue_depth(anchor_cpu);
	if (bpf_map_update_elem(&diag_anchor_waits, &tid, &pending, BPF_ANY))
		stat_inc(STAT_DIAG_MAP_UPDATE_FAIL);
}

static __always_inline void diag_record_fallback(struct task_struct *p,
						 s32 anchor_cpu, s32 selected_cpu,
						 u32 reason)
{
	struct diag_fallback_pending pending = {
		.enqueue_ns = bpf_ktime_get_ns(),
		.anchor_cpu = anchor_cpu,
		.selected_cpu = selected_cpu,
		.reason = reason,
	};
	u32 tid = p->pid;

	if (!diagnostic_mode || anchor_cpu < 0)
		return;
	if (bpf_map_update_elem(&diag_fallback_waits, &tid, &pending, BPF_ANY))
		stat_inc(STAT_DIAG_MAP_UPDATE_FAIL);
}

static __always_inline void diag_record_wait_hist(u64 wait_ns)
{
	if (wait_ns <= 10 * 1000)
		stat_inc(STAT_DIAG_ANCHOR_WAIT_LE_10US);
	else if (wait_ns <= 100 * 1000)
		stat_inc(STAT_DIAG_ANCHOR_WAIT_LE_100US);
	else if (wait_ns <= 1000 * 1000)
		stat_inc(STAT_DIAG_ANCHOR_WAIT_LE_1MS);
	else if (wait_ns <= 10 * 1000 * 1000)
		stat_inc(STAT_DIAG_ANCHOR_WAIT_LE_10MS);
	else
		stat_inc(STAT_DIAG_ANCHOR_WAIT_GT_10MS);
}

static __always_inline void diag_task_running(struct task_struct *p)
{
	struct diag_fallback_path_key path = {};
	struct diag_fallback_pending *fallback;
	struct diag_anchor_pending *anchor;
	struct diag_cpu_runtime runtime = {};
	u64 now;
	u64 wait_ns;
	u32 tid = p->pid;
	u32 cpu;

	if (!diagnostic_mode)
		return;
	now = bpf_ktime_get_ns();
	cpu = bpf_get_smp_processor_id();
	runtime.tid = tid;
	runtime.tgid = p->tgid;
	runtime.cgroup_id = diag_task_cgroup_id(p);
	runtime.running_since_ns = now;
	if (bpf_map_update_elem(&diag_cpu_runtime, &cpu, &runtime, BPF_ANY))
		stat_inc(STAT_DIAG_MAP_UPDATE_FAIL);

	anchor = bpf_map_lookup_elem(&diag_anchor_waits, &tid);
	if (anchor) {
		wait_ns = now >= anchor->enqueue_ns ? now - anchor->enqueue_ns : 0;
		stat_add(STAT_DIAG_ANCHOR_WAIT_SUM_NS, wait_ns);
		stat_inc(STAT_DIAG_ANCHOR_WAIT_COUNT);
		diag_record_wait_hist(wait_ns);
		if ((s32)cpu != anchor->anchor_cpu)
			stat_inc(STAT_DIAG_ANCHOR_RUN_CPU_MISMATCH);
		bpf_map_delete_elem(&diag_anchor_waits, &tid);
	}

	fallback = bpf_map_lookup_elem(&diag_fallback_waits, &tid);
	if (!fallback)
		return;
	wait_ns = now >= fallback->enqueue_ns ? now - fallback->enqueue_ns : 0;
	stat_add(STAT_DIAG_FALLBACK_WAIT_SUM_NS, wait_ns);
	stat_inc(STAT_DIAG_FALLBACK_WAIT_COUNT);
	if ((s32)cpu == fallback->selected_cpu)
		stat_inc(STAT_DIAG_FALLBACK_SELECTED_MATCH);
	else
		stat_inc(STAT_DIAG_FALLBACK_SELECTED_MISMATCH);
	path.anchor_cpu = fallback->anchor_cpu;
	path.selected_cpu = fallback->selected_cpu;
	path.actual_cpu = cpu;
	path.reason = fallback->reason;
	diag_count_fallback_path(&path);
	bpf_map_delete_elem(&diag_fallback_waits, &tid);
}

static __always_inline void diag_task_stopping(struct task_struct *p)
{
	struct diag_cpu_runtime *runtime;
	u32 cpu;

	if (!diagnostic_mode)
		return;
	cpu = bpf_get_smp_processor_id();
	runtime = bpf_map_lookup_elem(&diag_cpu_runtime, &cpu);
	if (runtime && runtime->tid == (u32)p->pid) {
		runtime->tid = 0;
		runtime->tgid = 0;
		runtime->cgroup_id = 0;
		runtime->running_since_ns = 0;
	}
}

static __always_inline struct lock_state *sync_state_get(
						const struct lock_key *key,
						u32 source, u32 capabilities)
{
	struct lock_state *state;
	struct lock_state initial = {
		.anchor_cpu = -1,
		.source = source,
		.capabilities = capabilities,
		.updated_ns = bpf_ktime_get_ns(),
	};

	state = bpf_map_lookup_elem(&sync_states, key);
	if (state)
		return state;
	if (bpf_map_update_elem(&sync_states, key, &initial, BPF_NOEXIST) &&
	    !bpf_map_lookup_elem(&sync_states, key)) {
		stat_inc(STAT_MAP_UPDATE_FAIL);
		return NULL;
	}
	return bpf_map_lookup_elem(&sync_states, key);
}

static __always_inline bool sync_event_begin(u32 tid,
					      const struct lock_key *key,
					      u32 type, u32 mode, u32 source,
					      u32 owner_tid, u32 capabilities)
{
	struct lock_event event = {
		.key = *key,
		.observed_ns = bpf_ktime_get_ns(),
		.tid = tid,
		.owner_tid = owner_tid,
		.type = type,
		.mode = mode,
		.source = source,
		.capabilities = capabilities,
	};

	if (bpf_map_update_elem(&pending_sync_events, &tid, &event, BPF_ANY)) {
		stat_inc(STAT_MAP_UPDATE_FAIL);
		return false;
	}
	if (type == LOCK_EVENT_WAIT)
		stat_inc(STAT_SYNC_EVENT_WAIT);
	return true;
}

static __always_inline void sync_event_end(u32 tid)
{
	bpf_map_delete_elem(&pending_sync_events, &tid);
}

static __always_inline bool rwlock_event_begin(u32 tid,
					       const struct lock_key *key,
					       u32 type, u32 mode,
					       u32 owner_tid)
{
	struct lock_event event = {
		.key = *key,
		.observed_ns = bpf_ktime_get_ns(),
		.tid = tid,
		.owner_tid = owner_tid,
		.type = type,
		.mode = mode,
		.source = LOCK_SRC_PTHREAD_RWLOCK,
		.capabilities = LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
				LOCK_CAP_READER_SET,
	};

	if (bpf_map_update_elem(&pending_rwlock_events, &tid, &event, BPF_ANY)) {
		stat_inc(STAT_MAP_UPDATE_FAIL);
		return false;
	}
	if (type == LOCK_EVENT_WAIT)
		stat_inc(STAT_SYNC_EVENT_WAIT);
	return true;
}

static __always_inline void rwlock_event_end(u32 tid)
{
	bpf_map_delete_elem(&pending_rwlock_events, &tid);
}

static __always_inline bool sync_note_acquire(const struct lock_key *key,
					       u32 tid, u32 mode, u32 source,
					       u32 capabilities)
{
	struct lock_state *state;
	struct held_sync_key held_key = {
		.key = *key,
		.tid = tid,
		.mode = mode,
	};
	u64 acquired_ns = bpf_ktime_get_ns();

	state = sync_state_get(key, source, capabilities);
	if (!state)
		return false;
	if (mode == LOCK_MODE_READ) {
		__sync_fetch_and_add(&state->reader_count, 1);
	} else {
		state->owner_tid = tid;
		state->writer_tid = tid;
	}
	state->source = source;
	state->capabilities |= capabilities;
	state->updated_ns = acquired_ns;
	if (bpf_map_update_elem(&held_sync_objects, &held_key, &acquired_ns,
				BPF_ANY))
		stat_inc(STAT_MAP_UPDATE_FAIL);
	stat_inc(STAT_SYNC_EVENT_ACQUIRE);
	return true;
}

static __always_inline u32 sync_note_release(const struct lock_key *key,
					      u32 tid, u32 source)
{
	struct lock_state *state;
	struct held_sync_key held_key = {
		.key = *key,
		.tid = tid,
		.mode = LOCK_MODE_WRITE,
	};
	u32 mode = LOCK_MODE_NONE;
	u32 readers;

	state = bpf_map_lookup_elem(&sync_states, key);
	if (!state)
		return mode;
	if (bpf_map_lookup_elem(&held_sync_objects, &held_key)) {
		mode = LOCK_MODE_WRITE;
		bpf_map_delete_elem(&held_sync_objects, &held_key);
		if (state->writer_tid == tid)
			state->writer_tid = 0;
		if (state->owner_tid == tid)
			state->owner_tid = 0;
	} else {
		held_key.mode = LOCK_MODE_EXCLUSIVE;
		if (bpf_map_lookup_elem(&held_sync_objects, &held_key)) {
			mode = LOCK_MODE_EXCLUSIVE;
			bpf_map_delete_elem(&held_sync_objects, &held_key);
			if (state->writer_tid == tid)
				state->writer_tid = 0;
			if (state->owner_tid == tid)
				state->owner_tid = 0;
		}
	}
	if (mode == LOCK_MODE_NONE) {
		held_key.mode = LOCK_MODE_READ;
		if (bpf_map_lookup_elem(&held_sync_objects, &held_key)) {
			mode = LOCK_MODE_READ;
			bpf_map_delete_elem(&held_sync_objects, &held_key);
			readers = state->reader_count;
			if (readers)
				__sync_fetch_and_sub(&state->reader_count, 1);
		}
	}
	if (mode == LOCK_MODE_NONE) {
		held_key.mode = LOCK_MODE_SPIN;
		if (bpf_map_lookup_elem(&held_sync_objects, &held_key)) {
			mode = LOCK_MODE_SPIN;
			bpf_map_delete_elem(&held_sync_objects, &held_key);
			if (state->owner_tid == tid)
				state->owner_tid = 0;
			if (state->writer_tid == tid)
				state->writer_tid = 0;
		}
	}
	if (mode == LOCK_MODE_NONE) {
		held_key.mode = LOCK_MODE_CAS;
		if (bpf_map_lookup_elem(&held_sync_objects, &held_key)) {
			mode = LOCK_MODE_CAS;
			bpf_map_delete_elem(&held_sync_objects, &held_key);
			if (state->owner_tid == tid)
				state->owner_tid = 0;
			if (state->writer_tid == tid)
				state->writer_tid = 0;
		}
	}
	state->source = source;
	state->updated_ns = bpf_ktime_get_ns();
	if (mode != LOCK_MODE_NONE)
		stat_inc(STAT_SYNC_EVENT_RELEASE);
	return mode;
}

static __always_inline u32 current_tid(void)
{
	return (u32)bpf_get_current_pid_tgid();
}

static __always_inline void scope_event_stat(bool admitted, u32 event)
{
	static const u32 admit_idx[] = {
		STAT_SCOPE_ADMIT_MUTEX_ENTER,
		STAT_SCOPE_ADMIT_MUTEX_SLOWPATH,
		STAT_SCOPE_ADMIT_MUTEX_UNLOCK,
		STAT_SCOPE_ADMIT_FUTEX_WAIT,
		STAT_SCOPE_ADMIT_FUTEX_WAKE,
	};
	static const u32 deny_idx[] = {
		STAT_SCOPE_DENY_MUTEX_ENTER,
		STAT_SCOPE_DENY_MUTEX_SLOWPATH,
		STAT_SCOPE_DENY_MUTEX_UNLOCK,
		STAT_SCOPE_DENY_FUTEX_WAIT,
		STAT_SCOPE_DENY_FUTEX_WAKE,
	};

	if (event >= 5)
		return;
	stat_inc(admitted ? admit_idx[event] : deny_idx[event]);
}

static __always_inline bool evidence_allowed(u32 event)
{
	u32 tgid;
	u64 cgid;
	u8 *allowed;

	/* The default path has no allow-map lookup or scope counter write. */
	if (evidence_scope_mode == LOCK_EVIDENCE_SCOPE_ALL)
		return true;
	stat_inc(STAT_EVIDENCE_SEEN);
	if (evidence_scope_mode == LOCK_EVIDENCE_SCOPE_TGID) {
		tgid = (u32)(bpf_get_current_pid_tgid() >> 32);
		allowed = bpf_map_lookup_elem(&evidence_tgid_allow, &tgid);
	} else {
		cgid = bpf_get_current_cgroup_id();
		allowed = bpf_map_lookup_elem(&evidence_cgroup_allow, &cgid);
	}
	if (allowed) {
		stat_inc(STAT_EVIDENCE_ADMITTED);
		scope_event_stat(true, event);
		return true;
	}
	stat_inc(STAT_EVIDENCE_SCOPE_DENIED);
	scope_event_stat(false, event);
	return false;
}

static __always_inline bool evidence_current_allowed(void)
{
	u32 tgid;
	u64 cgid;

	if (evidence_scope_mode == LOCK_EVIDENCE_SCOPE_ALL)
		return true;
	if (evidence_scope_mode == LOCK_EVIDENCE_SCOPE_TGID) {
		tgid = (u32)(bpf_get_current_pid_tgid() >> 32);
		return bpf_map_lookup_elem(&evidence_tgid_allow, &tgid) != NULL;
	}
	cgid = bpf_get_current_cgroup_id();
	return bpf_map_lookup_elem(&evidence_cgroup_allow, &cgid) != NULL;
}

static __always_inline bool maturity_enabled(void)
{
	return anchor_maturity_profile != LOCK_ANCHOR_MATURITY_OFF;
}

static __always_inline bool maturity_source_strong(u32 source)
{
	return source == LOCK_SRC_MUTEX_OWNER ||
	       source == LOCK_SRC_MUTEX_RELEASER ||
	       source == LOCK_SRC_PTHREAD_OWNER ||
	       source == LOCK_SRC_RWSEM_WRITER ||
	       source == LOCK_SRC_PI_OWNER ||
	       source == LOCK_SRC_PTHREAD_RWLOCK ||
	       source == LOCK_SRC_USER_ANNOTATION;
}

static __always_inline u32 maturity_max_score(void)
{
	u32 max = anchor_maturity_max_score;

	if (!max)
		max = 32;
	if (max > 65535)
		max = 65535;
	return max;
}

static __always_inline u32 maturity_threshold_for(u32 source)
{
	u32 threshold;

	if (anchor_maturity_profile == LOCK_ANCHOR_MATURITY_SOURCE) {
		threshold = maturity_source_strong(source) ?
			anchor_maturity_strong_threshold : anchor_maturity_weak_threshold;
		if (!threshold)
			threshold = maturity_source_strong(source) ? 2 : 8;
	} else {
		threshold = anchor_maturity_threshold;
		if (anchor_maturity_profile == LOCK_ANCHOR_MATURITY_ADAPTIVE &&
		    !threshold)
			threshold = 4;
	}
	if (threshold > maturity_max_score())
		threshold = maturity_max_score();
	return threshold;
}

static __always_inline u32 maturity_weight(u32 source)
{
	if (anchor_maturity_profile == LOCK_ANCHOR_MATURITY_SOURCE &&
	    maturity_source_strong(source))
		return 2;
	if (anchor_maturity_profile == LOCK_ANCHOR_MATURITY_ADAPTIVE &&
	    maturity_source_strong(source))
		return 2;
	return 1;
}

static __always_inline void maturity_reset(struct lock_nest_info *nest,
						   s32 cpu, u32 source, u64 now,
						   u32 reason)
{
	u32 score;

	if (!nest || !maturity_enabled())
		return;
	score = maturity_weight(source);
	if (score > maturity_max_score())
		score = maturity_max_score();
	nest->maturity_score = score;
	nest->maturity_threshold = maturity_threshold_for(source);
	nest->maturity_rejects = 0;
	nest->maturity_last_ns = now;
	stat_inc(STAT_ANCHOR_MATURITY_PHASE_RESET);
	if (reason < STAT_MAX)
		stat_inc(reason);
	(void)cpu;
}

static __always_inline void maturity_init(struct lock_nest_info *nest,
						  s32 cpu, u32 source, u64 now)
{
	if (!nest)
		return;
	nest->maturity_score = maturity_enabled() ? maturity_weight(source) : 0;
	nest->maturity_threshold = maturity_enabled() ?
		maturity_threshold_for(source) : 0;
	nest->maturity_rejects = 0;
	nest->maturity_last_ns = now;
	if (maturity_enabled())
		stat_inc(STAT_ANCHOR_MATURITY_UPDATE);
	(void)cpu;
}

static __always_inline void maturity_compatible_update(
						struct lock_nest_info *nest, s32 cpu,
						u32 source, u64 now)
{
	u32 score;
	u32 threshold;
	u64 window = anchor_maturity_window_ns;

	if (!nest || !maturity_enabled() || cpu != nest->anchor_cpu ||
	    (maturity_source_strong(nest->source) &&
	     !maturity_source_strong(source)))
		return;
	if (window && now - nest->maturity_last_ns > window) {
		maturity_reset(nest, cpu, source, now,
				       STAT_ANCHOR_MATURITY_RESET_STALE);
		return;
	}
	score = nest->maturity_score + maturity_weight(source);
	if (score < nest->maturity_score || score > maturity_max_score())
		score = maturity_max_score();
	nest->maturity_score = score;
	nest->maturity_last_ns = now;
	stat_inc(STAT_ANCHOR_MATURITY_UPDATE);
	stat_inc(STAT_ANCHOR_MATURITY_COMPATIBLE_UPDATE);
	threshold = nest->maturity_threshold;
	if (anchor_maturity_profile == LOCK_ANCHOR_MATURITY_ADAPTIVE &&
	    nest->maturity_rejects >= 2 && threshold > 2) {
		nest->maturity_threshold = threshold - 1;
		nest->maturity_rejects = 0;
	}
}

static __always_inline bool maturity_admitted(struct lock_nest_info *nest,
						      u64 now)
{
	u32 threshold;

	if (!maturity_enabled())
		return true;
	if (!nest)
		return false;
	threshold = nest->maturity_threshold;
	if (anchor_maturity_window_ns &&
	    now - nest->maturity_last_ns > anchor_maturity_window_ns)
		threshold = maturity_max_score();
	if (nest->maturity_score < threshold) {
		nest->maturity_rejects++;
		stat_inc(STAT_ANCHOR_MATURITY_REJECT);
		return false;
	}
	stat_inc(STAT_ANCHOR_MATURITY_PASS);
	if (maturity_source_strong(nest->source))
		stat_inc(STAT_ANCHOR_MATURITY_STRONG_PASS);
	else
		stat_inc(STAT_ANCHOR_MATURITY_WEAK_PASS);
	return true;
}

static __always_inline bool is_percpu_kthread(struct task_struct *p)
{
	return p->nr_cpus_allowed == 1 && (p->flags & PF_KTHREAD);
}

static __always_inline bool current_cpu_scope_constrained(void)
{
	struct task_struct *current;
	u32 allowed;
	u32 possible;

	current = bpf_get_current_task_btf();
	allowed = BPF_CORE_READ(current, nr_cpus_allowed);
	possible = scx_bpf_nr_cpu_ids();
	return possible && allowed * 2 <= possible;
}

static __always_inline u64 effective_probable_owner_window(void)
{
	u64 window = boost_target_delay_ns;

	if (!window)
		window = 100 * 1000;
	if (current_cpu_scope_constrained() && window < 10 * 1000 * 1000)
		window = 10 * 1000 * 1000;
	return window;
}

static __always_inline u32 confidence_for_waiter(bool owner_valid, u32 source)
{
	if (!owner_valid)
		return CONF_WAITER_ONLY;
	if (source == LOCK_SRC_MUTEX_OWNER ||
	    source == LOCK_SRC_MUTEX_RELEASER ||
	    source == LOCK_SRC_PTHREAD_OWNER ||
	    source == LOCK_SRC_RWSEM_WRITER ||
	    source == LOCK_SRC_PI_OWNER ||
	    source == LOCK_SRC_PTHREAD_RWLOCK ||
	    source == LOCK_SRC_USER_ANNOTATION)
		return CONF_VERIFIED;
	if (source == LOCK_SRC_FUTEX_WAKER)
		return CONF_PROBABLE;
	return CONF_PROBABLE;
}

static __always_inline u32 path_action_capabilities(
						const struct lock_key *key,
						u32 source, bool owner_valid)
{
	u32 caps = nest_enabled && key->kind != LOCK_KEY_PTHREAD_RWLOCK ?
		LOCK_ACTION_NEST : LOCK_ACTION_NONE;

	if (!boost_enabled || !owner_valid)
		return caps;
	if (key->kind == LOCK_KEY_KERNEL_MUTEX ||
	    key->kind == LOCK_KEY_PI_FUTEX ||
	    key->kind == LOCK_KEY_PTHREAD_RWLOCK ||
	    key->kind == LOCK_KEY_USER_SPIN ||
	    key->kind == LOCK_KEY_USER_CAS)
		return caps | LOCK_ACTION_BOOST_SHORT |
		       LOCK_ACTION_BOOST_STRONG;
	if (key->kind == LOCK_KEY_KERNEL_RWSEM &&
	    source == LOCK_SRC_RWSEM_WRITER)
		return caps | LOCK_ACTION_BOOST_SHORT |
		       LOCK_ACTION_BOOST_STRONG;
	if (key->kind == LOCK_KEY_PRIVATE_FUTEX) {
		if (source == LOCK_SRC_PTHREAD_OWNER)
			return caps | LOCK_ACTION_BOOST_SHORT |
			       LOCK_ACTION_BOOST_STRONG;
		if (source == LOCK_SRC_FUTEX_WAKER)
			return caps | LOCK_ACTION_BOOST_SHORT;
	}
	return caps;
}

static __always_inline bool boost_path_allowed(
						struct contention_info *info,
						u32 kind)
{
	struct lock_state *state;
	u32 caps = 0;

	if (!boost_enabled || !info->active_key.kind)
		return false;
	state = bpf_map_lookup_elem(&sync_states, &info->active_key);
	if (state)
		caps = state->action_capabilities;
	if (!caps) {
		if (info->active_key.kind == LOCK_KEY_KERNEL_MUTEX ||
		    info->active_key.kind == LOCK_KEY_PI_FUTEX ||
		    info->active_key.kind == LOCK_KEY_KERNEL_RWSEM ||
		    info->active_key.kind == LOCK_KEY_PTHREAD_RWLOCK ||
		    info->active_key.kind == LOCK_KEY_USER_SPIN ||
		    info->active_key.kind == LOCK_KEY_USER_CAS)
			caps = LOCK_ACTION_NEST | LOCK_ACTION_BOOST_SHORT |
			       LOCK_ACTION_BOOST_STRONG;
		else if (info->active_key.kind == LOCK_KEY_PRIVATE_FUTEX)
			caps = LOCK_ACTION_NEST | LOCK_ACTION_BOOST_SHORT |
			       (info->owner_confidence == CONF_VERIFIED ?
				LOCK_ACTION_BOOST_STRONG : 0);
		else
			caps = LOCK_ACTION_NEST;
	}
	if (kind == BOOST_KIND_SHORT)
		return caps & LOCK_ACTION_BOOST_SHORT;
	return caps & LOCK_ACTION_BOOST_STRONG;
}

static __always_inline void account_confidence(u32 confidence)
{
	if (confidence == CONF_VERIFIED)
		stat_inc(STAT_CONF_VERIFIED);
	else if (confidence == CONF_PROBABLE)
		stat_inc(STAT_CONF_PROBABLE);
	else if (confidence == CONF_WAITER_ONLY)
		stat_inc(STAT_CONF_WAITER_ONLY);
	else
		stat_inc(STAT_CONF_NONE);
}

static __always_inline u64 ewma8(u64 old, u64 sample)
{
	if (!old)
		return sample;
	if (sample > old)
		return old + ((sample - old) >> 3);
	return old - ((old - sample) >> 3);
}

static __always_inline u32 clamp_boost_threshold(u32 threshold)
{
	if (threshold < 2)
		return 2;
	if (threshold > 16)
		return 16;
	return threshold;
}

static __always_inline u64 short_boost_grant_ns(void)
{
	u64 grant = boost_grant_ns;

	if (grant)
		return grant;
	grant = SCX_SLICE_DFL >> 3;
	if (!grant || grant > 500 * 1000)
		grant = 500 * 1000;
	return grant;
}

static __always_inline u64 short_boost_cap_ns(void)
{
	u64 extra = boost_cap_ns;
	u64 grant = short_boost_grant_ns();

	if (!extra)
		extra = 1000 * 1000;
	if (extra > 10 * 1000 * 1000)
		extra = 10 * 1000 * 1000;
	if (extra < grant)
		extra = grant;
	return SCX_SLICE_DFL + extra;
}

static __always_inline u64 bounded_boost_grant_ns(void)
{
	u64 grant = owner_boost_slices;

	if (boost_grant_ns)
		return boost_grant_ns;
	if (!grant)
		return 0;
	return grant * SCX_SLICE_DFL;
}

static __always_inline u64 bounded_boost_cap_ns(u64 grant)
{
	u64 max = owner_boost_max_slices;

	if (boost_cap_ns)
		max = SCX_SLICE_DFL + boost_cap_ns;
	else
		max *= SCX_SLICE_DFL;
	if (!max || max < grant)
		max = grant;
	return max;
}

static __always_inline void apply_owner_boost_budget(struct task_struct *p,
						     u64 grant, u64 max)
{
	u64 next;

	if (!grant)
		return;
	next = p->scx.slice + grant;
	if (next < p->scx.slice || next > max)
		next = max;
	if (p->scx.slice < next)
		p->scx.slice = next;
}

static __always_inline u64 budget_window(void)
{
	if (boost_budget_window_ns)
		return boost_budget_window_ns;
	return 100 * 1000 * 1000;
}

static __always_inline u64 budget_cap_from_pct(u32 pct)
{
	u64 window = budget_window();

	if (!pct)
		return 0;
	if (pct > 100)
		pct = 100;
	return window * pct / 100;
}

static __always_inline void refill_budget(u64 *tokens, u64 *last_refill,
					  u64 now, u64 cap)
{
	u64 window = budget_window();
	u64 elapsed;
	u64 add;

	if (!cap)
		return;
	if (!*last_refill) {
		*last_refill = now;
		*tokens = cap;
		return;
	}
	elapsed = now - *last_refill;
	if (!elapsed)
		return;
	if (elapsed >= window) {
		*tokens = cap;
		*last_refill = now;
		return;
	}
	add = cap * elapsed / window;
	if (!add)
		return;
	if (*tokens + add < *tokens || *tokens + add > cap)
		*tokens = cap;
	else
		*tokens += add;
	*last_refill = now;
}

static __always_inline void update_lock_boost_state_on_waiter(struct lock_key *key,
							      u32 waiters,
							      u64 now)
{
	struct boost_lock_state *state;
	struct boost_lock_state new_state = {
		.budget_tokens_ns = budget_cap_from_pct(boost_lock_budget_pct),
		.budget_refill_ns = now,
		.fanin_ewma_x8 = (u64)waiters << 3,
	};

	if (!boost_lock_budget_pct && !stats_enabled && !diagnostic_mode)
		return;
	state = bpf_map_lookup_elem(&boost_lock_states, key);
	if (!state) {
		if (bpf_map_update_elem(&boost_lock_states, key, &new_state,
					BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		return;
	}
	state->fanin_ewma_x8 = ewma8(state->fanin_ewma_x8, (u64)waiters << 3);
}

static __always_inline void mark_lock_unlock_interval(struct lock_key *key,
						       u64 now)
{
	struct boost_lock_state *state;
	struct boost_lock_state new_state = {
		.budget_tokens_ns = budget_cap_from_pct(boost_lock_budget_pct),
		.budget_refill_ns = now,
		.last_unlock_ns = now,
	};
	u64 interval;

	if (!boost_lock_budget_pct && !stats_enabled && !diagnostic_mode)
		return;
	state = bpf_map_lookup_elem(&boost_lock_states, key);
	if (!state) {
		if (bpf_map_update_elem(&boost_lock_states, key, &new_state,
					BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		return;
	}
	if (state->last_unlock_ns && now > state->last_unlock_ns) {
		interval = now - state->last_unlock_ns;
		state->unlock_interval_ewma_ns =
			ewma8(state->unlock_interval_ewma_ns, interval);
	}
	state->last_unlock_ns = now;
}

static __always_inline void mark_owner_unlock(u32 tid, struct lock_key *key,
					      u64 now)
{
	struct contention_info *info;
	u64 interval;

	if (!boost_enabled)
		return;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (!info)
		return;
	if (info->active_key.kind && !lock_key_equal(&info->active_key, key))
		return;
	if (boost_profile == BOOST_PROFILE_ADAPTIVE ||
	    stats_enabled || diagnostic_mode) {
		if (info->last_unlock_ns && now > info->last_unlock_ns) {
			interval = now - info->last_unlock_ns;
			info->unlock_interval_ewma_ns =
				ewma8(info->unlock_interval_ewma_ns, interval);
		}
		info->last_unlock_ns = now;
	}
	/* The finite grant drains naturally after the dependency ends. */
	info->waiter_count = 0;
	info->owner_confidence = CONF_NONE;
	info->owner_observed_state = OWNER_STATE_UNKNOWN;
	info->adaptive_threshold = high_contention_threshold;
	info->last_owner_kick_ns = 0;
	info->active_key = (struct lock_key){};
	if (info->boosted) {
		info->boost_revoke_pending = true;
		info->boosted = false;
		info->boost_start_ns = 0;
		stat_inc(STAT_BOOST_REVOKE_UNLOCK);
	}
}

static __always_inline u32 adaptive_boost_threshold(struct contention_info *info)
{
	u32 threshold = info->adaptive_threshold;
	u32 old_threshold;
	u64 target = boost_target_delay_ns;

	if (boost_profile != BOOST_PROFILE_ADAPTIVE)
		return high_contention_threshold;
	if (!threshold)
		threshold = high_contention_threshold;
	threshold = clamp_boost_threshold(threshold);
	old_threshold = threshold;
	if (!target)
		target = 500 * 1000;
	if (info->budget_denied || info->longcs_denied) {
		threshold++;
	} else if (info->owner_delay_ewma_ns > target &&
		   info->waiter_count >= threshold &&
		   (info->unblocked_since_boost ||
		    info->waiter_count >= threshold * 2)) {
		threshold--;
	} else if (info->waiter_count < threshold &&
		   info->owner_delay_ewma_ns < (target >> 1)) {
		threshold++;
	}
	threshold = clamp_boost_threshold(threshold);
	if (threshold > old_threshold)
		stat_inc(STAT_THRESHOLD_UP);
	else if (threshold < old_threshold)
		stat_inc(STAT_THRESHOLD_DOWN);
	info->adaptive_threshold = threshold;
	info->budget_denied = false;
	info->longcs_denied = false;
	return threshold;
}

static __always_inline bool boost_longcs_blocked(struct contention_info *info,
						 u64 now)
{
	u64 cooldown;

	if (info->cooldown_until_ns && now < info->cooldown_until_ns) {
		info->longcs_denied = true;
		stat_inc(STAT_BOOST_LONGCS_DENIED);
		return true;
	}
	if (info->cooldown_until_ns && now >= info->cooldown_until_ns)
		info->cooldown_until_ns = 0;
	if (!boost_max_continuous_ns || !info->boost_start_ns)
		return false;
	if (now - info->boost_start_ns <= boost_max_continuous_ns)
		return false;
	cooldown = budget_window() >> 1;
	if (!cooldown || cooldown > decay_timeout_ns)
		cooldown = decay_timeout_ns >> 2;
	if (!cooldown)
		cooldown = 1000 * 1000;
	info->cooldown_until_ns = now + cooldown;
	info->boosted = false;
	info->boost_start_ns = 0;
	info->longcs_denied = true;
	stat_inc(STAT_BOOST_LONGCS_DENIED);
	return true;
}

static __always_inline bool boost_budget_allowed(struct contention_info *info,
						 u64 now, u64 grant)
{
	struct boost_lock_state *lock_state = NULL;
	struct boost_cgroup_state *cg_state = NULL;
	struct boost_lock_state new_lock_state;
	struct boost_cgroup_state new_cg_state;
	u64 task_cap = budget_cap_from_pct(boost_task_budget_pct);
	u64 lock_cap = budget_cap_from_pct(boost_lock_budget_pct);
	u64 cg_cap = budget_cap_from_pct(boost_cgroup_budget_pct);
	u64 cgid = 0;

	if (!task_cap && !lock_cap && !cg_cap)
		return true;
	if (task_cap) {
		refill_budget(&info->task_budget_tokens_ns,
			      &info->task_budget_refill_ns, now, task_cap);
		if (info->task_budget_tokens_ns < grant) {
			info->budget_denied = true;
			stat_inc(STAT_BOOST_BUDGET_DENIED);
			stat_inc(STAT_BOOST_TASK_BUDGET_DENIED);
			return false;
		}
	}
	if (lock_cap && info->active_key.kind) {
		lock_state = bpf_map_lookup_elem(&boost_lock_states,
						 &info->active_key);
		if (!lock_state) {
			__builtin_memset(&new_lock_state, 0,
					 sizeof(new_lock_state));
			new_lock_state.budget_tokens_ns = lock_cap;
			new_lock_state.budget_refill_ns = now;
			if (bpf_map_update_elem(&boost_lock_states,
						&info->active_key,
						&new_lock_state, BPF_ANY)) {
				stat_inc(STAT_MAP_UPDATE_FAIL);
				info->budget_denied = true;
				stat_inc(STAT_BOOST_BUDGET_DENIED);
				stat_inc(STAT_BOOST_LOCK_BUDGET_DENIED);
				return false;
			}
			lock_state = bpf_map_lookup_elem(&boost_lock_states,
							 &info->active_key);
		}
		if (!lock_state) {
			info->budget_denied = true;
			stat_inc(STAT_BOOST_BUDGET_DENIED);
			stat_inc(STAT_BOOST_LOCK_BUDGET_DENIED);
			return false;
		}
		refill_budget(&lock_state->budget_tokens_ns,
			      &lock_state->budget_refill_ns, now, lock_cap);
		if (lock_state->budget_tokens_ns < grant) {
			lock_state->budget_deny_count++;
			info->budget_denied = true;
			stat_inc(STAT_BOOST_BUDGET_DENIED);
			stat_inc(STAT_BOOST_LOCK_BUDGET_DENIED);
			return false;
		}
	}
	if (cg_cap) {
		cgid = bpf_get_current_cgroup_id();
		if (cgid) {
			cg_state = bpf_map_lookup_elem(&boost_cgroup_states,
						       &cgid);
			if (!cg_state) {
				__builtin_memset(&new_cg_state, 0,
						 sizeof(new_cg_state));
				new_cg_state.budget_tokens_ns = cg_cap;
				new_cg_state.budget_refill_ns = now;
				if (bpf_map_update_elem(&boost_cgroup_states,
							&cgid, &new_cg_state,
							BPF_ANY)) {
					stat_inc(STAT_MAP_UPDATE_FAIL);
					info->budget_denied = true;
					stat_inc(STAT_BOOST_BUDGET_DENIED);
					stat_inc(STAT_BOOST_CGROUP_BUDGET_DENIED);
					return false;
				}
				cg_state = bpf_map_lookup_elem(&boost_cgroup_states,
							       &cgid);
			}
			if (!cg_state) {
				info->budget_denied = true;
				stat_inc(STAT_BOOST_BUDGET_DENIED);
				stat_inc(STAT_BOOST_CGROUP_BUDGET_DENIED);
				return false;
			}
			refill_budget(&cg_state->budget_tokens_ns,
				      &cg_state->budget_refill_ns, now, cg_cap);
			if (cg_state->budget_tokens_ns < grant) {
				cg_state->budget_deny_count++;
				info->budget_denied = true;
				stat_inc(STAT_BOOST_BUDGET_DENIED);
				stat_inc(STAT_BOOST_CGROUP_BUDGET_DENIED);
				return false;
			}
		}
	}
	if (task_cap)
		info->task_budget_tokens_ns -= grant;
	if (lock_state)
		lock_state->budget_tokens_ns -= grant;
	if (cg_state)
		cg_state->budget_tokens_ns -= grant;
	return true;
}

static __always_inline bool lock_progress_is_fast(struct contention_info *info)
{
	struct boost_lock_state *state;
	u64 target = boost_target_delay_ns;
	u64 cutoff;
	u64 interval = info->unlock_interval_ewma_ns;

	if (boost_profile != BOOST_PROFILE_ADAPTIVE || !info->active_key.kind)
		return false;
	if (!interval && (boost_lock_budget_pct || stats_enabled || diagnostic_mode)) {
		state = bpf_map_lookup_elem(&boost_lock_states, &info->active_key);
		if (state)
			interval = state->unlock_interval_ewma_ns;
	}
	if (!interval)
		return false;
	if (!target)
		target = 500 * 1000;
	cutoff = target >> 3;
	if (!cutoff)
		cutoff = 1;
	return interval < cutoff;
}

static __always_inline u32 choose_boost_kind(struct contention_info *info,
					     u32 threshold)
{
	u64 target = boost_target_delay_ns;

	if (!boost_enabled || boost_profile == BOOST_PROFILE_NONE)
		return BOOST_KIND_NONE;
	if (lock_progress_is_fast(info))
		return BOOST_KIND_NONE;
	if (!target)
		target = 500 * 1000;
	if (info->owner_confidence < boost_confidence_gate) {
		stat_inc(STAT_BOOST_CONF_DENIED);
		return BOOST_KIND_NONE;
	}
	if (info->waiter_count < threshold) {
		if (boost_profile == BOOST_PROFILE_BOUNDED && !info->boosted)
			return BOOST_KIND_BOUNDED;
		if (boost_profile == BOOST_PROFILE_SHORT && !info->boosted)
			return BOOST_KIND_SHORT;
		if (boost_profile == BOOST_PROFILE_ADAPTIVE &&
		    info->owner_delay_ewma_ns > target && !info->boosted)
			return BOOST_KIND_SHORT;
		return BOOST_KIND_NONE;
	}
	if (info->owner_confidence == CONF_PROBABLE)
		return BOOST_KIND_SHORT;
	if (boost_profile == BOOST_PROFILE_SHORT)
		return BOOST_KIND_SHORT;
	if (boost_profile == BOOST_PROFILE_INF && boost_allow_inf)
		return BOOST_KIND_INF;
	if (boost_profile == BOOST_PROFILE_ADAPTIVE &&
	    info->waiter_count >= threshold * 2 &&
	    info->owner_delay_ewma_ns > target)
		return BOOST_KIND_ADAPTIVE;
	return BOOST_KIND_BOUNDED;
}

static __always_inline u64 clamp_anchor_expiry(u64 expiry)
{
	u64 min = adaptive_min_nest_expiry_ns;
	u64 max = adaptive_max_nest_expiry_ns;

	if (!adaptive_nest)
		return nest_expiry_ns;
	if (!min)
		min = nest_expiry_ns;
	if (!max)
		max = nest_expiry_ns;
	if (max < min)
		max = min;
	if (expiry < min)
		expiry = min;
	if (expiry > max)
		expiry = max;
	return expiry;
}

static __always_inline u64 initial_anchor_expiry(bool strong)
{
	u64 expiry = nest_expiry_ns;

	if (adaptive_nest && !strong && adaptive_min_nest_expiry_ns < expiry)
		expiry = adaptive_min_nest_expiry_ns;
	return clamp_anchor_expiry(expiry);
}

static __always_inline u64 current_anchor_expiry(struct lock_nest_info *nest)
{
	if (adaptive_nest && nest && nest->adaptive_expiry_ns)
		return clamp_anchor_expiry(nest->adaptive_expiry_ns);
	return nest_expiry_ns;
}

static __always_inline bool anchor_fresh(struct lock_nest_info *nest, u64 now)
{
	return nest_enabled && nest && nest->anchor_cpu >= 0 &&
	       now - nest->last_update_ns <= current_anchor_expiry(nest);
}

static __always_inline u32 effective_anchor_queue_limit(void)
{
	if (weak_anchor_queue_limit != ~0U)
		return weak_anchor_queue_limit;
	if (adaptive_nest)
		return adaptive_anchor_queue_limit;
	return ~0U;
}

static __always_inline bool anchor_queue_admitted(s32 cpu)
{
	s32 queued;

	if (weak_anchor_queue_limit == ~0U)
		return true;
	if (!weak_anchor_queue_limit)
		return false;
	queued = scx_bpf_dsq_nr_queued(SCX_DSQ_LOCAL_ON | (u64)cpu);
	return queued >= 0 && (u32)queued < weak_anchor_queue_limit;
}

static __always_inline bool adaptive_anchor_queue_admitted(s32 cpu)
{
	u32 limit = effective_anchor_queue_limit();
	s32 queued;

	if (limit == ~0U)
		return true;
	if (!limit)
		return false;
	queued = scx_bpf_dsq_nr_queued(SCX_DSQ_LOCAL_ON | (u64)cpu);
	return queued >= 0 && (u32)queued < limit;
}

static __always_inline void mark_anchor_direct(struct lock_nest_info *nest)
{
	u64 expiry, next;

	if (!adaptive_anchor_promote_threshold ||
	    !adaptive_nest || !nest)
		return;
	nest->direct_hits++;
	nest->pressure_rejects = 0;
	if (nest->direct_hits < adaptive_anchor_promote_threshold)
		return;
	nest->direct_hits = 0;
	expiry = current_anchor_expiry(nest);
	next = expiry + (expiry >> 1);
	if (next <= expiry)
		next = adaptive_max_nest_expiry_ns;
	next = clamp_anchor_expiry(next);
	if (next > expiry) {
		nest->adaptive_expiry_ns = next;
		stat_inc(STAT_ANCHOR_EXPIRY_EXTEND);
	}
}

static __always_inline void mark_anchor_pressure(struct lock_nest_info *nest,
						 u64 now)
{
	u64 expiry, next;

	if (!adaptive_nest || !nest)
		return;
	nest->pressure_rejects++;
	nest->direct_hits = 0;
	if (!adaptive_anchor_reject_threshold ||
	    nest->pressure_rejects < adaptive_anchor_reject_threshold)
		return;
	nest->pressure_rejects = 0;
	nest->cooldown_until_ns = now + adaptive_anchor_cooldown_ns;
	stat_inc(STAT_ANCHOR_COOLDOWN_REJECT);
	expiry = current_anchor_expiry(nest);
	next = expiry >> 1;
	next = clamp_anchor_expiry(next);
	if (next < expiry) {
		nest->adaptive_expiry_ns = next;
		stat_inc(STAT_ANCHOR_EXPIRY_SHORTEN);
	}
}

static __always_inline bool anchor_in_cooldown(struct lock_nest_info *nest,
					       u64 now)
{
	return adaptive_nest && nest && nest->cooldown_until_ns &&
	       now < nest->cooldown_until_ns;
}

static __always_inline void refresh_anchor(struct lock_key *key, u32 tid,
					   s32 cpu, u32 source, bool strong,
					   bool admit)
{
	struct lock_nest_info *nest;
	struct lock_nest_info new_nest = {
		.anchor_cpu = cpu,
		.last_holder_cpu = cpu,
		.last_holder_tid = tid,
		.source = source,
		.last_update_ns = bpf_ktime_get_ns(),
		.handoff_seq = 1,
		.adaptive_expiry_ns = initial_anchor_expiry(strong),
	};
	u64 now = new_nest.last_update_ns;
	u64 expiry;
	bool upgrade;
	bool expired;
	bool maturity_cpu_shift;

	if (!nest_enabled || cpu < 0)
		return;
	nest = bpf_map_lookup_elem(&lock_nests, key);
	if (!nest) {
		if (!admit)
			return;
		maturity_init(&new_nest, cpu, source, now);
		if (bpf_map_update_elem(&lock_nests, key, &new_nest, BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		else
			stat_inc(STAT_ANCHOR_CREATE);
		return;
	}
	expiry = current_anchor_expiry(nest);
	expired = now - nest->last_update_ns > expiry;
	/* A weak futex waker can legitimately rotate across waiter CPUs. */
	maturity_cpu_shift = maturity_enabled() && maturity_source_strong(source) &&
		nest->anchor_cpu != cpu;
	if (expired && !admit) {
		if (maturity_enabled())
			stat_inc(STAT_ANCHOR_MATURITY_RESET_STALE);
		bpf_map_delete_elem(&lock_nests, key);
		stat_inc(STAT_STALE_NEST);
		return;
	}
	upgrade = nest->source == LOCK_SRC_FUTEX_WAKER && strong;
	if (expired || upgrade || maturity_cpu_shift) {
		nest->anchor_cpu = cpu;
		nest->source = source;
		if (adaptive_nest) {
			nest->adaptive_expiry_ns = initial_anchor_expiry(strong);
			nest->cooldown_until_ns = 0;
			nest->direct_hits = 0;
			nest->pressure_rejects = 0;
		}
		if (upgrade)
			maturity_reset(nest, cpu, source, now,
				       STAT_ANCHOR_MATURITY_RESET_SOURCE);
		else if (maturity_cpu_shift)
			maturity_reset(nest, cpu, source, now,
				       STAT_ANCHOR_MATURITY_RESET_CPU);
		else
			maturity_reset(nest, cpu, source, now,
				       STAT_ANCHOR_MATURITY_RESET_STALE);
		stat_inc(upgrade ? STAT_ANCHOR_UPGRADE : STAT_ANCHOR_REFRESH);
	} else {
		maturity_compatible_update(nest, cpu, source, now);
		stat_inc(STAT_ANCHOR_REFRESH);
	}
	nest->last_holder_cpu = cpu;
	nest->last_holder_tid = tid;
	nest->last_update_ns = now;
	nest->handoff_seq++;
}

static __always_inline void decrement_contention(u32 owner_tid)
{
	struct contention_info *info;
	u32 old;
	u32 i;

	if (!boost_enabled)
		return;
	info = bpf_map_lookup_elem(&contended_tasks, &owner_tid);
	if (!info)
		return;
	#pragma unroll
	for (i = 0; i < 4; i++) {
		old = info->waiter_count;
		if (!old) {
			stat_inc(STAT_WAITER_ORPHAN_CLEANUP);
			return;
		}
		if (__sync_val_compare_and_swap(&info->waiter_count, old, old - 1) == old) {
			if (info->boosted) {
				info->unblocked_since_boost++;
				stat_inc(STAT_WAITERS_UNBLOCKED_AFTER_BOOST);
				if (info->active_key.kind) {
					struct boost_lock_state *state;

					state = bpf_map_lookup_elem(&boost_lock_states,
								    &info->active_key);
					if (state) {
						state->waiters_unblocked++;
						state->efficiency_ewma_x8 =
							ewma8(state->efficiency_ewma_x8,
							      (u64)info->unblocked_since_boost << 3);
					}
				}
			}
			if (old == 1 && info->boosted) {
				info->boost_revoke_pending = true;
				info->boosted = false;
				info->boost_start_ns = 0;
				stat_inc(STAT_BOOST_REVOKE_UNLOCK);
			}
			return;
		}
	}
	stat_inc(STAT_WAITER_ORPHAN_CLEANUP);
}

static __always_inline bool remove_waiter_value(u32 tid,
						 struct waiter_info *waiter)
{
	/* futex-exit and select_cpu can claim the same waiter concurrently. */
	if (__sync_val_compare_and_swap(&waiter->removed, 0, 1) != 0)
		return false;
	bpf_map_delete_elem(&waiters, &tid);
	stat_inc(STAT_WAITER_CLEANUP);
	return true;
}

static __always_inline bool remove_waiter(u32 tid)
{
	struct waiter_info *waiter;

	waiter = bpf_map_lookup_elem(&waiters, &tid);
	if (!waiter)
		return false;
	return remove_waiter_value(tid, waiter);
}

static __always_inline void dispatch_runnable_owner_soon(u32 owner_tid,
						  s32 owner_cpu, u64 now)
{
	struct urgent_owner_slot *pending;
	struct urgent_owner_slot request = {
		.tid = owner_tid,
		.observed_ns = now,
	};
	u32 cpu;

	if (!boost_enabled || boost_profile == BOOST_PROFILE_NONE || owner_cpu < 0)
		return;
	cpu = (u32)owner_cpu;
	if (cpu >= scx_bpf_nr_cpu_ids())
		return;
	pending = bpf_map_lookup_elem(&urgent_owner_slots, &cpu);
	if (pending && pending->tid == owner_tid &&
	    now - pending->observed_ns <= 1000 * 1000)
		return;
	if (bpf_map_update_elem(&urgent_owner_slots, &cpu, &request, BPF_ANY)) {
		stat_inc(STAT_MAP_UPDATE_FAIL);
		return;
	}
	stat_inc(STAT_URGENT_OWNER_MARK);
	scx_bpf_kick_cpu(owner_cpu, SCX_KICK_PREEMPT);
}

static __always_inline void add_waiter(u32 tid, struct lock_key *key,
					       u32 owner_tid, bool owner_valid,
					       s32 anchor_cpu, u32 source,
					       u32 owner_state)
{
	struct contention_info *info;
	struct waiter_info *existing;
	u64 now = bpf_ktime_get_ns();
	struct contention_info new_info = {
		.waiter_count = 1,
		.last_contention_ns = now,
		.active_key = *key,
		.owner_confidence = confidence_for_waiter(owner_valid, source),
		.owner_observed_state = owner_state,
		.adaptive_threshold = high_contention_threshold,
		.last_waiter_count = 1,
	};
	u32 confidence = confidence_for_waiter(owner_valid, source);
	u32 action_caps = path_action_capabilities(key, source, owner_valid);
	struct lock_state *sync_state;

	account_confidence(confidence);
	struct waiter_info waiter = {
		.key = *key,
		.owner_tid = owner_tid,
		.anchor_cpu = anchor_cpu,
		.source = source,
		.owner_valid = owner_valid,
		.observed_ns = now,
	};

	existing = bpf_map_lookup_elem(&waiters, &tid);
	if (existing) {
		stat_inc(STAT_WAITER_OVERWRITE);
		remove_waiter_value(tid, existing);
	}
	sync_state = bpf_map_lookup_elem(&sync_states, key);
	if (sync_state)
		sync_state->action_capabilities |= action_caps;
	if (boost_enabled && owner_valid) {
		info = bpf_map_lookup_elem(&contended_tasks, &owner_tid);
		if (info) {
			if (!info->owner_confidence || !info->active_key.kind) {
				info->waiter_count = 1;
				info->boost_revoke_pending = false;
				info->boosted = false;
				info->boost_start_ns = 0;
			} else {
				__sync_fetch_and_add(&info->waiter_count, 1);
			}
			info->last_contention_ns = now;
			info->active_key = *key;
			info->owner_observed_state = owner_state;
			if (info->owner_confidence <
			    confidence_for_waiter(owner_valid, source))
				info->owner_confidence =
					confidence_for_waiter(owner_valid, source);
			if (!info->adaptive_threshold)
				info->adaptive_threshold = high_contention_threshold;
			update_lock_boost_state_on_waiter(key,
						  info->waiter_count,
						  now);
		} else if (bpf_map_update_elem(&contended_tasks, &owner_tid,
							&new_info, BPF_NOEXIST)) {
			info = bpf_map_lookup_elem(&contended_tasks, &owner_tid);
			if (info) {
				__sync_fetch_and_add(&info->waiter_count, 1);
				info->last_contention_ns = now;
				info->active_key = *key;
				info->owner_observed_state = owner_state;
				if (info->owner_confidence <
				    confidence_for_waiter(owner_valid, source))
					info->owner_confidence =
						confidence_for_waiter(owner_valid, source);
				if (!info->adaptive_threshold)
					info->adaptive_threshold =
						high_contention_threshold;
				update_lock_boost_state_on_waiter(key,
							  info->waiter_count,
							  now);
			} else {
				stat_inc(STAT_MAP_UPDATE_FAIL);
				waiter.owner_valid = false;
			}
		} else {
			update_lock_boost_state_on_waiter(key, 1, now);
		}
	}
	if (bpf_map_update_elem(&waiters, &tid, &waiter, BPF_ANY)) {
		if (boost_enabled && owner_valid)
			decrement_contention(owner_tid);
		stat_inc(STAT_MAP_UPDATE_FAIL);
	} else {
		stat_inc(STAT_WAITER_HIT);
	}
}

static __always_inline u32 resolve_pthread_owner(u64 uaddr, u32 waiter_tid,
						 s32 *owner_cpu_out,
						 u32 *owner_state_out)
{
	struct task_struct *owner_task;
	struct mm_struct *owner_mm;
	struct owner_validation_state *cached;
	struct owner_validation_state validation;
	struct recent_owner_validation *recent;
	struct recent_owner_validation recent_value;
	u64 waiter_mm;
	u64 now;
	s32 owner_cpu;
	u32 recent_slot = 0;
	u32 owner_on_cpu = 0;
	u32 owner_on_rq = 0;
	u32 owner_tid = 0;

	*owner_cpu_out = -1;
	*owner_state_out = OWNER_STATE_UNKNOWN;

	if (pthread_owner_offset < 0)
		return 0;
	if (bpf_probe_read_user(&owner_tid, sizeof(owner_tid),
				(void *)(uaddr + (u64)pthread_owner_offset))) {
		stat_inc(STAT_OWNER_WORD_REJECT);
		return 0;
	}
	if (!owner_tid || owner_tid == waiter_tid || owner_tid > 4194304) {
		stat_inc(STAT_OWNER_WORD_REJECT);
		return 0;
	}
	waiter_mm = lock_current_mm_id();
	if (!waiter_mm)
		return 0;
	if (boost_profile == BOOST_PROFILE_NONE) {
		now = bpf_ktime_get_ns();
		recent = bpf_map_lookup_elem(&recent_owner_cache, &recent_slot);
		if (recent && recent->tid == owner_tid &&
		    recent->mm_id == waiter_mm &&
		    now - recent->validated_ns <= 1000ULL * 1000 * 1000 &&
		    !stats_enabled && !diagnostic_mode)
			return owner_tid;
		cached = bpf_map_lookup_elem(&owner_validation_cache, &owner_tid);
		if (cached && cached->mm_id == waiter_mm &&
		    now - cached->validated_ns <= 1000ULL * 1000 * 1000 &&
		    !stats_enabled && !diagnostic_mode) {
			recent_value.tid = owner_tid;
			recent_value.mm_id = waiter_mm;
			recent_value.validated_ns = cached->validated_ns;
			bpf_map_update_elem(&recent_owner_cache, &recent_slot,
					    &recent_value, BPF_ANY);
			return owner_tid;
		}
	}
	owner_task = bpf_task_from_pid((s32)owner_tid);
	if (!owner_task) {
		stat_inc(STAT_OWNER_TASK_MISS);
		return 0;
	}
	owner_mm = BPF_CORE_READ(owner_task, mm);
	owner_cpu = scx_bpf_task_cpu(owner_task);
	owner_on_cpu = BPF_CORE_READ(owner_task, on_cpu);
	if (!owner_on_cpu)
		owner_on_rq = BPF_CORE_READ(owner_task, on_rq);
	if ((u64)owner_mm != waiter_mm) {
		stat_inc(STAT_OWNER_MM_MISMATCH);
		bpf_task_release(owner_task);
		return 0;
	}
	stat_inc(STAT_OWNER_VALIDATED);
	bpf_task_release(owner_task);
	if (stats_enabled || diagnostic_mode) {
		if (owner_on_cpu)
			stat_inc(STAT_OWNER_OBS_RUNNING);
		else if (owner_on_rq)
			stat_inc(STAT_OWNER_OBS_RUNNABLE);
		else
			stat_inc(STAT_OWNER_OBS_SLEEPING);
	}
	if (owner_on_cpu)
		*owner_state_out = OWNER_STATE_RUNNING;
	else if (owner_on_rq)
		*owner_state_out = OWNER_STATE_RUNNABLE;
	else
		*owner_state_out = OWNER_STATE_SLEEPING;
	if (boost_profile == BOOST_PROFILE_NONE) {
		validation.mm_id = waiter_mm;
		validation.validated_ns = now;
		bpf_map_update_elem(&owner_validation_cache, &owner_tid,
				    &validation, BPF_ANY);
		recent_value.tid = owner_tid;
		recent_value.mm_id = waiter_mm;
		recent_value.validated_ns = now;
		bpf_map_update_elem(&recent_owner_cache, &recent_slot,
				    &recent_value, BPF_ANY);
	}
	*owner_cpu_out = owner_cpu;
	return owner_tid;
}

SEC("kprobe/mutex_lock")
int BPF_KPROBE(handle_mutex_lock_enter, struct mutex *lock)
{
	u32 tid = current_tid();
	struct lock_key key = lock_kernel_mutex_key((u64)lock);

	if (!evidence_allowed(SCOPE_EVENT_MUTEX_ENTER))
		return 0;
	if (bpf_map_update_elem(&active_mutex_lock, &tid, &key, BPF_ANY))
		stat_inc(STAT_MAP_UPDATE_FAIL);
	return 0;
}

SEC("kretprobe/mutex_lock")
int BPF_KRETPROBE(handle_mutex_lock_exit)
{
	u32 tid = current_tid();
	struct lock_key *key;
	struct owner_info owner = {
		.tid = tid,
		.cpu = bpf_get_smp_processor_id(),
		.source = LOCK_SRC_MUTEX_OWNER,
		.observed_ns = bpf_ktime_get_ns(),
	};

	key = bpf_map_lookup_elem(&active_mutex_lock, &tid);
	if (!key)
		return 0;
	remove_waiter(tid);
	if (bpf_map_update_elem(&lock_owners, key, &owner, BPF_ANY))
		stat_inc(STAT_MAP_UPDATE_FAIL);
	bpf_map_delete_elem(&active_mutex_lock, &tid);
	return 0;
}

SEC("kprobe/mutex_unlock")
int BPF_KPROBE(handle_mutex_unlock, struct mutex *lock)
{
	struct lock_key key = lock_kernel_mutex_key((u64)lock);
	struct owner_info *owner;
	u32 tid = current_tid();
	s32 cpu = bpf_get_smp_processor_id();
	bool allowed = evidence_allowed(SCOPE_EVENT_MUTEX_UNLOCK);
	u64 now = bpf_ktime_get_ns();

	owner = bpf_map_lookup_elem(&lock_owners, &key);
	if (owner && owner->tid == tid) {
		if (allowed) {
			refresh_anchor(&key, tid, cpu, LOCK_SRC_MUTEX_RELEASER, true,
				       false);
			mark_lock_unlock_interval(&key, now);
		} else {
			stat_inc(STAT_EVIDENCE_CLEANUP_OUT_OF_SCOPE);
		}
		mark_owner_unlock(tid, &key, now);
		bpf_map_delete_elem(&lock_owners, &key);
	}
	return 0;
}

SEC("kprobe/__mutex_lock_slowpath")
int BPF_KPROBE(handle_mutex_contended, struct mutex *lock)
{
	struct lock_key key = lock_kernel_mutex_key((u64)lock);
	struct owner_info *owner;
	u32 tid = current_tid();

	if (!evidence_allowed(SCOPE_EVENT_MUTEX_SLOWPATH))
		return 0;
	owner = bpf_map_lookup_elem(&lock_owners, &key);
	if (!owner || owner->tid == tid) {
		stat_inc(STAT_MUTEX_OWNER_MISS);
		stat_inc(STAT_WAITER_MISS);
		return 0;
	}
	add_waiter(tid, &key, owner->tid, true, owner->cpu,
			   LOCK_SRC_MUTEX_OWNER, OWNER_STATE_UNKNOWN);
	refresh_anchor(&key, owner->tid, owner->cpu, LOCK_SRC_MUTEX_OWNER, true,
		       true);
	stat_inc(STAT_MUTEX_OWNER_HIT);
	stat_inc(STAT_OWNER_HIT);
	return 0;
}

static __always_inline int rwsem_acquire_enter(struct rw_semaphore *sem,
						       u32 mode)
{
	struct lock_key key = lock_kernel_rwsem_key((u64)sem);
	u32 tid = current_tid();
	u32 source = mode == LOCK_MODE_READ ?
		LOCK_SRC_RWSEM_READER : LOCK_SRC_RWSEM_WRITER;

	if (!evidence_current_allowed())
		return 0;
	sync_event_begin(tid, &key, LOCK_EVENT_ACQUIRE, mode, source, 0,
			 LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			 LOCK_CAP_READER_SET);
	return 0;
}

static __always_inline int rwsem_acquire_exit(u32 expected_mode)
{
	struct lock_event *pending;
	struct lock_event event;
	struct lock_state *state;
	struct owner_info owner;
	u32 tid = current_tid();
	u32 source;

	pending = bpf_map_lookup_elem(&pending_sync_events, &tid);
	if (!pending || pending->key.kind != LOCK_KEY_KERNEL_RWSEM ||
	    pending->mode != expected_mode)
		return 0;
	event = *pending;
	remove_waiter(tid);
	state = bpf_map_lookup_elem(&sync_states, &event.key);
	if (state && event.type == LOCK_EVENT_WAIT && state->waiter_count)
		__sync_fetch_and_sub(&state->waiter_count, 1);
	source = expected_mode == LOCK_MODE_READ ?
		LOCK_SRC_RWSEM_READER : LOCK_SRC_RWSEM_WRITER;
	if (!sync_note_acquire(&event.key, tid, expected_mode, source,
			       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			       LOCK_CAP_READER_SET))
		goto out;
	if (expected_mode == LOCK_MODE_WRITE) {
		owner.tid = tid;
		owner.cpu = bpf_get_smp_processor_id();
		owner.source = LOCK_SRC_RWSEM_WRITER;
		owner.observed_ns = bpf_ktime_get_ns();
		if (bpf_map_update_elem(&lock_owners, &event.key, &owner, BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		refresh_anchor(&event.key, tid, owner.cpu,
			       LOCK_SRC_RWSEM_WRITER, true, true);
		stat_inc(STAT_RWSEM_WRITE_ACQUIRE);
	} else {
		refresh_anchor(&event.key, tid, bpf_get_smp_processor_id(),
			       LOCK_SRC_RWSEM_READER, false, true);
		stat_inc(STAT_RWSEM_READ_ACQUIRE);
	}
out:
	sync_event_end(tid);
	return 0;
}

static __always_inline int rwsem_wait(struct rw_semaphore *sem, u32 mode)
{
	struct lock_key key = lock_kernel_rwsem_key((u64)sem);
	struct lock_state *state;
	struct lock_nest_info *nest;
	struct owner_info *owner;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 source = mode == LOCK_MODE_READ ?
		LOCK_SRC_RWSEM_READER : LOCK_SRC_RWSEM_WRITER;
	s32 anchor = -1;

	if (!evidence_current_allowed())
		return 0;
	state = sync_state_get(&key, source,
			       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			       LOCK_CAP_READER_SET);
	if (state)
		__sync_fetch_and_add(&state->waiter_count, 1);
	owner = bpf_map_lookup_elem(&lock_owners, &key);
	nest = bpf_map_lookup_elem(&lock_nests, &key);
	if (anchor_fresh(nest, now))
		anchor = nest->anchor_cpu;
	if (owner && owner->tid != tid) {
		add_waiter(tid, &key, owner->tid, true, anchor,
			   LOCK_SRC_RWSEM_WRITER, OWNER_STATE_UNKNOWN);
		stat_inc(STAT_OWNER_HIT);
	} else {
		add_waiter(tid, &key, 0, false, anchor, source,
			   OWNER_STATE_UNKNOWN);
		stat_inc(STAT_OWNER_MISS);
	}
	sync_event_begin(tid, &key, LOCK_EVENT_WAIT, mode, source,
			 owner ? owner->tid : 0,
			 LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			 LOCK_CAP_READER_SET);
	if (mode == LOCK_MODE_READ)
		stat_inc(STAT_RWSEM_READ_WAIT);
	else
		stat_inc(STAT_RWSEM_WRITE_WAIT);
	return 0;
}

static __always_inline int rwsem_release(struct rw_semaphore *sem)
{
	struct lock_key key = lock_kernel_rwsem_key((u64)sem);
	struct owner_info *owner;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 mode;
	s32 cpu = bpf_get_smp_processor_id();

	mode = sync_note_release(&key, tid, LOCK_SRC_RWSEM_READER);
	if (mode == LOCK_MODE_WRITE) {
		owner = bpf_map_lookup_elem(&lock_owners, &key);
		if (owner && owner->tid == tid) {
			refresh_anchor(&key, tid, cpu, LOCK_SRC_RWSEM_WRITER,
				       true, false);
			mark_owner_unlock(tid, &key, now);
			bpf_map_delete_elem(&lock_owners, &key);
		}
	} else if (mode == LOCK_MODE_READ) {
		refresh_anchor(&key, tid, cpu, LOCK_SRC_RWSEM_READER,
			       false, false);
	}
	if (mode != LOCK_MODE_NONE)
		stat_inc(STAT_RWSEM_RELEASE);
	return 0;
}

SEC("kprobe/down_read")
int BPF_KPROBE(handle_down_read_enter, struct rw_semaphore *sem)
{
	return rwsem_acquire_enter(sem, LOCK_MODE_READ);
}

SEC("kretprobe/down_read")
int BPF_KRETPROBE(handle_down_read_exit)
{
	return rwsem_acquire_exit(LOCK_MODE_READ);
}

SEC("kprobe/down_write")
int BPF_KPROBE(handle_down_write_enter, struct rw_semaphore *sem)
{
	return rwsem_acquire_enter(sem, LOCK_MODE_WRITE);
}

SEC("kretprobe/down_write")
int BPF_KRETPROBE(handle_down_write_exit)
{
	return rwsem_acquire_exit(LOCK_MODE_WRITE);
}

SEC("kprobe/rwsem_down_read_slowpath")
int BPF_KPROBE(handle_rwsem_read_contended, struct rw_semaphore *sem)
{
	return rwsem_wait(sem, LOCK_MODE_READ);
}

SEC("kprobe/rwsem_down_write_slowpath")
int BPF_KPROBE(handle_rwsem_write_contended, struct rw_semaphore *sem)
{
	return rwsem_wait(sem, LOCK_MODE_WRITE);
}

SEC("kprobe/up_read")
int BPF_KPROBE(handle_up_read, struct rw_semaphore *sem)
{
	return rwsem_release(sem);
}

SEC("kprobe/up_write")
int BPF_KPROBE(handle_up_write, struct rw_semaphore *sem)
{
	return rwsem_release(sem);
}

SEC("tp/syscalls/sys_enter_futex")
int handle_futex_enter(struct trace_event_raw_sys_enter *ctx)
{
	u64 uaddr = (u64)ctx->args[0];
	u32 raw_op = (u32)ctx->args[1];
	u32 cmd = lock_futex_cmd(raw_op);
	u32 tid = current_tid();
	struct futex_call_state call = {
		.uaddr = uaddr,
		.uaddr2 = (u64)ctx->args[4],
		.raw_op = raw_op,
		.cmd = cmd,
		.nr_wake = (u32)ctx->args[2],
		.nr_requeue = (u32)ctx->args[3],
	};
	struct lock_key key;
	struct lock_state *sync_state;
	struct lock_nest_info *nest;
	struct owner_info *hint;
	u64 now;
	s32 anchor = -1;
	s32 owner_cpu = -1;
	u32 owner_state = OWNER_STATE_UNKNOWN;
	u32 owner_tid = 0;

	if (evidence_scope_mode != LOCK_EVIDENCE_SCOPE_ALL &&
	    !evidence_current_allowed())
		return 0;
	/* pthread rwlock probes already carry the typed lock identity and mode. */
	if (bpf_map_lookup_elem(&pending_rwlock_events, &tid))
		return 0;

	if (lock_futex_is_wait(cmd) || lock_futex_is_wake(cmd) ||
	    lock_futex_is_pi(cmd) || lock_futex_is_requeue(cmd)) {
		if (bpf_map_update_elem(&futex_calls, &tid, &call, BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
	}

	if (!lock_private_futex_key(uaddr, raw_op, &key)) {
		if (!lock_futex_is_wait(cmd) && !lock_futex_is_wake(cmd) &&
		    !lock_futex_is_pi(cmd) && !lock_futex_is_requeue(cmd))
			stat_inc(STAT_FUTEX_SHARED_FALLBACK);
		return 0;
	}

	if (lock_futex_is_wait(cmd)) {
		if (!evidence_allowed(SCOPE_EVENT_FUTEX_WAIT))
			return 0;
		sync_state = sync_state_get(&key, LOCK_SRC_FUTEX_WAITER,
					    LOCK_CAP_IDENTITY |
					    LOCK_CAP_SINGLE_OWNER);
		if (sync_state)
			__sync_fetch_and_add(&sync_state->waiter_count, 1);
		sync_event_begin(tid, &key, LOCK_EVENT_WAIT, LOCK_MODE_EXCLUSIVE,
				 LOCK_SRC_FUTEX_WAITER, 0,
				 LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER);
		now = bpf_ktime_get_ns();
		hint = bpf_map_lookup_elem(&futex_hints, &key);
		if (hint && now - hint->observed_ns > nest_expiry_ns) {
			bpf_map_delete_elem(&futex_hints, &key);
			hint = NULL;
		}
		nest = bpf_map_lookup_elem(&lock_nests, &key);
		if (nest && !anchor_fresh(nest, now)) {
			bpf_map_delete_elem(&lock_nests, &key);
			stat_inc(STAT_STALE_NEST);
			nest = NULL;
		}
		if (hint && hint->tid != tid) {
			refresh_anchor(&key, hint->tid, hint->cpu,
				       LOCK_SRC_FUTEX_WAKER, false, true);
			nest = bpf_map_lookup_elem(&lock_nests, &key);
			now = bpf_ktime_get_ns();
		}
		if (anchor_fresh(nest, now))
			anchor = nest->anchor_cpu;
		owner_tid = resolve_pthread_owner(uaddr, tid, &owner_cpu,
						  &owner_state);
		if (anchor < 0 && !owner_tid) {
			stat_inc(STAT_FUTEX_PRIVATE_OWNER_MISS);
			stat_inc(STAT_OWNER_MISS);
			stat_inc(STAT_BOOST_BLOCKED_NO_OWNER);
			stat_inc(STAT_FUTEX_PRIVATE_WAIT);
			return 0;
		}
		if (owner_tid) {
			if (owner_cpu >= 0 && current_cpu_scope_constrained()) {
				refresh_anchor(&key, owner_tid, owner_cpu,
					       LOCK_SRC_PTHREAD_OWNER, true, true);
				nest = bpf_map_lookup_elem(&lock_nests, &key);
				now = bpf_ktime_get_ns();
				if (anchor_fresh(nest, now))
					anchor = nest->anchor_cpu;
			}
			add_waiter(tid, &key, owner_tid, true, anchor,
				   LOCK_SRC_PTHREAD_OWNER, owner_state);
			if (owner_state == OWNER_STATE_RUNNABLE)
				dispatch_runnable_owner_soon(owner_tid, owner_cpu, now);
			stat_inc(STAT_FUTEX_PRIVATE_OWNER_HIT);
			stat_inc(STAT_OWNER_HIT);
		} else if (boost_confidence_gate <= CONF_PROBABLE && hint &&
			   hint->tid != tid &&
			   now - hint->observed_ns <=
				   effective_probable_owner_window()) {
			/* A recent waker is weaker evidence than the pthread owner word.
			 * Keep it in the probable tier so only explicitly enabled short
			 * boost can act on it.
			 */
			add_waiter(tid, &key, hint->tid, true, anchor,
				   LOCK_SRC_FUTEX_WAKER, OWNER_STATE_UNKNOWN);
			stat_inc(STAT_FUTEX_PRIVATE_OWNER_MISS);
			stat_inc(STAT_OWNER_MISS);
		} else {
			add_waiter(tid, &key, 0, false, anchor, LOCK_SRC_NONE,
				   OWNER_STATE_UNKNOWN);
			stat_inc(STAT_FUTEX_PRIVATE_OWNER_MISS);
			stat_inc(STAT_OWNER_MISS);
			stat_inc(STAT_BOOST_BLOCKED_NO_OWNER);
		}
		stat_inc(STAT_FUTEX_PRIVATE_WAIT);
		return 0;
	}
	if (lock_futex_is_wake(cmd)) {
		struct owner_info new_hint = {
			.tid = tid,
			.cpu = bpf_get_smp_processor_id(),
			.source = LOCK_SRC_FUTEX_WAKER,
			.observed_ns = bpf_ktime_get_ns(),
		};

		if (!evidence_allowed(SCOPE_EVENT_FUTEX_WAKE)) {
			mark_owner_unlock(tid, &key, new_hint.observed_ns);
			stat_inc(STAT_EVIDENCE_CLEANUP_OUT_OF_SCOPE);
			return 0;
		}
		if (bpf_map_update_elem(&futex_hints, &key, &new_hint, BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		refresh_anchor(&key, tid, bpf_get_smp_processor_id(),
				       LOCK_SRC_FUTEX_WAKER, false, false);
		mark_lock_unlock_interval(&key, new_hint.observed_ns);
		mark_owner_unlock(tid, &key, new_hint.observed_ns);
		stat_inc(STAT_FUTEX_WAKE);
		return 0;
	}
	if (lock_futex_is_requeue_pi(cmd))
		stat_inc(STAT_FUTEX_PI_FALLBACK);
	else if (cmd == LC_FUTEX_WAKE_OP)
		stat_inc(STAT_FUTEX_REQUEUE_FALLBACK);
	return 0;
}

SEC("fexit/get_futex_key")
int BPF_PROG(handle_get_futex_key_exit, u32 *uaddr, unsigned int flags,
	     union futex_key *futex_key, enum futex_access rw, int ret)
{
	struct futex_call_state *call;
	struct lock_key key;
	u64 addr = (u64)uaddr;
	u32 tid = current_tid();
	u32 kind;

	(void)flags;
	(void)rw;
	if (ret)
		return 0;
	call = bpf_map_lookup_elem(&futex_calls, &tid);
	if (!call || (addr != call->uaddr && addr != call->uaddr2))
		return 0;
	if (lock_futex_is_pi(call->cmd)) {
		kind = LOCK_KEY_PI_FUTEX;
		if (!lock_canonical_futex_key(futex_key, kind, &key))
			return 0;
	} else if (lock_futex_is_requeue(call->cmd)) {
		kind = lock_futex_is_private(call->raw_op) ?
			LOCK_KEY_PRIVATE_FUTEX : LOCK_KEY_SHARED_FUTEX;
		if (lock_futex_is_private(call->raw_op)) {
			if (!lock_process_key(addr, kind, &key))
				return 0;
		} else if (!lock_canonical_futex_key(futex_key, kind, &key)) {
			return 0;
		}
	} else if (!lock_futex_is_private(call->raw_op)) {
		if (!lock_canonical_futex_key(futex_key, LOCK_KEY_SHARED_FUTEX,
					      &key))
			return 0;
	} else {
		return 0;
	}
	if (addr == call->uaddr) {
		call->canonical_key = key;
		call->key_valid = true;
	} else {
		call->canonical_key2 = key;
		call->key2_valid = true;
	}
	return 0;
}

SEC("kprobe/futex_q_lock")
int BPF_KPROBE(handle_futex_q_lock, struct futex_q *q,
		       struct futex_hash_bucket *hb)
{
	struct futex_call_state *call;
	struct lock_nest_info *nest;
	struct lock_state *state;
	struct task_struct *task;
	struct task_struct *owner_task;
	struct futex_pi_state *pi_state;
	struct owner_info *known_owner;
	struct lock_key key;
	u64 now = bpf_ktime_get_ns();
	u32 current = current_tid();
	u32 tid;
	u32 owner_tid = 0;
	u32 owner_state = OWNER_STATE_UNKNOWN;
	u32 owner_on_cpu;
	u32 owner_on_rq;
	s32 anchor = -1;
	s32 owner_cpu = -1;

	(void)hb;
	call = bpf_map_lookup_elem(&futex_calls, &current);
	if (!call)
		return 0;
	if (lock_futex_is_pi_lock(call->cmd)) {
		if (!lock_canonical_futex_key(&q->key, LOCK_KEY_PI_FUTEX,
					      &key)) {
			stat_inc(STAT_PI_KEY_MISS);
			return 0;
		}
		task = BPF_CORE_READ(q, task);
		tid = task ? BPF_CORE_READ(task, pid) : current;
		if (!tid)
			tid = current;
		pi_state = BPF_CORE_READ(q, pi_state);
		owner_task = pi_state ? BPF_CORE_READ(pi_state, owner) : NULL;
		if (owner_task) {
			owner_tid = BPF_CORE_READ(owner_task, pid);
			owner_cpu = BPF_CORE_READ(owner_task, thread_info.cpu);
			owner_on_cpu = BPF_CORE_READ(owner_task, on_cpu);
			owner_on_rq = owner_on_cpu ? 0 :
				BPF_CORE_READ(owner_task, on_rq);
			owner_state = owner_on_cpu ? OWNER_STATE_RUNNING :
				(owner_on_rq ? OWNER_STATE_RUNNABLE :
				 OWNER_STATE_SLEEPING);
		} else {
			known_owner = bpf_map_lookup_elem(&lock_owners, &key);
			if (known_owner) {
				owner_tid = known_owner->tid;
				owner_cpu = known_owner->cpu;
			}
		}
		nest = bpf_map_lookup_elem(&lock_nests, &key);
		if (anchor_fresh(nest, now))
			anchor = nest->anchor_cpu;
		state = sync_state_get(&key, LOCK_SRC_PI_OWNER,
				       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER);
		if (state)
			__sync_fetch_and_add(&state->waiter_count, 1);
		if (owner_tid && owner_tid != tid) {
			add_waiter(tid, &key, owner_tid, true, anchor,
				   LOCK_SRC_PI_OWNER, owner_state);
			if (owner_cpu >= 0)
				refresh_anchor(&key, owner_tid, owner_cpu,
					       LOCK_SRC_PI_OWNER, true, true);
			stat_inc(STAT_PI_OWNER_HIT);
			stat_inc(STAT_OWNER_HIT);
			call->pi_owner_resolved = true;
		} else {
			add_waiter(tid, &key, 0, false, anchor,
				   LOCK_SRC_PI_OWNER, OWNER_STATE_UNKNOWN);
		}
		sync_event_begin(tid, &key, LOCK_EVENT_WAIT,
				 LOCK_MODE_EXCLUSIVE, LOCK_SRC_PI_OWNER,
				 owner_tid,
				 LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER);
		call->canonical_key = key;
		call->key_valid = true;
		call->path_handled = true;
		stat_inc(STAT_PI_FUTEX_WAIT);
		return 0;
	}
	if (lock_futex_is_private(call->raw_op) ||
	    !lock_futex_is_wait(call->cmd) || call->path_handled)
		return 0;
	if (!lock_canonical_futex_key(&q->key, LOCK_KEY_SHARED_FUTEX, &key)) {
		stat_inc(STAT_SHARED_FUTEX_KEY_MISS);
		return 0;
	}
	task = BPF_CORE_READ(q, task);
	tid = task ? BPF_CORE_READ(task, pid) : current;
	if (!tid)
		tid = current;
	nest = bpf_map_lookup_elem(&lock_nests, &key);
	if (anchor_fresh(nest, now))
		anchor = nest->anchor_cpu;
	state = sync_state_get(&key, LOCK_SRC_FUTEX_CANONICAL,
			       LOCK_CAP_IDENTITY);
	if (state)
		__sync_fetch_and_add(&state->waiter_count, 1);
	add_waiter(tid, &key, 0, false, anchor,
		   LOCK_SRC_FUTEX_CANONICAL, OWNER_STATE_UNKNOWN);
	sync_event_begin(tid, &key, LOCK_EVENT_WAIT, LOCK_MODE_EXCLUSIVE,
			 LOCK_SRC_FUTEX_CANONICAL, 0, LOCK_CAP_IDENTITY);
	call->canonical_key = key;
	call->key_valid = true;
	call->path_handled = true;
	stat_inc(STAT_SHARED_FUTEX_WAIT);
	return 0;
}

SEC("kprobe/futex_lock_pi_atomic")
int BPF_KPROBE(handle_futex_lock_pi_atomic_enter, u32 *uaddr,
	       struct futex_hash_bucket *hb, union futex_key *futex_key,
	       struct futex_pi_state **ps)
{
	struct futex_call_state *call;
	struct pi_atomic_call args = {
		.key_ptr = (u64)futex_key,
		.pi_state_ptr_ptr = (u64)ps,
	};
	u32 tid = current_tid();

	(void)uaddr;
	(void)hb;
	call = bpf_map_lookup_elem(&futex_calls, &tid);
	if (!call || !lock_futex_is_pi_lock(call->cmd))
		return 0;
	if (bpf_map_update_elem(&pi_atomic_calls, &tid, &args, BPF_ANY))
		stat_inc(STAT_MAP_UPDATE_FAIL);
	return 0;
}

SEC("kretprobe/futex_lock_pi_atomic")
int BPF_KRETPROBE(handle_futex_lock_pi_atomic_exit)
{
	struct futex_call_state *call;
	struct pi_atomic_call *saved;
	struct pi_atomic_call args;
	struct futex_pi_state *pi_state = NULL;
	struct task_struct *owner_task;
	union futex_key *futex_key;
	struct futex_pi_state **ps;
	struct lock_event *event;
	struct waiter_info *waiter;
	struct lock_nest_info *nest;
	struct lock_key key;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 owner_tid;
	u32 owner_on_cpu;
	u32 owner_on_rq;
	u32 owner_state;
	s32 owner_cpu;
	s32 anchor = -1;

	saved = bpf_map_lookup_elem(&pi_atomic_calls, &tid);
	if (!saved)
		return 0;
	args = *saved;
	bpf_map_delete_elem(&pi_atomic_calls, &tid);
	call = bpf_map_lookup_elem(&futex_calls, &tid);
	if (!call || !call->path_handled || call->pi_owner_resolved ||
	    !lock_futex_is_pi_lock(call->cmd))
		return 0;
	futex_key = (union futex_key *)args.key_ptr;
	ps = (struct futex_pi_state **)args.pi_state_ptr_ptr;
	if (!lock_canonical_futex_key(futex_key, LOCK_KEY_PI_FUTEX, &key)) {
		stat_inc(STAT_PI_KEY_MISS);
		return 0;
	}
	if (bpf_probe_read_kernel(&pi_state, sizeof(pi_state), ps) || !pi_state)
		goto miss;
	owner_task = BPF_CORE_READ(pi_state, owner);
	if (!owner_task)
		goto miss;
	owner_tid = BPF_CORE_READ(owner_task, pid);
	if (!owner_tid || owner_tid == tid)
		goto miss;
	owner_cpu = BPF_CORE_READ(owner_task, thread_info.cpu);
	owner_on_cpu = BPF_CORE_READ(owner_task, on_cpu);
	owner_on_rq = owner_on_cpu ? 0 : BPF_CORE_READ(owner_task, on_rq);
	owner_state = owner_on_cpu ? OWNER_STATE_RUNNING :
		(owner_on_rq ? OWNER_STATE_RUNNABLE : OWNER_STATE_SLEEPING);
	nest = bpf_map_lookup_elem(&lock_nests, &key);
	if (anchor_fresh(nest, now))
		anchor = nest->anchor_cpu;
	waiter = bpf_map_lookup_elem(&waiters, &tid);
	if (!waiter || !waiter->owner_valid)
		add_waiter(tid, &key, owner_tid, true, anchor,
			   LOCK_SRC_PI_OWNER, owner_state);
	event = bpf_map_lookup_elem(&pending_sync_events, &tid);
	if (event) {
		event->owner_tid = owner_tid;
		event->source = LOCK_SRC_PI_OWNER;
	}
	if (owner_cpu >= 0)
		refresh_anchor(&key, owner_tid, owner_cpu, LOCK_SRC_PI_OWNER,
			       true, true);
	call->canonical_key = key;
	call->key_valid = true;
	call->pi_owner_resolved = true;
	stat_inc(STAT_PI_OWNER_HIT);
	stat_inc(STAT_OWNER_HIT);
	return 0;
miss:
	call->pi_owner_resolved = true;
	stat_inc(STAT_PI_OWNER_MISS);
	stat_inc(STAT_OWNER_MISS);
	return 0;
}

SEC("kprobe/futex_wake_mark")
int BPF_KPROBE(handle_futex_wake_mark, struct wake_q_head *wake_q,
		       struct futex_q *q)
{
	struct futex_call_state *call;
	struct owner_info hint;
	struct lock_state *state;
	struct task_struct *task;
	struct waiter_info *waiter;
	struct lock_key key;
	u64 now = bpf_ktime_get_ns();
	u32 current = current_tid();
	u32 waiter_tid;

	(void)wake_q;
	call = bpf_map_lookup_elem(&futex_calls, &current);
	if (!call)
		return 0;
	if (lock_futex_is_requeue(call->cmd)) {
		task = BPF_CORE_READ(q, task);
		waiter_tid = task ? BPF_CORE_READ(task, pid) : 0;
		if (waiter_tid) {
			waiter = bpf_map_lookup_elem(&waiters, &waiter_tid);
			if (waiter)
				waiter->requeue_bypass_ns = now;
		}
		return 0;
	}
	if (lock_futex_is_private(call->raw_op) ||
	    !lock_futex_is_wake(call->cmd))
		return 0;
	if (!lock_canonical_futex_key(&q->key, LOCK_KEY_SHARED_FUTEX, &key)) {
		stat_inc(STAT_SHARED_FUTEX_KEY_MISS);
		return 0;
	}
	hint.tid = current;
	hint.cpu = bpf_get_smp_processor_id();
	hint.source = LOCK_SRC_FUTEX_WAKER;
	hint.observed_ns = now;
	if (bpf_map_update_elem(&futex_hints, &key, &hint, BPF_ANY))
		stat_inc(STAT_MAP_UPDATE_FAIL);
	refresh_anchor(&key, current, hint.cpu, LOCK_SRC_FUTEX_WAKER,
		       false, true);
	mark_lock_unlock_interval(&key, now);
	state = bpf_map_lookup_elem(&sync_states, &key);
	if (state) {
		task = BPF_CORE_READ(q, task);
		waiter_tid = task ? BPF_CORE_READ(task, pid) : 0;
		if (state->waiter_count)
			__sync_fetch_and_sub(&state->waiter_count, 1);
		state->anchor_cpu = hint.cpu;
		state->source = LOCK_SRC_FUTEX_WAKER;
		state->updated_ns = now;
		(void)waiter_tid;
	}
	stat_inc(STAT_SHARED_FUTEX_WAKE);
	return 0;
}

static __always_inline void handle_requeue_exit(
						struct futex_call_state *call,
						long ret)
{
	struct requeue_redirect redirect;
	struct lock_state *source_state;
	struct lock_state *destination_state;
	struct lock_nest_info *nest;
	u64 now = bpf_ktime_get_ns();
	u32 completed;
	u32 woken;
	u32 moved;

	stat_inc(STAT_REQUEUE_OP);
	if (ret < 0)
		return;
	if (!call->key_valid || !call->key2_valid) {
		stat_inc(STAT_REQUEUE_KEY_MISS);
		return;
	}
	completed = (u32)ret;
	woken = completed < call->nr_wake ? completed : call->nr_wake;
	moved = completed - woken;
	if (moved > call->nr_requeue)
		moved = call->nr_requeue;
	if (!moved || lock_key_equal(&call->canonical_key,
					      &call->canonical_key2))
		return;
	redirect.destination = call->canonical_key2;
	redirect.observed_ns = now;
	redirect.remaining = moved;
	redirect.source_cmd = call->cmd;
	if (bpf_map_update_elem(&requeue_redirects, &call->canonical_key,
				&redirect, BPF_ANY)) {
		stat_inc(STAT_MAP_UPDATE_FAIL);
		return;
	}
	source_state = bpf_map_lookup_elem(&sync_states, &call->canonical_key);
	destination_state = sync_state_get(&call->canonical_key2,
					   LOCK_SRC_REQUEUE,
					   LOCK_CAP_IDENTITY |
					   LOCK_CAP_TRANSFER);
	if (source_state) {
		u32 source_waiters = source_state->waiter_count;

		if (source_waiters >= moved)
			__sync_fetch_and_sub(&source_state->waiter_count, moved);
		else
			source_state->waiter_count = 0;
		source_state->updated_ns = now;
	}
	if (destination_state) {
		__sync_fetch_and_add(&destination_state->waiter_count, moved);
		destination_state->capabilities |= LOCK_CAP_TRANSFER;
		destination_state->updated_ns = now;
	}
	nest = bpf_map_lookup_elem(&lock_nests, &call->canonical_key);
	if (anchor_fresh(nest, now))
		refresh_anchor(&call->canonical_key2, nest->last_holder_tid,
			       nest->anchor_cpu, LOCK_SRC_REQUEUE, false, true);
	stat_add(STAT_REQUEUE_TRANSFER, moved);
}

static __always_inline void handle_pi_futex_exit(
						struct futex_call_state *call,
						long ret)
{
	struct lock_state *state;
	struct owner_info owner;
	struct lock_key key;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 mode;

	if (!call->key_valid) {
		if (!ret)
			stat_inc(STAT_PI_KEY_MISS);
		return;
	}
	key = call->canonical_key;
	if (lock_futex_is_pi_lock(call->cmd)) {
		state = bpf_map_lookup_elem(&sync_states, &key);
		if (state && call->path_handled && state->waiter_count)
			__sync_fetch_and_sub(&state->waiter_count, 1);
		remove_waiter(tid);
		if (ret)
			return;
		if (!sync_note_acquire(&key, tid, LOCK_MODE_EXCLUSIVE,
				       LOCK_SRC_PI_OWNER,
				       LOCK_CAP_IDENTITY |
				       LOCK_CAP_SINGLE_OWNER))
			return;
		state = bpf_map_lookup_elem(&sync_states, &key);
		if (state)
			state->action_capabilities = path_action_capabilities(
				&key, LOCK_SRC_PI_OWNER, true);
		owner.tid = tid;
		owner.cpu = bpf_get_smp_processor_id();
		owner.source = LOCK_SRC_PI_OWNER;
		owner.observed_ns = now;
		if (bpf_map_update_elem(&lock_owners, &key, &owner, BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		refresh_anchor(&key, tid, owner.cpu, LOCK_SRC_PI_OWNER,
			       true, true);
		stat_inc(STAT_PI_FUTEX_ACQUIRE);
		return;
	}
	if (!lock_futex_is_pi_unlock(call->cmd) || ret)
		return;
	mode = sync_note_release(&key, tid, LOCK_SRC_PI_OWNER);
	if (mode != LOCK_MODE_NONE) {
		refresh_anchor(&key, tid, bpf_get_smp_processor_id(),
			       LOCK_SRC_PI_OWNER, true, false);
		mark_lock_unlock_interval(&key, now);
		mark_owner_unlock(tid, &key, now);
		bpf_map_delete_elem(&lock_owners, &key);
		stat_inc(STAT_PI_FUTEX_RELEASE);
	}
}

SEC("tp/syscalls/sys_exit_futex")
int handle_futex_exit(struct trace_event_raw_sys_exit *ctx)
{
	struct futex_call_state *call;
	struct futex_call_state copy;
	struct lock_state *state;
	struct lock_key key;
	u32 tid = current_tid();
	bool out_of_scope = evidence_scope_mode != LOCK_EVIDENCE_SCOPE_ALL &&
		!evidence_current_allowed();

	if (bpf_map_lookup_elem(&pending_rwlock_events, &tid))
		return 0;

	call = bpf_map_lookup_elem(&futex_calls, &tid);
	if (!call && out_of_scope)
		return 0;
	if (call) {
		copy = *call;
		if (lock_futex_is_pi_lock(copy.cmd) ||
		    lock_futex_is_pi_unlock(copy.cmd))
			handle_pi_futex_exit(&copy, ctx->ret);
		else if (lock_futex_is_requeue(copy.cmd))
			handle_requeue_exit(&copy, ctx->ret);
		if (lock_futex_is_private(copy.raw_op) &&
		    lock_futex_is_wait(copy.cmd) &&
		    lock_process_key(copy.uaddr, LOCK_KEY_PRIVATE_FUTEX, &key)) {
			state = bpf_map_lookup_elem(&sync_states, &key);
			if (state && state->waiter_count)
				__sync_fetch_and_sub(&state->waiter_count, 1);
		}
	}
	if (remove_waiter(tid) && out_of_scope)
		stat_inc(STAT_EVIDENCE_CLEANUP_OUT_OF_SCOPE);
	sync_event_end(tid);
	bpf_map_delete_elem(&futex_calls, &tid);
	return 0;
}

static __always_inline int pthread_rwlock_enter(u64 addr, u32 mode)
{
	struct lock_state *state;
	struct owner_info *owner;
	struct lock_key key;
	u32 tid = current_tid();
	u32 owner_tid = 0;
	bool contended = false;

	if (!evidence_current_allowed())
		return 0;
	if (!lock_process_key(addr, LOCK_KEY_PTHREAD_RWLOCK, &key))
		return 0;
	state = sync_state_get(&key, LOCK_SRC_PTHREAD_RWLOCK,
			       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			       LOCK_CAP_READER_SET);
	if (state) {
		if (mode == LOCK_MODE_READ)
			contended = state->writer_tid != 0;
		else
			contended = state->writer_tid != 0 ||
				state->reader_count != 0;
	}
	if (!contended) {
		rwlock_event_begin(tid, &key, LOCK_EVENT_ACQUIRE, mode, 0);
		return 0;
	}
	if (state)
		__sync_fetch_and_add(&state->waiter_count, 1);
	owner = bpf_map_lookup_elem(&lock_owners, &key);
	if (owner && owner->tid != tid) {
		owner_tid = owner->tid;
		add_waiter(tid, &key, owner_tid, true, ANCHOR_CPU_DISABLED,
			   LOCK_SRC_PTHREAD_RWLOCK, OWNER_STATE_UNKNOWN);
		stat_inc(STAT_OWNER_HIT);
	} else {
		add_waiter(tid, &key, 0, false, ANCHOR_CPU_DISABLED,
			   LOCK_SRC_PTHREAD_RWLOCK, OWNER_STATE_UNKNOWN);
		stat_inc(STAT_OWNER_MISS);
	}
	rwlock_event_begin(tid, &key, LOCK_EVENT_WAIT, mode, owner_tid);
	stat_inc(STAT_PTHREAD_RWLOCK_WAIT);
	return 0;
}

static __always_inline int pthread_rwlock_acquire_exit(long ret,
						       u32 expected_mode)
{
	struct lock_event *pending;
	struct lock_event event;
	struct lock_state *state;
	struct owner_info owner;
	u32 tid = current_tid();

	pending = bpf_map_lookup_elem(&pending_rwlock_events, &tid);
	if (!pending || pending->key.kind != LOCK_KEY_PTHREAD_RWLOCK ||
	    pending->mode != expected_mode)
		return 0;
	event = *pending;
	if (event.type == LOCK_EVENT_WAIT) {
		state = bpf_map_lookup_elem(&sync_states, &event.key);
		if (state && state->waiter_count)
			__sync_fetch_and_sub(&state->waiter_count, 1);
		remove_waiter(tid);
	}
	if (ret)
		goto out;
	if (!sync_note_acquire(&event.key, tid, expected_mode,
			       LOCK_SRC_PTHREAD_RWLOCK,
			       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			       LOCK_CAP_READER_SET))
		goto out;
	state = bpf_map_lookup_elem(&sync_states, &event.key);
	if (state)
		state->action_capabilities = path_action_capabilities(
			&event.key, LOCK_SRC_PTHREAD_RWLOCK,
			expected_mode == LOCK_MODE_WRITE);
	if (expected_mode == LOCK_MODE_WRITE) {
		owner.tid = tid;
		owner.cpu = bpf_get_smp_processor_id();
		owner.source = LOCK_SRC_PTHREAD_RWLOCK;
		owner.observed_ns = bpf_ktime_get_ns();
		if (bpf_map_update_elem(&lock_owners, &event.key, &owner, BPF_ANY))
			stat_inc(STAT_MAP_UPDATE_FAIL);
		stat_inc(STAT_PTHREAD_RWLOCK_WRITE);
	} else {
		stat_inc(STAT_PTHREAD_RWLOCK_READ);
	}
out:
	rwlock_event_end(tid);
	return 0;
}

static __always_inline int pthread_rwlock_release_enter(u64 addr)
{
	struct lock_key key;
	u32 tid = current_tid();

	if (!evidence_current_allowed())
		return 0;
	if (!lock_process_key(addr, LOCK_KEY_PTHREAD_RWLOCK, &key))
		return 0;
	rwlock_event_begin(tid, &key, LOCK_EVENT_RELEASE, LOCK_MODE_NONE, 0);
	return 0;
}

static __always_inline int pthread_rwlock_release_exit(long ret)
{
	struct lock_event *pending;
	struct lock_event event;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 mode;

	pending = bpf_map_lookup_elem(&pending_rwlock_events, &tid);
	if (!pending || pending->key.kind != LOCK_KEY_PTHREAD_RWLOCK ||
	    pending->type != LOCK_EVENT_RELEASE)
		return 0;
	event = *pending;
	if (ret)
		goto out;
	mode = sync_note_release(&event.key, tid, LOCK_SRC_PTHREAD_RWLOCK);
	if (mode == LOCK_MODE_WRITE) {
		mark_owner_unlock(tid, &event.key, now);
		bpf_map_delete_elem(&lock_owners, &event.key);
	}
	if (mode != LOCK_MODE_NONE)
		stat_inc(STAT_PTHREAD_RWLOCK_RELEASE);
out:
	rwlock_event_end(tid);
	return 0;
}

SEC("uprobe")
int handle_pthread_rwlock_rdlock_enter(struct pt_regs *ctx)
{
	return pthread_rwlock_enter(PT_REGS_PARM1(ctx), LOCK_MODE_READ);
}

SEC("uretprobe")
int handle_pthread_rwlock_rdlock_exit(struct pt_regs *ctx)
{
	return pthread_rwlock_acquire_exit(PT_REGS_RC(ctx), LOCK_MODE_READ);
}

SEC("uprobe")
int handle_pthread_rwlock_wrlock_enter(struct pt_regs *ctx)
{
	return pthread_rwlock_enter(PT_REGS_PARM1(ctx), LOCK_MODE_WRITE);
}

SEC("uretprobe")
int handle_pthread_rwlock_wrlock_exit(struct pt_regs *ctx)
{
	return pthread_rwlock_acquire_exit(PT_REGS_RC(ctx), LOCK_MODE_WRITE);
}

SEC("uprobe")
int handle_pthread_rwlock_unlock_enter(struct pt_regs *ctx)
{
	return pthread_rwlock_release_enter(PT_REGS_PARM1(ctx));
}

SEC("uretprobe")
int handle_pthread_rwlock_unlock_exit(struct pt_regs *ctx)
{
	return pthread_rwlock_release_exit(PT_REGS_RC(ctx));
}

static __always_inline bool validate_annotated_owner(const struct lock_key *key,
						      u32 owner_tid,
						      u32 waiter_tid,
						      s32 *owner_cpu,
						      u32 *owner_state)
{
	struct owner_info *owner;
	u64 now = bpf_ktime_get_ns();

	*owner_cpu = -1;
	*owner_state = OWNER_STATE_UNKNOWN;
	if (!owner_tid || owner_tid == waiter_tid)
		return false;
	owner = bpf_map_lookup_elem(&lock_owners, key);
	if (!owner || owner->tid != owner_tid ||
	    now - owner->observed_ns > nest_expiry_ns)
		return false;
	*owner_cpu = owner->cpu;
	return true;
}

static __always_inline int user_sync_wait(u64 addr, u32 owner_tid,
					  u32 kind, u32 mode)
{
	struct lock_nest_info *nest;
	struct lock_state *state;
	struct lock_key key;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 owner_state;
	s32 owner_cpu;
	s32 anchor = -1;
	bool owner_valid;

	if (!lock_process_key(addr, kind, &key))
		return 0;
	owner_valid = validate_annotated_owner(&key, owner_tid, tid, &owner_cpu,
						 &owner_state);
	nest = bpf_map_lookup_elem(&lock_nests, &key);
	if (anchor_fresh(nest, now))
		anchor = nest->anchor_cpu;
	state = sync_state_get(&key, LOCK_SRC_USER_ANNOTATION,
			       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			       LOCK_CAP_USER_ANNOTATED);
	if (state)
		__sync_fetch_and_add(&state->waiter_count, 1);
	add_waiter(tid, &key, owner_tid, owner_valid, anchor,
		   LOCK_SRC_USER_ANNOTATION, owner_state);
	if (owner_valid && owner_cpu >= 0)
		refresh_anchor(&key, owner_tid, owner_cpu,
			       LOCK_SRC_USER_ANNOTATION, true, true);
	sync_event_begin(tid, &key, LOCK_EVENT_WAIT, mode,
			 LOCK_SRC_USER_ANNOTATION,
			 owner_valid ? owner_tid : 0,
			 LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			 LOCK_CAP_USER_ANNOTATED);
	if (kind == LOCK_KEY_USER_SPIN)
		stat_inc(STAT_USER_SPIN_WAIT);
	else
		stat_inc(STAT_USER_CAS_WAIT);
	return 0;
}

static __always_inline int user_sync_acquired(u64 addr, u32 kind, u32 mode)
{
	struct lock_state *state;
	struct owner_info owner;
	struct lock_key key;
	u32 tid = current_tid();

	if (!lock_process_key(addr, kind, &key))
		return 0;
	state = bpf_map_lookup_elem(&sync_states, &key);
	if (state && state->waiter_count)
		__sync_fetch_and_sub(&state->waiter_count, 1);
	remove_waiter(tid);
	if (!sync_note_acquire(&key, tid, mode, LOCK_SRC_USER_ANNOTATION,
			       LOCK_CAP_IDENTITY | LOCK_CAP_SINGLE_OWNER |
			       LOCK_CAP_USER_ANNOTATED))
		return 0;
	state = bpf_map_lookup_elem(&sync_states, &key);
	if (state)
		state->action_capabilities = path_action_capabilities(
			&key, LOCK_SRC_USER_ANNOTATION, true);
	owner.tid = tid;
	owner.cpu = bpf_get_smp_processor_id();
	owner.source = LOCK_SRC_USER_ANNOTATION;
	owner.observed_ns = bpf_ktime_get_ns();
	if (bpf_map_update_elem(&lock_owners, &key, &owner, BPF_ANY))
		stat_inc(STAT_MAP_UPDATE_FAIL);
	refresh_anchor(&key, tid, owner.cpu, LOCK_SRC_USER_ANNOTATION,
		       true, true);
	sync_event_end(tid);
	if (kind == LOCK_KEY_USER_SPIN)
		stat_inc(STAT_USER_SPIN_ACQUIRE);
	else
		stat_inc(STAT_USER_CAS_ACQUIRE);
	return 0;
}

static __always_inline int user_sync_release(u64 addr, u32 kind)
{
	struct lock_key key;
	u64 now = bpf_ktime_get_ns();
	u32 tid = current_tid();
	u32 mode;

	if (!lock_process_key(addr, kind, &key))
		return 0;
	mode = sync_note_release(&key, tid, LOCK_SRC_USER_ANNOTATION);
	if (mode == LOCK_MODE_NONE)
		return 0;
	refresh_anchor(&key, tid, bpf_get_smp_processor_id(),
		       LOCK_SRC_USER_ANNOTATION, true, false);
	mark_lock_unlock_interval(&key, now);
	mark_owner_unlock(tid, &key, now);
	bpf_map_delete_elem(&lock_owners, &key);
	if (kind == LOCK_KEY_USER_SPIN)
		stat_inc(STAT_USER_SPIN_RELEASE);
	else
		stat_inc(STAT_USER_CAS_RELEASE);
	return 0;
}

SEC("uprobe")
int handle_user_spin_wait(struct pt_regs *ctx)
{
	return user_sync_wait(PT_REGS_PARM1(ctx), (u32)PT_REGS_PARM2(ctx),
			      LOCK_KEY_USER_SPIN, LOCK_MODE_SPIN);
}

SEC("uprobe")
int handle_user_spin_acquired(struct pt_regs *ctx)
{
	return user_sync_acquired(PT_REGS_PARM1(ctx), LOCK_KEY_USER_SPIN,
				  LOCK_MODE_SPIN);
}

SEC("uprobe")
int handle_user_spin_release(struct pt_regs *ctx)
{
	return user_sync_release(PT_REGS_PARM1(ctx), LOCK_KEY_USER_SPIN);
}

SEC("uprobe")
int handle_user_cas_wait(struct pt_regs *ctx)
{
	return user_sync_wait(PT_REGS_PARM1(ctx), (u32)PT_REGS_PARM2(ctx),
			      LOCK_KEY_USER_CAS, LOCK_MODE_CAS);
}

SEC("uprobe")
int handle_user_cas_acquired(struct pt_regs *ctx)
{
	return user_sync_acquired(PT_REGS_PARM1(ctx), LOCK_KEY_USER_CAS,
				  LOCK_MODE_CAS);
}

SEC("uprobe")
int handle_user_cas_release(struct pt_regs *ctx)
{
	return user_sync_release(PT_REGS_PARM1(ctx), LOCK_KEY_USER_CAS);
}

struct sched_wakeup_args {
	unsigned long long unused;
	char comm[16];
	pid_t pid;
	int prio;
	int target_cpu;
};

SEC("tp/sched/sched_wakeup")
int handle_sched_wakeup(struct sched_wakeup_args *ctx)
{
	u32 tid = ctx->pid;

	if (!bpf_map_lookup_elem(&waiters, &tid))
		stat_inc(STAT_WAITER_MISS);
	return 0;
}

static __always_inline void resolve_requeue_destination(
						struct waiter_info *waiter,
						u64 now)
{
	struct requeue_redirect *redirect;
	u32 remaining;
	u32 i;

	redirect = bpf_map_lookup_elem(&requeue_redirects, &waiter->key);
	if (!redirect)
		return;
	if (now - redirect->observed_ns > 100 * 1000 * 1000) {
		bpf_map_delete_elem(&requeue_redirects, &waiter->key);
		return;
	}
	if (waiter->requeue_bypass_ns &&
	    redirect->observed_ns >= waiter->requeue_bypass_ns &&
	    redirect->observed_ns - waiter->requeue_bypass_ns <=
		50 * 1000 * 1000)
		return;
	#pragma unroll
	for (i = 0; i < 4; i++) {
		remaining = redirect->remaining;
		if (!remaining)
			return;
		if (__sync_val_compare_and_swap(&redirect->remaining, remaining,
						remaining - 1) == remaining) {
			waiter->key = redirect->destination;
			waiter->source = LOCK_SRC_REQUEUE;
			stat_inc(STAT_PATH_NEST_ACTION);
			return;
		}
	}
}

static __always_inline s32 dispatch_prev_cpu_if_idle(struct task_struct *p,
						     s32 prev_cpu)
{
	if (!adaptive_nest || prev_cpu < 0)
		return -1;
	if (!bpf_cpumask_test_cpu(prev_cpu, p->cpus_ptr))
		return -1;
	if (!scx_bpf_test_and_clear_cpu_idle(prev_cpu))
		return -1;
	stat_inc(STAT_ANCHOR_PREVCPU_FALLBACK);
	scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (u64)prev_cpu,
			   SCX_SLICE_DFL, 0);
	return prev_cpu;
}

static __always_inline s32 dispatch_default(struct task_struct *p, s32 prev_cpu,
					     u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu;

	stat_inc(STAT_ANCHOR_DEFAULT_FALLBACK);
	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle) {
		stat_inc(STAT_LOCAL);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, 0);
	}
	return cpu;
}

static __always_inline void mark_owner_runnable(struct task_struct *p)
{
	struct contention_info *info;
	u32 tid = p->pid;

	if (!boost_enabled ||
	    (boost_profile != BOOST_PROFILE_ADAPTIVE && !diagnostic_mode))
		return;
	if (fifo_sched)
		return;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (info)
		info->last_runnable_ns = bpf_ktime_get_ns();
}

static __always_inline void mark_owner_running(struct task_struct *p)
{
	struct contention_info *info;
	u64 now;
	u64 delay;
	u32 tid = p->pid;

	if (!boost_enabled ||
	    (boost_profile != BOOST_PROFILE_ADAPTIVE && !diagnostic_mode))
		return;
	if (fifo_sched)
		return;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (!info || !info->last_runnable_ns)
		return;
	now = bpf_ktime_get_ns();
	if (now <= info->last_runnable_ns) {
		info->last_runnable_ns = 0;
		return;
	}
	delay = now - info->last_runnable_ns;
	info->owner_delay_ewma_ns = ewma8(info->owner_delay_ewma_ns, delay);
	info->last_runnable_ns = 0;
	stat_add(STAT_OWNER_DELAY_SUM_NS, delay);
	stat_inc(STAT_OWNER_DELAY_COUNT);
}

static __always_inline void account_successful_boost(struct contention_info *info,
						     u32 kind, u64 now)
{
	struct boost_lock_state *lock_state;
	u64 cgid;
	struct boost_cgroup_state *cg_state;

	info->boosted = true;
	info->last_boost_kind = kind;
	info->last_boost_ns = now;
	info->last_waiter_count = info->waiter_count;
	info->unblocked_since_boost = 0;
	if (!info->boost_start_ns)
		info->boost_start_ns = now;
	if (info->active_key.kind) {
		lock_state = bpf_map_lookup_elem(&boost_lock_states,
						 &info->active_key);
		if (lock_state)
			lock_state->boost_count++;
	}
	if (boost_cgroup_budget_pct) {
		cgid = bpf_get_current_cgroup_id();
		cg_state = bpf_map_lookup_elem(&boost_cgroup_states, &cgid);
		if (cg_state)
			cg_state->boost_count++;
	}
	stat_inc(STAT_BOOST_APPLIED);
	if (kind == BOOST_KIND_SHORT) {
		stat_inc(STAT_BOOST_SHORT_APPLIED);
		stat_inc(STAT_PATH_SHORT_BOOST_ACTION);
	} else if (kind == BOOST_KIND_ADAPTIVE) {
		stat_inc(STAT_BOOST_ADAPTIVE_APPLIED);
		stat_inc(STAT_PATH_STRONG_BOOST_ACTION);
	} else if (kind == BOOST_KIND_INF) {
		stat_inc(STAT_BOOST_INF_APPLIED);
		stat_inc(STAT_PATH_STRONG_BOOST_ACTION);
	} else {
		stat_inc(STAT_BOOST_BOUNDED_APPLIED);
		stat_inc(STAT_PATH_STRONG_BOOST_ACTION);
	}
}

static __always_inline void boost_owner_on_run(struct task_struct *p)
{
	struct contention_info *info;
	u64 now;
	u64 grant;
	u32 tid = p->pid;

	if (!boost_enabled || boost_profile == BOOST_PROFILE_NONE ||
	    boost_profile == BOOST_PROFILE_SHORT || fifo_sched)
		return;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (!info)
		return;
	if (info->boosted || !info->waiter_count ||
	    info->owner_confidence < boost_confidence_gate)
		return;
	if (!boost_path_allowed(info, BOOST_KIND_SHORT)) {
		stat_inc(STAT_PATH_BOOST_DENIED);
		return;
	}
	now = bpf_ktime_get_ns();
	if (now - info->last_contention_ns > decay_timeout_ns ||
	    boost_longcs_blocked(info, now))
		return;
	grant = short_boost_grant_ns();
	if (!grant || !boost_budget_allowed(info, now, grant))
		return;
	apply_owner_boost_budget(p, grant, short_boost_cap_ns());
	account_successful_boost(info, BOOST_KIND_SHORT, now);
	stat_inc(STAT_BOOST_RUNNING_APPLIED);
}

s32 BPF_STRUCT_OPS(lockcoop_select_cpu, struct task_struct *p,
			   s32 prev_cpu, u64 wake_flags)
{
	struct waiter_info *waiter;
	struct lock_nest_info *nest;
	struct waiter_info copy;
	bool is_idle = false;
	u64 now;
	s32 cpu;
	s32 diag_anchor_cpu = -1;
	u32 diag_fallback_reason = 0;
	u32 tid = p->pid;

	if (is_percpu_kthread(p))
		goto fallback;
	if (fifo_sched)
		return scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	waiter = bpf_map_lookup_elem(&waiters, &tid);
	if (!waiter)
		goto fallback;
	copy = *waiter;
	remove_waiter(tid);
	if (!nest_enabled)
		goto fallback;
	if (copy.anchor_cpu == ANCHOR_CPU_DISABLED)
		goto fallback;
	now = bpf_ktime_get_ns();
	resolve_requeue_destination(&copy, now);
	if (now - copy.observed_ns > nest_expiry_ns) {
		stat_inc(STAT_STALE_NEST);
		goto fallback;
	}
	nest = bpf_map_lookup_elem(&lock_nests, &copy.key);
	if (nest && !anchor_fresh(nest, now)) {
		bpf_map_delete_elem(&lock_nests, &copy.key);
		stat_inc(STAT_STALE_NEST);
		copy.anchor_cpu = -1;
		nest = NULL;
	}
	if (nest)
		copy.anchor_cpu = nest->anchor_cpu;
	if (copy.anchor_cpu < 0 ||
	    !bpf_cpumask_test_cpu(copy.anchor_cpu, p->cpus_ptr)) {
		if (copy.anchor_cpu >= 0) {
			stat_inc(STAT_ANCHOR_CPUMASK_REJECT);
			diag_anchor_cpu = copy.anchor_cpu;
			diag_fallback_reason = DIAG_FALLBACK_CPUMASK;
		}
		goto fallback;
	}
	diag_observe_anchor_candidate(p, copy.anchor_cpu);
	if (!maturity_admitted(nest, now)) {
		stat_inc(STAT_LOCALITY_FALLBACK);
		cpu = dispatch_prev_cpu_if_idle(p, prev_cpu);
		if (cpu >= 0) {
			diag_record_fallback(p, copy.anchor_cpu, cpu,
					     DIAG_FALLBACK_MATURITY);
			return cpu;
		}
		cpu = dispatch_default(p, prev_cpu, wake_flags);
		diag_record_fallback(p, copy.anchor_cpu, cpu,
				     DIAG_FALLBACK_MATURITY);
		return cpu;
	}
	if (!adaptive_nest) {
		if (weak_anchor_queue_limit == ~0U ||
		    scx_bpf_test_and_clear_cpu_idle(copy.anchor_cpu) ||
		    anchor_queue_admitted(copy.anchor_cpu)) {
			stat_inc(STAT_LOCALITY_HIT);
			stat_inc(STAT_PATH_NEST_ACTION);
			diag_record_anchor_direct(p, copy.anchor_cpu);
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | copy.anchor_cpu,
					   SCX_SLICE_DFL, 0);
			return copy.anchor_cpu;
		}
		stat_inc(STAT_LOCALITY_FALLBACK);
		cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
		if (is_idle) {
			stat_inc(STAT_LOCAL);
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, 0);
		}
		diag_record_fallback(p, copy.anchor_cpu, cpu,
				     DIAG_FALLBACK_PRESSURE);
		return cpu;
	}
	if (anchor_in_cooldown(nest, now)) {
		stat_inc(STAT_LOCALITY_FALLBACK);
		stat_inc(STAT_ANCHOR_COOLDOWN_REJECT);
		cpu = dispatch_prev_cpu_if_idle(p, prev_cpu);
		if (cpu >= 0) {
			diag_record_fallback(p, copy.anchor_cpu, cpu,
					     DIAG_FALLBACK_COOLDOWN);
			return cpu;
		}
		diag_anchor_cpu = copy.anchor_cpu;
		diag_fallback_reason = DIAG_FALLBACK_COOLDOWN;
		goto fallback;
	}
	if (effective_anchor_queue_limit() == ~0U ||
	    scx_bpf_test_and_clear_cpu_idle(copy.anchor_cpu) ||
	    adaptive_anchor_queue_admitted(copy.anchor_cpu)) {
		stat_inc(STAT_LOCALITY_HIT);
		stat_inc(STAT_PATH_NEST_ACTION);
		mark_anchor_direct(nest);
		diag_record_anchor_direct(p, copy.anchor_cpu);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | copy.anchor_cpu,
				   SCX_SLICE_DFL, 0);
		return copy.anchor_cpu;
	}
	stat_inc(STAT_LOCALITY_FALLBACK);
	stat_inc(STAT_ANCHOR_PRESSURE_REJECT);
	mark_anchor_pressure(nest, now);
	cpu = dispatch_prev_cpu_if_idle(p, prev_cpu);
	if (cpu >= 0) {
		diag_record_fallback(p, copy.anchor_cpu, cpu,
				     DIAG_FALLBACK_PRESSURE);
		return cpu;
	}
	cpu = dispatch_default(p, prev_cpu, wake_flags);
	diag_record_fallback(p, copy.anchor_cpu, cpu,
			     DIAG_FALLBACK_PRESSURE);
	return cpu;

fallback:
	stat_inc(STAT_LOCALITY_FALLBACK);
	cpu = dispatch_default(p, prev_cpu, wake_flags);
	diag_record_fallback(p, diag_anchor_cpu, cpu, diag_fallback_reason);
	return cpu;
}

void BPF_STRUCT_OPS(lockcoop_enqueue, struct task_struct *p, u64 enq_flags)
{
	struct contention_info *owner_info;
	bool owner_urgent = false;
	u64 now;
	s32 target_cpu;
	u32 tid = p->pid;

	stat_inc(STAT_GLOBAL);
	mark_owner_runnable(p);
	if (is_percpu_kthread(p)) {
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, enq_flags);
		return;
	}
	if (fifo_sched) {
		scx_bpf_dsq_insert(p, SHARED_DSQ, SCX_SLICE_DFL, enq_flags);
	} else {
		u64 vtime = p->scx.dsq_vtime;
		u64 slice = SCX_SLICE_DFL;

		if (time_before(vtime, vtime_now - SCX_SLICE_DFL))
			vtime = vtime_now - SCX_SLICE_DFL;
		if (boost_enabled && boost_profile != BOOST_PROFILE_NONE) {
			owner_info = bpf_map_lookup_elem(&contended_tasks, &tid);
			now = bpf_ktime_get_ns();
			if (owner_info &&
			    owner_info->waiter_count &&
			    owner_info->owner_confidence >= boost_confidence_gate &&
			    boost_path_allowed(owner_info, BOOST_KIND_SHORT) &&
			    now - owner_info->last_contention_ns <= decay_timeout_ns) {
				stat_inc(STAT_URGENT_OWNER_MARK);
				if (!owner_info->last_owner_kick_ns ||
				    now - owner_info->last_owner_kick_ns >= 1000 * 1000) {
					vtime = vtime_now - (SCX_SLICE_DFL << 1);
					owner_info->last_owner_kick_ns = now;
					owner_urgent = true;
				}
			}
		}
		if (owner_urgent && boost_profile == BOOST_PROFILE_SHORT &&
		    owner_info->owner_observed_state == OWNER_STATE_SLEEPING) {
			slice = short_boost_cap_ns();
			owner_info->owner_observed_state = OWNER_STATE_RUNNABLE;
		}
		scx_bpf_dsq_insert_vtime(p, SHARED_DSQ, slice, vtime,
					 enq_flags);
		if (owner_urgent) {
			target_cpu = scx_bpf_task_cpu(p);
			if (target_cpu >= 0)
				scx_bpf_kick_cpu(target_cpu, SCX_KICK_PREEMPT);
			stat_inc(STAT_URGENT_OWNER_DISPATCH);
		}
	}
}

void BPF_STRUCT_OPS(lockcoop_dispatch, s32 cpu, struct task_struct *prev)
{
	struct urgent_owner_slot *slot;
	struct bpf_iter_scx_dsq iterator;
	struct task_struct *owner;
	u64 now;
	u32 key = (u32)cpu;
	u32 tid;
	bool moved;

	if (!boost_enabled) {
		scx_bpf_dsq_move_to_local(SHARED_DSQ);
		return;
	}
	slot = bpf_map_lookup_elem(&urgent_owner_slots, &key);
	if (slot) {
		tid = slot->tid;
		now = bpf_ktime_get_ns();
		if (tid && now - slot->observed_ns <= 1000 * 1000 &&
		    __sync_val_compare_and_swap(&slot->tid, tid, 0) == tid) {
			stat_inc(STAT_URGENT_OWNER_SCAN_TASK);
			owner = bpf_task_from_pid((s32)tid);
			if (owner &&
			    bpf_cpumask_test_cpu(cpu, owner->cpus_ptr)) {
				if (bpf_iter_scx_dsq_new(&iterator, SHARED_DSQ, 0)) {
					bpf_iter_scx_dsq_destroy(&iterator);
				} else {
					__COMPAT_scx_bpf_dsq_move_set_slice(
						&iterator, short_boost_cap_ns());
					moved = __COMPAT_scx_bpf_dsq_move(
						&iterator, owner, SCX_DSQ_LOCAL,
						SCX_ENQ_PREEMPT);
					bpf_iter_scx_dsq_destroy(&iterator);
					if (moved) {
						bpf_task_release(owner);
						stat_inc(STAT_URGENT_OWNER_DISPATCH);
						return;
					}
				}
			}
			if (owner)
				bpf_task_release(owner);
		}
	}
	scx_bpf_dsq_move_to_local(SHARED_DSQ);
}

void BPF_STRUCT_OPS(lockcoop_running, struct task_struct *p)
{
	diag_task_running(p);
	mark_owner_running(p);
	boost_owner_on_run(p);
	if (!fifo_sched && time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;
}

void BPF_STRUCT_OPS(lockcoop_stopping, struct task_struct *p, bool runnable)
{
	u64 slice_used;

	diag_task_stopping(p);
	if (fifo_sched || (!boost_enabled && !sensor_only_mode))
		return;
	slice_used = p->scx.slice > SCX_SLICE_DFL ?
		SCX_SLICE_DFL : SCX_SLICE_DFL - p->scx.slice;
	p->scx.dsq_vtime += slice_used * 100 / p->scx.weight;
}

void BPF_STRUCT_OPS(lockcoop_tick, struct task_struct *p)
{
	struct contention_info *info;
	u64 now;
	u64 grant;
	u64 cap;
	u32 threshold;
	u32 kind;
	u32 tid = p->pid;

	if (fifo_sched || sensor_only_mode)
		return;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (!info)
		return;
	now = bpf_ktime_get_ns();
	if (info->boost_revoke_pending) {
		info->boost_revoke_pending = false;
		info->boosted = false;
		info->boost_start_ns = 0;
		if (!info->waiter_count) {
			bpf_map_delete_elem(&contended_tasks, &tid);
			stat_inc(STAT_DECAY_CLEANUP);
			return;
		}
		if (info->waiter_count)
			return;
	}
	if (!info->waiter_count ||
	    now - info->last_contention_ns > decay_timeout_ns) {
		if (info->boosted && info->last_boost_kind == BOOST_KIND_INF)
			p->scx.slice = 0;
		bpf_map_delete_elem(&contended_tasks, &tid);
		stat_inc(STAT_DECAY_CLEANUP);
		return;
	}
	threshold = adaptive_boost_threshold(info);
	kind = choose_boost_kind(info, threshold);
	if (kind == BOOST_KIND_NONE)
		return;
	if (!boost_path_allowed(info, kind)) {
		stat_inc(STAT_PATH_BOOST_DENIED);
		return;
	}
	if (boost_longcs_blocked(info, now)) {
		p->scx.slice = 0;
		return;
	}
	if (kind == BOOST_KIND_INF) {
		p->scx.slice = SCX_SLICE_INF;
		account_successful_boost(info, kind, now);
		stat_inc(STAT_BOOST_TICK_APPLIED);
		return;
	}
	if (kind == BOOST_KIND_SHORT) {
		grant = short_boost_grant_ns();
		cap = short_boost_cap_ns();
	} else {
		grant = bounded_boost_grant_ns();
		if (kind == BOOST_KIND_ADAPTIVE && grant &&
		    grant < (bounded_boost_cap_ns(grant) >> 1))
			grant <<= 1;
		cap = bounded_boost_cap_ns(grant);
	}
	if (!grant)
		return;
	if (p->scx.slice >= cap)
		return;
	if (!boost_budget_allowed(info, now, grant))
		return;
	apply_owner_boost_budget(p, grant, cap);
	account_successful_boost(info, kind, now);
	stat_inc(STAT_BOOST_TICK_APPLIED);
}

void BPF_STRUCT_OPS(lockcoop_enable, struct task_struct *p)
{
	p->scx.dsq_vtime = vtime_now;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(lockcoop_init)
{
	return scx_bpf_create_dsq(SHARED_DSQ, -1);
}

void BPF_STRUCT_OPS(lockcoop_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(LOCKCOOP_OPS,
	       .select_cpu = (void *)lockcoop_select_cpu,
	       .enqueue = (void *)lockcoop_enqueue,
	       .dispatch = (void *)lockcoop_dispatch,
	       .running = (void *)lockcoop_running,
	       .stopping = (void *)lockcoop_stopping,
	       .tick = (void *)lockcoop_tick,
	       .enable = (void *)lockcoop_enable,
	       .init = (void *)lockcoop_init,
	       .exit = (void *)lockcoop_exit,
	       .name = LOCKCOOP_SCHED_NAME);
