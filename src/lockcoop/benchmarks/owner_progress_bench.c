// SPDX-License-Identifier: MIT
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>

#define HIST_BUCKETS 64

enum bench_mode {
	MODE_CONVOY = 0,
	MODE_PHASED = 1,
	MODE_OWNER_PREEMPT = 2,
};

struct config {
	enum bench_mode mode;
	int threads;
	int locks;
	int groups;
	int hog_threads;
	int duration_s;
	int critical_us;
	int outside_us;
	int owner_yields;
	int pin_cpus;
	int start_delay_ms;
};

struct worker_stat {
	uint64_t ops;
	uint64_t wait_ns_sum;
	uint64_t wait_ns_max;
	uint64_t hist[HIST_BUCKETS];
};

struct worker_arg {
	int id;
	int group_id;
	int is_owner;
	int is_hog;
	const struct config *cfg;
	pthread_mutex_t *locks;
	struct worker_stat *stat;
};

static atomic_int start_flag;
static atomic_int stop_flag;
static atomic_int *ready_waiters;
static atomic_int *phase_no;
static atomic_int *completed_waiters;

static const char *mode_name(enum bench_mode mode)
{
	switch (mode) {
	case MODE_CONVOY:
		return "convoy";
	case MODE_PHASED:
		return "phased";
	case MODE_OWNER_PREEMPT:
		return "owner-preempt";
	default:
		return "unknown";
	}
}

static int is_group_mode(enum bench_mode mode)
{
	return mode == MODE_PHASED || mode == MODE_OWNER_PREEMPT;
}

static uint64_t nsec_now(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void busy_wait_us(int us)
{
	uint64_t end;

	if (us <= 0)
		return;
	end = nsec_now() + (uint64_t)us * 1000ULL;
	while (nsec_now() < end)
		__asm__ __volatile__("pause" ::: "memory");
}

static int hist_bucket_us(uint64_t us)
{
	int bucket = 0;
	uint64_t upper = 1;

	while (bucket + 1 < HIST_BUCKETS && upper < us) {
		upper <<= 1;
		bucket++;
	}
	return bucket;
}

static uint64_t bucket_upper_us(int bucket)
{
	if (bucket <= 0)
		return 1;
	if (bucket >= 63)
		return UINT64_MAX;
	return 1ULL << bucket;
}

static void hist_add(struct worker_stat *stat, uint64_t ns)
{
	uint64_t us = (ns + 999ULL) / 1000ULL;
	int bucket = hist_bucket_us(us);

	stat->hist[bucket]++;
}

static uint64_t hist_percentile_us(const uint64_t hist[HIST_BUCKETS], uint64_t total, double q)
{
	uint64_t target;
	uint64_t seen = 0;

	if (!total)
		return 0;
	target = (uint64_t)ceil((double)total * q);
	if (!target)
		target = 1;
	for (int i = 0; i < HIST_BUCKETS; i++) {
		seen += hist[i];
		if (seen >= target)
			return bucket_upper_us(i);
	}
	return bucket_upper_us(HIST_BUCKETS - 1);
}

static int pin_current_thread(int worker_id, int pin_cpus)
{
	long online;
	cpu_set_t set;
	int cpu;

	if (pin_cpus <= 0)
		return 0;
	online = sysconf(_SC_NPROCESSORS_ONLN);
	if (online <= 0)
		return -1;
	if (pin_cpus > online)
		pin_cpus = (int)online;
	cpu = worker_id % pin_cpus;
	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	return pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

static void *convoy_worker_main(void *data)
{
	struct worker_arg *arg = data;
	const struct config *cfg = arg->cfg;
	struct worker_stat *stat = arg->stat;
	uint64_t op = 0;

	if (pin_current_thread(arg->id, cfg->pin_cpus) != 0) {
		fprintf(stderr, "warning: failed to pin worker %d: %s\n", arg->id, strerror(errno));
	}

	while (!atomic_load_explicit(&start_flag, memory_order_acquire))
		__asm__ __volatile__("pause" ::: "memory");

	while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
		int lock_id = cfg->locks == 1 ? 0 : (int)((op + (uint64_t)arg->id) % (uint64_t)cfg->locks);
		uint64_t before = nsec_now();
		uint64_t after;

		pthread_mutex_lock(&arg->locks[lock_id]);
		after = nsec_now();
		hist_add(stat, after - before);
		stat->wait_ns_sum += after - before;
		if (after - before > stat->wait_ns_max)
			stat->wait_ns_max = after - before;

		busy_wait_us(cfg->critical_us);
		pthread_mutex_unlock(&arg->locks[lock_id]);

		busy_wait_us(cfg->outside_us);
		stat->ops++;
		op++;
	}
	return NULL;
}

static void *phased_owner_main(void *data)
{
	struct worker_arg *arg = data;
	const struct config *cfg = arg->cfg;
	struct worker_stat *stat = arg->stat;
	int group = arg->group_id;
	int waiters = cfg->threads / cfg->groups - 1;
	int phase = 0;

	if (pin_current_thread(arg->id, cfg->pin_cpus) != 0)
		fprintf(stderr, "warning: failed to pin owner %d: %s\n", arg->id, strerror(errno));

	while (!atomic_load_explicit(&start_flag, memory_order_acquire))
		__asm__ __volatile__("pause" ::: "memory");
	while (atomic_load_explicit(&ready_waiters[group], memory_order_acquire) < waiters &&
	       !atomic_load_explicit(&stop_flag, memory_order_relaxed))
		__asm__ __volatile__("pause" ::: "memory");

	while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
		pthread_mutex_lock(&arg->locks[group]);
		atomic_store_explicit(&completed_waiters[group], 0, memory_order_release);
		phase++;
		atomic_store_explicit(&phase_no[group], phase, memory_order_release);

		for (int i = 0; i < cfg->owner_yields; i++)
			sched_yield();
		busy_wait_us(cfg->critical_us);
		pthread_mutex_unlock(&arg->locks[group]);

		while (atomic_load_explicit(&completed_waiters[group], memory_order_acquire) < waiters &&
		       !atomic_load_explicit(&stop_flag, memory_order_relaxed))
			__asm__ __volatile__("pause" ::: "memory");

		busy_wait_us(cfg->outside_us);
		stat->ops++;
	}
	return NULL;
}

