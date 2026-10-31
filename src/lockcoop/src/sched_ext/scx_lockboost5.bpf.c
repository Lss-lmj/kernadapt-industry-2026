/* SPDX-License-Identifier: GPL-2.0 */
/*
 * scx_lockboost5: owner-progress scheduling with owner-resolved futex
 * evidence, separated owner-futex slice/INF thresholds, and wait-time
 * pressure for tail-latency driven boosts.
 *
 * Kernel mutex slowpath events and owner-resolved futex waits create strong
 * owner pressure.  Unknown raw futex wait/wake events are retained only as an
 * optional weak fallback.
 */
#include <scx/common.bpf.h>
#include <bpf/bpf_core_read.h>

char _license[] SEC("license") = "GPL";

#define SHARED_DSQ 0

#define FUTEX_WAIT 0
#define FUTEX_WAKE 1
#define FUTEX_LOCK_PI 6
#define FUTEX_UNLOCK_PI 7
#define FUTEX_TRYLOCK_PI 8
#define FUTEX_LOCK_PI2 13

#define FUTEX_WAITERS 0x80000000U
#define FUTEX_OWNER_DIED 0x40000000U
#define FUTEX_TID_MASK 0x3fffffffU

#define OWNER_MUTEX 1
#define OWNER_FUTEX_RAW 2
#define OWNER_FUTEX_CONFIRMED 3
#define OWNER_PTHREAD_MUTEX 4
#define OWNER_PI_FUTEX 5

#define FUTEX_BOOST_OFF 0
#define FUTEX_BOOST_WEAK 1
#define FUTEX_BOOST_EQUAL 2
#define FUTEX_BOOST_OWNER 3
#define FUTEX_BOOST_OWNER_WEAK 4

enum stat_idx {
	STAT_LOCAL = 0,
	STAT_GLOBAL = 1,
	STAT_MUTEX_WAIT = 2,
	STAT_MUTEX_OWNER_HIT = 3,
	STAT_MUTEX_OWNER_MISS = 4,
	STAT_FUTEX_WAIT = 5,
	STAT_FUTEX_WAKE = 6,
	STAT_FUTEX_CANDIDATE = 7,
	STAT_FUTEX_CONFIRMED = 8,
	STAT_FUTEX_MISMATCH = 9,
	STAT_FUTEX_SKIP_NO_CONF = 10,
	STAT_MUTEX_PRESSURE = 11,
	STAT_FUTEX_PRESSURE = 12,
	STAT_STRONG_BOOST = 13,
	STAT_WEAK_BOOST = 14,
	STAT_EQUAL_BOOST = 15,
	STAT_WAKE_DEC_MUTEX = 16,
	STAT_WAKE_DEC_FUTEX = 17,
	STAT_OWNER_FUTEX_PRESSURE = 18,
	STAT_OWNER_FUTEX_BOOST = 19,
	STAT_WAKE_DEC_OWNER_FUTEX = 20,
	STAT_PTHREAD_LOCK_ENTER = 21,
	STAT_PTHREAD_LOCK_SUCCESS = 22,
	STAT_PTHREAD_UNLOCK = 23,
	STAT_OWNER_MAP_HIT = 24,
	STAT_OWNER_FIELD_HIT = 25,
	STAT_OWNER_FIELD_ZERO = 26,
	STAT_OWNER_INVALID_MISSING_TASK = 27,
	STAT_OWNER_INVALID_TGID = 28,
	STAT_OWNER_SELF_SKIP = 29,
	STAT_PI_OWNER_HIT = 30,
	STAT_PI_OWNER_MISS = 31,
	STAT_FUTEX_UNKNOWN_IGNORED = 32,
	STAT_OWNER_FUTEX_BELOW_THRESHOLD = 33,
	STAT_OWNER_FUTEX_SLICE_BOOST = 34,
	STAT_OWNER_FUTEX_INF_BOOST = 35,
	STAT_OWNER_TIME_ELIGIBLE = 36,
	STAT_OWNER_TIME_SLICE_BOOST = 37,
	STAT_OWNER_TIME_INF_BOOST = 38,
	STAT_OWNER_TIME_HARD_INF = 39,
	STAT_OWNER_TIME_BELOW_THRESHOLD = 40,
	STAT_OWNER_TIME_PRESSURE_SUM = 41,
	STAT_OWNER_TIME_AGE_MS_SUM = 42,
	STAT_MAX = 43,
};

