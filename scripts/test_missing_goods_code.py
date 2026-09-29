"""Missing SKU evidence blocks only affected invoices without dropping rows."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import run_invoice
from template_io import SHEETS, make_output_rows
from test_invoice_scope_pipeline import inputs, write


class MissingGoodsCodeTests(unittest.TestCase):
    def test_verified_missing_code_keeps_other_invoice_exportable(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root)
            batches = run_invoice.load(root / 'order_batches.json')
            batches['items'][0]['goods_code'] = ''
            write(root / 'order_batches.json', batches)
            write(root / 'old_details.json', [{
                'order_no': batches['items'][0]['order_no'], 'verified_order': True,
                'items': [dict(batches['items'][0])],
            }])
            out = root / 'output'
            out.mkdir()
            source = run_invoice.assemble(SimpleNamespace(input_dir=root, output_dir=out,
                date=None, all_pending=True, replay=False, store='Synthetic shop', issuer='Synthetic issuer'))
            self.assertEqual(len(source['order_items']), 2)
            self.assertEqual(source['order_items'][0]['goods_code'], '')
            plan = run_invoice.build_plan(source, out)
            invoices = {invoice['invoice_serial_no']: invoice for invoice in plan['invoices']}
            self.assertTrue(any('缺少商家编码' in e for e in invoices['A-1']['errors']))
            ready = run_invoice.verify_sources(source, plan)
            self.assertEqual([invoice['invoice_serial_no'] for invoice in ready], ['A-2'])
            rows = make_output_rows(plan)
            for sheet in SHEETS[:2]:
                self.assertEqual([row['发票流水号'] for row in rows[sheet]], ['A-2'])

    def test_only_missing_code_needs_no_fake_jst_query(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            inputs(root, rows=[('A-1', '2026-01-01', '10.00', '待处理')])
            batches = run_invoice.load(root / 'order_batches.json')
            batches['items'][0]['goods_code'] = ''
            write(root / 'order_batches.json', batches)
            (root / 'jst_query.json').unlink()
            out = root / 'output'
            out.mkdir()
            source = run_invoice.assemble(SimpleNamespace(input_dir=root, output_dir=out,
                date=None, all_pending=True, replay=False, store='Synthetic shop', issuer='Synthetic issuer'))
            plan = run_invoice.build_plan(source, out)
            self.assertEqual(run_invoice.verify_sources(source, plan), [])
            self.assertTrue(plan['invoices'][0]['errors'])
            self.assertFalse((root / 'jst_query.json').exists())


if __name__ == '__main__':
    unittest.main()