static void *phased_waiter_main(void *data)
{
	struct worker_arg *arg = data;
	const struct config *cfg = arg->cfg;
	struct worker_stat *stat = arg->stat;
	int group = arg->group_id;
	int local_phase = 0;

	if (pin_current_thread(arg->id, cfg->pin_cpus) != 0)
		fprintf(stderr, "warning: failed to pin waiter %d: %s\n", arg->id, strerror(errno));

	while (!atomic_load_explicit(&start_flag, memory_order_acquire))
		__asm__ __volatile__("pause" ::: "memory");
	atomic_fetch_add_explicit(&ready_waiters[group], 1, memory_order_acq_rel);

	while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
		int phase;
		uint64_t before;
		uint64_t after;

		do {
			phase = atomic_load_explicit(&phase_no[group], memory_order_acquire);
			if (atomic_load_explicit(&stop_flag, memory_order_relaxed))
				return NULL;
			__asm__ __volatile__("pause" ::: "memory");
		} while (phase <= local_phase);
		local_phase = phase;

		before = nsec_now();
		pthread_mutex_lock(&arg->locks[group]);
		after = nsec_now();
		hist_add(stat, after - before);
		stat->wait_ns_sum += after - before;
		if (after - before > stat->wait_ns_max)
			stat->wait_ns_max = after - before;
		pthread_mutex_unlock(&arg->locks[group]);

		stat->ops++;
		atomic_fetch_add_explicit(&completed_waiters[group], 1, memory_order_acq_rel);
	}
	return NULL;
}

