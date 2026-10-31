#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static uint64_t now(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) exit(2);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

int main(int argc, char **argv)
{
    if (argc != 4) return 2;
    int children = atoi(argv[1]), block_ms = atoi(argv[3]);
    uint64_t iterations = strtoull(argv[2], NULL, 10);
    if (children < 1 || children > 64 || block_ms < 0) return 2;
    uint64_t *values = mmap(NULL, children * sizeof(*values),
        PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (values == MAP_FAILED) return 2;
    char byte;
    if (read(STDIN_FILENO, &byte, 1) != 1) return 2;
    int gate[2];
    if (pipe(gate)) return 2;
    for (int i = 0; i < children; ++i) {
        pid_t pid = fork();
        if (pid < 0) return 2;
        if (!pid) {
            close(gate[1]);
            if (read(gate[0], &byte, 1) != 1) _exit(2);
            uint64_t value = (uint64_t)i + 1;
            for (uint64_t step = 0; step < iterations; ++step)
                value = value * 6364136223846793005ULL + 1442695040888963407ULL;
            values[i] = value;
            _exit(0);
        }
    }
    close(gate[0]);
    puts("prepared"); fflush(stdout);
    if (read(STDIN_FILENO, &byte, 1) != 1) return 2;
    uint64_t start = now();
    for (int i = 0; i < children; ++i)
        if (write(gate[1], "x", 1) != 1) return 2;
    close(gate[1]);
    for (int i = 0; i < children; ++i) {
        int status;
        pid_t pid;
        do { pid = wait(&status); } while (pid < 0 && errno == EINTR);
        if (pid < 0 || !WIFEXITED(status) || WEXITSTATUS(status)) return 3;
    }
    uint64_t cpu_done = now(), checksum = 0;
    for (int i = 0; i < children; ++i) checksum += values[i];
    struct timespec remaining = {block_ms / 1000, (block_ms % 1000) * 1000000L};
    while (nanosleep(&remaining, &remaining))
        if (errno != EINTR) return 2;
    struct rusage usage;
    if (getrusage(RUSAGE_CHILDREN, &usage)) return 2;
    uint64_t cpu_us = (uint64_t)(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) * 1000000ULL + usage.ru_utime.tv_usec + usage.ru_stime.tv_usec;
    printf("{\"start_ns\":%"PRIu64",\"cpu_done_ns\":%"PRIu64
           ",\"end_ns\":%"PRIu64",\"checksum\":%"PRIu64
           ",\"children\":%d,\"iterations\":%"PRIu64",\"cpu_us\":%"PRIu64"}\n",
           start, cpu_done, now(), checksum, children, iterations, cpu_us);
    return 0;
}
