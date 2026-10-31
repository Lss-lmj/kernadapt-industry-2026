/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Minimal userspace loader for the FlowCausal sched_ext prototype.
 *
 * This process attaches the scheduler and keeps the BPF object alive.  The
 * target runtime pairs it with flowd, a small C/libbpf daemon that registers
 * sessions and exports audit events without Python polling.
 */
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>

#ifndef FLOWCAUSAL_DEFAULT_PIN_DIR
#define FLOWCAUSAL_DEFAULT_PIN_DIR "/sys/fs/bpf/flowcausal"
#endif
#ifndef FLOWCAUSAL_DEFAULT_MODE_AWARE
#define FLOWCAUSAL_DEFAULT_MODE_AWARE 0
#endif
#ifndef FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER
#define FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER 0
#endif
#ifndef FLOWCAUSAL_BPF_SKEL_HEADER
#define FLOWCAUSAL_BPF_SKEL_HEADER "scx_flowcausal.bpf.skel.h"
#endif
#ifndef FLOWCAUSAL_BPF_SKEL_NAME
#define FLOWCAUSAL_BPF_SKEL_NAME scx_flowcausal
#endif

#define FLOWCAUSAL_SKEL_NAME1(name) name
#define FLOWCAUSAL_SKEL_NAME(name) FLOWCAUSAL_SKEL_NAME1(name)
#define FLOWCAUSAL_SKEL_DESTROY1(name) name##__destroy
#define FLOWCAUSAL_SKEL_DESTROY(name) FLOWCAUSAL_SKEL_DESTROY1(name)
#define FLOWCAUSAL_SCX_OPS_OPEN1(ops, name) SCX_OPS_OPEN(ops, name)
#define FLOWCAUSAL_SCX_OPS_OPEN(ops, name) FLOWCAUSAL_SCX_OPS_OPEN1(ops, name)
#define FLOWCAUSAL_SCX_OPS_LOAD1(skel, ops, name, uei) SCX_OPS_LOAD(skel, ops, name, uei)
#define FLOWCAUSAL_SCX_OPS_LOAD(skel, ops, name, uei) FLOWCAUSAL_SCX_OPS_LOAD1(skel, ops, name, uei)
#define FLOWCAUSAL_SCX_OPS_ATTACH1(skel, ops, name) SCX_OPS_ATTACH(skel, ops, name)
#define FLOWCAUSAL_SCX_OPS_ATTACH(skel, ops, name) FLOWCAUSAL_SCX_OPS_ATTACH1(skel, ops, name)

#include FLOWCAUSAL_BPF_SKEL_HEADER

static volatile int exit_req;
static const char *pin_dir = FLOWCAUSAL_DEFAULT_PIN_DIR;
static bool do_pin = true;
static bool mode_aware = FLOWCAUSAL_DEFAULT_MODE_AWARE ? true : false;
static bool bpf_auto_frontier = FLOWCAUSAL_DEFAULT_BPF_AUTO_FRONTIER ? true : false;
static unsigned int frontier_dispatch_quota = 4;
static unsigned int guard_cooldown_dispatches;
static unsigned int pressure_fallback_enqueues;
static unsigned int pressure_fallback_ms;
static unsigned int shared_wait_pressure_us;
static unsigned int pressure_dispatch_ms;
static unsigned int frontier_dispatch_window_ms;
static unsigned int frontier_dispatch_window_budget;
static unsigned int shared_slice_us;
static bool pressure_local_cpu;
static bool locality_frontier_only;
static bool locality_remembered_first;
static unsigned int shared_wakeup_slice_us;
static unsigned int shared_wakeup_min_block_us;
static bool shared_wakeup_preempt;
static bool frontier_wakeup_preempt;
static bool shared_local_dsq;
static bool tracked_local_dsq;
static bool frontier_fallback_local_dsq;
static bool shared_percpu_dsq;
static bool background_dsq;
static unsigned int background_dispatch_quota;

