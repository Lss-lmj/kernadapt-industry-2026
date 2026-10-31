// SPDX-License-Identifier: GPL-2.0
/* 创建固定数量的处理器密集型子进程，记录完成时间和最长未获运行时间。 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

struct shared_state {
	_Atomic uint64_t maximum_gap_ns;
};

static uint64_t monotonic_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now)) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static uint64_t process_cpu_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &now)) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static void update_maximum(_Atomic uint64_t *maximum, uint64_t value)
{
	uint64_t old = atomic_load_explicit(maximum, memory_order_relaxed);

	while (old < value &&
	       !atomic_compare_exchange_weak_explicit(maximum, &old, value,
						     memory_order_relaxed,
						     memory_order_relaxed))
		;
}

static void burn_cpu(struct shared_state *state, uint64_t duration_ns)
{
	uint64_t begin = process_cpu_ns();
	uint64_t last_wall = monotonic_ns();
	volatile uint64_t value = 1;

	while (process_cpu_ns() - begin < duration_ns) {
		uint64_t wall;

		for (unsigned int index = 0; index < 4096; index++)
			value = value * 2862933555777941757ULL + 3037000493ULL;
		wall = monotonic_ns();
		update_maximum(&state->maximum_gap_ns, wall - last_wall);
		last_wall = wall;
	}
	(void)value;
}

int main(int argc, char **argv)
{
	struct shared_state *state;
	unsigned int children;
	uint64_t cpu_ms;
	uint64_t begin;
	char start;
	int failed = 0;

	if (argc != 3)
		return 2;
	children = (unsigned int)strtoul(argv[1], NULL, 10);
	cpu_ms = strtoull(argv[2], NULL, 10);
	if (!children || children > 256 || !cpu_ms || cpu_ms > 60000)
		return 2;
	state = mmap(NULL, sizeof(*state), PROT_READ | PROT_WRITE,
		     MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (state == MAP_FAILED) {
		perror("mmap");
		return 2;
	}
	atomic_init(&state->maximum_gap_ns, 0);
	if (read(STDIN_FILENO, &start, 1) != 1)
		return 2;
	begin = monotonic_ns();
	for (unsigned int index = 0; index < children; index++) {
		pid_t child = fork();

		if (child < 0) {
			perror("fork");
			return 2;
		}
		if (!child) {
			burn_cpu(state, cpu_ms * 1000000ULL);
			_exit(0);
		}
	}
	for (unsigned int index = 0; index < children; index++) {
		int status;
		pid_t child;

		do {
			child = wait(&status);
		} while (child < 0 && errno == EINTR);
		if (child < 0 || !WIFEXITED(status) || WEXITSTATUS(status))
			failed = 1;
	}
	printf("elapsed_ns=%" PRIu64 " maximum_gap_ns=%" PRIu64 "\n",
	       monotonic_ns() - begin,
	       atomic_load_explicit(&state->maximum_gap_ns, memory_order_relaxed));
	munmap(state, sizeof(*state));
	return failed;
}
