#!/usr/bin/env python3
"""Check measured CPU bursts across sleeps, preemption, and phase changes.

This exercises the running scheduler and reads its task storage through a
pidfd. It does not infer correctness from source text or elapsed wall time.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time


WORKER = r'''
import os, sys, time
print("ready", flush=True)
for line in sys.stdin:
    target, do_yield = map(int, line.split())
    start = time.thread_time_ns()
    next_yield = start + 1_000_000
    yields = 0
    while (now := time.thread_time_ns()) - start < target:
        if do_yield and now >= next_yield:
            os.sched_yield()
            yields += 1
            next_yield = now + 1_000_000
    print(f"{time.thread_time_ns()-start} {yields}", flush=True)
'''

COMPETITOR = r'''
import sys
print("ready", flush=True)
sys.stdin.readline()
print("running", flush=True)
while True:
    pass
'''

READ_STORAGE = r'''
import ctypes, json, os, sys
lib = ctypes.CDLL("libbpf.so.1", use_errno=True)
lib.bpf_map_get_fd_by_id.argtypes = [ctypes.c_uint]
lib.bpf_map_lookup_elem.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
fd = lib.bpf_map_get_fd_by_id(int(sys.argv[1]))
if fd < 0: raise OSError(ctypes.get_errno(), "open BPF task storage")
pidfd = os.pidfd_open(int(sys.argv[2]))
key = ctypes.c_int(pidfd)
value = ctypes.create_string_buffer(int(sys.argv[3]))
if lib.bpf_map_lookup_elem(fd, ctypes.byref(key), value) != 0:
    raise OSError(ctypes.get_errno(), "read BPF task storage")
offsets = json.loads(sys.argv[4])
print(json.dumps({name: int.from_bytes(value.raw[offset:offset+size], "little")
                  for name, (offset, size) in offsets.items()}))
os.close(pidfd)
os.close(fd)
'''


def privileged(*command, check=True):
    return subprocess.run(['sudo', '-n', *map(str, command)], check=check,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(predicate, message, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError(message)


def line_from(worker):
    if not select.select([worker.stdout], [], [], 5)[0]:
        raise TimeoutError('Worker did not respond')
    return worker.stdout.readline().strip()


def involuntary_switches(pid):
    status = Path(f'/proc/{pid}/status').read_text()
    return int(re.search(r'^nonvoluntary_ctxt_switches:\s*(\d+)', status, re.MULTILINE).group(1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scheduler', type=Path, required=True)
    parser.add_argument('--flowd', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scheduler-arg', action='append', default=[],
                        help='Additional scheduler option (use --scheduler-arg=--option)')
    args = parser.parse_args()
    state = Path('/sys/kernel/sched_ext/state')
    if state.read_text().strip() != 'disabled':
        raise RuntimeError('Another scheduler is active')
    args.output.mkdir(parents=True, exist_ok=False)
    cgroup = Path('/sys/fs/cgroup') / f'flowcausal-credit-test-{os.getpid()}'
    scheduler = worker = competitor = None
    samples = []
    with (args.output/'scheduler.log').open('w') as log:
        try:
            scheduler = subprocess.Popen(['sudo', '-n', str(args.scheduler.resolve()),
                '--managed-cpus', '0-7', '--adaptive-wakeup-credit',
                '--quiet', *args.scheduler_arg], stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: state.read_text().strip() == 'enabled', 'Scheduler did not load')
            worker = subprocess.Popen(['taskset', '-c', '0', sys.executable, '-u', '-c', WORKER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert line_from(worker) == 'ready'
            privileged('mkdir', str(cgroup))
            subprocess.run(['sudo', '-n', 'tee', str(cgroup/'cgroup.procs')],
                input=f'{worker.pid}\n', text=True, stdout=subprocess.DEVNULL, check=True)
            privileged(args.flowd.resolve(), '--operation', 'session-open', '--session',
                f'wakeup-credit-{os.getpid()}', '--cgroup', cgroup, '--root-pid', worker.pid, '--once')
            maps = json.loads(privileged('bpftool', '-j', 'map', 'show').stdout)
            task_map, = [m for m in maps if m.get('name') == 'quota_task_stat'
                         and m.get('type') == 'task_storage']
            btf = privileged('bpftool', 'btf', 'dump', 'id', task_map['btf_id'], 'format', 'raw').stdout
            struct = re.search(r"^\[\d+\] STRUCT 'flow_v2_quota_task_state'.*\n((?:\t.*\n)+)",
                               btf, re.MULTILINE).group(1)
            offsets = {}
            for name, size in [('previous_sleep_exec_runtime', 8), ('last_burst_ns', 8), ('completed_burst', 1)]:
                offset = int(re.search(rf"'{name}'.*bits_offset=(\d+)", struct).group(1))
                assert offset % 8 == 0
                offsets[name] = (offset//8, size)
            for name in ['last_sleep_ns', 'sleep_started_ns']:
                field = re.search(rf"'{name}'.*bits_offset=(\d+)", struct)
                if field:
                    offsets[name] = (int(field.group(1))//8, 8)
            for label, duration, do_yield in [('warmup', 100_000, 0), ('short', 100_000, 0),
                                             ('long_preempted', 30_000_000, 1), ('short_again', 100_000, 0)]:
                if do_yield:
                    # A second runnable process on the same CPU ensures that
                    # sched_yield really switches tasks instead of returning
                    # straight to the only runnable worker.
                    competitor = subprocess.Popen(['taskset', '-c', '0', sys.executable, '-u', '-c', COMPETITOR],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    assert line_from(competitor) == 'ready'
                    subprocess.run(['sudo', '-n', 'tee', str(cgroup/'cgroup.procs')],
                        input=f'{competitor.pid}\n', text=True, stdout=subprocess.DEVNULL, check=True)
                    competitor.stdin.write('run\n')
                    competitor.stdin.flush()
                    assert line_from(competitor) == 'running'
                switches_before = involuntary_switches(worker.pid)
                wall_start = time.monotonic_ns()
                worker.stdin.write(f'{duration} {do_yield}\n')
                worker.stdin.flush()
                cpu, yields = map(int, line_from(worker).split())
                wall_elapsed = time.monotonic_ns() - wall_start
                wait_for(lambda: Path(f'/proc/{worker.pid}/stat').read_text().rsplit(') ', 1)[1][0] == 'S',
                         'Worker did not block after reporting', timeout=3)
                value = json.loads(privileged(sys.executable, '-c', READ_STORAGE,
                    task_map['id'], worker.pid, task_map['bytes_value'], json.dumps(offsets)).stdout)
                switches = involuntary_switches(worker.pid) - switches_before
                samples.append(dict(phase=label,worker_cpu_ns=cpu,yields=yields,
                                    involuntary_switches=switches,wall_elapsed_ns=wall_elapsed,**value))
                if competitor is not None:
                    competitor.terminate()
                    competitor.wait(timeout=5)
                    competitor = None
            checks = {
                'short_burst_measured': 0 < samples[1]['last_burst_ns'] < 2_000_000,
                'preemptions_do_not_split_burst': 20_000_000 <= samples[2]['last_burst_ns'] < 100_000_000
                    and samples[2]['yields'] >= 2 and samples[2]['involuntary_switches'] >= 2,
                'long_to_short_phase_adapts': 0 < samples[3]['last_burst_ns'] < 2_000_000,
                'sleep_samples_marked': all(s['completed_burst'] for s in samples),
            }
            result = dict(status='pass' if all(checks.values()) else 'fail', checks=checks, samples=samples,
                          task_map=task_map, offsets=offsets, kernel=os.uname().release,
                          scheduler=str(args.scheduler.resolve()), flowd=str(args.flowd.resolve()),
                          scheduler_args=args.scheduler_arg,
                          source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          worker_program_sha256=hashlib.sha256(WORKER.encode()).hexdigest(),
                          scheduler_sha256=hashlib.sha256(args.scheduler.read_bytes()).hexdigest(),
                          flowd_sha256=hashlib.sha256(args.flowd.read_bytes()).hexdigest())
            (args.output/'result.json').write_text(json.dumps(result, indent=2)+'\n')
            print(json.dumps(result, indent=2))
            return 0 if all(checks.values()) else 1
        finally:
            if competitor is not None and competitor.poll() is None:
                competitor.terminate()
                competitor.wait(timeout=5)
            if worker is not None:
                worker.terminate()
                worker.wait(timeout=5)
            if scheduler is not None and scheduler.poll() is None:
                privileged('kill', '-INT', scheduler.pid, check=False)
                scheduler.wait(timeout=10)
            if cgroup.exists():
                privileged('rmdir', cgroup, check=False)
            wait_for(lambda: state.read_text().strip() == 'disabled', 'Scheduler did not stop')


if __name__ == '__main__':
    raise SystemExit(main())