static void sigint_handler(int sig)
{
	(void)sig;
	exit_req = 1;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s [--pin-dir PATH] [--no-pin] [--frontier-dispatch-quota N]\n"
		"       [--guard-cooldown-dispatches N] [--pressure-fallback-enqueues N]\n"
		"       [--pressure-fallback-ms MS]\n"
		"       [--shared-wait-pressure-us US]\n"
		"       [--pressure-dispatch-ms MS]\n"
		"       [--pressure-local-cpu]\n"
		"       [--locality-frontier-only] [--locality-remembered-first]\n"
		"       [--frontier-dispatch-window-ms MS] [--frontier-dispatch-window-budget N]\n"
		"       [--shared-slice-us US]\n"
		"       [--shared-wakeup-preempt] [--shared-wakeup-slice-us US]\n"
		"       [--shared-wakeup-min-block-us US]\n"
			"       [--frontier-wakeup-preempt]\n"
			"       [--shared-local-dsq] [--tracked-local-dsq]\n"
			"       [--frontier-fallback-local-dsq]\n"
			"       [--shared-percpu-dsq] [--background-dsq]\n"
			"       [--mode-aware | --no-mode-aware]\n"
		"       [--background-dispatch-quota N]\n"
		"\n"
		"Options:\n"
		"  --pin-dir PATH  bpffs directory for FlowCausal maps\n"
		"                  default: /sys/fs/bpf/flowcausal\n"
		"  --no-pin        attach scheduler without pinning maps\n"
		"  --frontier-dispatch-quota N\n"
		"                  after N consecutive frontier dispatches, try shared first\n"
		"                  0 disables the shared-first guard; default: 4\n"
		"  --guard-cooldown-dispatches N\n"
		"                  after a successful shared-first guard dispatch, keep trying\n"
		"                  shared first for up to N dispatch opportunities; default: 0\n"
		"  --pressure-fallback-enqueues N\n"
		"                  after shared pressure is observed by a safety probe,\n"
		"                  send the next N admitted frontier enqueues to shared DSQ;\n"
		"                  0 disables admission-level pressure fallback; default: 0\n"
		"  --pressure-fallback-ms MS\n"
		"                  after shared pressure is observed, keep admission-level\n"
		"                  pressure fallback open for up to MS milliseconds;\n"
		"                  0 disables time-bounded pressure fallback; default: 0\n"
		"  --shared-wait-pressure-us US\n"
		"                  if an untracked shared task waits at least US microseconds\n"
		"                  before running, open the same pressure-fallback epoch;\n"
		"                  0 disables wait-triggered pressure; default: 0\n"
		"  --pressure-dispatch-ms MS\n"
		"                  after shared pressure is observed, try the shared DSQ before\n"
		"                  the frontier DSQ for up to MS milliseconds;\n"
		"                  0 disables dispatch-side pressure backoff; default: 0\n"
		"  --pressure-local-cpu\n"
		"                  confine pressure fallback/dispatch to the triggering CPU;\n"
		"                  default: off\n"
		"  --locality-frontier-only\n"
		"                  apply session locality anchors only to frontier tasks;\n"
		"                  ordinary inherited children use the shared path;\n"
		"                  default: off\n"
		"  --locality-remembered-first\n"
		"                  for anchored locality tasks, prefer the task's remembered\n"
		"                  CPU before falling back to the session anchor;\n"
		"                  default: off\n"
		"  --frontier-dispatch-window-ms MS\n"
		"                  wall-clock window for the soft frontier dispatch budget;\n"
		"                  0 disables the budget; default: 0\n"
		"  --frontier-dispatch-window-budget N\n"
		"                  after N frontier dispatches in the window, try shared first;\n"
		"                  0 disables the budget; default: 0\n"
		"  --shared-slice-us US\n"
		"                  slice used for unhinted/shared/fallback tasks;\n"
		"                  0 uses SCX_SLICE_DFL; default: 0\n"
		"  --shared-wakeup-preempt\n"
		"                  preempt the previous CPU when an untracked shared task\n"
		"                  wakes after blocking; default: off\n"
		"  --shared-wakeup-slice-us US\n"
		"                  slice used only for untracked blocked-wakeup tasks in\n"
		"                  the latency DSQ; 0 follows --shared-slice-us/default;\n"
		"                  default: 0\n"
		"  --shared-wakeup-min-block-us US\n"
		"                  require an untracked task to have blocked for at least\n"
		"                  US microseconds before --shared-wakeup-preempt routes\n"
		"                  it to the latency DSQ; 0 preserves legacy behavior;\n"
		"                  default: 0\n"
		"  --frontier-wakeup-preempt\n"
		"                  preempt the previous CPU when an admitted frontier task\n"
		"                  wakes after blocking; default: off\n"
		"  --shared-local-dsq\n"
		"                  enqueue ordinary shared/fallback tasks to their target\n"
		"                  CPU local DSQ instead of FlowCausal's global shared DSQ;\n"
		"                  latency wakeups and frontier tasks still use dedicated\n"
		"                  DSQs; default: off\n"
			"  --tracked-local-dsq\n"
			"                  enqueue only FlowCausal-tracked fallback workflow tasks\n"
			"                  to their target CPU local DSQ. Untracked co-runners stay\n"
			"                  on latency/global shared paths; default: off\n"
			"  --frontier-fallback-local-dsq\n"
			"                  enqueue only frontier fallback workflow tasks to their\n"
			"                  target CPU local DSQ; this is narrower than\n"
			"                  --tracked-local-dsq and keeps ordinary tracked tasks on\n"
			"                  the global shared path; default: off\n"
		"  --shared-percpu-dsq\n"
		"                  enqueue ordinary shared/fallback tasks to FlowCausal-owned\n"
		"                  per-CPU shared DSQs. Unlike --shared-local-dsq, dispatch\n"
		"                  still goes through FlowCausal and latency/frontier DSQs\n"
		"                  remain ordered by the scheduler; default: off\n"
		"  --background-dsq\n"
		"                  enqueue controller-marked sandbox background tasks to a\n"
		"                  low-priority DSQ drained after latency/frontier/shared;\n"
		"                  default: off\n"
		"  --mode-aware    enable per-task workflow mode routing; default follows\n"
		"                  the binary build preset\n"
		"  --no-mode-aware disable per-task workflow mode routing\n"
		"  --bpf-auto-frontier\n"
		"                  let the kernel auto-promote compute-like tracked tasks\n"
		"                  into frontier/cohort-tail/propagate state; default follows\n"
		"                  the binary build preset\n"
		"  --no-bpf-auto-frontier\n"
		"                  disable in-kernel automatic frontier promotion\n"
		"  --background-dispatch-quota N\n"
		"                  when --background-dsq is enabled, try one background\n"
		"                  dispatch after N foreground dispatches; 0 only drains\n"
		"                  background after latency/frontier/shared are empty;\n"
		"                  default: 0\n"
		"  --help          show this message\n",
		prog);
}

