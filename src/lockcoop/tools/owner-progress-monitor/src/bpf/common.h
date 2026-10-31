#ifndef __OWNER_PROGRESS_COMMON_H
#define __OWNER_PROGRESS_COMMON_H

#if defined(__bpf__)
#else
#include <stdint.h>
#endif

#define OWNER_HIST_BUCKETS 64

enum owner_counter {
	OWNER_CNT_LOCK_ENTER = 0,
	OWNER_CNT_LOCK_SUCCESS = 1,
	OWNER_CNT_UNLOCK_ENTER = 2,
	OWNER_CNT_CONTENTION_EVENTS = 3,
	OWNER_CNT_OWNER_ALREADY_RUNNING = 4,
	OWNER_CNT_OWNER_NOT_RUNNING = 5,
	OWNER_CNT_OWNER_RUN_DELAY_SAMPLES = 6,
	OWNER_CNT_OWNER_UNLOCK_DELAY_SAMPLES = 7,
	OWNER_CNT_WAITER_WAIT_SAMPLES = 8,
	OWNER_CNT_FANIN_SAMPLES = 9,
	OWNER_CNT_MISSING_LOCK_ARG = 10,
	OWNER_CNT_LOCK_FAILED = 11,
	OWNER_CNT_UNLOCK_WITHOUT_OWNER = 12,
	OWNER_CNT_UNLOCK_OWNER_MISMATCH = 13,
	OWNER_CNT_PENDING_WITHOUT_RUN_ON_UNLOCK = 14,
	OWNER_CNT_OWNER_MAP_SEEN = 15,
	OWNER_CNT_OWNER_FIELD_SEEN = 16,
	OWNER_CNT_OWNER_FIELD_ZERO = 17,
	OWNER_CNT_OWNER_SELF = 18,
	OWNER_CNT_CONTENDED_OWNER_TRACKED = 19,
	OWNER_CNT_OWNER_OFFCPU_EVENTS = 20,
	OWNER_CNT_OWNER_PREEMPTED_EVENTS = 21,
	OWNER_CNT_OWNER_OFFCPU_DELAY_SAMPLES = 22,
	OWNER_CNT_OWNER_RUNNABLE_OFFCPU_DELAY_SAMPLES = 23,
	OWNER_CNT_NR = 24,
};

struct active_lock_arg {
	uint64_t lock_addr;
	uint64_t enter_ns;
	uint64_t contention_ns;
	uint32_t owner_tid;
	uint8_t contended;
	uint8_t owner_already_running;
};

struct lock_owner {
	uint32_t tid;
	uint64_t acquired_ns;
};

struct lock_episode {
	uint32_t owner_tid;
	uint32_t waiter_count;
	uint64_t first_contention_ns;
	uint8_t owner_already_running;
};

struct owner_pending {
	uint64_t first_contention_ns;
	uint8_t run_recorded;
};

struct contended_owner {
	uint64_t lock_addr;
	uint64_t first_contention_ns;
	uint64_t offcpu_start_ns;
	uint32_t waiter_count;
	uint8_t offcpu_active;
	uint8_t offcpu_runnable;
};

#endif
