/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Userspace component for the scx_lockcoop scheduler.
 *
 * This program loads the scx_lockcoop BPF scheduler, attaches it,
 * and provides a user interface to monitor its state, including the
 * dynamically created lock nests.
 *
 * Combines owner-progress scheduling and lock-handoff placement.
 */
#include <stdio.h>
#include <stdlib.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <libgen.h>
#include <getopt.h>
#include <errno.h>
#include <pthread.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <linux/magic.h>
#include <bpf/bpf.h>
#include <scx/common.h>

#ifdef __GLIBC__
#include <gnu/libc-version.h>
#endif

#ifndef LOCKCOOP_SKEL_HEADER
#define LOCKCOOP_SKEL_HEADER "scx_lockcoop.bpf.skel.h"
#endif
#ifndef LOCKCOOP_SKEL
#define LOCKCOOP_SKEL scx_lockcoop
#endif
#ifndef LOCKCOOP_OPS
#define LOCKCOOP_OPS lockcoop_ops
#endif
#ifndef LOCKCOOP_DESTROY
#define LOCKCOOP_DESTROY scx_lockcoop__destroy
#endif
#ifndef LOCKCOOP_SKEL__open
#define LOCKCOOP_SKEL__open scx_lockcoop__open
#endif
#ifndef LOCKCOOP_SKEL__load
#define LOCKCOOP_SKEL__load scx_lockcoop__load
#endif
#ifndef LOCKCOOP_SKEL__attach
#define LOCKCOOP_SKEL__attach scx_lockcoop__attach
#endif
#ifndef LOCKCOOP_SCHED_LABEL
#define LOCKCOOP_SCHED_LABEL "scx_lockcoop"
#endif
#ifndef LOCKCOOP_FORCE_NEST
#define LOCKCOOP_FORCE_NEST 1
#endif
#ifndef LOCKCOOP_FORCE_BOOST
#define LOCKCOOP_FORCE_BOOST 1
#endif

#include LOCKCOOP_SKEL_HEADER

enum lock_source {
    LOCK_SRC_NONE = 0,
    LOCK_SRC_MUTEX_OWNER = 1,
    LOCK_SRC_MUTEX_RELEASER = 2,
    LOCK_SRC_FUTEX_WAKER = 3,
    LOCK_SRC_PTHREAD_OWNER = 4,
    LOCK_SRC_FUTEX_WAITER = 5,
    LOCK_SRC_RWSEM_WRITER = 6,
    LOCK_SRC_RWSEM_READER = 7,
    LOCK_SRC_FUTEX_CANONICAL = 8,
    LOCK_SRC_PI_OWNER = 9,
    LOCK_SRC_REQUEUE = 10,
    LOCK_SRC_PTHREAD_RWLOCK = 11,
    LOCK_SRC_USER_ANNOTATION = 12,
};

enum lock_key_kind {
    LOCK_KEY_NONE = 0,
    LOCK_KEY_KERNEL_MUTEX = 1,
    LOCK_KEY_PRIVATE_FUTEX = 2,
    LOCK_KEY_KERNEL_RWSEM = 3,
    LOCK_KEY_SHARED_FUTEX = 4,
    LOCK_KEY_PI_FUTEX = 5,
    LOCK_KEY_REQUEUE_FUTEX = 6,
    LOCK_KEY_PTHREAD_RWLOCK = 7,
    LOCK_KEY_USER_SPIN = 8,
    LOCK_KEY_USER_CAS = 9,
};

