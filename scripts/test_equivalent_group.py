"""Synthetic end-to-end regressions for interchangeable source product groups.

These fixtures contain no customer snapshots.  They exercise the saved raw
detail -> assembly -> planning -> independent source verification boundary.
"""
import json
import tempfile
import unittest
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as ET

import build_invoice_plan as rules
from run_invoice import assemble, verify_sources
from template_io import Q
from test_pipeline import col, fixture


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def common_fixture(path, rows):
    """Extend the shared minimal source workbook with matching fields."""
    fixture(path, common=True, common_rows=rows)
    with ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    name = 'xl/worksheets/custom30.xml'
    xml = ET.fromstring(entries[name])
    data = xml.find(Q('sheetData'))
    labels = ['货物名称', '规格', '商品编码', '子订单号', '税率']
    for row_number, node in enumerate(data.findall(Q('row'))):
        for column, label in enumerate(labels, 11):
            value = label if row_number == 0 else rows[row_number - 1].get(label, '')
            if value == '':
                continue
            cell = ET.SubElement(node, Q('c'), r=col(column) + node.get('r'), t='inlineStr')
            ET.SubElement(ET.SubElement(cell, Q('is')), Q('t')).text = str(value)
    entries[name] = ET.tostring(xml)
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def group_fixture():
    base = {'申请流水号': 'A', '订单编号': '123', '开票总金额': '35.80',
            '货物名称': '测试曲奇组合', '发票类型': '全电普通发票',
            '抬头类型': '个人', '发票抬头': '合成测试购方', '开票状态': '待处理'}
    rows = []
    items = []
    for index, flavor in enumerate(('开心果', '树莓')):
        rows.extend([{**base, '商品金额': '23.00', '数量': '1'},
                     {**base, '商品金额': '-5.10', '数量': ''}])
        items.append({'order_no': '123', 'sub_order_no': str(124 + index),
                      'goods_code': 'SYNTHETIC-SKU-' + str(index),
                      'title': base['货物名称'], 'specification': ['口味:' + flavor],
                      'quantity': '1', 'source': 'order_detail_dom',
                      'unit_price': '23.00', 'price_cell': '23.00\n\nx1'})
    documents = [{'order_no': '123', 'verified_order': True, 'items': deepcopy(items),
                  'url': 'https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId=123',
                  'queried_at': '2026-01-02T00:00:00Z'}]
    batch = [{key: value for key, value in item.items()
              if key not in {'source', 'unit_price', 'price_cell'}} for item in items]
    jst = [{'input_goods_code': item['goods_code'], 'ok': True,
            'exact_matches': [{'sku_id': item['goods_code'], 'invoice_enabled': True,
                               'invoice_name': '测试曲奇', 'tax_code': '101',
                               'issuing_office': '组', 'tax_rate': '0.13',
                               'properties_value': item['specification'][0],
                               'invoice_spec': '曲奇/' + item['specification'][0]}]}
           for item in items]
    return {'rows': rows, 'batch': batch, 'documents': documents, 'jst': jst}


def save_fixture(root, data):
    common_fixture(root / 'qianniu_common.xlsx', data['rows'])
    by_serial = {}
    for row in data['rows']:
        by_serial[row['申请流水号']] = row['订单编号']
    orders = list(dict.fromkeys(row['订单编号'] for row in data['rows']))
    snapshot_rows = data.get('snapshot_rows', [{'serialNo': serial, 'tid': order}
                                              for serial, order in by_serial.items()])
    write(root / 'applications.json', {
        'date': '2026-01-01', 'queried_at': '2026-01-02T00:00:00Z',
        'total': len(snapshot_rows), 'rows': snapshot_rows})
    write(root / 'order_batches.json', {
        'batches': [{'ids': orders, 'pageNum': 1,
                     'page': {'totalNumber': len(orders), 'totalPage': 1}, 'order_ids': orders}],
        'items': data['batch']})
    if data['documents'] is not None:
        write(root / 'supplemental_details.json', data['documents'])
    write(root / 'jst_query.json', {'data': data['jst']})
    write(root / 'tax-policy.json', {'source': 'piaoju'})
    return SimpleNamespace(input_dir=root, output_dir=root / 'out', replay=True,
                           date='2026-01-01', store='synthetic-test', issuer='synthetic-test',
                           tax_rate_policy_file=root / 'tax-policy.json')


