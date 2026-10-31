// SPDX-License-Identifier: GPL-2.0
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include "common.h"

char LICENSE[] SEC("license") = "GPL";

const volatile uint32_t target_tgid = 0;
const volatile uint64_t min_wait_ns = 1000ULL;
const volatile int32_t pthread_owner_offset = 8;

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint32_t);
	__type(value, struct active_lock_arg);
	__uint(max_entries, 131072);
} active_lock_args SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint64_t);
	__type(value, struct lock_owner);
	__uint(max_entries, 262144);
} lock_owners SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint64_t);
	__type(value, struct lock_episode);
	__uint(max_entries, 262144);
} lock_episodes SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint32_t);
	__type(value, struct owner_pending);
	__uint(max_entries, 131072);
} pending_owners SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint32_t);
	__type(value, struct contended_owner);
	__uint(max_entries, 131072);
} contended_owners SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint32_t);
	__type(value, uint32_t);
	__uint(max_entries, 262144);
} running_tids SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_CNT_NR);
} counters SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_HIST_BUCKETS);
} owner_next_run_delay_hist SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_HIST_BUCKETS);
} owner_unlock_delay_hist SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_HIST_BUCKETS);
} owner_offcpu_delay_hist SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_HIST_BUCKETS);
} owner_runnable_offcpu_delay_hist SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_HIST_BUCKETS);
} waiter_lock_wait_hist SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, OWNER_HIST_BUCKETS);
} waiter_fanin_hist SEC(".maps");

static __always_inline bool tgid_allowed(uint64_t pid_tgid)
{
	uint32_t tgid = pid_tgid >> 32;

	return !target_tgid || tgid == target_tgid;
}

static __always_inline void counter_inc(uint32_t idx)
{
	uint64_t *cnt;

	cnt = bpf_map_lookup_elem(&counters, &idx);
	if (cnt)
		(*cnt)++;
}

static __always_inline uint32_t hist_bucket_us(uint64_t ns)
{
	uint64_t us = ns / 1000ULL;
	uint32_t bucket = 0;

	if (us == 0)
		return 0;
	if (us >= (1ULL << 32)) {
		us >>= 32;
		bucket += 32;
	}
	if (us >= (1ULL << 16)) {
		us >>= 16;
		bucket += 16;
	}
	if (us >= (1ULL << 8)) {
		us >>= 8;
		bucket += 8;
	}
	if (us >= (1ULL << 4)) {
		us >>= 4;
		bucket += 4;
	}
	if (us >= (1ULL << 2)) {
		us >>= 2;
		bucket += 2;
	}
	if (us >= (1ULL << 1))
		bucket += 1;
	if (bucket >= OWNER_HIST_BUCKETS)
		bucket = OWNER_HIST_BUCKETS - 1;
	return bucket;
}

#define HIST_INC(map_name, ns_value) do {				\
	uint32_t __idx = hist_bucket_us(ns_value);			\
	uint64_t *__cnt = bpf_map_lookup_elem(&(map_name), &__idx);	\
	if (__cnt)							\
		(*__cnt)++;						\
} while (0)

static __always_inline void record_owner_run_delay(uint32_t owner_tid, uint64_t now)
{
	struct owner_pending *pending;
	uint64_t delay;

	pending = bpf_map_lookup_elem(&pending_owners, &owner_tid);
	if (!pending || pending->run_recorded)
		return;
	delay = now - pending->first_contention_ns;
	HIST_INC(owner_next_run_delay_hist, delay);
	counter_inc(OWNER_CNT_OWNER_RUN_DELAY_SAMPLES);
	pending->run_recorded = 1;
}

static __always_inline void mark_contended_owner(uint32_t owner_tid, uint64_t lock_addr, uint64_t now)
{
	struct contended_owner *owner;

	owner = bpf_map_lookup_elem(&contended_owners, &owner_tid);
	if (owner) {
		if (!owner->lock_addr || owner->lock_addr == lock_addr) {
			owner->waiter_count++;
			if (!owner->first_contention_ns || now < owner->first_contention_ns)
				owner->first_contention_ns = now;
			return;
		}
	}

	struct contended_owner new_owner = {
		.lock_addr = lock_addr,
		.first_contention_ns = now,
		.waiter_count = 1,
	};

	bpf_map_update_elem(&contended_owners, &owner_tid, &new_owner, BPF_ANY);
	counter_inc(OWNER_CNT_CONTENDED_OWNER_TRACKED);
}

static __always_inline void record_owner_offcpu_start(uint32_t owner_tid, uint64_t now, long prev_state)
{
	struct contended_owner *owner;

	owner = bpf_map_lookup_elem(&contended_owners, &owner_tid);
	if (!owner || owner->offcpu_active)
		return;

	owner->offcpu_start_ns = now;
	owner->offcpu_active = 1;
	owner->offcpu_runnable = prev_state == 0;
	counter_inc(OWNER_CNT_OWNER_OFFCPU_EVENTS);
	if (owner->offcpu_runnable)
		counter_inc(OWNER_CNT_OWNER_PREEMPTED_EVENTS);
}

