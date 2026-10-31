/* SPDX-License-Identifier: GPL-2.0 */
/*
 * 构造不调用 wait/waitpid 的三层工具进程：上层进程阻塞读取管道，
 * 最深层进程执行处理器计算并沿管道逐级通知。用于验证 FlowCausal
 * 能否仅根据睡眠、父子来源和处理器排队识别真正执行计算的后代。
 */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

static uint64_t monotonic_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static int write_byte(int fd)
{
	char value = 'x';

	while (write(fd, &value, 1) < 0) {
		if (errno != EINTR)
			return -1;
	}
	return 0;
}

static int read_byte(int fd)
{
	char value;

	while (read(fd, &value, 1) < 0) {
		if (errno != EINTR)
			return -1;
	}
	return 0;
}

static void compute_for_ms(unsigned long milliseconds)
{
	uint64_t deadline = monotonic_ns() + milliseconds * 1000000ULL;
	volatile uint64_t value = 0x9e3779b97f4a7c15ULL;

	while (monotonic_ns() < deadline) {
		for (unsigned int i = 0; i < 20000; i++)
			value = (value ^ (value >> 13)) * 0xff51afd7ed558ccdULL + i;
	}
	if (!value)
		fprintf(stderr, "unexpected zero\n");
}

int main(int argc, char **argv)
{
	unsigned long milliseconds = 1500;
	int upper_pipe[2];
	pid_t middle;

	if (argc == 2) {
		char *end = NULL;

		errno = 0;
		milliseconds = strtoul(argv[1], &end, 10);
		if (errno || !end || *end || !milliseconds) {
			fprintf(stderr, "invalid milliseconds: %s\n", argv[1]);
			return 2;
		}
	} else if (argc != 1) {
		fprintf(stderr, "usage: %s [milliseconds]\n", argv[0]);
		return 2;
	}
	/* 子进程由内核自动回收；程序本身不调用 wait/waitpid。 */
	if (signal(SIGCHLD, SIG_IGN) == SIG_ERR || pipe(upper_pipe)) {
		perror("prepare upper pipe");
		return 2;
	}
	middle = fork();
	if (middle < 0) {
		perror("fork middle");
		return 2;
	}
	if (!middle) {
		int lower_pipe[2];
		pid_t compute;

		close(upper_pipe[0]);
		if (pipe(lower_pipe)) {
			perror("prepare lower pipe");
			_exit(2);
		}
		compute = fork();
		if (compute < 0) {
			perror("fork compute");
			_exit(2);
		}
		if (!compute) {
			close(lower_pipe[0]);
			compute_for_ms(milliseconds);
			if (write_byte(lower_pipe[1]))
				_exit(3);
			close(lower_pipe[1]);
			_exit(0);
		}
		close(lower_pipe[1]);
		if (read_byte(lower_pipe[0]))
			_exit(3);
		close(lower_pipe[0]);
		if (write_byte(upper_pipe[1]))
			_exit(3);
		close(upper_pipe[1]);
		_exit(0);
	}
	close(upper_pipe[1]);
	if (read_byte(upper_pipe[0])) {
		perror("read upper pipe");
		return 3;
	}
	close(upper_pipe[0]);
	return 0;
}