const volatile u32 high_lockboost_threshold = 4;
const volatile u32 futex_waiter_threshold = 4;
const volatile u32 futex_confirm_threshold = 2;
const volatile u32 owner_futex_waiter_threshold = 4;
const volatile u32 owner_futex_inf_threshold = 8;
const volatile u32 futex_boost_policy = FUTEX_BOOST_OWNER_WEAK;
const volatile u32 owner_time_threshold_ms = 5;
const volatile u32 owner_time_age_cap_ms = 50;
const volatile u32 owner_time_slice_pressure = 16;
const volatile u32 owner_time_inf_pressure = 64;
const volatile u32 owner_time_hard_ms = 0;
const volatile u64 decay_timeout_ns = 200 * 1000 * 1000; /* 200 ms */
const volatile u64 futex_state_timeout_ns = 100 * 1000 * 1000; /* 100 ms */
const volatile s32 pthread_owner_offset = 8;
const volatile bool fifo_sched;
const volatile bool enable_pthread_owner = true;

struct owner_info {
	u64 tgid_tid;
	u32 tid;
	u32 source;
	u64 ts;
};

struct waiter_event {
	u64 lock_addr;
	u32 owner_tid;
	u32 source;
	u64 observed_ts;
};

struct boost_info {
	u32 mutex_waiters;
	u32 futex_waiters;
	u32 owner_futex_waiters;
	u32 futex_confidence;
	u64 last_mutex_ts;
	u64 last_futex_ts;
	u64 last_owner_futex_ts;
	u64 first_owner_futex_ts;
	bool strong_boosted;
	bool weak_boosted;
	bool owner_boosted;
	bool owner_time_boosted;
	bool owner_time_inf_boosted;
};

struct futex_state {
	u32 predicted_owner_tid;
	u32 last_waker_tid;
	u32 waiter_count;
	u32 confidence;
	u64 last_wait_ts;
	u64 last_wake_ts;
};

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u64);
	__type(value, struct owner_info);
	__uint(max_entries, 10240);
} lock_owners SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct waiter_event);
	__uint(max_entries, 10240);
} waiters SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, struct boost_info);
	__uint(max_entries, 10240);
} contended_tasks SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, u64);
	__type(value, struct futex_state);
	__uint(max_entries, 32768);
} futex_states SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, u64);
	__uint(max_entries, 10240);
} active_mutex_lock SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, u32);
	__type(value, u64);
	__uint(max_entries, 65536);
} active_pthread_lock SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__type(key, u32);
	__type(value, u32);
	__uint(max_entries, 262144);
} task_tgids SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(u32));
	__uint(value_size, sizeof(u64));
	__uint(max_entries, STAT_MAX);
} stats SEC(".maps");

static u64 vtime_now;
UEI_DEFINE(uei);

static __always_inline void stat_inc(u32 idx)
{
	u64 *cnt_p;

	if (idx >= STAT_MAX)
		return;
	cnt_p = bpf_map_lookup_elem(&stats, &idx);
	if (cnt_p)
		(*cnt_p)++;
}

static __always_inline void stat_add(u32 idx, u64 delta)
{
	u64 *cnt_p;

	if (idx >= STAT_MAX)
		return;
	cnt_p = bpf_map_lookup_elem(&stats, &idx);
	if (cnt_p)
		(*cnt_p) += delta;
}

static __always_inline u32 current_tid(void)
{
	return (u32)bpf_get_current_pid_tgid();
}

static __always_inline u32 current_tgid(void)
{
	return (u32)(bpf_get_current_pid_tgid() >> 32);
}

static __always_inline void record_current_task(void)
{
	u64 pid_tgid = bpf_get_current_pid_tgid();
	u32 tid = (u32)pid_tgid;
	u32 tgid = (u32)(pid_tgid >> 32);

	if (tid)
		bpf_map_update_elem(&task_tgids, &tid, &tgid, BPF_ANY);
}

static __always_inline void record_task(struct task_struct *p)
{
	u32 tid;
	u32 tgid;

	if (!p)
		return;
	tid = BPF_CORE_READ(p, pid);
	tgid = BPF_CORE_READ(p, tgid);
	if (tid)
		bpf_map_update_elem(&task_tgids, &tid, &tgid, BPF_ANY);
}

static __always_inline u32 validate_owner_tid(u32 owner_tid, u32 waiter_tid,
					      u32 waiter_tgid)
{
	u32 *owner_tgid;

	if (!owner_tid)
		return 0;
	if (owner_tid == waiter_tid) {
		stat_inc(STAT_OWNER_SELF_SKIP);
		return 0;
	}
	owner_tgid = bpf_map_lookup_elem(&task_tgids, &owner_tid);
	if (!owner_tgid) {
		stat_inc(STAT_OWNER_INVALID_MISSING_TASK);
		return 0;
	}
	if (*owner_tgid != waiter_tgid) {
		stat_inc(STAT_OWNER_INVALID_TGID);
		return 0;
	}
	return owner_tid;
}

