"""Batch isolation and recovery tests; synthetic accounts, no browser calls."""
import json
import tempfile
import unittest
from pathlib import Path

from manage_browsers import initialize
from run_batch import BatchRunner
from run_online import OnlineError
from shop_registry import load_registry


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class ShopFailure(OnlineError):
    def __init__(self, site):
        super().__init__('synthetic login required', 'auth_required')
        self.site = site


class Jobs:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = failures or {}

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(kwargs.copy())
        return self

    def run(self):
        k = self.kwargs
        root = k.get('run_dir') or k['resume']
        generated = root / 'generated'
        if k['store'] in self.failures:
            write(root / 'run-state.json', {'status': 'failed'})
            raise ShopFailure(self.failures[k['store']])
        result = {'status': 'complete', 'generated_dir': str(generated),
                  'ready_count': 1, 'ready_amount': '12.50', 'blocked_count': 0,
                  'blocked_amount': '0', 'excluded_count': 0, 'excluded_amount': '0'}
        write(root / 'run-state.json', {'date': k['date'], 'status': 'complete',
                                      'generated_dir': str(generated), 'stages': {}, 'input_hashes': []})
        write(root / 'run.json', result)
        write(generated / 'run.json', {**result, 'output': str(generated / f"qianniu_invoice_tax_template_{k['date']}.xlsx")})
        (generated / 'exceptions.csv').write_text('synthetic', encoding='utf-8')
        (generated / f"qianniu_common_{k['date']}.xlsx").write_bytes(b'synthetic original')
        (generated / f"qianniu_invoice_tax_template_{k['date']}.xlsx").write_bytes(b'synthetic result')
        return result


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'input.json'
        write(self.input, [{'id': f'shop{i:02}', 'store': f'Test shop {i}'} for i in range(1, 10)])
        initialize(self.root / 'work', self.input, 'Test issuer')
        self.registry = self.root / 'work/shops.json'

    def batch(self, jobs, **kwargs):
        return BatchRunner(registry=self.registry, date='2026-01-01', runner_factory=jobs, **kwargs)

    def test_nine_stores_share_only_jst_and_resume_has_no_browser_calls(self):
        jobs = Jobs()
        batch = self.batch(jobs)
        summary = batch.run()
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(summary['finished_shops'], 9)
        self.assertEqual(summary['totals_of_finished_shops']['ready_amount'], '112.50')
        self.assertEqual(len({k['browser_config'] for k in jobs.calls}), 9)
        self.assertEqual(len({k['jst_browser_config'] for k in jobs.calls}), 1)
        self.assertEqual(len({k['run_dir'] for k in jobs.calls}), 9)
        replay = Jobs()
        self.batch(replay, resume=batch.run_dir).run()
        self.assertEqual(replay.calls, [])

    def test_shop_auth_failure_continues_and_resume_retries_only_failed_shop(self):
        jobs = Jobs({'Test shop 2': 'qianniu'})
        batch = self.batch(jobs)
        result = batch.run()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(jobs.calls), 9)
        followup = Jobs()
        result = self.batch(followup, resume=batch.run_dir).run()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([k['store'] for k in followup.calls], ['Test shop 2'])
        self.assertIn('resume', followup.calls[0])

    def test_shared_failure_stops_once_then_resumes_remaining_shops(self):
        jobs = Jobs({'Test shop 2': 'jst'})
        batch = self.batch(jobs)
        result = batch.run()
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(jobs.calls), 2)
        self.assertEqual(result['shops'][2]['status'], 'pending')
        resumed = Jobs()
        result = self.batch(resumed, resume=batch.run_dir).run()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(resumed.calls), 8)

    def test_tampered_output_stops_before_any_new_job(self):
        batch = self.batch(Jobs({'Test shop 2': 'jst'}))
        batch.run()
        path = Path(batch.state['shops'][0]['common_template'])
        path.write_bytes(b'changed')
        jobs = Jobs()
        with self.assertRaises(OnlineError):
            self.batch(jobs, resume=batch.run_dir).run()
        self.assertEqual(jobs.calls, [])

    def test_registry_change_blocks_resume(self):
        batch = self.batch(Jobs()); batch.run()
        config = self.root / 'work/config/piaoju.json'
        value = json.loads(config.read_text())
        value['profile_directory'] = 'Profile 2'
        write(config, value)
        with self.assertRaises(OnlineError):
            self.batch(Jobs(), resume=batch.run_dir)

    def test_edited_pending_shop_identity_rejected_before_requests(self):
        batch = self.batch(Jobs({'Test shop 1': 'jst'})); batch.run()
        path = batch.run_dir / 'batch-state.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for key, value in (('store', 'Another store'), ('id', 'shop01'),
                           ('browser_config', 'some-other-config.json'), ('run_dir', 'other')):
            with self.subTest(key=key):
                edited = json.loads(json.dumps(original))
                edited['shops'][1][key] = value
                write(path, edited)
                jobs = Jobs()
                with self.assertRaises(OnlineError):
                    self.batch(jobs, resume=batch.run_dir)
                self.assertEqual(jobs.calls, [])

    def test_profiles_and_ports_cannot_overlap(self):
        one = self.root / 'work/config/shop01.json'
        two = self.root / 'work/config/shop02.json'
        original = json.loads(two.read_text())
        reference = json.loads(one.read_text())
        for changes in ({'user_data_dir': reference['user_data_dir']},
                        {'user_data_dir': reference['user_data_dir'] + '/nested'},
                        {'remote_debugging_port': reference['remote_debugging_port']},
                        {'download_dir': reference['download_dir']}):
            with self.subTest(changes=list(changes)):
                write(two, {**original, **changes})
                with self.assertRaises(OnlineError):
                    load_registry(self.registry)
        write(two, original)

    def test_initialization_preserves_existing_profiles_and_rejects_passwords(self):
        marker = self.root / 'work/profiles/shop01/keep'
        marker.write_text('existing session')
        before = self.registry.read_bytes()
        result = initialize(self.root / 'work', self.input, 'Test issuer')
        self.assertEqual(result['status'], 'already_initialized')
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(marker.read_text(), 'existing session')
        write(self.input, [{'id': 'shop01', 'store': 'Test', 'password': 'synthetic'}])
        with self.assertRaises(OnlineError):
            initialize(self.root / 'other', self.input, 'Test issuer')
        self.assertFalse((self.root / 'other').exists())

    def test_explicit_subset_and_independent_outputs(self):
        jobs = Jobs()
        batch = self.batch(jobs, shops=['shop09', 'shop03'])
        batch.run()
        self.assertEqual([item['store'] for item in jobs.calls], ['Test shop 9', 'Test shop 3'])
        self.assertTrue((batch.run_dir / 'batch-summary.csv').is_file())
        with self.assertRaises(OnlineError):
            self.batch(Jobs(), shops=['shop03', 'shop03'])


if __name__ == '__main__':
    unittest.main()