static int mkdir_p(const char *path)
{
	char tmp[PATH_MAX];
	size_t len;

	if (!path || !path[0])
		return -EINVAL;
	if (snprintf(tmp, sizeof(tmp), "%s", path) >= (int)sizeof(tmp))
		return -ENAMETOOLONG;

	len = strlen(tmp);
	if (len > 1 && tmp[len - 1] == '/')
		tmp[len - 1] = '\0';

	for (char *p = tmp + 1; *p; p++) {
		if (*p != '/')
			continue;
		*p = '\0';
		if (mkdir(tmp, 0755) && errno != EEXIST)
			return -errno;
		*p = '/';
	}

	if (mkdir(tmp, 0755) && errno != EEXIST)
		return -errno;
	return 0;
}

static int join_path(char *out, size_t out_sz, const char *dir, const char *name)
{
	int n = snprintf(out, out_sz, "%s/%s", dir, name);

	if (n < 0 || n >= (int)out_sz)
		return -ENAMETOOLONG;
	return 0;
}

static int pin_map(struct bpf_map *map, const char *name)
{
	char path[PATH_MAX];
	int err;

	err = join_path(path, sizeof(path), pin_dir, name);
	if (err)
		return err;

	/* Replace stale pins left by a previous crashed loader.  The map object
	 * itself remains alive as long as the scheduler process is alive.
	 */
	if (unlink(path) && errno != ENOENT)
		return -errno;

	err = bpf_map__pin(map, path);
	if (err) {
		fprintf(stderr, "failed to pin %s at %s: %d\n", name, path, err);
		return err;
	}
	return 0;
}

