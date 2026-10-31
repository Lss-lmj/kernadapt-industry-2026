#!/usr/bin/env python3
"""Exercise actual local/remote futex wakes and CPU-burst exclusions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time

WORKER = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#define N 16
struct shared { _Atomic int ticket[N]; } *shared;
int ack[2];
long burst;
static uint64_t cpu_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static void *worker(void *arg) {
    int n = (int)(intptr_t)arg;
    for (;;) {
        while (!atomic_load(&shared->ticket[n]))
            syscall(SYS_futex, &shared->ticket[n], FUTEX_WAIT, 0, NULL, NULL, 0);
        atomic_store(&shared->ticket[n], 0);
        uint64_t start = cpu_ns();
        while (cpu_ns() - start < (uint64_t)burst) {}
        if (write(ack[1], ".", 1) != 1) _exit(2);
    }
}
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    int remote = atoi(argv[1]);
    burst = atol(argv[2]);
    shared = mmap(NULL, sizeof(*shared), PROT_READ|PROT_WRITE, MAP_SHARED|MAP_ANONYMOUS, -1, 0);
    if (shared == MAP_FAILED || pipe(ack)) return 2;
    puts("ready"); fflush(stdout);
    if (getchar() == EOF) return 2;
    pid_t child = remote ? fork() : -1;
    if (!remote || child == 0) {
        pthread_t threads[N];
        for (int n = 0; n < N; n++)
            if (pthread_create(&threads[n], NULL, worker, (void *)(intptr_t)n)) _exit(2);
        if (remote) for (;;) pause();
    }
    usleep(50000);
    /* Warm-up makes each receiver complete multiple sleeps before sampling. */
    for (int round = 0; round < 24; round++) {
        usleep(1000);
        for (int n = 0; n < N; n++) {
            atomic_store(&shared->ticket[n], 1);
            syscall(SYS_futex, &shared->ticket[n], FUTEX_WAKE, 1, NULL, NULL, 0);
        }
        for (int n = 0; n < N; n++) { char c; if (read(ack[0], &c, 1) != 1) return 2; }
    }
    if (remote) { kill(child, SIGKILL); waitpid(child, NULL, 0); }
    puts("done");
    return 0;
}
'''

def sudo(*cmd, **kw):
    return subprocess.run(['sudo', '-n', *map(str, cmd)], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)

def wait_for(pred, message):
    deadline = time.monotonic() + 15
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError(message)
        time.sleep(.02)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scheduler-arg', action='append', default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    build = args.build.resolve()
    code = args.output/'worker.c'; code.write_text(WORKER)
    binary = (args.output/'worker').resolve()
    subprocess.run(['cc', '-O2', '-Wall', '-Werror', '-pthread', str(code), '-o', str(binary)], check=True)
    state = Path('/sys/kernel/sched_ext/state')
    assert state.read_text().strip() == 'disabled'
    samples = []
    phases = [('local-short', 0, 100000, '0'), ('local-long', 0, 5000000, '0'),
              ('remote-short', 1, 100000, '0')]
    for label, remote, burst, cpus in phases:
        cgroup = Path('/sys/fs/cgroup')/f'flowcausal-futex-test-{os.getpid()}'
        logpath = args.output/f'{label}.log'
        sched = worker = None
        with logpath.open('w') as log:
            try:
                sched = subprocess.Popen(['sudo', '-n', str(build/'scx_flowcausal_v2'),
                    '--managed-cpus', '0-7', '--quiet', *args.scheduler_arg], stdout=log, stderr=subprocess.STDOUT)
                wait_for(lambda: state.read_text().strip() == 'enabled' or sched.poll() is not None, 'load scheduler')
                assert sched.poll() is None, logpath.read_text()[-2000:]
                worker = subprocess.Popen(['taskset', '-c', cpus, str(binary), str(remote), str(burst)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                assert select.select([worker.stdout], [], [], 5)[0]
                assert worker.stdout.readline().strip() == 'ready'
                sudo('mkdir', cgroup)
                sudo('tee', cgroup/'cgroup.procs', input=f'{worker.pid}\n')
                sudo(build/'flowd_v2', '--operation', 'session-open', '--session', f'futex-{label}',
                     '--cgroup', cgroup, '--root-pid', worker.pid, '--once')
                out, err = worker.communicate('\n', timeout=60)
                assert worker.returncode == 0 and out.strip() == 'done', (worker.returncode, out, err)
                assert sched.poll() is None and state.read_text().strip() == 'enabled', logpath.read_text()[-2500:]
            finally:
                if cgroup.exists():
                    sudo('tee', cgroup/'cgroup.kill', input='1\n')
                if worker and worker.poll() is None:
                    worker.wait(timeout=5)
                if sched and sched.poll() is None:
                    sudo('kill', '-INT', sched.pid)
                    sched.wait(timeout=10)
                if cgroup.exists(): sudo('rmdir', cgroup)
                wait_for(lambda: state.read_text().strip() == 'disabled', 'unload scheduler')
        matches = re.search(r'local_futex_marked=(\d+) local_futex_limited=(\d+)', logpath.read_text())
        assert matches, logpath
        samples.append(dict(phase=label, marked=int(matches[1]), limited=int(matches[2])))
    checks = {
        'short_local_wakes_detected': samples[0]['marked'] > 200,
        'short_local_credit_limited': samples[0]['limited'] > 100,
        'long_bursts_excluded': samples[1]['marked'] < 32 and samples[1]['limited'] == 0,
        'cross_process_wakes_excluded': samples[2]['marked'] == 0,
    }
    result = dict(checks=checks, samples=samples, scheduler_args=args.scheduler_arg, status='pass' if all(checks.values()) else 'fail',
                  scheduler=str(build/'scx_flowcausal_v2'),
                  scheduler_sha256=hashlib.sha256((build/'scx_flowcausal_v2').read_bytes()).hexdigest(),
                  source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  kernel=os.uname().release)
    (args.output/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    return int(not all(checks.values()))

if __name__ == '__main__':
    raise SystemExit(main())
