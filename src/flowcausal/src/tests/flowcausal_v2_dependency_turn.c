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

struct result { uint64_t checksum, start, end, cpu; };

static uint64_t clock_ns(clockid_t clock)
{
    struct timespec ts;
    if (clock_gettime(clock, &ts)) exit(2);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static void receive(int fd)
{
    char c;
    ssize_t ret;
    do { ret = read(fd, &c, 1); } while (ret < 0 && errno == EINTR);
    if (ret != 1) exit(2);
}

static pid_t worker(int gate[2], struct result *result, uint64_t seed,
                    uint64_t iterations, int background_nice)
{
    pid_t pid = fork();
    if (pid < 0) exit(2);
    if (!pid) {
        close(gate[1]);
        if (background_nice && setpriority(PRIO_PROCESS, 0, background_nice)) _exit(3);
        receive(gate[0]);
        result->start = clock_ns(CLOCK_MONOTONIC);
        uint64_t cpu = clock_ns(CLOCK_PROCESS_CPUTIME_ID);
        for (uint64_t i = 0; i < iterations; ++i)
            seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
        result->checksum = seed;
        result->cpu = clock_ns(CLOCK_PROCESS_CPUTIME_ID) - cpu;
        result->end = clock_ns(CLOCK_MONOTONIC);
        _exit(0);
    }
    return pid;
}

static void join(pid_t pid)
{
    int status;
    pid_t ret;
    do { ret = waitpid(pid, &status, 0); } while (ret < 0 && errno == EINTR);
    if (ret != pid || !WIFEXITED(status) || WEXITSTATUS(status)) exit(4);
}

int main(int argc, char **argv)
{
    if (argc != 6) return 2;
    int count = atoi(argv[1]), target = atoi(argv[2]);
    uint64_t iterations = strtoull(argv[3], NULL, 10);
    int block_ms = atoi(argv[4]), background_nice = atoi(argv[5]);
    if (count < 2 || count > 64 || target < 0 || target >= count || block_ms < 0) return 2;
    struct result *results = mmap(NULL, (count+1)*sizeof(*results), PROT_READ|PROT_WRITE,
                                 MAP_SHARED|MAP_ANONYMOUS, -1, 0);
    if (results == MAP_FAILED) return 2;
    int gate[2];
    if (pipe(gate)) return 2;
    pid_t pids[65];
    receive(STDIN_FILENO);
    for (int i = 0; i < count; ++i)
        pids[i] = worker(gate, &results[i], i+1, iterations*(i == target ? 2 : 1), background_nice);
    puts("old-prepared"); fflush(stdout);
    receive(STDIN_FILENO);
    pids[count] = worker(gate, &results[count], count+1, iterations/5, 0);
    puts("current-prepared"); fflush(stdout);
    receive(STDIN_FILENO);
    uint64_t start = clock_ns(CLOCK_MONOTONIC);
    close(gate[0]);
    for (int i = 0; i <= count; ++i)
        if (write(gate[1], "x", 1) != 1) return 2;
    close(gate[1]);
    join(pids[count]);
    uint64_t foreground_done = clock_ns(CLOCK_MONOTONIC);
    /* The dependency changes here. No scheduler-specific notification reveals
     * which old child is needed. The kernel observes an ordinary blocking wait. */
    join(pids[target]);
    uint64_t dependency_done = clock_ns(CLOCK_MONOTONIC);
    struct timespec remaining = {block_ms/1000, (block_ms%1000)*1000000L};
    while (nanosleep(&remaining, &remaining))
        if (errno != EINTR) return 2;
    for (int i = 0; i < count; ++i)
        if (i != target) join(pids[i]);
    printf("{\"start_ns\":%"PRIu64",\"foreground_done_ns\":%"PRIu64
           ",\"dependency_done_ns\":%"PRIu64",\"end_ns\":%"PRIu64
           ",\"target\":%d,\"workers\":[", start, foreground_done, dependency_done,
           clock_ns(CLOCK_MONOTONIC), target);
    for (int i = 0; i <= count; ++i)
        printf("%s{\"pid\":%d,\"checksum\":%"PRIu64",\"start_ns\":%"PRIu64
               ",\"end_ns\":%"PRIu64",\"cpu_ns\":%"PRIu64"}", i ? "," : "",
               pids[i], results[i].checksum, results[i].start, results[i].end, results[i].cpu);
    puts("]}");
    return 0;
}
