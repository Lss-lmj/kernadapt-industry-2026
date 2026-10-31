// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

struct worker_arg {
	pthread_mutex_t *lock;
	volatile uint64_t *counter;
	int seconds;
	int hold_us;
};

static uint64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static void *worker(void *data)
{
	struct worker_arg *arg = data;
	uint64_t end_ns = now_ns() + (uint64_t)arg->seconds * 1000000000ULL;

	while (now_ns() < end_ns) {
		if (pthread_mutex_lock(arg->lock) != 0) {
			perror("pthread_mutex_lock");
			break;
		}
		(*arg->counter)++;
		if (arg->hold_us > 0)
			usleep(arg->hold_us);
		if (pthread_mutex_unlock(arg->lock) != 0) {
			perror("pthread_mutex_unlock");
			break;
		}
	}
	return NULL;
}

int main(int argc, char **argv)
{
	int threads = argc > 1 ? atoi(argv[1]) : 8;
	int seconds = argc > 2 ? atoi(argv[2]) : 5;
	int hold_us = argc > 3 ? atoi(argv[3]) : 50;
	int start_delay_s = argc > 4 ? atoi(argv[4]) : 0;
	pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
	pthread_t *tids;
	struct worker_arg arg;
	volatile uint64_t counter = 0;

	if (threads <= 0)
		threads = 8;
	tids = calloc((size_t)threads, sizeof(*tids));
	if (!tids)
		return 1;

	arg.lock = &lock;
	arg.counter = &counter;
	arg.seconds = seconds;
	arg.hold_us = hold_us;

	if (start_delay_s > 0)
		sleep((unsigned int)start_delay_s);

	for (int i = 0; i < threads; i++) {
		int ret = pthread_create(&tids[i], NULL, worker, &arg);
		if (ret) {
			errno = ret;
			perror("pthread_create");
			return 1;
		}
	}
	for (int i = 0; i < threads; i++)
		pthread_join(tids[i], NULL);

	printf("owner_progress_smoke threads=%d seconds=%d hold_us=%d start_delay_s=%d counter=%llu\n",
	       threads, seconds, hold_us, start_delay_s, (unsigned long long)counter);
	free(tids);
	return 0;
}
