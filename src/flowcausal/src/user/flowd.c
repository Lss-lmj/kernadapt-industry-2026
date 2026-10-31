/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
/*
 * flowd.c
 *
 * No-Python FlowCausal runtime daemon.
 *
 * flowd intentionally does not poll /proc and does not call bpftool.  It can
 * run as a pure session registrar or as a small C control plane that consumes
 * FlowCausal ring-buffer events and refreshes task/session hints.  Per-task
 * inheritance, runnable accounting, and scheduling actions remain in
 * BPF/sched_ext.
 */
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <flowcausal.h>

#define U64(x) ((uint64_t)(x))
#define FLOWD_SLICE_DFL_NS 20000000ULL

static volatile sig_atomic_t exit_req;

static const char *pin_dir = "/sys/fs/bpf/flowcausal";
static const char *session_name = "flowd:default";
static __u32 root_pid;
static __u32 root_tgid;
static __u32 root_ppid;
static __u32 min_confidence = 75;
static __u32 assist_confidence = 80;
static __u32 max_frontier_width = 1;
static __u32 max_vtime_boost_ns = 500000;
static __u64 policy_window_ms = 100;
static __u64 boost_budget_ms = 10;
static __u64 ttl_ms = 1000;
static __s32 anchor_cpu = -1;
static bool root_frontier;
static __u32 propagation_depth;
static bool width_control;
static bool enable_events;
static bool control_plane;
static bool assist_bpf_auto;
static bool cxx_only_session_policy;
static bool once;
static bool affinity_by_session;
static bool python_affinity_by_session;
static bool critical_affinity_by_session;
static bool balanced_affinity_by_session;
static bool affinity_release_after_first_fork;
static bool affinity_release_package_only;
static bool affinity_release_done;
static __u64 affinity_release_start_ns;
static __u64 affinity_release_after_ns = 20000000ULL;
static cpu_set_t affinity_restore_mask;
static bool affinity_restore_mask_valid;
static const char *stage_state_file;
static __u64 stage_state_poll_ms = 50;
static __u64 stage_state_cleanup_window_ms = 5000;
static bool stage_state_cleanup_marked;

static int control_task_fd = -1;
static int control_policy_fd = -1;
static __u64 control_session_id;
static __u64 control_updates;
static __u64 stage_state_updates;

static void sigint_handler(int sig)
{
	(void)sig;
	exit_req = 1;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s --root-pid PID [--session NAME] [options]\n"
		"\n"
		"Options:\n"
		"  --pin-dir PATH           bpffs directory containing FlowCausal maps\n"
		"                           default: /sys/fs/bpf/flowcausal\n"
		"  --session NAME           stable session name, default: flowd:default\n"
		"  --confidence N           min confidence, default: 75\n"
		"  --max-frontier-width N   policy width, default: 1\n"
		"  --max-vtime-boost-us US  max vtime boost, default: 500\n"
		"  --policy-window-ms MS    policy window, default: 100\n"
		"  --boost-budget-ms MS     boost budget, default: 10\n"
		"  --ttl-ms MS              policy TTL, 0 disables expiry, default: 1000\n"
		"  --anchor-cpu CPU         optional locality anchor, default: -1\n"
		"  --frontier               mark the registered root as a frontier task\n"
		"  --propagation-depth N    inherit frontier state across N fork levels\n"
		"  --width-control          enforce max-frontier-width in BPF\n"
		"  --control-plane          run the C online control plane: consume BPF\n"
		"                           events and update task hints without Python\n"
		"  --assist-bpf-auto       BPF-main/C-assist mode: consume BPF events and\n"
		"                           only add conservative cmdline-derived hints that\n"
		"                           improve the in-kernel auto frontier classifier\n"
		"  --cxx-only-session-policy\n"
		"                           for known harness sessions, register only the\n"
		"                           C++ build-heavy session with FlowCausal\n"
		"                           frontier/locality hints; Python/package stay\n"
		"                           tracked but use the shared path\n"
		"  --affinity-by-session   set root CPU affinity once by known harness\n"
		"                           session; children inherit the cold-path mask\n"
		"  --python-affinity-by-session\n"
		"                           set affinity only for Python TEST sessions;\n"
		"                           C++ and package sessions stay elastic\n"
		"  --critical-affinity-by-session\n"
		"                           set affinity only for known critical-stage\n"
		"                           sessions; package remains elastic\n"
		"  --balanced-affinity-by-session\n"
		"                           set coordinated C++/Python/package masks;\n"
		"                           package avoids Python's CPU3-4 but keeps\n"
		"                           CPU5-7 tail capacity\n"
		"  --affinity-release-after-first-fork\n"
		"                           after the first direct child fork of a package\n"
		"                           session, restore the root task's original CPU\n"
		"                           mask so later children can expand\n"
		"  --affinity-release-package-only\n"
		"                           with --affinity-release-after-first-fork,\n"
		"                           release only package-heavy sessions; keep\n"
		"                           C++/Python critical sessions pinned\n"
		"  --stage-state-file PATH  poll workload stage-state JSON and mark root\n"
		"                           final cleanup in C, without Python control plane\n"
		"  --stage-state-poll-ms MS poll interval for --stage-state-file, default 50\n"
		"  --stage-state-cleanup-window-ms MS\n"
		"                           BPF cleanup window refreshed while cleanup is active,\n"
		"                           default 5000\n"
		"  --events                 print ringbuf audit events after registration\n"
		"  --once                   register root/policy and exit\n"
		"  --help                   show this message\n",
		prog);
}

static int parse_u32_arg(const char *name, const char *text, __u32 *out)
{
	char *end = NULL;
	unsigned long v;

	errno = 0;
	v = strtoul(text, &end, 10);
	if (errno || !end || *end || v > UINT32_MAX) {
		fprintf(stderr, "invalid %s value: %s\n", name, text);
		return -EINVAL;
	}
	*out = (__u32)v;
	return 0;
}

static int parse_u64_arg(const char *name, const char *text, __u64 *out)
{
	char *end = NULL;
	unsigned long long v;

	errno = 0;
	v = strtoull(text, &end, 10);
	if (errno || !end || *end) {
		fprintf(stderr, "invalid %s value: %s\n", name, text);
		return -EINVAL;
	}
	*out = (__u64)v;
	return 0;
}

static int parse_s32_arg(const char *name, const char *text, __s32 *out)
{
	char *end = NULL;
	long v;

	errno = 0;
	v = strtol(text, &end, 10);
	if (errno || !end || *end || v < INT32_MIN || v > INT32_MAX) {
		fprintf(stderr, "invalid %s value: %s\n", name, text);
		return -EINVAL;
	}
	*out = (__s32)v;
	return 0;
}