static __always_inline u32 read_user_u32(u64 addr)
{
	u32 value = 0;

	bpf_probe_read_user(&value, sizeof(value), (void *)addr);
	return value;
}

static __always_inline bool futex_owner_policy_enabled(void)
{
	return futex_boost_policy == FUTEX_BOOST_OWNER ||
	       futex_boost_policy == FUTEX_BOOST_OWNER_WEAK;
}

static __always_inline bool futex_weak_policy_enabled(void)
{
	return futex_boost_policy == FUTEX_BOOST_WEAK ||
	       futex_boost_policy == FUTEX_BOOST_EQUAL ||
	       futex_boost_policy == FUTEX_BOOST_OWNER_WEAK;
}

static __always_inline void reset_futex_state_if_stale(struct futex_state *st,
						       u64 now)
{
	u64 last = st->last_wait_ts;

	if (st->last_wake_ts > last)
		last = st->last_wake_ts;
	if (last && now - last <= futex_state_timeout_ns)
		return;

	st->predicted_owner_tid = 0;
	st->last_waker_tid = 0;
	st->waiter_count = 0;
	st->confidence = 0;
	st->last_wait_ts = now;
	st->last_wake_ts = 0;
}

static __always_inline void record_owner(u64 lock_addr, u32 source)
{
	u64 tgid_tid = bpf_get_current_pid_tgid();
	struct owner_info owner = {
		.tgid_tid = tgid_tid,
		.tid = (u32)tgid_tid,
		.source = source,
		.ts = bpf_ktime_get_ns(),
	};

	bpf_map_update_elem(&lock_owners, &lock_addr, &owner, BPF_ANY);
}

static __always_inline void add_mutex_pressure(u32 owner_tid, u64 now)
{
	struct boost_info *info;
	struct boost_info new_info = {};

	info = bpf_map_lookup_elem(&contended_tasks, &owner_tid);
	if (info) {
		info->mutex_waiters++;
		info->last_mutex_ts = now;
	} else {
		new_info.mutex_waiters = 1;
		new_info.last_mutex_ts = now;
		bpf_map_update_elem(&contended_tasks, &owner_tid, &new_info,
				    BPF_ANY);
	}
	stat_inc(STAT_MUTEX_PRESSURE);
}

static __always_inline void add_futex_pressure(u32 owner_tid, u32 confidence,
					       u64 now)
{
	struct boost_info *info;
	struct boost_info new_info = {};

	info = bpf_map_lookup_elem(&contended_tasks, &owner_tid);
	if (info) {
		info->futex_waiters++;
		info->last_futex_ts = now;
		if (confidence > info->futex_confidence)
			info->futex_confidence = confidence;
	} else {
		new_info.futex_waiters = 1;
		new_info.futex_confidence = confidence;
		new_info.last_futex_ts = now;
		bpf_map_update_elem(&contended_tasks, &owner_tid, &new_info,
				    BPF_ANY);
	}
	stat_inc(STAT_FUTEX_PRESSURE);
}

static __always_inline void add_owner_futex_pressure(u32 owner_tid, u64 now)
{
	struct boost_info *info;
	struct boost_info new_info = {};

	info = bpf_map_lookup_elem(&contended_tasks, &owner_tid);
	if (info) {
		if (!info->owner_futex_waiters || !info->first_owner_futex_ts)
			info->first_owner_futex_ts = now;
		info->owner_futex_waiters++;
		info->last_owner_futex_ts = now;
	} else {
		new_info.owner_futex_waiters = 1;
		new_info.last_owner_futex_ts = now;
		new_info.first_owner_futex_ts = now;
		bpf_map_update_elem(&contended_tasks, &owner_tid, &new_info,
				    BPF_ANY);
	}
	stat_inc(STAT_OWNER_FUTEX_PRESSURE);
}

static __always_inline void record_waiter(u32 waiter_tid, u64 lock_addr,
					  u32 owner_tid, u32 source, u64 now)
{
	struct waiter_event event = {
		.lock_addr = lock_addr,
		.owner_tid = owner_tid,
		.source = source,
		.observed_ts = now,
	};

	bpf_map_update_elem(&waiters, &waiter_tid, &event, BPF_ANY);
}