static void *owner_preempt_owner_main(void *data)
{
	struct worker_arg *arg = data;
	const struct config *cfg = arg->cfg;
	struct worker_stat *stat = arg->stat;
	int group = arg->group_id;
	int waiters = cfg->threads / cfg->groups - 1;
	int phase = 0;

	if (pin_current_thread(arg->id, cfg->pin_cpus) != 0)
		fprintf(stderr, "warning: failed to pin owner %d: %s\n", arg->id, strerror(errno));

	while (!atomic_load_explicit(&start_flag, memory_order_acquire))
		__asm__ __volatile__("pause" ::: "memory");
	while (atomic_load_explicit(&ready_waiters[group], memory_order_acquire) < waiters &&
	       !atomic_load_explicit(&stop_flag, memory_order_relaxed))
		__asm__ __volatile__("pause" ::: "memory");

	while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
		pthread_mutex_lock(&arg->locks[group]);
		atomic_store_explicit(&completed_waiters[group], 0, memory_order_release);
		phase++;
		atomic_store_explicit(&phase_no[group], phase, memory_order_release);

		for (int i = 0; i < cfg->owner_yields; i++)
			sched_yield();
		busy_wait_us(cfg->critical_us);
		pthread_mutex_unlock(&arg->locks[group]);

		while (atomic_load_explicit(&completed_waiters[group], memory_order_acquire) < waiters &&
		       !atomic_load_explicit(&stop_flag, memory_order_relaxed))
			__asm__ __volatile__("pause" ::: "memory");

		busy_wait_us(cfg->outside_us);
		stat->ops++;
	}
	return NULL;
}

static void *owner_preempt_waiter_main(void *data)
{
	struct worker_arg *arg = data;
	const struct config *cfg = arg->cfg;
	struct worker_stat *stat = arg->stat;
	int group = arg->group_id;
	int local_phase = 0;

	if (pin_current_thread(arg->id, cfg->pin_cpus) != 0)
		fprintf(stderr, "warning: failed to pin waiter %d: %s\n", arg->id, strerror(errno));

	while (!atomic_load_explicit(&start_flag, memory_order_acquire))
		__asm__ __volatile__("pause" ::: "memory");
	atomic_fetch_add_explicit(&ready_waiters[group], 1, memory_order_acq_rel);

	while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
		int phase;
		uint64_t before;
		uint64_t after;

		do {
			phase = atomic_load_explicit(&phase_no[group], memory_order_acquire);
			if (atomic_load_explicit(&stop_flag, memory_order_relaxed))
				return NULL;
			__asm__ __volatile__("pause" ::: "memory");
		} while (phase <= local_phase);
		local_phase = phase;

		before = nsec_now();
		pthread_mutex_lock(&arg->locks[group]);
		after = nsec_now();
		hist_add(stat, after - before);
		stat->wait_ns_sum += after - before;
		if (after - before > stat->wait_ns_max)
			stat->wait_ns_max = after - before;
		pthread_mutex_unlock(&arg->locks[group]);
		stat->ops++;
		atomic_fetch_add_explicit(&completed_waiters[group], 1, memory_order_acq_rel);
	}
	return NULL;
}

static void *hog_worker_main(void *data)
{
	struct worker_arg *arg = data;
	const struct config *cfg = arg->cfg;
	uint64_t spins = 0;

	if (pin_current_thread(arg->id, cfg->pin_cpus) != 0)
		fprintf(stderr, "warning: failed to pin hog %d: %s\n", arg->id, strerror(errno));

	while (!atomic_load_explicit(&start_flag, memory_order_acquire))
		__asm__ __volatile__("pause" ::: "memory");

	while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
		for (int i = 0; i < 1000; i++)
			__asm__ __volatile__("pause" ::: "memory");
		spins++;
	}
	arg->stat->ops = spins;
	return NULL;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s [options]\n"
		"\n"
		"Options:\n"
			"  --mode MODE          convoy, phased, or owner-preempt (default: phased)\n"
			"  --threads N          Worker threads (default: 128)\n"
			"  --locks N            Contended mutex count (default: 1)\n"
			"  --groups N           Independent phased owner/waiter groups (default: 8)\n"
			"  --hog-threads N      Extra CPU-bound pressure threads (default: 0)\n"
			"  --duration SEC       Measurement duration (default: 20)\n"
		"  --critical-us US     Busy time while holding the mutex (default: 100)\n"
		"  --outside-us US      Busy time outside the mutex (default: 0)\n"
		"  --owner-yields N     sched_yield calls by phased owner while holding the mutex (default: 0)\n"
		"  --pin-cpus N         Pin workers to first N CPUs; 0 disables pinning (default: 0)\n"
		"  --start-delay-ms MS  Delay before workers start, for attaching monitors (default: 1000)\n",
		prog);
}

