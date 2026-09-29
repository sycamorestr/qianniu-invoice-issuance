"""Store policy changes rates without changing invoice source money or evidence."""

from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

import build_invoice_plan as rules
from run_invoice import assemble, verify_sources
from template_io import SHEETS, make_output_rows
from test_build_invoice_plan import valid_source, build, fee_row
from test_pipeline import fee_verification_fixture, fixture, jst_assembly_fixture, write


HERE = Path(__file__).parent


def verified_fixture(policy):
    source, _ = fee_verification_fixture()
    source['run'] = {'store_name': '测试店', 'tax_rate_policy': policy}
    source['template_rows'][0].update({'发票类型': '全电普通发票', '抬头类型': '个人'})
    source['order_goods'] = [{'order_no': 'O-1', 'goods_code': 'SKU-1'}]
    invoice = rules.build_invoice(
        rules.InvoiceBuild('A-1', [(row['__source_row'], row) for row in source['template_rows']]),
        rules.make_order_goods_index(source['order_goods']),
        rules.make_jst_index(source['jst_invoice_goods']), source['run'],
        rules.make_order_items_index(source['order_items']))
    return source, {'invoices': [invoice], 'tax_rate_policy': rules.normalize_policy(policy)}


class TaxPolicyPlannerTests(unittest.TestCase):
    def test_fixed_rates_override_missing_conflicting_and_invalid_rates_only(self):
        for fixed in ['0.1', '0']:
            for jst_rate in [None, '0.13', 'unknown']:
                with self.subTest(fixed=fixed, jst_rate=jst_rate):
                    source = valid_source()
                    source['run']['tax_rate_policy'] = {'source': 'fixed', 'rate': fixed}
                    source['jst_invoice_goods'][0].update(tax_rate=jst_rate, 虚拟分类='未知')
                    source['template_rows'][0].update({'开票总金额': '70.30', '税率': '13%'})
                    source['template_rows'][1]['税率'] = 'unknown'
                    source['template_rows'].append(fee_row(税率='9%'))
                    invoice = build(source)
                    self.assertEqual(invoice['errors'], [])
                    self.assertEqual(invoice['computed_total_amount'], '70.3')
                    line = invoice['detail_lines'][0]
                    self.assertEqual((line['tax_rate'], line['tax_rate_effective'], line['tax_rate_source']),
                                     (fixed, fixed, 'store_fixed'))
                    self.assertEqual((line['quantity'], line['original_amount'], line['extra_fee_amount'],
                                      line['amount'], line['discount_amount']), ('7', '69.3', '5', '74.3', '-4'))
                    plan = {'invoices': [invoice], 'selected_application_ids': ['A-1']}
                    self.assertEqual(make_output_rows(plan)[SHEETS[1]][0]['税率'], fixed)

    def test_fixed_policy_still_requires_goods_mapping_and_other_piaoju_fields(self):
        for field, value, expected in [('开票名称', '', 'item_name'), ('税收编码', '', 'tax_classification_code'),
                                       ('开票单位', '', 'unit'), ('是否开票', False, '标记为不开票')]:
            source = valid_source()
            source['run']['tax_rate_policy'] = {'source': 'fixed', 'rate': '0.1'}
            source['jst_invoice_goods'][0][field] = value
            self.assertTrue(any(expected in error for error in build(source)['errors']))
        source = valid_source()
        source['run']['tax_rate_policy'] = {'source': 'fixed', 'rate': '0.1'}
        source['jst_invoice_goods'] = []
        self.assertEqual(build(source)['status'], 'blocked')
        source = valid_source()
        source['run']['tax_rate_policy'] = {'source': 'fixed', 'rate': '0.1'}
        source['template_rows'][0]['开票总金额'] = '70.3'
        source['template_rows'].append(fee_row(商品编码='OTHER'))
        self.assertTrue(any('商品编码与归属商品不一致' in error for error in build(source)['errors']))

    def test_piaoju_policy_retains_missing_and_conflict_checks_for_exception_store(self):
        source = valid_source()
        source['run'].update(store_name='票聚税率示例店', tax_rate_policy={'source': 'piaoju'})
        self.assertEqual(build(source)['detail_lines'][0]['tax_rate'], '0.13')
        source['jst_invoice_goods'][0]['虚拟分类'] = ''
        self.assertEqual(build(source)['status'], 'blocked')
        for target, rate in [(0, '9%'), (1, '9%')]:
            source = valid_source()
            source['run']['tax_rate_policy'] = {'source': 'piaoju'}
            source['template_rows'][target]['税率'] = rate
            self.assertEqual(build(source)['status'], 'blocked')

    def test_invalid_policy_is_global_error_even_for_excluded_invoice(self):
        for policy in [{'source': 'other'}, {'source': 'fixed'}, {'source': 'fixed', 'rate': '13'},
                       {'source': 'fixed', 'rate': 'NaN'}, {'source': 'fixed', 'rate': '-0.1'}]:
            source = valid_source()
            source['run']['tax_rate_policy'] = policy
            source['template_rows'][0]['开票总金额'] = '-1'
            with self.assertRaises(ValueError):
                build(source)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            write(path, source)
            result = subprocess.run([sys.executable, '-X', 'utf8', str(HERE / 'build_invoice_plan.py'), str(path)],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)


