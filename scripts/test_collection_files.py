"""Order merge preserves missing-code evidence without weakening identity checks."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import collection_files


class CollectionOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.items = [
            {'order_no': 'O1', 'sub_order_no': 'S1', 'goods_code': '', 'title': '商品一', 'quantity': '1'},
            {'order_no': 'O2', 'sub_order_no': 'S2', 'goods_code': 'SKU2', 'title': '商品二', 'quantity': '2'},
        ]
        self.part = {'items': self.items, 'batches': [
            {'ids': ['O1', 'O2'], 'pageNum': 1, 'page': {'totalNumber': 2, 'totalPage': 1},
             'order_ids': ['O1', 'O2']}]}
        self.write('order_ids.json', {'orders': ['O1', 'O2']})

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def merge(self):
        with patch('sys.argv', ['collection_files.py', 'merge-orders', '--run-dir', str(self.root),
                                '--part', str(self.root / 'part.json')]), redirect_stdout(io.StringIO()):
            collection_files.main()

    def test_blank_codes_are_recorded_for_detail_without_modifying_raw_parts(self):
        for code in ('', None, '  '):
            with self.subTest(code=code):
                self.items[0]['goods_code'] = code
                self.write('part.json', self.part)
                # Each case is its own immutable source, so start a fresh
                # manifest rather than pretend a changed source is resumable.
                (self.root / 'parts_manifest.json').unlink(missing_ok=True)
                original = (self.root / 'part.json').read_bytes()
                self.merge()
                batches = json.loads((self.root / 'order_batches.json').read_text(encoding='utf-8'))
                self.assertEqual(batches['missing'], [])
                self.assertEqual(batches['missing_goods_code_orders'], ['O1'])
                self.assertEqual(batches['items'], self.items)
                self.assertEqual(json.loads((self.root / 'goods_codes.json').read_text())['codes'], ['SKU2'])
                self.assertEqual((self.root / 'part.json').read_bytes(), original)

    def test_verified_detail_supplies_new_code_and_clears_missing_code_diagnostic(self):
        self.write('part.json', self.part)
        self.merge()
        self.write('old_details.json', [{'order_no': 'O1', 'verified_order': True,
                                        'items': [{**self.items[0], 'goods_code': 'SKU1'}]}])
        self.merge()
        batches = json.loads((self.root / 'order_batches.json').read_text(encoding='utf-8'))
        self.assertEqual(batches['missing_goods_code_orders'], [])
        self.assertEqual(batches['items'][0]['goods_code'], '')
        self.assertEqual(json.loads((self.root / 'goods_codes.json').read_text())['codes'], ['SKU1', 'SKU2'])

    def test_missing_code_does_not_relax_identity_title_quantity_or_duplicates(self):
        for field, value in (('order_no', 'unexpected'), ('sub_order_no', ''), ('title', ''),
                             ('quantity', ''), ('quantity', '0'), ('quantity', '-1'), ('quantity', 'NaN')):
            items = deepcopy(self.items)
            items[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                collection_files.validate_order_items(items, {'O1', 'O2'}, allow_missing_goods_code=True)
        with self.assertRaises(ValueError):
            collection_files.validate_order_items([self.items[0], self.items[0]], {'O1'},
                                                   allow_missing_goods_code=True)

    def test_missing_code_does_not_relax_pagination_or_detail_conflict(self):
        self.part['batches'][0]['page']['totalPage'] = 2
        self.write('part.json', self.part)
        with self.assertRaises(ValueError):
            self.merge()
        self.assertFalse((self.root / 'order_batches.json').exists())
        self.part['batches'][0]['page']['totalPage'] = 1
        self.write('part.json', self.part)
        self.write('old_details.json', [{'order_no': 'O1', 'verified_order': True,
             'items': [{**self.items[0], 'goods_code': 'SKU1', 'quantity': '999'}]}])
        with self.assertRaises(ValueError):
            self.merge()
        self.assertFalse((self.root / 'order_batches.json').exists())


if __name__ == '__main__':
    unittest.main()