static int parse_args(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--help")) {
			usage(argv[0]);
			exit(0);
		} else if (!strcmp(argv[i], "--pin-dir") && i + 1 < argc) {
			pin_dir = argv[++i];
		} else if (!strcmp(argv[i], "--root-pid") && i + 1 < argc) {
			int err = parse_u32_arg("--root-pid", argv[++i], &root_pid);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--session") && i + 1 < argc) {
			session_name = argv[++i];
		} else if (!strcmp(argv[i], "--confidence") && i + 1 < argc) {
			int err = parse_u32_arg("--confidence", argv[++i], &min_confidence);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--max-frontier-width") && i + 1 < argc) {
			int err = parse_u32_arg("--max-frontier-width", argv[++i], &max_frontier_width);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--max-vtime-boost-us") && i + 1 < argc) {
			__u32 boost_us;
			int err = parse_u32_arg("--max-vtime-boost-us", argv[++i], &boost_us);

			if (err)
				return err;
			max_vtime_boost_ns = boost_us * 1000U;
		} else if (!strcmp(argv[i], "--policy-window-ms") && i + 1 < argc) {
			int err = parse_u64_arg("--policy-window-ms", argv[++i], &policy_window_ms);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--boost-budget-ms") && i + 1 < argc) {
			int err = parse_u64_arg("--boost-budget-ms", argv[++i], &boost_budget_ms);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--ttl-ms") && i + 1 < argc) {
			int err = parse_u64_arg("--ttl-ms", argv[++i], &ttl_ms);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--anchor-cpu") && i + 1 < argc) {
			int err = parse_s32_arg("--anchor-cpu", argv[++i], &anchor_cpu);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--frontier")) {
			root_frontier = true;
		} else if (!strcmp(argv[i], "--propagation-depth") && i + 1 < argc) {
			int err = parse_u32_arg("--propagation-depth", argv[++i], &propagation_depth);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--width-control")) {
			width_control = true;
		} else if (!strcmp(argv[i], "--control-plane")) {
			control_plane = true;
		} else if (!strcmp(argv[i], "--assist-bpf-auto")) {
			assist_bpf_auto = true;
			control_plane = true;
		} else if (!strcmp(argv[i], "--cxx-only-session-policy")) {
			cxx_only_session_policy = true;
		} else if (!strcmp(argv[i], "--affinity-by-session")) {
			affinity_by_session = true;
		} else if (!strcmp(argv[i], "--python-affinity-by-session")) {
			python_affinity_by_session = true;
		} else if (!strcmp(argv[i], "--critical-affinity-by-session")) {
			critical_affinity_by_session = true;
		} else if (!strcmp(argv[i], "--balanced-affinity-by-session")) {
			balanced_affinity_by_session = true;
		} else if (!strcmp(argv[i], "--affinity-release-after-first-fork")) {
			affinity_release_after_first_fork = true;
		} else if (!strcmp(argv[i], "--affinity-release-package-only")) {
			affinity_release_package_only = true;
		} else if (!strcmp(argv[i], "--stage-state-file") && i + 1 < argc) {
			stage_state_file = argv[++i];
		} else if (!strcmp(argv[i], "--stage-state-poll-ms") && i + 1 < argc) {
			int err = parse_u64_arg("--stage-state-poll-ms",
						argv[++i], &stage_state_poll_ms);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--stage-state-cleanup-window-ms") && i + 1 < argc) {
			int err = parse_u64_arg("--stage-state-cleanup-window-ms",
						argv[++i],
						&stage_state_cleanup_window_ms);

			if (err)
				return err;
		} else if (!strcmp(argv[i], "--events")) {
			enable_events = true;
		} else if (!strcmp(argv[i], "--once")) {
			once = true;
		} else {
			fprintf(stderr, "unknown or incomplete argument: %s\n", argv[i]);
			usage(argv[0]);
			return -EINVAL;
		}
	}

	if (!root_pid) {
		fprintf(stderr, "--root-pid is required\n");
		usage(argv[0]);
		return -EINVAL;
	}
	if (min_confidence > 100) {
		fprintf(stderr, "--confidence expects 0..100\n");
		return -EINVAL;
	}
	if (!max_frontier_width) {
		fprintf(stderr, "--max-frontier-width must be positive\n");
		return -EINVAL;
	}
	if (control_plane && once) {
		fprintf(stderr, "--control-plane conflicts with --once\n");
		return -EINVAL;
	}
	if (stage_state_file && once) {
		fprintf(stderr, "--stage-state-file conflicts with --once\n");
		return -EINVAL;
	}
	if (stage_state_file && !stage_state_poll_ms) {
		fprintf(stderr, "--stage-state-poll-ms must be positive\n");
		return -EINVAL;
	}
	return 0;
}

static __u64 stable_hash64(const char *s)
{
	__u64 h = 1469598103934665603ULL;

	while (*s) {
		h ^= (unsigned char)*s++;
		h *= 1099511628211ULL;
	}
	return h ?: 1;
}

static bool session_has(const char *needle)
{
	return session_name && needle && strstr(session_name, needle);
}

static __s32 session_anchor_hint(void)
{
	if (anchor_cpu >= 0)
		return anchor_cpu;
	if (cxx_only_session_policy && !session_has("cxx-rust-build-heavy"))
		return -1;
	if (session_has("cxx-rust-build-heavy"))
		return 0;
	if (session_has("python-test-straggler"))
		return 4;
	if (session_has("package-log-artifact-heavy"))
		return 5;
	return -1;
}

static __u32 session_anchor_span_hint(void)
{
	if (anchor_cpu >= 0)
		return 1;
	if (cxx_only_session_policy && !session_has("cxx-rust-build-heavy"))
		return 0;
	if (session_has("cxx-rust-build-heavy"))
		return 1;
	if (session_has("python-test-straggler"))
		return 1;
	if (session_has("package-log-artifact-heavy"))
		return 1;
	return 0;
}

static void flowd_cpu_set(cpu_set_t *set, long nr_cpus, int cpu)
{
	if (!set || cpu < 0 || cpu >= CPU_SETSIZE)
		return;
	if (nr_cpus > 0 && cpu >= nr_cpus)
		return;
	CPU_SET(cpu, set);
}

static bool session_affinity_mask(cpu_set_t *set)
{
	long nr_cpus;

	if (!set)
		return false;

	CPU_ZERO(set);
	nr_cpus = sysconf(_SC_NPROCESSORS_CONF);
	if (session_has("cxx-rust-build-heavy")) {
		if (python_affinity_by_session && !affinity_by_session &&
		    !critical_affinity_by_session && !balanced_affinity_by_session)
			return false;
		flowd_cpu_set(set, nr_cpus, 0);
		flowd_cpu_set(set, nr_cpus, 1);
		flowd_cpu_set(set, nr_cpus, 2);
		return true;
	}
	if (session_has("python-test-straggler")) {
		flowd_cpu_set(set, nr_cpus, 3);
		flowd_cpu_set(set, nr_cpus, 4);
		flowd_cpu_set(set, nr_cpus, 5);
		flowd_cpu_set(set, nr_cpus, 6);
		flowd_cpu_set(set, nr_cpus, 7);
		return true;
	}
	if (session_has("package-log-artifact-heavy")) {
		if ((python_affinity_by_session || critical_affinity_by_session) &&
		    !affinity_by_session && !balanced_affinity_by_session)
			return false;
		flowd_cpu_set(set, nr_cpus, 0);
		flowd_cpu_set(set, nr_cpus, 1);
		flowd_cpu_set(set, nr_cpus, 2);
		flowd_cpu_set(set, nr_cpus, 5);
		if (balanced_affinity_by_session) {
			flowd_cpu_set(set, nr_cpus, 6);
			flowd_cpu_set(set, nr_cpus, 7);
		}
		return true;
	}
	return false;
}

static bool session_affinity_release_enabled(void)
{
	if (affinity_release_package_only &&
	    !session_has("package-log-artifact-heavy"))
		return false;
	return affinity_release_after_first_fork &&
	       (affinity_by_session || python_affinity_by_session ||
		critical_affinity_by_session ||
	        balanced_affinity_by_session);
}

static int snapshot_root_affinity_mask(void)
{
	if (!session_affinity_release_enabled())
		return 0;
	if (affinity_restore_mask_valid)
		return 0;
	if (sched_getaffinity((pid_t)root_pid, sizeof(affinity_restore_mask),
			      &affinity_restore_mask)) {
		int err = errno;

		fprintf(stderr, "failed to snapshot affinity for root_pid=%u session=%s: %s\n",
			root_pid, session_name, strerror(err));
		return -err;
	}
	affinity_restore_mask_valid = true;
	return 0;
}

static int restore_root_affinity_mask(void)
{
	if (!session_affinity_release_enabled() || affinity_release_done)
		return 0;
	if (!affinity_restore_mask_valid)
		return 0;
	if (sched_setaffinity((pid_t)root_pid, sizeof(affinity_restore_mask),
			      &affinity_restore_mask)) {
		int err = errno;

		fprintf(stderr, "failed to restore affinity for root_pid=%u session=%s: %s\n",
			root_pid, session_name, strerror(err));
		return -err;
	}
	affinity_release_done = true;
	printf("flowd restored root affinity session=%s root_pid=%u after first fork\n",
	       session_name, root_pid);
	fflush(stdout);
	return 0;
}

static bool affinity_release_due(__u64 now, const struct flow_event *e)
{
	if (!session_affinity_release_enabled() || affinity_release_done)
		return false;
	if (e && e->kind == FLOW_EVT_FORK && e->ppid == root_pid)
		return true;
	if (!affinity_release_after_ns || !affinity_release_start_ns)
		return false;
	return now >= affinity_release_start_ns + affinity_release_after_ns;
}

