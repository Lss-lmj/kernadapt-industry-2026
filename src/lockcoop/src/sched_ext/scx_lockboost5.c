/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Userspace loader for scx_lockboost5.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <libgen.h>
#include <errno.h>
#include <fcntl.h>
#include <gelf.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>
#include "scx_lockboost5.bpf.skel.h"

#define FUTEX_BOOST_OFF 0
#define FUTEX_BOOST_WEAK 1
#define FUTEX_BOOST_EQUAL 2
#define FUTEX_BOOST_OWNER 3
#define FUTEX_BOOST_OWNER_WEAK 4

#define STAT_MAX 43

enum stat_idx {
	STAT_LOCAL = 0,
	STAT_GLOBAL = 1,
	STAT_MUTEX_WAIT = 2,
	STAT_MUTEX_OWNER_HIT = 3,
	STAT_MUTEX_OWNER_MISS = 4,
	STAT_FUTEX_WAIT = 5,
	STAT_FUTEX_WAKE = 6,
	STAT_FUTEX_CANDIDATE = 7,
	STAT_FUTEX_CONFIRMED = 8,
	STAT_FUTEX_MISMATCH = 9,
	STAT_FUTEX_SKIP_NO_CONF = 10,
	STAT_MUTEX_PRESSURE = 11,
	STAT_FUTEX_PRESSURE = 12,
	STAT_STRONG_BOOST = 13,
	STAT_WEAK_BOOST = 14,
	STAT_EQUAL_BOOST = 15,
	STAT_WAKE_DEC_MUTEX = 16,
	STAT_WAKE_DEC_FUTEX = 17,
	STAT_OWNER_FUTEX_PRESSURE = 18,
	STAT_OWNER_FUTEX_BOOST = 19,
	STAT_WAKE_DEC_OWNER_FUTEX = 20,
	STAT_PTHREAD_LOCK_ENTER = 21,
	STAT_PTHREAD_LOCK_SUCCESS = 22,
	STAT_PTHREAD_UNLOCK = 23,
	STAT_OWNER_MAP_HIT = 24,
	STAT_OWNER_FIELD_HIT = 25,
	STAT_OWNER_FIELD_ZERO = 26,
	STAT_OWNER_INVALID_MISSING_TASK = 27,
	STAT_OWNER_INVALID_TGID = 28,
	STAT_OWNER_SELF_SKIP = 29,
	STAT_PI_OWNER_HIT = 30,
	STAT_PI_OWNER_MISS = 31,
	STAT_FUTEX_UNKNOWN_IGNORED = 32,
	STAT_OWNER_FUTEX_BELOW_THRESHOLD = 33,
	STAT_OWNER_FUTEX_SLICE_BOOST = 34,
	STAT_OWNER_FUTEX_INF_BOOST = 35,
	STAT_OWNER_TIME_ELIGIBLE = 36,
	STAT_OWNER_TIME_SLICE_BOOST = 37,
	STAT_OWNER_TIME_INF_BOOST = 38,
	STAT_OWNER_TIME_HARD_INF = 39,
	STAT_OWNER_TIME_BELOW_THRESHOLD = 40,
	STAT_OWNER_TIME_PRESSURE_SUM = 41,
	STAT_OWNER_TIME_AGE_MS_SUM = 42,
};

struct boost_info {
	u32 mutex_waiters;
	u32 futex_waiters;
	u32 owner_futex_waiters;
	u32 futex_confidence;
	u64 last_mutex_ts;
	u64 last_futex_ts;
	u64 last_owner_futex_ts;
	u64 first_owner_futex_ts;
	bool strong_boosted;
	bool weak_boosted;
	bool owner_boosted;
	bool owner_time_boosted;
	bool owner_time_inf_boosted;
};

