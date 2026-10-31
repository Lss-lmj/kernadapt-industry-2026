/* SPDX-License-Identifier: GPL-2.0 */
/*
 * flowmon.c
 *
 * Small observability helper for the FlowCausal sched_ext prototype.  The
 * loader pins maps under /sys/fs/bpf/flowcausal; flowmon opens those maps,
 * prints session-level mechanism counters, and can subscribe to the event
 * ring buffer.  This is intentionally a measurement/debugging tool, not a
 * policy engine.
 */
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
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

static volatile sig_atomic_t exit_req;

static const char *pin_dir = "/sys/fs/bpf/flowcausal";
static bool show_events;
static bool show_stats = true;
static bool once;
static unsigned int interval_sec = 1;

static void sigint_handler(int sig)
{
	(void)sig;
	exit_req = 1;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s [--pin-dir PATH] [--events] [--no-stats] [--once] [--interval SEC]\n"
		"\n"
		"Options:\n"
		"  --pin-dir PATH  bpffs directory containing FlowCausal maps\n"
		"                  default: /sys/fs/bpf/flowcausal\n"
		"  --events        subscribe to the FlowCausal ringbuf event stream\n"
		"  --no-stats      do not periodically print session stats/policies\n"
		"  --once          print one stats snapshot and exit\n"
		"  --interval SEC  stats refresh interval, default: 1\n"
		"  --help          show this message\n",
		prog);
}

static int parse_args(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--help")) {
			usage(argv[0]);
			exit(0);
		} else if (!strcmp(argv[i], "--pin-dir") && i + 1 < argc) {
			pin_dir = argv[++i];
		} else if (!strcmp(argv[i], "--events")) {
			show_events = true;
		} else if (!strcmp(argv[i], "--no-stats")) {
			show_stats = false;
		} else if (!strcmp(argv[i], "--once")) {
			once = true;
		} else if (!strcmp(argv[i], "--interval") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v == 0 || v > 3600) {
				fprintf(stderr, "invalid --interval value\n");
				return -EINVAL;
			}
			interval_sec = (unsigned int)v;
		} else {
			fprintf(stderr, "unknown or incomplete argument: %s\n", argv[i]);
			usage(argv[0]);
			return -EINVAL;
		}
	}

	if (!show_stats && !show_events) {
		fprintf(stderr, "nothing to show: remove --no-stats or add --events\n");
		return -EINVAL;
	}
	return 0;
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

