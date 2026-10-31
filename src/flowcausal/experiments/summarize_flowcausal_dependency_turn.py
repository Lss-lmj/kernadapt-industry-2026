#!/usr/bin/env python3
"""Recompute dependency-turn experiments with explicit native scheduler identity checks."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics


def stats(values):
    return dict(mean=statistics.mean(values), sd=statistics.stdev(values) if len(values)>1 else None,
                n=len(values), values=values)


def check_inputs(plan):
    for name,expected in plan['input_sha256'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==expected,name


def mechanism(root, name, folder):
    plan=json.loads((root/(name+'-plan.json')).read_text());check_inputs(plan)
    rows=[json.loads(l) for l in (root/folder/'journal.jsonl').read_text().splitlines()]
    assert len(rows)==len(plan['runs'])
    groups=defaultdict(list)
    for number,(row,job) in enumerate(zip(rows,plan['runs'])):
        assert row['job']==job and row['work_match']
        if job['profile']=='linux':assert not plan['profiles']['linux'].get('binary')
        log=(root/folder/f'{number:02d}-{job["profile"]}'/'scheduler.log').read_text()
        found=re.search(r'background_dependency_rebases=(\d+) rebased_vtime=(\d+)',log)
        row['rebase_count']=int(found[1]) if found else 0
        groups[(job['scene'],job['cpus'],job['background_count'],job['profile'])].append(row)
    result=[]
    for key,selected in groups.items():
        item=dict(scene=key[0],cpus=key[1],background_count=key[2],profile=key[3])
        for metric in ['mean_completion_sec','span_sec']:
            item[metric]=stats([row[metric] for row in selected])
            base=statistics.mean(row[metric] for row in groups[key[:3]+('linux',)])
            item[metric]['reduction_vs_native_pct']=100*(1-item[metric]['mean']/base)
        for field in ['foreground_done_ns','dependency_done_ns']:
            item[field.replace('_ns','_sec')]=stats([
                statistics.mean((w[field]-row['release_ns'])/1e9 for w in row['workflows'])
                for row in selected])
        item['cross_generation_wait_links']=[sum(s['cross_generation_wait_links'] for s in row['scheduler_stats']) for row in selected]
        item['rebase_count']=[row['rebase_count'] for row in selected]
        result.append(item)
    env=json.loads((root/folder/'environment.json').read_text())
    assert env['restored'] and env['sched_ext']=='disabled'
    return result


def full_workloads(root):
    plan=json.loads((root/'hrb-corrected-plan.json').read_text());check_inputs(plan)
    rows=[json.loads(l) for l in (root/'hrb-corrected-plan.journal.jsonl').read_text().splitlines()]
    assert len(rows)==len(plan['runs'])==9
    groups=defaultdict(list); cases=defaultdict(lambda:defaultdict(list));counts=[]
    for row in rows:
        assert row['returncode']==0 and row['status']=='pass' and row['work_match'] and row['scheduler_identity_ok']
        out=Path(row['output']);data=json.loads((out/'result.json').read_text());identity=data['tested_system']
        assert identity['scheduler_valid']
        if row['profile']=='linux':
            assert not identity['command']
            assert identity['sched_ext_during']['state']==identity['sched_ext_at_workload_end']['state']=='disabled'
        else:
            assert identity['sched_ext_during']['state']==identity['sched_ext_at_workload_end']['state']=='enabled'
            assert identity['scheduler_alive_at_workload_end']
        capture=data['kernel_capture'];assert capture['complete'] and capture['dropped_events']==0
        assert not capture['missing_session_ids'] and data['aggregate']['unexpected_failure_commands']==0
        groups[row['profile']].append(data['aggregate'])
        for case in data['cases']:cases[case['instance_id']][row['profile']].append(case['workflow_completion_sec'])
        if row['profile']=='complete':
            log=(out/'scheduler.log').read_text()
            counts.append(dict(background_cpu_ns=int(re.search(r'aggregate_background_actual_cpu_ns=(\d+)',log)[1]),
                               dependency_rebases=int(re.search(r'background_dependency_rebases=(\d+)',log)[1])))
    result={}
    for profile,selected in groups.items():
        result[profile]={}
        for metric in ['mean_workflow_completion_sec','makespan_sec']:
            values=[d[metric] for d in selected];item=stats(values)
            item['reduction_vs_native_pct']=100*(1-item['mean']/statistics.mean(d[metric] for d in groups['linux']))
            result[profile][metric]=item
    per_case={k:{p:stats(v) for p,v in profiles.items()} for k,profiles in cases.items()}
    for profiles in per_case.values():
        profiles['complete_reduction_vs_native_pct']=100*(1-profiles['complete']['mean']/profiles['linux']['mean'])
    return dict(profiles=result,per_case=per_case,complete_mechanism_counters=counts)


def screens(root):
    """Keep single-run candidate selection separate from repeated formal results."""
    result={}
    for name in ['execution','fair','io-v2','io-progress','io-fast','io-fair']:
        plan=json.loads((root/(name+'-screen-plan.json')).read_text());check_inputs(plan)
        rows=[json.loads(l) for l in (root/(name+'-screen-plan.journal.jsonl')).read_text().splitlines()]
        assert len(rows)==len(plan['runs']),name
        items=[]
        for row,job in zip(rows,plan['runs']):
            assert row['output']==job['output'] and row['profile']==job['profile']
            assert row['returncode']==0 and row['status']=='pass' and row['work_match'] and row['scheduler_identity_ok']
            out=Path(row['output']);data=json.loads((out/'result.json').read_text())
            identity=data['tested_system'];capture=data['kernel_capture']
            assert identity['scheduler_valid'] and identity['scheduler_alive_at_workload_end']
            assert identity['sched_ext_during']['state']==identity['sched_ext_at_workload_end']['state']=='enabled'
            assert capture['complete'] and capture['dropped_events']==0 and not capture['missing_session_ids']
            assert data['aggregate']['unexpected_failure_commands']==0
            log=(out/'scheduler.log').read_text()
            items.append(dict(profile=row['profile'],
                mean_workflow_completion_sec=data['aggregate']['mean_workflow_completion_sec'],
                makespan_sec=data['aggregate']['makespan_sec'],
                per_case={c['instance_id']:c['workflow_completion_sec'] for c in data['cases']},
                counters={k:int(v) for k,v in re.findall(r'(ready_io_requests|io_return_candidates|io_return_dispatches|io_return_preemptions)=(\d+)',log)}))
        result[name]=items
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root',type=Path)
    args=parser.parse_args();r=args.root
    summary=dict(formal=mechanism(r,'formal','formal'),ablation=mechanism(r,'ablation','ablation-runs'),
                 hrb=full_workloads(r),screens=screens(r),interpretation='Synthetic mechanism and real-workflow results are separate; old Linux-labelled runs with active FlowCausal are excluded, not rewritten. Single-run screens are not pooled into formal means.')
    (r/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(summary['hrb']['profiles'],indent=2))


if __name__=='__main__':main()
