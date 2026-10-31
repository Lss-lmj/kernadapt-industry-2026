// SPDX-License-Identifier: MIT
/*
 * flowbench.c
 *
 * A small sandbox-workflow microbenchmark for FlowCausal characterization.
 *
 * Each stage forks a process cohort.  Children burn a configurable amount of
 * *thread CPU time* and then exit.  The last child in the cohort is marked as
 * the oracle frontier/tail by default.  The parent waits for every child with
 * wait4(), records per-child wall time and rusage CPU time, and reports a
 * simple runqueue/wait proxy:
 *
 *     wait_proxy = child_wall_time - child_user_cpu - child_sys_cpu
 *
 * For a CPU-bound child whose work target is measured in CPU time, this proxy
 * captures time spent runnable but not executing plus scheduler/interference
 * overhead.  It is not a replacement for BPF runqueue tracing; it is a portable
 * gate for deciding whether the CPU-frontier hypothesis is worth pursuing.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

struct options {
	int stages;
	int cohort;
	int frontier_width;
	int work_ms;
	int tail_work_ms;
	int stage_gap_ms;
	int start_delay_ms;
	const char *session;
	bool quiet_children;
};

struct child_rec {
	pid_t pid;
	int stage;
	int index;
	bool frontier;
	uint64_t fork_ns;
	uint64_t done_ns;
	uint64_t user_ns;
	uint64_t sys_ns;
	int status;
};

static uint64_t now_ns(clockid_t clk)
{
	struct timespec ts;

	if (clock_gettime(clk, &ts) != 0) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static uint64_t timeval_to_ns(struct timeval tv)
{
	return (uint64_t)tv.tv_sec * 1000000000ULL + (uint64_t)tv.tv_usec * 1000ULL;
}

static void burn_thread_cpu_ms(int target_ms)
{
	uint64_t target_ns = (uint64_t)target_ms * 1000000ULL;
	uint64_t start = now_ns(CLOCK_THREAD_CPUTIME_ID);
	volatile uint64_t x = 0x9e3779b97f4a7c15ULL;

	while (now_ns(CLOCK_THREAD_CPUTIME_ID) - start < target_ns) {
		for (int i = 0; i < 4096; i++) {
			x ^= x << 7;
			x ^= x >> 9;
			x *= 0xbf58476d1ce4e5b9ULL;
		}
	}

	/* Prevent the compiler from proving the loop dead. */
	if (x == 0xdeadbeefULL)
		_exit(3);
}

static void sleep_ms(int ms)
{
	struct timespec ts;

	if (ms <= 0)
		return;

	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (long)(ms % 1000) * 1000000L;
	while (nanosleep(&ts, &ts) != 0 && errno == EINTR)
		;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s [options]\n"
		"\n"
		"Options:\n"
		"  --stages N          number of workflow stages (default 3)\n"
		"  --cohort N          children per stage (default 8)\n"
		"  --frontier-width N  tail children marked frontier (default 1)\n"
		"  --work-ms N         CPU ms per non-frontier child (default 25)\n"
		"  --tail-work-ms N    CPU ms for oracle frontier child (default 60)\n"
		"  --stage-gap-ms N    sleep between stages (default 0)\n"
		"  --start-delay-ms N  child sleep before CPU work, for live hints (default 0)\n"
		"  --session NAME      stable session label (default pid:<pid>)\n"
		"  --help              show this help\n",
		prog);
}

static int parse_int_arg(const char *name, const char *value, int min_value)
{
	char *end = NULL;
	long parsed;

	errno = 0;
	parsed = strtol(value, &end, 10);
	if (errno || !end || *end || parsed < min_value || parsed > INT32_MAX) {
		fprintf(stderr, "invalid %s: %s\n", name, value);
		exit(2);
	}
	return (int)parsed;
}