static int apply_session_affinity_once(void)
{
	cpu_set_t set;

	if (!affinity_by_session && !python_affinity_by_session &&
	    !critical_affinity_by_session &&
	    !balanced_affinity_by_session)
		return 0;
	if (!session_affinity_mask(&set))
		return 0;
	if (CPU_COUNT(&set) <= 0)
		return 0;
	if (sched_setaffinity((pid_t)root_pid, sizeof(set), &set)) {
		int err = errno;

		fprintf(stderr, "failed to set affinity for root_pid=%u session=%s: %s\n",
			root_pid, session_name, strerror(err));
		return -err;
	}
	printf("flowd set root affinity session=%s root_pid=%u cpus=",
	       session_name, root_pid);
	for (int cpu = 0, first = 1; cpu < CPU_SETSIZE; cpu++) {
		if (!CPU_ISSET(cpu, &set))
			continue;
		printf("%s%d", first ? "" : ",", cpu);
		first = 0;
	}
	printf("\n");
	fflush(stdout);
	return 0;
}

static __u64 monotonic_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts))
		return 0;
	return (__u64)ts.tv_sec * 1000000000ULL + (__u64)ts.tv_nsec;
}

static int read_proc_identity(__u32 pid, __u32 *tgid_out, __u32 *ppid_out)
{
	char path[64];
	char buf[4096];
	char *rparen;
	unsigned int stat_pid = 0;
	unsigned int stat_ppid = 0;
	char state = 0;
	FILE *f;

	if (!pid || !tgid_out || !ppid_out)
		return -EINVAL;

	snprintf(path, sizeof(path), "/proc/%u/stat", pid);
	f = fopen(path, "r");
	if (!f)
		return -errno;
	if (!fgets(buf, sizeof(buf), f)) {
		int err = ferror(f) ? -errno : -EINVAL;

		fclose(f);
		return err;
	}
	fclose(f);

	if (sscanf(buf, "%u", &stat_pid) != 1)
		return -EINVAL;
	rparen = strrchr(buf, ')');
	if (!rparen)
		return -EINVAL;
	if (sscanf(rparen + 1, " %c %u", &state, &stat_ppid) != 2)
		return -EINVAL;

	*tgid_out = stat_pid ?: pid;
	*ppid_out = stat_ppid;
	return 0;
}

static bool text_has(const char *haystack, const char *needle);

static int read_stage_state_json(char *buf, size_t len)
{
	FILE *f;
	size_t n;

	if (!stage_state_file || !buf || len < 2)
		return -EINVAL;
	f = fopen(stage_state_file, "r");
	if (!f)
		return -errno;
	n = fread(buf, 1, len - 1, f);
	fclose(f);
	buf[n] = '\0';
	return 0;
}

static bool stage_state_cleanup_begin(const char *buf)
{
	if (!buf)
		return false;
	return text_has(buf, "\"phase\": \"cleanup_begin\"") ||
	       text_has(buf, "\"phase\":\"cleanup_begin\"");
}

static void poll_stage_state_cleanup(int task_fd)
{
	struct flow_task_key key = { .pid = root_pid };
	struct flow_task_ctx ctx = {};
	char buf[4096];
	__u64 now;
	__u64 window_ns;

	if (!stage_state_file || task_fd < 0 || !root_pid)
		return;
	if (read_stage_state_json(buf, sizeof(buf)))
		return;
	if (!stage_state_cleanup_begin(buf)) {
		stage_state_cleanup_marked = false;
		return;
	}

	now = monotonic_ns();
	if (!now)
		return;
	window_ns = stage_state_cleanup_window_ms * 1000000ULL;
	if (!window_ns)
		window_ns = 1000000000ULL;

	if (!bpf_map_lookup_elem(task_fd, &key, &ctx)) {
		if (ctx.session_id == FLOWCAUSAL_PARENT_EXIT_SESSION_ID &&
		    (ctx.flags & FLOWCAUSAL_TASK_PARENT_LATENCY) &&
		    ctx.stage_id && ctx.stage_id > now + (window_ns >> 1))
			return;
	} else {
		ctx.stage_id = 0;
		ctx.cohort_id = stable_hash64(session_name) ^
				stable_hash64("stage_state_cleanup");
		ctx.tgid = root_tgid ? root_tgid : root_pid;
		ctx.parent_pid = root_ppid;
	}

	ctx.session_id = FLOWCAUSAL_PARENT_EXIT_SESSION_ID;
	ctx.last_update_ns = now;
	ctx.stage_id = now + window_ns;
	ctx.flags = FLOWCAUSAL_TASK_PARENT_LATENCY |
		    FLOWCAUSAL_TASK_MODE_INTERACTIVE |
		    FLOWCAUSAL_TASK_BPF_AUTO;
	ctx.confidence = 95;
	ctx.anchor_cpu = -1;
	ctx.propagation_depth = 0;

	if (!bpf_map_update_elem(task_fd, &key, &ctx, BPF_ANY)) {
		stage_state_updates++;
		if (!stage_state_cleanup_marked) {
			printf("flowd stage-state cleanup marked session=%s root_pid=%u window_ms=%" PRIu64 "\n",
			       session_name, root_pid, U64(stage_state_cleanup_window_ms));
			fflush(stdout);
			stage_state_cleanup_marked = true;
		}
	}
}

static int join_path(char *out, size_t out_sz, const char *dir, const char *name)
{
	int n = snprintf(out, out_sz, "%s/%s", dir, name);

	if (n < 0 || n >= (int)out_sz)
		return -ENAMETOOLONG;
	return 0;
}

static int open_pinned_map(const char *name, bool required)
{
	char path[PATH_MAX];
	int err;
	int fd;

	err = join_path(path, sizeof(path), pin_dir, name);
	if (err) {
		errno = -err;
		return -1;
	}

	fd = bpf_obj_get(path);
	if (fd < 0 && required)
		fprintf(stderr, "failed to open pinned map %s: %s\n",
			path, strerror(errno));
	return fd;
}

