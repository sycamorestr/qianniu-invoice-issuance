"""Offline scope and workbook checks using synthetic multi-date inputs."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as ET

import run_invoice
import collection_files
from invoice_scope import scope_fields
from template_io import Q, SHEETS, inspect_template, read_rows, sheet_paths
from test_pipeline import fixture


ALL = {'mode': 'all_pending', 'start_date': '2025-12-20', 'end_date': '2026-02-20'}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def common_fixture(path, rows):
    fixture(path, common=True, common_rows=rows)
    # Add the real date/title columns to the synthetic common workbook.
    # This is a small XML test double, not the production authoring path.
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    part = 'xl/worksheets/custom30.xml'
    root = ET.fromstring(parts[part])
    for index, row in enumerate(root.find(Q('sheetData'))):
        for column, label in (('K', '申请时间'), ('L', '货物名称')):
            text = label if index == 0 else rows[index - 1].get(label, '')
            cell = ET.SubElement(row, Q('c'), r=column + row.get('r'), t='inlineStr')
            ET.SubElement(ET.SubElement(cell, Q('is')), Q('t')).text = text
    parts[part] = ET.tostring(root)
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def inputs(root, *, scope=ALL, rows=None):
    records = rows if rows is not None else [
        ('A-1', '2026-01-01', '10.00', '待处理'),
        ('A-2', '2026-02-15', '20.00', '待处理'),
        ('A-negative', '2026-01-20', '-5.00', '待处理'),
        ('A-ignored', '2026-02-10', '50.00', '已准'),
    ]
    common_rows = [{'申请流水号': key, '订单编号': 'O-' + key, '开票总金额': amount,
                    '商品金额': amount, '数量': '-1' if amount.startswith('-') else '1',
                    '发票类型': '全电普通发票', '抬头类型': '个人', '发票抬头': '合成购方',
                    '开票状态': status, '申请时间': date, '货物名称': '合成商品-' + key}
                   for key, date, amount, status in records]
    common_fixture(root / 'qianniu_common.xlsx', common_rows)
    binding = {'date': scope.get('date'), **scope_fields(scope)}
    write(root / 'capture_context.json', {**binding, 'store': 'Synthetic shop', 'issuer': 'Synthetic issuer',
          'verified_at': '2026-02-20T00:00:00Z', 'invoice_url': 'https://myseller.taobao.com/',
          'jst_url': 'https://fp.erp321.com/'})
    write(root / 'applications.json', {**binding, 'queried_at': '2026-02-20T00:00:00Z',
          'total': len(records), 'api_total': len(records), 'observed_total': len(records),
          'rows': [{'serialNo': key, 'tid': 'O-' + key, 'applyTime': date} for key, date, _, _ in records],
          'list_non_pending_snapshot_rows': []})
    active = [row for row in common_rows if row['开票状态'] == '待处理' and not row['开票总金额'].startswith('-')]
    orders = [row['订单编号'] for row in active]
    write(root / 'order_batches.json', {'batches': [{'ids': orders, 'pageNum': 1,
          'page': {'totalNumber': len(orders), 'totalPage': 1}, 'order_ids': orders}] if orders else [],
          'items': [{'order_no': row['订单编号'], 'sub_order_no': 'S-' + row['申请流水号'],
                     'goods_code': 'SKU-' + row['申请流水号'], 'title': row['货物名称'], 'quantity': '1'}
                    for row in active]})
    write(root / 'jst_query.json', {'data': [{'input_goods_code': 'SKU-' + row['申请流水号'], 'ok': True,
          'exact_matches': [{'sku_id': 'SKU-' + row['申请流水号'], 'invoice_enabled': True,
                            'invoice_name': row['货物名称'], 'tax_code': '1010101010000000000',
                            'properties_value': '', 'issuing_office': '件',
                            'tax_rate': '0' if row['申请流水号'] == 'A-1' else '0.13'}]}
          for row in active]})


def fake_author(light, payload_path, output):
    payload = json.loads(Path(payload_path).read_text(encoding='utf-8'))
    with ZipFile(light) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    for name, schema in payload['schema'].items():
        root = ET.fromstring(parts[schema['path']])
        data = root.find(Q('sheetData'))
        for index, values in enumerate(payload['rows'][name], schema['header_row'] + 1):
            row = ET.SubElement(data, Q('row'), r=str(index))
            for label, value in values.items():
                if value in ('', None):
                    continue
                cell = ET.SubElement(row, Q('c'), r=schema['columns'][label] + str(index), t='inlineStr')
                ET.SubElement(ET.SubElement(cell, Q('is')), Q('t')).text = str(value)
        parts[schema['path']] = ET.tostring(root)
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


class InvoiceScopePipelineTests(unittest.TestCase):
    def collect_orders(self, root):
        with patch.object(sys, 'argv', ['collection_files.py', 'orders', '--run-dir', str(root)]), \
                redirect_stdout(StringIO()):
            collection_files.main()

    def run_cli(self, root, output, scope_args, *, plan_only=False, replay=False, render=False):
        argv = ['run_invoice.py', *scope_args, '--store', 'Synthetic shop', '--issuer', 'Synthetic issuer',
                '--input-dir', str(root), '--output-dir', str(output)]
        if plan_only:
            argv.append('--plan-only')
        if replay:
            argv.append('--replay')
        if render:
            argv += ['--node', 'synthetic-node', '--node-modules', str(root / 'synthetic-modules')]
        real_run = subprocess.run
        def execute(command, **kwargs):
            if len(command) > 1 and Path(command[1]).name == 'render_invoice_template.mjs':
                fake_author(*command[2:5])
                return SimpleNamespace(returncode=0)
            return real_run(command, **kwargs)
        with patch.object(sys, 'argv', argv), patch.object(run_invoice.subprocess, 'run', side_effect=execute), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return run_invoice.main()

    def test_all_pending_cross_dates_preserves_selection_negative_and_zero_tax(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root)
            original = (root / 'qianniu_common.xlsx').read_bytes()
            output = root / 'result'
            self.assertEqual(self.run_cli(root, output, ['--all-pending'], render=True), 0)
            manifest = run_invoice.load(output / 'run.json')
            plan = run_invoice.load(output / 'invoice_plan.json')
            source = run_invoice.load(output / 'invoice_input.json')
            self.assertEqual(manifest['status'], 'complete')
            self.assertIsNone(manifest['date'])
            for record in (manifest, plan, source['run']):
                self.assertEqual(record['query_scope'], ALL)
            self.assertEqual(plan['apply_date_range'], {'start': '2025-12-20', 'end': '2026-02-20'})
            self.assertEqual(source['run']['apply_date_range'], plan['apply_date_range'])
            self.assertEqual({row['申请时间'] for row in source['template_rows']},
                             {'2026-01-01', '2026-02-15', '2026-01-20'})
            self.assertEqual((manifest['selected_count'], manifest['ready_count'], manifest['blocked_count'], manifest['excluded_count']), (3, 2, 0, 1))
            self.assertEqual(manifest['ready_amount'], '30')
            self.assertEqual(manifest['excluded_amount'], '-5')
            self.assertEqual(source['selection']['ignored_template_rows'][0]['serialNo'], 'A-ignored')
            self.assertEqual({item['order_no'] for item in source['order_items']}, {'O-A-1', 'O-A-2'})
            self.assertEqual((output / 'qianniu_common_all-pending.xlsx').read_bytes(), original)
            tax = output / 'qianniu_invoice_tax_template_all-pending.xlsx'
            self.assertEqual(Path(manifest['output']), tax.resolve())
            schema = inspect_template(tax)
            for name in SHEETS[:2]:
                meta = schema[name]
                records = [row for rn, row in read_rows(tax, name) if rn > meta['header_row'] and any(row.values())]
                self.assertEqual({row[meta['columns']['发票流水号']] for row in records}, {'A-1', 'A-2'})
            with ZipFile(tax) as archive:
                meta = schema[SHEETS[1]]
                data = ET.fromstring(archive.read(meta['path'])).find(Q('sheetData'))
                zero = data.find(Q('row') + "[@r='" + str(meta['header_row'] + 1) + "']/" + Q('c') + "[@r='" + meta['columns']['税率'] + str(meta['header_row'] + 1) + "']")
                self.assertEqual(zero.get('t'), 'inlineStr')
                self.assertEqual(''.join(zero.find(Q('is')).itertext()), '0')
                self.assertEqual([name for name, (_, state) in sheet_paths(archive).items() if state == 'visible'], list(SHEETS))

    def test_all_pending_empty_and_negative_only_preserve_original_without_tax_file(self):
        for records, status in (([], 'no_applications'),
                                ([('A-negative', '2026-01-20', '-5.00', '待处理')], 'all_excluded')):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                inputs(root, rows=records)
                (root / 'order_batches.json').unlink()
                (root / 'jst_query.json').unlink()
                output = root / 'result'
                self.run_cli(root, output, ['--all-pending'])
                self.assertEqual(run_invoice.load(output / 'run.json')['status'], status)
                for path in ('invoice_plan.json', 'invoice_input.json'):
                    value = run_invoice.load(output / path)
                    self.assertEqual((value['run'] if path == 'invoice_input.json' else value)['query_scope'], ALL)
                self.assertEqual((output / 'qianniu_common_all-pending.xlsx').read_bytes(), (root / 'qianniu_common.xlsx').read_bytes())
                self.assertFalse((output / 'qianniu_invoice_tax_template_all-pending.xlsx').exists())

    def test_mixed_context_or_application_scopes_fail_even_for_replay(self):
        for name in ('capture_context.json', 'applications.json'):
            for replay in (False, True):
                with self.subTest(name=name, replay=replay), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    inputs(root)
                    value = run_invoice.load(root / name)
                    value.pop('query_scope')
                    value['date'] = '2026-01-01'
                    write(root / name, value)
                    output = root / 'result'
                    with self.assertRaises(ValueError):
                        self.run_cli(root, output, ['--all-pending'], replay=replay, plan_only=True)
                    self.assertEqual(run_invoice.load(output / 'run.json')['status'], 'failed')
                    self.assertEqual((output / 'qianniu_common_all-pending.xlsx').read_bytes(), (root / 'qianniu_common.xlsx').read_bytes())
                    self.assertFalse((output / 'invoice_plan.json').exists())

    def test_dated_inputs_keep_legacy_shape_and_names_without_all_pending_attribute(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root, scope={'mode': 'date', 'date': '2026-01-01'}, rows=[])
            source = run_invoice.assemble(SimpleNamespace(input_dir=root, output_dir=root / 'unused',
                date='2026-01-01', replay=False, store='Synthetic shop', issuer='Synthetic issuer'))
            self.assertNotIn('query_scope', source['run'])
            output = root / 'result'
            self.run_cli(root, output, ['--date', '2026-01-01'])
            self.assertTrue((output / 'qianniu_common_2026-01-01.xlsx').exists())
            self.assertNotIn('query_scope', run_invoice.load(output / 'run.json'))
            self.assertNotIn('query_scope', run_invoice.load(output / 'invoice_plan.json'))

    def test_countdown_filter_survives_dated_and_all_pending_builds(self):
        for scope, options in (({'mode': 'date', 'date': '2026-01-01', 'countdown': 'started'},
                                ['--date', '2026-01-01']),
                               ({**ALL, 'countdown': 'started'}, ['--all-pending'])):
            for records, expected in (([], 'no_applications'),
                                      ([('A-negative', '2026-01-01', '-5.00', '待处理')], 'all_excluded'),
                                      (None, 'complete')):
                with self.subTest(scope=scope, status=expected), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    inputs(root, scope=scope, rows=records)
                    self.collect_orders(root)
                    before = (root / 'selection.json').read_bytes()
                    self.collect_orders(root)
                    self.assertEqual((root / 'selection.json').read_bytes(), before)
                    selection = run_invoice.load(root / 'selection.json')
                    self.assertEqual(selection['query_scope'], scope)
                    orders = run_invoice.load(root / 'order_ids.json')
                    self.assertEqual(orders['selection_sha256'], run_invoice.sha(root / 'selection.json'))
                    output = root / 'result'
                    self.run_cli(root, output, options, render=records is None)
                    manifest = run_invoice.load(output / 'run.json')
                    plan = run_invoice.load(output / 'invoice_plan.json')
                    source = run_invoice.load(output / 'invoice_input.json')
                    self.assertEqual(manifest['status'], expected)
                    for record in (manifest, plan, source['run'], source['selection']):
                        self.assertEqual(record['query_scope'], scope)

    def test_legacy_selection_bytes_remain_unchanged_on_collection_and_build(self):
        for scope, options in (({'mode': 'date', 'date': '2026-01-01'}, ['--date', '2026-01-01']),
                               (ALL, ['--all-pending'])):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                inputs(root, scope=scope)
                rows = run_invoice.read_source(root / 'qianniu_common.xlsx')
                expected = run_invoice.select_template_rows(rows, root / 'qianniu_common.xlsx')[2]
                run_invoice.save(root / 'selection.json', expected)
                before = (root / 'selection.json').read_bytes()
                self.collect_orders(root)
                self.assertEqual((root / 'selection.json').read_bytes(), before)
                self.assertNotIn('query_scope', run_invoice.load(root / 'selection.json'))
                output = root / 'result'
                self.run_cli(root, output, options, plan_only=True)
                self.assertEqual((output / 'selection.json').read_bytes(), before)

    def test_collection_rejects_disagreement_about_countdown_before_order_selection(self):
        for new_filter_in in ('capture_context.json', 'applications.json'):
            with self.subTest(new_filter_in=new_filter_in), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                scope = {'mode': 'date', 'date': '2026-01-01'}
                inputs(root, scope=scope)
                record = run_invoice.load(root / new_filter_in)
                record['query_scope'] = {**scope, 'countdown': 'started'}
                write(root / new_filter_in, record)
                with self.assertRaisesRegex(ValueError, '查询范围'):
                    self.collect_orders(root)
                self.assertFalse((root / 'selection.json').exists())
                self.assertFalse((root / 'order_ids.json').exists())

    def test_countdown_collection_cannot_reuse_an_unfiltered_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope = {'mode': 'date', 'date': '2026-01-01', 'countdown': 'started'}
            inputs(root, scope=scope)
            rows = run_invoice.read_source(root / 'qianniu_common.xlsx')
            old = run_invoice.select_template_rows(rows, root / 'qianniu_common.xlsx')[2]
            run_invoice.save(root / 'selection.json', old)
            before = (root / 'selection.json').read_bytes()
            with self.assertRaisesRegex(ValueError, '检查点已存在且内容不同'):
                self.collect_orders(root)
            self.assertEqual((root / 'selection.json').read_bytes(), before)
            with self.assertRaisesRegex(ValueError, '通用模板选择检查点'):
                self.run_cli(root, root / 'result', ['--date', '2026-01-01'], plan_only=True)

    def test_countdown_replay_requires_matching_context_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope = {'mode': 'date', 'date': '2026-01-01', 'countdown': 'started'}
            inputs(root, scope=scope)
            write(root / 'capture_context.json', {'store': 'Synthetic shop', 'issuer': 'Synthetic issuer'})
            output = root / 'result'
            with self.assertRaises(ValueError):
                self.run_cli(root, output, ['--date', '2026-01-01'], replay=True, plan_only=True)
            self.assertEqual(run_invoice.load(output / 'run.json')['status'], 'failed')
            self.assertFalse((output / 'invoice_plan.json').exists())

    def test_broad_export_is_intersected_with_countdown_membership_before_order_queries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scope = {**ALL, 'countdown': 'started'}
            inputs(root, scope=scope)
            original = (root / 'qianniu_common.xlsx').read_bytes()
            applications = run_invoice.load(root / 'applications.json')
            applications['rows'] = [row for row in applications['rows'] if row['serialNo'] != 'A-2']
            # These values must not override list membership or export status.
            for row in applications['rows']:
                row.update(applyStatus=9, remainTime=0)
            applications.update(total=3, observed_total=3, api_total=2,
                                list_non_pending_snapshot_rows=[applications['rows'][-1]])
            write(root / 'applications.json', applications)
            self.collect_orders(root)
            selection = run_invoice.load(root / 'selection.json')
            self.assertEqual(selection['selected_application_ids'], ['A-1', 'A-negative'])
            self.assertEqual(selection['raw_pending_application_ids'], ['A-1', 'A-2', 'A-negative'])
            self.assertEqual(selection['raw_pending_source_row_count'], 3)
            self.assertEqual(selection['pending_source_row_count'], 2)
            self.assertEqual(selection['filtered_out_countdown_application_ids'], ['A-2'])
            self.assertEqual(selection['filtered_out_countdown_rows'],
                             [{'source_row': 3, 'serialNo': 'A-2', 'status': '待处理'}])
            self.assertEqual(selection['applications_sha256'], run_invoice.sha(root / 'applications.json'))
            self.assertEqual(run_invoice.load(root / 'order_ids.json')['orders'], ['O-A-1'])
            batches = run_invoice.load(root / 'order_batches.json')
            batches['batches'] = [{'ids': ['O-A-1'], 'pageNum': 1,
                                  'page': {'totalNumber': 1, 'totalPage': 1}, 'order_ids': ['O-A-1']}]
            batches['items'] = [item for item in batches['items'] if item['order_no'] == 'O-A-1']
            write(root / 'order_batches.json', batches)
            output = root / 'result'
            self.run_cli(root, output, ['--all-pending'], render=True)
            manifest = run_invoice.load(output / 'run.json')
            self.assertEqual((manifest['selected_count'], manifest['ready_count'], manifest['excluded_count']), (2, 1, 1))
            self.assertEqual((output / 'qianniu_common_all-pending.xlsx').read_bytes(), original)
            tax = output / 'qianniu_invoice_tax_template_all-pending.xlsx'
            schema = inspect_template(tax)
            for name in SHEETS[:2]:
                meta = schema[name]
                records = [row for rn, row in read_rows(tax, name) if rn > meta['header_row'] and any(row.values())]
                self.assertEqual({row[meta['columns']['发票流水号']] for row in records}, {'A-1'})

    def test_countdown_rejects_missing_or_incomplete_filtered_list(self):
        for alteration in ('missing', 'missing_count', 'too_few', 'duplicate', 'wrong_scope'):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                scope = {**ALL, 'countdown': 'started'}
                inputs(root, scope=scope)
                path = root / 'applications.json'
                applications = run_invoice.load(path)
                if alteration == 'missing':
                    path.unlink()
                else:
                    if alteration == 'missing_count':
                        applications.pop('api_total')
                    elif alteration == 'too_few':
                        applications['api_total'] = applications['observed_total'] + 1
                    elif alteration == 'duplicate':
                        applications['rows'].append(applications['rows'][0])
                    else:
                        applications['query_scope'].pop('countdown')
                    write(path, applications)
                rows = run_invoice.read_source(root / 'qianniu_common.xlsx')
                with self.assertRaises(ValueError):
                    run_invoice.select_template_rows(rows, root / 'qianniu_common.xlsx', scope,
                                                     applications_path=path)

    def test_changed_filtered_membership_invalidates_existing_selection_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root, scope={**ALL, 'countdown': 'started'})
            self.collect_orders(root)
            checkpoint = run_invoice.load(root / 'order_ids.json')
            collection_files.validate_selection_scope(root, checkpoint, '订单清单')
            applications = run_invoice.load(root / 'applications.json')
            applications['queried_at'] = '2026-02-20T01:00:00Z'
            write(root / 'applications.json', applications)
            with self.assertRaisesRegex(ValueError, '开票倒计时申请列表'):
                collection_files.validate_selection_scope(root, checkpoint, '订单清单')

    def test_replay_without_context_uses_frozen_application_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root)
            (root / 'capture_context.json').unlink()
            output = root / 'result'
            self.run_cli(root, output, ['--all-pending'], replay=True, plan_only=True)
            manifest = run_invoice.load(output / 'run.json')
            self.assertEqual(manifest['query_scope'], ALL)
            self.assertEqual(manifest['status'], 'plan_only')
            self.assertEqual(run_invoice.load(output / 'invoice_plan.json')['query_scope'], ALL)
            self.assertFalse((output / 'qianniu_invoice_tax_template_all-pending.xlsx').exists())

    def test_replay_identity_only_context_preserves_dated_and_frozen_all_scopes(self):
        for scope, options, label in (
                ({'mode': 'date', 'date': '2026-01-01'}, ['--date', '2026-01-01'], '2026-01-01'),
                (ALL, ['--all-pending'], 'all-pending')):
            for empty_date in ({}, {'date': None}, {'date': ''}):
                with self.subTest(scope=scope, empty_date=empty_date), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    inputs(root, scope=scope)
                    write(root / 'capture_context.json', {'store': 'Synthetic shop',
                          'issuer': 'Synthetic issuer', **empty_date})
                    output = root / 'result'
                    self.run_cli(root, output, options, replay=True, plan_only=True)
                    manifest = run_invoice.load(output / 'run.json')
                    plan = run_invoice.load(output / 'invoice_plan.json')
                    self.assertEqual(manifest['status'], 'plan_only')
                    if scope == ALL:
                        self.assertEqual(manifest['query_scope'], ALL)
                        self.assertEqual(plan['query_scope'], ALL)
                    else:
                        self.assertNotIn('query_scope', manifest)
                        self.assertNotIn('query_scope', plan)
                    self.assertEqual((output / f'qianniu_common_{label}.xlsx').read_bytes(),
                                     (root / 'qianniu_common.xlsx').read_bytes())

    def test_non_replay_identity_only_context_still_fails(self):
        for scope, options in ((ALL, ['--all-pending']),
                               ({'mode': 'date', 'date': '2026-01-01'}, ['--date', '2026-01-01'])):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                inputs(root, scope=scope)
                context = run_invoice.load(root / 'capture_context.json')
                context.pop('date', None)
                context.pop('query_scope', None)
                write(root / 'capture_context.json', context)
                output = root / 'result'
                with self.assertRaises(ValueError):
                    self.run_cli(root, output, options, plan_only=True)
                self.assertEqual(run_invoice.load(output / 'run.json')['status'], 'failed')
                self.assertFalse((output / 'invoice_plan.json').exists())

    def test_replay_malformed_explicit_context_scope_never_falls_back_to_applications(self):
        for invalid in (None, [], {'mode': 'all_pending'}):
            with self.subTest(query_scope=invalid), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                inputs(root)
                context = run_invoice.load(root / 'capture_context.json')
                context['query_scope'] = invalid
                write(root / 'capture_context.json', context)
                output = root / 'result'
                with self.assertRaises(ValueError):
                    self.run_cli(root, output, ['--all-pending'], replay=True, plan_only=True)
                self.assertEqual(run_invoice.load(output / 'run.json')['status'], 'failed')
                self.assertFalse((output / 'invoice_plan.json').exists())

    def test_two_different_valid_all_pending_windows_do_not_mix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root)
            applications = run_invoice.load(root / 'applications.json')
            applications['query_scope'] = {'mode': 'all_pending', 'start_date': '2026-01-20', 'end_date': '2026-03-20'}
            write(root / 'applications.json', applications)
            output = root / 'result'
            with self.assertRaises(ValueError):
                self.run_cli(root, output, ['--all-pending'], replay=True, plan_only=True)
            self.assertFalse((output / 'invoice_plan.json').exists())
            self.assertEqual((output / 'qianniu_common_all-pending.xlsx').read_bytes(), (root / 'qianniu_common.xlsx').read_bytes())

    def test_cli_requires_exactly_one_explicit_scope_before_creating_output(self):
        for options in ([], ['--date', '2026-01-01', '--all-pending']):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output = root / 'result'
                with self.assertRaises(SystemExit) as error:
                    self.run_cli(root, output, options)
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