static __always_inline void mark_mutex_waiter(u64 lock_addr, u32 waiter_tid)
{
	struct owner_info *owner;
	u64 now = bpf_ktime_get_ns();

	stat_inc(STAT_MUTEX_WAIT);
	owner = bpf_map_lookup_elem(&lock_owners, &lock_addr);
	if (!owner || owner->tid == waiter_tid) {
		stat_inc(STAT_MUTEX_OWNER_MISS);
		return;
	}

	record_waiter(waiter_tid, lock_addr, owner->tid, OWNER_MUTEX, now);
	add_mutex_pressure(owner->tid, now);
	stat_inc(STAT_MUTEX_OWNER_HIT);
}

static __always_inline u32 resolve_pthread_owner(u64 uaddr, u32 waiter_tid,
						 u32 waiter_tgid, u32 *source)
{
	struct owner_info *owner;
	u32 owner_tid;

	if (!enable_pthread_owner)
		return 0;

	owner = bpf_map_lookup_elem(&lock_owners, &uaddr);
	if (owner && owner->source == OWNER_PTHREAD_MUTEX) {
		owner_tid = validate_owner_tid(owner->tid, waiter_tid,
					       waiter_tgid);
		if (owner_tid) {
			*source = OWNER_PTHREAD_MUTEX;
			stat_inc(STAT_OWNER_MAP_HIT);
			return owner_tid;
		}
	}

	if (pthread_owner_offset < 0)
		return 0;

	owner_tid = read_user_u32(uaddr + (u64)pthread_owner_offset);
	if (!owner_tid) {
		stat_inc(STAT_OWNER_FIELD_ZERO);
		return 0;
	}

	owner_tid = validate_owner_tid(owner_tid, waiter_tid, waiter_tgid);
	if (!owner_tid)
		return 0;

	*source = OWNER_PTHREAD_MUTEX;
	stat_inc(STAT_OWNER_FIELD_HIT);
	return owner_tid;
}

static __always_inline u32 resolve_pi_futex_owner(u64 uaddr, u32 waiter_tid,
						  u32 waiter_tgid, u32 *source)
{
	u32 word;
	u32 owner_tid;

	word = read_user_u32(uaddr);
	owner_tid = word & FUTEX_TID_MASK;
	if (!owner_tid) {
		stat_inc(STAT_PI_OWNER_MISS);
		return 0;
	}

	owner_tid = validate_owner_tid(owner_tid, waiter_tid, waiter_tgid);
	if (!owner_tid)
		return 0;

	*source = OWNER_PI_FUTEX;
	stat_inc(STAT_PI_OWNER_HIT);
	return owner_tid;
}

static __always_inline void handle_futex_wait(u64 uaddr, u32 waiter_tid)
{
	struct futex_state *st;
	struct futex_state new_st = {};
	u64 now = bpf_ktime_get_ns();
	u32 owner_tid = 0;
	u32 source = OWNER_FUTEX_RAW;
	u32 waiter_tgid = current_tgid();

	stat_inc(STAT_FUTEX_WAIT);
	record_current_task();

	if (futex_owner_policy_enabled()) {
		owner_tid = resolve_pthread_owner(uaddr, waiter_tid,
						  waiter_tgid, &source);
		if (owner_tid) {
			add_owner_futex_pressure(owner_tid, now);
			record_waiter(waiter_tid, uaddr, owner_tid, source, now);
			return;
		}
	}

	if (!futex_weak_policy_enabled()) {
		stat_inc(STAT_FUTEX_UNKNOWN_IGNORED);
		record_waiter(waiter_tid, uaddr, 0, OWNER_FUTEX_RAW, now);
		return;
	}

	st = bpf_map_lookup_elem(&futex_states, &uaddr);
	if (st) {
		reset_futex_state_if_stale(st, now);
		st->waiter_count++;
		st->last_wait_ts = now;
		owner_tid = st->predicted_owner_tid;
		if (owner_tid && owner_tid != waiter_tid) {
			if (st->confidence >= futex_confirm_threshold) {
				source = OWNER_FUTEX_CONFIRMED;
				if (futex_boost_policy != FUTEX_BOOST_OFF)
					add_futex_pressure(owner_tid,
							   st->confidence,
							   now);
			} else {
				stat_inc(STAT_FUTEX_SKIP_NO_CONF);
			}
		}
	} else {
		new_st.waiter_count = 1;
		new_st.last_wait_ts = now;
		bpf_map_update_elem(&futex_states, &uaddr, &new_st, BPF_ANY);
	}

	record_waiter(waiter_tid, uaddr, owner_tid, source, now);
}