static int register_session(int task_fd, int policy_fd)
{
	struct flow_session_policy policy = {};
	struct flow_task_key key = {};
	struct flow_task_ctx ctx = {};
	__u64 now = monotonic_ns();
	__u64 session_id = stable_hash64(session_name);
	__u32 anchor_span;
	int err;

	if (!now)
		return -errno;
	if (!root_tgid) {
		__u32 stat_tgid = 0;
		__u32 stat_ppid = 0;

		if (!read_proc_identity(root_pid, &stat_tgid, &stat_ppid)) {
			root_tgid = stat_tgid;
			root_ppid = stat_ppid;
		} else {
			root_tgid = root_pid;
			root_ppid = 0;
		}
	}

	err = snapshot_root_affinity_mask();
	if (err)
		return err;
	if (session_affinity_release_enabled())
		affinity_release_start_ns = now;
	err = apply_session_affinity_once();
	if (err)
		return err;

	anchor_cpu = session_anchor_hint();
	anchor_span = session_anchor_span_hint();
	if (anchor_cpu < 0)
		anchor_span = 0;
	else if (!anchor_span)
		anchor_span = 1;

	policy.flags = FLOWCAUSAL_POLICY_ENABLE;
	if (anchor_cpu >= 0)
		policy.flags |= FLOWCAUSAL_POLICY_LOCALITY;
	if (width_control)
		policy.flags |= FLOWCAUSAL_POLICY_WIDTH_CTL;
	policy.min_confidence = min_confidence;
	policy.max_frontier_width = max_frontier_width;
	policy.max_vtime_boost = max_vtime_boost_ns;
	policy.window_ns = policy_window_ms * 1000000ULL;
	policy.boost_budget_ns = boost_budget_ms * 1000000ULL;
	policy.expires_ns = ttl_ms ? now + ttl_ms * 1000000ULL : 0;
	policy.anchor_cpu = anchor_cpu;
	policy.anchor_span = anchor_span;

	key.pid = root_pid;
	ctx.session_id = session_id;
	ctx.stage_id = stable_hash64("FLOW_STAGE_UNKNOWN");
	ctx.cohort_id = stable_hash64(session_name) ^ stable_hash64("cohort0");
	ctx.last_update_ns = now;
	ctx.tgid = root_tgid;
	ctx.parent_pid = root_ppid;
	ctx.flags = 0;
	ctx.flags |= FLOWCAUSAL_TASK_ROOT;
	if (anchor_cpu >= 0)
		ctx.flags |= FLOWCAUSAL_TASK_LOCALITY;
	if (root_frontier || propagation_depth > 0)
		ctx.flags |= FLOWCAUSAL_TASK_FRONTIER |
			     FLOWCAUSAL_TASK_COHORT_TAIL |
			     FLOWCAUSAL_TASK_PROPAGATE;
	ctx.confidence = min_confidence;
	ctx.anchor_cpu = anchor_cpu;
	ctx.propagation_depth = propagation_depth;

	if (!control_plane && !assist_bpf_auto &&
	    (session_has("cxx-rust-build-heavy") ||
	     (!cxx_only_session_policy &&
	      session_has("package-log-artifact-heavy")))) {
		ctx.flags |= FLOWCAUSAL_TASK_STAGE_BUILD_WIDE |
			     FLOWCAUSAL_TASK_FRONTIER |
			     FLOWCAUSAL_TASK_PROPAGATE;
		ctx.confidence = assist_confidence;
		if (ctx.propagation_depth < 1)
			ctx.propagation_depth = 1;
	} else if (!control_plane && !assist_bpf_auto &&
		   !cxx_only_session_policy &&
		   session_has("python-test-straggler")) {
		ctx.flags |= FLOWCAUSAL_TASK_STAGE_TEST_TAIL |
			     FLOWCAUSAL_TASK_FRONTIER |
			     FLOWCAUSAL_TASK_COHORT_TAIL |
			     FLOWCAUSAL_TASK_PROPAGATE;
		ctx.confidence = assist_confidence;
		if (ctx.propagation_depth < 1)
			ctx.propagation_depth = 1;
	}

	if (bpf_map_update_elem(policy_fd, &session_id, &policy, BPF_ANY)) {
		fprintf(stderr, "failed to update session_policy: %s\n", strerror(errno));
		return -errno;
	}
	if (bpf_map_update_elem(task_fd, &key, &ctx, BPF_ANY)) {
		fprintf(stderr, "failed to update root task_ctx: %s\n", strerror(errno));
		return -errno;
	}

	control_session_id = session_id;

	printf("flowd registered session=%s id=%" PRIu64
	       " root_pid=%u tgid=%u ppid=%u ttl_ms=%" PRIu64
	       " policy_flags=0x%x task_flags=0x%x propagation_depth=%u"
	       " width_control=%u affinity_by_session=%u python_affinity=%u"
	       " affinity_release=%u"
	       " affinity_release_package_only=%u"
	       " cxx_only_session_policy=%u"
	       " expires_ns=%" PRIu64 "\n",
	       session_name, U64(session_id), root_pid, root_tgid, root_ppid,
	       U64(ttl_ms), policy.flags, ctx.flags, ctx.propagation_depth,
	       width_control ? 1 : 0, affinity_by_session ? 1 : 0,
	       python_affinity_by_session ? 1 : 0,
	       affinity_release_after_first_fork ? 1 : 0,
	       affinity_release_package_only ? 1 : 0,
	       cxx_only_session_policy ? 1 : 0,
	       U64(policy.expires_ns));
	fflush(stdout);
	return 0;
}

static const char *event_name(__u32 kind)
{
	switch (kind) {
	case FLOW_EVT_FORK:
		return "fork";
	case FLOW_EVT_EXEC:
		return "exec";
	case FLOW_EVT_EXIT:
		return "exit";
	case FLOW_EVT_WAKEUP:
		return "wakeup";
	case FLOW_EVT_SWITCH_IN:
		return "switch-in";
	case FLOW_EVT_SWITCH_OUT:
		return "switch-out";
	case FLOW_EVT_WAIT:
		return "wait";
	case FLOW_EVT_FRONTIER_ENQUEUE:
		return "frontier-enqueue";
	case FLOW_EVT_FALLBACK:
		return "fallback";
	case FLOW_EVT_CLEANUP:
		return "cleanup";
	default:
		return "unknown";
	}
}

static const char *mode_name(__u32 flags)
{
	switch (flags & FLOWCAUSAL_TASK_MODE_MASK) {
	case FLOWCAUSAL_TASK_MODE_INTERACTIVE:
		return "interactive";
	case FLOWCAUSAL_TASK_MODE_COMPUTE:
		return "compute";
	case FLOWCAUSAL_TASK_MODE_TAIL:
		return "tail";
	case FLOWCAUSAL_TASK_MODE_BACKGROUND:
		return "background";
	default:
		return "unknown";
	}
}

static bool text_has_any(const char *text, const char *const *needles, size_t n);

static bool comm_has_prefix(const char comm[FLOWCAUSAL_MAX_COMM], const char *prefix)
{
	size_t n = strlen(prefix);

	return !strncmp(comm, prefix, n);
}

static bool text_has(const char *text, const char *needle)
{
	return text && needle && strstr(text, needle);
}

static bool is_cmd_sep(char c)
{
	return c == '\0' || c == ' ' || c == '\t' || c == '\n';
}

static bool is_cmd_suffix_sep(char c)
{
	return is_cmd_sep(c) || c == '-' || c == '.' || (c >= '0' && c <= '9');
}

static bool text_has_cmd_prefix(const char *text, const char *needle)
{
	size_t needle_len;
	const char *p;

	if (!text || !needle || !needle[0])
		return false;

	needle_len = strlen(needle);
	p = text;
	while ((p = strstr(p, needle))) {
		char prev = p == text ? '\0' : p[-1];
		char next = p[needle_len];

		if ((p == text || is_cmd_sep(prev) || prev == '/') &&
		    is_cmd_suffix_sep(next))
			return true;
		p++;
	}
	return false;
}

static bool text_has_any(const char *text, const char *const *needles, size_t n)
{
	for (size_t i = 0; i < n; i++) {
		if (text_has(text, needles[i]))
			return true;
	}
	return false;
}

static int read_cmdline(__u32 pid, char *buf, size_t len)
{
	char path[64];
	FILE *f;
	size_t n;

	if (!buf || !len)
		return -EINVAL;
	buf[0] = '\0';
	if (snprintf(path, sizeof(path), "/proc/%u/cmdline", pid) >= (int)sizeof(path))
		return -ENAMETOOLONG;
	f = fopen(path, "re");
	if (!f)
		return -errno;
	n = fread(buf, 1, len - 1, f);
	fclose(f);
	buf[n] = '\0';
	for (size_t i = 0; i < n; i++) {
		if (buf[i] == '\0')
			buf[i] = ' ';
	}
	return (int)n;
}

static int read_proc_env_value(__u32 pid, const char *needle, char *buf, size_t len)
{
	char path[64];
	FILE *f;
	size_t n;
	const size_t needle_len = needle ? strlen(needle) : 0;

	if (!buf || !len || !needle_len)
		return -EINVAL;
	buf[0] = '\0';
	if (snprintf(path, sizeof(path), "/proc/%u/environ", pid) >= (int)sizeof(path))
		return -ENAMETOOLONG;
	f = fopen(path, "re");
	if (!f)
		return -errno;
	n = fread(buf, 1, len - 1, f);
	fclose(f);
	buf[n] = '\0';
	for (size_t i = 0; i < n; ) {
		char *entry = &buf[i];
		size_t entry_len = strlen(entry);

		if (entry_len >= needle_len + 1 &&
		    !strncmp(entry, needle, needle_len) &&
		    entry[needle_len] == '=') {
			memmove(buf, entry + needle_len + 1, entry_len - needle_len);
			buf[entry_len - needle_len - 1] = '\0';
			return 0;
		}
		i += entry_len + 1;
	}
	return -ENOENT;
}

static bool read_stage_env(__u32 pid, char *buf, size_t len)
{
	if (read_proc_env_value(pid, "FLOWCAUSAL_STAGE", buf, len) == 0)
		return true;
	if (read_proc_env_value(pid, "HRB_STAGE", buf, len) == 0)
		return true;
	return false;
}

static __u32 stage_mode_from_name(const char *stage)
{
	if (!stage)
		return FLOWCAUSAL_TASK_MODE_UNKNOWN;
	if (!strcmp(stage, "BUILD") || !strcmp(stage, "BUILD_WIDE"))
		return FLOWCAUSAL_TASK_MODE_COMPUTE;
	if (!strcmp(stage, "TEST") || !strcmp(stage, "RETRY_PROBE"))
		return FLOWCAUSAL_TASK_MODE_TAIL;
	if (!strcmp(stage, "PACKAGE_BACKGROUND"))
		return FLOWCAUSAL_TASK_MODE_BACKGROUND;
	if (!strcmp(stage, "INSPECT"))
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	if (!strcmp(stage, "EDIT"))
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	return FLOWCAUSAL_TASK_MODE_UNKNOWN;
}