static void unpin_map(const char *name)
{
	char path[PATH_MAX];

	if (join_path(path, sizeof(path), pin_dir, name))
		return;
	(void)unlink(path);
}

static int pin_maps(struct FLOWCAUSAL_SKEL_NAME(FLOWCAUSAL_BPF_SKEL_NAME) *skel)
{
	int err;

	err = mkdir_p(pin_dir);
	if (err) {
		fprintf(stderr, "failed to create pin dir %s: %s\n",
			pin_dir, strerror(-err));
		return err;
	}

	if ((err = pin_map(skel->maps.task_ctx, "task_ctx")))
		return err;
	if ((err = pin_map(skel->maps.session_policy, "session_policy")))
		return err;
	if ((err = pin_map(skel->maps.session_stats, "session_stats")))
		return err;
	if ((err = pin_map(skel->maps.global_stats, "global_stats")))
		return err;
	if ((err = pin_map(skel->maps.events, "events")))
		return err;
	return 0;
}

static void unpin_maps(void)
{
	unpin_map("task_ctx");
	unpin_map("session_policy");
	unpin_map("session_stats");
	unpin_map("global_stats");
	unpin_map("events");
}

static int parse_args(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--help")) {
			usage(argv[0]);
			exit(0);
		} else if (!strcmp(argv[i], "--no-pin")) {
			do_pin = false;
		} else if (!strcmp(argv[i], "--pin-dir") && i + 1 < argc) {
			pin_dir = argv[++i];
		} else if (!strcmp(argv[i], "--frontier-dispatch-quota") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --frontier-dispatch-quota value\n");
				return -EINVAL;
			}
			frontier_dispatch_quota = (unsigned int)v;
		} else if (!strcmp(argv[i], "--guard-cooldown-dispatches") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --guard-cooldown-dispatches value\n");
				return -EINVAL;
			}
			guard_cooldown_dispatches = (unsigned int)v;
		} else if (!strcmp(argv[i], "--pressure-fallback-enqueues") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --pressure-fallback-enqueues value\n");
				return -EINVAL;
			}
			pressure_fallback_enqueues = (unsigned int)v;
		} else if (!strcmp(argv[i], "--pressure-fallback-ms") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --pressure-fallback-ms value\n");
				return -EINVAL;
			}
			pressure_fallback_ms = (unsigned int)v;
		} else if (!strcmp(argv[i], "--shared-wait-pressure-us") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --shared-wait-pressure-us value\n");
				return -EINVAL;
			}
			shared_wait_pressure_us = (unsigned int)v;
		} else if (!strcmp(argv[i], "--pressure-dispatch-ms") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --pressure-dispatch-ms value\n");
				return -EINVAL;
			}
			pressure_dispatch_ms = (unsigned int)v;
		} else if (!strcmp(argv[i], "--pressure-local-cpu")) {
			pressure_local_cpu = true;
		} else if (!strcmp(argv[i], "--locality-frontier-only")) {
			locality_frontier_only = true;
		} else if (!strcmp(argv[i], "--locality-remembered-first")) {
			locality_remembered_first = true;
		} else if (!strcmp(argv[i], "--frontier-dispatch-window-ms") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --frontier-dispatch-window-ms value\n");
				return -EINVAL;
			}
			frontier_dispatch_window_ms = (unsigned int)v;
		} else if (!strcmp(argv[i], "--frontier-dispatch-window-budget") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --frontier-dispatch-window-budget value\n");
				return -EINVAL;
			}
			frontier_dispatch_window_budget = (unsigned int)v;
		} else if (!strcmp(argv[i], "--shared-slice-us") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --shared-slice-us value\n");
				return -EINVAL;
			}
			shared_slice_us = (unsigned int)v;
		} else if (!strcmp(argv[i], "--shared-wakeup-preempt")) {
			shared_wakeup_preempt = true;
		} else if (!strcmp(argv[i], "--frontier-wakeup-preempt")) {
			frontier_wakeup_preempt = true;
		} else if (!strcmp(argv[i], "--shared-wakeup-slice-us") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --shared-wakeup-slice-us value\n");
				return -EINVAL;
			}
			shared_wakeup_slice_us = (unsigned int)v;
		} else if (!strcmp(argv[i], "--shared-wakeup-min-block-us") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --shared-wakeup-min-block-us value\n");
				return -EINVAL;
			}
			shared_wakeup_min_block_us = (unsigned int)v;
		} else if (!strcmp(argv[i], "--shared-local-dsq")) {
			shared_local_dsq = true;
		} else if (!strcmp(argv[i], "--tracked-local-dsq")) {
			tracked_local_dsq = true;
		} else if (!strcmp(argv[i], "--frontier-fallback-local-dsq")) {
			frontier_fallback_local_dsq = true;
		} else if (!strcmp(argv[i], "--shared-percpu-dsq")) {
			shared_percpu_dsq = true;
		} else if (!strcmp(argv[i], "--background-dsq")) {
			background_dsq = true;
		} else if (!strcmp(argv[i], "--mode-aware")) {
			mode_aware = true;
		} else if (!strcmp(argv[i], "--no-mode-aware")) {
			mode_aware = false;
		} else if (!strcmp(argv[i], "--bpf-auto-frontier")) {
			bpf_auto_frontier = true;
		} else if (!strcmp(argv[i], "--no-bpf-auto-frontier")) {
			bpf_auto_frontier = false;
		} else if (!strcmp(argv[i], "--background-dispatch-quota") && i + 1 < argc) {
			char *end = NULL;
			unsigned long v = strtoul(argv[++i], &end, 10);

			if (!end || *end || v > UINT_MAX) {
				fprintf(stderr, "invalid --background-dispatch-quota value\n");
				return -EINVAL;
			}
			background_dispatch_quota = (unsigned int)v;
		} else {
			fprintf(stderr, "unknown or incomplete argument: %s\n", argv[i]);
			usage(argv[0]);
			return -EINVAL;
		}
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct FLOWCAUSAL_SKEL_NAME(FLOWCAUSAL_BPF_SKEL_NAME) *skel;
	struct bpf_link *link;
	__u64 ecode;
	int err;

	err = parse_args(argc, argv);
	if (err)
		return 2;

	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

	skel = FLOWCAUSAL_SCX_OPS_OPEN(flowcausal_ops, FLOWCAUSAL_BPF_SKEL_NAME);
	if (!skel) {
		fprintf(stderr, "failed to open FlowCausal BPF skeleton\n");
		return 1;
	}
	skel->rodata->default_frontier_dispatch_quota = frontier_dispatch_quota;
	skel->rodata->default_guard_cooldown_dispatches = guard_cooldown_dispatches;
	skel->rodata->default_pressure_fallback_enqueues = pressure_fallback_enqueues;
	skel->rodata->default_pressure_fallback_ns =
		(__u64)pressure_fallback_ms * 1000000ULL;
	skel->rodata->default_shared_wait_pressure_ns =
		(__u64)shared_wait_pressure_us * 1000ULL;
	skel->rodata->default_pressure_dispatch_ns =
		(__u64)pressure_dispatch_ms * 1000000ULL;
	skel->rodata->default_pressure_local_cpu = pressure_local_cpu;
	skel->rodata->default_locality_frontier_only = locality_frontier_only;
	skel->rodata->default_locality_remembered_first = locality_remembered_first;
	skel->rodata->default_frontier_dispatch_window_ns =
		(__u64)frontier_dispatch_window_ms * 1000000ULL;
	skel->rodata->default_frontier_dispatch_window_budget =
		frontier_dispatch_window_budget;
	skel->rodata->default_shared_slice_ns =
		(__u64)shared_slice_us * 1000ULL;
	skel->rodata->default_shared_wakeup_preempt = shared_wakeup_preempt;
	skel->rodata->default_shared_wakeup_slice_ns =
		(__u64)shared_wakeup_slice_us * 1000ULL;
	skel->rodata->default_shared_wakeup_min_block_ns =
		(__u64)shared_wakeup_min_block_us * 1000ULL;
	skel->rodata->default_frontier_wakeup_preempt = frontier_wakeup_preempt;
	skel->rodata->default_shared_local_dsq = shared_local_dsq;
	skel->rodata->default_tracked_local_dsq = tracked_local_dsq;
	skel->rodata->default_frontier_fallback_local_dsq =
		frontier_fallback_local_dsq;
	skel->rodata->default_shared_percpu_dsq = shared_percpu_dsq;
	skel->rodata->default_background_dsq = background_dsq;
	skel->rodata->default_background_dispatch_quota = background_dispatch_quota;
	skel->rodata->default_mode_aware = mode_aware;
	skel->rodata->default_bpf_auto_frontier = bpf_auto_frontier;

	FLOWCAUSAL_SCX_OPS_LOAD(skel, flowcausal_ops, FLOWCAUSAL_BPF_SKEL_NAME, uei);

	if (do_pin) {
		err = pin_maps(skel);
		if (err) {
			FLOWCAUSAL_SKEL_DESTROY(FLOWCAUSAL_BPF_SKEL_NAME)(skel);
			return 1;
		}
	}

	link = FLOWCAUSAL_SCX_OPS_ATTACH(skel, flowcausal_ops,
					 FLOWCAUSAL_BPF_SKEL_NAME);
	if (!link) {
		fprintf(stderr, "failed to attach FlowCausal sched_ext ops: %s\n",
			strerror(errno));
		if (do_pin)
			unpin_maps();
		FLOWCAUSAL_SKEL_DESTROY(FLOWCAUSAL_BPF_SKEL_NAME)(skel);
		return 1;
	}

	printf("FlowCausal scheduler attached.");
	if (do_pin)
		printf(" Maps pinned under %s.", pin_dir);
	printf(" frontier_dispatch_quota=%u.", frontier_dispatch_quota);
	printf(" guard_cooldown_dispatches=%u.", guard_cooldown_dispatches);
	printf(" pressure_fallback_enqueues=%u.", pressure_fallback_enqueues);
	printf(" pressure_fallback_ms=%u.", pressure_fallback_ms);
	printf(" shared_wait_pressure_us=%u.", shared_wait_pressure_us);
	printf(" pressure_dispatch_ms=%u.", pressure_dispatch_ms);
	printf(" pressure_local_cpu=%s.", pressure_local_cpu ? "on" : "off");
	printf(" locality_frontier_only=%s.", locality_frontier_only ? "on" : "off");
	printf(" locality_remembered_first=%s.", locality_remembered_first ? "on" : "off");
	printf(" frontier_dispatch_window_ms=%u.", frontier_dispatch_window_ms);
	printf(" frontier_dispatch_window_budget=%u.", frontier_dispatch_window_budget);
	printf(" shared_slice_us=%u.", shared_slice_us);
	printf(" shared_wakeup_preempt=%s.", shared_wakeup_preempt ? "on" : "off");
	printf(" shared_wakeup_slice_us=%u.", shared_wakeup_slice_us);
	printf(" shared_wakeup_min_block_us=%u.", shared_wakeup_min_block_us);
	printf(" frontier_wakeup_preempt=%s.", frontier_wakeup_preempt ? "on" : "off");
	printf(" shared_local_dsq=%s.", shared_local_dsq ? "on" : "off");
	printf(" tracked_local_dsq=%s.", tracked_local_dsq ? "on" : "off");
	printf(" frontier_fallback_local_dsq=%s.",
	       frontier_fallback_local_dsq ? "on" : "off");
	printf(" shared_percpu_dsq=%s.", shared_percpu_dsq ? "on" : "off");
	printf(" background_dsq=%s.", background_dsq ? "on" : "off");
	printf(" background_dispatch_quota=%u.", background_dispatch_quota);
	printf(" mode_aware=%s.", mode_aware ? "on" : "off");
	printf(" bpf_auto_frontier=%s.", bpf_auto_frontier ? "on" : "off");
	printf(" Use flowd to register sandbox sessions. Press Ctrl-C to exit.\n");

	while (!exit_req && !UEI_EXITED(skel, uei))
		sleep(1);

	bpf_link__destroy(link);
	ecode = UEI_REPORT(skel, uei);
	if (do_pin)
		unpin_maps();
	FLOWCAUSAL_SKEL_DESTROY(FLOWCAUSAL_BPF_SKEL_NAME)(skel);

	if (UEI_ECODE_RESTART(ecode))
		return 1;
	return 0;
}
