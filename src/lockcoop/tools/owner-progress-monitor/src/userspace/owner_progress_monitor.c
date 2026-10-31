// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <fcntl.h>
#include <gelf.h>
#include <getopt.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "common.h"
#include "owner_progress.skel.h"

struct options {
	pid_t pid;
	const char *library;
	const char *out_json;
	int duration_s;
	int min_wait_us;
	int pthread_owner_offset;
	bool verbose;
};

struct hist_summary {
	uint64_t buckets[OWNER_HIST_BUCKETS];
	uint64_t samples;
	double p50_us;
	double p95_us;
	double p99_us;
	double max_bucket_upper_us;
};

static volatile sig_atomic_t exiting;

static const char *counter_names[OWNER_CNT_NR] = {
	"lock_enter",
	"lock_success",
	"unlock_enter",
	"contention_events",
	"owner_already_running",
	"owner_not_running",
	"owner_run_delay_samples",
	"owner_unlock_delay_samples",
	"waiter_wait_samples",
	"fanin_samples",
	"missing_lock_arg",
	"lock_failed",
	"unlock_without_owner",
	"unlock_owner_mismatch",
	"pending_without_run_on_unlock",
	"owner_map_seen",
	"owner_field_seen",
	"owner_field_zero",
	"owner_self",
	"contended_owner_tracked",
	"owner_offcpu_events",
	"owner_preempted_events",
	"owner_offcpu_delay_samples",
	"owner_runnable_offcpu_delay_samples",
};

static void sig_handler(int sig)
{
	(void)sig;
	exiting = 1;
}

static int libbpf_print_fn(enum libbpf_print_level level, const char *format, va_list args)
{
	if (level == LIBBPF_DEBUG)
		return 0;
	return vfprintf(stderr, format, args);
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s --pid PID --out-json FILE [options]\n"
		"\n"
		"Options:\n"
		"  --pid PID            Target process id\n"
		"  --out-json FILE      Output summary JSON\n"
		"  --duration SEC       Run for a fixed duration; 0 waits for signal (default: 0)\n"
		"  --library PATH       Shared library containing pthread_mutex symbols\n"
		"  --min-wait-us US     Min contended lock wait recorded in wait histogram (default: 1)\n"
		"  --pthread-owner-offset N  Offset of glibc pthread_mutex owner field; -1 disables (default: 8)\n"
		"  --verbose           Print libbpf debug output\n",
		prog);
}

static uint64_t bucket_upper_us(uint32_t bucket)
{
	if (bucket == 0)
		return 1;
	if (bucket >= 63)
		return UINT64_MAX;
	return 1ULL << bucket;
}

static uint64_t read_percpu_counter(int fd, uint32_t idx, int nr_cpus)
{
	uint64_t *vals = calloc((size_t)nr_cpus, sizeof(uint64_t));
	uint64_t sum = 0;

	if (!vals)
		return 0;
	if (bpf_map_lookup_elem(fd, &idx, vals) == 0) {
		for (int cpu = 0; cpu < nr_cpus; cpu++)
			sum += vals[cpu];
	}
	free(vals);
	return sum;
}

static int read_histogram(int fd, struct hist_summary *hist)
{
	int nr_cpus = libbpf_num_possible_cpus();
	uint64_t *vals = calloc((size_t)nr_cpus, sizeof(uint64_t));

	if (!vals)
		return -1;
	memset(hist, 0, sizeof(*hist));
	for (uint32_t idx = 0; idx < OWNER_HIST_BUCKETS; idx++) {
		uint64_t sum = 0;

		memset(vals, 0, (size_t)nr_cpus * sizeof(uint64_t));
		if (bpf_map_lookup_elem(fd, &idx, vals) != 0)
			continue;
		for (int cpu = 0; cpu < nr_cpus; cpu++)
			sum += vals[cpu];
		hist->buckets[idx] = sum;
		hist->samples += sum;
		if (sum)
			hist->max_bucket_upper_us = (double)bucket_upper_us(idx);
	}
	free(vals);
	return 0;
}

static double percentile_us(const struct hist_summary *hist, double pct)
{
	uint64_t target;
	uint64_t seen = 0;

	if (!hist->samples)
		return 0.0;
	target = (uint64_t)((double)hist->samples * pct + 0.999999);
	if (target == 0)
		target = 1;
	for (uint32_t idx = 0; idx < OWNER_HIST_BUCKETS; idx++) {
		seen += hist->buckets[idx];
		if (seen >= target)
			return (double)bucket_upper_us(idx);
	}
	return hist->max_bucket_upper_us;
}

