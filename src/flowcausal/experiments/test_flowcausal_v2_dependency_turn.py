"""Fixed-work role changes, ordinary waitpid evidence, complete background joins."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import statistics
import subprocess
import time
from test_flowcausal_v2_lifecycle import stable_id, stop_process

STATE = Path('/sys/kernel/sched_ext/state')


def sudo(*args, **kwargs):
    return subprocess.run(['sudo','-n',*map(str,args)], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def wait_for(predicate):
    end = time.monotonic()+20
    while not predicate():
        if time.monotonic() > end: raise TimeoutError('scheduler state')
        time.sleep(.02)


def advance(worker, expected):
    worker.stdin.write('x'); worker.stdin.flush()
    if not select.select([worker.stdout], [], [], 10)[0]: raise TimeoutError(expected)
    assert worker.stdout.readline().strip() == expected


def checksum(seed, iterations):
    mask = (1<<64)-1
    a, b = 6364136223846793005, 1442695040888963407
    while iterations:
        if iterations & 1: seed = (seed*a+b)&mask
        a, b, iterations = a*a&mask, b*(a+1)&mask, iterations>>1
    return seed


def one(plan, job, output):
    assert STATE.read_text().strip() == 'disabled'
    output.mkdir(parents=True, exist_ok=False)
    profile = plan['profiles'][job['profile']]
    log = (output/'scheduler.log').open('w')
    scheduler = None
    groups, workers, sessions, rows, stats = [], [], [], [], []
    try:
        if profile.get('binary'):
            scheduler = subprocess.Popen(['sudo','-n',profile['binary'],'--managed-cpus',job['cpus'],
                '--quiet',*profile.get('flags',[])], stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            wait_for(lambda: STATE.read_text().strip() == 'enabled' or scheduler.poll() is not None)
            assert scheduler.poll() is None, output/'scheduler.log'
        for index in range(job.get('workflows',2)):
            group = Path('/sys/fs/cgroup')/f'fc-turn-{os.getpid()}-{index}'
            sudo('mkdir',group); groups.append(group)
            session = f'dependency-turn-{index}'; sessions.append(session)
            target = (job['trial']+index*3) % job['background_count']
            process = subprocess.Popen(['taskset','-c',job['cpus'],plan['workload'],
                str(job['background_count']),str(target),str(job['iterations']),str(job['block_ms']),
                str(profile.get('background_nice',0))], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            workers.append(process)
            sudo('tee',group/'cgroup.procs',input=f'{process.pid}\n')

            def event(*args):
                if scheduler:
                    sudo(plan['flowd'],'--session',session,'--cgroup',group,'--once',*args)

            event('--operation','session-open','--root-pid',process.pid)
            event('--operation','tool-begin','--tool-id','100','--generation','1','--sequence','1',
                  '--execution-mode','asynchronous','--stage','PACKAGE_BACKGROUND','--root-pid',process.pid)
            advance(process,'old-prepared')
            event('--operation','tool-end','--tool-id','100','--generation','1','--sequence','2',
                  '--execution-mode','asynchronous','--stage','PACKAGE_BACKGROUND')
            event('--operation','tool-begin','--tool-id','200','--generation','3','--sequence','3',
                  '--execution-mode','synchronous','--stage','TEST','--root-pid',process.pid)
            advance(process,'current-prepared')
        release = time.monotonic_ns()
        for process in workers: process.stdin.write('x'); process.stdin.flush()
        for index,process in enumerate(workers):
            stdout,stderr = process.communicate(timeout=90)
            (output/f'worker-{index}.stdout').write_text(stdout)
            (output/f'worker-{index}.stderr').write_text(stderr)
            assert process.returncode == 0, (process.returncode,stderr)
            row = json.loads(stdout); rows.append(row)
            for i,w in enumerate(row['workers']):
                n = job['iterations']//5 if i == job['background_count'] else job['iterations']*(2 if i==row['target'] else 1)
                assert w['checksum'] == checksum(i+1,n)
                assert release <= w['start_ns'] <= w['end_ns'] <= row['end_ns']
        if scheduler:
            assert scheduler.poll() is None and STATE.read_text().strip()=='enabled'
            for session in sessions:
                raw=sudo(plan['flowd'],'--operation','stats','--session-id',stable_id(session),'--once').stdout
                stats.append(json.loads([s for s in raw.splitlines() if s.startswith('{')][-1]))
        completions=[(row['end_ns']-release)/1e9 for row in rows]
        result=dict(job=job,release_ns=release,workflows=rows,work_match=True,
                    mean_completion_sec=statistics.mean(completions),span_sec=max(completions),
                    scheduler_stats=stats)
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    finally:
        for group in groups:
            if (group/'cgroup.procs').read_text().strip(): sudo('tee',group/'cgroup.kill',input='1\n')
        for process in workers: process.wait(timeout=5)
        if scheduler and scheduler.poll() is None:
            for group,session in zip(groups,sessions):
                sudo(plan['flowd'],'--operation','session-close','--session',session,'--cgroup',group,'--once')
            stop_process(scheduler,signal.SIGTERM)
        log.close()
        wait_for(lambda: STATE.read_text().strip()=='disabled')
        for group in groups: sudo('rmdir',group)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();plan=json.loads(args.plan.read_text())
    for name,expected in plan['input_sha256'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==expected,name
    args.output.mkdir(parents=True,exist_ok=False)
    governors={str(p):p.read_text().strip() for p in Path('/sys/devices/system/cpu/cpufreq').glob('policy*/scaling_governor')}
    def set_governors(values):
        sudo('python3','-c','import json,sys;from pathlib import Path;[Path(p).write_text(v+chr(10)) for p,v in json.load(sys.stdin).items()]',input=json.dumps(values))
    env=dict(start_ns=time.time_ns(),original_governors=governors)
    try:
        set_governors({p:'performance' for p in governors})
        for number,job in enumerate(plan['runs']):
            output=args.output/f'{number:02d}-{job["profile"]}'
            result=one(plan,job,output)
            with (args.output/'journal.jsonl').open('a') as stream: stream.write(json.dumps(result)+'\n')
            print(job,result['mean_completion_sec'],result['span_sec'],flush=True)
    finally:
        set_governors(governors)
        env.update(end_ns=time.time_ns(),restored=all(Path(p).read_text().strip()==v for p,v in governors.items()),sched_ext=STATE.read_text().strip())
        (args.output/'environment.json').write_text(json.dumps(env,indent=2)+'\n')


if __name__=='__main__': main()