static __u64 stage_id_from_name(const char *stage, __u32 mode)
{
	if (!stage)
		return stable_hash64("FLOW_STAGE_UNKNOWN");
	if (!strcmp(stage, "BUILD") || !strcmp(stage, "BUILD_WIDE"))
		return stable_hash64("FLOW_STAGE_BUILD");
	if (!strcmp(stage, "TEST") || !strcmp(stage, "RETRY_PROBE"))
		return stable_hash64("FLOW_STAGE_TEST");
	if (!strcmp(stage, "PACKAGE_BACKGROUND"))
		return stable_hash64("FLOW_STAGE_PACKAGE_BG");
	if (!strcmp(stage, "INSPECT") || !strcmp(stage, "EDIT"))
		return stable_hash64("FLOW_STAGE_INSPECT");
	return stable_hash64(stage) ^ stable_hash64("FLOW_STAGE_UNKNOWN") ^ mode;
}

static bool cmdline_has_any_cmd(const char *cmdline,
				const char *const *needles, size_t n)
{
	for (size_t i = 0; i < n; i++) {
		if (text_has_cmd_prefix(cmdline, needles[i]))
			return true;
	}
	return false;
}

static bool comm_is_compute_like(const char comm[FLOWCAUSAL_MAX_COMM])
{
	return comm_has_prefix(comm, "gcc") ||
	       comm_has_prefix(comm, "g++") ||
	       comm_has_prefix(comm, "clang") ||
	       comm_has_prefix(comm, "cc1") ||
	       comm_has_prefix(comm, "rustc") ||
	       comm_has_prefix(comm, "javac") ||
	       comm_has_prefix(comm, "pdflatex") ||
	       comm_has_prefix(comm, "flowbench") ||
	       comm_has_prefix(comm, "test_runner") ||
	       comm_has_prefix(comm, "ld") ||
	       comm_has_prefix(comm, "mold") ||
	       comm_has_prefix(comm, "gold") ||
	       comm_has_prefix(comm, "collect2") ||
	       comm_has_prefix(comm, "ar") ||
	       comm_has_prefix(comm, "as") ||
	       comm_has_prefix(comm, "strip") ||
	       comm_has_prefix(comm, "objcopy") ||
	       comm_has_prefix(comm, "objdump") ||
	       comm_has_prefix(comm, "nm") ||
	       comm_has_prefix(comm, "ranlib") ||
	       comm_has_prefix(comm, "make") ||
	       comm_has_prefix(comm, "ninja") ||
	       comm_has_prefix(comm, "cmake") ||
	       comm_has_prefix(comm, "cargo") ||
	       comm_has_prefix(comm, "go") ||
	       comm_has_prefix(comm, "mvn") ||
	       comm_has_prefix(comm, "gradle") ||
	       comm_has_prefix(comm, "pytest") ||
	       comm_has_prefix(comm, "ctest") ||
	       comm_has_prefix(comm, "jest") ||
	       comm_has_prefix(comm, "bats") ||
	       comm_has_prefix(comm, "lit") ||
	       comm_has_prefix(comm, "pip") ||
	       comm_has_prefix(comm, "docker") ||
	       comm_has_prefix(comm, "git") ||
	       comm_has_prefix(comm, "sudo") ||
	       comm_has_prefix(comm, "bpftool") ||
	       comm_has_prefix(comm, "tar") ||
	       comm_has_prefix(comm, "gzip") ||
	       comm_has_prefix(comm, "find") ||
	       comm_has_prefix(comm, "grep") ||
	       comm_has_prefix(comm, "xargs") ||
	       comm_has_prefix(comm, "hg");
}

static bool comm_is_interactive_like(const char comm[FLOWCAUSAL_MAX_COMM])
{
	return comm_has_prefix(comm, "bash") ||
	       comm_has_prefix(comm, "sh") ||
	       comm_has_prefix(comm, "zsh") ||
	       comm_has_prefix(comm, "fish") ||
	       comm_has_prefix(comm, "vim") ||
	       comm_has_prefix(comm, "vi") ||
	       comm_has_prefix(comm, "nano") ||
	       comm_has_prefix(comm, "emacs") ||
	       comm_has_prefix(comm, "code") ||
	       comm_has_prefix(comm, "codex") ||
	       comm_has_prefix(comm, "agent") ||
	       comm_has_prefix(comm, "openhands") ||
	       comm_has_prefix(comm, "swe-agent") ||
	       comm_has_prefix(comm, "python") ||
	       comm_has_prefix(comm, "ipython") ||
	       comm_has_prefix(comm, "jupyter") ||
	       comm_has_prefix(comm, "node") ||
	       comm_has_prefix(comm, "npm") ||
	       comm_has_prefix(comm, "ruby") ||
	       comm_has_prefix(comm, "perl") ||
	       comm_has_prefix(comm, "lua");
}

static __u32 mode_from_comm(const char comm[FLOWCAUSAL_MAX_COMM])
{
	if (comm_is_compute_like(comm))
		return FLOWCAUSAL_TASK_MODE_COMPUTE;
	if (comm_is_interactive_like(comm))
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	return FLOWCAUSAL_TASK_MODE_UNKNOWN;
}

static __u32 mode_from_text(const char comm[FLOWCAUSAL_MAX_COMM], const char *cmdline)
{
	static const char *const compute_cmds[] = {
		"pytest", "tox", "nox", "ctest", "jest", "bats", "lit",
		"gcc", "g++", "clang", "rustc", "cargo", "go", "npm",
		"mvn", "gradle", "make", "ninja", "cmake", "pip"
	};
	static const char *const compute_phrases[] = {
		"-m pytest", " unittest", "manage.py test", "cargo test",
		"go test", "npm test", "mvn test", "gradle test",
		"cargo build", "setup.py build", "pip install",
		"harness_breadth_workloads.py cxx-rust-build",
		"harness_breadth_workloads.py cxx-rust-test",
		"harness_breadth_workloads.py test-straggler-prepare",
		"harness_breadth_workloads.py test-straggler-run",
		"harness_breadth_workloads.py package-prepare"
	};
	static const char *const background_phrases[] = {
		"harness_breadth_workloads.py package-run",
		"harness_breadth_workloads.py package-validate"
	};
	static const char *const inspect_phrases[] = {
		"harness_breadth_workloads.py inspect-tools"
	};
	static const char *const test_tail_phrases[] = {
		"harness_breadth_workloads.py cxx-rust-test",
		"harness_breadth_workloads.py test-straggler-run",
		"pytest", "tox", "nox", "ctest", "jest", "bats", "lit"
	};
	static const char *const interactive_cmds[] = {
		"rg", "grep", "find", "sed", "awk", "cat", "ls", "head",
		"tail", "python", "python3", "bash", "sh"
	};

	if (text_has_any(cmdline, inspect_phrases,
			 sizeof(inspect_phrases) / sizeof(inspect_phrases[0])))
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	if (text_has_any(cmdline, background_phrases,
			 sizeof(background_phrases) / sizeof(background_phrases[0])))
		return FLOWCAUSAL_TASK_MODE_BACKGROUND;
	if (text_has_any(cmdline, test_tail_phrases,
			 sizeof(test_tail_phrases) / sizeof(test_tail_phrases[0])))
		return FLOWCAUSAL_TASK_MODE_TAIL;
	if (text_has_any(cmdline, compute_phrases,
			 sizeof(compute_phrases) / sizeof(compute_phrases[0])) ||
	    cmdline_has_any_cmd(cmdline, compute_cmds,
				sizeof(compute_cmds) / sizeof(compute_cmds[0])))
		return FLOWCAUSAL_TASK_MODE_COMPUTE;
	if (cmdline_has_any_cmd(cmdline, interactive_cmds,
				sizeof(interactive_cmds) / sizeof(interactive_cmds[0])))
		return FLOWCAUSAL_TASK_MODE_INTERACTIVE;
	return mode_from_comm(comm);
}

