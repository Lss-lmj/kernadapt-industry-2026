#ifndef __HANDOFF_LOCALITY_COMMON_H
#define __HANDOFF_LOCALITY_COMMON_H

#if defined(__bpf__)
#else
#include <stdint.h>
#endif

enum handoff_counter {
	HANDOFF_CNT_LOCK_ENTER = 0,
	HANDOFF_CNT_LOCK_SUCCESS = 1,
	HANDOFF_CNT_UNLOCK_ENTER = 2,
	HANDOFF_CNT_HANDOFF_EVENTS = 3,
	HANDOFF_CNT_MISSING_LOCK_ARG = 4,
	HANDOFF_CNT_LOCK_FAILED = 5,
	HANDOFF_CNT_NO_LAST_RELEASE = 6,
	HANDOFF_CNT_SAME_TID_SKIPPED = 7,
	HANDOFF_CNT_STALE_SKIPPED = 8,
	HANDOFF_CNT_NR = 9,
};

struct active_lock_arg {
	uint64_t lock_addr;
	uint64_t enter_ns;
};

struct release_info {
	uint32_t tid;
	uint32_t cpu;
	uint64_t ts_ns;
};

struct cpu_pair_key {
	uint32_t prev_cpu;
	uint32_t curr_cpu;
};

#endif