static void fill_percentiles(struct hist_summary *hist)
{
	hist->p50_us = percentile_us(hist, 0.50);
	hist->p95_us = percentile_us(hist, 0.95);
	hist->p99_us = percentile_us(hist, 0.99);
}

static double ratio(uint64_t num, uint64_t den)
{
	return den ? (double)num / (double)den : 0.0;
}

static double coverage_ratio(uint64_t num, uint64_t den)
{
	if (!den)
		return 0.0;
	if (num > den)
		num = den;
	return (double)num / (double)den;
}

static void json_hist(FILE *f, const char *name, const struct hist_summary *hist, bool comma)
{
	fprintf(f, "    \"%s\": {\n", name);
	fprintf(f, "      \"samples\": %llu,\n", (unsigned long long)hist->samples);
	fprintf(f, "      \"p50_us\": %.3f,\n", hist->p50_us);
	fprintf(f, "      \"p95_us\": %.3f,\n", hist->p95_us);
	fprintf(f, "      \"p99_us\": %.3f,\n", hist->p99_us);
	fprintf(f, "      \"max_bucket_upper_us\": %.3f,\n", hist->max_bucket_upper_us);
	fprintf(f, "      \"buckets\": [");
	for (uint32_t i = 0; i < OWNER_HIST_BUCKETS; i++) {
		fprintf(f, "%s%llu", i ? ", " : "", (unsigned long long)hist->buckets[i]);
	}
	fprintf(f, "]\n");
	fprintf(f, "    }%s\n", comma ? "," : "");
}

static int write_json(const char *path, const struct options *opts, const char *lib_path,
		      struct owner_progress_bpf *skel)
{
	FILE *f = fopen(path, "w");
	int counter_fd = bpf_map__fd(skel->maps.counters);
	int nr_cpus = libbpf_num_possible_cpus();
	uint64_t counters[OWNER_CNT_NR] = {};
	struct hist_summary owner_run = {};
	struct hist_summary owner_unlock = {};
	struct hist_summary owner_offcpu = {};
	struct hist_summary owner_runnable_offcpu = {};
	struct hist_summary waiter_wait = {};
	struct hist_summary fanin = {};

	if (!f)
		return -1;
	for (uint32_t i = 0; i < OWNER_CNT_NR; i++)
		counters[i] = read_percpu_counter(counter_fd, i, nr_cpus);

	read_histogram(bpf_map__fd(skel->maps.owner_next_run_delay_hist), &owner_run);
	read_histogram(bpf_map__fd(skel->maps.owner_unlock_delay_hist), &owner_unlock);
	read_histogram(bpf_map__fd(skel->maps.owner_offcpu_delay_hist), &owner_offcpu);
	read_histogram(bpf_map__fd(skel->maps.owner_runnable_offcpu_delay_hist), &owner_runnable_offcpu);
	read_histogram(bpf_map__fd(skel->maps.waiter_lock_wait_hist), &waiter_wait);
	read_histogram(bpf_map__fd(skel->maps.waiter_fanin_hist), &fanin);
	fill_percentiles(&owner_run);
	fill_percentiles(&owner_unlock);
	fill_percentiles(&owner_offcpu);
	fill_percentiles(&owner_runnable_offcpu);
	fill_percentiles(&waiter_wait);
	fill_percentiles(&fanin);