static int parse_int_arg(const char *value, const char *name, int min_value)
{
	char *end = NULL;
	long v;

	errno = 0;
	v = strtol(value, &end, 10);
	if (errno || !end || *end || v < min_value || v > INT32_MAX) {
		fprintf(stderr, "invalid %s: %s\n", name, value);
		exit(2);
	}
	return (int)v;
}

static void parse_args(int argc, char **argv, struct config *cfg)
{
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
			const char *mode = argv[++i];
			if (strcmp(mode, "phased") == 0) {
				cfg->mode = MODE_PHASED;
			} else if (strcmp(mode, "convoy") == 0) {
				cfg->mode = MODE_CONVOY;
			} else if (strcmp(mode, "owner-preempt") == 0 || strcmp(mode, "owner_preempt") == 0) {
				cfg->mode = MODE_OWNER_PREEMPT;
			} else {
				fprintf(stderr, "invalid mode: %s\n", mode);
				exit(2);
			}
		} else if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
			cfg->threads = parse_int_arg(argv[++i], "threads", 1);
		} else if (strcmp(argv[i], "--locks") == 0 && i + 1 < argc) {
			cfg->locks = parse_int_arg(argv[++i], "locks", 1);
		} else if (strcmp(argv[i], "--groups") == 0 && i + 1 < argc) {
			cfg->groups = parse_int_arg(argv[++i], "groups", 1);
		} else if (strcmp(argv[i], "--hog-threads") == 0 && i + 1 < argc) {
			cfg->hog_threads = parse_int_arg(argv[++i], "hog-threads", 0);
		} else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
			cfg->duration_s = parse_int_arg(argv[++i], "duration", 1);
		} else if (strcmp(argv[i], "--critical-us") == 0 && i + 1 < argc) {
			cfg->critical_us = parse_int_arg(argv[++i], "critical-us", 0);
		} else if (strcmp(argv[i], "--outside-us") == 0 && i + 1 < argc) {
			cfg->outside_us = parse_int_arg(argv[++i], "outside-us", 0);
		} else if (strcmp(argv[i], "--owner-yields") == 0 && i + 1 < argc) {
			cfg->owner_yields = parse_int_arg(argv[++i], "owner-yields", 0);
		} else if (strcmp(argv[i], "--pin-cpus") == 0 && i + 1 < argc) {
			cfg->pin_cpus = parse_int_arg(argv[++i], "pin-cpus", 0);
		} else if (strcmp(argv[i], "--start-delay-ms") == 0 && i + 1 < argc) {
			cfg->start_delay_ms = parse_int_arg(argv[++i], "start-delay-ms", 0);
		} else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
			usage(argv[0]);
			exit(0);
		} else {
			usage(argv[0]);
			exit(2);
		}
	}
}

static double jain_fairness_selected(const struct worker_stat *stats, const struct worker_arg *args, int n)
{
	long double sum = 0.0;
	long double sum_sq = 0.0;
	int selected = 0;

	for (int i = 0; i < n; i++) {
		if (args[i].is_owner || args[i].is_hog)
			continue;
		sum += (long double)stats[i].ops;
		sum_sq += (long double)stats[i].ops * (long double)stats[i].ops;
		selected++;
	}
	if (selected == 0 || sum_sq == 0.0)
		return 0.0;
	return (double)((sum * sum) / ((long double)selected * sum_sq));
}