static __always_inline void handle_futex_wake(u64 uaddr, u32 waker_tid)
{
	struct futex_state *st;
	struct futex_state new_st = {};
	u64 now = bpf_ktime_get_ns();

	stat_inc(STAT_FUTEX_WAKE);

	st = bpf_map_lookup_elem(&futex_states, &uaddr);
	if (st) {
		reset_futex_state_if_stale(st, now);
		if (st->predicted_owner_tid == waker_tid) {
			if (st->confidence < 32)
				st->confidence++;
			stat_inc(STAT_FUTEX_CONFIRMED);
		} else if (st->predicted_owner_tid) {
			if (st->confidence > 0)
				st->confidence--;
			stat_inc(STAT_FUTEX_MISMATCH);
		}
		st->last_waker_tid = waker_tid;
		st->last_wake_ts = now;
	} else {
		new_st.last_waker_tid = waker_tid;
		new_st.last_wake_ts = now;
		bpf_map_update_elem(&futex_states, &uaddr, &new_st, BPF_ANY);
	}
}

SEC("kprobe/mutex_lock")
int BPF_KPROBE(handle_mutex_lock_enter, struct mutex *lock)
{
	u32 tid = current_tid();
	u64 lock_addr = (u64)lock;

	bpf_map_update_elem(&active_mutex_lock, &tid, &lock_addr, BPF_ANY);
	return 0;
}

SEC("kretprobe/mutex_lock")
int BPF_KRETPROBE(handle_mutex_lock_exit, int ret)
{
	u32 tid = current_tid();
	u64 *lock_addr_ptr;

	lock_addr_ptr = bpf_map_lookup_elem(&active_mutex_lock, &tid);
	if (!lock_addr_ptr)
		return 0;
	if (ret == 0)
		record_owner(*lock_addr_ptr, OWNER_MUTEX);
	bpf_map_delete_elem(&active_mutex_lock, &tid);
	return 0;
}

SEC("kprobe/mutex_unlock")
int BPF_KPROBE(handle_mutex_unlock, struct mutex *lock)
{
	u64 lock_addr = (u64)lock;

	bpf_map_delete_elem(&lock_owners, &lock_addr);
	return 0;
}

SEC("kprobe/__mutex_lock_slowpath")
int BPF_KPROBE(handle_mutex_contended, struct mutex *lock)
{
	u64 lock_addr = (u64)lock;
	u32 waiter_tid = current_tid();

	mark_mutex_waiter(lock_addr, waiter_tid);
	return 0;
}

SEC("uprobe")
int handle_pthread_mutex_lock_enter(struct pt_regs *ctx)
{
	u32 tid = current_tid();
	u64 lock_addr = PT_REGS_PARM1(ctx);

	record_current_task();
	if (!lock_addr)
		return 0;
	stat_inc(STAT_PTHREAD_LOCK_ENTER);
	bpf_map_update_elem(&active_pthread_lock, &tid, &lock_addr, BPF_ANY);
	return 0;
}

SEC("uretprobe")
int handle_pthread_mutex_lock_return(struct pt_regs *ctx)
{
	u32 tid = current_tid();
	u64 *lock_addr;
	long ret = PT_REGS_RC(ctx);

	record_current_task();
	lock_addr = bpf_map_lookup_elem(&active_pthread_lock, &tid);
	if (!lock_addr)
		return 0;

	if (ret == 0) {
		struct owner_info owner = {
			.tgid_tid = bpf_get_current_pid_tgid(),
			.tid = tid,
			.source = OWNER_PTHREAD_MUTEX,
			.ts = bpf_ktime_get_ns(),
		};

		bpf_map_update_elem(&lock_owners, lock_addr, &owner, BPF_ANY);
		stat_inc(STAT_PTHREAD_LOCK_SUCCESS);
	}
	bpf_map_delete_elem(&active_pthread_lock, &tid);
	return 0;
}

SEC("uprobe")
int handle_pthread_mutex_unlock_enter(struct pt_regs *ctx)
{
	u64 lock_addr = PT_REGS_PARM1(ctx);

	record_current_task();
	if (!lock_addr)
		return 0;
	bpf_map_delete_elem(&lock_owners, &lock_addr);
	stat_inc(STAT_PTHREAD_UNLOCK);
	return 0;
}

