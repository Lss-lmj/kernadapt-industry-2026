// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <getopt.h>
#include <gelf.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <fcntl.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>

#include "common.h"
#include "handoff_locality.skel.h"

struct options {
	pid_t pid;
	const char *library;
	const char *out_json;
	int duration_s;
	int max_gap_ms;
	bool verbose;
	bool skip_same_tid;
};

struct topology {
	int ncpus;
	int *package_id;
	int *llc_id;
};

struct aggregate {
	uint64_t handoff_events;
	uint64_t same_cpu;
	uint64_t same_llc;
	uint64_t same_socket_other_llc;
	uint64_t cross_socket;
	uint64_t unknown_topology;
	uint64_t observed_pairs;
};

static volatile sig_atomic_t exiting;

static const char *counter_names[HANDOFF_CNT_NR] = {
	"lock_enter",
	"lock_success",
	"unlock_enter",
	"handoff_events",
	"missing_lock_arg",
	"lock_failed",
	"no_last_release",
	"same_tid_skipped",
	"stale_skipped",
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
		"  --pid PID            Target process id, e.g. mariadbd pid\n"
		"  --out-json FILE      Output summary JSON\n"
		"  --duration SEC       Run for a fixed duration; 0 waits for signal (default: 0)\n"
		"  --library PATH       Shared library containing pthread_mutex symbols\n"
		"  --max-gap-ms MS      Max unlock-to-lock gap counted as handoff (default: 100)\n"
		"  --include-same-tid   Count same-thread reacquire transitions\n"
		"  --verbose           Print libbpf debug output\n",
		prog);
}

static int read_int_file(const char *path, int fallback)
{
	FILE *f = fopen(path, "r");
	int val;

	if (!f)
		return fallback;
	if (fscanf(f, "%d", &val) != 1)
		val = fallback;
	fclose(f);
	return val;
}

static int read_cpu_package(int cpu)
{
	char path[256];

	snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/topology/physical_package_id", cpu);
	return read_int_file(path, -1);
}

static int read_cpu_llc(int cpu)
{
	int best_level = -1;
	int best_id = -1;

	for (int idx = 0; idx < 16; idx++) {
		char path[256];
		char type[64] = {};
		FILE *f;
		int level;
		int id;

		snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cache/index%d/type", cpu, idx);
		f = fopen(path, "r");
		if (!f)
			continue;
		if (!fgets(type, sizeof(type), f)) {
			fclose(f);
			continue;
		}
		fclose(f);
		if (strncmp(type, "Unified", 7) != 0)
			continue;

		snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cache/index%d/level", cpu, idx);
		level = read_int_file(path, -1);
		snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu%d/cache/index%d/id", cpu, idx);
		id = read_int_file(path, -1);
		if (level > best_level && id >= 0) {
			best_level = level;
			best_id = id;
		}
	}
	return best_id;
}

static int load_topology(struct topology *topo)
{
	topo->ncpus = (int)sysconf(_SC_NPROCESSORS_CONF);
	if (topo->ncpus <= 0)
		return -1;

	topo->package_id = calloc(topo->ncpus, sizeof(int));
	topo->llc_id = calloc(topo->ncpus, sizeof(int));
	if (!topo->package_id || !topo->llc_id)
		return -1;

	for (int cpu = 0; cpu < topo->ncpus; cpu++) {
		topo->package_id[cpu] = read_cpu_package(cpu);
		topo->llc_id[cpu] = read_cpu_llc(cpu);
	}
	return 0;
}

static void free_topology(struct topology *topo)
{
	free(topo->package_id);
	free(topo->llc_id);
	memset(topo, 0, sizeof(*topo));
}

static const char *classify_pair(const struct topology *topo, uint32_t prev_cpu, uint32_t curr_cpu)
{
	int prev_pkg, curr_pkg, prev_llc, curr_llc;

	if ((int)prev_cpu >= topo->ncpus || (int)curr_cpu >= topo->ncpus)
		return "unknown_topology";
	if (prev_cpu == curr_cpu)
		return "same_cpu";

	prev_pkg = topo->package_id[prev_cpu];
	curr_pkg = topo->package_id[curr_cpu];
	prev_llc = topo->llc_id[prev_cpu];
	curr_llc = topo->llc_id[curr_cpu];

	if (prev_pkg >= 0 && curr_pkg >= 0 && prev_pkg != curr_pkg)
		return "cross_socket";
	if (prev_pkg >= 0 && curr_pkg >= 0 && prev_pkg == curr_pkg &&
	    prev_llc >= 0 && curr_llc >= 0 && prev_llc == curr_llc)
		return "same_llc";
	if (prev_pkg >= 0 && curr_pkg >= 0 && prev_pkg == curr_pkg)
		return "same_socket_other_llc";
	return "unknown_topology";
}

