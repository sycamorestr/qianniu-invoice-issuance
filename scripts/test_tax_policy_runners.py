"""Frozen tax policy routing and resume guards; no browser or network."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import invoice_tax_policy
import run_online
from manage_browsers import initialize
from run_batch import BatchRunner
from test_run_batch import Jobs, write
from test_run_online import FakeAdapter


PIAOJU = {'source': 'piaoju'}
FIXED = {'source': 'fixed', 'rate': '0.1'}


class TaxPolicyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'tax-rates.json'
        self.write_config()
        self.default = patch.object(invoice_tax_policy, 'DEFAULT_CONFIG', self.config)
        self.default.start()
        self.addCleanup(self.default.stop)

    def write_config(self, rate='0.1'):
        write(self.config, {'schema_version': 1, 'default': {'source': 'fixed', 'rate': rate},
                            'stores': {'票聚税率示例店': PIAOJU}})

    def online(self, store='固定税率示例店', **kwargs):
        kwargs.setdefault('adapter_runner', FakeAdapter())
        return run_online.OnlineRunner(date='2026-09-29', store=store, issuer='主体',
                                       output_root=self.root / 'outputs', **kwargs)

    def registry(self):
        source = self.root / 'shops-input.json'
        write(source, [{'id': 'shop01', 'store': '票聚税率示例店'},
                       {'id': 'shop02', 'store': '固定税率示例店'}])
        initialize(self.root / 'work', source, '主体')
        return self.root / 'work/shops.json'

    def test_new_online_freezes_policy_and_uses_same_file_for_probe_and_final(self):
        for store, expected in (('票聚税率示例店', PIAOJU), ('固定税率示例店', FIXED)):
            with self.subTest(store=store):
                self.write_config()
                runner = self.online(store, tax_rate_config=self.config)
                self.assertEqual(runner.state['tax_rate_policy'], expected)
                self.assertEqual(run_online.read_json(runner.tax_rate_policy_path), expected)
                original = runner.tax_rate_policy_path.read_bytes()
                self.write_config('0.2')
                with patch.object(runner, 'run_script') as script:
                    runner._run_invoice(runner.run_dir / 'probe', plan_only=True)
                    runner._run_invoice(runner.generated_dir)
                for call in script.call_args_list:
                    args = call.args[0]
                    self.assertEqual(args[args.index('--tax-rate-policy-file') + 1],
                                     str(runner.tax_rate_policy_path))
                self.assertEqual(runner.tax_rate_policy_path.read_bytes(), original)
                self.assertEqual(runner.state['tax_rate_policy_file']['sha256'],
                                 run_online.file_sha256(runner.tax_rate_policy_path))

    def test_resume_ignores_changed_default_and_rejects_explicit_mismatch(self):
        runner = self.online()
        before = (runner.run_dir / 'run-state.json').read_bytes()
        self.write_config('0.2')
        resumed = self.online(resume=runner.run_dir)
        self.assertEqual(resumed.tax_rate_policy, FIXED)
        for kwargs in ({'tax_rate_config': self.config},
                       {'tax_rate_policy': {'source': 'fixed', 'rate': '0.2'}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(run_online.OnlineError) as caught:
                self.online(resume=runner.run_dir, **kwargs)
            self.assertEqual(caught.exception.code, 'resume_mismatch')
        self.assertEqual((runner.run_dir / 'run-state.json').read_bytes(), before)

    def test_legacy_resume_uses_piaoju_without_reading_new_default_config(self):
        runner = self.online(tax_rate_policy=PIAOJU)
        state = run_online.read_json(runner.run_dir / 'run-state.json')
        state.pop('tax_rate_policy')
        state.pop('tax_rate_policy_file')
        write(runner.run_dir / 'run-state.json', state)
        runner.tax_rate_policy_path.unlink()
        with patch('run_online.resolve_tax_rate_policy', side_effect=AssertionError('must not resolve default')):
            resumed = self.online(resume=runner.run_dir)
        self.assertEqual(resumed.tax_rate_policy, PIAOJU)
        self.assertEqual(run_online.read_json(resumed.tax_rate_policy_path), PIAOJU)

    def test_invalid_config_stops_before_creating_run_or_touching_browser(self):
        write(self.config, {'schema_version': 1, 'default': {'source': 'fixed', 'rate': '10%'}, 'stores': {}})
        fake = FakeAdapter()
        with self.assertRaises(run_online.OnlineError) as caught:
            self.online(adapter_runner=fake)
        self.assertEqual(caught.exception.code, 'configuration')
        self.assertEqual(fake.calls, [])
        self.assertFalse((self.root / 'outputs').exists())

    def test_changed_frozen_file_stops_before_browser(self):
        runner = self.online()
        write(runner.tax_rate_policy_path, PIAOJU)
        fake = FakeAdapter()
        with self.assertRaises(run_online.OnlineError) as caught:
            self.online(resume=runner.run_dir, adapter_runner=fake)
        self.assertEqual(caught.exception.code, 'resume_mismatch')
        with patch.object(runner, '_connect_browser') as browser:
            with self.assertRaises(run_online.OnlineError):
                runner.run()
            browser.assert_not_called()
        self.assertEqual(fake.calls, [])

    def test_empty_result_and_reports_include_policy_without_changing_business_requests(self):
        fake = FakeAdapter()
        def adapter(site, operation, source, output):
            payload = run_online.read_json(source)
            self.assertNotIn('tax_rate_policy', payload)
            if operation == 'applications':
                return {'payload': {'date': payload['date'], 'query_scope': payload['query_scope'],
                                    'rows': [], 'total': 0, 'api_total': 0, 'observed_total': 0}}
            if operation == 'export':
                return b''
            return fake(site, operation, source, output)
        runner = self.online(adapter_runner=adapter)
        result = runner.run()
        self.assertEqual(result['status'], 'no_applications')
        for record in (result, run_online.read_json(runner.run_dir / 'run.json'),
                       run_online.read_json(runner.generated_dir / 'run.json')):
            self.assertEqual(record['tax_rate_policy'], FIXED)

    def test_legacy_empty_manifest_interrupted_before_stage_commit_is_reused(self):
        runner = self.online(tax_rate_policy=PIAOJU)
        for name in ('capture_context.json', 'applications.json'):
            run_online.atomic_json(runner.input_dir / name, {})
        empty = runner.input_dir / 'common-export.bin'
        empty.write_bytes(b'')
        evidence = {'path': str(empty.resolve()), 'sha256': run_online.file_sha256(empty),
                    'reason': 'applications_and_export_empty'}
        runner._generate_no_applications(evidence)
        manifest_path = runner.generated_dir / 'run.json'
        manifest = run_online.read_json(manifest_path)
        manifest.pop('tax_rate_policy')
        run_online.atomic_json(manifest_path, manifest)
        original = manifest_path.read_bytes()
        state = run_online.read_json(runner.run_dir / 'run-state.json')
        state.pop('tax_rate_policy')
        state.pop('tax_rate_policy_file')
        run_online.atomic_json(runner.run_dir / 'run-state.json', state)
        runner.tax_rate_policy_path.unlink()
        resumed = self.online(resume=runner.run_dir)
        resumed._generate_no_applications(evidence)
        self.assertEqual(manifest_path.read_bytes(), original)
        self.assertEqual(resumed.tax_rate_policy, PIAOJU)

    def test_batch_resolves_once_and_routes_all_shops_even_when_config_changes(self):
        registry = self.registry()
        jobs = Jobs()
        def factory(**kwargs):
            self.write_config('0.2')
            return jobs(**kwargs)
        with patch('run_batch.load_tax_rate_config', wraps=invoice_tax_policy.load_tax_rate_config) as load:
            batch = BatchRunner(registry=registry, date='2026-09-29', runner_factory=factory)
            load.assert_called_once()
        self.assertEqual([s['tax_rate_policy'] for s in batch.state['shops']], [PIAOJU, FIXED])
        summary = batch.run()
        self.assertEqual([c['tax_rate_policy'] for c in jobs.calls], [PIAOJU, FIXED])
        self.assertEqual([s['tax_rate_policy'] for s in summary['shops']], [PIAOJU, FIXED])

    def test_pending_batch_resume_keeps_frozen_rules_and_explicit_change_fails(self):
        registry = self.registry()
        batch = BatchRunner(registry=registry, date='2026-09-29', runner_factory=Jobs())
        batch.save()
        before = (batch.run_dir / 'batch-state.json').read_bytes()
        self.write_config('0.2')
        jobs = Jobs()
        with self.assertRaises(run_online.OnlineError) as caught:
            BatchRunner(registry=registry, resume=batch.run_dir,
                        tax_rate_config=self.config, runner_factory=jobs)
        self.assertEqual(caught.exception.code, 'resume_mismatch')
        self.assertEqual(jobs.calls, [])
        self.assertEqual((batch.run_dir / 'batch-state.json').read_bytes(), before)
        resumed = BatchRunner(registry=registry, resume=batch.run_dir, runner_factory=jobs)
        resumed.run()
        self.assertEqual([c['tax_rate_policy'] for c in jobs.calls], [PIAOJU, FIXED])

    def test_legacy_pending_batch_uses_piaoju_for_every_shop(self):
        registry = self.registry()
        batch = BatchRunner(registry=registry, date='2026-09-29', runner_factory=Jobs())
        for shop in batch.state['shops']:
            shop.pop('tax_rate_policy')
        batch.save()
        jobs = Jobs()
        with patch('run_batch.load_tax_rate_config', side_effect=AssertionError('must not load defaults')):
            resumed = BatchRunner(registry=registry, resume=batch.run_dir, runner_factory=jobs)
        resumed.run()
        self.assertEqual([c['tax_rate_policy'] for c in jobs.calls], [PIAOJU, PIAOJU])

    def test_invalid_batch_config_stops_all_shops_before_output_creation(self):
        registry = self.registry()
        jobs = Jobs()
        write(self.config, {})
        output = self.root / 'new-batch-output'
        with self.assertRaises(run_online.OnlineError) as caught:
            BatchRunner(registry=registry, date='2026-09-29', output_root=output,
                        tax_rate_config=self.config, runner_factory=jobs)
        self.assertEqual(caught.exception.code, 'configuration')
        self.assertEqual(jobs.calls, [])
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
