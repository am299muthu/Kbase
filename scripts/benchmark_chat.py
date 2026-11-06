"""Repeat live authenticated chat requests and retain content-free timings."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import getpass
import json
import math
from pathlib import Path
import statistics
import time

import httpx

QUERIES = {
    'simple': 'Covid-19 deaths in Q2 2022 ?',
    'multi': 'Covid-19 deaths in Q2 2022 ?\nwhat was net profit for wholesale banking in the FY 2022 ?',
}


def summarize(rows):
    groups = {}
    for kind in QUERIES:
        selected = [row for row in rows if row['case'] == kind]
        metrics = defaultdict(list)
        for row in selected:
            metrics['end_to_end'].append(row['elapsed_ms'])
            for stage, duration in row['stage_totals_ms'].items():
                metrics[stage].append(duration)
        groups[kind] = {
            'requests': len(selected),
            'successes': sum(row['success'] for row in selected),
            'metrics_ms': {name: {'n': len(values), 'p50': round(statistics.median(values), 3),
                                  'p95': round(sorted(values)[math.ceil(.95 * len(values)) - 1], 3)}
                           for name, values in metrics.items()},
        }
    return {'percentiles': 'p50 median; p95 nearest rank. Includes failed requests. Missing stages are excluded.',
            'aggregation': 'Stage totals sum calls per request, including retries. Concurrent and nested stages overlap.',
            'cases': QUERIES, 'groups': groups}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeats', type=int, default=10)
    parser.add_argument('--employee', default='E001')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error('repeats must be 1–100 per case')
    password = getpass.getpass('Local app password: ')
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=1080, trust_env=False) as client:
        client.post('/api/login', json={'employee_id': args.employee, 'password': password}).raise_for_status()
        try:
            for repetition in range(1, args.repeats + 1):
                for kind, query in QUERIES.items():
                    started = time.perf_counter()
                    row = {'case': kind, 'repetition': repetition, 'at': datetime.now(timezone.utc).isoformat(),
                           'top_k': 5, 'stage_totals_ms': {}, 'samples': [], 'success': False}
                    try:
                        response = client.post('/chat', json={'message': query, 'top_k': 5})
                        response.raise_for_status()
                        data = response.json()
                        row['escalation'] = data.get('escalation')
                        row['citation_count'] = len(data.get('citations', []))
                        row['success'] = not row['escalation'] and row['citation_count'] > 0
                        row['samples'] = data.get('timings', [])
                        for step in data.get('trace', []):
                            if step['tool'] == 'plan_questions':
                                row['planning_method'] = step['result']['method']
                                row['samples'].append({'stage': 'planning', 'duration_ms': step['duration_ms'], 'status': 'ok'})
                            elif step['tool'] == 'searchdocuments':
                                row['samples'].extend(step['result'].get('timings', []))
                        totals = defaultdict(float)
                        for sample in row['samples']:
                            totals[sample['stage']] += sample['duration_ms']
                        row['stage_totals_ms'] = dict(totals)
                    except Exception as exc:
                        row['error_type'] = type(exc).__name__
                    row['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)
                    rows.append(row)
                    with (args.output / 'samples.jsonl').open('a') as file:
                        file.write(json.dumps(row) + '\n')
                    (args.output / 'summary.json').write_text(json.dumps(summarize(rows), indent=2))
                    print(f"{kind} {repetition}/{args.repeats}: {row['elapsed_ms']/1000:.1f}s success={row['success']}", flush=True)
        finally:
            client.post('/api/logout')


if __name__ == '__main__':
    main()
