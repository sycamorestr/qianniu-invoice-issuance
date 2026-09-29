import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import invoice_delivery as delivery


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def proof(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows runners may expose TEMP through an 8.3 alias.
        self.root = Path(self.temp.name).resolve()
        self.scope = {'mode': 'date', 'date': '2026-09-25', 'countdown': 'started'}
        self.config = self.root / 'notifications.json'
        write_json(self.config, {'schema_version': 1, 'wecom': {
            'enabled': True,
            'webhook_url': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake-test-key'}})

    def make_run(self, path=None, status='complete', empty=False):
        path = path or self.root / 'run'
        generated = path / 'generated'
        generated.mkdir(parents=True)
        manifest = {'date': '2026-09-25', 'query_scope': self.scope,
                    'status': status, 'store': '醉语阁', 'issuer': '测试公司',
                    'selected_count': 2, 'ready_count': 1 if status == 'complete' else 0,
                    'ready_amount': '12.30', 'blocked_count': 1, 'blocked_amount': '4.50',
                    'excluded_count': 0, 'excluded_amount': '0',
                    'browser_config': 'SECRET-CONFIG', 'inputs': {'secret': 'SECRET-RAW'},
                    'error': 'SECRET-KEY'}
        files = []
        if empty:
            empty_path = path / 'common-export.bin'
            empty_path.write_bytes(b'')
            manifest['empty_export'] = proof(empty_path)
        else:
            common = generated / 'qianniu_common_2026-09-25.xlsx'
            common.write_bytes(b'original workbook')
            files.append(common)
            manifest['common_template_output'] = proof(common)
        if status == 'complete':
            tax = generated / 'qianniu_invoice_tax_template_2026-09-25.xlsx'
            tax.write_bytes(b'verified tax workbook')
            files.append(tax)
            manifest.update(output=str(tax), output_sha256=proof(tax)['sha256'])
        exceptions = generated / 'exceptions.csv'
        exceptions.write_bytes(b'header\r\n')
        files.append(exceptions)
        manifest_path = generated / 'run.json'
        write_json(manifest_path, manifest)
        files.append(manifest_path)
        write_json(path / 'run-state.json', {'date': '2026-09-25', 'query_scope': self.scope,
            'status': 'complete', 'result_status': status, 'generated_dir': str(generated),
            'stages': {'generate': {'status': 'complete', 'outputs': [proof(f) for f in files]}}})
        (path / 'cookies.json').write_text('SECRET-COOKIES')
        return path

    def make_batch(self, statuses):
        batch = self.root / 'batch'
        batch.mkdir()
        shops = []
        for index, status in enumerate(statuses, 1):
            sid = f'shop{index:02}'
            child = batch / 'shops' / sid
            row = {'id': sid, 'store': '醉语阁', 'status': status, 'run_dir': str(child),
                   'error': 'SECRET-ERROR'}
            if status in delivery.SUCCESS:
                self.make_run(child, status=status)
                row['proof'] = [proof(f) for f in child.rglob('*') if f.is_file()]
            shops.append(row)
        write_json(batch / 'batch-state.json', {'date': '2026-09-25', 'query_scope': self.scope,
                   'status': 'complete' if all(s in delivery.SUCCESS for s in statuses) else 'partial',
                   'plan_only': False, 'shops': shops})
        return batch

    def names(self, result):
        with zipfile.ZipFile(result['zip_path']) as archive:
            return archive.namelist(), b''.join(archive.read(n) for n in archive.namelist())

    def test_complete_deterministic_private_and_immutable(self):
        run = self.make_run()
        before = {str(f): f.read_bytes() for f in run.rglob('*') if f.is_file()}
        first = delivery.deliver_result(run, package_only=True)
        second = delivery.deliver_result(run, package_only=True)
        self.assertEqual(first['status'], 'packaged')
        self.assertEqual(first['zip_sha256'], second['zip_sha256'])
        names, content = self.names(first)
        self.assertEqual(set(names), {'result.json', 'exceptions.csv', 'qianniu_common_2026-09-25.xlsx',
                                     'qianniu_invoice_tax_template_2026-09-25.xlsx'})
        self.assertNotIn(b'SECRET', content)
        self.assertNotIn(str(run).encode(), content)
        for name, data in before.items():
            self.assertEqual(Path(name).read_bytes(), data)

    def test_no_config_never_discovers_parent_secret(self):
        with patch.object(delivery, '__file__', str(self.root / 'scripts' / 'invoice_delivery.py')), \
                patch.object(delivery, '_request') as network:
            result = delivery.deliver_result(self.make_run())
        self.assertEqual(result['status'], 'not_configured')
        network.assert_not_called()

    def test_config_resolver_priorities_and_cwd_independence(self):
        skill = self.root / 'skill'
        skill.mkdir()
        local = skill / 'notifications.json'
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        explicit = self.root / 'missing-explicit.json'
        with patch.object(delivery, '__file__', str(skill / 'scripts' / 'invoice_delivery.py')), \
                contextlib.chdir(elsewhere):
            self.assertIsNone(delivery.resolve_notification_config())
            self.assertEqual(delivery.resolve_notification_config(legacy=self.config), self.config)
            write_json(local, {'schema_version': 1, 'wecom': {'enabled': False, 'webhook_url': ''}})
            self.assertEqual(delivery.resolve_notification_config(legacy=self.config), local)
            self.assertEqual(delivery.resolve_notification_config(explicit, self.config), explicit)

    def test_disabled_skill_config_never_falls_back_to_enabled_legacy(self):
        skill = self.root / 'skill'
        skill.mkdir()
        local = skill / 'notifications.json'
        write_json(local, {'schema_version': 1, 'wecom': {'enabled': False, 'webhook_url': ''}})
        with patch.object(delivery, '__file__', str(skill / 'scripts' / 'invoice_delivery.py')), \
                patch.object(delivery, '_request') as network:
            config = delivery.resolve_notification_config(legacy=self.config)
            result = delivery.deliver_result(self.make_run(), config)
        self.assertEqual(result['status'], 'disabled')
        self.assertTrue(Path(result['zip_path']).is_file())
        network.assert_not_called()

    def test_cli_defaults_to_skill_config_but_package_only_never_sends(self):
        run = self.make_run()
        with patch.object(delivery, '__file__', str(self.root / 'scripts' / 'invoice_delivery.py')), \
                patch.object(delivery, '_request', side_effect=[
                    {'errcode': 0, 'media_id': 'M'}, {'errcode': 0}]) as network, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(delivery.main(['--run-dir', str(run), '--package-only']), 0)
            network.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['status'], 'packaged')
            output.truncate(0)
            output.seek(0)
            self.assertEqual(delivery.main(['--run-dir', str(run)]), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'sent')
        self.assertEqual(network.call_count, 2)
        self.assertEqual(network.call_args_list[-1].args[0], json.loads(self.config.read_text())['wecom']['webhook_url'])

    def test_cli_with_no_config_only_packages(self):
        with patch.object(delivery, '__file__', str(self.root / 'no-config-skill' / 'scripts' / 'invoice_delivery.py')), \
                patch.object(delivery, '_request') as network, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(delivery.main(['--run-dir', str(self.make_run())]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'not_configured')
        self.assertTrue(Path(result['zip_path']).is_file())
        network.assert_not_called()

    def test_disabled_and_package_only_and_plan_only_never_send(self):
        run = self.make_run(status='plan_only')
        with patch.object(delivery, '_request') as network:
            result = delivery.deliver_result(run, self.config)
            self.assertEqual(result['status'], 'plan_only')
            network.assert_not_called()
        config = json.loads(self.config.read_text())
        config['wecom']['enabled'] = False
        write_json(self.config, config)
        other = self.make_run(self.root / 'other')
        self.assertEqual(delivery.deliver_result(other, self.config)['status'], 'disabled')
        self.assertEqual(delivery.deliver_result(other, self.root / 'missing', package_only=True)['status'], 'packaged')

    def test_no_invoices_empty_export_and_all_excluded(self):
        run = self.make_run(status='no_applications', empty=True)
        result = delivery.deliver_result(run)
        self.assertEqual(set(self.names(result)[0]), {'exceptions.csv', 'result.json'})
        other = self.make_run(self.root / 'other', status='all_excluded')
        result = delivery.deliver_result(other)
        self.assertFalse(any('tax_template' in n for n in self.names(result)[0]))

    def test_partial_batch_includes_success_only_and_marks_other_shops(self):
        run = self.make_batch(['complete', 'failed', 'pending'])
        result = delivery.deliver_result(run)
        self.assertEqual(result['status'], 'not_configured')
        names, content = self.names(result)
        self.assertTrue(any(n.startswith('shop01_醉语阁/') for n in names))
        self.assertFalse(any(n.startswith(('shop02_', 'shop03_')) for n in names))
        self.assertNotIn(b'SECRET', content)
        with zipfile.ZipFile(result['zip_path']) as archive:
            report = json.loads(archive.read('result.json'))
        self.assertEqual([s['status'] for s in report['shops']], ['complete', 'failed', 'not_executed'])

    def test_tampered_file_rejected_without_network(self):
        run = self.make_run()
        (run / 'generated' / 'exceptions.csv').write_bytes(b'changed')
        with patch.object(delivery, '_request') as network:
            result = delivery.deliver_result(run, self.config)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'verification_failed')
        network.assert_not_called()

    def test_batch_child_state_tamper_rejected(self):
        run = self.make_batch(['complete'])
        state = run / 'shops' / 'shop01' / 'run-state.json'
        state.write_bytes(state.read_bytes() + b' ')
        self.assertEqual(delivery.deliver_result(run)['status'], 'failed')

    def test_outside_path_rejected(self):
        run = self.make_run()
        state = json.loads((run / 'run-state.json').read_text())
        state['generated_dir'] = str(self.root)
        state['stages']['generate']['outputs'].append(proof(self.config))
        write_json(run / 'run-state.json', state)
        self.assertEqual(delivery.deliver_result(run)['code'], 'path_outside_run')

    def test_upload_then_send_and_idempotency(self):
        run = self.make_run()
        with patch.object(delivery, '_request', side_effect=[{'errcode': 0, 'media_id': 'MEDIA'}, {'errcode': 0}]) as network:
            first = delivery.deliver_result(run, self.config)
            second = delivery.deliver_result(run, self.config)
        self.assertEqual(first['status'], 'sent')
        self.assertTrue(second['already_sent'])
        self.assertEqual(network.call_count, 2)
        upload, send = network.call_args_list
        self.assertIn('/upload_media?', upload.args[0])
        self.assertTrue(upload.args[0].endswith('&type=file'))
        self.assertIn(b'name="media"', upload.args[1])
        self.assertIn('醉语阁'.encode(), upload.args[1])
        self.assertEqual(json.loads(send.args[1]), {'msgtype': 'file', 'file': {'media_id': 'MEDIA'}})
        receipt = Path(first['receipt_path']).read_text(encoding='utf-8')
        self.assertNotIn('fake-test-key', receipt)
        self.assertNotIn('MEDIA', receipt)

    def test_explicit_upload_error_is_safe_to_retry(self):
        run = self.make_run()
        with patch.object(delivery, '_request', return_value={'errcode': 93000, 'errmsg': 'fake-test-key'}):
            first = delivery.deliver_result(run, self.config)
        self.assertEqual(first['code'], 'upload_rejected')
        self.assertNotIn('fake-test-key', json.dumps(first))
        with patch.object(delivery, '_request', side_effect=[{'errcode': 0, 'media_id': 'M'}, {'errcode': 0}]):
            self.assertEqual(delivery.deliver_result(run, self.config)['status'], 'sent')

    def test_receipts_and_atomic_temporary_files_fit_windows_paths(self):
        nested = self.root / ('x' * max(1, 125 - len(str(self.root)) - 1))
        run = self.make_run(nested)
        original_open = Path.open

        def windows_open(path, *args, **kwargs):
            if len(str(path)) >= 260:
                raise OSError('Windows path length limit')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', windows_open), patch.object(
                delivery, '_request', side_effect=[{'errcode': 0, 'media_id': 'M'}, {'errcode': 0}]) as network:
            first = delivery.deliver_result(run, self.config)
            again = delivery.deliver_result(run, self.config)
        self.assertEqual(first['status'], 'sent')
        self.assertTrue(again['already_sent'])
        self.assertEqual(network.call_count, 2)

    def test_send_timeout_stops_retries_until_explicit(self):
        run = self.make_run()
        with patch.object(delivery, '_request', side_effect=[{'errcode': 0, 'media_id': 'M'}, TimeoutError('fake-test-key')]):
            first = delivery.deliver_result(run, self.config)
        self.assertEqual(first['status'], 'unknown')
        self.assertNotIn('fake-test-key', json.dumps(first))
        with patch.object(delivery, '_request') as network:
            again = delivery.deliver_result(run, self.config)
        self.assertEqual(again['status'], 'unknown')
        network.assert_not_called()
        with patch.object(delivery, '_request', side_effect=[{'errcode': 0, 'media_id': 'M'}, {'errcode': 0}]):
            self.assertEqual(delivery.deliver_result(run, self.config, retry_unknown=True)['status'], 'sent')

    def test_sending_crash_checkpoint_does_not_duplicate(self):
        run = self.make_run()
        def request(url, body, content_type):
            if 'upload_media' in url:
                return {'errcode': 0, 'media_id': 'M'}
            receipts = list((run / 'delivery').glob('receipt-*.json'))
            self.assertEqual(json.loads(receipts[0].read_text(encoding='utf-8'))['status'], 'sending')
            raise KeyboardInterrupt()
        with patch.object(delivery, '_request', side_effect=request):
            with self.assertRaises(KeyboardInterrupt):
                delivery.deliver_result(run, self.config)
        with patch.object(delivery, '_request') as network:
            self.assertEqual(delivery.deliver_result(run, self.config)['status'], 'unknown')
            network.assert_not_called()

    def test_explicit_send_error(self):
        with patch.object(delivery, '_request', side_effect=[{'errcode': 0, 'media_id': 'M'}, {'errcode': 40058, 'errmsg': 'fake-test-key'}]):
            result = delivery.deliver_result(self.make_run(), self.config)
        self.assertEqual(result['code'], 'send_rejected')
        self.assertNotIn('fake-test-key', json.dumps(result))

    def test_size_limit_preserves_package_without_network(self):
        with patch.object(delivery, 'MAX_FILE_BYTES', 10), patch.object(delivery, '_request') as network:
            result = delivery.deliver_result(self.make_run(), self.config)
        self.assertEqual(result['code'], 'file_too_large')
        self.assertTrue(Path(result['zip_path']).is_file())
        network.assert_not_called()

    def test_bad_webhook_never_contacts_other_host(self):
        config = json.loads(self.config.read_text())
        config['wecom']['webhook_url'] = 'https://attacker.test/?key=fake-test-key'
        write_json(self.config, config)
        with patch.object(delivery, '_request') as network:
            result = delivery.deliver_result(self.make_run(), self.config)
        self.assertEqual(result['code'], 'configuration')
        network.assert_not_called()

    def test_redirects_are_not_followed(self):
        request = delivery.urllib.request.Request('https://qyapi.weixin.qq.com/')
        with self.assertRaises(urllib.error.HTTPError):
            delivery._NoRedirect().redirect_request(request, io.BytesIO(), 302, 'redirect', {}, 'https://other.test')


if __name__ == '__main__':
    unittest.main()
