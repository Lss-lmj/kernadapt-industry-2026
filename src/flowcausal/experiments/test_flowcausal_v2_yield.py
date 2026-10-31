#!/usr/bin/env python3
"""Validate rescheduling after ENQ_LAST, peer progress, and CPU quota."""
import argparse,json,os,re,select,subprocess,time
from pathlib import Path
WORKER=r'''
#define _GNU_SOURCE
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
static uint64_t ns(clockid_t id) { struct timespec t; clock_gettime(id,&t); return (uint64_t)t.tv_sec*1000000000+t.tv_nsec; }
int main(void) {
    puts("ready"); fflush(stdout); if (getchar()==EOF) return 2;
    uint64_t calls=0,start=ns(CLOCK_MONOTONIC),cpu=ns(CLOCK_THREAD_CPUTIME_ID);
    while (ns(CLOCK_MONOTONIC)-start<1000000000) { sched_yield(); calls++; }
    uint64_t solo_wall=ns(CLOCK_MONOTONIC)-start,solo_cpu=ns(CLOCK_THREAD_CPUTIME_ID)-cpu;
    fprintf(stderr,"solo finished: %llu calls\n",(unsigned long long)calls);
    _Atomic int *done=mmap(NULL,4096,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);
    int gate[2],ack[2]; if(done==MAP_FAILED || pipe(gate) || pipe(ack)) return 2;
    pid_t child=fork();
    if(child<0) return 2;
    if(!child) {
        char c='.'; if(write(ack[1],&c,1)!=1 || read(gate[0],&c,1)!=1) _exit(2);
        uint64_t child_start=ns(CLOCK_THREAD_CPUTIME_ID);
        while(ns(CLOCK_THREAD_CPUTIME_ID)-child_start<50000000) {}
        atomic_store(done,1); _exit(0);
    }
    char c; if(read(ack[0],&c,1)!=1) return 2;
    fprintf(stderr,"peer blocked\n");
    /* The peer first sleeps; then it becomes runnable during yielding. */
    for(int i=0;i<10000;i++) sched_yield();
    fprintf(stderr,"warmup finished\n");
    start=ns(CLOCK_MONOTONIC); if(write(gate[1],".",1)!=1) return 2;
    while(!atomic_load(done)) {
        sched_yield();
        if(ns(CLOCK_MONOTONIC)-start>3000000000ULL) return 3;
    }
    uint64_t peer_wall=ns(CLOCK_MONOTONIC)-start;
    int status; if(waitpid(child,&status,0)!=child || status) return 2;
    printf("{\"yield_calls\":%llu,\"solo_wall_ns\":%llu,\"solo_cpu_ns\":%llu,\"peer_finish_ns\":%llu}\n",(unsigned long long)calls,(unsigned long long)solo_wall,(unsigned long long)solo_cpu,(unsigned long long)peer_wall);
    return 0;
}
'''
def sudo(*cmd,**kw):return subprocess.run(['sudo','-n',*map(str,cmd)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,**kw)
def wait_for(fn):
    end=time.monotonic()+15
    while not fn():
        if time.monotonic()>end:raise TimeoutError()
        time.sleep(.02)
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--scheduler-arg',action='append',default=[]);args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    build=args.build.resolve();code=args.output/'worker.c';code.write_text(WORKER);binary=(args.output/'worker').resolve();subprocess.run(['cc','-O2','-Wall','-Werror',str(code),'-o',str(binary)],check=True)
    state=Path('/sys/kernel/sched_ext/state');assert state.read_text().strip()=='disabled';samples=[]
    for label,quota in [('unlimited',False),('quota',True)]:
        cg=Path('/sys/fs/cgroup')/f'flowcausal-yield-{os.getpid()}';sched=worker=None;logpath=args.output/f'{label}.log'
        with logpath.open('w') as log:
            try:
                command=['sudo','-n',str(build/'scx_flowcausal_v2'),'--managed-cpus','0-7','--quiet','--enforce-cpu-max',*args.scheduler_arg]
                sched=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                wait_for(lambda:state.read_text().strip()=='enabled' or sched.poll() is not None);assert sched.poll() is None,logpath.read_text()[-2500:]
                worker=subprocess.Popen(['taskset','-c','0',str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                assert select.select([worker.stdout],[],[],5)[0] and worker.stdout.readline().strip()=='ready'
                sudo('mkdir',cg)
                if quota:sudo('tee',cg/'cpu.max',input='50000 100000\n')
                sudo('tee',cg/'cgroup.procs',input=f'{worker.pid}\n')
                sudo(build/'flowd_v2','--operation','session-open','--session',f'yield-{label}','--root-pid',worker.pid,'--cgroup',cg,'--once')
                try:
                    out,err=worker.communicate('\n',timeout=10)
                except subprocess.TimeoutExpired as error:
                    print('TIMEOUT PARTIAL OUTPUT',repr(error.stdout),repr(error.stderr),flush=True)
                    raise
                assert worker.returncode==0,(worker.returncode,out,err)
                assert sched.poll() is None and state.read_text().strip()=='enabled', 'Scheduler exited during worker execution: '+logpath.read_text()[-2500:]
                row=json.loads(out);row.update(phase=label,quota=quota);samples.append(row)
            finally:
                if cg.exists():sudo('tee',cg/'cgroup.kill',input='1\n')
                if worker and worker.poll() is None:worker.wait(timeout=5)
                if sched and sched.poll() is None:sudo('kill','-INT',sched.pid);sched.wait(timeout=10)
                if cg.exists():sudo('rmdir',cg)
                wait_for(lambda:state.read_text().strip()=='disabled')
    checks=dict(last_task_rescheduled=all(x['yield_calls']>10000 and x['solo_wall_ns']<1500000000 for x in samples),
                queued_peer_runs=all(x['peer_finish_ns']<1000000000 for x in samples),
                quota_preserved=0<samples[1]['solo_cpu_ns']/samples[1]['solo_wall_ns']<.60)
    result=dict(status='pass' if all(checks.values()) else 'fail',checks=checks,samples=samples)
    (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2));return int(not all(checks.values()))
if __name__=='__main__':raise SystemExit(main())
