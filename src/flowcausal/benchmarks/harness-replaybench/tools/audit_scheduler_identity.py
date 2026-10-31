#!/usr/bin/env python3
"""Audit Linux-labelled results against recorded scheduler state; never alter raw results."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess


def audit(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('scheduler') != 'linux':
        return None
    system = data.get('tested_system') or {}
    during = system.get('sched_ext_during') or {}
    end = system.get('sched_ext_at_workload_end') or {}
    command = system.get('command') or []
    if command or during.get('state') == 'enabled' or end.get('state') == 'enabled':
        verdict = 'mislabeled_linux'
    elif during.get('state') == 'disabled' and end.get('state') == 'disabled' and system.get('scheduler_valid'):
        verdict = 'native_linux_verified'
    else:
        verdict = 'insufficient_identity_evidence'
    return dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                verdict=verdict, command=command, during=during, end=end,
                suite_id=data.get('suite_id'), status=data.get('status'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    rows=[]
    paths=subprocess.check_output(['rg','--files','--hidden','--no-ignore',
        '-g','result.json','-g','!**/.git/**','-g','!**/cases/**',str(args.root)],text=True)
    for name in sorted(paths.splitlines()):
        row=audit(Path(name))
        if row: rows.append(row)
    counts=dict(Counter(row['verdict'] for row in rows))
    with args.output.open('x') as stream:
        json.dump(dict(counts=counts, results=rows),stream,ensure_ascii=False,indent=2)
        stream.write('\n')
    print(json.dumps(counts))


if __name__=='__main__': main()