static const char help_fmt[] =
"A lock-holder progress scheduler with separated mutex/futex evidence.\n"
"\n"
"Usage: %s [-f] [-v] [-t threshold] [-d decay_ms]\n"
"          [-F off|weak|equal|owner|ownerweak]\n"
"          [-w futex_waiters] [-a owner_futex_waiters] [-A owner_futex_inf_waiters]\n"
"          [-T tail_ms] [-B age_cap_ms] [-s slice_score] [-I inf_score] [-H hard_ms]\n"
"          [-c futex_confidence] [-x futex_state_ms]\n"
"          [-O pthread_owner_offset] [-L libc_path] [-N]\n"
"\n"
"  -f                  Use FIFO scheduling instead of weighted vtime scheduling\n"
"  -t <count>          Strong boost threshold (default: 4 waiters)\n"
"  -d <ms>             Mutex/owner decay timeout (default: 200ms)\n"
"  -F <policy>         Futex boost policy (default: ownerweak)\n"
"  -w <count>          Weak futex waiter threshold (default: 4)\n"
"  -a <count>          Owner-resolved futex slice threshold (default: 4)\n"
"  -A <count>          Owner-resolved futex INF threshold; 0 disables INF (default: 8)\n"
"  -T <ms>             Owner-futex tail wait threshold; 0 disables time boost (default: 5)\n"
"  -B <ms>             Owner-futex age cap for pressure score (default: 50)\n"
"  -s <score>          Owner-futex time slice pressure threshold (default: 16)\n"
"  -I <score>          Owner-futex time INF pressure threshold; 0 disables (default: 64)\n"
"  -H <ms>             Owner-futex hard wait INF threshold; 0 disables (default: 0)\n"
"  -c <count>          Futex confirmation threshold (default: 2)\n"
"  -x <ms>             Futex state timeout (default: 100ms)\n"
"  -O <offset>         pthread_mutex owner offset; -1 disables direct read (default: 8)\n"
"  -L <path>           libc/libpthread path for pthread mutex uprobes\n"
"  -N                  Disable pthread mutex uprobes and direct owner read\n"
"  -v                  Print libbpf debug messages\n"
"  -h                  Display this help and exit\n";

static bool verbose;
static volatile int exit_req;
static const char *pthread_library;
static bool no_pthread_owner;

static int libbpf_print_fn(enum libbpf_print_level level, const char *format,
			   va_list args)
{
	if (level == LIBBPF_DEBUG && !verbose)
		return 0;
	return vfprintf(stderr, format, args);
}

static void sigint_handler(int sig)
{
	exit_req = 1;
}

static const char *futex_policy_name(u32 policy)
{
	switch (policy) {
	case FUTEX_BOOST_OFF:
		return "off";
	case FUTEX_BOOST_WEAK:
		return "weak";
	case FUTEX_BOOST_EQUAL:
		return "equal";
	case FUTEX_BOOST_OWNER:
		return "owner";
	case FUTEX_BOOST_OWNER_WEAK:
		return "ownerweak";
	default:
		return "unknown";
	}
}

static int parse_futex_policy(const char *arg, u32 *policy)
{
	if (!strcmp(arg, "off") || !strcmp(arg, "0")) {
		*policy = FUTEX_BOOST_OFF;
		return 0;
	}
	if (!strcmp(arg, "weak") || !strcmp(arg, "1")) {
		*policy = FUTEX_BOOST_WEAK;
		return 0;
	}
	if (!strcmp(arg, "equal") || !strcmp(arg, "2")) {
		*policy = FUTEX_BOOST_EQUAL;
		return 0;
	}
	if (!strcmp(arg, "owner") || !strcmp(arg, "3")) {
		*policy = FUTEX_BOOST_OWNER;
		return 0;
	}
	if (!strcmp(arg, "ownerweak") || !strcmp(arg, "owner_weak") ||
	    !strcmp(arg, "4")) {
		*policy = FUTEX_BOOST_OWNER_WEAK;
		return 0;
	}
	return -EINVAL;
}

static void read_stats(struct scx_lockboost5 *skel, __u64 *stats)
{
	int nr_cpus = libbpf_num_possible_cpus();
	__u64 cnts[STAT_MAX][nr_cpus];
	__u32 idx;

	memset(stats, 0, sizeof(stats[0]) * STAT_MAX);
	for (idx = 0; idx < STAT_MAX; idx++) {
		int ret, cpu;

		ret = bpf_map_lookup_elem(bpf_map__fd(skel->maps.stats), &idx,
					  cnts[idx]);
		if (ret < 0)
			continue;
		for (cpu = 0; cpu < nr_cpus; cpu++)
			stats[idx] += cnts[idx][cpu];
	}
}

static void print_contended_tasks(struct scx_lockboost5 *skel)
{
	int map_fd = bpf_map__fd(skel->maps.contended_tasks);
	__u32 key = 0, prev_key = 0;
	struct boost_info info;
	int printed = 0;

	printf("--- LockBoost5 owners ---\n");
	while (bpf_map_get_next_key(map_fd, &prev_key, &key) == 0) {
		if (bpf_map_lookup_elem(map_fd, &key, &info) == 0) {
				printf("  tid=%-7u mutex_waiters=%-3u owner_futex_waiters=%-3u "
				       "weak_futex_waiters=%-3u futex_conf=%-3u strong=%u owner=%u "
				       "time=%u time_inf=%u weak=%u\n",
				       key, info.mutex_waiters,
				       info.owner_futex_waiters, info.futex_waiters,
				       info.futex_confidence, info.strong_boosted,
				       info.owner_boosted, info.owner_time_boosted,
				       info.owner_time_inf_boosted, info.weak_boosted);
			printed++;
			if (printed >= 16)
				break;
		}
		prev_key = key;
	}
	if (!printed)
		printf("  none\n");
}