static __always_inline void record_owner_observed_offcpu(uint32_t owner_tid, uint64_t now)
{
	struct contended_owner *owner;

	owner = bpf_map_lookup_elem(&contended_owners, &owner_tid);
	if (!owner || owner->offcpu_active)
		return;

	owner->offcpu_start_ns = now;
	owner->offcpu_active = 1;
	owner->offcpu_runnable = 0;
	counter_inc(OWNER_CNT_OWNER_OFFCPU_EVENTS);
}

static __always_inline void record_owner_offcpu_end(uint32_t owner_tid, uint64_t now)
{
	struct contended_owner *owner;
	uint64_t delay;

	owner = bpf_map_lookup_elem(&contended_owners, &owner_tid);
	if (!owner || !owner->offcpu_active)
		return;

	delay = now - owner->offcpu_start_ns;
	HIST_INC(owner_offcpu_delay_hist, delay);
	counter_inc(OWNER_CNT_OWNER_OFFCPU_DELAY_SAMPLES);
	if (owner->offcpu_runnable) {
		HIST_INC(owner_runnable_offcpu_delay_hist, delay);
		counter_inc(OWNER_CNT_OWNER_RUNNABLE_OFFCPU_DELAY_SAMPLES);
	}
	owner->offcpu_active = 0;
	owner->offcpu_runnable = 0;
	owner->offcpu_start_ns = 0;
}

static __always_inline uint32_t read_pthread_mutex_owner(uint64_t lock_addr)
{
	uint32_t owner = 0;

	if (pthread_owner_offset < 0)
		return 0;
	bpf_probe_read_user(&owner, sizeof(owner), (void *)(lock_addr + pthread_owner_offset));
	return owner;
}

SEC("tp/sched/sched_switch")
int handle_sched_switch(struct trace_event_raw_sched_switch *ctx)
{
	uint32_t prev = ctx->prev_pid;
	uint32_t next = ctx->next_pid;
	uint32_t one = 1;
	uint64_t now = bpf_ktime_get_ns();

	if (prev) {
		record_owner_offcpu_start(prev, now, ctx->prev_state);
		bpf_map_delete_elem(&running_tids, &prev);
	}
	if (next) {
		bpf_map_update_elem(&running_tids, &next, &one, BPF_ANY);
		record_owner_run_delay(next, now);
		record_owner_offcpu_end(next, now);
	}
	return 0;
}

SEC("uprobe")
int handle_pthread_mutex_lock_enter(struct pt_regs *ctx)
{
	uint64_t pid_tgid = bpf_get_current_pid_tgid();
	uint32_t tid = pid_tgid;
	uint64_t lock_addr = ctx->di;
	struct active_lock_arg arg = {};
	struct lock_owner *owner;
	struct lock_owner observed_owner = {};
	uint64_t now;
	uint32_t owner_tid = 0;

	if (!tgid_allowed(pid_tgid))
		return 0;
	if (!lock_addr)
		return 0;

	now = bpf_ktime_get_ns();
	arg.lock_addr = lock_addr;
	arg.enter_ns = now;
	counter_inc(OWNER_CNT_LOCK_ENTER);

	owner = bpf_map_lookup_elem(&lock_owners, &lock_addr);
	if (owner && owner->tid) {
		owner_tid = owner->tid;
		counter_inc(OWNER_CNT_OWNER_MAP_SEEN);
	}
	if (!owner_tid) {
		owner_tid = read_pthread_mutex_owner(lock_addr);
		if (owner_tid) {
			observed_owner.tid = owner_tid;
			observed_owner.acquired_ns = 0;
			bpf_map_update_elem(&lock_owners, &lock_addr, &observed_owner, BPF_ANY);
			counter_inc(OWNER_CNT_OWNER_FIELD_SEEN);
		} else {
			counter_inc(OWNER_CNT_OWNER_FIELD_ZERO);
		}
	}
	if (owner_tid == tid)
		counter_inc(OWNER_CNT_OWNER_SELF);
	if (owner_tid && owner_tid != tid) {
		struct lock_episode *episode;
		struct owner_pending *pending;
		uint32_t *running;

		arg.contended = 1;
		arg.owner_tid = owner_tid;
		arg.contention_ns = now;
		counter_inc(OWNER_CNT_CONTENTION_EVENTS);
		mark_contended_owner(owner_tid, lock_addr, now);

		running = bpf_map_lookup_elem(&running_tids, &owner_tid);
		if (running) {
			arg.owner_already_running = 1;
			counter_inc(OWNER_CNT_OWNER_ALREADY_RUNNING);
			HIST_INC(owner_next_run_delay_hist, 0);
			counter_inc(OWNER_CNT_OWNER_RUN_DELAY_SAMPLES);
		} else {
			struct owner_pending new_pending = {
				.first_contention_ns = now,
				.run_recorded = 0,
			};

			counter_inc(OWNER_CNT_OWNER_NOT_RUNNING);
			record_owner_observed_offcpu(owner_tid, now);
			pending = bpf_map_lookup_elem(&pending_owners, &owner_tid);
			if (!pending || pending->run_recorded || now < pending->first_contention_ns)
				bpf_map_update_elem(&pending_owners, &owner_tid, &new_pending, BPF_ANY);
		}

		episode = bpf_map_lookup_elem(&lock_episodes, &lock_addr);
		if (episode && episode->owner_tid == owner_tid) {
			episode->waiter_count++;
			if (arg.owner_already_running)
				episode->owner_already_running = 1;
		} else {
			struct lock_episode new_episode = {
				.owner_tid = owner_tid,
				.waiter_count = 1,
				.first_contention_ns = now,
				.owner_already_running = arg.owner_already_running,
			};

			bpf_map_update_elem(&lock_episodes, &lock_addr, &new_episode, BPF_ANY);
		}
	}

	bpf_map_update_elem(&active_lock_args, &tid, &arg, BPF_ANY);
	return 0;
}