	fprintf(f, "{\n");
	fprintf(f, "  \"target_pid\": %d,\n", opts->pid);
	fprintf(f, "  \"library\": \"%s\",\n", lib_path ? lib_path : "");
	fprintf(f, "  \"duration_s\": %d,\n", opts->duration_s);
	fprintf(f, "  \"min_wait_us\": %d,\n", opts->min_wait_us);
	fprintf(f, "  \"pthread_owner_offset\": %d,\n", opts->pthread_owner_offset);
	fprintf(f, "  \"counters\": {\n");
	for (uint32_t i = 0; i < OWNER_CNT_NR; i++) {
		fprintf(f, "    \"%s\": %llu%s\n", counter_names[i],
			(unsigned long long)counters[i],
			i + 1 == OWNER_CNT_NR ? "" : ",");
	}
	fprintf(f, "  },\n");
	fprintf(f, "  \"ratios\": {\n");
	fprintf(f, "    \"owner_already_running\": %.9f,\n",
		ratio(counters[OWNER_CNT_OWNER_ALREADY_RUNNING],
		      counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "    \"owner_not_running\": %.9f,\n",
		ratio(counters[OWNER_CNT_OWNER_NOT_RUNNING],
		      counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "    \"owner_run_delay_coverage\": %.9f,\n",
		coverage_ratio(counters[OWNER_CNT_OWNER_RUN_DELAY_SAMPLES],
			       counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "    \"owner_unlock_delay_coverage\": %.9f,\n",
		coverage_ratio(counters[OWNER_CNT_OWNER_UNLOCK_DELAY_SAMPLES],
			       counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "    \"owner_offcpu_events_per_contention\": %.9f,\n",
		ratio(counters[OWNER_CNT_OWNER_OFFCPU_EVENTS],
		      counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "    \"owner_preempted_events_per_contention\": %.9f,\n",
		ratio(counters[OWNER_CNT_OWNER_PREEMPTED_EVENTS],
		      counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "    \"owner_runnable_offcpu_fraction\": %.9f,\n",
		ratio(counters[OWNER_CNT_OWNER_PREEMPTED_EVENTS],
		      counters[OWNER_CNT_OWNER_OFFCPU_EVENTS]));
	fprintf(f, "    \"waiter_wait_coverage\": %.9f\n",
		coverage_ratio(counters[OWNER_CNT_WAITER_WAIT_SAMPLES],
			       counters[OWNER_CNT_CONTENTION_EVENTS]));
	fprintf(f, "  },\n");
	fprintf(f, "  \"histograms\": {\n");
	json_hist(f, "owner_next_run_delay_us", &owner_run, true);
	json_hist(f, "owner_contention_to_unlock_delay_us", &owner_unlock, true);
	json_hist(f, "owner_offcpu_delay_us", &owner_offcpu, true);
	json_hist(f, "owner_runnable_offcpu_delay_us", &owner_runnable_offcpu, true);
	json_hist(f, "waiter_lock_wait_us", &waiter_wait, true);
	json_hist(f, "waiter_fanin", &fanin, false);
	fprintf(f, "  }\n");
	fprintf(f, "}\n");
	fclose(f);
	return 0;
}

static struct bpf_link *attach_symbol(const struct bpf_program *prog, pid_t pid,
				      const char *library, const char *symbol, bool retprobe)
{
	struct bpf_uprobe_opts opts = {};
	struct bpf_link *link;

	opts.sz = sizeof(opts);
	opts.func_name = symbol;
	opts.retprobe = retprobe;
	errno = 0;
	link = bpf_program__attach_uprobe_opts(prog, pid, library, 0, &opts);
	if (!link && errno != ENOENT)
		fprintf(stderr, "attach by name failed: lib=%s symbol=%s retprobe=%d errno=%d (%s)\n",
			library, symbol, retprobe, errno, strerror(errno));
	return link;
}

static int resolve_symbol_offset(const char *path, const char *symbol, size_t *offset)
{
	Elf *elf = NULL;
	Elf_Scn *scn = NULL;
	GElf_Ehdr ehdr;
	int fd = -1;
	int ret = -1;

	if (elf_version(EV_CURRENT) == EV_NONE)
		return -1;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;

	elf = elf_begin(fd, ELF_C_READ, NULL);
	if (!elf)
		goto out;
	if (!gelf_getehdr(elf, &ehdr))
		goto out;

	while ((scn = elf_nextscn(elf, scn)) != NULL) {
		GElf_Shdr shdr;
		Elf_Data *data;
		int count;

		if (!gelf_getshdr(scn, &shdr))
			continue;
		if (shdr.sh_type != SHT_DYNSYM && shdr.sh_type != SHT_SYMTAB)
			continue;

		data = elf_getdata(scn, NULL);
		if (!data)
			continue;
		count = shdr.sh_size / shdr.sh_entsize;
		for (int i = 0; i < count; i++) {
			GElf_Sym sym;
			const char *name;

			if (!gelf_getsym(data, i, &sym))
				continue;
			if (GELF_ST_TYPE(sym.st_info) != STT_FUNC)
				continue;
			name = elf_strptr(elf, shdr.sh_link, sym.st_name);
			if (!name || strcmp(name, symbol) != 0)
				continue;
			*offset = (size_t)sym.st_value;
			ret = 0;
			goto out;
		}
	}

out:
	if (elf)
		elf_end(elf);
	if (fd >= 0)
		close(fd);
	return ret;
}

static struct bpf_link *attach_symbol_by_offset(const struct bpf_program *prog, pid_t pid,
						const char *library, const char *symbol,
						bool retprobe)
{
	size_t offset = 0;
	struct bpf_link *link;

	if (resolve_symbol_offset(library, symbol, &offset) != 0) {
		fprintf(stderr, "resolve symbol failed: lib=%s symbol=%s\n", library, symbol);
		return NULL;
	}
	errno = 0;
	link = bpf_program__attach_uprobe(prog, retprobe, pid, library, offset);
	if (!link && errno != ENOENT)
		fprintf(stderr, "attach by offset failed: lib=%s symbol=%s offset=0x%zx retprobe=%d errno=%d (%s)\n",
			library, symbol, offset, retprobe, errno, strerror(errno));
	return link;
}

static int attach_one_library(struct owner_progress_bpf *skel, pid_t pid, const char *library,
			      struct bpf_link **lock_enter, struct bpf_link **lock_ret,
			      struct bpf_link **unlock_enter)
{
	const char *lock_symbols[] = {"pthread_mutex_lock", "__pthread_mutex_lock", NULL};
	const char *unlock_symbols[] = {"pthread_mutex_unlock", "__pthread_mutex_unlock", NULL};

	for (int i = 0; lock_symbols[i]; i++) {
		*lock_enter = attach_symbol(skel->progs.handle_pthread_mutex_lock_enter, pid, library, lock_symbols[i], false);
		if (!*lock_enter)
			*lock_enter = attach_symbol_by_offset(skel->progs.handle_pthread_mutex_lock_enter, pid, library, lock_symbols[i], false);
		if (!*lock_enter)
			continue;
		*lock_ret = attach_symbol(skel->progs.handle_pthread_mutex_lock_return, pid, library, lock_symbols[i], true);
		if (!*lock_ret)
			*lock_ret = attach_symbol_by_offset(skel->progs.handle_pthread_mutex_lock_return, pid, library, lock_symbols[i], true);
		if (*lock_ret)
			break;
		bpf_link__destroy(*lock_enter);
		*lock_enter = NULL;
	}
	if (!*lock_enter || !*lock_ret)
		return -1;

	for (int i = 0; unlock_symbols[i]; i++) {
		*unlock_enter = attach_symbol(skel->progs.handle_pthread_mutex_unlock_enter, pid, library, unlock_symbols[i], false);
		if (!*unlock_enter)
			*unlock_enter = attach_symbol_by_offset(skel->progs.handle_pthread_mutex_unlock_enter, pid, library, unlock_symbols[i], false);
		if (*unlock_enter)
			return 0;
	}

	bpf_link__destroy(*lock_enter);
	bpf_link__destroy(*lock_ret);
	*lock_enter = NULL;
	*lock_ret = NULL;
	return -1;
}

static int attach_uprobes(struct owner_progress_bpf *skel, const struct options *opts,
			  const char **used_library, struct bpf_link **lock_enter,
			  struct bpf_link **lock_ret, struct bpf_link **unlock_enter)
{
	const char *candidates[] = {
		"/lib/x86_64-linux-gnu/libc.so.6",
		"/usr/lib/x86_64-linux-gnu/libc.so.6",
		"/lib/x86_64-linux-gnu/libpthread.so.0",
		"/usr/lib/x86_64-linux-gnu/libpthread.so.0",
		NULL,
	};

	if (opts->library && opts->library[0] && access(opts->library, R_OK) == 0) {
		if (attach_one_library(skel, opts->pid, opts->library, lock_enter, lock_ret, unlock_enter) == 0) {
			*used_library = opts->library;
			return 0;
		}
	}

	for (int i = 0; candidates[i]; i++) {
		if (access(candidates[i], R_OK) != 0)
			continue;
		if (attach_one_library(skel, opts->pid, candidates[i], lock_enter, lock_ret, unlock_enter) == 0) {
			*used_library = candidates[i];
			return 0;
		}
	}
	return -1;
}

static int parse_args(int argc, char **argv, struct options *opts)
{
	static const struct option long_opts[] = {
		{"pid", required_argument, NULL, 'p'},
		{"out-json", required_argument, NULL, 'o'},
		{"duration", required_argument, NULL, 'd'},
		{"library", required_argument, NULL, 'l'},
		{"min-wait-us", required_argument, NULL, 'w'},
		{"pthread-owner-offset", required_argument, NULL, 'O'},
		{"verbose", no_argument, NULL, 'v'},
		{"help", no_argument, NULL, 'h'},
		{},
	};
	int opt;

	opts->duration_s = 0;
	opts->min_wait_us = 1;
	opts->pthread_owner_offset = 8;

	while ((opt = getopt_long(argc, argv, "p:o:d:l:w:O:vh", long_opts, NULL)) != -1) {
		switch (opt) {
		case 'p':
			opts->pid = (pid_t)atoi(optarg);
			break;
		case 'o':
			opts->out_json = optarg;
			break;
		case 'd':
			opts->duration_s = atoi(optarg);
			break;
		case 'l':
			opts->library = optarg;
			break;
		case 'w':
			opts->min_wait_us = atoi(optarg);
			break;
		case 'O':
			opts->pthread_owner_offset = atoi(optarg);
			break;
		case 'v':
			opts->verbose = true;
			break;
		default:
			usage(argv[0]);
			return opt == 'h' ? 1 : -1;
		}
	}

	if (opts->pid <= 0 || !opts->out_json) {
		usage(argv[0]);
		return -1;
	}
	if (opts->min_wait_us < 0)
		opts->min_wait_us = 0;
	return 0;
}

int main(int argc, char **argv)
{
	struct options opts = {};
	struct owner_progress_bpf *skel = NULL;
	struct bpf_link *lock_enter = NULL, *lock_ret = NULL, *unlock_enter = NULL;
	struct bpf_link *sched_switch = NULL;
	const char *used_library = NULL;
	struct rlimit r = {RLIM_INFINITY, RLIM_INFINITY};
	time_t start;
	int err = 0;

	if (parse_args(argc, argv, &opts) != 0)
		return 1;

	if (!opts.verbose)
		libbpf_set_print(libbpf_print_fn);
	if (setrlimit(RLIMIT_MEMLOCK, &r) != 0) {
		perror("setrlimit");
		return 1;
	}

	skel = owner_progress_bpf__open();
	if (!skel) {
		fprintf(stderr, "failed to open BPF skeleton\n");
		err = 1;
		goto cleanup;
	}
	skel->rodata->target_tgid = opts.pid;
	skel->rodata->min_wait_ns = (uint64_t)opts.min_wait_us * 1000ULL;
	skel->rodata->pthread_owner_offset = opts.pthread_owner_offset;
	if (owner_progress_bpf__load(skel)) {
		fprintf(stderr, "failed to load BPF skeleton\n");
		err = 1;
		goto cleanup;
	}
	sched_switch = bpf_program__attach_tracepoint(skel->progs.handle_sched_switch, "sched", "sched_switch");
	if (!sched_switch) {
		fprintf(stderr, "failed to attach sched_switch tracepoint\n");
		err = 1;
		goto cleanup;
	}
	if (attach_uprobes(skel, &opts, &used_library, &lock_enter, &lock_ret, &unlock_enter) != 0) {
		fprintf(stderr, "failed to attach pthread mutex uprobes\n");
		err = 1;
		goto cleanup;
	}

	signal(SIGINT, sig_handler);
	signal(SIGTERM, sig_handler);

	fprintf(stderr, "owner-progress monitor attached: pid=%d library=%s duration=%d min_wait_us=%d\n",
		opts.pid, used_library, opts.duration_s, opts.min_wait_us);

	start = time(NULL);
	while (!exiting) {
		if (opts.duration_s > 0 && time(NULL) - start >= opts.duration_s)
			break;
		sleep(1);
	}

	if (write_json(opts.out_json, &opts, used_library, skel) != 0) {
		fprintf(stderr, "failed to write %s: %s\n", opts.out_json, strerror(errno));
		err = 1;
		goto cleanup;
	}
	fprintf(stderr, "owner-progress monitor wrote %s\n", opts.out_json);

cleanup:
	bpf_link__destroy(unlock_enter);
	bpf_link__destroy(lock_ret);
	bpf_link__destroy(lock_enter);
	bpf_link__destroy(sched_switch);
	owner_progress_bpf__destroy(skel);
	return err;
}