static void print_stats(__u64 *s)
{
	printf("stats local=%llu global=%llu mutex_wait=%llu mutex_hit=%llu mutex_miss=%llu "
	       "futex_wait=%llu futex_wake=%llu futex_candidate=%llu futex_confirmed=%llu "
	       "futex_mismatch=%llu futex_skip_no_conf=%llu mutex_pressure=%llu "
	       "weak_futex_pressure=%llu strong_boost=%llu weak_boost=%llu equal_boost=%llu "
	       "wake_dec_mutex=%llu wake_dec_weak_futex=%llu owner_futex_pressure=%llu "
	       "owner_futex_boost=%llu wake_dec_owner_futex=%llu pthread_enter=%llu "
	       "pthread_success=%llu pthread_unlock=%llu owner_map_hit=%llu "
	       "owner_field_hit=%llu owner_field_zero=%llu owner_invalid_missing_task=%llu "
	       "owner_invalid_tgid=%llu owner_self_skip=%llu pi_owner_hit=%llu "
	       "pi_owner_miss=%llu futex_unknown_ignored=%llu "
	       "owner_futex_below_threshold=%llu owner_futex_slice_boost=%llu "
	       "owner_futex_inf_boost=%llu owner_time_eligible=%llu "
	       "owner_time_slice_boost=%llu owner_time_inf_boost=%llu "
	       "owner_time_hard_inf=%llu owner_time_below_threshold=%llu "
	       "owner_time_pressure_sum=%llu owner_time_age_ms_sum=%llu\n",
	       s[STAT_LOCAL], s[STAT_GLOBAL], s[STAT_MUTEX_WAIT],
	       s[STAT_MUTEX_OWNER_HIT], s[STAT_MUTEX_OWNER_MISS],
	       s[STAT_FUTEX_WAIT], s[STAT_FUTEX_WAKE],
	       s[STAT_FUTEX_CANDIDATE], s[STAT_FUTEX_CONFIRMED],
	       s[STAT_FUTEX_MISMATCH], s[STAT_FUTEX_SKIP_NO_CONF],
	       s[STAT_MUTEX_PRESSURE], s[STAT_FUTEX_PRESSURE],
	       s[STAT_STRONG_BOOST], s[STAT_WEAK_BOOST],
	       s[STAT_EQUAL_BOOST], s[STAT_WAKE_DEC_MUTEX],
	       s[STAT_WAKE_DEC_FUTEX], s[STAT_OWNER_FUTEX_PRESSURE],
	       s[STAT_OWNER_FUTEX_BOOST], s[STAT_WAKE_DEC_OWNER_FUTEX],
	       s[STAT_PTHREAD_LOCK_ENTER], s[STAT_PTHREAD_LOCK_SUCCESS],
	       s[STAT_PTHREAD_UNLOCK], s[STAT_OWNER_MAP_HIT],
	       s[STAT_OWNER_FIELD_HIT], s[STAT_OWNER_FIELD_ZERO],
	       s[STAT_OWNER_INVALID_MISSING_TASK], s[STAT_OWNER_INVALID_TGID],
	       s[STAT_OWNER_SELF_SKIP], s[STAT_PI_OWNER_HIT],
	       s[STAT_PI_OWNER_MISS], s[STAT_FUTEX_UNKNOWN_IGNORED],
	       s[STAT_OWNER_FUTEX_BELOW_THRESHOLD],
	       s[STAT_OWNER_FUTEX_SLICE_BOOST],
	       s[STAT_OWNER_FUTEX_INF_BOOST],
	       s[STAT_OWNER_TIME_ELIGIBLE],
	       s[STAT_OWNER_TIME_SLICE_BOOST],
	       s[STAT_OWNER_TIME_INF_BOOST],
	       s[STAT_OWNER_TIME_HARD_INF],
	       s[STAT_OWNER_TIME_BELOW_THRESHOLD],
	       s[STAT_OWNER_TIME_PRESSURE_SUM],
	       s[STAT_OWNER_TIME_AGE_MS_SUM]);
}

