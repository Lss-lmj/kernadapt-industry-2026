#!/usr/bin/env python3
"""Compare post-run work observations without modifying either result."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runners'))
from work_observation import compare


def compare_results(reference, candidate):
    a = json.loads((reference / 'result.json').read_text())
    b = json.loads((candidate / 'result.json').read_text())
    left = {case['instance_id']: case for case in a['cases']}
    right = {case['instance_id']: case for case in b['cases']}
    rows = []
    for task in sorted(left.keys() | right.keys()):
        if task not in left or task not in right:
            rows.append(dict(instance_id=task, match=False, reason='missing task'))
            continue
        observations = [case.get('work_observation') for case in (left[task], right[task])]
        if not all(observations):
            rows.append(dict(instance_id=task, match=False, reason='missing work observation'))
            continue
        files = [root / obs['file'] for root, obs in zip((reference, candidate), observations)]
        row = compare(*(json.loads(path.read_text()) for path in files))
        row['instance_id'] = task
        row['run_passed'] = all(case['status'] == 'pass' for case in (left[task], right[task]))
        row['match'] &= row['run_passed']
        rows.append(row)
    return dict(reference=str(reference), candidate=str(candidate),
                match=all(row['match'] for row in rows) and bool(rows), cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = compare_results(args.reference, args.candidate)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')
    return 0 if result['match'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