SEC("tp/syscalls/sys_enter_futex")
int handle_futex_enter(struct trace_event_raw_sys_enter *ctx)
{
	u64 uaddr = (u64)ctx->args[0];
	int op = (int)ctx->args[1] & 127;
	u32 tid = current_tid();
	u32 source = OWNER_PI_FUTEX;
	u32 owner_tid;
	u64 now;

	if (op == FUTEX_WAIT)
		handle_futex_wait(uaddr, tid);
	else if (op == FUTEX_WAKE) {
		record_current_task();
		handle_futex_wake(uaddr, tid);
	} else if ((op == FUTEX_LOCK_PI || op == FUTEX_LOCK_PI2) &&
		   futex_owner_policy_enabled()) {
		record_current_task();
		now = bpf_ktime_get_ns();
		stat_inc(STAT_FUTEX_WAIT);
		owner_tid = resolve_pi_futex_owner(uaddr, tid, current_tgid(),
						   &source);
		if (owner_tid) {
			add_owner_futex_pressure(owner_tid, now);
			record_waiter(tid, uaddr, owner_tid, source, now);
		}
	}
	return 0;
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
	u32 woken_tid = ctx->pid;
	struct waiter_event *event;
	struct boost_info *info;
	struct futex_state *st;

	event = bpf_map_lookup_elem(&waiters, &woken_tid);
	if (!event)
		return 0;

	if (event->source == OWNER_MUTEX && event->owner_tid) {
		info = bpf_map_lookup_elem(&contended_tasks, &event->owner_tid);
		if (info && info->mutex_waiters > 0)
			info->mutex_waiters--;
		stat_inc(STAT_WAKE_DEC_MUTEX);
		} else if ((event->source == OWNER_PTHREAD_MUTEX ||
			    event->source == OWNER_PI_FUTEX) &&
			   event->owner_tid) {
			info = bpf_map_lookup_elem(&contended_tasks, &event->owner_tid);
			if (info && info->owner_futex_waiters > 0) {
				info->owner_futex_waiters--;
				if (!info->owner_futex_waiters) {
					info->first_owner_futex_ts = 0;
					info->owner_time_boosted = false;
					info->owner_time_inf_boosted = false;
				}
			}
			stat_inc(STAT_WAKE_DEC_OWNER_FUTEX);
		} else if (event->source == OWNER_FUTEX_CONFIRMED && event->owner_tid) {
		info = bpf_map_lookup_elem(&contended_tasks, &event->owner_tid);
		if (info && info->futex_waiters > 0)
			info->futex_waiters--;
		stat_inc(STAT_WAKE_DEC_FUTEX);
	}

	if (event->source == OWNER_FUTEX_RAW ||
	    event->source == OWNER_FUTEX_CONFIRMED) {
		st = bpf_map_lookup_elem(&futex_states, &event->lock_addr);
		if (st) {
			if (st->waiter_count > 0)
				st->waiter_count--;
			st->predicted_owner_tid = woken_tid;
			if (st->confidence == 0)
				st->confidence = 1;
			stat_inc(STAT_FUTEX_CANDIDATE);
		}
	}

	bpf_map_delete_elem(&waiters, &woken_tid);
	return 0;
}

s32 BPF_STRUCT_OPS(lockboost5_select_cpu, struct task_struct *p, s32 prev_cpu,
		   u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu;

	record_task(p);
	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle) {
		stat_inc(STAT_LOCAL);
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, SCX_SLICE_DFL, 0);
	}
	return cpu;
}

void BPF_STRUCT_OPS(lockboost5_enqueue, struct task_struct *p, u64 enq_flags)
{
	stat_inc(STAT_GLOBAL);
	record_task(p);
	if (fifo_sched) {
		scx_bpf_dsq_insert(p, SHARED_DSQ, SCX_SLICE_DFL, enq_flags);
	} else {
		u64 vtime = p->scx.dsq_vtime;

		if (time_before(vtime, vtime_now - SCX_SLICE_DFL))
			vtime = vtime_now - SCX_SLICE_DFL;
		scx_bpf_dsq_insert_vtime(p, SHARED_DSQ, SCX_SLICE_DFL, vtime,
					 enq_flags);
	}
}

void BPF_STRUCT_OPS(lockboost5_dispatch, s32 cpu, struct task_struct *prev)
{
	scx_bpf_dsq_move_to_local(SHARED_DSQ);
}

void BPF_STRUCT_OPS(lockboost5_running, struct task_struct *p)
{
	record_task(p);
	if (fifo_sched)
		return;
	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;
}

