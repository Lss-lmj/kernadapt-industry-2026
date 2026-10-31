// SPDX-License-Identifier: GPL-2.0
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include "common.h"

char LICENSE[] SEC("license") = "GPL";

const volatile uint32_t target_tgid = 0;
const volatile uint64_t max_handoff_gap_ns = 100ULL * 1000ULL * 1000ULL;
const volatile bool skip_same_tid = true;

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint32_t);
	__type(value, struct active_lock_arg);
	__uint(max_entries, 131072);
} active_lock_args SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__type(key, uint64_t);
	__type(value, struct release_info);
	__uint(max_entries, 262144);
} last_release_by_lock SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_HASH);
	__type(key, struct cpu_pair_key);
	__type(value, uint64_t);
	__uint(max_entries, 65536);
} cpu_pair_counts SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(key_size, sizeof(uint32_t));
	__uint(value_size, sizeof(uint64_t));
	__uint(max_entries, HANDOFF_CNT_NR);
} counters SEC(".maps");

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

static __always_inline void cpu_pair_inc(uint32_t prev_cpu, uint32_t curr_cpu)
{
	struct cpu_pair_key key = {
		.prev_cpu = prev_cpu,
		.curr_cpu = curr_cpu,
	};
	uint64_t init = 1;
	uint64_t *cnt;

	cnt = bpf_map_lookup_elem(&cpu_pair_counts, &key);
	if (cnt) {
		(*cnt)++;
		return;
	}
	bpf_map_update_elem(&cpu_pair_counts, &key, &init, BPF_ANY);
}

SEC("uprobe")
int handle_pthread_mutex_lock_enter(struct pt_regs *ctx)
{
	uint64_t pid_tgid = bpf_get_current_pid_tgid();
	uint32_t tid = pid_tgid;
	struct active_lock_arg arg = {
		.lock_addr = ctx->di,
		.enter_ns = bpf_ktime_get_ns(),
	};

	if (!tgid_allowed(pid_tgid))
		return 0;
	if (!arg.lock_addr)
		return 0;

	counter_inc(HANDOFF_CNT_LOCK_ENTER);
	bpf_map_update_elem(&active_lock_args, &tid, &arg, BPF_ANY);
	return 0;
}

SEC("uretprobe")
int handle_pthread_mutex_lock_return(struct pt_regs *ctx)
{
	uint64_t pid_tgid = bpf_get_current_pid_tgid();
	uint32_t tid = pid_tgid;
	struct active_lock_arg *arg;
	struct release_info *prev;
	uint64_t now;
	long ret;
	uint32_t cpu;

	if (!tgid_allowed(pid_tgid))
		return 0;

	arg = bpf_map_lookup_elem(&active_lock_args, &tid);
	if (!arg) {
		counter_inc(HANDOFF_CNT_MISSING_LOCK_ARG);
		return 0;
	}

	ret = (long)ctx->ax;
	if (ret != 0) {
		counter_inc(HANDOFF_CNT_LOCK_FAILED);
		bpf_map_delete_elem(&active_lock_args, &tid);
		return 0;
	}

	counter_inc(HANDOFF_CNT_LOCK_SUCCESS);
	now = bpf_ktime_get_ns();
	cpu = bpf_get_smp_processor_id();
	prev = bpf_map_lookup_elem(&last_release_by_lock, &arg->lock_addr);
	if (!prev) {
		counter_inc(HANDOFF_CNT_NO_LAST_RELEASE);
		bpf_map_delete_elem(&active_lock_args, &tid);
		return 0;
	}

	if (skip_same_tid && prev->tid == tid) {
		counter_inc(HANDOFF_CNT_SAME_TID_SKIPPED);
		bpf_map_delete_elem(&active_lock_args, &tid);
		return 0;
	}

	if (max_handoff_gap_ns && now - prev->ts_ns > max_handoff_gap_ns) {
		counter_inc(HANDOFF_CNT_STALE_SKIPPED);
		bpf_map_delete_elem(&active_lock_args, &tid);
		return 0;
	}

	cpu_pair_inc(prev->cpu, cpu);
	counter_inc(HANDOFF_CNT_HANDOFF_EVENTS);
	bpf_map_delete_elem(&active_lock_args, &tid);
	return 0;
}

SEC("uprobe")
int handle_pthread_mutex_unlock_enter(struct pt_regs *ctx)
{
	uint64_t pid_tgid = bpf_get_current_pid_tgid();
	uint32_t tid = pid_tgid;
	uint64_t lock_addr = ctx->di;
	struct release_info info = {
		.tid = tid,
		.cpu = bpf_get_smp_processor_id(),
		.ts_ns = bpf_ktime_get_ns(),
	};

	if (!tgid_allowed(pid_tgid))
		return 0;
	if (!lock_addr)
		return 0;

	counter_inc(HANDOFF_CNT_UNLOCK_ENTER);
	bpf_map_update_elem(&last_release_by_lock, &lock_addr, &info, BPF_ANY);
	return 0;
}