def build_all(source):
    grouped = defaultdict(list)
    for row in source['template_rows']:
        grouped[row['申请流水号']].append((row['__source_row'], row))
    order_goods = defaultdict(set)
    for item in source['order_items']:
        if item['goods_code']:
            order_goods[item['order_no']].add(item['goods_code'])
    return {'invoices': [rules.build_invoice(
        rules.InvoiceBuild(serial, rows), order_goods,
        rules.make_jst_index(source['jst_invoice_goods']), source['run'],
        rules.make_order_items_index(source['order_items']))
        for serial, rows in grouped.items()]}


def business_lines(invoice):
    fields = ('goods_code', 'sub_order_no', 'item_name', 'tax_classification_code',
              'specification', 'unit', 'quantity', 'amount', 'discount_amount', 'tax_rate')
    return Counter(tuple(line[field] for field in fields) for line in invoice['detail_lines'])


class EquivalentGroupTests(unittest.TestCase):
    def assemble_and_build(self, root, data):
        source = assemble(save_fixture(root, data))
        plan = build_all(source)
        verify_sources(source, plan)
        return source, plan

    def assert_blocked(self, data):
        with tempfile.TemporaryDirectory() as folder:
            _, plan = self.assemble_and_build(Path(folder), data)
        self.assertTrue(plan['invoices'])
        self.assertTrue(all(invoice['status'] == 'blocked' for invoice in plan['invoices']), plan)

    def test_identical_rows_cover_two_flavors_once_and_keep_discounts(self):
        with tempfile.TemporaryDirectory() as folder:
            source, plan = self.assemble_and_build(Path(folder), group_fixture())
            invoice = plan['invoices'][0]
            self.assertEqual(invoice['errors'], [])
            self.assertEqual(invoice['status'], 'ready_for_export')
            self.assertEqual(Decimal(invoice['computed_total_amount']), Decimal('35.80'))
            self.assertEqual({line['goods_code'] for line in invoice['detail_lines']},
                             {'SYNTHETIC-SKU-0', 'SYNTHETIC-SKU-1'})
            self.assertEqual({line['sub_order_no'] for line in invoice['detail_lines']}, {'124', '125'})
            self.assertEqual({line['specification'] for line in invoice['detail_lines']},
                             {'曲奇/口味:开心果', '曲奇/口味:树莓'})
            self.assertEqual({tuple(item['source_specification']) for item in source['order_items']},
                             {('口味:开心果',), ('口味:树莓',)})
            for line in invoice['detail_lines']:
                self.assertEqual(Decimal(line['quantity']), 1)
                self.assertEqual(Decimal(line['amount']), Decimal('23'))
                self.assertEqual(Decimal(line['discount_amount']), Decimal('-5.1'))
                self.assertEqual(line['match_method'], 'group_equivalence')
                self.assertTrue(line['group_equivalence'])
            self.assertEqual(len(verify_sources(source, plan)), 1)

    def test_order_and_source_group_reordering_preserve_business_lines(self):
        expected = None
        for reverse_batch, reverse_details, reverse_source in (
                (False, False, False), (True, False, False),
                (False, True, False), (True, True, True)):
            data = group_fixture()
            if reverse_batch:
                data['batch'].reverse()
            if reverse_details:
                data['documents'][0]['items'].reverse()
            if reverse_source:
                data['rows'] = data['rows'][2:] + data['rows'][:2]
            with self.subTest(batch=reverse_batch, detail=reverse_details, source=reverse_source), \
                    tempfile.TemporaryDirectory() as folder:
                _, plan = self.assemble_and_build(Path(folder), data)
                invoice = plan['invoices'][0]
                self.assertEqual(invoice['errors'], [])
                actual = business_lines(invoice)
                if expected is None:
                    expected = actual
                self.assertEqual(actual, expected)

    def test_one_source_row_cannot_choose_between_two_candidates(self):
        data = group_fixture()
        data['rows'] = data['rows'][:2]
        for row in data['rows']:
            row['开票总金额'] = '17.90'
        self.assert_blocked(data)

    def test_group_must_cover_every_candidate(self):
        data = group_fixture()
        extra = deepcopy(data['batch'][0])
        extra.update(sub_order_no='126', goods_code='SYNTHETIC-SKU-2')
        data['batch'].append(extra)
        detail = deepcopy(data['documents'][0]['items'][0])
        detail.update(extra)
        data['documents'][0]['items'].append(detail)
        response = deepcopy(data['jst'][0])
        response['input_goods_code'] = response['exact_matches'][0]['sku_id'] = extra['goods_code']
        data['jst'].append(response)
        self.assert_blocked(data)

    def test_different_discounts_do_not_pass_by_equal_invoice_total(self):
        data = group_fixture()
        data['rows'][1]['商品金额'] = '-4.10'
        data['rows'][3]['商品金额'] = '-6.10'
        self.assert_blocked(data)

    def test_equal_net_amount_does_not_replace_gross_and_discount_evidence(self):
        data = group_fixture()
        data['rows'][2]['商品金额'] = '24.00'
        data['rows'][3]['商品金额'] = '-6.10'
        # Both net amounts are still 17.90 and the invoice is still 35.80,
        # but the detail snapshot proves 23.00 gross for both products.
        self.assert_blocked(data)

    def test_uncovered_quantity_conflict_blocks_whole_invoice(self):
        data = group_fixture()
        data['rows'][2]['数量'] = '2'
        self.assert_blocked(data)

    def test_source_identity_and_specification_conflicts_are_not_relaxed(self):
        for field, value in [('货物名称', '不同商品'), ('规格', '口味:不存在'),
                             ('商品编码', 'SYNTHETIC-NOT-IN-ORDER'), ('子订单号', '999')]:
            with self.subTest(field=field):
                data = group_fixture()
                for row in data['rows']:
                    row[field] = value
                self.assert_blocked(data)

    def test_explicit_goods_codes_keep_case_and_internal_whitespace_significant(self):
        for conflicting_code in ('synthetic-sku-0', 'SYNTHETIC -SKU-0'):
            with self.subTest(conflicting_code=conflicting_code):
                data = group_fixture()
                for row in data['rows']:
                    row['商品编码'] = 'SYNTHETIC-SKU-0'
                data['batch'][1]['goods_code'] = conflicting_code
                data['documents'][0]['items'][1]['goods_code'] = conflicting_code
                data['jst'][1]['input_goods_code'] = conflicting_code
                data['jst'][1]['exact_matches'][0]['sku_id'] = conflicting_code
                self.assert_blocked(data)

    def test_explicit_suborder_identifiers_are_not_casefolded(self):
        data = group_fixture()
        for row in data['rows']:
            row['子订单号'] = 'SUB-ORDER'
        for items in (data['batch'], data['documents'][0]['items']):
            items[0]['sub_order_no'] = 'SUB-ORDER'
            items[1]['sub_order_no'] = 'sub-order'
        self.assert_blocked(data)

    def test_duplicate_or_missing_suborders_fail_assembly(self):
        for location in ('batch', 'detail'):
            for value in ('', '124'):
                with self.subTest(location=location, value=value), tempfile.TemporaryDirectory() as folder:
                    data = group_fixture()
                    items = data['batch'] if location == 'batch' else data['documents'][0]['items']
                    items[1]['sub_order_no'] = value
                    with self.assertRaises(ValueError):
                        self.assemble_and_build(Path(folder), data)

    def test_missing_goods_code_blocks_whole_group(self):
        data = group_fixture()
        data['batch'][1]['goods_code'] = ''
        data['documents'][0]['items'][1]['goods_code'] = ''
        self.assert_blocked(data)

    def test_same_sku_in_distinct_verified_suborders_is_covered_once_each(self):
        data = group_fixture()
        for items in (data['batch'], data['documents'][0]['items']):
            items[1]['goods_code'] = items[0]['goods_code']
            items[1]['specification'] = deepcopy(items[0]['specification'])
        data['jst'] = data['jst'][:1]
        with tempfile.TemporaryDirectory() as folder:
            _, plan = self.assemble_and_build(Path(folder), data)
        invoice = plan['invoices'][0]
        self.assertEqual(invoice['status'], 'ready_for_export')
        self.assertEqual({line['sub_order_no'] for line in invoice['detail_lines']}, {'124', '125'})
        self.assertEqual([line['goods_code'] for line in invoice['detail_lines']],
                         ['SYNTHETIC-SKU-0', 'SYNTHETIC-SKU-0'])
        self.assertEqual(Decimal(invoice['computed_total_amount']), Decimal('35.80'))

    def test_missing_partial_or_invalid_raw_price_evidence_blocks(self):
        for mutation in ('missing_file', 'partial_items', 'source', 'unit_price', 'price_cell',
                         'url', 'queried_at'):
            with self.subTest(mutation=mutation):
                data = group_fixture()
                if mutation == 'missing_file':
                    data['documents'] = None
                elif mutation == 'partial_items':
                    data['documents'][0]['items'].pop()
                elif mutation in ('url', 'queried_at'):
                    data['documents'][0][mutation] = ''
                else:
                    data['documents'][0]['items'][0][mutation] = '999'
                self.assert_blocked(data)

    def test_extra_fee_is_not_spread_across_equivalent_group(self):
        data = group_fixture()
        data['rows'].append({**data['rows'][0], '货物名称': '价外费用', '商品金额': '2.00'})
        for row in data['rows']:
            row['开票总金额'] = '37.80'
        self.assert_blocked(data)

    def test_source_rows_from_different_applications_cannot_complete_group(self):
        data = group_fixture()
        for row in data['rows']:
            row['开票总金额'] = '17.90'
        for row in data['rows'][2:]:
            row['申请流水号'] = 'B'
        self.assert_blocked(data)

    def test_source_rows_from_different_orders_cannot_complete_group(self):
        data = group_fixture()
        for row in data['rows'][2:]:
            row['订单编号'] = '223'
        second_items = deepcopy(data['batch'])
        for item in second_items:
            item['order_no'] = '223'
            item['sub_order_no'] = str(int(item['sub_order_no']) + 100)
        data['batch'].extend(second_items)
        second_doc = deepcopy(data['documents'][0])
        second_doc['order_no'] = '223'
        second_doc['url'] = second_doc['url'].replace('=123', '=223')
        for item in second_doc['items']:
            item['order_no'] = '223'
            item['sub_order_no'] = str(int(item['sub_order_no']) + 100)
        data['documents'].append(second_doc)
        # The legacy list snapshot records one order per serial; omission is
        # supported, so remove that diagnostic row after writing the fixture.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = save_fixture(root, data)
            write(root / 'applications.json', {'date': '2026-01-01',
                  'queried_at': '2026-01-02T00:00:00Z', 'total': 0, 'rows': []})
            source = assemble(args)
            plan = build_all(source)
            verify_sources(source, plan)
            self.assertEqual(plan['invoices'][0]['status'], 'blocked')

    def test_two_complete_orders_within_one_application_are_independent_groups(self):
        data = group_fixture()
        second_rows = deepcopy(data['rows'])
        for row in second_rows:
            row['订单编号'] = '223'
        data['rows'].extend(second_rows)
        for row in data['rows']:
            row['开票总金额'] = '71.60'
        second_items = deepcopy(data['batch'])
        for item in second_items:
            item['order_no'] = '223'
            item['sub_order_no'] = str(int(item['sub_order_no']) + 100)
        data['batch'].extend(second_items)
        second_doc = deepcopy(data['documents'][0])
        second_doc['order_no'] = '223'
        second_doc['url'] = second_doc['url'].replace('=123', '=223')
        for item in second_doc['items']:
            item['order_no'] = '223'
            item['sub_order_no'] = str(int(item['sub_order_no']) + 100)
        data['documents'].append(second_doc)
        # Legacy diagnostic snapshots may omit selected applications. The
        # complete common template remains authoritative for invoice scope.
        data['snapshot_rows'] = []
        with tempfile.TemporaryDirectory() as folder:
            _, plan = self.assemble_and_build(Path(folder), data)
        invoice = plan['invoices'][0]
        self.assertEqual(invoice['status'], 'ready_for_export')
        self.assertEqual(Decimal(invoice['computed_total_amount']), Decimal('71.60'))
        self.assertEqual({line['sub_order_no'] for line in invoice['detail_lines']},
                         {'124', '125', '224', '225'})
        self.assertEqual({line['group_equivalence']['order_no'] for line in invoice['detail_lines']},
                         {'123', '223'})

    def test_piaoju_required_unit_and_enabled_gate_still_apply(self):
        for field, value in [('issuing_office', ''), ('invoice_enabled', False)]:
            with self.subTest(field=field):
                data = group_fixture()
                data['jst'][0]['exact_matches'][0][field] = value
                self.assert_blocked(data)

    def test_group_proof_and_selected_assignment_are_independently_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            source, plan = self.assemble_and_build(Path(folder), group_fixture())
            self.assertEqual(plan['invoices'][0]['status'], 'ready_for_export')
            for mutation in ('proof', 'assignment', 'permutation', 'drop_proof', 'drop_method'):
                with self.subTest(mutation=mutation):
                    forged = deepcopy(plan)
                    lines = forged['invoices'][0]['detail_lines']
                    if mutation == 'proof':
                        lines[0]['group_equivalence']['application_no'] = 'forged'
                    elif mutation == 'assignment':
                        # Keep the selected item valid individually but select
                        # it twice. The whole group no longer covers both SKUs.
                        for key in ('goods_code', 'sub_order_no', 'order_item_evidence',
                                    'specification'):
                            lines[0][key] = deepcopy(lines[1][key])
                    elif mutation == 'permutation':
                        # Both lines still satisfy ordinary per-line checks;
                        # only the proof's claimed deterministic assignment is
                        # false. Recompute the group instead of trusting it.
                        for key in ('goods_code', 'sub_order_no', 'order_item_evidence',
                                    'specification'):
                            lines[0][key], lines[1][key] = lines[1][key], lines[0][key]
                    elif mutation == 'drop_proof':
                        lines[0].pop('group_equivalence')
                    else:
                        lines[0].pop('match_method')
                    with self.assertRaises(ValueError):
                        verify_sources(source, forged)

    def test_mutated_raw_group_evidence_cannot_validate_a_ready_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            source, plan = self.assemble_and_build(Path(folder), group_fixture())
            self.assertEqual(plan['invoices'][0]['status'], 'ready_for_export')
            for mutation in ('price', 'hash', 'missing'):
                with self.subTest(mutation=mutation):
                    forged_source = deepcopy(source)
                    item = forged_source['order_items'][0]
                    if mutation == 'price':
                        item['detail_gross_evidence'][0]['item']['price_cell'] = '24.00 x1'
                    elif mutation == 'hash':
                        item['detail_gross_evidence'][0]['checkpoint_sha256'] = '0' * 64
                    else:
                        item.pop('detail_gross_evidence')
                    forged_plan = deepcopy(plan)
                    for line in forged_plan['invoices'][0]['detail_lines']:
                        if line['sub_order_no'] == item['sub_order_no']:
                            line['order_item_evidence'] = item
                    with self.assertRaises(ValueError):
                        verify_sources(forged_source, forged_plan)

    def test_removing_all_group_claims_cannot_bypass_ambiguity_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            source, plan = self.assemble_and_build(Path(folder), group_fixture())
            self.assertEqual(plan['invoices'][0]['status'], 'ready_for_export')
            forged_source = deepcopy(source)
            forged_plan = deepcopy(plan)
            by_suborder = {}
            for item in forged_source['order_items']:
                item.pop('detail_gross_evidence')
                by_suborder[item['sub_order_no']] = item
            for line in forged_plan['invoices'][0]['detail_lines']:
                line.pop('match_method')
                line.pop('group_equivalence')
                line['order_item_evidence'] = by_suborder[line['sub_order_no']]
            # Each selected item remains in the source collection and passes
            # ordinary field equality. Removing every proof must not turn two
            # ambiguous candidates into a supposedly unique mapping.
            with self.assertRaises(ValueError):
                verify_sources(forged_source, forged_plan)

    def test_physical_checkpoint_byte_change_invalidates_group_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, plan = self.assemble_and_build(root, group_fixture())
            checkpoint = root / 'supplemental_details.json'
            original = checkpoint.read_bytes()
            checkpoint.write_bytes(original + b'\n')
            # JSON membership still agrees, but the original saved byte hash
            # does not; neither a fresh plan nor the previous ready plan may pass.
            rebuilt = build_all(source)
            self.assertEqual(rebuilt['invoices'][0]['status'], 'blocked')
            with self.assertRaises(ValueError):
                verify_sources(source, plan)

    def test_forged_raw_document_and_rehashed_proof_do_not_replace_saved_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, plan = self.assemble_and_build(root, group_fixture())
            checkpoint = root / 'supplemental_details.json'
            original = checkpoint.read_bytes()
            forged_source = deepcopy(source)
            forged_plan = deepcopy(plan)
            by_suborder = {}
            hashes = {}
            for item in forged_source['order_items']:
                for record in item['detail_gross_evidence']:
                    record['document']['queried_at'] = '2026-01-03T00:00:00Z'
                by_suborder[item['sub_order_no']] = item
                hashes[item['sub_order_no']] = sorted({rules.canonical_hash(record)
                    for record in item['detail_gross_evidence']})
            for line in forged_plan['invoices'][0]['detail_lines']:
                line['order_item_evidence'] = by_suborder[line['sub_order_no']]
                proof = deepcopy(line['group_equivalence'])
                proof['raw_evidence_sha256'] = hashes
                proof.pop('proof_sha256')
                proof['proof_sha256'] = rules.canonical_hash(proof)
                line['group_equivalence'] = proof
            self.assertEqual(checkpoint.read_bytes(), original)
            self.assertEqual(build_all(forged_source)['invoices'][0]['status'], 'blocked')
            with self.assertRaises(ValueError):
                verify_sources(forged_source, forged_plan)
            # The untouched real evidence remains valid in the same directory.
            self.assertEqual(len(verify_sources(source, plan)), 1)

    def test_existing_unique_single_item_matching_still_works(self):
        data = group_fixture()
        data['rows'] = data['rows'][:2]
        for row in data['rows']:
            row['开票总金额'] = '17.90'
        data['batch'] = data['batch'][:1]
        data['documents'][0]['items'] = data['documents'][0]['items'][:1]
        data['jst'] = data['jst'][:1]
        with tempfile.TemporaryDirectory() as folder:
            _, plan = self.assemble_and_build(Path(folder), data)
        invoice = plan['invoices'][0]
        self.assertEqual(invoice['status'], 'ready_for_export')
        self.assertEqual(invoice['errors'], [])
        self.assertEqual(Decimal(invoice['computed_total_amount']), Decimal('17.90'))


if __name__ == '__main__':
    unittest.main()