void BPF_STRUCT_OPS(lockboost5_stopping, struct task_struct *p, bool runnable)
{
	struct boost_info *info;
	u64 slice_used;
	u32 tid;

	if (fifo_sched)
		return;

	record_task(p);
	tid = p->pid;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (info && (info->strong_boosted || info->weak_boosted ||
		     info->owner_boosted || info->owner_time_boosted ||
		     info->owner_time_inf_boosted)) {
		info->strong_boosted = false;
		info->weak_boosted = false;
		info->owner_boosted = false;
		info->owner_time_boosted = false;
		info->owner_time_inf_boosted = false;
		if (!runnable)
			bpf_map_delete_elem(&contended_tasks, &tid);
	}

	slice_used = SCX_SLICE_DFL - p->scx.slice;
	if (p->scx.slice > SCX_SLICE_DFL)
		slice_used = SCX_SLICE_DFL;
	p->scx.dsq_vtime += slice_used * 100 / p->scx.weight;
}

static __always_inline u32 owner_time_pressure(struct boost_info *info, u64 now,
					       u32 *age_ms_out)
{
	u64 age_ns;
	u32 age_ms;
	u32 over_ms;
	u32 cap_ms;

	*age_ms_out = 0;
	if (!owner_time_threshold_ms || !info->owner_futex_waiters ||
	    !info->first_owner_futex_ts)
		return 0;

	age_ns = now - info->first_owner_futex_ts;
	age_ms = age_ns / 1000000;
	*age_ms_out = age_ms;
	if (age_ms < owner_time_threshold_ms)
		return 0;

	over_ms = age_ms - owner_time_threshold_ms;
	cap_ms = owner_time_age_cap_ms;
	if (cap_ms && over_ms > cap_ms)
		over_ms = cap_ms;

	return info->owner_futex_waiters * over_ms;
}

static __always_inline bool maybe_owner_time_inf(struct task_struct *p,
						 struct boost_info *info,
						 u64 now)
{
	u32 age_ms = 0;
	u32 pressure;

	pressure = owner_time_pressure(info, now, &age_ms);
	if (!pressure) {
		if (age_ms)
			stat_inc(STAT_OWNER_TIME_BELOW_THRESHOLD);
		return false;
	}

	stat_inc(STAT_OWNER_TIME_ELIGIBLE);
	stat_add(STAT_OWNER_TIME_PRESSURE_SUM, pressure);
	stat_add(STAT_OWNER_TIME_AGE_MS_SUM, age_ms);

	if (owner_time_hard_ms && age_ms >= owner_time_hard_ms) {
		p->scx.slice = SCX_SLICE_INF;
		info->owner_time_inf_boosted = true;
		stat_inc(STAT_OWNER_TIME_HARD_INF);
		stat_inc(STAT_OWNER_TIME_INF_BOOST);
		return true;
	}

	if (owner_time_inf_pressure &&
	    pressure >= owner_time_inf_pressure) {
		p->scx.slice = SCX_SLICE_INF;
		info->owner_time_inf_boosted = true;
		stat_inc(STAT_OWNER_TIME_INF_BOOST);
		return true;
	}

	return false;
}

static __always_inline bool maybe_owner_time_slice(struct task_struct *p,
						   struct boost_info *info,
						   u64 now)
{
	u32 age_ms = 0;
	u32 pressure;

	if (info->owner_time_boosted)
		return false;

	pressure = owner_time_pressure(info, now, &age_ms);
	if (!pressure) {
		if (age_ms)
			stat_inc(STAT_OWNER_TIME_BELOW_THRESHOLD);
		return false;
	}

	stat_inc(STAT_OWNER_TIME_ELIGIBLE);
	stat_add(STAT_OWNER_TIME_PRESSURE_SUM, pressure);
	stat_add(STAT_OWNER_TIME_AGE_MS_SUM, age_ms);

	if (owner_time_slice_pressure &&
	    pressure >= owner_time_slice_pressure) {
		p->scx.slice += SCX_SLICE_DFL;
		info->owner_time_boosted = true;
		stat_inc(STAT_OWNER_TIME_SLICE_BOOST);
		return true;
	}

	stat_inc(STAT_OWNER_TIME_BELOW_THRESHOLD);
	return false;
}