static int resolve_symbol_offset(const char *path, const char *symbol,
				 size_t *offset)
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

static struct bpf_link *attach_uprobe_symbol(const struct bpf_program *prog,
					     const char *library,
					     const char *symbol,
					     bool retprobe)
{
	struct bpf_uprobe_opts opts = {};
	struct bpf_link *link;
	size_t offset = 0;

	opts.sz = sizeof(opts);
	opts.func_name = symbol;
	opts.retprobe = retprobe;
	errno = 0;
	link = bpf_program__attach_uprobe_opts(prog, -1, library, 0, &opts);
	if (link)
		return link;

	if (resolve_symbol_offset(library, symbol, &offset) != 0)
		return NULL;

	errno = 0;
	return bpf_program__attach_uprobe(prog, retprobe, -1, library, offset);
}

static int attach_pthread_uprobes(struct scx_lockboost5 *skel,
				  struct bpf_link **lock_enter,
				  struct bpf_link **lock_ret,
				  struct bpf_link **unlock_enter,
				  const char **used_library)
{
	const char *candidates[] = {
		pthread_library,
		"/lib/x86_64-linux-gnu/libc.so.6",
		"/usr/lib/x86_64-linux-gnu/libc.so.6",
		"/lib/x86_64-linux-gnu/libpthread.so.0",
		"/usr/lib/x86_64-linux-gnu/libpthread.so.0",
		NULL,
	};
	const char *lock_symbols[] = {"pthread_mutex_lock", "__pthread_mutex_lock", NULL};
	const char *unlock_symbols[] = {"pthread_mutex_unlock", "__pthread_mutex_unlock", NULL};

	if (no_pthread_owner)
		return 0;

	for (int c = 0; candidates[c]; c++) {
		const char *library = candidates[c];

		if (!library || !library[0] || access(library, R_OK) != 0)
			continue;

		for (int i = 0; lock_symbols[i]; i++) {
			*lock_enter = attach_uprobe_symbol(
				skel->progs.handle_pthread_mutex_lock_enter,
				library, lock_symbols[i], false);
			if (!*lock_enter)
				continue;
			*lock_ret = attach_uprobe_symbol(
				skel->progs.handle_pthread_mutex_lock_return,
				library, lock_symbols[i], true);
			if (*lock_ret)
				break;
			bpf_link__destroy(*lock_enter);
			*lock_enter = NULL;
		}
		if (!*lock_enter || !*lock_ret)
			continue;

		for (int i = 0; unlock_symbols[i]; i++) {
			*unlock_enter = attach_uprobe_symbol(
				skel->progs.handle_pthread_mutex_unlock_enter,
				library, unlock_symbols[i], false);
			if (*unlock_enter) {
				*used_library = library;
				return 0;
			}
		}

		bpf_link__destroy(*lock_enter);
		bpf_link__destroy(*lock_ret);
		*lock_enter = NULL;
		*lock_ret = NULL;
	}

	return -ENOENT;
}

int main(int argc, char **argv)
{
	struct scx_lockboost5 *skel;
	struct bpf_link *link;
	struct bpf_link *pthread_lock_enter = NULL;
	struct bpf_link *pthread_lock_ret = NULL;
	struct bpf_link *pthread_unlock_enter = NULL;
	const char *used_pthread_library = NULL;
	int opt;
	__u64 ecode;

	libbpf_set_print(libbpf_print_fn);
	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);

