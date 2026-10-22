"""Output numbering is frozen without changing collection or application IDs."""
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


class ExportSerialRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'tax-rates.json'
        write(self.config, {'schema_version': 1, 'default': {'source': 'piaoju'}, 'stores': {}})
        default = patch.object(invoice_tax_policy, 'DEFAULT_CONFIG', self.config)
        default.start()
        self.addCleanup(default.stop)

    def online(self, **kwargs):
        kwargs.setdefault('adapter_runner', FakeAdapter())
        return run_online.OnlineRunner(date='2026-09-29', store='示例店', issuer='主体',
                                       output_root=self.root / 'outputs', **kwargs)

    def registry(self):
        source = self.root / 'shops-input.json'
        write(source, [{'id': 'shop01', 'store': '示例店一'},
                       {'id': 'shop02', 'store': '示例店二'}])
        initialize(self.root / 'work', source, '主体')
        return self.root / 'work/shops.json'

    def test_new_online_defaults_to_orders_and_passes_frozen_source_to_both_generators(self):
        runner = self.online()
        self.assertEqual(runner.state['export_serial_source'], 'order_no')
        with patch.object(runner, 'run_script') as script:
            runner._run_invoice(runner.run_dir / 'probe', plan_only=True)
            runner._run_invoice(runner.generated_dir)
        self.assertEqual(script.call_count, 2)
        for call in script.call_args_list:
            args = call.args[0]
            self.assertEqual(args[args.index('--export-serial-source') + 1], 'order_no')

    def test_online_resume_keeps_source_and_rejects_explicit_change(self):
        for source in ('order_no', 'application_no'):
            with self.subTest(source=source):
                runner = self.online(export_serial_source=source)
                original = (runner.run_dir / 'run-state.json').read_bytes()
                resumed = self.online(resume=runner.run_dir)
                self.assertEqual(resumed.export_serial_source, source)
                opposite = 'application_no' if source == 'order_no' else 'order_no'
                with self.assertRaises(run_online.OnlineError) as caught:
                    self.online(resume=runner.run_dir, export_serial_source=opposite)
                self.assertEqual(caught.exception.code, 'resume_mismatch')
                self.assertEqual((runner.run_dir / 'run-state.json').read_bytes(), original)

    def test_legacy_online_resume_keeps_applications_and_does_not_upgrade_state(self):
        runner = self.online()
        runner.state.pop('export_serial_source')
        runner._write_state()
        original = (runner.run_dir / 'run-state.json').read_bytes()
        resumed = self.online(resume=runner.run_dir)
        self.assertEqual(resumed.export_serial_source, 'application_no')
        self.assertNotIn('export_serial_source', resumed.state)
        self.assertEqual((runner.run_dir / 'run-state.json').read_bytes(), original)
        with patch.object(resumed, 'run_script') as script:
            resumed._run_invoice(resumed.generated_dir)
        args = script.call_args.args[0]
        self.assertEqual(args[args.index('--export-serial-source') + 1], 'application_no')
        self.assertNotIn('export_serial_source', run_online.read_json(runner.run_dir / 'run-state.json'))
        with self.assertRaises(run_online.OnlineError) as caught:
            self.online(resume=runner.run_dir, export_serial_source='order_no')
        self.assertEqual(caught.exception.code, 'resume_mismatch')

    def test_new_replay_of_legacy_input_defaults_to_orders(self):
        source = self.root / 'legacy-input'
        write(source / 'capture_context.json', {'date': '2026-09-29', 'store': '示例店', 'issuer': '主体'})
        original = (source / 'capture_context.json').read_bytes()
        runner = self.online(replay_input=source)
        self.assertTrue(runner.replay)
        self.assertEqual(runner.export_serial_source, 'order_no')
        self.assertEqual(runner.state['export_serial_source'], 'order_no')
        self.assertFalse(runner.state['approve_applications'])
        self.assertEqual((source / 'capture_context.json').read_bytes(), original)
        resumed = self.online(resume=runner.run_dir)
        self.assertEqual(resumed.export_serial_source, 'order_no')

    def test_invalid_requested_source_fails_before_output_or_browser(self):
        for invalid in ('', 'order', 1, [], {}):
            with self.subTest(invalid=invalid):
                fake = FakeAdapter()
                with self.assertRaises(run_online.OnlineError) as caught:
                    self.online(export_serial_source=invalid, adapter_runner=fake)
                self.assertEqual(caught.exception.code, 'configuration')
                self.assertEqual(fake.calls, [])
                self.assertFalse((self.root / 'outputs').exists())

    def test_invalid_saved_source_fails_before_browser(self):
        runner = self.online()
        for invalid in (None, '', 'order', 1, [], {}):
            with self.subTest(invalid=invalid):
                runner.state['export_serial_source'] = invalid
                runner._write_state()
                fake = FakeAdapter()
                with self.assertRaises(run_online.OnlineError) as caught:
                    self.online(resume=runner.run_dir, adapter_runner=fake)
                self.assertEqual(caught.exception.code, 'checkpoint_invalid')
                self.assertEqual(fake.calls, [])

    def test_new_batch_freezes_and_routes_orders_to_every_shop(self):
        jobs = Jobs()
        batch = BatchRunner(registry=self.registry(), date='2026-09-29', runner_factory=jobs)
        self.assertEqual(batch.state['export_serial_source'], 'order_no')
        batch.run()
        self.assertEqual([call['export_serial_source'] for call in jobs.calls], ['order_no', 'order_no'])

    def test_pending_legacy_batch_keeps_applications_for_unstarted_shops(self):
        registry = self.registry()
        batch = BatchRunner(registry=registry, date='2026-09-29', runner_factory=Jobs())
        batch.state.pop('export_serial_source')
        batch.save()
        jobs = Jobs()
        resumed = BatchRunner(registry=registry, resume=batch.run_dir, runner_factory=jobs)
        self.assertEqual(resumed.export_serial_source, 'application_no')
        self.assertNotIn('export_serial_source', resumed.state)
        resumed.run()
        self.assertEqual([call['export_serial_source'] for call in jobs.calls], ['application_no', 'application_no'])

    def test_batch_resume_retains_saved_source_and_rejects_change(self):
        registry = self.registry()
        for source in ('order_no', 'application_no'):
            with self.subTest(source=source):
                batch = BatchRunner(registry=registry, date='2026-09-29',
                                    export_serial_source=source, runner_factory=Jobs())
                batch.save()
                original = (batch.run_dir / 'batch-state.json').read_bytes()
                opposite = 'application_no' if source == 'order_no' else 'order_no'
                jobs = Jobs()
                with self.assertRaises(run_online.OnlineError) as caught:
                    BatchRunner(registry=registry, resume=batch.run_dir,
                                export_serial_source=opposite, runner_factory=jobs)
                self.assertEqual(caught.exception.code, 'resume_mismatch')
                self.assertEqual(jobs.calls, [])
                self.assertEqual((batch.run_dir / 'batch-state.json').read_bytes(), original)
                resumed = BatchRunner(registry=registry, resume=batch.run_dir, runner_factory=jobs)
                resumed.run()
                self.assertEqual([call['export_serial_source'] for call in jobs.calls], [source, source])

    def test_legacy_batch_resume_preserves_completed_shop_outputs(self):
        registry = self.registry()
        batch = BatchRunner(registry=registry, date='2026-09-29', export_serial_source='application_no',
                            runner_factory=Jobs({'示例店二': 'jst'}))
        result = batch.run()
        self.assertEqual(result['status'], 'stopped')
        completed = Path(batch.state['shops'][0]['run_dir'])
        original = {path.relative_to(completed): path.read_bytes()
                    for path in completed.rglob('*') if path.is_file()}
        batch.state.pop('export_serial_source')
        batch.save()
        jobs = Jobs()
        resumed = BatchRunner(registry=registry, resume=batch.run_dir, runner_factory=jobs)
        resumed.run()
        self.assertEqual(len(jobs.calls), 1)
        self.assertEqual(jobs.calls[0]['store'], '示例店二')
        self.assertEqual(jobs.calls[0]['export_serial_source'], 'application_no')
        self.assertEqual(original, {path.relative_to(completed): path.read_bytes()
                                    for path in completed.rglob('*') if path.is_file()})

    def test_invalid_batch_source_is_rejected_before_children_run(self):
        registry = self.registry()
        jobs = Jobs()
        for invalid in ('', 'order', 1, [], {}):
            with self.subTest(requested=invalid), self.assertRaises(run_online.OnlineError) as caught:
                BatchRunner(registry=registry, date='2026-09-29', export_serial_source=invalid,
                            runner_factory=jobs)
            self.assertEqual(caught.exception.code, 'configuration')
        batch = BatchRunner(registry=registry, date='2026-09-29', runner_factory=jobs)
        for invalid in (None, '', 'order', 1, [], {}):
            with self.subTest(saved=invalid):
                batch.state['export_serial_source'] = invalid
                batch.save()
                with self.assertRaises(run_online.OnlineError) as caught:
                    BatchRunner(registry=registry, resume=batch.run_dir, runner_factory=jobs)
                self.assertEqual(caught.exception.code, 'checkpoint_invalid')
        self.assertEqual(jobs.calls, [])

    def test_numbering_policy_never_enters_business_requests_or_capture_context(self):
        fake = FakeAdapter()
        def adapter(site, operation, source, output):
            payload = run_online.read_json(source)
            self.assertNotIn('export_serial_source', payload)
            if operation == 'applications':
                return {'payload': {'date': payload['date'], 'query_scope': payload['query_scope'],
                                    'rows': [], 'total': 0, 'api_total': 0, 'observed_total': 0}}
            if operation == 'export':
                return b''
            return fake(site, operation, source, output)
        runner = self.online(adapter_runner=adapter)
        result = runner.run()
        self.assertEqual(result['status'], 'no_applications')
        context = run_online.read_json(runner.input_dir / 'capture_context.json')
        self.assertNotIn('export_serial_source', context)
        self.assertEqual(runner.state['export_serial_source'], 'order_no')


if __name__ == '__main__':
    unittest.main()