static __u64 stage_id_from_comm(const char comm[FLOWCAUSAL_MAX_COMM], __u32 mode)
{
	if (comm_has_prefix(comm, "pytest") || comm_has_prefix(comm, "ctest") ||
	    comm_has_prefix(comm, "jest") || comm_has_prefix(comm, "bats") ||
	    comm_has_prefix(comm, "lit") || comm_has_prefix(comm, "test_runner"))
		return stable_hash64("FLOW_STAGE_TEST");
	if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE)
		return stable_hash64("FLOW_STAGE_BUILD");
	if (mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE)
		return stable_hash64("FLOW_STAGE_SHELL");
	return stable_hash64("FLOW_STAGE_UNKNOWN");
}

static __u64 stage_id_from_text(const char comm[FLOWCAUSAL_MAX_COMM], const char *cmdline, __u32 mode)
{
	static const char *const test_cmds[] = {
		"pytest", "tox", "nox", "ctest", "jest", "bats", "lit"
	};
	static const char *const build_cmds[] = {
		"gcc", "g++", "clang", "rustc", "cargo", "make", "ninja",
		"cmake"
	};
	static const char *const search_cmds[] = {
		"rg", "grep", "find"
	};

	if (text_has(cmdline, "harness_breadth_workloads.py inspect-tools"))
		return stable_hash64("FLOW_STAGE_INSPECT");
	if (text_has(cmdline, "harness_breadth_workloads.py package-run") ||
	    text_has(cmdline, "harness_breadth_workloads.py package-validate"))
		return stable_hash64("FLOW_STAGE_PACKAGE_BACKGROUND");
	if (cmdline_has_any_cmd(cmdline, test_cmds,
				sizeof(test_cmds) / sizeof(test_cmds[0])) ||
	    text_has(cmdline, "unittest") || text_has(cmdline, "manage.py test") ||
	    text_has(cmdline, "cargo test") || text_has(cmdline, "go test") ||
	    text_has(cmdline, "npm test") ||
	    text_has(cmdline, "harness_breadth_workloads.py cxx-rust-test") ||
	    text_has(cmdline, "harness_breadth_workloads.py test-straggler-run"))
		return stable_hash64("FLOW_STAGE_TEST");
	if (cmdline_has_any_cmd(cmdline, build_cmds,
				sizeof(build_cmds) / sizeof(build_cmds[0])) ||
	    text_has(cmdline, "cargo build") || text_has(cmdline, "setup.py build") ||
	    text_has(cmdline, "harness_breadth_workloads.py cxx-rust-build") ||
	    text_has(cmdline, "harness_breadth_workloads.py test-straggler-prepare") ||
	    text_has(cmdline, "harness_breadth_workloads.py package-prepare"))
		return stable_hash64("FLOW_STAGE_BUILD");
	if (cmdline_has_any_cmd(cmdline, search_cmds,
				sizeof(search_cmds) / sizeof(search_cmds[0])) ||
	    text_has(cmdline, "harness_breadth_workloads.py inspect-tools"))
		return stable_hash64("FLOW_STAGE_SEARCH");
	return stage_id_from_comm(comm, mode);
}

static bool has_compute_frontier_signal(const char *cmdline)
{
	static const char *const compute_cmds[] = {
		"pytest", "gcc", "g++", "clang", "rustc", "make", "ninja",
		"cargo", "go"
	};

	return cmdline_has_any_cmd(cmdline, compute_cmds,
				   sizeof(compute_cmds) / sizeof(compute_cmds[0])) ||
	       text_has(cmdline, "-m pytest") ||
	       text_has(cmdline, "manage.py test") ||
	       text_has(cmdline, "cargo test") ||
	       text_has(cmdline, "go test") ||
	       text_has(cmdline, "harness_breadth_workloads.py cxx-rust-build") ||
	       text_has(cmdline, "harness_breadth_workloads.py test-straggler-prepare") ||
	       text_has(cmdline, "harness_breadth_workloads.py package-prepare");
}

static bool has_build_wide_signal(const char *cmdline)
{
	static const char *const build_phrases[] = {
		"harness_breadth_workloads.py cxx-rust-build",
		"harness_breadth_workloads.py test-straggler-prepare",
		"harness_breadth_workloads.py package-prepare",
	};

	return text_has_any(cmdline, build_phrases,
			    sizeof(build_phrases) / sizeof(build_phrases[0]));
}

static bool has_test_tail_signal(const char *cmdline)
{
	static const char *const test_tail_phrases[] = {
		"harness_breadth_workloads.py cxx-rust-test",
		"harness_breadth_workloads.py test-straggler-run",
		"pytest", "tox", "nox", "ctest", "jest", "bats", "lit",
	};

	return text_has_any(cmdline, test_tail_phrases,
			    sizeof(test_tail_phrases) / sizeof(test_tail_phrases[0]));
}

static bool has_background_signal(const char *cmdline)
{
	return text_has(cmdline, "harness_breadth_workloads.py package-run") ||
	       text_has(cmdline, "harness_breadth_workloads.py package-validate");
}

static __u32 stage_flags_from_text(const char *cmdline)
{
	__u32 flags = 0;

	if (text_has(cmdline, "harness_breadth_workloads.py inspect-tools"))
		flags |= FLOWCAUSAL_TASK_STAGE_INSPECT;
	if (has_build_wide_signal(cmdline))
		flags |= FLOWCAUSAL_TASK_STAGE_BUILD_WIDE;
	if (has_test_tail_signal(cmdline))
		flags |= FLOWCAUSAL_TASK_STAGE_TEST_TAIL;
	if (has_background_signal(cmdline))
		flags |= FLOWCAUSAL_TASK_STAGE_PACKAGE_BG;
	return flags;
}

static bool has_cmdline_assist_signal(const char *cmdline)
{
	static const char *const interactive_cmds[] = {
		"python", "python3", "bash", "sh", "rg", "grep", "find"
	};

	return has_build_wide_signal(cmdline) ||
	       has_test_tail_signal(cmdline) ||
	       has_background_signal(cmdline) ||
	       text_has(cmdline, "harness_breadth_workloads.py inspect-tools") ||
	       cmdline_has_any_cmd(cmdline, interactive_cmds,
				   sizeof(interactive_cmds) / sizeof(interactive_cmds[0]));
}

