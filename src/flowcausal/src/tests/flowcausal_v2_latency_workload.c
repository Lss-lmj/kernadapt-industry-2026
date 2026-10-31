// SPDX-License-Identifier: GPL-2.0
/* 反复执行 fork/wait，记录子进程退出到父进程完成短处理的时间。 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static uint64_t now_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void burn_for(uint64_t duration_ns)
{
	uint64_t until = now_ns() + duration_ns;
	volatile uint64_t value = 1;

	while (now_ns() < until)
		value = value * 2862933555777941757ULL + 3037000493ULL;
	(void)value;
}

int main(int argc, char **argv)
{
	uint64_t *child_exit_ns;
	unsigned int rounds = 40;
	char start;

	if (argc > 1)
		rounds = (unsigned int)strtoul(argv[1], NULL, 10);
	if (!rounds || rounds > 1000)
		return 2;
	child_exit_ns = mmap(NULL, sizeof(*child_exit_ns),
			     PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS,
			     -1, 0);
	if (child_exit_ns == MAP_FAILED) {
		perror("mmap");
		return 2;
	}
	if (read(STDIN_FILENO, &start, 1) != 1)
		return 2;
	for (unsigned int round = 0; round < rounds; round++) {
		pid_t child;
		int status;
		struct timespec pause = {.tv_sec = 0, .tv_nsec = 2000000};

		*child_exit_ns = 0;
		child = fork();
		if (child < 0) {
			perror("fork");
			return 2;
		}
		if (!child) {
			nanosleep(&pause, NULL);
			burn_for(150000);
			*child_exit_ns = now_ns();
			_exit(0);
		}
		do {
			if (waitpid(child, &status, 0) == child)
				break;
		} while (errno == EINTR);
		if (!WIFEXITED(status) || WEXITSTATUS(status))
			return 3;
		/* 模拟读取并整理一小段工具输出。 */
		burn_for(200000);
		printf("%" PRIu64 "\n", now_ns() - *child_exit_ns);
		fflush(stdout);
	}
	munmap(child_exit_ns, sizeof(*child_exit_ns));
	return 0;
}
