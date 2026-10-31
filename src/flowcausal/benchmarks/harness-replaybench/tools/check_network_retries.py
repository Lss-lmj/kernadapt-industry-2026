#!/usr/bin/env python3
"""Locate recorded pip connection retries in retained benchmark output.

This is a post-run diagnostic. It never changes a run's status or timing,
and absence of retry messages does not establish absence of network waits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


RETRY = re.compile(r'^WARNING: Retrying .* after connection broken by .+$', re.MULTILINE)


def inspect_run(output: Path) -> dict:
    result = json.loads((output/'result.json').read_text())
    findings = []
    unchecked = []
    for case in result['cases']:
        relative_events = case.get('retained', {}).get('events')
        if not relative_events:
            unchecked.append(dict(instance_id=case['instance_id'], reason='No retained event path'))
            continue
        events = output/relative_events
        log = events.parent/'command-output.log'
        if not events.is_file() or not log.is_file():
            unchecked.append(dict(instance_id=case['instance_id'], reason='Command events or output not retained'))
            continue
        raw = log.read_bytes()
        for line in events.read_text().splitlines():
            event = json.loads(line)
            if event.get('record') != 'command_end' or 'output_span' not in event:
                continue
            start, end = event['output_span']
            messages = RETRY.findall(raw[start:end].decode(errors='replace'))
            if messages:
                findings.append(dict(instance_id=case['instance_id'],
                    command_id=event['command_id'], command_index=event['command_index'],
                    command_elapsed_sec=event['elapsed_ns']/1e9,
                    retry_messages=messages, output_log=str(log), output_span=[start,end]))
    return dict(result_directory=str(output), status=result['status'],
                aggregate=result['aggregate'], network_retries_observed=bool(findings),
                checked_all_cases=not unchecked, unchecked_cases=unchecked, findings=findings,
                interpretation='Retry messages identify a timing confound, not a workload failure. '
                'Do not subtract these command times or silently discard runs. '
                'No detected retries does not rule out other external waiting.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path, nargs='+', help='Directories containing result.json')
    parser.add_argument('--output', type=Path, help='New report file; original results are never rewritten')
    args = parser.parse_args()
    report = dict(schema='harness_replaybench.network_retries.v1',
                  runs=[inspect_run(p.resolve()) for p in args.results])
    text = json.dumps(report,indent=2,ensure_ascii=False)+'\n'
    if args.output:
        with args.output.open('x') as handle:
            handle.write(text)
    else:
        print(text,end='')


if __name__ == '__main__':
    main()