static void apply_bpf_assist_update(const struct flow_event *e)
{
	char cmdline[512];
	char stage_env[64] = {};
	struct flow_task_key key = {};
	struct flow_task_ctx ctx = {};
	struct flow_session_policy policy = {};
	__u32 mode;
	__u32 flags;
	__u64 stage_id;
	bool strong_compute;
	bool have_stage_env;

	if (!control_plane || control_task_fd < 0 || !e || !e->pid || !e->session_id)
		return;
	if (control_session_id && e->session_id != control_session_id)
		return;
	if (e->kind != FLOW_EVT_EXEC &&
	    e->kind != FLOW_EVT_FORK &&
	    e->kind != FLOW_EVT_WAKEUP)
		return;

	key.pid = e->pid;
	if (bpf_map_lookup_elem(control_task_fd, &key, &ctx))
		return;

	have_stage_env = read_stage_env(e->pid, stage_env, sizeof(stage_env));
	cmdline[0] = '\0';
	read_cmdline(e->pid, cmdline, sizeof(cmdline));
	if (have_stage_env) {
		mode = stage_mode_from_name(stage_env);
		stage_id = stage_id_from_name(stage_env, mode);
		flags = ctx.flags & FLOWCAUSAL_TASK_LOCALITY;
		if (mode)
			flags |= mode;
		else
			flags |= FLOWCAUSAL_TASK_MODE_UNKNOWN;
		flags |= FLOWCAUSAL_TASK_C_ASSIST | FLOWCAUSAL_TASK_BPF_AUTO;
		if (!strcmp(stage_env, "BUILD") || !strcmp(stage_env, "BUILD_WIDE")) {
			flags |= FLOWCAUSAL_TASK_STAGE_BUILD_WIDE |
				 FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_PROPAGATE;
			flags &= ~(FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_CONVERGE_TAIL |
				   FLOWCAUSAL_TASK_BACKGROUND |
				   FLOWCAUSAL_TASK_OBSERVE_ONLY);
			if (ctx.confidence < assist_confidence)
				ctx.confidence = assist_confidence;
			if (ctx.propagation_depth < 1)
				ctx.propagation_depth = 1;
		} else if (!strcmp(stage_env, "TEST") || !strcmp(stage_env, "RETRY_PROBE")) {
			flags |= FLOWCAUSAL_TASK_STAGE_TEST_TAIL |
				 FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE |
				 FLOWCAUSAL_TASK_CONVERGE_TAIL;
			flags &= ~FLOWCAUSAL_TASK_BACKGROUND;
			if (ctx.confidence < assist_confidence)
				ctx.confidence = assist_confidence;
			if (ctx.propagation_depth < 1)
				ctx.propagation_depth = 1;
		} else if (!strcmp(stage_env, "PACKAGE_BACKGROUND")) {
			flags |= FLOWCAUSAL_TASK_STAGE_PACKAGE_BG |
				 FLOWCAUSAL_TASK_BACKGROUND;
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				   FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_PROPAGATE |
				   FLOWCAUSAL_TASK_CONVERGE_TAIL);
			ctx.confidence = min_confidence;
			ctx.propagation_depth = 0;
		} else if (!strcmp(stage_env, "INSPECT") || !strcmp(stage_env, "EDIT")) {
			flags |= FLOWCAUSAL_TASK_STAGE_INSPECT;
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				   FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_PROPAGATE |
				   FLOWCAUSAL_TASK_CONVERGE_TAIL |
				   FLOWCAUSAL_TASK_BACKGROUND);
			if (ctx.confidence < min_confidence)
				ctx.confidence = min_confidence;
		} else {
			if (ctx.runtime_ns >= (FLOWD_SLICE_DFL_NS << 1) ||
			    ctx.runtime_ns > ctx.runnable_wait_ns + (FLOWD_SLICE_DFL_NS >> 1))
				mode = FLOWCAUSAL_TASK_MODE_COMPUTE;
			else if (ctx.runnable_wait_ns >= (FLOWD_SLICE_DFL_NS >> 1))
				mode = FLOWCAUSAL_TASK_MODE_INTERACTIVE;
			flags |= FLOWCAUSAL_TASK_MODE_UNKNOWN;
		}
	} else {
		strong_compute = has_compute_frontier_signal(cmdline) ||
				 has_test_tail_signal(cmdline);
		if (!has_cmdline_assist_signal(cmdline))
			return;

		mode = mode_from_text(e->comm, cmdline);
		if (!mode) {
			if (ctx.runtime_ns >= (FLOWD_SLICE_DFL_NS << 1) ||
			    ctx.runtime_ns > ctx.runnable_wait_ns + (FLOWD_SLICE_DFL_NS >> 1))
				mode = FLOWCAUSAL_TASK_MODE_COMPUTE;
			else if (ctx.runnable_wait_ns >= (FLOWD_SLICE_DFL_NS >> 1))
				mode = FLOWCAUSAL_TASK_MODE_INTERACTIVE;
		}
		stage_id = stage_id_from_text(e->comm, cmdline, mode);

		flags = ctx.flags & ~FLOWCAUSAL_TASK_MODE_MASK;
		if (mode)
			flags |= mode;
		else
			flags |= FLOWCAUSAL_TASK_MODE_UNKNOWN;
		flags |= FLOWCAUSAL_TASK_C_ASSIST;

		/* BPF remains the primary classifier.  The C assist only promotes
		 * strong command-line evidence that BPF cannot derive from comm
		 * alone, such as "python -m pytest" or "python manage.py test".
		 * Existing BPF-owned frontier/background/width/accounting flags are
		 * otherwise preserved.
		 */
		if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE && strong_compute) {
			flags |= FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE |
				 FLOWCAUSAL_TASK_BPF_AUTO;
			if (ctx.confidence < assist_confidence)
				ctx.confidence = assist_confidence;
			if (ctx.confidence < min_confidence)
				ctx.confidence = min_confidence;
			if (ctx.propagation_depth < 1)
				ctx.propagation_depth = 1;
		}
		if (mode == FLOWCAUSAL_TASK_MODE_BACKGROUND && has_background_signal(cmdline)) {
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				   FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_PROPAGATE);
			flags |= FLOWCAUSAL_TASK_BACKGROUND | FLOWCAUSAL_TASK_BPF_AUTO;
			if (ctx.confidence < min_confidence)
				ctx.confidence = min_confidence;
			ctx.propagation_depth = 0;
		}
		flags |= stage_flags_from_text(cmdline);
		if (flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE) {
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				   FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_CONVERGE_TAIL);
			flags |= FLOWCAUSAL_TASK_PROPAGATE |
				 FLOWCAUSAL_TASK_BPF_AUTO;
			if (ctx.confidence < assist_confidence)
				ctx.confidence = assist_confidence;
			if (ctx.propagation_depth < 1)
				ctx.propagation_depth = 1;
		}
		if (flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL) {
			flags |= FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE |
				 FLOWCAUSAL_TASK_BPF_AUTO;
			if (ctx.confidence < assist_confidence)
				ctx.confidence = assist_confidence;
			if (ctx.propagation_depth < 1)
				ctx.propagation_depth = 1;
		}
		if (flags & FLOWCAUSAL_TASK_STAGE_PACKAGE_BG) {
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER | FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_PROPAGATE);
			ctx.confidence = min_confidence;
			ctx.propagation_depth = 0;
		}
		if (flags & FLOWCAUSAL_TASK_STAGE_INSPECT) {
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER | FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_PROPAGATE);
		}
		if (flags & FLOWCAUSAL_TASK_STAGE_BUILD_WIDE)
			flags &= ~FLOWCAUSAL_TASK_CONVERGE_TAIL;
		if (flags & FLOWCAUSAL_TASK_STAGE_TEST_TAIL)
			flags |= FLOWCAUSAL_TASK_CONVERGE_TAIL;
	}
	if (anchor_cpu >= 0)
		flags |= FLOWCAUSAL_TASK_LOCALITY;

	if (ctx.flags == flags &&
	    ctx.stage_id == stage_id &&
	    ctx.cohort_id == (control_session_id ^ stage_id))
		return;

	ctx.flags = flags;
	ctx.stage_id = stage_id;
	ctx.cohort_id = control_session_id ^ stage_id;
	ctx.last_update_ns = e->ts_ns ? e->ts_ns : monotonic_ns();
	bpf_map_update_elem(control_task_fd, &key, &ctx, BPF_ANY);

	if (control_policy_fd >= 0 &&
	    !bpf_map_lookup_elem(control_policy_fd, &ctx.session_id, &policy)) {
		policy.flags |= FLOWCAUSAL_POLICY_ENABLE;
		if (anchor_cpu >= 0)
			policy.flags |= FLOWCAUSAL_POLICY_LOCALITY;
		if (width_control)
			policy.flags |= FLOWCAUSAL_POLICY_WIDTH_CTL;
		policy.anchor_cpu = anchor_cpu;
		bpf_map_update_elem(control_policy_fd, &ctx.session_id, &policy, BPF_ANY);
	}

	control_updates++;
}