SEC("uretprobe")
int handle_pthread_mutex_lock_return(struct pt_regs *ctx)
{
	uint64_t pid_tgid = bpf_get_current_pid_tgid();
	uint32_t tid = pid_tgid;
	struct active_lock_arg *arg;
	struct lock_owner owner = {};
	uint64_t now;
	long ret;

	if (!tgid_allowed(pid_tgid))
		return 0;

	arg = bpf_map_lookup_elem(&active_lock_args, &tid);
	if (!arg) {
		counter_inc(OWNER_CNT_MISSING_LOCK_ARG);
		return 0;
	}

	ret = (long)ctx->ax;
	if (ret != 0) {
		counter_inc(OWNER_CNT_LOCK_FAILED);
		bpf_map_delete_elem(&active_lock_args, &tid);
		return 0;
	}

	now = bpf_ktime_get_ns();
	counter_inc(OWNER_CNT_LOCK_SUCCESS);
	if (arg->contended && now > arg->enter_ns && now - arg->enter_ns >= min_wait_ns) {
		HIST_INC(waiter_lock_wait_hist, now - arg->enter_ns);
		counter_inc(OWNER_CNT_WAITER_WAIT_SAMPLES);
	}

	owner.tid = tid;
	owner.acquired_ns = now;
	bpf_map_update_elem(&lock_owners, &arg->lock_addr, &owner, BPF_ANY);
	bpf_map_delete_elem(&active_lock_args, &tid);
	return 0;
}

SEC("uprobe")
int handle_pthread_mutex_unlock_enter(struct pt_regs *ctx)
{
	uint64_t pid_tgid = bpf_get_current_pid_tgid();
	uint32_t tid = pid_tgid;
	uint64_t lock_addr = ctx->di;
	struct lock_owner *owner;
	struct lock_episode *episode;
	struct owner_pending *pending;
	uint64_t now;

	if (!tgid_allowed(pid_tgid))
		return 0;
	if (!lock_addr)
		return 0;

	now = bpf_ktime_get_ns();
	counter_inc(OWNER_CNT_UNLOCK_ENTER);
	owner = bpf_map_lookup_elem(&lock_owners, &lock_addr);
	if (!owner) {
		counter_inc(OWNER_CNT_UNLOCK_WITHOUT_OWNER);
		return 0;
	}
	if (owner->tid != tid)
		counter_inc(OWNER_CNT_UNLOCK_OWNER_MISMATCH);

	episode = bpf_map_lookup_elem(&lock_episodes, &lock_addr);
	if (episode && episode->owner_tid == tid) {
		uint64_t delay = now - episode->first_contention_ns;

		HIST_INC(owner_unlock_delay_hist, delay);
		HIST_INC(waiter_fanin_hist, episode->waiter_count);
		counter_inc(OWNER_CNT_OWNER_UNLOCK_DELAY_SAMPLES);
		counter_inc(OWNER_CNT_FANIN_SAMPLES);
		pending = bpf_map_lookup_elem(&pending_owners, &tid);
		if (pending && !pending->run_recorded)
			counter_inc(OWNER_CNT_PENDING_WITHOUT_RUN_ON_UNLOCK);
	}

	bpf_map_delete_elem(&lock_episodes, &lock_addr);
	bpf_map_delete_elem(&pending_owners, &tid);
	bpf_map_delete_elem(&contended_owners, &tid);
	bpf_map_delete_elem(&lock_owners, &lock_addr);
	return 0;
}