enum stat_idx {
    STAT_LOCAL = 0,
    STAT_GLOBAL = 1,
    STAT_MUTEX_OWNER_HIT = 2,
    STAT_MUTEX_OWNER_MISS = 3,
    STAT_FUTEX_PRIVATE_WAIT = 4,
    STAT_FUTEX_PRIVATE_OWNER_HIT = 5,
    STAT_FUTEX_PRIVATE_OWNER_MISS = 6,
    STAT_FUTEX_WAKE = 7,
    STAT_FUTEX_SHARED_FALLBACK = 8,
    STAT_FUTEX_PI_FALLBACK = 9,
    STAT_FUTEX_REQUEUE_FALLBACK = 10,
    STAT_OWNER_HIT = 11,
    STAT_OWNER_MISS = 12,
    STAT_WAITER_HIT = 13,
    STAT_WAITER_MISS = 14,
    STAT_LOCALITY_HIT = 15,
    STAT_LOCALITY_FALLBACK = 16,
    STAT_STALE_NEST = 17,
    STAT_BOOST_APPLIED = 18,
    STAT_BOOST_BLOCKED_NO_OWNER = 19,
    STAT_MAP_UPDATE_FAIL = 20,
    STAT_WAITER_OVERWRITE = 21,
	STAT_WAITER_CLEANUP = 22,
	STAT_COUNTER_UNDERFLOW = 23,
	STAT_WAITER_ORPHAN_CLEANUP = 24,
	STAT_ANCHOR_CREATE = 25,
	STAT_ANCHOR_REFRESH = 26,
	STAT_ANCHOR_UPGRADE = 27,
	STAT_DECAY_CLEANUP = 28,
	STAT_ANCHOR_PRESSURE_REJECT = 29,
	STAT_ANCHOR_CPUMASK_REJECT = 30,
	STAT_ANCHOR_COOLDOWN_REJECT = 31,
	STAT_ANCHOR_PREVCPU_FALLBACK = 32,
	STAT_ANCHOR_DEFAULT_FALLBACK = 33,
	STAT_ANCHOR_EXPIRY_SHORTEN = 34,
	STAT_ANCHOR_EXPIRY_EXTEND = 35,
	STAT_BOOST_CONF_DENIED = 36,
	STAT_BOOST_BUDGET_DENIED = 37,
	STAT_BOOST_LONGCS_DENIED = 38,
	STAT_BOOST_REVOKE_UNLOCK = 39,
	STAT_OWNER_DELAY_SUM_NS = 40,
	STAT_OWNER_DELAY_COUNT = 41,
	STAT_WAITERS_UNBLOCKED_AFTER_BOOST = 42,
	STAT_BOOST_INF_APPLIED = 43,
	STAT_BOOST_SHORT_APPLIED = 44,
	STAT_BOOST_BOUNDED_APPLIED = 45,
	STAT_BOOST_ADAPTIVE_APPLIED = 46,
	STAT_BOOST_TASK_BUDGET_DENIED = 47,
	STAT_BOOST_LOCK_BUDGET_DENIED = 48,
	STAT_BOOST_CGROUP_BUDGET_DENIED = 49,
	STAT_CONF_NONE = 50,
	STAT_CONF_WAITER_ONLY = 51,
	STAT_CONF_PROBABLE = 52,
	STAT_CONF_VERIFIED = 53,
	STAT_THRESHOLD_UP = 54,
	STAT_THRESHOLD_DOWN = 55,
	STAT_ANCHOR_MATURITY_UPDATE = 56,
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

enum owner_confidence {
    CONF_NONE = 0,
    CONF_WAITER_ONLY = 1,
    CONF_PROBABLE = 2,
    CONF_VERIFIED = 3,
};

enum boost_profile {
    BOOST_PROFILE_NONE = 0,
    BOOST_PROFILE_SHORT = 1,
    BOOST_PROFILE_BOUNDED = 2,
    BOOST_PROFILE_ADAPTIVE = 3,
    BOOST_PROFILE_INF = 4,
};

enum anchor_maturity_profile {
    ANCHOR_MATURITY_OFF = 0,
    ANCHOR_MATURITY_STATIC = 1,
    ANCHOR_MATURITY_SOURCE = 2,
    ANCHOR_MATURITY_ADAPTIVE = 3,
};

enum evidence_scope_mode {
    EVIDENCE_SCOPE_ALL = 0,
    EVIDENCE_SCOPE_TGID = 1,
    EVIDENCE_SCOPE_CGROUP = 2,
};

#define EVIDENCE_ALLOW_MAX 64

struct lock_key {
    u64 addr;
    u64 scope_id;
    u32 kind;
    u32 discriminator;
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


const char help_fmt[] =
"A lockcoop sched_ext scheduler with owner progress and handoff anchoring.\n"
"\n"
"Usage: %s [-f] [-v] [-D] [-A] [-t threshold] [-d decay_ms] [-e expiry_ms] [-q queue_limit] [-O owner_offset] [-B boost_slices] [-M max_boost_slices] [--boost-profile PROFILE] [--anchor-maturity-profile PROFILE] [--evidence-scope SCOPE] [--stats-only]\n"
"\n"
"  -f            Use FIFO scheduling (disables affinity logic)\n"
"  -t <count>    Set the high contention threshold (default: 4 waiters)\n"
"  -d <ms>       Set the priority decay timeout in milliseconds (default: 200ms)\n"
"  -e <ms>       Set lock-nest expiry timeout in milliseconds (default: 2000ms)\n"
"  -q <count>    Limit queued anchor waiters per CPU (default: direct handoff)\n"
"  -O <offset>   Override the validated pthread owner-word offset; -1 disables (default: auto)\n"
"  -B <slices>   Owner boost grant in default slices (default: 1)\n"
"  -M <slices>   Owner boost budget cap in default slices (default: 2)\n"
"  -A            Enable adaptive LockNest admission/expiry\n"
"  -Q <count>    Adaptive anchor queue soft limit when -q is unset (default: unlimited)\n"
"  -R <count>    Pressure rejects before anchor cooldown (default: 4)\n"
"  -P <count>    Direct hits before expiry extension (default: disabled)\n"
"  -C <ms>       Adaptive anchor cooldown window (default: 5ms)\n"
"  -N <ms>       Adaptive minimum anchor expiry (default: expiry_ms)\n"
"  -X <ms>       Adaptive maximum anchor expiry (default: 5000ms)\n"
"  --boost-profile none|short|bounded|adaptive|inf\n"
"                Select LockBoost profile (default: bounded)\n"
"  --boost-grant-us <us>\n"
"                Override owner boost grant in microseconds\n"
"  --boost-cap-us <us>\n"
"                Override owner boost cap in microseconds\n"
"  --boost-window-ms <ms>\n"
"                Sliding budget refill window (default: 100ms)\n"
"  --boost-task-budget-pct <pct>\n"
"                Per-owner boost budget as percent of window (0 disables)\n"
"  --boost-lock-budget-pct <pct>\n"
"                Per-lock boost budget as percent of window (0 disables)\n"
"  --boost-cgroup-budget-pct <pct>\n"
"                Per-cgroup boost budget as percent of window (0 disables)\n"
"  --boost-target-delay-us <us>\n"
"                Owner runnable-delay target for adaptive boost (default: 500us)\n"
"  --boost-confidence-gate none|waiter|probable|verified\n"
"                Minimum owner confidence for boost (default: verified)\n"
"  --boost-max-continuous-us <us>\n"
"                Max continuous bounded boost before cooldown (default: disabled; adaptive default: 5000us)\n"
"  --allow-inf   Allow --boost-profile inf to use SCX_SLICE_INF\n"
"  --stats-only  Print aggregate counters without loading diagnostic-only probes\n"
"  --sync-path-profile none|mutex|futex|legacy|kernel|full\n"
"                Select one probe group or a cumulative production profile\n"
"  --sensor-only  Keep lock probes and state maintenance but disable Nest/Boost actions\n"
"  --switch-partial\n"
"                Manage only tasks explicitly assigned to SCHED_EXT\n"
"  --anchor-maturity-profile off|static|source|adaptive\n"
"  --anchor-maturity-threshold N     Static/adaptive warm-up threshold (0 permits immediately)\n"
"  --anchor-maturity-strong-threshold N  Source threshold for verified evidence\n"
"  --anchor-maturity-weak-threshold N    Source threshold for futex-waker evidence\n"
"  --anchor-maturity-max-score N     Saturating maturity score cap (default: 32)\n"
"  --anchor-maturity-window-ms N     Compatible-update window; 0 follows anchor expiry\n"
"  --evidence-scope all|tgid|cgroup  Producer scope; default all\n"
"  --evidence-tgid PID               Allow a TGID (repeatable with scope=tgid)\n"
"  --evidence-cgroup-id ID           Allow a cgroup v2 id (repeatable with scope=cgroup)\n"
"  --evidence-cgroup-path PATH       Resolve a cgroup v2 path to an id (repeatable)\n"
"  --evidence-scope-stats            Enable scope counters\n"
"  --pthread-rwlock-lib PATH         libc used for pthread rwlock uprobes\n"
"  --user-sync-lib PATH              spin/CAS annotation ABI library\n"
"  --no-user-probes                  Disable rwlock and annotation uprobes\n"
"  -D            Enable Phase 1/2 diagnostic counters\n"
"  -v            Print libbpf debug messages\n"
"  -h            Display this help and exit\n";

static bool verbose;
static volatile int exit_req;
static volatile sig_atomic_t snapshot_req;

static int libbpf_print_fn(enum libbpf_print_level level, const char *format, va_list args) {
	if (level == LIBBPF_DEBUG && !verbose)
		return 0;
	return vfprintf(stderr, format, args);
}

static void sigint_handler(int sig) {
	exit_req = 1;
}

static void snapshot_handler(int sig)
{
	(void)sig;
	snapshot_req = 1;
}

static int parse_boost_profile(const char *s)
{
	if (!strcmp(s, "none"))
		return BOOST_PROFILE_NONE;
	if (!strcmp(s, "short"))
		return BOOST_PROFILE_SHORT;
	if (!strcmp(s, "bounded"))
		return BOOST_PROFILE_BOUNDED;
	if (!strcmp(s, "adaptive"))
		return BOOST_PROFILE_ADAPTIVE;
	if (!strcmp(s, "inf"))
		return BOOST_PROFILE_INF;
	return -1;
}

static int parse_confidence_gate(const char *s)
{
	if (!strcmp(s, "none"))
		return CONF_NONE;
	if (!strcmp(s, "waiter"))
		return CONF_WAITER_ONLY;
	if (!strcmp(s, "probable"))
		return CONF_PROBABLE;
	if (!strcmp(s, "verified"))
		return CONF_VERIFIED;
	return -1;
}

static const char *boost_profile_name(u32 profile)
{
	switch (profile) {
	case BOOST_PROFILE_NONE:
		return "none";
	case BOOST_PROFILE_SHORT:
		return "short";
	case BOOST_PROFILE_BOUNDED:
		return "bounded";
	case BOOST_PROFILE_ADAPTIVE:
		return "adaptive";
	case BOOST_PROFILE_INF:
		return "inf";
	default:
		return "unknown";
	}
}

static const char *confidence_name(u32 confidence)
{
	switch (confidence) {
	case CONF_NONE:
		return "none";
	case CONF_WAITER_ONLY:
		return "waiter";
	case CONF_PROBABLE:
		return "probable";
	case CONF_VERIFIED:
		return "verified";
	default:
		return "unknown";
	}
}

struct evidence_scope_config {
	int mode;
	bool mode_set;
	bool stats;
	u32 tgids[EVIDENCE_ALLOW_MAX];
	size_t nr_tgids;
	u64 cgroup_ids[EVIDENCE_ALLOW_MAX];
	size_t nr_cgroup_ids;
};

static int parse_anchor_maturity_profile(const char *s)
{
	if (!strcmp(s, "off"))
		return ANCHOR_MATURITY_OFF;
	if (!strcmp(s, "static"))
		return ANCHOR_MATURITY_STATIC;
	if (!strcmp(s, "source"))
		return ANCHOR_MATURITY_SOURCE;
	if (!strcmp(s, "adaptive"))
		return ANCHOR_MATURITY_ADAPTIVE;
	return -1;
}

static int parse_evidence_scope(const char *s)
{
	if (!strcmp(s, "all"))
		return EVIDENCE_SCOPE_ALL;
	if (!strcmp(s, "tgid"))
		return EVIDENCE_SCOPE_TGID;
	if (!strcmp(s, "cgroup"))
		return EVIDENCE_SCOPE_CGROUP;
	return -1;
}

static const char *anchor_maturity_profile_name(u32 profile)
{
	switch (profile) {
	case ANCHOR_MATURITY_OFF:
		return "off";
	case ANCHOR_MATURITY_STATIC:
		return "static";
	case ANCHOR_MATURITY_SOURCE:
		return "source";
	case ANCHOR_MATURITY_ADAPTIVE:
		return "adaptive";
	default:
		return "unknown";
	}
}

static const char *evidence_scope_name(u32 mode)
{
	switch (mode) {
	case EVIDENCE_SCOPE_ALL:
		return "all";
	case EVIDENCE_SCOPE_TGID:
		return "tgid";
	case EVIDENCE_SCOPE_CGROUP:
		return "cgroup";
	default:
		return "unknown";
	}
}

static int parse_u64_arg(const char *arg, u64 *value)
{
	char *end = NULL;
	unsigned long long parsed;

	errno = 0;
	parsed = strtoull(arg, &end, 0);
	if (errno || !end || *end != '\0')
		return -1;
	*value = parsed;
	return 0;
}

static int append_tgid(struct evidence_scope_config *scope, const char *arg)
{
	u64 value;
	size_t i;

	if (parse_u64_arg(arg, &value) || !value || value > UINT_MAX)
		return -1;
	for (i = 0; i < scope->nr_tgids; i++)
		if (scope->tgids[i] == (u32)value)
			return 0;
	if (scope->nr_tgids >= EVIDENCE_ALLOW_MAX)
		return -1;
	scope->tgids[scope->nr_tgids++] = (u32)value;
	return 0;
}

static int append_cgroup_id(struct evidence_scope_config *scope, u64 value)
{
	size_t i;

	if (!value)
		return -1;
	for (i = 0; i < scope->nr_cgroup_ids; i++)
		if (scope->cgroup_ids[i] == value)
			return 0;
	if (scope->nr_cgroup_ids >= EVIDENCE_ALLOW_MAX)
		return -1;
	scope->cgroup_ids[scope->nr_cgroup_ids++] = value;
	return 0;
}

static int cgroup_path_to_id(const char *path, u64 *id)
{
	struct statfs fs;
	struct stat st;

	if (statfs(path, &fs))
		return -1;
	if ((unsigned long)fs.f_type != CGROUP2_SUPER_MAGIC) {
		errno = EINVAL;
		return -1;
	}
	if (stat(path, &st) || !S_ISDIR(st.st_mode) || !st.st_ino) {
		if (!errno)
			errno = EINVAL;
		return -1;
	}
	/* cgroup v2 exposes the kernfs node id as the directory inode number. */
	*id = (u64)st.st_ino;
	return 0;
}

static int validate_evidence_scope(const struct evidence_scope_config *scope)
{
	char proc_path[64];
	size_t i;

	if (scope->mode == EVIDENCE_SCOPE_ALL)
		return !scope->nr_tgids && !scope->nr_cgroup_ids ? 0 : -1;
	if (scope->mode == EVIDENCE_SCOPE_TGID) {
		if (!scope->nr_tgids || scope->nr_cgroup_ids)
			return -1;
		for (i = 0; i < scope->nr_tgids; i++) {
			snprintf(proc_path, sizeof(proc_path), "/proc/%u", scope->tgids[i]);
			if (access(proc_path, F_OK))
				return -1;
		}
		return 0;
	}
	if (scope->mode == EVIDENCE_SCOPE_CGROUP)
		return scope->nr_cgroup_ids && !scope->nr_tgids ? 0 : -1;
	return -1;
}

static int populate_evidence_scope_maps(struct LOCKCOOP_SKEL *skel,
						const struct evidence_scope_config *scope)
{
	u8 value = 1;
	size_t i;
	int fd;

	if (scope->mode == EVIDENCE_SCOPE_ALL)
		return 0;
	if (scope->mode == EVIDENCE_SCOPE_TGID) {
		fd = bpf_map__fd(skel->maps.evidence_tgid_allow);
		for (i = 0; i < scope->nr_tgids; i++)
			if (bpf_map_update_elem(fd, &scope->tgids[i], &value, BPF_ANY))
				return -1;
		return 0;
	}
	fd = bpf_map__fd(skel->maps.evidence_cgroup_allow);
	for (i = 0; i < scope->nr_cgroup_ids; i++)
		if (bpf_map_update_elem(fd, &scope->cgroup_ids[i], &value, BPF_ANY))
			return -1;
	return 0;
}

static void print_evidence_scope_config(const struct evidence_scope_config *scope)
{
	size_t i;

	printf("Evidence scope=%s allow entries:", evidence_scope_name(scope->mode));
	if (scope->mode == EVIDENCE_SCOPE_ALL)
		printf(" all producers\n");
	else if (scope->mode == EVIDENCE_SCOPE_TGID) {
		for (i = 0; i < scope->nr_tgids; i++)
			printf(" %u", scope->tgids[i]);
		printf("\n");
	} else {
		for (i = 0; i < scope->nr_cgroup_ids; i++)
			printf(" %llu", (unsigned long long)scope->cgroup_ids[i]);
		printf("\n");
	}
}

static void read_stats(struct LOCKCOOP_SKEL *skel, __u64 *stats,
		       __u32 nr_stats) {
	int nr_cpus = libbpf_num_possible_cpus();
	__u64 *cnts;
	__u32 idx;

	memset(stats, 0, sizeof(stats[0]) * STAT_MAX);
	cnts = calloc(nr_cpus, sizeof(*cnts));
	if (!cnts)
		return;

	for (idx = 0; idx < nr_stats; idx++) {
		int ret, cpu;

		memset(cnts, 0, sizeof(*cnts) * nr_cpus);
		ret = bpf_map_lookup_elem(bpf_map__fd(skel->maps.stats), &idx, cnts);
		if (ret < 0) continue;
		for (cpu = 0; cpu < nr_cpus; cpu++)
			stats[idx] += cnts[cpu];
	}
	free(cnts);
}

static const char *source_name(u32 source) {
    switch (source) {
    case LOCK_SRC_MUTEX_OWNER:
        return "mutex-owner";
    case LOCK_SRC_MUTEX_RELEASER:
        return "mutex-releaser";
    case LOCK_SRC_FUTEX_WAKER:
        return "futex-waker";
    case LOCK_SRC_PTHREAD_OWNER:
        return "pthread-owner";
    case LOCK_SRC_FUTEX_WAITER:
        return "futex-waiter";
    case LOCK_SRC_RWSEM_WRITER:
        return "rwsem-writer";
    case LOCK_SRC_RWSEM_READER:
        return "rwsem-reader";
    case LOCK_SRC_FUTEX_CANONICAL:
        return "futex-canonical";
    case LOCK_SRC_PI_OWNER:
        return "pi-owner";
    case LOCK_SRC_REQUEUE:
        return "requeue";
    case LOCK_SRC_PTHREAD_RWLOCK:
        return "pthread-rwlock";
    case LOCK_SRC_USER_ANNOTATION:
        return "user-annotation";
    default:
        return "none";
    }
}

static const char *key_kind_name(u32 kind) {
    switch (kind) {
    case LOCK_KEY_KERNEL_MUTEX:
        return "kernel-mutex";
    case LOCK_KEY_PRIVATE_FUTEX:
        return "private-futex";
    case LOCK_KEY_KERNEL_RWSEM:
        return "kernel-rwsem";
    case LOCK_KEY_SHARED_FUTEX:
        return "shared-futex";
    case LOCK_KEY_PI_FUTEX:
        return "pi-futex";
    case LOCK_KEY_REQUEUE_FUTEX:
        return "requeue-futex";
    case LOCK_KEY_PTHREAD_RWLOCK:
        return "pthread-rwlock";
    case LOCK_KEY_USER_SPIN:
        return "user-spin";
    case LOCK_KEY_USER_CAS:
        return "user-cas";
    default:
        return "none";
    }
}

static void print_stats(__u64 *stats) {
    printf("queues local=%-10llu global=%-10llu | owner hit/miss=%llu/%llu\n",
           stats[STAT_LOCAL], stats[STAT_GLOBAL],
           stats[STAT_OWNER_HIT], stats[STAT_OWNER_MISS]);
    printf("waiter hit/miss=%llu/%llu | locality hit/fallback=%llu/%llu stale_nest=%llu | boost applied=%llu\n",
           stats[STAT_WAITER_HIT], stats[STAT_WAITER_MISS],
           stats[STAT_LOCALITY_HIT], stats[STAT_LOCALITY_FALLBACK],
           stats[STAT_STALE_NEST],
           stats[STAT_BOOST_APPLIED]);
    printf("futex private wait=%llu owner h/m=%llu/%llu wake=%llu shared/pi/requeue fallback=%llu/%llu/%llu\n\n",
           stats[STAT_FUTEX_PRIVATE_WAIT],
           stats[STAT_FUTEX_PRIVATE_OWNER_HIT],
           stats[STAT_FUTEX_PRIVATE_OWNER_MISS],
           stats[STAT_FUTEX_WAKE],
           stats[STAT_FUTEX_SHARED_FALLBACK],
           stats[STAT_FUTEX_PI_FALLBACK],
           stats[STAT_FUTEX_REQUEUE_FALLBACK]);
    printf("map_fail=%llu waiter_overwrite=%llu cleanup=%llu underflow=%llu anchor create/refresh/upgrade=%llu/%llu/%llu decay=%llu\n",
           stats[STAT_MAP_UPDATE_FAIL],
           stats[STAT_WAITER_OVERWRITE],
           stats[STAT_WAITER_CLEANUP],
           stats[STAT_COUNTER_UNDERFLOW],
           stats[STAT_ANCHOR_CREATE],
           stats[STAT_ANCHOR_REFRESH],
           stats[STAT_ANCHOR_UPGRADE],
           stats[STAT_DECAY_CLEANUP]);
    printf("waiter orphan cleanup=%llu\n",
           stats[STAT_WAITER_ORPHAN_CLEANUP]);
    printf("adaptive pressure/cpumask/cooldown=%llu/%llu/%llu prevcpu/default=%llu/%llu expiry shorten/extend=%llu/%llu\n",
           stats[STAT_ANCHOR_PRESSURE_REJECT],
           stats[STAT_ANCHOR_CPUMASK_REJECT],
           stats[STAT_ANCHOR_COOLDOWN_REJECT],
           stats[STAT_ANCHOR_PREVCPU_FALLBACK],
           stats[STAT_ANCHOR_DEFAULT_FALLBACK],
           stats[STAT_ANCHOR_EXPIRY_SHORTEN],
           stats[STAT_ANCHOR_EXPIRY_EXTEND]);
    printf("boost deny conf/budget/longcs=%llu/%llu/%llu revoke=%llu unblocked=%llu kind short/bounded/adaptive/inf=%llu/%llu/%llu/%llu\n",
           stats[STAT_BOOST_CONF_DENIED],
           stats[STAT_BOOST_BUDGET_DENIED],
           stats[STAT_BOOST_LONGCS_DENIED],
           stats[STAT_BOOST_REVOKE_UNLOCK],
           stats[STAT_WAITERS_UNBLOCKED_AFTER_BOOST],
           stats[STAT_BOOST_SHORT_APPLIED],
           stats[STAT_BOOST_BOUNDED_APPLIED],
           stats[STAT_BOOST_ADAPTIVE_APPLIED],
           stats[STAT_BOOST_INF_APPLIED]);
    printf("boost budget deny task/lock/cgroup=%llu/%llu/%llu owner_delay count=%llu avg_us=%llu\n",
           stats[STAT_BOOST_TASK_BUDGET_DENIED],
           stats[STAT_BOOST_LOCK_BUDGET_DENIED],
           stats[STAT_BOOST_CGROUP_BUDGET_DENIED],
           stats[STAT_OWNER_DELAY_COUNT],
           stats[STAT_OWNER_DELAY_COUNT] ?
           stats[STAT_OWNER_DELAY_SUM_NS] / stats[STAT_OWNER_DELAY_COUNT] / 1000 : 0);
    printf("boost trigger running/tick=%llu/%llu owner state running/runnable/sleeping=%llu/%llu/%llu\n",
           stats[STAT_BOOST_RUNNING_APPLIED],
           stats[STAT_BOOST_TICK_APPLIED],
           stats[STAT_OWNER_OBS_RUNNING],
           stats[STAT_OWNER_OBS_RUNNABLE],
           stats[STAT_OWNER_OBS_SLEEPING]);
    printf("queued-owner mark/dispatch/scanned=%llu/%llu/%llu\n",
           stats[STAT_URGENT_OWNER_MARK],
           stats[STAT_URGENT_OWNER_DISPATCH],
           stats[STAT_URGENT_OWNER_SCAN_TASK]);
    printf("pthread owner resolve word_reject/task_miss/mm_mismatch/validated=%llu/%llu/%llu/%llu\n",
           stats[STAT_OWNER_WORD_REJECT],
           stats[STAT_OWNER_TASK_MISS],
           stats[STAT_OWNER_MM_MISMATCH],
           stats[STAT_OWNER_VALIDATED]);
    printf("owner confidence none/waiter/probable/verified=%llu/%llu/%llu/%llu threshold up/down=%llu/%llu\n",
           stats[STAT_CONF_NONE],
           stats[STAT_CONF_WAITER_ONLY],
           stats[STAT_CONF_PROBABLE],
           stats[STAT_CONF_VERIFIED],
           stats[STAT_THRESHOLD_UP],
           stats[STAT_THRESHOLD_DOWN]);
    printf("anchor maturity update/compatible=%llu/%llu reset cpu/source/stale=%llu/%llu/%llu pass/reject=%llu/%llu strong/weak pass=%llu/%llu\n",
           stats[STAT_ANCHOR_MATURITY_UPDATE],
           stats[STAT_ANCHOR_MATURITY_COMPATIBLE_UPDATE],
           stats[STAT_ANCHOR_MATURITY_RESET_CPU],
           stats[STAT_ANCHOR_MATURITY_RESET_SOURCE],
           stats[STAT_ANCHOR_MATURITY_RESET_STALE],
           stats[STAT_ANCHOR_MATURITY_PASS],
           stats[STAT_ANCHOR_MATURITY_REJECT],
           stats[STAT_ANCHOR_MATURITY_STRONG_PASS],
           stats[STAT_ANCHOR_MATURITY_WEAK_PASS]);
    printf("evidence scope seen/admitted/denied=%llu/%llu/%llu cleanup_out=%llu admit mutex enter/slow/unlock wait/wake=%llu/%llu/%llu/%llu/%llu deny=%llu/%llu/%llu/%llu/%llu\n",
           stats[STAT_EVIDENCE_SEEN],
           stats[STAT_EVIDENCE_ADMITTED],
           stats[STAT_EVIDENCE_SCOPE_DENIED],
           stats[STAT_EVIDENCE_CLEANUP_OUT_OF_SCOPE],
           stats[STAT_SCOPE_ADMIT_MUTEX_ENTER],
           stats[STAT_SCOPE_ADMIT_MUTEX_SLOWPATH],
           stats[STAT_SCOPE_ADMIT_MUTEX_UNLOCK],
           stats[STAT_SCOPE_ADMIT_FUTEX_WAIT],
           stats[STAT_SCOPE_ADMIT_FUTEX_WAKE],
           stats[STAT_SCOPE_DENY_MUTEX_ENTER],
           stats[STAT_SCOPE_DENY_MUTEX_SLOWPATH],
           stats[STAT_SCOPE_DENY_MUTEX_UNLOCK],
           stats[STAT_SCOPE_DENY_FUTEX_WAIT],
           stats[STAT_SCOPE_DENY_FUTEX_WAKE]);
    printf("sync events wait/acquire/release=%llu/%llu/%llu | rwsem read_acq/write_acq/read_wait/write_wait/release=%llu/%llu/%llu/%llu/%llu\n",
           stats[STAT_SYNC_EVENT_WAIT],
           stats[STAT_SYNC_EVENT_ACQUIRE],
           stats[STAT_SYNC_EVENT_RELEASE],
           stats[STAT_RWSEM_READ_ACQUIRE],
           stats[STAT_RWSEM_WRITE_ACQUIRE],
           stats[STAT_RWSEM_READ_WAIT],
           stats[STAT_RWSEM_WRITE_WAIT],
           stats[STAT_RWSEM_RELEASE]);
    printf("extended futex shared wait/wake/key_miss=%llu/%llu/%llu pi wait/acquire/release owner_h/m/key_miss=%llu/%llu/%llu %llu/%llu/%llu requeue op/transfer/key_miss=%llu/%llu/%llu\n",
           stats[STAT_SHARED_FUTEX_WAIT],
           stats[STAT_SHARED_FUTEX_WAKE],
           stats[STAT_SHARED_FUTEX_KEY_MISS],
           stats[STAT_PI_FUTEX_WAIT],
           stats[STAT_PI_FUTEX_ACQUIRE],
           stats[STAT_PI_FUTEX_RELEASE],
           stats[STAT_PI_OWNER_HIT],
           stats[STAT_PI_OWNER_MISS],
           stats[STAT_PI_KEY_MISS],
           stats[STAT_REQUEUE_OP],
           stats[STAT_REQUEUE_TRANSFER],
           stats[STAT_REQUEUE_KEY_MISS]);
    printf("user paths rwlock read/write/wait/release=%llu/%llu/%llu/%llu spin wait/acq/release=%llu/%llu/%llu cas wait/acq/release=%llu/%llu/%llu\n",
           stats[STAT_PTHREAD_RWLOCK_READ],
           stats[STAT_PTHREAD_RWLOCK_WRITE],
           stats[STAT_PTHREAD_RWLOCK_WAIT],
           stats[STAT_PTHREAD_RWLOCK_RELEASE],
           stats[STAT_USER_SPIN_WAIT],
           stats[STAT_USER_SPIN_ACQUIRE],
           stats[STAT_USER_SPIN_RELEASE],
           stats[STAT_USER_CAS_WAIT],
           stats[STAT_USER_CAS_ACQUIRE],
           stats[STAT_USER_CAS_RELEASE]);
    printf("path actions nest/short/strong/boost_denied=%llu/%llu/%llu/%llu\n",
           stats[STAT_PATH_NEST_ACTION],
           stats[STAT_PATH_SHORT_BOOST_ACTION],
           stats[STAT_PATH_STRONG_BOOST_ACTION],
           stats[STAT_PATH_BOOST_DENIED]);
    printf("diag anchor idle/busy/occupant_unknown=%llu/%llu/%llu same_tgid/same_cgroup/different_tgid=%llu/%llu/%llu\n",
           stats[STAT_DIAG_ANCHOR_IDLE],
           stats[STAT_DIAG_ANCHOR_BUSY],
           stats[STAT_DIAG_ANCHOR_OCCUPANT_UNKNOWN],
           stats[STAT_DIAG_ANCHOR_SAME_TGID],
           stats[STAT_DIAG_ANCHOR_SAME_CGROUP],
           stats[STAT_DIAG_ANCHOR_DIFFERENT_TGID]);
    printf("diag direct local_dsq q0/q1/q2-3/q4-7/q8-15/q16-31/q32+=%llu/%llu/%llu/%llu/%llu/%llu/%llu\n",
           stats[STAT_DIAG_DIRECT_Q_0],
           stats[STAT_DIAG_DIRECT_Q_1],
           stats[STAT_DIAG_DIRECT_Q_2_3],
           stats[STAT_DIAG_DIRECT_Q_4_7],
           stats[STAT_DIAG_DIRECT_Q_8_15],
           stats[STAT_DIAG_DIRECT_Q_16_31],
           stats[STAT_DIAG_DIRECT_Q_GE_32]);
    printf("diag anchor wait count=%llu avg_us=%llu hist <=10us/<=100us/<=1ms/<=10ms/>10ms=%llu/%llu/%llu/%llu/%llu run_cpu_mismatch=%llu\n",
           stats[STAT_DIAG_ANCHOR_WAIT_COUNT],
           stats[STAT_DIAG_ANCHOR_WAIT_COUNT] ?
           stats[STAT_DIAG_ANCHOR_WAIT_SUM_NS] /
           stats[STAT_DIAG_ANCHOR_WAIT_COUNT] / 1000 : 0,
           stats[STAT_DIAG_ANCHOR_WAIT_LE_10US],
           stats[STAT_DIAG_ANCHOR_WAIT_LE_100US],
           stats[STAT_DIAG_ANCHOR_WAIT_LE_1MS],
           stats[STAT_DIAG_ANCHOR_WAIT_LE_10MS],
           stats[STAT_DIAG_ANCHOR_WAIT_GT_10MS],
           stats[STAT_DIAG_ANCHOR_RUN_CPU_MISMATCH]);
    printf("diag fallback wait count=%llu avg_us=%llu selected match/mismatch=%llu/%llu map_fail=%llu\n",
           stats[STAT_DIAG_FALLBACK_WAIT_COUNT],
           stats[STAT_DIAG_FALLBACK_WAIT_COUNT] ?
           stats[STAT_DIAG_FALLBACK_WAIT_SUM_NS] /
           stats[STAT_DIAG_FALLBACK_WAIT_COUNT] / 1000 : 0,
           stats[STAT_DIAG_FALLBACK_SELECTED_MATCH],
           stats[STAT_DIAG_FALLBACK_SELECTED_MISMATCH],
           stats[STAT_DIAG_MAP_UPDATE_FAIL]);
}

static void print_contended_tasks(struct LOCKCOOP_SKEL *skel) {
    int map_fd = bpf_map__fd(skel->maps.contended_tasks);
    __u32 key = 0, prev_key = 0;
    struct contention_info info;

    printf("--- Contended Tasks (Boosted Holders) ---\n");
    // Note: bpf_map_get_next_key might not work correctly if the first key is 0.
    // A safer but slower iteration is to iterate all possible TIDs.
    // For this example, we'll stick to the simpler approach.
    while (bpf_map_get_next_key(map_fd, &prev_key, &key) == 0) {
        if (bpf_map_lookup_elem(map_fd, &key, &info) == 0) {
            printf("  owner=%-7u waiters=%-3u boosted=%s conf=%-8s T=%-2u kind=%u delay_ewma_us=%llu revoke=%u last_ns=%llu\n",
                   key, info.waiter_count,
                   info.boosted ? "yes" : "no",
                   confidence_name(info.owner_confidence),
                   info.adaptive_threshold,
                   info.last_boost_kind,
                   (unsigned long long)(info.owner_delay_ewma_ns / 1000),
                   info.boost_revoke_pending,
                   (unsigned long long)info.last_contention_ns);
        }
        prev_key = key;
    }
    printf("-----------------------------------------\n");
}

static void print_lock_nests(struct LOCKCOOP_SKEL *skel) {
    int map_fd = bpf_map__fd(skel->maps.lock_nests);
    struct lock_key key = {}, next_key = {};
    bool have_key = false;
    struct lock_nest_info info;

    printf("--- Active Lock Nests ---\n");
    while (bpf_map_get_next_key(map_fd, have_key ? &key : NULL, &next_key) == 0) {
        key = next_key;
        have_key = true;
        if (bpf_map_lookup_elem(map_fd, &key, &info) == 0) {
            printf("  key=%-16s addr=0x%-16llx scope=0x%-16llx tag=0x%-8x anchor=%-3d holder=%-7u holder_cpu=%-3d source=%-16s seq=%llu maturity=%u/%u reject=%u expiry_ms=%llu cooldown_ns=%llu hits=%u pressure=%u\n",
                   key_kind_name(key.kind),
                   (unsigned long long)key.addr,
                   (unsigned long long)key.scope_id,
                   key.discriminator,
                   info.anchor_cpu,
                   info.last_holder_tid, info.last_holder_cpu,
                   source_name(info.source),
                   (unsigned long long)info.handoff_seq,
                   info.maturity_score, info.maturity_threshold,
                   info.maturity_rejects,
                   (unsigned long long)(info.adaptive_expiry_ns / 1000 / 1000),
                   (unsigned long long)info.cooldown_until_ns,
                   info.direct_hits,
                   info.pressure_rejects);
        }
    }
    printf("-------------------------\n");
}

static const char *diag_fallback_reason_name(__u32 reason)
{
	switch (reason) {
	case 1:
		return "cpumask";
	case 2:
		return "maturity";
	case 3:
		return "cooldown";
	case 4:
		return "pressure";
	default:
		return "unknown";
	}
}

static void print_diag_anchor_relations(struct LOCKCOOP_SKEL *skel)
{
	int map_fd = bpf_map__fd(skel->maps.diag_anchor_relations);
	struct diag_anchor_relation_key key = {}, next_key = {};
	bool have_key = false;
	__u64 count;

	printf("--- Diagnostic Anchor Occupants ---\n");
	while (bpf_map_get_next_key(map_fd, have_key ? &key : NULL,
				    &next_key) == 0) {
		key = next_key;
		have_key = true;
		if (bpf_map_lookup_elem(map_fd, &key, &count) == 0)
			printf("DIAG_ANCHOR_RELATION anchor=%d idle=%u wake_tgid=%u wake_cgroup=%llu occupant_tid=%u occupant_tgid=%u occupant_cgroup=%llu same_service=%u same_cgroup=%u count=%llu\n",
			       key.anchor_cpu, key.idle, key.wake_tgid,
			       (unsigned long long)key.wake_cgroup_id,
			       key.occupant_tid, key.occupant_tgid,
			       (unsigned long long)key.occupant_cgroup_id,
			       key.occupant_tgid &&
			       key.wake_tgid == key.occupant_tgid,
			       key.wake_cgroup_id &&
			       key.wake_cgroup_id == key.occupant_cgroup_id,
			       (unsigned long long)count);
	}
	printf("-------------------------------------\n");
}

static void print_diag_fallback_paths(struct LOCKCOOP_SKEL *skel)
{
	int map_fd = bpf_map__fd(skel->maps.diag_fallback_paths);
	struct diag_fallback_path_key key = {}, next_key = {};
	bool have_key = false;
	__u64 count;

	printf("--- Diagnostic Fallback CPUs ---\n");
	while (bpf_map_get_next_key(map_fd, have_key ? &key : NULL,
				    &next_key) == 0) {
		key = next_key;
		have_key = true;
		if (bpf_map_lookup_elem(map_fd, &key, &count) == 0)
			printf("DIAG_FALLBACK_PATH reason=%s anchor=%d selected=%d actual=%d selected_match=%u count=%llu\n",
			       diag_fallback_reason_name(key.reason), key.anchor_cpu,
			       key.selected_cpu, key.actual_cpu,
			       key.selected_cpu == key.actual_cpu,
			       (unsigned long long)count);
	}
	printf("--------------------------------\n");
}

static const char *find_default_libc(void)
{
	static const char *candidates[] = {
		"/lib/x86_64-linux-gnu/libc.so.6",
		"/usr/lib/x86_64-linux-gnu/libc.so.6",
		"/lib/aarch64-linux-gnu/libc.so.6",
		"/usr/lib/aarch64-linux-gnu/libc.so.6",
		"/lib/riscv64-linux-gnu/libc.so.6",
		"/usr/lib/riscv64-linux-gnu/libc.so.6",
		"/lib64/libc.so.6",
	};
	size_t i;

	for (i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++) {
		if (access(candidates[i], R_OK) == 0)
			return candidates[i];
	}
	return NULL;
}

static struct bpf_link *attach_symbol_uprobe(struct bpf_program *prog,
						     const char *path,
						     const char *symbol,
						     bool retprobe)
{
	struct bpf_uprobe_opts opts = {
		.sz = sizeof(opts),
		.retprobe = retprobe,
		.func_name = symbol,
	};

	return bpf_program__attach_uprobe_opts(prog, -1, path, 0, &opts);
}

static int attach_pthread_rwlock_probes(struct LOCKCOOP_SKEL *skel,
						const char *libc_path)
{
	if (!libc_path)
		return -ENOENT;
	skel->links.handle_pthread_rwlock_rdlock_enter =
		attach_symbol_uprobe(skel->progs.handle_pthread_rwlock_rdlock_enter,
				     libc_path, "pthread_rwlock_rdlock", false);
	skel->links.handle_pthread_rwlock_rdlock_exit =
		attach_symbol_uprobe(skel->progs.handle_pthread_rwlock_rdlock_exit,
				     libc_path, "pthread_rwlock_rdlock", true);
	skel->links.handle_pthread_rwlock_wrlock_enter =
		attach_symbol_uprobe(skel->progs.handle_pthread_rwlock_wrlock_enter,
				     libc_path, "pthread_rwlock_wrlock", false);
	skel->links.handle_pthread_rwlock_wrlock_exit =
		attach_symbol_uprobe(skel->progs.handle_pthread_rwlock_wrlock_exit,
				     libc_path, "pthread_rwlock_wrlock", true);
	skel->links.handle_pthread_rwlock_unlock_enter =
		attach_symbol_uprobe(skel->progs.handle_pthread_rwlock_unlock_enter,
				     libc_path, "pthread_rwlock_unlock", false);
	skel->links.handle_pthread_rwlock_unlock_exit =
		attach_symbol_uprobe(skel->progs.handle_pthread_rwlock_unlock_exit,
				     libc_path, "pthread_rwlock_unlock", true);
	if (!skel->links.handle_pthread_rwlock_rdlock_enter ||
	    !skel->links.handle_pthread_rwlock_rdlock_exit ||
	    !skel->links.handle_pthread_rwlock_wrlock_enter ||
	    !skel->links.handle_pthread_rwlock_wrlock_exit ||
	    !skel->links.handle_pthread_rwlock_unlock_enter ||
	    !skel->links.handle_pthread_rwlock_unlock_exit)
		return -errno;
	return 0;
}

static const char *find_default_user_sync(char *path, size_t path_size)
{
	char *slash;
	ssize_t len;

	len = readlink("/proc/self/exe", path, path_size - 1);
	if (len <= 0 || (size_t)len >= path_size - 1)
		return NULL;
	path[len] = '\0';
	slash = strrchr(path, '/');
	if (!slash)
		return NULL;
	if ((size_t)(slash - path) + sizeof("/liblockcoop_sync.so") >
	    path_size)
		return NULL;
	strcpy(slash, "/liblockcoop_sync.so");
	return access(path, R_OK) == 0 ? path : NULL;
}

static int attach_user_sync_probes(struct LOCKCOOP_SKEL *skel,
					   const char *library_path)
{
	skel->links.handle_user_spin_wait =
		attach_symbol_uprobe(skel->progs.handle_user_spin_wait,
				     library_path, "lockcoop_spin_wait", false);
	skel->links.handle_user_spin_acquired =
		attach_symbol_uprobe(skel->progs.handle_user_spin_acquired,
				     library_path, "lockcoop_spin_acquired", false);
	skel->links.handle_user_spin_release =
		attach_symbol_uprobe(skel->progs.handle_user_spin_release,
				     library_path, "lockcoop_spin_release", false);
	skel->links.handle_user_cas_wait =
		attach_symbol_uprobe(skel->progs.handle_user_cas_wait,
				     library_path, "lockcoop_cas_wait", false);
	skel->links.handle_user_cas_acquired =
		attach_symbol_uprobe(skel->progs.handle_user_cas_acquired,
				     library_path, "lockcoop_cas_acquired", false);
	skel->links.handle_user_cas_release =
		attach_symbol_uprobe(skel->progs.handle_user_cas_release,
				     library_path, "lockcoop_cas_release", false);
	if (!skel->links.handle_user_spin_wait ||
	    !skel->links.handle_user_spin_acquired ||
	    !skel->links.handle_user_spin_release ||
	    !skel->links.handle_user_cas_wait ||
	    !skel->links.handle_user_cas_acquired ||
	    !skel->links.handle_user_cas_release)
		return -errno;
	return 0;
}

enum {
	OPT_BOOST_PROFILE = 1000,
	OPT_BOOST_GRANT_US,
	OPT_BOOST_CAP_US,
	OPT_BOOST_WINDOW_MS,
	OPT_BOOST_TASK_BUDGET_PCT,
	OPT_BOOST_LOCK_BUDGET_PCT,
	OPT_BOOST_CGROUP_BUDGET_PCT,
	OPT_BOOST_TARGET_DELAY_US,
	OPT_BOOST_CONFIDENCE_GATE,
	OPT_BOOST_MAX_CONTINUOUS_US,
	OPT_ALLOW_INF,
	OPT_STATS_ONLY,
	OPT_ANCHOR_MATURITY_PROFILE,
	OPT_ANCHOR_MATURITY_THRESHOLD,
	OPT_ANCHOR_MATURITY_STRONG_THRESHOLD,
	OPT_ANCHOR_MATURITY_WEAK_THRESHOLD,
	OPT_ANCHOR_MATURITY_MAX_SCORE,
	OPT_ANCHOR_MATURITY_WINDOW_MS,
	OPT_EVIDENCE_SCOPE,
	OPT_EVIDENCE_TGID,
	OPT_EVIDENCE_CGROUP_ID,
	OPT_EVIDENCE_CGROUP_PATH,
	OPT_EVIDENCE_SCOPE_STATS,
	OPT_PTHREAD_RWLOCK_LIB,
	OPT_USER_SYNC_LIB,
	OPT_NO_USER_PROBES,
	OPT_SYNC_PATH_PROFILE,
	OPT_SENSOR_ONLY,
	OPT_SWITCH_PARTIAL,
};

enum sync_path_profile {
	SYNC_PATH_NONE = 0,
	SYNC_PATH_MUTEX = 1,
	SYNC_PATH_FUTEX = 2,
	SYNC_PATH_LEGACY = 3,
	SYNC_PATH_KERNEL = 4,
	SYNC_PATH_FULL = 5,
};

static int validate_pthread_owner_offset(size_t offset)
{
#ifdef __GLIBC__
	static const int mutex_types[] = {
		PTHREAD_MUTEX_NORMAL,
		PTHREAD_MUTEX_RECURSIVE,
		PTHREAD_MUTEX_ERRORCHECK,
	};
	uint32_t tid = (uint32_t)syscall(SYS_gettid);
	size_t i;

	if (offset > sizeof(pthread_mutex_t) - sizeof(uint32_t))
		return -ERANGE;

	for (i = 0; i < sizeof(mutex_types) / sizeof(mutex_types[0]); i++) {
		pthread_mutexattr_t attr;
		pthread_mutex_t mutex;
		uint32_t owner = 0;
		int err;

		err = pthread_mutexattr_init(&attr);
		if (err)
			return -err;
		err = pthread_mutexattr_settype(&attr, mutex_types[i]);
		if (err) {
			pthread_mutexattr_destroy(&attr);
			return -err;
		}
		err = pthread_mutex_init(&mutex, &attr);
		pthread_mutexattr_destroy(&attr);
		if (err)
			return -err;

		err = pthread_mutex_lock(&mutex);
		if (err) {
			pthread_mutex_destroy(&mutex);
			return -err;
		}
		memcpy(&owner, (const char *)&mutex + offset, sizeof(owner));
		if (owner != tid) {
			pthread_mutex_unlock(&mutex);
			pthread_mutex_destroy(&mutex);
			return -EINVAL;
		}

		err = pthread_mutex_unlock(&mutex);
		if (err) {
			pthread_mutex_destroy(&mutex);
			return -err;
		}
		memcpy(&owner, (const char *)&mutex + offset, sizeof(owner));
		err = pthread_mutex_destroy(&mutex);
		if (err)
			return -err;
		if (owner != 0)
			return -EINVAL;
	}

	return 0;
#else
	(void)offset;
	return -EOPNOTSUPP;
#endif
}

static int configure_pthread_owner_offset(struct LOCKCOOP_SKEL *skel,
					  bool override_set,
					  int override_offset)
{
#ifdef __GLIBC__
	size_t detected = offsetof(pthread_mutex_t, __data.__owner);
	int offset = override_set ? override_offset : (int)detected;
	int err;

	if (offset == -1) {
		skel->rodata->pthread_owner_offset = -1;
		printf("pthread owner-word layout: disabled\n");
		return 0;
	}
	if (offset < 0)
		return -EINVAL;

	err = validate_pthread_owner_offset((size_t)offset);
	if (err) {
		fprintf(stderr,
			"pthread owner-word layout validation failed: glibc=%s sizeof(pthread_mutex_t)=%zu offset=%d: %s\n",
			gnu_get_libc_version(), sizeof(pthread_mutex_t), offset,
			strerror(-err));
		return err;
	}

	skel->rodata->pthread_owner_offset = offset;
	printf("pthread owner-word layout: glibc=%s sizeof(pthread_mutex_t)=%zu offset=%d validation=passed%s\n",
	       gnu_get_libc_version(), sizeof(pthread_mutex_t), offset,
	       override_set ? " source=override" : " source=auto");
	return 0;
#else
	if (override_set && override_offset != -1) {
		fprintf(stderr,
			"pthread owner-word layout validation requires glibc/NPTL\n");
		return -EOPNOTSUPP;
	}
	skel->rodata->pthread_owner_offset = -1;
	printf("pthread owner-word layout: unavailable for this libc\n");
	return 0;
#endif
}

static int parse_sync_path_profile(const char *s)
{
	if (!strcmp(s, "none"))
		return SYNC_PATH_NONE;
	if (!strcmp(s, "mutex"))
		return SYNC_PATH_MUTEX;
	if (!strcmp(s, "futex"))
		return SYNC_PATH_FUTEX;
	if (!strcmp(s, "legacy"))
		return SYNC_PATH_LEGACY;
	if (!strcmp(s, "kernel"))
		return SYNC_PATH_KERNEL;
	if (!strcmp(s, "full"))
		return SYNC_PATH_FULL;
	return -1;
}

static const char *sync_path_profile_name(int profile)
{
	if (profile == SYNC_PATH_NONE)
		return "none";
	if (profile == SYNC_PATH_MUTEX)
		return "mutex";
	if (profile == SYNC_PATH_FUTEX)
		return "futex";
	if (profile == SYNC_PATH_LEGACY)
		return "legacy";
	if (profile == SYNC_PATH_KERNEL)
		return "kernel";
	return "full";
}

static void disable_kernel_mutex_paths(struct LOCKCOOP_SKEL *skel)
{
	bpf_program__set_autoload(skel->progs.handle_mutex_lock_enter, false);
	bpf_program__set_autoload(skel->progs.handle_mutex_lock_exit, false);
	bpf_program__set_autoload(skel->progs.handle_mutex_unlock, false);
	bpf_program__set_autoload(skel->progs.handle_mutex_contended, false);
}

static void disable_futex_tracepoint_paths(struct LOCKCOOP_SKEL *skel)
{
	bpf_program__set_autoload(skel->progs.handle_futex_enter, false);
	bpf_program__set_autoload(skel->progs.handle_futex_exit, false);
}

static void disable_legacy_paths(struct LOCKCOOP_SKEL *skel)
{
	disable_kernel_mutex_paths(skel);
	disable_futex_tracepoint_paths(skel);
}

static void disable_extended_kernel_paths(struct LOCKCOOP_SKEL *skel)
{
	bpf_program__set_autoload(skel->progs.handle_down_read_enter, false);
	bpf_program__set_autoload(skel->progs.handle_down_read_exit, false);
	bpf_program__set_autoload(skel->progs.handle_down_write_enter, false);
	bpf_program__set_autoload(skel->progs.handle_down_write_exit, false);
	bpf_program__set_autoload(skel->progs.handle_rwsem_read_contended, false);
	bpf_program__set_autoload(skel->progs.handle_rwsem_write_contended, false);
	bpf_program__set_autoload(skel->progs.handle_up_read, false);
	bpf_program__set_autoload(skel->progs.handle_up_write, false);
	bpf_program__set_autoload(skel->progs.handle_get_futex_key_exit, false);
	bpf_program__set_autoload(skel->progs.handle_futex_q_lock, false);
	bpf_program__set_autoload(skel->progs.handle_futex_lock_pi_atomic_enter,
				  false);
	bpf_program__set_autoload(skel->progs.handle_futex_lock_pi_atomic_exit,
				  false);
	bpf_program__set_autoload(skel->progs.handle_futex_wake_mark, false);
}

int main(int argc, char **argv) {
	struct LOCKCOOP_SKEL *skel;
	struct bpf_link *link;
	int opt;
	__u64 ecode;
	int profile = BOOST_PROFILE_BOUNDED;
	bool profile_set = false;
	bool grant_set = false;
	bool cap_set = false;
	bool task_budget_set = false;
	bool lock_budget_set = false;
	bool cgroup_budget_set = false;
	bool max_continuous_set = false;
	bool stats_only = false;
	bool user_probes = true;
	bool sensor_only = false;
	bool switch_partial = false;
	bool owner_offset_set = false;
	int owner_offset_override = -1;
	int sync_path_profile = SYNC_PATH_FULL;
	unsigned int snapshot_seq = 0;
	const char *pthread_rwlock_lib = NULL;
	const char *user_sync_lib = NULL;
	char default_user_sync_lib[PATH_MAX];
	int maturity_profile = ANCHOR_MATURITY_OFF;
	struct evidence_scope_config evidence_scope = {
		.mode = EVIDENCE_SCOPE_ALL,
	};
	static const struct option long_options[] = {
		{ "boost-profile", required_argument, NULL, OPT_BOOST_PROFILE },
		{ "boost-grant-us", required_argument, NULL, OPT_BOOST_GRANT_US },
		{ "boost-cap-us", required_argument, NULL, OPT_BOOST_CAP_US },
		{ "boost-window-ms", required_argument, NULL, OPT_BOOST_WINDOW_MS },
		{ "boost-task-budget-pct", required_argument, NULL, OPT_BOOST_TASK_BUDGET_PCT },
		{ "boost-lock-budget-pct", required_argument, NULL, OPT_BOOST_LOCK_BUDGET_PCT },
		{ "boost-cgroup-budget-pct", required_argument, NULL, OPT_BOOST_CGROUP_BUDGET_PCT },
		{ "boost-target-delay-us", required_argument, NULL, OPT_BOOST_TARGET_DELAY_US },
		{ "boost-confidence-gate", required_argument, NULL, OPT_BOOST_CONFIDENCE_GATE },
		{ "boost-max-continuous-us", required_argument, NULL, OPT_BOOST_MAX_CONTINUOUS_US },
		{ "allow-inf", no_argument, NULL, OPT_ALLOW_INF },
		{ "stats-only", no_argument, NULL, OPT_STATS_ONLY },
		{ "anchor-maturity-profile", required_argument, NULL, OPT_ANCHOR_MATURITY_PROFILE },
		{ "anchor-maturity-threshold", required_argument, NULL, OPT_ANCHOR_MATURITY_THRESHOLD },
		{ "anchor-maturity-strong-threshold", required_argument, NULL, OPT_ANCHOR_MATURITY_STRONG_THRESHOLD },
		{ "anchor-maturity-weak-threshold", required_argument, NULL, OPT_ANCHOR_MATURITY_WEAK_THRESHOLD },
		{ "anchor-maturity-max-score", required_argument, NULL, OPT_ANCHOR_MATURITY_MAX_SCORE },
		{ "anchor-maturity-window-ms", required_argument, NULL, OPT_ANCHOR_MATURITY_WINDOW_MS },
		{ "evidence-scope", required_argument, NULL, OPT_EVIDENCE_SCOPE },
		{ "evidence-tgid", required_argument, NULL, OPT_EVIDENCE_TGID },
		{ "evidence-cgroup-id", required_argument, NULL, OPT_EVIDENCE_CGROUP_ID },
		{ "evidence-cgroup-path", required_argument, NULL, OPT_EVIDENCE_CGROUP_PATH },
		{ "evidence-scope-stats", no_argument, NULL, OPT_EVIDENCE_SCOPE_STATS },
		{ "pthread-rwlock-lib", required_argument, NULL, OPT_PTHREAD_RWLOCK_LIB },
		{ "user-sync-lib", required_argument, NULL, OPT_USER_SYNC_LIB },
			{ "no-user-probes", no_argument, NULL, OPT_NO_USER_PROBES },
			{ "sync-path-profile", required_argument, NULL,
			  OPT_SYNC_PATH_PROFILE },
		{ "sensor-only", no_argument, NULL, OPT_SENSOR_ONLY },
		{ "switch-partial", no_argument, NULL, OPT_SWITCH_PARTIAL },
		{},
	};

	libbpf_set_print(libbpf_print_fn);
	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);
	signal(SIGUSR1, snapshot_handler);

restart:
	skel = SCX_OPS_OPEN(LOCKCOOP_OPS, LOCKCOOP_SKEL);
    if (!skel) SCX_BUG("Failed to open BPF skeleton");