static void apply_control_plane_update(const struct flow_event *e)
{
	struct flow_task_key key = {};
	struct flow_task_ctx ctx = {};
	struct flow_session_policy policy = {};
	__u32 mode;
	__u32 flags;
	__u64 stage_id;
	char stage_env[64] = {};
	bool have_stage_env;

	if (assist_bpf_auto) {
		apply_bpf_assist_update(e);
		return;
	}

	if (!control_plane || control_task_fd < 0 || !e || !e->pid || !e->session_id)
		return;
	if (control_session_id && e->session_id != control_session_id)
		return;

	key.pid = e->pid;
	if (bpf_map_lookup_elem(control_task_fd, &key, &ctx))
		return;

	have_stage_env = read_stage_env(e->pid, stage_env, sizeof(stage_env));
	if (have_stage_env) {
		mode = stage_mode_from_name(stage_env);
		stage_id = stage_id_from_name(stage_env, mode);
		flags = ctx.flags & FLOWCAUSAL_TASK_LOCALITY;
		if (mode)
			flags |= mode;
		else
			flags |= FLOWCAUSAL_TASK_MODE_UNKNOWN;
		if (!strcmp(stage_env, "BUILD") || !strcmp(stage_env, "BUILD_WIDE")) {
			flags |= FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE;
			ctx.confidence = ctx.confidence > 80 ? ctx.confidence : 80;
		} else if (!strcmp(stage_env, "TEST") || !strcmp(stage_env, "RETRY_PROBE")) {
			flags |= FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE |
				 FLOWCAUSAL_TASK_CONVERGE_TAIL;
			ctx.confidence = ctx.confidence > min_confidence ? ctx.confidence : min_confidence;
		} else if (!strcmp(stage_env, "PACKAGE_BACKGROUND")) {
			flags |= FLOWCAUSAL_TASK_BACKGROUND;
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				   FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_PROPAGATE);
		} else if (!strcmp(stage_env, "INSPECT") || !strcmp(stage_env, "EDIT")) {
			flags |= FLOWCAUSAL_TASK_PROPAGATE;
			flags &= ~(FLOWCAUSAL_TASK_FRONTIER |
				   FLOWCAUSAL_TASK_COHORT_TAIL |
				   FLOWCAUSAL_TASK_BACKGROUND |
				   FLOWCAUSAL_TASK_CONVERGE_TAIL);
			ctx.confidence = ctx.confidence > min_confidence ? ctx.confidence : min_confidence;
		}
		if (control_plane && width_control && (flags & FLOWCAUSAL_TASK_FRONTIER))
			flags |= FLOWCAUSAL_TASK_BPF_AUTO;
	} else {
		mode = mode_from_comm(e->comm);
		if (!mode) {
			if (ctx.runtime_ns >= (FLOWD_SLICE_DFL_NS << 1) ||
			    ctx.runtime_ns > ctx.runnable_wait_ns + (FLOWD_SLICE_DFL_NS >> 1))
				mode = FLOWCAUSAL_TASK_MODE_COMPUTE;
			else if (ctx.runnable_wait_ns >= (FLOWD_SLICE_DFL_NS >> 1))
				mode = FLOWCAUSAL_TASK_MODE_INTERACTIVE;
		}

		flags = ctx.flags & FLOWCAUSAL_TASK_LOCALITY;
		stage_id = stage_id_from_comm(e->comm, mode);
		if (mode == FLOWCAUSAL_TASK_MODE_COMPUTE) {
			flags |= FLOWCAUSAL_TASK_FRONTIER |
				 FLOWCAUSAL_TASK_COHORT_TAIL |
				 FLOWCAUSAL_TASK_PROPAGATE;
			ctx.confidence = ctx.confidence > 80 ? ctx.confidence : 80;
		} else if (mode == FLOWCAUSAL_TASK_MODE_INTERACTIVE) {
			flags |= FLOWCAUSAL_TASK_PROPAGATE;
			ctx.confidence = ctx.confidence > min_confidence ? ctx.confidence : min_confidence;
		} else if (ctx.runtime_ns >= FLOWD_SLICE_DFL_NS) {
			flags |= FLOWCAUSAL_TASK_BACKGROUND;
		}
		if (anchor_cpu >= 0)
			flags |= FLOWCAUSAL_TASK_LOCALITY;
		if (mode)
			flags |= mode;
		else
			flags |= FLOWCAUSAL_TASK_MODE_UNKNOWN;
		if (control_plane && width_control && (flags & FLOWCAUSAL_TASK_FRONTIER))
			flags |= FLOWCAUSAL_TASK_BPF_AUTO;
	}
	ctx.flags = flags;
	ctx.stage_id = stage_id;
	ctx.cohort_id = control_session_id ^ stage_id;
	ctx.last_update_ns = e->ts_ns ? e->ts_ns : monotonic_ns();
	if (e->kind == FLOW_EVT_EXEC || e->kind == FLOW_EVT_FORK ||
	    e->kind == FLOW_EVT_WAKEUP || e->kind == FLOW_EVT_SWITCH_IN)
		bpf_map_update_elem(control_task_fd, &key, &ctx, BPF_ANY);

	if (control_policy_fd >= 0 &&
	    !bpf_map_lookup_elem(control_policy_fd, &ctx.session_id, &policy)) {
		if (anchor_cpu >= 0)
			policy.flags |= FLOWCAUSAL_POLICY_LOCALITY;
		if (width_control && (flags & FLOWCAUSAL_TASK_FRONTIER))
			policy.flags |= FLOWCAUSAL_POLICY_WIDTH_CTL;
		policy.flags |= FLOWCAUSAL_POLICY_ENABLE;
		policy.anchor_cpu = anchor_cpu;
		bpf_map_update_elem(control_policy_fd, &ctx.session_id, &policy, BPF_ANY);
	}

	control_updates++;
}

static int handle_event(void *ctx, void *data, size_t len)
{
	const struct flow_event *e = data;
	char comm[FLOWCAUSAL_MAX_COMM + 1] = {};

	(void)ctx;
	if (len < sizeof(*e))
		return 0;

	apply_control_plane_update(e);
	if (affinity_release_due(e->ts_ns ? e->ts_ns : monotonic_ns(), e))
		restore_root_affinity_mask();
	memcpy(comm, e->comm, FLOWCAUSAL_MAX_COMM);
	comm[FLOWCAUSAL_MAX_COMM] = '\0';
	if (enable_events) {
		printf("event ts=%" PRIu64 " kind=%s session=%" PRIu64
		       " pid=%u tgid=%u ppid=%u cpu=%u comm=%s flags=0x%x\n",
		       U64(e->ts_ns), event_name(e->kind), U64(e->session_id),
		       e->pid, e->tgid, e->ppid, e->cpu, comm, e->flags);
		printf("  mode=%s\n", mode_name(e->flags));
		fflush(stdout);
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct ring_buffer *rb = NULL;
	int task_fd = -1;
	int policy_fd = -1;
	int events_fd = -1;
	int err;

	err = parse_args(argc, argv);
	if (err)
		return 2;

	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

	task_fd = open_pinned_map("task_ctx", true);
	policy_fd = open_pinned_map("session_policy", true);
	if (task_fd < 0 || policy_fd < 0) {
		err = 1;
		goto out;
	}
	control_task_fd = task_fd;
	control_policy_fd = policy_fd;

	err = register_session(task_fd, policy_fd);
	if (err) {
		err = 1;
		goto out;
	}
	if (once || (!enable_events && !control_plane &&
		     !affinity_release_after_first_fork &&
		     !stage_state_file))
		goto out;

	if (enable_events || control_plane || affinity_release_after_first_fork) {
		events_fd = open_pinned_map("events", true);
		if (events_fd < 0) {
			err = 1;
			goto out;
		}
		rb = ring_buffer__new(events_fd, handle_event, NULL, NULL);
		if (!rb) {
			fprintf(stderr, "failed to create ringbuf reader: %s\n", strerror(errno));
			err = 1;
			goto out;
		}
	}

	if (control_plane) {
		printf("flowd control plane enabled for session=%" PRIu64
		       " updates=%" PRIu64 " task_fd=%d policy_fd=%d\n",
		       U64(control_session_id), U64(control_updates),
		       control_task_fd, control_policy_fd);
		if (assist_bpf_auto)
			printf("flowd BPF-main/C-assist mode enabled\n");
		fflush(stdout);
	}

	while (!exit_req) {
		if (stage_state_file)
			poll_stage_state_cleanup(task_fd);
		if (rb) {
			err = ring_buffer__poll(rb, stage_state_file ? 50 : 250);
			if (err == -EINTR)
				break;
			if (err < 0) {
				fprintf(stderr, "ringbuf poll failed: %d\n", err);
				break;
			}
		} else {
			usleep((useconds_t)stage_state_poll_ms * 1000U);
		}
	}
	err = 0;

out:
	ring_buffer__free(rb);
	if (events_fd >= 0)
		close(events_fd);
	if (policy_fd >= 0)
		close(policy_fd);
	if (task_fd >= 0)
		close(task_fd);
	if (control_plane) {
		printf("flowd exiting session=%" PRIu64 " updates=%" PRIu64 "\n",
		       U64(control_session_id), U64(control_updates));
		fflush(stdout);
	}
	if (stage_state_file) {
		printf("flowd exiting session=%" PRIu64
		       " stage_state_updates=%" PRIu64 "\n",
		       U64(control_session_id), U64(stage_state_updates));
		fflush(stdout);
	}
	control_task_fd = -1;
	control_policy_fd = -1;
	return err;
}
