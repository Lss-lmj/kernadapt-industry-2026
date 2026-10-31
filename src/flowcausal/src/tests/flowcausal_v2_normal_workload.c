// SPDX-License-Identifier: GPL-2.0
/* 普通单进程计算任务：按固定处理器时间采样相邻运行片段的墙钟间隔。 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static uint64_t clock_ns(clockid_t clock_id)
{
	struct timespec now;

	if (clock_gettime(clock_id, &now)) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static int compare_u64(const void *left, const void *right)
{
	uint64_t a = *(const uint64_t *)left;
	uint64_t b = *(const uint64_t *)right;

	return (a > b) - (a < b);
}

static uint64_t percentile(uint64_t *samples, size_t count, unsigned int percent)
{
	size_t index = (count * percent + 99) / 100;

	if (!index)
		return samples[0];
	if (index > count)
		index = count;
	return samples[index - 1];
}

int main(int argc, char **argv)
{
	const uint64_t chunk_ns = 250000;
	uint64_t *gaps;
	uint64_t duration_ns;
	uint64_t cpu_begin;
	uint64_t wall_begin;
	uint64_t previous_wall;
	uint64_t maximum = 0;
	size_t capacity;
	size_t count = 0;
	char start;
	volatile uint64_t value = 1;

	if (argc != 2)
		return 2;
	duration_ns = strtoull(argv[1], NULL, 10) * 1000000ULL;
	if (!duration_ns || duration_ns > 60000000000ULL)
		return 2;
	capacity = (size_t)(duration_ns / chunk_ns) + 32;
	gaps = calloc(capacity, sizeof(*gaps));
	if (!gaps)
		return 2;
	if (read(STDIN_FILENO, &start, 1) != 1)
		return 2;
	cpu_begin = clock_ns(CLOCK_PROCESS_CPUTIME_ID);
	wall_begin = previous_wall = clock_ns(CLOCK_MONOTONIC);
	while (clock_ns(CLOCK_PROCESS_CPUTIME_ID) - cpu_begin < duration_ns) {
		uint64_t chunk_begin = clock_ns(CLOCK_PROCESS_CPUTIME_ID);
		uint64_t wall;

		while (clock_ns(CLOCK_PROCESS_CPUTIME_ID) - chunk_begin < chunk_ns)
			value = value * 2862933555777941757ULL + 3037000493ULL;
		wall = clock_ns(CLOCK_MONOTONIC);
		if (count < capacity) {
			gaps[count] = wall - previous_wall;
			if (gaps[count] > maximum)
				maximum = gaps[count];
			count++;
		}
		previous_wall = wall;
	}
	if (!count)
		return 2;
	qsort(gaps, count, sizeof(*gaps), compare_u64);
	printf("elapsed_ns=%" PRIu64 " gap_p95_ns=%" PRIu64
	       " gap_p99_ns=%" PRIu64 " maximum_gap_ns=%" PRIu64
	       " samples=%zu\n",
	       clock_ns(CLOCK_MONOTONIC) - wall_begin,
	       percentile(gaps, count, 95), percentile(gaps, count, 99),
	       maximum, count);
	free(gaps);
	(void)value;
	return 0;
}