	optind = 1;
	while ((opt = getopt_long(argc, argv, "fvDAt:d:e:q:O:B:M:Q:R:P:C:N:X:h",
				  long_options, NULL)) != -1) {
		switch (opt) {
		case 'f':
			skel->rodata->fifo_sched = true;
			break;
		case 'v':
			verbose = true;
			break;
		case 'D':
			skel->rodata->diagnostic_mode = true;
			skel->rodata->stats_enabled = true;
			break;
		case 'A':
			skel->rodata->adaptive_nest = true;
			break;
        case 't':
            skel->rodata->high_contention_threshold = atoi(optarg);
            break;
        case 'd':
            skel->rodata->decay_timeout_ns = (__u64)atoi(optarg) * 1000 * 1000;
            break;
        case 'e':
            skel->rodata->nest_expiry_ns = (__u64)atoi(optarg) * 1000 * 1000;
            break;
        case 'q':
            skel->rodata->weak_anchor_queue_limit = atoi(optarg);
            break;
        case 'O': {
			char *end = NULL;
			long offset;

			errno = 0;
			offset = strtol(optarg, &end, 10);
			if (errno || !end || *end || offset < -1 || offset > INT_MAX) {
				fprintf(stderr, "invalid pthread owner-word offset: %s\n",
					optarg);
				return 1;
			}
			owner_offset_override = (int)offset;
			owner_offset_set = true;
            break;
		}
        case 'B':
            skel->rodata->owner_boost_slices = atoi(optarg);
            break;
        case 'M':
            skel->rodata->owner_boost_max_slices = atoi(optarg);
            break;
        case 'Q':
            skel->rodata->adaptive_anchor_queue_limit = atoi(optarg);
            break;
        case 'R':
            skel->rodata->adaptive_anchor_reject_threshold = atoi(optarg);
            break;
        case 'P':
            skel->rodata->adaptive_anchor_promote_threshold = atoi(optarg);
            break;
        case 'C':
            skel->rodata->adaptive_anchor_cooldown_ns = (__u64)atoi(optarg) * 1000 * 1000;
            break;
        case 'N':
            skel->rodata->adaptive_min_nest_expiry_ns = (__u64)atoi(optarg) * 1000 * 1000;
            break;
        case 'X':
            skel->rodata->adaptive_max_nest_expiry_ns = (__u64)atoi(optarg) * 1000 * 1000;
            break;
		case OPT_BOOST_PROFILE:
			profile = parse_boost_profile(optarg);
			if (profile < 0) {
				fprintf(stderr, "unknown boost profile: %s\n", optarg);
				return 1;
			}
			skel->rodata->boost_profile = profile;
			profile_set = true;
			break;
		case OPT_BOOST_GRANT_US:
			skel->rodata->boost_grant_ns = (__u64)atoi(optarg) * 1000;
			grant_set = true;
			break;
		case OPT_BOOST_CAP_US:
			skel->rodata->boost_cap_ns = (__u64)atoi(optarg) * 1000;
			cap_set = true;
			break;
		case OPT_BOOST_WINDOW_MS:
			skel->rodata->boost_budget_window_ns = (__u64)atoi(optarg) * 1000 * 1000;
			break;
		case OPT_BOOST_TASK_BUDGET_PCT:
			skel->rodata->boost_task_budget_pct = atoi(optarg);
			task_budget_set = true;
			break;
		case OPT_BOOST_LOCK_BUDGET_PCT:
			skel->rodata->boost_lock_budget_pct = atoi(optarg);
			lock_budget_set = true;
			break;
		case OPT_BOOST_CGROUP_BUDGET_PCT:
			skel->rodata->boost_cgroup_budget_pct = atoi(optarg);
			cgroup_budget_set = true;
			break;
		case OPT_BOOST_TARGET_DELAY_US:
			skel->rodata->boost_target_delay_ns = (__u64)atoi(optarg) * 1000;
			break;
		case OPT_BOOST_CONFIDENCE_GATE: {
			int gate = parse_confidence_gate(optarg);

			if (gate < 0) {
				fprintf(stderr, "unknown boost confidence gate: %s\n", optarg);
				return 1;
			}
			skel->rodata->boost_confidence_gate = gate;
			break;
		}
		case OPT_BOOST_MAX_CONTINUOUS_US:
			skel->rodata->boost_max_continuous_ns = (__u64)atoi(optarg) * 1000;
			max_continuous_set = true;
			break;
		case OPT_ALLOW_INF:
			skel->rodata->boost_allow_inf = true;
			break;
		case OPT_STATS_ONLY:
			stats_only = true;
			skel->rodata->stats_enabled = true;
			break;
		case OPT_ANCHOR_MATURITY_PROFILE:
			maturity_profile = parse_anchor_maturity_profile(optarg);
			if (maturity_profile < 0) {
				fprintf(stderr, "unknown anchor maturity profile: %s\n", optarg);
				return 1;
			}
			skel->rodata->anchor_maturity_profile = maturity_profile;
			if (maturity_profile != ANCHOR_MATURITY_OFF)
				skel->rodata->stats_enabled = true;
			break;
		case OPT_ANCHOR_MATURITY_THRESHOLD:
			skel->rodata->anchor_maturity_threshold = atoi(optarg);
			break;
		case OPT_ANCHOR_MATURITY_STRONG_THRESHOLD:
			skel->rodata->anchor_maturity_strong_threshold = atoi(optarg);
			break;
		case OPT_ANCHOR_MATURITY_WEAK_THRESHOLD:
			skel->rodata->anchor_maturity_weak_threshold = atoi(optarg);
			break;
		case OPT_ANCHOR_MATURITY_MAX_SCORE:
			skel->rodata->anchor_maturity_max_score = atoi(optarg);
			break;
		case OPT_ANCHOR_MATURITY_WINDOW_MS:
			skel->rodata->anchor_maturity_window_ns =
				(__u64)atoi(optarg) * 1000 * 1000;
			break;
		case OPT_EVIDENCE_SCOPE:
			evidence_scope.mode = parse_evidence_scope(optarg);
			if (evidence_scope.mode < 0) {
				fprintf(stderr, "unknown evidence scope: %s\n", optarg);
				return 1;
			}
			evidence_scope.mode_set = true;
			skel->rodata->evidence_scope_mode = evidence_scope.mode;
			break;
		case OPT_EVIDENCE_TGID:
			if (append_tgid(&evidence_scope, optarg)) {
				fprintf(stderr, "invalid or excessive evidence TGID: %s\n", optarg);
				return 1;
			}
			break;
		case OPT_EVIDENCE_CGROUP_ID: {
			u64 id;

			if (parse_u64_arg(optarg, &id) ||
			    append_cgroup_id(&evidence_scope, id)) {
				fprintf(stderr, "invalid or excessive evidence cgroup id: %s\n", optarg);
				return 1;
			}
			break;
		}
		case OPT_EVIDENCE_CGROUP_PATH: {
			u64 id;

			if (cgroup_path_to_id(optarg, &id) ||
			    append_cgroup_id(&evidence_scope, id)) {
				fprintf(stderr, "invalid cgroup v2 path: %s (%s)\n",
					optarg, strerror(errno));
				return 1;
			}
			printf("evidence cgroup path %s -> id %llu\n", optarg,
			       (unsigned long long)id);
			break;
		}
		case OPT_EVIDENCE_SCOPE_STATS:
			evidence_scope.stats = true;
			skel->rodata->evidence_scope_stats = true;
			skel->rodata->stats_enabled = true;
			break;
		case OPT_PTHREAD_RWLOCK_LIB:
			pthread_rwlock_lib = optarg;
			break;
		case OPT_USER_SYNC_LIB:
			user_sync_lib = optarg;
			break;
		case OPT_NO_USER_PROBES:
			user_probes = false;
			break;
		case OPT_SYNC_PATH_PROFILE:
			sync_path_profile = parse_sync_path_profile(optarg);
			if (sync_path_profile < 0) {
				fprintf(stderr, "invalid sync path profile: %s\n", optarg);
				return 1;
			}
			break;
		case OPT_SENSOR_ONLY:
			sensor_only = true;
			break;
		case OPT_SWITCH_PARTIAL:
			switch_partial = true;
			break;
		default:
			fprintf(stderr, help_fmt, basename(argv[0]));
			return opt != 'h';
		}
	}
	skel->rodata->nest_enabled = LOCKCOOP_FORCE_NEST;
	skel->rodata->boost_enabled = LOCKCOOP_FORCE_BOOST;
	if (sensor_only) {
		skel->rodata->nest_enabled = false;
		skel->rodata->boost_enabled = false;
		skel->rodata->sensor_only_mode = true;
		skel->rodata->boost_profile = BOOST_PROFILE_NONE;
		profile = BOOST_PROFILE_NONE;
		profile_set = false;
	}
	if (!LOCKCOOP_FORCE_BOOST) {
		skel->rodata->boost_profile = BOOST_PROFILE_NONE;
		profile = BOOST_PROFILE_NONE;
		profile_set = false;
	}
	if (sync_path_profile == SYNC_PATH_NONE) {
		disable_legacy_paths(skel);
		disable_extended_kernel_paths(skel);
	} else if (sync_path_profile == SYNC_PATH_MUTEX) {
		disable_futex_tracepoint_paths(skel);
		disable_extended_kernel_paths(skel);
	} else if (sync_path_profile == SYNC_PATH_FUTEX) {
		disable_kernel_mutex_paths(skel);
		disable_extended_kernel_paths(skel);
	} else if (sync_path_profile == SYNC_PATH_LEGACY) {
		disable_extended_kernel_paths(skel);
	}
	if (sync_path_profile != SYNC_PATH_FULL)
		user_probes = false;

