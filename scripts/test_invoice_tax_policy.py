"""Configuration routing and validation; no business accounts or browser."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from invoice_tax_policy import load_tax_rate_config, normalize_policy, resolve_tax_rate_policy


class TaxPolicyTests(unittest.TestCase):
    def test_default_and_exact_store_override(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'tax-rates.json'
            config.write_text(json.dumps({'schema_version': 1,
                'default': {'source': 'fixed', 'rate': '0.10'},
                'stores': {'例外旗舰店': {'source': 'piaoju'},
                           '免税店': {'source': 'fixed', 'rate': 0}}}), encoding='utf-8')
            self.assertEqual(resolve_tax_rate_policy('例外旗舰店', config), {'source': 'piaoju'})
            self.assertEqual(resolve_tax_rate_policy('免税店', config), {'source': 'fixed', 'rate': '0'})
            for name in ('其他店', '新店', '例外'):
                self.assertEqual(resolve_tax_rate_policy(name, config), {'source': 'fixed', 'rate': '0.1'})

    def test_absent_default_is_legacy_but_explicit_missing_is_error(self):
        with tempfile.TemporaryDirectory() as folder:
            absent = Path(folder) / 'missing.json'
            with patch('invoice_tax_policy.DEFAULT_CONFIG', absent):
                self.assertEqual(resolve_tax_rate_policy('测试店'), {'source': 'piaoju'})
                with self.assertRaises(ValueError):
                    resolve_tax_rate_policy('测试店', absent)

    def test_invalid_rules_fail_closed(self):
        invalid = [True, '', {}, {'source': 'unknown'}, {'source': 'fixed'},
                   {'source': 'piaoju', 'rate': '0.1'}, {'source': 'fixed', 'rate': None}]
        invalid += [{'source': 'fixed', 'rate': value} for value in
                    (True, -0.1, 10, '10%', 'NaN', float('inf'), '1.01', '', 'zero')]
        for rule in invalid:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                normalize_policy(rule)
        self.assertEqual(normalize_policy(None), {'source': 'piaoju'})
        self.assertEqual(normalize_policy({'source': 'fixed', 'rate': 0.1}), {'source': 'fixed', 'rate': '0.1'})

    def test_invalid_config_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tax-rates.json'
            valid = {'schema_version': 1, 'default': {'source': 'piaoju'}, 'stores': {}}
            invalid = [{}, [], {**valid, 'schema_version': True}, {**valid, 'default': None},
                       {**valid, 'stores': {' typo ': {'source': 'piaoju'}}},
                       {**valid, 'stores': {'示例店': None}}, {**valid, 'extra': True}]
            for content in invalid:
                path.write_text(json.dumps(content), encoding='utf-8')
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_tax_rate_config(path)
            path.write_text('{"schema_version":1,"default":{"source":"piaoju"},"stores":{},"stores":{}}', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_tax_rate_config(path)


if __name__ == '__main__':
    unittest.main()