static struct options parse_options(int argc, char **argv)
{
	struct options opt = {
		.stages = 3,
		.cohort = 8,
		.frontier_width = 1,
		.work_ms = 25,
		.tail_work_ms = 60,
		.stage_gap_ms = 0,
		.start_delay_ms = 0,
		.session = NULL,
		.quiet_children = true,
	};

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--help")) {
			usage(argv[0]);
			exit(0);
		} else if (!strcmp(argv[i], "--stages") && i + 1 < argc) {
			opt.stages = parse_int_arg("--stages", argv[++i], 1);
		} else if (!strcmp(argv[i], "--cohort") && i + 1 < argc) {
			opt.cohort = parse_int_arg("--cohort", argv[++i], 1);
		} else if (!strcmp(argv[i], "--frontier-width") && i + 1 < argc) {
			opt.frontier_width = parse_int_arg("--frontier-width", argv[++i], 1);
		} else if (!strcmp(argv[i], "--work-ms") && i + 1 < argc) {
			opt.work_ms = parse_int_arg("--work-ms", argv[++i], 1);
		} else if (!strcmp(argv[i], "--tail-work-ms") && i + 1 < argc) {
			opt.tail_work_ms = parse_int_arg("--tail-work-ms", argv[++i], 1);
		} else if (!strcmp(argv[i], "--stage-gap-ms") && i + 1 < argc) {
			opt.stage_gap_ms = parse_int_arg("--stage-gap-ms", argv[++i], 0);
		} else if (!strcmp(argv[i], "--start-delay-ms") && i + 1 < argc) {
			opt.start_delay_ms = parse_int_arg("--start-delay-ms", argv[++i], 0);
		} else if (!strcmp(argv[i], "--session") && i + 1 < argc) {
			opt.session = argv[++i];
		} else {
			fprintf(stderr, "unknown or incomplete argument: %s\n", argv[i]);
			usage(argv[0]);
			exit(2);
		}
	}

	if (opt.frontier_width > opt.cohort) {
		fprintf(stderr, "--frontier-width cannot exceed --cohort\n");
		exit(2);
	}

	return opt;
}

static struct child_rec *find_rec(struct child_rec *records, int nrecords, pid_t pid)
{
	for (int i = 0; i < nrecords; i++) {
		if (records[i].pid == pid)
			return &records[i];
	}
	return NULL;
}

static void print_child_json(const char *session, const struct child_rec *rec,
			     int start_delay_ms)
{
	uint64_t wall_ns = rec->done_ns - rec->fork_ns;
	uint64_t cpu_ns = rec->user_ns + rec->sys_ns;
	uint64_t wait_proxy_ns = wall_ns > cpu_ns ? wall_ns - cpu_ns : 0;
	uint64_t sync_delay_ns = (uint64_t)start_delay_ms * 1000000ULL;
	uint64_t sched_wait_proxy_ns =
		wait_proxy_ns > sync_delay_ns ? wait_proxy_ns - sync_delay_ns : 0;

	printf("{\"type\":\"child\",\"session\":\"%s\",\"stage\":%d,"
	       "\"index\":%d,\"pid\":%d,\"frontier\":%s,"
	       "\"wall_ns\":%" PRIu64 ",\"cpu_ns\":%" PRIu64 ","
	       "\"user_ns\":%" PRIu64 ",\"sys_ns\":%" PRIu64 ","
	       "\"wait_proxy_ns\":%" PRIu64 ","
	       "\"sync_delay_ns\":%" PRIu64 ","
	       "\"sched_wait_proxy_ns\":%" PRIu64 ",\"status\":%d}\n",
	       session, rec->stage, rec->index, rec->pid,
	       rec->frontier ? "true" : "false",
	       wall_ns, cpu_ns, rec->user_ns, rec->sys_ns,
	       wait_proxy_ns, sync_delay_ns, sched_wait_proxy_ns, rec->status);
}

static void print_oracle_frontier_json(const char *session, int stage, int index,
				       pid_t pid, uint64_t fork_ns)
{
	printf("{\"type\":\"oracle_frontier\",\"session\":\"%s\","
	       "\"stage\":%d,\"index\":%d,\"pid\":%d,"
	       "\"fork_ns\":%" PRIu64 "}\n",
	       session, stage, index, pid, fork_ns);
}

static void print_stage_json(const char *session, int stage, uint64_t stage_ns,
			     const struct child_rec *records, int nrecords,
			     int start_delay_ms)
{
	uint64_t total_wait = 0;
	uint64_t frontier_wait = 0;
	uint64_t total_sched_wait = 0;
	uint64_t frontier_sched_wait = 0;
	uint64_t total_wall = 0;
	uint64_t frontier_wall = 0;
	uint64_t sync_delay_ns = (uint64_t)start_delay_ms * 1000000ULL;
	int frontier_count = 0;

	for (int i = 0; i < nrecords; i++) {
		uint64_t wall_ns = records[i].done_ns - records[i].fork_ns;
		uint64_t cpu_ns = records[i].user_ns + records[i].sys_ns;
		uint64_t wait_proxy_ns = wall_ns > cpu_ns ? wall_ns - cpu_ns : 0;
		uint64_t sched_wait_proxy_ns =
			wait_proxy_ns > sync_delay_ns ? wait_proxy_ns - sync_delay_ns : 0;

		total_wait += wait_proxy_ns;
		total_sched_wait += sched_wait_proxy_ns;
		total_wall += wall_ns;
		if (records[i].frontier) {
			frontier_wait += wait_proxy_ns;
			frontier_sched_wait += sched_wait_proxy_ns;
			frontier_wall += wall_ns;
			frontier_count++;
		}
	}

	printf("{\"type\":\"stage\",\"session\":\"%s\",\"stage\":%d,"
	       "\"stage_ns\":%" PRIu64 ",\"cohort\":%d,"
	       "\"frontier_count\":%d,\"total_child_wall_ns\":%" PRIu64 ","
	       "\"total_wait_proxy_ns\":%" PRIu64 ","
	       "\"total_sched_wait_proxy_ns\":%" PRIu64 ","
	       "\"frontier_wall_ns\":%" PRIu64 ","
	       "\"frontier_wait_proxy_ns\":%" PRIu64 ","
	       "\"frontier_sched_wait_proxy_ns\":%" PRIu64 "}\n",
	       session, stage, stage_ns, nrecords, frontier_count,
	       total_wall, total_wait, total_sched_wait,
	       frontier_wall, frontier_wait, frontier_sched_wait);
}

