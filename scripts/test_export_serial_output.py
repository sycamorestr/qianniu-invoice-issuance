"""Order-number export joins must not change platform application identity."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as ET

import run_invoice
from template_io import (Q, SHEETS, finalize, inspect_template, make_output_rows,
                         read_rows)
from test_pipeline import fixture


ORDER_19 = '1234567890123456789'
ORDER_20 = '00012345678901234567'


def export_fixture(applications=None):
    """Two product rows per application model the basic-to-detail join."""
    applications = applications or [('A-1', ORDER_19), ('A-2', ORDER_20)]
    source = {'template_rows': []}
    plan = {'fatal': False, 'errors': [], 'selected_application_ids': [],
            'invoices': []}
    for application, order in applications:
        plan['selected_application_ids'].append(application)
        lines = []
        for index, (amount, discount) in enumerate([('10.00', '-2.00'), ('20.00', '0')]):
            source_row = len(source['template_rows']) + 2
            source['template_rows'].append({
                '__source_row': source_row, '申请流水号': application,
                '订单编号': order, '开票状态': '待处理', '商品金额': amount,
                '开票总金额': '28.00', '数量': '1',
            })
            lines.append({
                'invoice_serial_no': application, 'order_no': order,
                'source_row': source_row, 'goods_code': f'SKU-{index}',
                'item_name': f'商品{index}', 'tax_classification_code': '101',
                'specification': f'规格{index}', 'unit': '袋', 'quantity': '1',
                'amount': amount, 'tax_rate': '0.01', 'discount_amount': discount,
            })
        plan['invoices'].append({
            'invoice_serial_no': application, 'invoice_total_amount': '28.00',
            'errors': [], 'status': 'ready_for_export',
            'basic': {'invoice_title': f'购方{application}',
                      'invoice_type': '普通发票', 'tax_included': '是'},
            'detail_lines': lines,
        })
    return source, plan


def write_authored_rows(template, authored, schema, expected):
    """Supply a synthetic authoring workbook to the real XLSX finalizer."""
    by_path = {meta['path']: name for name, meta in schema.items()}
    with ZipFile(template) as original, ZipFile(authored, 'w', ZIP_DEFLATED) as out:
        for entry in original.infolist():
            raw = original.read(entry.filename)
            if entry.filename in by_path:
                name = by_path[entry.filename]
                meta = schema[name]
                xml = ET.fromstring(raw)
                data = xml.find(Q('sheetData'))
                for rn, values in enumerate(expected[name], meta['header_row'] + 1):
                    row = ET.SubElement(data, Q('row'), r=str(rn))
                    for label, value in values.items():
                        if value == '':
                            continue
                        cell = ET.SubElement(row, Q('c'),
                                             r=meta['columns'][label] + str(rn),
                                             t='inlineStr')
                        ET.SubElement(ET.SubElement(cell, Q('is')), Q('t')).text = str(value)
                raw = ET.tostring(xml)
            out.writestr(entry, raw)


def collision_input_fixture(root):
    """Real replay inputs: two otherwise valid applications for one order."""
    inputs = root / 'inputs'
    inputs.mkdir()
    fixture(inputs / 'qianniu_common.xlsx', common=True, common_rows=[
        {'申请流水号': application, '订单编号': ORDER_19, '开票总金额': '10.00',
         '商品金额': '10.00', '数量': '1', '发票类型': '全电普通发票',
         '抬头类型': '个人', '发票抬头': '测试购方', '开票状态': '待处理'}
        for application in ['A-1', 'A-2']
    ])
    # The shared minimal fixture omits merchandise titles; a complete positive
    # replay needs that independently verified field in its common export.
    titled = inputs / 'common-with-title.xlsx'
    with ZipFile(inputs / 'qianniu_common.xlsx') as original, ZipFile(titled, 'w', ZIP_DEFLATED) as out:
        for entry in original.infolist():
            raw = original.read(entry.filename)
            if entry.filename == 'xl/worksheets/custom30.xml':
                xml = ET.fromstring(raw)
                for row in xml.find(Q('sheetData')):
                    rn = row.get('r')
                    cell = ET.SubElement(row, Q('c'), r='K' + rn, t='inlineStr')
                    ET.SubElement(ET.SubElement(cell, Q('is')), Q('t')).text = (
                        '货物名称' if rn == '1' else '测试商品')
                raw = ET.tostring(xml)
            out.writestr(entry, raw)
    titled.replace(inputs / 'qianniu_common.xlsx')
    records = {
        'applications.json': {
            'date': '2026-01-01', 'queried_at': '2026-01-02T00:00:00Z',
            'total': 2, 'api_total': 2, 'observed_total': 2,
            'rows': [{'serialNo': application, 'tid': ORDER_19} for application in ['A-1', 'A-2']],
            'list_non_pending_snapshot_rows': [],
        },
        'order_batches.json': {
            'batches': [{'ids': [ORDER_19], 'pageNum': 1,
                         'page': {'totalNumber': 1, 'totalPage': 1}, 'order_ids': [ORDER_19]}],
            'items': [{'order_no': ORDER_19, 'sub_order_no': 'SUB-1',
                       'goods_code': 'SKU-1', 'title': '测试商品', 'quantity': '1'}],
        },
        'jst_query.json': {
            'data': [{'input_goods_code': 'SKU-1', 'ok': True,
                      'exact_matches': [{'sku_id': 'SKU-1', 'invoice_enabled': True,
                                         'invoice_name': '测试商品', 'tax_code': '101',
                                         'properties_value': '250g', 'issuing_office': '袋',
                                         'tax_rate': '0.13'}]}],
        },
    }
    for name, data in records.items():
        (inputs / name).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    policy = root / 'isolated-tax-policy.json'
    policy.write_text(json.dumps({'source': 'piaoju'}), encoding='utf-8')
    template = root / 'template.xlsx'
    fixture(template, clean=True)
    return [
        'run_invoice.py', '--date', '2026-01-01', '--store', 'test', '--issuer', 'test',
        '--input-dir', str(inputs), '--replay', '--template', str(template),
        '--tax-rate-policy-file', str(policy),
        '--node', str(root / 'unused-node.exe'), '--node-modules', str(root / 'unused-modules'),
    ]


class OrderSerialMappingTests(unittest.TestCase):
    def test_two_product_rows_share_the_order_number_without_mutating_plan_or_source(self):
        source, plan = export_fixture()
        old_source, old_plan = deepcopy(source), deepcopy(plan)
        mapping = run_invoice.order_serial_mapping(source, plan)
        self.assertEqual(mapping, {'A-1': ORDER_19, 'A-2': ORDER_20})
        self.assertEqual(source, old_source)
        self.assertEqual(plan, old_plan)

    def test_source_order_whitespace_is_trimmed_without_losing_leading_zeroes(self):
        source, plan = export_fixture([('A-1', '  ' + ORDER_20 + '\t')])
        self.assertEqual(run_invoice.order_serial_mapping(source, plan), {'A-1': ORDER_20})

    def test_each_source_row_requires_an_original_text_order_number(self):
        for value in [None, '', ' \t', int(ORDER_19), float(ORDER_19), True]:
            with self.subTest(value=value):
                source, plan = export_fixture([('A-1', ORDER_19)])
                source['template_rows'][1]['订单编号'] = value
                before = deepcopy(plan)
                with self.assertRaises(ValueError):
                    run_invoice.order_serial_mapping(source, plan)
                self.assertEqual(plan, before)
        source, plan = export_fixture([('A-1', ORDER_19)])
        del source['template_rows'][1]['订单编号']
        with self.assertRaises(ValueError):
            run_invoice.order_serial_mapping(source, plan)

    def test_ready_application_without_any_source_rows_is_rejected(self):
        source, plan = export_fixture([('A-1', ORDER_19)])
        source['template_rows'] = []
        with self.assertRaises(ValueError):
            run_invoice.order_serial_mapping(source, plan)

    def test_multiple_orders_under_one_application_cannot_choose_an_arbitrary_order(self):
        source, plan = export_fixture([('A-1', ORDER_19)])
        source['template_rows'][1]['订单编号'] = ORDER_20
        with self.assertRaises(ValueError):
            run_invoice.order_serial_mapping(source, plan)

    def test_over_twenty_characters_is_rejected_even_when_all_rows_agree(self):
        source, plan = export_fixture([('A-1', '1' * 21)])
        with self.assertRaises(ValueError):
            run_invoice.order_serial_mapping(source, plan)

    def test_two_output_applications_cannot_share_an_order_number(self):
        source, plan = export_fixture([('A-1', ORDER_19), ('A-2', ORDER_19)])
        with self.assertRaises(ValueError):
            run_invoice.order_serial_mapping(source, plan)

    def test_cancelled_unselected_and_nonexported_applications_do_not_create_collisions(self):
        source, plan = export_fixture([
            ('A-1', ORDER_19), ('A-blocked', ORDER_19),
            ('A-excluded', None), ('A-negative', ORDER_19),
        ])
        plan['invoices'][1].update(status='blocked', errors=['缺少开票单位'])
        plan['invoices'][2].update(status='excluded_negative', invoice_total_amount='-28.00',
                                   detail_lines=[])
        plan['invoices'][3]['invoice_total_amount'] = '-28.00'
        source['template_rows'].extend([
            {'申请流水号': 'A-old-cancelled', '订单编号': ORDER_19, '开票状态': '已取消'},
            {'申请流水号': 'A-unselected', '订单编号': ORDER_19, '开票状态': '待处理'},
        ])
        mapping = run_invoice.order_serial_mapping(source, plan)
        self.assertEqual(mapping, {'A-1': ORDER_19})
        rows = make_output_rows(plan, serial_mapping=mapping)
        self.assertEqual(len(rows[SHEETS[0]]), 1)
        self.assertEqual(len(rows[SHEETS[1]]), 2)

    def test_same_order_in_separate_batches_does_not_become_cross_batch_deduplication(self):
        for application in ['A-cancelled-earlier', 'A-reapplied-later']:
            with self.subTest(application=application):
                source, plan = export_fixture([(application, ORDER_19)])
                mapping = run_invoice.order_serial_mapping(source, plan)
                self.assertEqual(mapping, {application: ORDER_19})
                self.assertEqual(make_output_rows(plan, serial_mapping=mapping)[SHEETS[0]][0]
                                 ['发票流水号'], ORDER_19)

    def test_no_ready_invoices_needs_no_order_number(self):
        source, plan = export_fixture([('A-1', None)])
        plan['invoices'][0].update(status='blocked', errors=['商品缺资料'])
        self.assertEqual(run_invoice.order_serial_mapping(source, plan), {})
        self.assertEqual(make_output_rows(plan, serial_mapping={}), {name: [] for name in SHEETS})


class ExportSerialRowsTests(unittest.TestCase):
    def test_only_serial_columns_change_and_both_sheets_keep_the_same_join(self):
        source, plan = export_fixture()
        before = deepcopy(plan)
        legacy = make_output_rows(plan)
        mapping = run_invoice.order_serial_mapping(source, plan)
        rows = make_output_rows(plan, serial_mapping=mapping)
        expected = deepcopy(legacy)
        for name in SHEETS[:2]:
            for row in expected[name]:
                row['发票流水号'] = mapping[row['发票流水号']]
        self.assertEqual(rows, expected)
        self.assertEqual(plan, before)
        self.assertEqual(rows[SHEETS[2]], [])
        self.assertEqual(rows[SHEETS[3]], [])
        self.assertEqual([r['发票流水号'] for r in rows[SHEETS[1]]],
                         [ORDER_19, ORDER_19, ORDER_20, ORDER_20])

    def test_legacy_without_mapping_keeps_application_ids_in_both_sheets(self):
        _, plan = export_fixture([('A-1', ORDER_19)])
        del plan['invoices'][0]['status']
        for mapping_call in [lambda: make_output_rows(plan),
                             lambda: make_output_rows(plan, serial_mapping=None)]:
            rows = mapping_call()
            self.assertEqual(rows[SHEETS[0]][0]['发票流水号'], 'A-1')
            self.assertEqual([r['发票流水号'] for r in rows[SHEETS[1]]], ['A-1', 'A-1'])

    def test_mapping_must_cover_exactly_the_exported_applications(self):
        _, plan = export_fixture()
        for mapping in [{}, {'A-1': ORDER_19},
                        {'A-1': ORDER_19, 'A-2': ORDER_20, 'A-unused': '123'}]:
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                make_output_rows(plan, serial_mapping=mapping)

    def test_direct_mapping_rejects_invalid_serials_and_collisions(self):
        _, plan = export_fixture()
        for value in ['', ' \t', None, 123, 1.25, True, '1' * 21,
                      ' ' + ORDER_19, ORDER_19 + ' ', ORDER_20]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                make_output_rows(plan, serial_mapping={'A-1': value, 'A-2': ORDER_20})

    def test_serial_mapping_does_not_bypass_global_or_application_completeness_gates(self):
        _, base = export_fixture([('A-1', ORDER_19)])
        for mutate in [lambda p: p.update(fatal=True),
                       lambda p: p.update(errors=['原始数据损坏']),
                       lambda p: p['selected_application_ids'].append('A-missing')]:
            with self.subTest(mutate=mutate):
                plan = deepcopy(base)
                mutate(plan)
                with self.assertRaises(ValueError):
                    make_output_rows(plan, serial_mapping={'A-1': ORDER_19})

    def test_xlsx_preserves_long_orders_and_leading_zeroes_as_text_in_both_sheets(self):
        source, plan = export_fixture()
        expected = make_output_rows(plan, serial_mapping=run_invoice.order_serial_mapping(source, plan))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            template, authored, output = [root / name for name in
                                          ['template.xlsx', 'authored.xlsx', 'output.xlsx']]
            fixture(template, clean=True)
            schema = inspect_template(template)
            write_authored_rows(template, authored, schema, expected)
            finalize(template, authored, output, schema)
            run_invoice.verify_workbook(template, output, schema, expected)
            with ZipFile(output) as workbook:
                for name in SHEETS[:2]:
                    meta = schema[name]
                    column = meta['columns']['发票流水号']
                    actual = [values[column] for rn, values in read_rows(output, name)
                              if rn > meta['header_row'] and values.get(column)]
                    wanted = [row['发票流水号'] for row in expected[name]]
                    self.assertEqual(actual, wanted)
                    xml = ET.fromstring(workbook.read(meta['path']))
                    cells = {cell.get('r'): cell for cell in xml.iter(Q('c'))}
                    for offset, value in enumerate(wanted, 1):
                        cell = cells[column + str(meta['header_row'] + offset)]
                        self.assertEqual(cell.get('t'), 'inlineStr')
                        self.assertEqual(cell.find(Q('is')).find(Q('t')).text, value)


class ExportSerialMainTests(unittest.TestCase):
    def assert_original_business_artifacts_preserved(self, root, output):
        self.assertEqual((output / 'qianniu_common_2026-01-01.xlsx').read_bytes(),
                         (root / 'inputs' / 'qianniu_common.xlsx').read_bytes())
        self.assertTrue((output / 'invoice_input.json').is_file())
        self.assertEqual((output / 'exceptions.csv').read_text(encoding='utf-8-sig').strip(),
                         '申请流水号,金额,暂缓原因')
        plan = json.loads((output / 'invoice_plan.json').read_text(encoding='utf-8'))
        self.assertEqual(plan['selected_application_ids'], ['A-1', 'A-2'])
        self.assertEqual([invoice['invoice_serial_no'] for invoice in plan['invoices']], ['A-1', 'A-2'])
        self.assertTrue(all(not invoice['errors'] for invoice in plan['invoices']))
        self.assertTrue(all(invoice['status'] == 'ready_for_export' for invoice in plan['invoices']))
        for invoice in plan['invoices']:
            self.assertTrue(all(line['invoice_serial_no'] == invoice['invoice_serial_no']
                                for line in invoice['detail_lines']))
        self.assertFalse((output / 'qianniu_invoice_tax_template_2026-01-01.xlsx').exists())
        self.assertFalse((output / 'invoice.pending.xlsx').exists())
        self.assertFalse((output / 'payload.json').exists())
        return plan

    def test_plan_only_with_duplicate_output_orders_preserves_business_plan_and_skips_serial_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'plan-only'
            argv = collision_input_fixture(root) + ['--output-dir', str(output), '--plan-only']
            with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()), \
                    patch.object(run_invoice, 'order_serial_mapping', wraps=run_invoice.order_serial_mapping) as mapping:
                self.assertEqual(run_invoice.main(), 0)
                mapping.assert_not_called()
            plan = self.assert_original_business_artifacts_preserved(root, output)
            manifest = json.loads((output / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'plan_only')
            self.assertEqual(manifest['ready_count'], 2)
            self.assertEqual(manifest['export_serial_source'], 'order_no')
            source = json.loads((output / 'invoice_input.json').read_text(encoding='utf-8'))
            with self.assertRaises(ValueError):
                run_invoice.order_serial_mapping(source, plan)

    def test_actual_export_rejects_duplicate_orders_after_preserving_business_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'export'
            argv = collision_input_fixture(root) + ['--output-dir', str(output)]
            with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()), \
                    patch.object(run_invoice, 'order_serial_mapping', wraps=run_invoice.order_serial_mapping) as mapping:
                with self.assertRaises(ValueError):
                    run_invoice.main()
                mapping.assert_called_once()
            self.assert_original_business_artifacts_preserved(root, output)
            manifest = json.loads((output / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual(manifest['ready_count'], 2)
            self.assertEqual(manifest['blocked_count'], 0)
            self.assertEqual(manifest['export_serial_source'], 'order_no')
            self.assertIn('error', manifest)
            self.assertFalse((output / 'authored.xlsx').exists())


if __name__ == '__main__':
    unittest.main()