	if (profile_set && profile == BOOST_PROFILE_INF &&
	    !skel->rodata->boost_allow_inf) {
		fprintf(stderr, "--boost-profile inf requires --allow-inf\n");
		return 1;
	}
	if (profile_set && profile == BOOST_PROFILE_SHORT) {
		if (!grant_set)
			skel->rodata->boost_grant_ns = 500 * 1000;
		if (!cap_set)
			skel->rodata->boost_cap_ns = 1000 * 1000;
	}
	if (profile_set && profile == BOOST_PROFILE_ADAPTIVE) {
		if (!grant_set)
			skel->rodata->boost_grant_ns = 1000 * 1000;
		if (!cap_set)
			skel->rodata->boost_cap_ns = 4 * 1000 * 1000;
		if (!task_budget_set)
			skel->rodata->boost_task_budget_pct = 20;
		if (!lock_budget_set)
			skel->rodata->boost_lock_budget_pct = 40;
		if (!cgroup_budget_set)
			skel->rodata->boost_cgroup_budget_pct = 40;
		if (!max_continuous_set)
				skel->rodata->boost_max_continuous_ns = 5 * 1000 * 1000;
	}
	if (validate_evidence_scope(&evidence_scope)) {
		fprintf(stderr,
			"evidence scope requires a non-empty, non-conflicting allow set\n");
		return 1;
	}
	if (configure_pthread_owner_offset(skel, owner_offset_set,
					   owner_offset_override))
		return 1;