int main(int argc, char **argv)
{
	struct options opt = parse_options(argc, argv);
	char default_session[64];
	const char *session = opt.session;
	uint64_t bench_start = now_ns(CLOCK_MONOTONIC);
	uint64_t bench_end;

	if (!session) {
		snprintf(default_session, sizeof(default_session), "pid:%d", getpid());
		session = default_session;
	}

	setvbuf(stdout, NULL, _IOLBF, 0);

	printf("{\"type\":\"bench_start\",\"session\":\"%s\",\"pid\":%d,"
	       "\"stages\":%d,\"cohort\":%d,\"frontier_width\":%d,"
	       "\"work_ms\":%d,"
	       "\"tail_work_ms\":%d,\"stage_gap_ms\":%d,"
	       "\"start_delay_ms\":%d}\n",
	       session, getpid(), opt.stages, opt.cohort, opt.frontier_width,
	       opt.work_ms, opt.tail_work_ms, opt.stage_gap_ms,
	       opt.start_delay_ms);

	for (int stage = 0; stage < opt.stages; stage++) {
		int nrecords = opt.cohort;
		struct child_rec *records = calloc((size_t)nrecords, sizeof(*records));
		uint64_t stage_start = now_ns(CLOCK_MONOTONIC);

		if (!records) {
			perror("calloc");
			return 2;
		}

		for (int i = 0; i < opt.cohort; i++) {
			bool frontier = i >= opt.cohort - opt.frontier_width;
			int work_ms = frontier ? opt.tail_work_ms : opt.work_ms;
			uint64_t fork_ns = now_ns(CLOCK_MONOTONIC);
			pid_t pid = fork();

			if (pid < 0) {
				perror("fork");
				return 2;
			}
			if (pid == 0) {
				sleep_ms(opt.start_delay_ms);
				burn_thread_cpu_ms(work_ms);
				_exit(frontier ? 11 : 0);
			}

			records[i].pid = pid;
			records[i].stage = stage;
			records[i].index = i;
			records[i].frontier = frontier;
			records[i].fork_ns = fork_ns;

			if (frontier)
				print_oracle_frontier_json(session, stage, i, pid, fork_ns);
		}

		for (int waited = 0; waited < opt.cohort; waited++) {
			struct rusage ru;
			int status = 0;
			pid_t pid = wait4(-1, &status, 0, &ru);
			uint64_t done_ns = now_ns(CLOCK_MONOTONIC);
			struct child_rec *rec;

			if (pid < 0) {
				perror("wait4");
				return 2;
			}

			rec = find_rec(records, nrecords, pid);
			if (!rec) {
				fprintf(stderr, "unknown child pid %d\n", pid);
				return 2;
			}

			rec->done_ns = done_ns;
			rec->user_ns = timeval_to_ns(ru.ru_utime);
			rec->sys_ns = timeval_to_ns(ru.ru_stime);
			rec->status = status;
			print_child_json(session, rec, opt.start_delay_ms);
		}

		print_stage_json(session, stage, now_ns(CLOCK_MONOTONIC) - stage_start,
				 records, nrecords, opt.start_delay_ms);
		free(records);
		sleep_ms(opt.stage_gap_ms);
	}

	bench_end = now_ns(CLOCK_MONOTONIC);
	printf("{\"type\":\"bench_end\",\"session\":\"%s\",\"pid\":%d,"
	       "\"wall_ns\":%" PRIu64 "}\n",
	       session, getpid(), bench_end - bench_start);
	return 0;
}