static void print_task_flags(__u32 flags)
{
	bool first = true;

	printf("[");
	if (flags & FLOWCAUSAL_TASK_FRONTIER) {
		printf("%sfrontier", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_COHORT_TAIL) {
		printf("%stail", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_LOCALITY) {
		printf("%slocality", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_WIDTH_HELD) {
		printf("%swidth-held", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_PROPAGATE) {
		printf("%spropagate", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_PRESSURE_HELD) {
		printf("%spressure-held", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_OBSERVE_ONLY) {
		printf("%sobserve", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_TASK_BACKGROUND) {
		printf("%sbackground", first ? "" : ",");
		first = false;
	}
	if ((flags & FLOWCAUSAL_TASK_MODE_MASK) == FLOWCAUSAL_TASK_MODE_INTERACTIVE) {
		printf("%smode-interactive", first ? "" : ",");
		first = false;
	} else if ((flags & FLOWCAUSAL_TASK_MODE_MASK) == FLOWCAUSAL_TASK_MODE_COMPUTE) {
		printf("%smode-compute", first ? "" : ",");
		first = false;
	} else if ((flags & FLOWCAUSAL_TASK_MODE_MASK) == FLOWCAUSAL_TASK_MODE_TAIL) {
		printf("%smode-tail", first ? "" : ",");
		first = false;
	} else if ((flags & FLOWCAUSAL_TASK_MODE_MASK) == FLOWCAUSAL_TASK_MODE_BACKGROUND) {
		printf("%smode-background", first ? "" : ",");
		first = false;
	}
	printf("]");
}

static void print_policy_flags(__u32 flags)
{
	bool first = true;

	printf("[");
	if (flags & FLOWCAUSAL_POLICY_ENABLE) {
		printf("%senable", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_POLICY_LOCALITY) {
		printf("%slocality", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_POLICY_WIDTH_CTL) {
		printf("%swidth-ctl", first ? "" : ",");
		first = false;
	}
	if (flags & FLOWCAUSAL_POLICY_FALLBACK) {
		printf("%sfallback", first ? "" : ",");
		first = false;
	}
	printf("]");
}

static int handle_event(void *ctx, void *data, size_t len)
{
	const struct flow_event *e = data;
	char comm[FLOWCAUSAL_MAX_COMM + 1] = {};

	(void)ctx;
	if (len < sizeof(*e)) {
		fprintf(stderr, "short FlowCausal event: %zu bytes\n", len);
		return 0;
	}

	memcpy(comm, e->comm, FLOWCAUSAL_MAX_COMM);
	comm[FLOWCAUSAL_MAX_COMM] = '\0';

	printf("event ts=%" PRIu64 " kind=%s session=%" PRIu64
	       " stage=%" PRIu64 " cohort=%" PRIu64
	       " pid=%u tgid=%u ppid=%u cpu=%u comm=%s flags=",
	       U64(e->ts_ns), event_name(e->kind), U64(e->session_id),
	       U64(e->stage_id), U64(e->cohort_id), e->pid, e->tgid,
	       e->ppid, e->cpu, comm);
	print_task_flags(e->flags);
	printf("\n");
	fflush(stdout);
	return 0;
}

static void print_policies(int policy_fd)
{
	__u64 key = 0;
	__u64 prev_key = 0;
	bool have_prev = false;
	struct flow_session_policy p;
	int count = 0;

	if (policy_fd < 0)
		return;

	printf("FlowCausal policies:\n");
	while (bpf_map_get_next_key(policy_fd, have_prev ? &prev_key : NULL, &key) == 0) {
		if (bpf_map_lookup_elem(policy_fd, &key, &p) == 0) {
			printf("  session=%" PRIu64 " flags=", U64(key));
			print_policy_flags(p.flags);
			printf(" min_conf=%u width=%u boost=%u window=%" PRIu64
			       " budget=%" PRIu64 "/%" PRIu64 " budget_start=%" PRIu64
			       " expires=%" PRIu64 " anchor_cpu=%d\n",
			       p.min_confidence, p.max_frontier_width,
			       p.max_vtime_boost, U64(p.window_ns),
			       U64(p.budget_used_ns),
			       U64(p.boost_budget_ns),
			       U64(p.budget_window_start_ns),
			       U64(p.expires_ns), p.anchor_cpu);
			count++;
		}
		prev_key = key;
		have_prev = true;
	}
	if (!count)
		printf("  <empty>\n");
}

static void print_stats(int stats_fd)
{
	__u64 key = 0;
	__u64 prev_key = 0;
	bool have_prev = false;
	struct flow_session_stats s;
	int count = 0;

	if (stats_fd < 0)
		return;

	printf("FlowCausal stats:\n");
	while (bpf_map_get_next_key(stats_fd, have_prev ? &prev_key : NULL, &key) == 0) {
		if (bpf_map_lookup_elem(stats_fd, &key, &s) == 0) {
			double frontier_ratio = 0.0;
			double fallback_ratio = 0.0;

			if (s.seen_enqueues) {
				frontier_ratio = (double)s.frontier_enqueues /
						 (double)s.seen_enqueues;
				fallback_ratio = (double)s.fallback_enqueues /
						 (double)s.seen_enqueues;
			}

			printf("  session=%" PRIu64
			       " enq=%" PRIu64 " frontier=%" PRIu64
			       " fallback=%" PRIu64 " width_held=%" PRIu64
			       " inflight=%" PRIu64 " width_releases=%" PRIu64
			       " frontier_ratio=%.3f fallback_ratio=%.3f\n",
			       U64(key), U64(s.seen_enqueues),
			       U64(s.frontier_enqueues),
			       U64(s.fallback_enqueues),
			       U64(s.width_held_enqueues),
			       U64(s.frontier_inflight),
			       U64(s.frontier_width_releases),
			       frontier_ratio, fallback_ratio);
			printf("    wait_ns total=%" PRIu64 " frontier=%" PRIu64
			       " runtime_ns total=%" PRIu64 " frontier=%" PRIu64
			       " last_event=%" PRIu64 "\n",
			       U64(s.total_runnable_wait_ns),
			       U64(s.frontier_runnable_wait_ns),
			       U64(s.total_runtime_ns),
			       U64(s.frontier_runtime_ns),
			       U64(s.last_event_ns));
			count++;
		}
		prev_key = key;
		have_prev = true;
	}
	if (!count)
		printf("  <empty>\n");
}

static void print_global_stats(int global_fd)
{
	__u32 key = 0;
	struct flow_global_stats s;

	if (global_fd < 0)
		return;

	printf("FlowCausal global stats:\n");
	if (bpf_map_lookup_elem(global_fd, &key, &s) != 0) {
		printf("  <empty>\n");
		return;
	}

	printf("  dispatch frontier=%" PRIu64 " shared=%" PRIu64
	       " shared_first=%" PRIu64 " guard_trips=%" PRIu64
	       " guard_misses=%" PRIu64 " cooldown_entries=%" PRIu64
	       " cooldown_dispatches=%" PRIu64 " cooldown_misses=%" PRIu64
	       " pressure_fallback_entries=%" PRIu64
	       " pressure_fallback_enqueues=%" PRIu64
	       " shared_wait_pressure_events=%" PRIu64
	       " shared_wait_pressure_wait_ns=%" PRIu64
	       " pressure_dispatch_entries=%" PRIu64
	       " pressure_dispatches=%" PRIu64
	       " pressure_dispatch_misses=%" PRIu64
	       " shared_wakeup_preempt_entries=%" PRIu64
	       " shared_wakeup_preempt_dispatches=%" PRIu64
	       " shared_wakeup_preempt_misses=%" PRIu64
	       " shared_local_enqueues=%" PRIu64
	       " shared_percpu_enqueues=%" PRIu64
	       " shared_percpu_dispatches=%" PRIu64
	       " shared_percpu_misses=%" PRIu64
	       " frontier_wakeup_preempt_entries=%" PRIu64
		       " background_enqueues=%" PRIu64
		       " background_dispatches=%" PRIu64
		       " frontier_dsq_enqueues=%" PRIu64
		       " tracked_shared_path_enqueues=%" PRIu64
		       " untracked_shared_path_enqueues=%" PRIu64
		       " shared_global_enqueues=%" PRIu64
		       " shared_global_tracked_enqueues=%" PRIu64
		       " shared_global_untracked_enqueues=%" PRIu64
		       " latency_dsq_enqueues=%" PRIu64
		       " shared_local_tracked_enqueues=%" PRIu64
		       " shared_local_untracked_enqueues=%" PRIu64
		       " shared_percpu_tracked_enqueues=%" PRIu64
		       " shared_percpu_untracked_enqueues=%" PRIu64
		       " shared_local_pick_fallbacks=%" PRIu64
		       " shared_percpu_pick_fallbacks=%" PRIu64
		       " shared_local_frontier_fallback_enqueues=%" PRIu64
		       " mode_enq_interactive=%" PRIu64
		       " mode_enq_compute=%" PRIu64
		       " mode_enq_tail=%" PRIu64
		       " mode_enq_background=%" PRIu64
		       " mode_dispatch_interactive=%" PRIu64
		       " mode_dispatch_compute=%" PRIu64
		       " mode_dispatch_tail=%" PRIu64
		       " mode_dispatch_background=%" PRIu64
		       " converge_dsq_enqueues=%" PRIu64
		       " converge_dispatches=%" PRIu64
		       " session_frontier_dsq_enqueues=%" PRIu64
		       " session_frontier_dispatches=%" PRIu64
			       " adaptive_width_tail_admissions=%" PRIu64
			       " adaptive_width_wide_admissions=%" PRIu64
			       " adaptive_width_held_enqueues=%" PRIu64
			       " root_copy_dsq_enqueues=%" PRIu64
			       " root_copy_dispatches=%" PRIu64
			       " frontier_budget_windows=%" PRIu64
			       " frontier_budget_trips=%" PRIu64
			       " frontier_budget_shared=%" PRIu64
	       " frontier_budget_misses=%" PRIu64 "\n",
	       U64(s.frontier_dispatches), U64(s.shared_dispatches),
	       U64(s.shared_first_dispatches), U64(s.frontier_guard_trips),
	       U64(s.frontier_guard_misses),
	       U64(s.shared_cooldown_entries),
	       U64(s.shared_cooldown_dispatches),
	       U64(s.shared_cooldown_misses),
	       U64(s.pressure_fallback_entries),
	       U64(s.pressure_fallback_enqueues),
	       U64(s.shared_wait_pressure_events),
	       U64(s.shared_wait_pressure_wait_ns),
	       U64(s.pressure_dispatch_entries),
	       U64(s.pressure_dispatches),
	       U64(s.pressure_dispatch_misses),
	       U64(s.shared_wakeup_preempt_entries),
	       U64(s.shared_wakeup_preempt_dispatches),
	       U64(s.shared_wakeup_preempt_misses),
	       U64(s.shared_local_enqueues),
	       U64(s.shared_percpu_enqueues),
	       U64(s.shared_percpu_dispatches),
	       U64(s.shared_percpu_misses),
		       U64(s.frontier_wakeup_preempt_entries),
		       U64(s.background_enqueues),
		       U64(s.background_dispatches),
		       U64(s.frontier_dsq_enqueues),
		       U64(s.tracked_shared_path_enqueues),
		       U64(s.untracked_shared_path_enqueues),
		       U64(s.shared_global_enqueues),
		       U64(s.shared_global_tracked_enqueues),
		       U64(s.shared_global_untracked_enqueues),
		       U64(s.latency_dsq_enqueues),
		       U64(s.shared_local_tracked_enqueues),
		       U64(s.shared_local_untracked_enqueues),
		       U64(s.shared_percpu_tracked_enqueues),
		       U64(s.shared_percpu_untracked_enqueues),
		       U64(s.shared_local_pick_fallbacks),
		       U64(s.shared_percpu_pick_fallbacks),
		       U64(s.shared_local_frontier_fallback_enqueues),
		       U64(s.mode_interactive_enqueues),
		       U64(s.mode_compute_enqueues),
		       U64(s.mode_tail_enqueues),
		       U64(s.mode_background_enqueues),
		       U64(s.mode_interactive_dispatches),
		       U64(s.mode_compute_dispatches),
		       U64(s.mode_tail_dispatches),
		       U64(s.mode_background_dispatches),
		       U64(s.converge_dsq_enqueues),
		       U64(s.converge_dispatches),
		       U64(s.session_frontier_dsq_enqueues),
		       U64(s.session_frontier_dispatches),
			       U64(s.adaptive_width_tail_admissions),
			       U64(s.adaptive_width_wide_admissions),
			       U64(s.adaptive_width_held_enqueues),
			       U64(s.root_copy_dsq_enqueues),
			       U64(s.root_copy_dispatches),
			       U64(s.frontier_budget_windows),
		       U64(s.frontier_budget_trips),
		       U64(s.frontier_budget_shared_dispatches),
	       U64(s.frontier_budget_misses));
}

static void print_snapshot(int policy_fd, int stats_fd, int global_fd)
{
	time_t now = time(NULL);
	char stamp[64] = {};
	struct tm tm;

	localtime_r(&now, &tm);
	strftime(stamp, sizeof(stamp), "%F %T", &tm);
	printf("\n== FlowCausal snapshot @ %s ==\n", stamp);
	print_policies(policy_fd);
	print_stats(stats_fd);
	print_global_stats(global_fd);
	fflush(stdout);
}

int main(int argc, char **argv)
{
	struct ring_buffer *rb = NULL;
	int policy_fd = -1;
	int stats_fd = -1;
	int global_fd = -1;
	int events_fd = -1;
	time_t next_stats = 0;
	int err;

	err = parse_args(argc, argv);
	if (err)
		return 2;

	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

	if (show_stats) {
		policy_fd = open_pinned_map("session_policy", true);
		stats_fd = open_pinned_map("session_stats", true);
		global_fd = open_pinned_map("global_stats", false);
		if (policy_fd < 0 || stats_fd < 0)
			return 1;
	}

	if (show_events) {
		events_fd = open_pinned_map("events", true);
		if (events_fd < 0)
			return 1;
		rb = ring_buffer__new(events_fd, handle_event, NULL, NULL);
		if (!rb) {
			fprintf(stderr, "failed to create ringbuf reader: %s\n",
				strerror(errno));
			return 1;
		}
	}

	if (show_stats) {
		print_snapshot(policy_fd, stats_fd, global_fd);
		if (once)
			goto out;
		next_stats = time(NULL) + interval_sec;
	}

	while (!exit_req) {
		if (show_events) {
			err = ring_buffer__poll(rb, 250);
			if (err == -EINTR)
				break;
			if (err < 0) {
				fprintf(stderr, "ringbuf poll failed: %d\n", err);
				break;
			}
		} else {
			sleep(interval_sec);
		}

		if (show_stats && time(NULL) >= next_stats) {
			print_snapshot(policy_fd, stats_fd, global_fd);
			next_stats = time(NULL) + interval_sec;
		}
	}

out:
	ring_buffer__free(rb);
	if (events_fd >= 0)
		close(events_fd);
	if (policy_fd >= 0)
		close(policy_fd);
	if (stats_fd >= 0)
		close(stats_fd);
	if (global_fd >= 0)
		close(global_fd);
	return 0;
}