int main(int argc, char **argv)
{
	struct config cfg = {
		.mode = MODE_PHASED,
		.threads = 128,
		.locks = 1,
		.groups = 8,
		.hog_threads = 0,
		.duration_s = 20,
		.critical_us = 100,
		.outside_us = 0,
		.owner_yields = 0,
		.pin_cpus = 0,
		.start_delay_ms = 1000,
	};
	pthread_mutex_t *locks = NULL;
	pthread_t *threads = NULL;
	struct worker_stat *stats = NULL;
	struct worker_arg *args = NULL;
	uint64_t total_ops = 0;
	uint64_t wait_ns_sum = 0;
	uint64_t wait_ns_max = 0;
	uint64_t hist[HIST_BUCKETS] = {};
	uint64_t ops_min = UINT64_MAX;
	uint64_t ops_max = 0;
	int zero_progress = 0;
	uint64_t start_ns;
	uint64_t end_ns;
	uint64_t owner_rounds = 0;
	uint64_t hog_spins = 0;
	int measured_threads = 0;
	int group_mode;
	int total_threads;
	double elapsed_s;

	parse_args(argc, argv, &cfg);
	group_mode = is_group_mode(cfg.mode);
	if (group_mode) {
		if (cfg.threads < 2 || cfg.groups < 1 || cfg.groups > cfg.threads) {
			fprintf(stderr, "%s mode requires 1 <= groups <= threads and threads >= 2\n",
				mode_name(cfg.mode));
			return 2;
		}
		if (cfg.threads % cfg.groups != 0) {
			fprintf(stderr, "%s mode requires threads divisible by groups\n",
				mode_name(cfg.mode));
			return 2;
		}
		if (cfg.threads / cfg.groups < 2) {
			fprintf(stderr, "%s mode requires at least one owner and one waiter per group\n",
				mode_name(cfg.mode));
			return 2;
		}
		cfg.locks = cfg.groups;
	}
	total_threads = cfg.threads + cfg.hog_threads;
	if (total_threads <= 0) {
		fprintf(stderr, "threads + hog-threads must be positive\n");
		return 2;
	}

	locks = calloc((size_t)cfg.locks, sizeof(*locks));
	threads = calloc((size_t)total_threads, sizeof(*threads));
	stats = calloc((size_t)total_threads, sizeof(*stats));
	args = calloc((size_t)total_threads, sizeof(*args));
	if (!locks || !threads || !stats || !args) {
		fprintf(stderr, "allocation failure\n");
		return 1;
	}
	if (group_mode) {
		ready_waiters = calloc((size_t)cfg.groups, sizeof(*ready_waiters));
		phase_no = calloc((size_t)cfg.groups, sizeof(*phase_no));
		if (cfg.mode == MODE_PHASED || cfg.mode == MODE_OWNER_PREEMPT)
			completed_waiters = calloc((size_t)cfg.groups, sizeof(*completed_waiters));
		if (!ready_waiters || !phase_no ||
		    ((cfg.mode == MODE_PHASED || cfg.mode == MODE_OWNER_PREEMPT) && !completed_waiters)) {
			fprintf(stderr, "group state allocation failure\n");
			return 1;
		}
	}

	for (int i = 0; i < cfg.locks; i++)
		pthread_mutex_init(&locks[i], NULL);

	atomic_store(&start_flag, 0);
	atomic_store(&stop_flag, 0);
	for (int i = 0; i < cfg.groups && group_mode; i++) {
		atomic_store(&ready_waiters[i], 0);
		atomic_store(&phase_no[i], 0);
		if (completed_waiters)
			atomic_store(&completed_waiters[i], 0);
	}
	for (int i = 0; i < cfg.threads; i++) {
		int group_size = group_mode ? cfg.threads / cfg.groups : 0;
		void *(*fn)(void *) = convoy_worker_main;

		args[i].id = i;
		args[i].group_id = group_mode ? i / group_size : 0;
		args[i].is_owner = group_mode && i % group_size == 0;
		args[i].is_hog = 0;
		args[i].cfg = &cfg;
		args[i].locks = locks;
		args[i].stat = &stats[i];
		if (cfg.mode == MODE_PHASED)
			fn = args[i].is_owner ? phased_owner_main : phased_waiter_main;
		else if (cfg.mode == MODE_OWNER_PREEMPT)
			fn = args[i].is_owner ? owner_preempt_owner_main : owner_preempt_waiter_main;
		if (pthread_create(&threads[i], NULL, fn, &args[i]) != 0) {
			fprintf(stderr, "pthread_create failed at %d\n", i);
			return 1;
		}
	}
	for (int i = 0; i < cfg.hog_threads; i++) {
		int idx = cfg.threads + i;

		args[idx].id = idx;
		args[idx].group_id = -1;
		args[idx].is_owner = 0;
		args[idx].is_hog = 1;
		args[idx].cfg = &cfg;
		args[idx].locks = locks;
		args[idx].stat = &stats[idx];
		if (pthread_create(&threads[idx], NULL, hog_worker_main, &args[idx]) != 0) {
			fprintf(stderr, "pthread_create failed at hog %d\n", i);
			return 1;
		}
	}

	if (cfg.start_delay_ms > 0) {
		struct timespec delay = {
			.tv_sec = cfg.start_delay_ms / 1000,
			.tv_nsec = (long)(cfg.start_delay_ms % 1000) * 1000000L,
		};
		nanosleep(&delay, NULL);
	}

	start_ns = nsec_now();
	atomic_store_explicit(&start_flag, 1, memory_order_release);
	sleep((unsigned int)cfg.duration_s);
	atomic_store(&stop_flag, 1);

	for (int i = 0; i < total_threads; i++)
		pthread_join(threads[i], NULL);
	end_ns = nsec_now();
	elapsed_s = (double)(end_ns - start_ns) / 1000000000.0;

	for (int i = 0; i < total_threads; i++) {
		if (args[i].is_hog) {
			hog_spins += stats[i].ops;
			continue;
		}
		if (args[i].is_owner) {
			owner_rounds += stats[i].ops;
			continue;
		}
		total_ops += stats[i].ops;
		measured_threads++;
		wait_ns_sum += stats[i].wait_ns_sum;
		if (stats[i].wait_ns_max > wait_ns_max)
			wait_ns_max = stats[i].wait_ns_max;
		if (stats[i].ops < ops_min)
			ops_min = stats[i].ops;
		if (stats[i].ops > ops_max)
			ops_max = stats[i].ops;
		if (stats[i].ops == 0)
			zero_progress++;
		for (int b = 0; b < HIST_BUCKETS; b++)
			hist[b] += stats[i].hist[b];
	}
	if (ops_min == UINT64_MAX)
		ops_min = 0;

	printf("owner_progress_bench\n");
	printf("Mode             : %s\n", mode_name(cfg.mode));
	printf("Threads          : %d\n", cfg.threads);
	printf("Hog threads      : %d\n", cfg.hog_threads);
	if (group_mode) {
		printf("Groups           : %d\n", cfg.groups);
		printf("Waiters          : %d\n", cfg.threads - cfg.groups);
		printf("Waiters/group    : %d\n", cfg.threads / cfg.groups - 1);
	}
	printf("Locks            : %d\n", cfg.locks);
	printf("Duration         : %.3f s\n", elapsed_s);
	printf("Critical section : %d us\n", cfg.critical_us);
	printf("Outside work     : %d us\n", cfg.outside_us);
	printf("Owner yields     : %d\n", cfg.owner_yields);
	printf("Pinned CPUs      : %d\n", cfg.pin_cpus);
	if (group_mode)
		printf("Owner rounds     : %" PRIu64 "\n", owner_rounds);
	if (cfg.hog_threads > 0)
		printf("Hog spins        : %" PRIu64 "\n", hog_spins);
	printf("Total ops        : %" PRIu64 "\n", total_ops);
	printf("Throughput       : %.3f ops/s\n", elapsed_s > 0.0 ? (double)total_ops / elapsed_s : 0.0);
	printf("Ops/thread min   : %" PRIu64 "\n", ops_min);
	printf("Ops/thread max   : %" PRIu64 "\n", ops_max);
	printf("Ops/thread mean  : %.3f\n", measured_threads > 0 ? (double)total_ops / (double)measured_threads : 0.0);
	printf("Zero-progress thr: %d\n", zero_progress);
	printf("Jain fairness    : %.9f\n", jain_fairness_selected(stats, args, total_threads));
	printf("Lock wait avg    : %.3f us\n", total_ops ? (double)wait_ns_sum / (double)total_ops / 1000.0 : 0.0);
	printf("Lock wait p50    : %" PRIu64 " us\n", hist_percentile_us(hist, total_ops, 0.50));
	printf("Lock wait p95    : %" PRIu64 " us\n", hist_percentile_us(hist, total_ops, 0.95));
	printf("Lock wait p99    : %" PRIu64 " us\n", hist_percentile_us(hist, total_ops, 0.99));
	printf("Lock wait p99.9  : %" PRIu64 " us\n", hist_percentile_us(hist, total_ops, 0.999));
	printf("Lock wait max    : %.3f us\n", (double)wait_ns_max / 1000.0);
	fflush(stdout);

	for (int i = 0; i < cfg.locks; i++)
		pthread_mutex_destroy(&locks[i]);
	free(args);
	free(stats);
	free(threads);
	free(locks);
	free(ready_waiters);
	free(phase_no);
	free(completed_waiters);
	return 0;
}
