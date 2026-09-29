"""Batch isolation and recovery tests; synthetic accounts, no browser calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manage_browsers import initialize
from invoice_scope import scope_fields as query_scope_fields
from run_batch import BatchRunner, main
from run_online import OnlineError, file_sha256
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
        label = 'all-pending' if k.get('all_pending') else k['date']
        scope_fields = query_scope_fields(k['query_scope'])
        result = {'status': 'complete', 'generated_dir': str(generated),
                  'ready_count': 1, 'ready_amount': '12.50', 'blocked_count': 0,
                  'blocked_amount': '0', 'excluded_count': 0, 'excluded_amount': '0'}
        write(root / 'run-state.json', {'date': k['date'], **scope_fields, 'status': 'complete',
                                      'generated_dir': str(generated), 'stages': {}, 'input_hashes': []})
        write(root / 'run.json', result)
        write(generated / 'run.json', {**result, 'date': k['date'], **scope_fields,
              'output': str(generated / f'qianniu_invoice_tax_template_{label}.xlsx')})
        (generated / 'exceptions.csv').write_text('synthetic', encoding='utf-8')
        (generated / f'qianniu_common_{label}.xlsx').write_bytes(b'synthetic original')
        (generated / f'qianniu_invoice_tax_template_{label}.xlsx').write_bytes(b'synthetic result')
        return result


class EmptyJobs(Jobs):
    def run(self):
        k = self.kwargs
        root = k.get('run_dir') or k['resume']
        generated = root / 'generated'
        generated.mkdir(parents=True)
        evidence = root / 'common-export.bin'
        evidence.write_bytes(b'')
        result = {'status': 'no_applications', 'generated_dir': str(generated),
                  'ready_count': 0, 'blocked_count': 0, 'excluded_count': 0,
                  'ready_amount': '0', 'blocked_amount': '0', 'excluded_amount': '0'}
        scope_fields = query_scope_fields(k['query_scope'])
        write(root / 'run-state.json', {'date': k['date'], **scope_fields, 'generated_dir': str(generated),
                                       'stages': {}, 'input_hashes': []})
        write(root / 'run.json', result)
        write(generated / 'run.json', {**result, 'date': k['date'], **scope_fields, 'common_template_output': None,
              'empty_export': {'path': str(evidence), 'sha256': file_sha256(evidence)}})
        (generated / 'exceptions.csv').write_text('申请流水号,金额,暂缓原因\n', encoding='utf-8')
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
        kwargs.setdefault('date', '2026-01-01')
        return BatchRunner(registry=self.registry, runner_factory=jobs, **kwargs)

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
        self.assertTrue(all('all_pending' not in k for k in jobs.calls))
        self.assertEqual(batch.state['query_scope'], {'mode': 'date', 'date': '2026-01-01', 'countdown': 'started'})
        self.assertEqual(summary['query_scope'], batch.query_scope)
        self.assertTrue(all(k['query_scope'] == batch.query_scope for k in jobs.calls))
        replay = Jobs()
        self.batch(replay, resume=batch.run_dir).run()
        self.assertEqual(replay.calls, [])

    def test_all_pending_scope_routes_to_shops_and_proves_labelled_outputs(self):
        jobs = Jobs()
        batch = self.batch(jobs, date=None, all_pending=True, shops=['shop01', 'shop02'])
        result = batch.run()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(batch.run_dir.name.startswith('batch-all-pending-'))
        self.assertIsNone(batch.state['date'])
        self.assertEqual(batch.state['query_scope']['mode'], 'all_pending')
        self.assertEqual(batch.state['query_scope']['countdown'], 'started')
        self.assertIn('start_date', batch.state['query_scope'])
        self.assertIn('end_date', batch.state['query_scope'])
        saved_summary = json.loads((batch.run_dir / 'batch-summary.json').read_text(encoding='utf-8'))
        self.assertEqual(saved_summary['query_scope'], batch.query_scope)
        self.assertIsNone(saved_summary['date'])
        self.assertTrue(all(call['all_pending'] and call['date'] is None for call in jobs.calls))
        self.assertTrue(all(call['query_scope'] == batch.query_scope for call in jobs.calls))
        for shop in batch.state['shops']:
            proof = {Path(item['path']).name for item in shop['proof']}
            self.assertIn('qianniu_common_all-pending.xlsx', proof)
            self.assertIn('qianniu_invoice_tax_template_all-pending.xlsx', proof)
            self.assertEqual(Path(shop['common_template']).name, 'qianniu_common_all-pending.xlsx')
        replay = Jobs()
        resumed = self.batch(replay, date=None, resume=batch.run_dir)
        self.assertEqual(resumed.run()['query_scope'], batch.query_scope)
        self.assertEqual(replay.calls, [])

    def test_all_pending_resume_infers_scope_for_unfinished_shops(self):
        batch = self.batch(Jobs({'Test shop 1': 'qianniu'}), date=None,
                           all_pending=True, shops=['shop01', 'shop02'])
        self.assertEqual(batch.run()['status'], 'partial')
        jobs = Jobs()
        result = self.batch(jobs, date=None, resume=batch.run_dir).run()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(jobs.calls), 1)
        self.assertTrue(jobs.calls[0]['all_pending'])
        self.assertEqual(jobs.calls[0]['query_scope'], batch.query_scope)
        self.assertIsNone(jobs.calls[0]['date'])
        self.assertIn('resume', jobs.calls[0])

    def test_explicit_all_pending_resume_keeps_saved_two_month_window(self):
        batch = self.batch(Jobs({'Test shop 1': 'qianniu'}), date=None,
                           all_pending=True, shops=['shop01'])
        batch.run()
        frozen = {'mode': 'all_pending', 'start_date': '2026-01-01', 'end_date': '2026-03-01'}
        batch.state['query_scope'] = frozen
        write(batch.run_dir / 'batch-state.json', batch.state)
        jobs = Jobs()
        result = self.batch(jobs, date=None, all_pending=True, resume=batch.run_dir).run()
        self.assertEqual(result['query_scope'], frozen)
        self.assertEqual(jobs.calls[0]['query_scope'], frozen)

    def test_all_pending_empty_export_is_proven_without_common_workbook(self):
        batch = self.batch(EmptyJobs(), date=None, all_pending=True, shops=['shop01'])
        result = batch.run()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['shops'][0]['status'], 'no_applications')
        self.assertIsNone(result['shops'][0]['common_template'])
        self.assertFalse(list((batch.run_dir / 'shops/shop01/generated').glob('*.xlsx')))
        proof = {Path(item['path']).name for item in batch.state['shops'][0]['proof']}
        self.assertIn('common-export.bin', proof)
        jobs = Jobs()
        self.batch(jobs, date=None, resume=batch.run_dir).run()
        self.assertEqual(jobs.calls, [])
        evidence = batch.run_dir / 'shops/shop01/common-export.bin'
        evidence.write_bytes(b'changed')
        with self.assertRaises(OnlineError):
            self.batch(jobs, date=None, resume=batch.run_dir).run()
        self.assertEqual(jobs.calls, [])

    def test_new_batch_requires_exactly_one_scope(self):
        jobs = Jobs()
        for kwargs in ({'date': None}, {'all_pending': True}, {'date': 'not-a-date'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(OnlineError) as caught:
                self.batch(jobs, **kwargs)
            self.assertEqual(caught.exception.code, 'configuration')
        self.assertEqual(jobs.calls, [])

    def test_resume_rejects_changed_or_corrupt_scope_before_requests(self):
        for initial, changed in (({'date': None, 'all_pending': True}, {'date': '2026-01-01'}),
                                 ({}, {'date': None, 'all_pending': True}),
                                 ({}, {'date': '2026-01-02'})):
            with self.subTest(initial=initial, changed=changed):
                batch = self.batch(Jobs(), shops=['shop01'], **initial)
                batch.run()
                jobs = Jobs()
                with self.assertRaises(OnlineError) as caught:
                    self.batch(jobs, resume=batch.run_dir, **changed)
                self.assertEqual(caught.exception.code, 'resume_mismatch')
                self.assertEqual(jobs.calls, [])
        batch = self.batch(Jobs(), date=None, all_pending=True, shops=['shop01'])
        batch.run()
        batch.state['date'] = '2026-01-01'
        write(batch.run_dir / 'batch-state.json', batch.state)
        with self.assertRaises(OnlineError) as caught:
            self.batch(Jobs(), date=None, resume=batch.run_dir)
        self.assertEqual(caught.exception.code, 'resume_mismatch')

    def test_legacy_dated_resume_infers_date_without_adding_all_pending_flag(self):
        batch = self.batch(Jobs({'Test shop 1': 'qianniu'}), shops=['shop01'])
        batch.run()
        # Model the recorded shape of a task created before countdown filtering.
        batch.state.pop('query_scope')
        write(batch.run_dir / 'batch-state.json', batch.state)
        self.assertNotIn('query_scope', batch.state)
        jobs = Jobs()
        result = self.batch(jobs, date=None, resume=batch.run_dir).run()
        self.assertEqual(result['date'], '2026-01-01')
        self.assertNotIn('query_scope', result)
        self.assertEqual(jobs.calls[0]['date'], '2026-01-01')
        self.assertNotIn('all_pending', jobs.calls[0])
        self.assertEqual(jobs.calls[0]['query_scope'], {'mode': 'date', 'date': '2026-01-01'})

    def test_legacy_pending_child_receives_unfiltered_scope_before_first_run(self):
        batch = self.batch(Jobs(), shops=['shop01'])
        batch.state.pop('query_scope')
        batch.save()
        jobs = Jobs()
        result = self.batch(jobs, date=None, resume=batch.run_dir).run()
        self.assertEqual(result['status'], 'complete')
        self.assertNotIn('query_scope', result)
        self.assertEqual(jobs.calls[0]['query_scope'], {'mode': 'date', 'date': '2026-01-01'})
        self.assertIn('run_dir', jobs.calls[0])
        self.assertNotIn('resume', jobs.calls[0])

    def test_filtered_dated_child_resume_keeps_filter(self):
        batch = self.batch(Jobs({'Test shop 1': 'qianniu'}), shops=['shop01'])
        batch.run()
        jobs = Jobs()
        result = self.batch(jobs, date=None, resume=batch.run_dir).run()
        self.assertEqual(result['query_scope'], batch.query_scope)
        self.assertEqual(jobs.calls[0]['query_scope']['countdown'], 'started')
        self.assertIn('resume', jobs.calls[0])

    def test_cli_rejects_date_and_all_pending_together(self):
        with patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
            main(['--registry', str(self.registry), '--date', '2026-01-01', '--all-pending'])
        self.assertEqual(caught.exception.code, 2)

    def test_verified_empty_export_needs_no_fabricated_common_file(self):
        batch = self.batch(EmptyJobs(), shops=['shop01'])
        result = batch.run()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['shops'][0]['status'], 'no_applications')
        self.assertIsNone(result['shops'][0]['common_template'])
        resumed = Jobs()
        self.batch(resumed, resume=batch.run_dir).run()
        self.assertEqual(resumed.calls, [])
        (batch.run_dir / 'shops/shop01/common-export.bin').write_bytes(b'changed')
        with self.assertRaises(OnlineError):
            self.batch(Jobs(), resume=batch.run_dir).run()

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

    def test_shop_permission_failure_does_not_block_other_shops(self):
        class PermissionJobs(Jobs):
            def run(self):
                if self.kwargs['store'] == 'Test shop 1':
                    raise OnlineError('permission_required', 'permission_required', site='qianniu')
                return super().run()
        jobs = PermissionJobs()
        result = self.batch(jobs, shops=['shop01', 'shop02']).run()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(jobs.calls), 2)
        self.assertIn('权限', result['shops'][0]['recovery_action'])
        self.assertEqual(result['shops'][1]['status'], 'complete')

    def test_order_rate_limit_stops_before_next_shop_and_preserves_completed_shop(self):
        class ThrottledJobs(Jobs):
            def run(self):
                if self.kwargs['store'] == 'Test shop 2':
                    root = self.kwargs.get('run_dir') or self.kwargs['resume']
                    write(root / 'run-state.json', {'status': 'failed'})
                    raise OnlineError('rate_limited', 'rate_limited', site='qianniu')
                return super().run()

        jobs = ThrottledJobs()
        batch = self.batch(jobs)
        result = batch.run()
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual([call['store'] for call in jobs.calls], ['Test shop 1', 'Test shop 2'])
        self.assertEqual(batch.state['shops'][0]['status'], 'complete')
        self.assertEqual(batch.state['shops'][1]['error_code'], 'rate_limited')
        self.assertIn('限流', batch.state['shops'][1]['recovery_action'])
        self.assertEqual(result['finished_shops'], 1)
        resumed_jobs = Jobs()
        resumed = self.batch(resumed_jobs, resume=batch.run_dir).run()
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(len(resumed_jobs.calls), 8)
        self.assertNotIn('Test shop 1', [call['store'] for call in resumed_jobs.calls])

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

    def test_account_binding_is_routed_and_cannot_change_on_resume(self):
        registry = json.loads(self.registry.read_text(encoding='utf-8'))
        registry['shops'][0]['login_username'] = 'account:operator'
        write(self.registry, registry)
        jobs = Jobs()
        batch = self.batch(jobs, shops=['shop01'])
        batch.run()
        self.assertEqual(jobs.calls[0]['expected_account'], 'account:operator')
        registry['shops'][0]['login_username'] = 'another:operator'
        write(self.registry, registry)
        with self.assertRaisesRegex(OnlineError, '登录账号'):
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

    def test_resume_keeps_selection_when_workbench_adds_a_shop(self):
        batch = self.batch(Jobs(), shops=['shop01'])
        batch.run()
        config = self.root / 'work/new-browser.json'
        write(config, {'schema_version': 1, 'browser': 'Edge',
                       'user_data_dir': 'new-shop-profile', 'download_dir': 'new-shop-downloads',
                       'remote_debugging_port': 19999,
                       'browser_sessions': {'home': {'url': 'https://myseller.taobao.com/'}}})
        write(self.registry.parent / '.browser-workbench-shops.json', {
            'schema_version': 1, 'shops': [{'id': 'shop-new', 'store': 'New display label',
                                         'browser_config': str(config)}]})
        resumed = Jobs()
        result = self.batch(resumed, resume=batch.run_dir).run()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['selected_shops'], 1)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(len(self.batch(Jobs()).state['shops']), 10)

    def test_resume_rejects_changed_selected_profile_and_supports_legacy_state(self):
        batch = self.batch(Jobs(), shops=['shop01'])
        batch.run()
        state_path = batch.run_dir / 'batch-state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state.update(version=1, registry_identity_sha256=load_registry(self.registry)['identity_sha256'])
        write(state_path, state)
        self.assertEqual(self.batch(Jobs(), resume=batch.run_dir).run()['status'], 'complete')
        config = self.registry.parent / 'config/shop01.json'
        value = json.loads(config.read_text(encoding='utf-8'))
        value['user_data_dir'] = '../different-profile'
        write(config, value)
        with self.assertRaises(OnlineError):
            self.batch(Jobs(), resume=batch.run_dir)

    def test_connect_only_passes_to_every_shop_and_failure_has_recovery_details(self):
        jobs = Jobs({'Test shop 2': 'qianniu'})
        result = self.batch(jobs, connect_only=True).run()
        self.assertTrue(all(call['connect_only'] for call in jobs.calls))
        failure = result['shops'][1]
        self.assertIn('人工登录', failure['recovery_action'])
        self.assertIsInstance(failure['elapsed_seconds'], float)


if __name__ == '__main__':
    unittest.main()
