"""Delivery boundaries: no browser/network, no changes to business checkpoints."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_batch
import run_online
import invoice_delivery
from manage_browsers import initialize
from test_run_batch import Jobs, write


class DeliveryEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.output = self.work / 'outputs'
        self.config = self.work / 'notifications.json'
        self.config.write_text('{}', encoding='utf-8')
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.skill_root = self.root / 'skill'
        self.skill_root.mkdir()
        self.stack.enter_context(patch.object(invoice_delivery, '__file__',
            str(self.skill_root / 'scripts' / 'invoice_delivery.py')))

    def fake_online(self, *, status='complete', replay=False, plan_only=False, failure=None):
        events = []
        runner = SimpleNamespace(output_root=self.output, run_dir=self.output / 'job',
                                 replay=replay, plan_only=plan_only)
        runner._browser_runner = SimpleNamespace(close=lambda: events.append('browser_closed'))

        def run():
            events.append('business')
            if failure:
                raise failure
            return {'status': status, 'run_dir': str(runner.run_dir)}

        runner.run = Mock(side_effect=run)
        return runner, events

    def online_args(self, *extra):
        return ['--date', '2026-09-29', '--store', '测试店铺', '--issuer', '测试主体',
                '--output-root', str(self.output), *extra]

    def test_single_delivery_runs_after_browser_close_and_uses_default_config(self):
        runner, events = self.fake_online()

        def deliver(*args, **kwargs):
            events.append('delivery')
            return {'status': 'sent'}

        with patch.object(run_online, 'OnlineRunner', return_value=runner), \
                patch.object(run_online, 'finalize_delivery', side_effect=deliver) as send:
            self.assertEqual(run_online.main(self.online_args()), 0)
        self.assertEqual(events, ['business', 'browser_closed', 'delivery'])
        send.assert_called_once_with(runner.run_dir, self.config, package_only=False)
        runner.run.assert_called_once_with()

    def test_single_resume_uses_original_actual_output_root(self):
        runner, _ = self.fake_online()
        write(runner.run_dir / 'run-state.json', {
            'date': '2026-09-29', 'store': '测试店铺', 'issuer': '测试主体'})
        with patch.object(run_online, 'OnlineRunner', return_value=runner), \
                patch.object(run_online, 'finalize_delivery', return_value={'status': 'sent'}) as send:
            self.assertEqual(run_online.main(['--resume', str(runner.run_dir)]), 0)
        send.assert_called_once_with(runner.run_dir, self.config, package_only=False)

    def test_single_skill_config_takes_precedence_over_legacy(self):
        local = self.skill_root / 'notifications.json'
        write(local, {'schema_version': 1, 'wecom': {'enabled': False, 'webhook_url': ''}})
        runner, _ = self.fake_online()
        with patch.object(run_online, 'OnlineRunner', return_value=runner), \
                patch.object(run_online, 'finalize_delivery', return_value={'status': 'disabled'}) as send:
            self.assertEqual(run_online.main(self.online_args()), 0)
        send.assert_called_once_with(runner.run_dir, local, package_only=False)

    def test_single_explicit_config_wins_and_no_notify_still_packages(self):
        runner, _ = self.fake_online()
        explicit = self.root / 'private-config.json'
        with patch.object(run_online, 'OnlineRunner', return_value=runner), \
                patch.object(run_online, 'finalize_delivery', return_value={'status': 'packaged'}) as send:
            self.assertEqual(run_online.main(self.online_args(
                '--notification-config', str(explicit), '--no-notify')), 0)
        send.assert_called_once_with(runner.run_dir, explicit, package_only=True)

    def test_single_missing_default_config_is_not_an_error(self):
        runner, _ = self.fake_online()
        runner.output_root = self.root / 'elsewhere' / 'outputs'
        with patch.object(run_online, 'OnlineRunner', return_value=runner), \
                patch.object(run_online, 'finalize_delivery', return_value={'status': 'not_configured'}) as send:
            self.assertEqual(run_online.main(self.online_args()), 0)
        send.assert_called_once_with(runner.run_dir, None, package_only=False)

    def test_replay_and_resumed_replay_can_only_package(self):
        runner, _ = self.fake_online(replay=True)
        source = self.root / 'snapshot'
        write(source / 'capture_context.json', {'date': '2026-09-29', 'store': '测试店铺', 'issuer': '测试主体'})
        write(runner.run_dir / 'run-state.json', {'date': '2026-09-29', 'store': '测试店铺',
                                                'issuer': '测试主体', 'mode': 'replay'})
        for argv in (['--replay-input', str(source)], ['--resume', str(runner.run_dir)]):
            with self.subTest(argv=argv), patch.object(run_online, 'OnlineRunner', return_value=runner), \
                    patch.object(run_online, 'finalize_delivery', return_value={'status': 'packaged'}) as send:
                self.assertEqual(run_online.main(argv), 0)
                send.assert_called_once_with(runner.run_dir, self.config, package_only=True)

    def test_single_plan_only_and_failed_jobs_do_not_deliver(self):
        for options, expected in (({'plan_only': True}, 0), ({'status': 'plan_only'}, 0),
                                  ({'failure': run_online.OnlineError('synthetic failure')}, 2)):
            runner, _ = self.fake_online(**options)
            with self.subTest(options=options), patch.object(run_online, 'OnlineRunner', return_value=runner), \
                    patch.object(run_online, 'finalize_delivery') as send:
                self.assertEqual(run_online.main(self.online_args()), expected)
                send.assert_not_called()

    def test_single_notification_failure_has_separate_exit_and_no_business_retry(self):
        for status in ('failed', 'unknown', 'busy'):
            runner, _ = self.fake_online()
            with self.subTest(status=status), patch.object(run_online, 'OnlineRunner', return_value=runner), \
                    patch.object(run_online, 'finalize_delivery', return_value={'status': status}):
                self.assertEqual(run_online.main(self.online_args()), 3)
                runner.run.assert_called_once_with()

    def test_unhandled_delivery_error_does_not_print_exception_contents(self):
        sentinel = 'private-webhook-token-that-must-not-appear'
        deliver = Mock(side_effect=ValueError(sentinel))
        with patch.dict(sys.modules, {'invoice_delivery': SimpleNamespace(deliver_result=deliver)}):
            result = run_online.finalize_delivery(self.output / 'job', self.config, package_only=False)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(sentinel, json.dumps(result) + self.stdout.getvalue() + self.stderr.getvalue())
        self.assertIn('不重新采集', self.stderr.getvalue())
        deliver.assert_called_once()

    def registry(self):
        account_file = self.root / 'accounts.json'
        write(account_file, [{'id': f'shop{i:02}', 'store': f'Test shop {i}'} for i in range(1, 4)])
        initialize(self.work, account_file, 'Test issuer')
        return self.work / 'shops.json'

    def test_batch_sends_once_after_lock_release_and_notification_retry_skips_completed_shops(self):
        registry = self.registry()
        jobs = Jobs()
        runners = []
        runner_class = run_batch.BatchRunner

        def factory(**kwargs):
            self.assertNotIn('notification_config', kwargs)
            self.assertNotIn('no_notify', kwargs)
            runner = runner_class(**kwargs, runner_factory=jobs)
            runners.append(runner)
            return runner

        def deliver(run_dir, config_path, *, package_only):
            runner = runners[-1]
            # A new holder can acquire the same mutex only after BatchRunner.run ends.
            lock = run_batch.FileMutex(run_dir / '.batch.lock')
            lock.acquire()
            lock.release()
            self.assertEqual(config_path, self.config)
            self.assertFalse(package_only)
            self.assertEqual(runner.state['status'], 'complete')
            return {'status': 'failed' if len(runners) == 1 else 'sent'}

        with patch.object(run_batch, 'BatchRunner', side_effect=factory), \
                patch.object(run_batch, 'finalize_delivery', side_effect=deliver) as send:
            self.assertEqual(run_batch.main(['--registry', str(registry), '--date', '2026-09-29']), 3)
            self.assertEqual(len(jobs.calls), 3)
            send.assert_called_once()
            batch_dir = runners[0].run_dir
            before = {path: path.read_bytes() for path in (batch_dir / 'shops').rglob('*') if path.is_file()}
            self.assertEqual(run_batch.main(['--registry', str(registry), '--resume', str(batch_dir)]), 0)
            self.assertEqual(len(jobs.calls), 3)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
        summary = run_online.read_json(batch_dir / 'batch-summary.json')
        self.assertEqual(summary['status'], 'complete')
        self.assertNotIn('delivery', summary)

    def test_batch_partial_and_stopped_deliver_but_plan_only_does_not(self):
        registry = self.registry()
        for status, plan_only in (('partial', False), ('stopped', False), ('complete', True)):
            runner = SimpleNamespace(run_dir=self.output / 'batch', state={'plan_only': plan_only},
                                     run=Mock(return_value={'status': status}))
            with self.subTest(status=status, plan_only=plan_only), \
                    patch.object(run_batch, 'BatchRunner', return_value=runner), \
                    patch.object(run_batch, 'finalize_delivery', return_value={'status': 'sent'}) as send:
                self.assertEqual(run_batch.main(['--registry', str(registry), '--date', '2026-09-29']),
                                 0 if status == 'complete' else 2)
                if plan_only:
                    send.assert_not_called()
                else:
                    send.assert_called_once_with(runner.run_dir, self.config, package_only=False)

    def test_batch_explicit_config_and_no_notify(self):
        registry = self.registry()
        explicit = self.root / 'elsewhere.json'
        runner = SimpleNamespace(run_dir=self.output / 'batch', state={'plan_only': False},
                                 run=Mock(return_value={'status': 'complete'}))
        with patch.object(run_batch, 'BatchRunner', return_value=runner), \
                patch.object(run_batch, 'finalize_delivery', return_value={'status': 'packaged'}) as send:
            self.assertEqual(run_batch.main(['--registry', str(registry), '--date', '2026-09-29',
                '--notification-config', str(explicit), '--no-notify']), 0)
        send.assert_called_once_with(runner.run_dir, explicit, package_only=True)

    def test_batch_skill_config_takes_precedence_over_legacy(self):
        local = self.skill_root / 'notifications.json'
        write(local, {'schema_version': 1, 'wecom': {'enabled': False, 'webhook_url': ''}})
        registry = self.registry()
        runner = SimpleNamespace(run_dir=self.output / 'batch', state={'plan_only': False},
                                 run=Mock(return_value={'status': 'complete'}))
        with patch.object(run_batch, 'BatchRunner', return_value=runner), \
                patch.object(run_batch, 'finalize_delivery', return_value={'status': 'disabled'}) as send:
            self.assertEqual(run_batch.main(['--registry', str(registry), '--date', '2026-09-29']), 0)
        send.assert_called_once_with(runner.run_dir, local, package_only=False)


if __name__ == '__main__':
    unittest.main()