	if (!skel->rodata->diagnostic_mode) {
		bpf_program__set_autoload(skel->progs.handle_sched_wakeup, false);
	}
	bpf_program__set_autoattach(
		skel->progs.handle_pthread_rwlock_rdlock_enter, false);
	bpf_program__set_autoattach(
		skel->progs.handle_pthread_rwlock_rdlock_exit, false);
	bpf_program__set_autoattach(
		skel->progs.handle_pthread_rwlock_wrlock_enter, false);
	bpf_program__set_autoattach(
		skel->progs.handle_pthread_rwlock_wrlock_exit, false);
	bpf_program__set_autoattach(
		skel->progs.handle_pthread_rwlock_unlock_enter, false);
	bpf_program__set_autoattach(
		skel->progs.handle_pthread_rwlock_unlock_exit, false);
	bpf_program__set_autoattach(skel->progs.handle_user_spin_wait, false);
	bpf_program__set_autoattach(skel->progs.handle_user_spin_acquired, false);
	bpf_program__set_autoattach(skel->progs.handle_user_spin_release, false);
	bpf_program__set_autoattach(skel->progs.handle_user_cas_wait, false);
	bpf_program__set_autoattach(skel->progs.handle_user_cas_acquired, false);
	bpf_program__set_autoattach(skel->progs.handle_user_cas_release, false);
	if (switch_partial)
		skel->struct_ops.LOCKCOOP_OPS->flags |= SCX_OPS_SWITCH_PARTIAL;