void BPF_STRUCT_OPS(lockboost5_tick, struct task_struct *p)
{
	struct boost_info *info;
	u64 now;
	u32 tid;

	if (fifo_sched)
		return;

	tid = p->pid;
	info = bpf_map_lookup_elem(&contended_tasks, &tid);
	if (!info)
		return;

	now = bpf_ktime_get_ns();
	if (info->mutex_waiters &&
	    now - info->last_mutex_ts > decay_timeout_ns)
		info->mutex_waiters = 0;
	if (info->futex_waiters &&
	    now - info->last_futex_ts > futex_state_timeout_ns)
		info->futex_waiters = 0;
	if (info->owner_futex_waiters &&
	    now - info->last_owner_futex_ts > futex_state_timeout_ns)
		info->owner_futex_waiters = 0;
	if (!info->owner_futex_waiters) {
		info->first_owner_futex_ts = 0;
		info->owner_time_boosted = false;
		info->owner_time_inf_boosted = false;
	}

	if (!info->mutex_waiters && !info->futex_waiters &&
	    !info->owner_futex_waiters) {
		if (info->strong_boosted || info->weak_boosted ||
		    info->owner_boosted || info->owner_time_boosted ||
		    info->owner_time_inf_boosted) {
			info->strong_boosted = false;
			info->weak_boosted = false;
			info->owner_boosted = false;
			info->owner_time_boosted = false;
			info->owner_time_inf_boosted = false;
			p->scx.slice = 0;
		}
		bpf_map_delete_elem(&contended_tasks, &tid);
		return;
	}

	if (info->mutex_waiters >= high_lockboost_threshold) {
		p->scx.slice = SCX_SLICE_INF;
		info->strong_boosted = true;
		stat_inc(STAT_STRONG_BOOST);
		return;
	}
	if (info->mutex_waiters > 0) {
		if (!info->strong_boosted) {
			p->scx.slice += SCX_SLICE_DFL;
			info->strong_boosted = true;
			stat_inc(STAT_STRONG_BOOST);
		}
		return;
	}

	if (info->owner_futex_waiters &&
	    maybe_owner_time_inf(p, info, now))
		return;

	if (info->owner_futex_waiters >= owner_futex_waiter_threshold) {
		if (owner_futex_inf_threshold &&
		    info->owner_futex_waiters >= owner_futex_inf_threshold) {
			p->scx.slice = SCX_SLICE_INF;
			stat_inc(STAT_OWNER_FUTEX_INF_BOOST);
		} else if (!info->owner_boosted) {
			p->scx.slice += SCX_SLICE_DFL;
			stat_inc(STAT_OWNER_FUTEX_SLICE_BOOST);
		}
		info->owner_boosted = true;
		stat_inc(STAT_OWNER_FUTEX_BOOST);
		return;
	} else if (info->owner_futex_waiters > 0) {
		stat_inc(STAT_OWNER_FUTEX_BELOW_THRESHOLD);
	}

	if (info->owner_futex_waiters &&
	    maybe_owner_time_slice(p, info, now))
		return;

	if (futex_boost_policy == FUTEX_BOOST_EQUAL &&
	    info->futex_waiters > 0 &&
	    info->futex_confidence >= futex_confirm_threshold) {
		if (info->futex_waiters >= high_lockboost_threshold)
			p->scx.slice = SCX_SLICE_INF;
		else if (!info->strong_boosted)
			p->scx.slice += SCX_SLICE_DFL;
		info->strong_boosted = true;
		stat_inc(STAT_EQUAL_BOOST);
		return;
	}

	if (futex_boost_policy == FUTEX_BOOST_WEAK &&
	    info->futex_waiters >= futex_waiter_threshold &&
	    info->futex_confidence >= futex_confirm_threshold) {
		if (!info->weak_boosted) {
			p->scx.slice += SCX_SLICE_DFL;
			info->weak_boosted = true;
			stat_inc(STAT_WEAK_BOOST);
		}
	}
}

void BPF_STRUCT_OPS(lockboost5_enable, struct task_struct *p)
{
	record_task(p);
	p->scx.dsq_vtime = vtime_now;
}

SEC("tp/sched/sched_process_exit")
int handle_sched_process_exit(void *ctx)
{
	u32 tid = current_tid();

	bpf_map_delete_elem(&task_tgids, &tid);
	return 0;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(lockboost5_init)
{
	return scx_bpf_create_dsq(SHARED_DSQ, -1);
}

void BPF_STRUCT_OPS(lockboost5_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(lockboost5_ops,
	.select_cpu	= (void *)lockboost5_select_cpu,
	.enqueue	= (void *)lockboost5_enqueue,
	.dispatch	= (void *)lockboost5_dispatch,
	.running	= (void *)lockboost5_running,
	.stopping	= (void *)lockboost5_stopping,
	.tick		= (void *)lockboost5_tick,
	.enable		= (void *)lockboost5_enable,
	.init		= (void *)lockboost5_init,
	.exit		= (void *)lockboost5_exit,
	.name		= "lockboost5"
);