class TaxPolicyVerificationTests(unittest.TestCase):
    def test_fixed_policy_verifies_without_piaoju_rate_and_keeps_money(self):
        for rate in ['0.1', '0']:
            source, plan = verified_fixture({'source': 'fixed', 'rate': rate})
            source['jst_invoice_goods'][0].pop('tax_rate')
            for row in source['template_rows']:
                row['税率'] = 'unrelated source value'
            self.assertEqual(verify_sources(source, plan), plan['invoices'])
            line = plan['invoices'][0]['detail_lines'][0]
            self.assertEqual((line['amount'], line['discount_amount'], line['quantity']), ('75.3', '-4', '7'))

    def test_independent_verifier_rejects_changed_policy_rate_and_provenance(self):
        source, plan = verified_fixture({'source': 'fixed', 'rate': '0.1'})
        for field, value, reason in [('tax_rate', '0.13', '税率不一致'), ('tax_rate', 0.1, '税率不一致'),
                                     ('tax_rate_source', 'jst_tax_rate', '税率来源不一致'),
                                     ('tax_rate_effective', '0.13', '有效税率不一致'),
                                     ('amount', '99', '商品金额与原金额加价外费用不一致')]:
            changed = deepcopy(plan)
            changed['invoices'][0]['detail_lines'][0][field] = value
            with self.assertRaisesRegex(ValueError, reason):
                verify_sources(source, changed)
        for policy in [{'source': 'fixed', 'rate': '0'}, {'source': 'piaoju'}]:
            changed = deepcopy(plan)
            changed['tax_rate_policy'] = policy
            with self.assertRaisesRegex(ValueError, '计划税率策略与输入不一致'):
                verify_sources(source, changed)
        changed = deepcopy(source)
        changed['run']['tax_rate_policy']['rate'] = 'Infinity'
        with self.assertRaises(ValueError):
            verify_sources(changed, plan)

    def test_fixed_independent_verifier_still_requires_piaoju_fields(self):
        source, plan = verified_fixture({'source': 'fixed', 'rate': '0.1'})
        for field, key in [('invoice_name', 'item_name'), ('tax_code', 'tax_classification_code'),
                           ('issuing_office', 'unit')]:
            changed_source, changed_plan = deepcopy(source), deepcopy(plan)
            changed_source['jst_invoice_goods'][0][field] = ''
            changed_plan['invoices'][0]['detail_lines'][0][key] = ''
            with self.assertRaisesRegex(ValueError, '票聚必需字段为空'):
                verify_sources(changed_source, changed_plan)

    def test_piaoju_verifier_rejects_forged_rate_and_source_conflicts(self):
        source, plan = verified_fixture({'source': 'piaoju'})
        self.assertEqual(verify_sources(source, plan), plan['invoices'])
        for index, reason in [(0, '商品源行税率不一致'), (1, '折扣税率不一致'), (2, '价外费用税率不一致')]:
            changed = deepcopy(source)
            changed['template_rows'][index]['税率'] = '9%'
            with self.assertRaisesRegex(ValueError, reason):
                verify_sources(changed, plan)
        for rate in ['1.3', 'NaN', 'Infinity']:
            changed = deepcopy(source)
            changed['jst_invoice_goods'][0]['tax_rate'] = rate
            with self.assertRaisesRegex(ValueError, '票聚税率必须'):
                verify_sources(changed, plan)
        changed = deepcopy(plan)
        changed['invoices'][0]['detail_lines'][0]['tax_rate_source'] = 'store_fixed'
        with self.assertRaisesRegex(ValueError, '税率来源不一致'):
            verify_sources(source, changed)

    def test_assembly_consumes_frozen_policy_without_rereading_current_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = jst_assembly_fixture(root, {'input_goods_code': 'SKU-1', 'ok': True,
                                              'exact_matches': [{'sku_id': 'SKU-1'}]})
            args.tax_rate_policy_file = root / 'policy.json'
            write(args.tax_rate_policy_file, {'source': 'fixed', 'rate': '0.100'})
            self.assertEqual(assemble(args)['run']['tax_rate_policy'], {'source': 'fixed', 'rate': '0.1'})
            args.tax_rate_config = root / 'another.json'
            with self.assertRaisesRegex(ValueError, '不能同时指定'):
                assemble(args)

    def test_local_cli_records_frozen_policy_in_manifest_input_and_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture(root / 'qianniu_common.xlsx', common=True)
            write(root / 'applications.json', {'date': '2026-01-01', 'total': 0, 'rows': [],
                                                'queried_at': '2026-01-02T00:00:00Z'})
            policy_file = root / 'frozen-policy.json'
            write(policy_file, {'source': 'fixed', 'rate': '0.1000'})
            output = root / 'output'
            command = [sys.executable, '-X', 'utf8', str(HERE / 'run_invoice.py'), '--date', '2026-01-01',
                       '--store', '测试店', '--issuer', '测试主体', '--input-dir', str(root),
                       '--output-dir', str(output), '--tax-rate-policy-file', str(policy_file), '--replay']
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            for file_name in ['run.json', 'invoice_plan.json', 'invoice_input.json']:
                saved = json.loads((output / file_name).read_text(encoding='utf-8'))
                if file_name == 'invoice_input.json':
                    saved = saved['run']
                self.assertEqual(saved['tax_rate_policy'], {'source': 'fixed', 'rate': '0.1'})
            result = subprocess.run(command + ['--tax-rate-config', str(root / 'config.json')],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 2)
            self.assertIn('not allowed with argument', result.stderr)


if __name__ == '__main__':
    unittest.main()