	SCX_OPS_LOAD(skel, LOCKCOOP_OPS, LOCKCOOP_SKEL, uei);
	if (populate_evidence_scope_maps(skel, &evidence_scope))
		SCX_BUG("Failed to populate evidence scope allow map");
	link = SCX_OPS_ATTACH(skel, LOCKCOOP_OPS, LOCKCOOP_SKEL);
    if (!link) SCX_BUG("Failed to attach BPF program");
	if (user_probes) {
		if (!pthread_rwlock_lib)
			pthread_rwlock_lib = find_default_libc();
		if (attach_pthread_rwlock_probes(skel, pthread_rwlock_lib))
			SCX_BUG("Failed to attach pthread rwlock probes to %s",
				pthread_rwlock_lib ? pthread_rwlock_lib : "<not found>");
		if (!user_sync_lib)
			user_sync_lib = find_default_user_sync(default_user_sync_lib,
						       sizeof(default_user_sync_lib));
		if (!user_sync_lib || attach_user_sync_probes(skel, user_sync_lib))
			SCX_BUG("Failed to attach user sync ABI probes to %s",
				user_sync_lib ? user_sync_lib : "<not found>");
	}

	    printf("%s scheduler attached. threshold=%u decay_ms=%llu expiry_ms=%llu weak_queue=%u owner_offset=%d boost_slices=%u/%u adaptive=%s adaptive_queue=%u reject=%u promote=%u cooldown_ms=%llu min_expiry_ms=%llu max_expiry_ms=%llu maturity=%s threshold=%u strong/weak=%u/%u scope=%s\n",
	           LOCKCOOP_SCHED_LABEL,
           skel->rodata->high_contention_threshold,
           (unsigned long long)(skel->rodata->decay_timeout_ns / 1000 / 1000),
           (unsigned long long)(skel->rodata->nest_expiry_ns / 1000 / 1000),
           skel->rodata->weak_anchor_queue_limit,
           skel->rodata->pthread_owner_offset,
           skel->rodata->owner_boost_slices,
           skel->rodata->owner_boost_max_slices,
           skel->rodata->adaptive_nest ? "on" : "off",
           skel->rodata->adaptive_anchor_queue_limit,
           skel->rodata->adaptive_anchor_reject_threshold,
           skel->rodata->adaptive_anchor_promote_threshold,
           (unsigned long long)(skel->rodata->adaptive_anchor_cooldown_ns / 1000 / 1000),
           (unsigned long long)(skel->rodata->adaptive_min_nest_expiry_ns / 1000 / 1000),
           (unsigned long long)(skel->rodata->adaptive_max_nest_expiry_ns / 1000 / 1000),
           anchor_maturity_profile_name(skel->rodata->anchor_maturity_profile),
           skel->rodata->anchor_maturity_threshold,
           skel->rodata->anchor_maturity_strong_threshold,
           skel->rodata->anchor_maturity_weak_threshold,
	           evidence_scope_name(skel->rodata->evidence_scope_mode));
	printf("Sync path profile=%s user_probes=%s actions=%s switch=%s\n",
	       sync_path_profile_name(sync_path_profile), user_probes ? "on" : "off",
	       sensor_only ? "off" : "on", switch_partial ? "partial" : "all");
    print_evidence_scope_config(&evidence_scope);
    printf("LockBoost profile=%s grant_us=%llu cap_us=%llu window_ms=%llu budget_pct task/lock/cgroup=%u/%u/%u target_delay_us=%llu conf_gate=%s max_cont_us=%llu allow_inf=%s\n",
           boost_profile_name(skel->rodata->boost_profile),
           (unsigned long long)(skel->rodata->boost_grant_ns / 1000),
           (unsigned long long)(skel->rodata->boost_cap_ns / 1000),
           (unsigned long long)(skel->rodata->boost_budget_window_ns / 1000 / 1000),
           skel->rodata->boost_task_budget_pct,
           skel->rodata->boost_lock_budget_pct,
           skel->rodata->boost_cgroup_budget_pct,
           (unsigned long long)(skel->rodata->boost_target_delay_ns / 1000),
           confidence_name(skel->rodata->boost_confidence_gate),
           (unsigned long long)(skel->rodata->boost_max_continuous_ns / 1000),
           skel->rodata->boost_allow_inf ? "yes" : "no");
    printf("Monitoring verified-owner progress and handoff locality...\n");