restart:
	pthread_lock_enter = NULL;
	pthread_lock_ret = NULL;
	pthread_unlock_enter = NULL;
	used_pthread_library = NULL;
	skel = SCX_OPS_OPEN(lockboost5_ops, scx_lockboost5);
	if (!skel)
		SCX_BUG("Failed to open BPF skeleton");

	while ((opt = getopt(argc, argv, "fvt:d:F:w:a:A:T:B:s:I:H:c:x:O:L:Nh")) != -1) {
		switch (opt) {
		case 'f':
			skel->rodata->fifo_sched = true;
			break;
		case 'v':
			verbose = true;
			break;
		case 't':
			skel->rodata->high_lockboost_threshold = atoi(optarg);
			break;
		case 'd':
			skel->rodata->decay_timeout_ns = (__u64)atoi(optarg) * 1000 * 1000;
			break;
		case 'F': {
			u32 policy;

			if (parse_futex_policy(optarg, &policy)) {
				fprintf(stderr, "invalid futex policy: %s\n", optarg);
				fprintf(stderr, help_fmt, basename(argv[0]));
				return 1;
			}
			skel->rodata->futex_boost_policy = policy;
			break;
		}
		case 'w':
			skel->rodata->futex_waiter_threshold = atoi(optarg);
			break;
		case 'a':
			skel->rodata->owner_futex_waiter_threshold = atoi(optarg);
			break;
		case 'A':
			skel->rodata->owner_futex_inf_threshold = atoi(optarg);
			break;
		case 'T':
			skel->rodata->owner_time_threshold_ms = atoi(optarg);
			break;
		case 'B':
			skel->rodata->owner_time_age_cap_ms = atoi(optarg);
			break;
		case 's':
			skel->rodata->owner_time_slice_pressure = atoi(optarg);
			break;
		case 'I':
			skel->rodata->owner_time_inf_pressure = atoi(optarg);
			break;
		case 'H':
			skel->rodata->owner_time_hard_ms = atoi(optarg);
			break;
		case 'c':
			skel->rodata->futex_confirm_threshold = atoi(optarg);
			break;
		case 'x':
			skel->rodata->futex_state_timeout_ns = (__u64)atoi(optarg) * 1000 * 1000;
			break;
		case 'O':
			skel->rodata->pthread_owner_offset = atoi(optarg);
			break;
		case 'L':
			pthread_library = optarg;
			break;
		case 'N':
			no_pthread_owner = true;
			skel->rodata->enable_pthread_owner = false;
			break;
		default:
			fprintf(stderr, help_fmt, basename(argv[0]));
			return opt != 'h';
		}
	}

	SCX_OPS_LOAD(skel, lockboost5_ops, scx_lockboost5, uei);
	if (attach_pthread_uprobes(skel, &pthread_lock_enter, &pthread_lock_ret,
				   &pthread_unlock_enter,
				   &used_pthread_library) != 0) {
		fprintf(stderr,
			"warning: failed to attach pthread mutex uprobes; "
			"direct owner-field reads remain %s\n",
			skel->rodata->enable_pthread_owner ? "enabled" : "disabled");
	}
	link = SCX_OPS_ATTACH(skel, lockboost5_ops, scx_lockboost5);
	if (!link)
		SCX_BUG("Failed to attach BPF program");

	printf("scx_lockboost5 attached. threshold=%u decay_ms=%llu futex_policy=%s "
	       "futex_waiters=%u owner_futex_waiters=%u owner_futex_inf_waiters=%u "
	       "tail_ms=%u age_cap_ms=%u time_slice_score=%u time_inf_score=%u "
	       "hard_ms=%u futex_conf=%u futex_state_ms=%llu "
	       "pthread_owner_offset=%d pthread_uprobes=%s%s%s\n",
	       skel->rodata->high_lockboost_threshold,
	       (unsigned long long)(skel->rodata->decay_timeout_ns / 1000 / 1000),
	       futex_policy_name(skel->rodata->futex_boost_policy),
	       skel->rodata->futex_waiter_threshold,
	       skel->rodata->owner_futex_waiter_threshold,
	       skel->rodata->owner_futex_inf_threshold,
	       skel->rodata->owner_time_threshold_ms,
	       skel->rodata->owner_time_age_cap_ms,
	       skel->rodata->owner_time_slice_pressure,
	       skel->rodata->owner_time_inf_pressure,
	       skel->rodata->owner_time_hard_ms,
	       skel->rodata->futex_confirm_threshold,
	       (unsigned long long)(skel->rodata->futex_state_timeout_ns / 1000 / 1000),
	       skel->rodata->pthread_owner_offset,
	       used_pthread_library ? "on:" : "off",
	       used_pthread_library ? used_pthread_library : "",
	       no_pthread_owner ? " owner-read-disabled" : "");
	printf("Kernel mutex and owner-resolved futex evidence are strong; "
	       "unknown futex evidence is optional weak fallback.\n");

	while (!exit_req && !UEI_EXITED(skel, uei)) {
		__u64 stats[STAT_MAX];

		read_stats(skel, stats);
		print_stats(stats);
		print_contended_tasks(skel);
		fflush(stdout);
		sleep(1);
	}

	bpf_link__destroy(link);
	bpf_link__destroy(pthread_unlock_enter);
	bpf_link__destroy(pthread_lock_ret);
	bpf_link__destroy(pthread_lock_enter);
	ecode = UEI_REPORT(skel, uei);
	scx_lockboost5__destroy(skel);

	if (UEI_ECODE_RESTART(ecode))
		goto restart;
	return 0;
}
