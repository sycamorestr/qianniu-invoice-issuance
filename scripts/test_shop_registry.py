"""Read-only workbench integration, using synthetic temporary configs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from invoice_browser_config import INVOICE_URL, ORDERS_URL, with_invoice_pages
from manage_browsers import initialize, manage
from playwright_controller import BrowserControllerError, load_browser_config
from run_online import OnlineError, stable_sha256
from shop_registry import load_registry


HOME = 'https://myseller.taobao.com/'
GOODS = 'https://fp.erp321.com/setting/goodsManage'


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class InvoicePageConfigTests(unittest.TestCase):
    def test_home_becomes_business_roles_without_mutating_input(self):
        config = {'browser_sessions': {'home': {'url': HOME}}, 'user_data_dir': 'same-profile',
                  'remote_debugging_port': 9450, 'playwright': {'nested': ['kept']}}
        before = deepcopy(config)
        result = with_invoice_pages(config)
        self.assertEqual(result['browser_sessions'], {'invoice': {'url': INVOICE_URL}, 'orders': {'url': ORDERS_URL}})
        self.assertEqual(config, before)
        self.assertEqual(result['user_data_dir'], 'same-profile')
        self.assertEqual(result['remote_debugging_port'], 9450)
        result['playwright']['nested'].append('changed')
        self.assertEqual(config, before)

    def test_explicit_options_and_goods_are_retained(self):
        invoice = {'url': INVOICE_URL + '?custom=1', 'login_positive_selectors': ['.ready']}
        config = {'browser_sessions': {'home': HOME, 'invoice': invoice,
                                      'orders': ORDERS_URL + '?custom=2', 'goods': {'url': GOODS}}}
        result = with_invoice_pages(config)
        self.assertEqual(result['browser_sessions'], {key: value for key, value in config['browser_sessions'].items() if key != 'home'})
        result['browser_sessions']['invoice']['login_positive_selectors'].append('.new')
        self.assertEqual(invoice['login_positive_selectors'], ['.ready'])

    def test_nonhome_business_layouts_remain_identical_without_filling_roles(self):
        for roles in ({'goods': {'url': GOODS}}, {'invoice': INVOICE_URL},
                      {'invoice': INVOICE_URL, 'orders': ORDERS_URL, 'goods': GOODS}):
            config = {'schema_version': 1, 'browser_sessions': roles}
            result = with_invoice_pages(config)
            self.assertEqual(result, config)
            self.assertIsNot(result, config)

    def test_unknown_roles_and_non_qianniu_home_are_rejected(self):
        for config in ({}, {'browser_sessions': {}}, {'browser_sessions': {'unknown': HOME}},
                       {'browser_sessions': {'home': HOME, 'other': GOODS}},
                       {'browser_sessions': {'home': 'https://myseller.taobao.com.attacker.test/'}},
                       {'browser_sessions': {'home': {'url': GOODS}}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                with_invoice_pages(config)


class WorkbenchRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows runners may expose TEMP through an 8.3 alias.
        self.root = Path(self.temp.name).resolve()
        self.registry = self.root / 'shops.json'
        self.sidecar = self.root / '.browser-workbench-shops.json'
        self.config('piaoju', {'goods': {'url': GOODS}}, 9440)
        self.config('shop01', {'invoice': {'url': INVOICE_URL}, 'orders': {'url': ORDERS_URL}}, 9441)
        self.raw = {'schema_version': 1, 'issuer': 'Synthetic issuer', 'jst_browser_config': 'config/piaoju.json',
                    'output_root': 'outputs', 'shops': [{'id': 'shop01', 'store': 'Synthetic shop one',
                                                       'browser_config': 'config/shop01.json'}]}
        write(self.registry, self.raw)

    def config(self, sid, roles, port):
        value = {'schema_version': 1, 'browser': 'Edge', 'user_data_dir': f'../profiles/{sid}',
                 'download_dir': f'../downloads/{sid}', 'profile_directory': 'Default',
                 'remote_debugging_port': port, 'browser_sessions': roles}
        path = self.root / 'config' / f'{sid}.json'
        write(path, value)
        return path

    def custom(self, **changes):
        path = self.config('custom', {'home': {'url': HOME}}, 9442)
        record = {'id': 'custom', 'store': 'Synthetic custom shop', 'login_username': 'synthetic:operator',
                  'browser_config': str(path.relative_to(self.root)), **changes}
        write(self.sidecar, {'schema_version': 1, 'shops': [record]})
        return record, path

    def bytes_snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def test_legacy_identity_hash_is_unchanged(self):
        shared_path = self.root / 'config/piaoju.json'
        shop_path = self.root / 'config/shop01.json'
        shared, _ = load_browser_config(shared_path)
        shop, _ = load_browser_config(shop_path)
        original_identity = {'issuer': self.raw['issuer'], 'jst_browser_config': str(shared_path),
                             'shared_identity_sha256': stable_sha256(shared), 'shops': [
                                 {'id': 'shop01', 'store': 'Synthetic shop one', 'browser_config': str(shop_path),
                                  'browser_identity_sha256': stable_sha256(shop)}]}
        registry = load_registry(self.registry)
        self.assertEqual(registry['identity_sha256'], stable_sha256(original_identity))
        self.assertEqual(registry['shared_identity_sha256'], stable_sha256(shared))
        self.assertEqual(registry['shops'][0]['browser_identity_sha256'], stable_sha256(shop))

    def test_homeonly_sidecar_merges_without_writing_or_copying_configs(self):
        old = load_registry(self.registry)
        record, path = self.custom()
        before = self.bytes_snapshot()
        merged = load_registry(self.registry)
        self.assertEqual([shop['id'] for shop in merged['shops']], ['shop01', 'custom'])
        self.assertEqual(merged['shops'][1]['browser_config'], str(path))
        config, _ = load_browser_config(path)
        self.assertEqual(merged['shops'][1]['browser_identity_sha256'], stable_sha256(with_invoice_pages(config)))
        self.assertEqual(merged['shops'][0]['browser_identity_sha256'], old['shops'][0]['browser_identity_sha256'])
        self.assertEqual(merged['shared_identity_sha256'], old['shared_identity_sha256'])
        self.assertEqual(self.bytes_snapshot(), before)
        self.assertEqual(merged['shops'][1]['login_username'], 'synthetic:operator')
        record['login_username'] = 'another:operator'
        write(self.sidecar, {'schema_version': 1, 'shops': [record]})
        # Keep the legacy environment digest stable. Batch/online states bind
        # the expected account separately from browser environment identity.
        updated = load_registry(self.registry)
        self.assertEqual(updated['identity_sha256'], merged['identity_sha256'])
        self.assertEqual(updated['shops'][1]['login_username'], 'another:operator')

    def test_include_workbench_false_keeps_original_registry_even_with_invalid_sidecar(self):
        original = load_registry(self.registry)
        self.custom()
        self.assertEqual(load_registry(self.registry, include_workbench=False), original)
        self.sidecar.write_text('invalid json', encoding='utf-8')
        self.assertEqual(load_registry(self.registry, include_workbench=False), original)
        with self.assertRaises(OnlineError):
            load_registry(self.registry)

    def test_merged_duplicate_id_or_name_is_rejected(self):
        for changes in ({'id': 'shop01'}, {'store': 'Synthetic shop one'}, {'store': ' SYNTHETIC  SHOP ONE '}):
            with self.subTest(changes=changes):
                self.custom(**changes)
                with self.assertRaises(OnlineError):
                    load_registry(self.registry)

    def test_merged_profile_download_and_port_conflicts_are_rejected(self):
        _, path = self.custom()
        original = json.loads(path.read_text(encoding='utf-8'))
        for changes in ({'user_data_dir': '../profiles/shop01'},
                        {'user_data_dir': '../profiles/shop01/nested'},
                        {'user_data_dir': '../profiles', 'profile_directory': 'Another profile'},
                        {'remote_debugging_port': 9441}, {'remote_debugging_port': 9440},
                        {'download_dir': '../downloads/shop01'}):
            with self.subTest(changes=changes):
                write(path, {**original, **changes})
                with self.assertRaises(OnlineError):
                    load_registry(self.registry)

    def test_invalid_sidecar_schema_and_records_are_not_silently_ignored(self):
        for value in ([], {'schema_version': 2, 'shops': []}, {'schema_version': 1, 'shops': {}},
                      {'schema_version': 1, 'shops': [None]}, {'schema_version': 1, 'shops': [
                          {'id': 'bad', 'store': 'Bad', 'browser_config': 'config/shop01.json', 'login_username': 7}]}):
            with self.subTest(value=value):
                write(self.sidecar, value)
                with self.assertRaises(OnlineError):
                    load_registry(self.registry)

    def test_business_roles_without_home_are_not_implicitly_repaired(self):
        path = self.config('shop01', {'invoice': {'url': INVOICE_URL}}, 9441)
        before = path.read_bytes()
        with self.assertRaises(OnlineError):
            load_registry(self.registry)
        self.assertEqual(path.read_bytes(), before)

    def test_home_and_goods_are_not_accepted_as_a_shared_shop_environment(self):
        _, path = self.custom()
        config = json.loads(path.read_text(encoding='utf-8'))
        config['browser_sessions']['goods'] = {'url': GOODS}
        write(path, config)
        with self.assertRaises(OnlineError):
            load_registry(self.registry)

    def test_initialize_compares_only_original_shops_and_preserves_workbench_files(self):
        self.custom()
        inputs = self.root / 'shops-input.json'
        write(inputs, [{'id': 'shop01', 'store': 'Synthetic shop one'}])
        before = self.bytes_snapshot()
        result = initialize(self.root, inputs, 'Synthetic issuer')
        self.assertEqual(result['status'], 'already_initialized')
        self.assertEqual(result['shops'], 1)
        self.assertEqual(self.bytes_snapshot(), before)

    def test_manage_start_and_status_derive_business_pages_only_in_memory(self):
        _, custom_path = self.custom()
        before = self.bytes_snapshot()
        for command in ('start', 'status'):
            captured = []
            def controller(config, *, timeout_ms):
                instance = SimpleNamespace(start=AsyncMock(), connect=AsyncMock(), close=AsyncMock(),
                                           registrations={key: None for key in config['browser_sessions']})
                captured.append((config, instance))
                return instance
            with patch('manage_browsers.PlaywrightBrowserController', side_effect=controller):
                result = asyncio.run(manage(self.registry, command, ['custom', 'piaoju'], False, 100))
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(set(captured[0][0]['browser_sessions']), {'invoice', 'orders'})
            self.assertEqual(set(captured[1][0]['browser_sessions']), {'goods'})
            original, _ = load_browser_config(custom_path)
            self.assertEqual(captured[0][0]['user_data_dir'], original['user_data_dir'])
            for _, instance in captured:
                if command == 'start':
                    instance.start.assert_awaited_once_with(open_missing=True)
                    instance.connect.assert_not_awaited()
                else:
                    instance.connect.assert_awaited_once_with()
                    instance.start.assert_not_awaited()
                instance.close.assert_awaited_once()
        self.assertEqual(self.bytes_snapshot(), before)


if __name__ == '__main__':
    unittest.main()