	while (!exit_req && !UEI_EXITED(skel, uei)) {
		__u64 stats[STAT_MAX];
		bool diagnostics = skel->rodata->diagnostic_mode;
		bool full_stats = diagnostics || stats_only || evidence_scope.stats;

		if (!stats_only)
			printf("\033[2J\033[H");
		read_stats(skel, stats, full_stats ? STAT_MAX : 2);
		if (full_stats) {
			if (snapshot_req) {
				printf("counter snapshot %u\n", ++snapshot_seq);
				snapshot_req = 0;
			}
			print_stats(stats);
			if (diagnostics) {
				print_contended_tasks(skel);
				printf("\n");
				print_lock_nests(skel);
			}
		} else {
			printf("queues local=%-10llu global=%-10llu\n",
			       stats[STAT_LOCAL], stats[STAT_GLOBAL]);
		}
		fflush(stdout);
		sleep(1);
	}
	if (stats_only) {
		__u64 stats[STAT_MAX];

		read_stats(skel, stats, STAT_MAX);
		printf("final aggregate counters:\n");
		print_stats(stats);
		if (skel->rodata->diagnostic_mode) {
			print_diag_anchor_relations(skel);
			print_diag_fallback_paths(skel);
		}
		fflush(stdout);
	}

	bpf_link__destroy(link);
	ecode = UEI_REPORT(skel, uei);
	LOCKCOOP_DESTROY(skel);

	if (UEI_ECODE_RESTART(ecode))
		goto restart;
	return 0;
}
