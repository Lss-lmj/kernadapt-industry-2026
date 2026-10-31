"""Compact, post-run evidence of executed work; never predicts correctness from timing."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
COUNTS = re.compile(r'(\d+) (passed|failed|skipped|deselected|errors?|xfailed|xpassed|warnings?)\b')


def summarize_output(text: str) -> dict:
    outcomes = []
    exceptions = []
    installed = []
    for line in ANSI.sub('', text).splitlines():
        line = line.strip()
        match = re.fullmatch(r'Successfully installed (.+)', line)
        if match:
            installed.append(sorted(match.group(1).split()))
        if re.search(r'\bin \d+(?:\.\d+)?s\b', line):
            counts = [(kind.rstrip('s') if kind.startswith(('error','warning')) else kind, int(n))
                      for n, kind in COUNTS.findall(line) if not kind.startswith('warning')]
            if counts:
                outcomes.append({'framework': 'pytest', 'counts': sorted(counts)})
        match = re.fullmatch(r'Ran (\d+) tests? in [\d.]+s', line)
        if match:
            outcomes.append({'framework': 'unittest', 'tests': int(match.group(1))})
        if re.fullmatch(r'(?:OK|FAILED)(?: \([^\n]*\))?', line):
            outcomes.append({'framework': 'unittest', 'status': line})
        # Keep only the exception type; paths, addresses and timing are unstable.
        match = re.match(r'^([A-Za-z_][\w.]*(?:Error|Exception)):', line)
        if match:
            exceptions.append(match.group(1))
    return {'test_outcomes': outcomes, 'exception_types': sorted(set(exceptions)),
            'installed_distributions': installed}


def observe(events: list[dict], output_path: pathlib.Path, trace: dict) -> dict:
    rows = []
    commands = trace['commands']
    asynchronous = any(c.get('async', c.get('asynchronous', False)) for c in commands)
    with output_path.open('rb') as handle:
        for event in events:
            if event.get('record') != 'command_end':
                continue
            index = int(event['command_index'])
            command = commands[index]
            span = event.get('output_span')
            if span is None:
                summary = {'test_outcomes': [], 'exception_types': [], 'installed_distributions': []}
                length = 0
            else:
                begin, end = span
                handle.seek(begin)
                raw = handle.read(max(0, end-begin))
                length = len(raw)
                summary = summarize_output(raw.decode('utf-8', errors='replace'))
            rows.append({
                'command_index': index, 'command_id': event['command_id'],
                'command_sha256': hashlib.sha256(
                    str(command.get('replay_cmd', command['cmd'])).encode()).hexdigest(),
                'returncode': event.get('returncode'),
                'source_returncode_known': isinstance(command.get('observed_returncode'), int)
                    and command['observed_returncode'] >= 0,
                'source_returncode': command.get('observed_returncode'),
                'output_bytes': length, 'output_span': span,
                'output_attribution': 'shared_async' if asynchronous else 'synchronous_interval',
                **summary,
            })
    return {
        'schema': 'harness_replaybench.work_observation.v1',
        'instance_id': trace['instance_id'],
        'scope': 'Command text, exit status, available pytest/unittest summaries and visible pip installed versions. '
                 'Quiet/truncated installs do not provide a complete dependency lock. '
                 'Not proof of arbitrary output equivalence; surviving descendants may write into later intervals.',
        'expected_commands': len(commands), 'observed_commands': len(rows),
        'source_unknown_commands': sum(not r['source_returncode_known'] for r in rows),
        'commands': rows,
    }


def compare(reference: dict, candidate: dict) -> dict:
    differences = []
    if reference['instance_id'] != candidate['instance_id']:
        differences.append({'reason': 'different task'})
    left = {r['command_id']: r for r in reference['commands']}
    right = {r['command_id']: r for r in candidate['commands']}
    for key in sorted(left.keys() | right.keys()):
        a, b = left.get(key), right.get(key)
        if a is None or b is None:
            differences.append({'command_id': key, 'reason': 'missing command'})
            continue
        fields = ['command_sha256', 'returncode']
        if a['output_attribution'] != 'shared_async' and b['output_attribution'] != 'shared_async':
            fields += ['test_outcomes', 'exception_types', 'installed_distributions']
        for field in fields:
            if a.get(field) != b.get(field):
                differences.append({'command_id': key, 'field': field,
                                    'reference': a.get(field), 'candidate': b.get(field)})
    complete = all(x['observed_commands'] == x['expected_commands'] for x in (reference, candidate))
    return {'match': complete and not differences, 'complete': complete, 'differences': differences}