static uint64_t read_percpu_counter(int fd, uint32_t idx, int nr_cpus)
{
	uint64_t *vals = calloc(nr_cpus, sizeof(uint64_t));
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

static int read_pair_counts(struct handoff_locality_bpf *skel, const struct topology *topo,
			    struct aggregate *agg)
{
	int fd = bpf_map__fd(skel->maps.cpu_pair_counts);
	int nr_cpus = libbpf_num_possible_cpus();
	uint64_t *vals = calloc(nr_cpus, sizeof(uint64_t));
	struct cpu_pair_key key = {};
	struct cpu_pair_key next_key = {};
	bool have_key = false;

	if (!vals)
		return -1;

	while (bpf_map_get_next_key(fd, have_key ? &key : NULL, &next_key) == 0) {
		uint64_t count = 0;
		const char *cls;

		memset(vals, 0, nr_cpus * sizeof(uint64_t));
		if (bpf_map_lookup_elem(fd, &next_key, vals) != 0) {
			key = next_key;
			have_key = true;
			continue;
		}
		for (int cpu = 0; cpu < nr_cpus; cpu++)
			count += vals[cpu];
		if (!count) {
			key = next_key;
			have_key = true;
			continue;
		}

		agg->handoff_events += count;
		agg->observed_pairs++;
		cls = classify_pair(topo, next_key.prev_cpu, next_key.curr_cpu);
		if (strcmp(cls, "same_cpu") == 0)
			agg->same_cpu += count;
		else if (strcmp(cls, "same_llc") == 0)
			agg->same_llc += count;
		else if (strcmp(cls, "same_socket_other_llc") == 0)
			agg->same_socket_other_llc += count;
		else if (strcmp(cls, "cross_socket") == 0)
			agg->cross_socket += count;
		else
			agg->unknown_topology += count;

		key = next_key;
		have_key = true;
	}

	free(vals);
	return 0;
}

static double ratio(uint64_t num, uint64_t den)
{
	return den ? (double)num / (double)den : 0.0;
}

static int write_json(const char *path, const struct options *opts, const char *lib_path,
		      struct handoff_locality_bpf *skel, const struct topology *topo,
		      const struct aggregate *agg)
{
	FILE *f = fopen(path, "w");
	int counter_fd = bpf_map__fd(skel->maps.counters);
	int nr_cpus = libbpf_num_possible_cpus();

	if (!f)
		return -1;

	fprintf(f, "{\n");
	fprintf(f, "  \"target_pid\": %d,\n", opts->pid);
	fprintf(f, "  \"library\": \"%s\",\n", lib_path ? lib_path : "");
	fprintf(f, "  \"duration_s\": %d,\n", opts->duration_s);
	fprintf(f, "  \"max_gap_ms\": %d,\n", opts->max_gap_ms);
	fprintf(f, "  \"skip_same_tid\": %s,\n", opts->skip_same_tid ? "true" : "false");
	fprintf(f, "  \"topology\": {\n");
	fprintf(f, "    \"cpu_count\": %d\n", topo->ncpus);
	fprintf(f, "  },\n");
	fprintf(f, "  \"counters\": {\n");
	for (uint32_t i = 0; i < HANDOFF_CNT_NR; i++) {
		fprintf(f, "    \"%s\": %llu%s\n", counter_names[i],
			(unsigned long long)read_percpu_counter(counter_fd, i, nr_cpus),
			i + 1 == HANDOFF_CNT_NR ? "" : ",");
	}
	fprintf(f, "  },\n");
	fprintf(f, "  \"handoff_counts\": {\n");
	fprintf(f, "    \"handoff_events\": %llu,\n", (unsigned long long)agg->handoff_events);
	fprintf(f, "    \"same_cpu\": %llu,\n", (unsigned long long)agg->same_cpu);
	fprintf(f, "    \"same_llc\": %llu,\n", (unsigned long long)agg->same_llc);
	fprintf(f, "    \"same_socket_other_llc\": %llu,\n", (unsigned long long)agg->same_socket_other_llc);
	fprintf(f, "    \"cross_socket\": %llu,\n", (unsigned long long)agg->cross_socket);
	fprintf(f, "    \"unknown_topology\": %llu,\n", (unsigned long long)agg->unknown_topology);
	fprintf(f, "    \"observed_cpu_pairs\": %llu\n", (unsigned long long)agg->observed_pairs);
	fprintf(f, "  },\n");
	fprintf(f, "  \"handoff_ratios\": {\n");
	fprintf(f, "    \"same_cpu\": %.9f,\n", ratio(agg->same_cpu, agg->handoff_events));
	fprintf(f, "    \"same_llc\": %.9f,\n", ratio(agg->same_llc, agg->handoff_events));
	fprintf(f, "    \"same_socket_other_llc\": %.9f,\n", ratio(agg->same_socket_other_llc, agg->handoff_events));
	fprintf(f, "    \"cross_socket\": %.9f,\n", ratio(agg->cross_socket, agg->handoff_events));
	fprintf(f, "    \"unknown_topology\": %.9f\n", ratio(agg->unknown_topology, agg->handoff_events));
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

static int attach_one_library(struct handoff_locality_bpf *skel, pid_t pid, const char *library,
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

static int attach_uprobes(struct handoff_locality_bpf *skel, const struct options *opts,
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
		if (!candidates[i] || access(candidates[i], R_OK) != 0)
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
		{"max-gap-ms", required_argument, NULL, 'g'},
		{"include-same-tid", no_argument, NULL, 's'},
		{"verbose", no_argument, NULL, 'v'},
		{"help", no_argument, NULL, 'h'},
		{},
	};
	int opt;

	opts->duration_s = 0;
	opts->max_gap_ms = 100;
	opts->skip_same_tid = true;

	while ((opt = getopt_long(argc, argv, "p:o:d:l:g:svh", long_opts, NULL)) != -1) {
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
		case 'g':
			opts->max_gap_ms = atoi(optarg);
			break;
		case 's':
			opts->skip_same_tid = false;
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
	if (opts->max_gap_ms < 0)
		opts->max_gap_ms = 0;
	return 0;
}

int main(int argc, char **argv)
{
	struct options opts = {};
	struct handoff_locality_bpf *skel = NULL;
	struct bpf_link *lock_enter = NULL, *lock_ret = NULL, *unlock_enter = NULL;
	struct topology topo = {};
	struct aggregate agg = {};
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
	if (load_topology(&topo) != 0) {
		fprintf(stderr, "failed to load CPU topology\n");
		return 1;
	}

	skel = handoff_locality_bpf__open();
	if (!skel) {
		fprintf(stderr, "failed to open BPF skeleton\n");
		err = 1;
		goto cleanup;
	}
	skel->rodata->target_tgid = opts.pid;
	skel->rodata->max_handoff_gap_ns = (uint64_t)opts.max_gap_ms * 1000ULL * 1000ULL;
	skel->rodata->skip_same_tid = opts.skip_same_tid;
	if (handoff_locality_bpf__load(skel)) {
		fprintf(stderr, "failed to load BPF skeleton\n");
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

	fprintf(stderr, "handoff monitor attached: pid=%d library=%s duration=%d max_gap_ms=%d\n",
		opts.pid, used_library, opts.duration_s, opts.max_gap_ms);

	start = time(NULL);
	while (!exiting) {
		if (opts.duration_s > 0 && time(NULL) - start >= opts.duration_s)
			break;
		sleep(1);
	}

	if (read_pair_counts(skel, &topo, &agg) != 0) {
		fprintf(stderr, "failed to read cpu_pair_counts\n");
		err = 1;
		goto cleanup;
	}
	if (write_json(opts.out_json, &opts, used_library, skel, &topo, &agg) != 0) {
		fprintf(stderr, "failed to write %s: %s\n", opts.out_json, strerror(errno));
		err = 1;
		goto cleanup;
	}
	fprintf(stderr, "handoff monitor wrote %s: events=%llu cross_socket=%.4f\n",
		opts.out_json,
		(unsigned long long)agg.handoff_events,
		ratio(agg.cross_socket, agg.handoff_events));

cleanup:
	bpf_link__destroy(unlock_enter);
	bpf_link__destroy(lock_ret);
	bpf_link__destroy(lock_enter);
	handoff_locality_bpf__destroy(skel);
	free_topology(&topo);
	return err;
}
