"""Run the complete coordinator against recorded responses, without a browser.

The snapshot stays read-only. No handwritten match_evidence is loaded: detail
enrichment and all local merges must work through the normal coordinator.
The destination is explicitly labelled as a simulation, never a live capture.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from run_online import JST_BATCH_SIZE, OnlineRunner, atomic_json, file_sha256, read_json


class SnapshotAdapter:
    def __init__(self, source: Path):
        self.source = source
        self.calls = []
        self.hashes = {}
        self.goods = {}
        for part in sorted(source.glob('jst_part_*.json')):
            if not re.fullmatch(r'jst_part_\d+\.json', part.name):
                continue
            for row in self.read(part.name)['data']:
                self.goods[row['input_goods_code']] = row

    def read(self, name):
        file = self.source / name
        self.hashes[name] = file_sha256(file)
        return read_json(file)

    def __call__(self, site, operation, input_path, output_path):
        request = read_json(input_path)
        self.calls.append({'site': site, 'operation': operation,
                           'count': len(request.get('codes', request.get('orders', [])))})
        if operation == 'context':
            result = self.read('context_qianniu.json' if site == 'qianniu' else 'context_piaoju.json')
        elif operation == 'applications':
            result = self.read('applications.json')
        elif operation == 'export':
            source = self.source / 'qianniu_common.xlsx'
            self.hashes[source.name] = file_sha256(source)
            return source.read_bytes()
        elif operation == 'orders':
            # This acceptance fixture currently supports the recorded one-batch case.
            result = self.read('orders_part_001.json')
            found = {str(row['order_no']) for row in result['items']}
            if found | set(result['missing']) != set(request['orders']):
                raise AssertionError('Snapshot order range differs from coordinator request')
        elif operation == 'detail':
            order = request['order_no']
            normal = f'detail_{order}.json'
            result = self.read(normal if (self.source / normal).exists() else f'detail_enrich_{order}.json')
        elif site == 'jst' and operation == 'query':
            result = {'blocked': False, 'data': [self.goods[code] for code in request['codes']]}
        else:
            raise AssertionError(f'Unexpected call: {site}/{operation}')
        return {'payload': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output-root', required=True, type=Path)
    parser.add_argument('--expected-ready', type=int)
    parser.add_argument('--expected-amount')
    parser.add_argument('--expected-blocked', type=int)
    args = parser.parse_args()
    source = args.snapshot.resolve()
    context = read_json(source / 'capture_context.json')
    adapter = SnapshotAdapter(source)
    issuer = adapter.read('context_piaoju.json')['issuer']
    runner = OnlineRunner(date=context['date'], store=context['store'], issuer=issuer,
                          output_root=args.output_root, adapter_runner=adapter, plan_only=True)
    runner.state['simulation'] = True
    runner.state['simulation_source'] = str(source)
    runner._write_state()
    result = runner.run()
    expected = {'ready_count': args.expected_ready, 'ready_amount': args.expected_amount,
                'blocked_count': args.expected_blocked}
    for key, value in expected.items():
        if value is not None:
            assert str(result[key]) == str(value), (key, result[key], value)
    first_calls = list(adapter.calls)
    query_count = Counter((x['site'], x['operation']) for x in first_calls)
    # The recorded fixture uses the same public batch size as the live path.
    # Enrichment must reuse successfully collected codes.
    assert query_count[('jst', 'query')] == (len(adapter.goods) + JST_BATCH_SIZE - 1) // JST_BATCH_SIZE, first_calls
    assert query_count[('qianniu', 'orders')] == 1, first_calls
    resumed = OnlineRunner(date=context['date'], store=context['store'], issuer=issuer,
                           output_root=args.output_root, resume=runner.run_dir,
                           adapter_runner=adapter, plan_only=True)
    resumed.run()
    resume_calls = adapter.calls[len(first_calls):]
    assert [call['operation'] for call in resume_calls] == ['context', 'context'], resume_calls
    for name, digest in adapter.hashes.items():
        assert file_sha256(source / name) == digest, f'Source changed: {name}'
    evidence = runner.generated_dir / 'derived_match_evidence.json'
    report = {'simulation': True, 'source': str(source), 'source_unchanged': True,
              'no_manual_match_evidence': not (runner.input_dir / 'match_evidence.json').exists(),
              'result': result, 'first_run_calls': first_calls, 'resume_calls': resume_calls,
              'source_sha256': adapter.hashes,
              'derived_evidence_present': evidence.exists()}
    atomic_json(runner.run_dir / 'simulation-verification.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
