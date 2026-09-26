"""Synthetic regression tests for supplemental detail consumption and receipts."""
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import build_invoice_plan as rules
from collection_files import read_part, record_parts_manifest
from order_evidence import merge_order_details, derive_amount_evidence, attach_amount_evidence
from run_invoice import assemble, verify_sources
from test_pipeline import fixture

HERE=Path(__file__).parent


def write(path, data):
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')


def detail_fixture():
    items=[{'order_no':'123','sub_order_no':str(124+i),'goods_code':'SKU-'+str(i),
            'title':'同名商品','specification':['规格:'+str(i)],'quantity':'1',
            'source':'order_detail_dom','unit_price':price,'price_cell':price+'\n\nx1'}
           for i,price in enumerate(['26.10','17.22'])]
    document={'order_no':'123','verified_order':True,'items':items,
              'url':'https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId=123',
              'queried_at':'2026-01-02T00:00:00Z'}
    batch=[{k:v for k,v in item.items() if k not in {'source','unit_price','price_cell'}} for item in items]
    for item in batch:
        item['specification']=['[object Object]'];item['real_total']='999'
    rows=[{'申请流水号':'A','订单编号':'123','商品金额':price,'数量':'1',
           '货物名称':'同名商品','__source_row':2+i} for i,price in enumerate(['26.10','17.22'])]
    return document,batch,rows


class OrderEvidenceTests(unittest.TestCase):
    def test_supplemental_roundtrip_automatically_resolves_two_same_titles(self):
        document,batch,rows=detail_fixture()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for row in rows:
                row.update({'开票总金额':'40.32','发票类型':'全电普通发票','抬头类型':'个人',
                            '发票抬头':'测试购方','开票状态':'待处理'})
            source_rows=[rows[0],{**rows[0],'商品金额':'-1.81','数量':''},
                         rows[1],{**rows[1],'商品金额':'-1.19','数量':''}]
            fixture(root/'qianniu_common.xlsx',common=True,common_rows=source_rows)
            write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
                                           'total':1,'rows':[{'serialNo':'A','tid':'123'}]})
            batches={'batches':[{'ids':['123'],'pageNum':1,'page':{'totalNumber':1,'totalPage':1},
                                'order_ids':['123']}],'items':batch}
            write(root/'order_batches.json',batches)
            write(root/'part.json',batches)
            write(root/'order_ids.json',{'orders':['123'],'selected_application_ids':['A']})
            write(root/'supplemental_details.json',[document])
            write(root/'jst_query.json',{'data':[{'input_goods_code':i['goods_code'],'ok':True,
                  'exact_matches':[{'sku_id':i['goods_code'],'invoice_enabled':True,'invoice_name':'测试商品',
                                    'tax_code':'101','issuing_office':'件','tax_rate':'0'}]} for i in batch]})
            result=subprocess.run([sys.executable,str(HERE/'collection_files.py'),'merge-orders',
                                   '--run-dir',str(root),'--part',str(root/'part.json')],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            source=assemble(SimpleNamespace(input_dir=root,output_dir=root/'out',replay=True,
                                           date='2026-01-01',store='test',issuer='test'))
            self.assertFalse((root/'match_evidence.json').exists())
            self.assertEqual(len(source['derived_match_evidence']),2)
            # The minimal shared OOXML fixture has no optional title column.
            for row in source['template_rows']:
                row['货物名称']='同名商品'
            build=rules.InvoiceBuild('A',[(r['__source_row'],r) for r in source['template_rows']])
            invoice=rules.build_invoice(build,{'123':{'SKU-0','SKU-1'}},
                                        rules.make_jst_index(source['jst_invoice_goods']),source['run'],
                                        rules.make_order_items_index(source['order_items']))
            self.assertEqual(invoice['errors'],[])
            self.assertEqual([line['goods_code'] for line in invoice['detail_lines']],['SKU-0','SKU-1'])
            self.assertEqual([line['amount'] for line in invoice['detail_lines']],['26.1','17.22'])
            self.assertEqual([line['discount_amount'] for line in invoice['detail_lines']],['-1.81','-1.19'])
            verify_sources(source,{'invoices':[invoice]})

    def test_overlap_is_idempotent_and_placeholder_spec_is_replaced(self):
        document,batch,rows=detail_fixture()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write(root/'old_details.json',[document]);write(root/'supplemental_details.json',[document])
            items,details,_=merge_order_details(root,batch,['123'])
            self.assertEqual(len(items),2)
            self.assertEqual(items[0]['specification'],['规格:0'])
            evidence,_=derive_amount_evidence(details,rows)
            self.assertEqual(len(evidence),2)

    def test_conflicting_identity_and_duplicate_item_stop_merge(self):
        for mutation in ('goods_code','quantity','title','duplicate','sub_order_no'):
            document,batch,_=detail_fixture()
            if mutation=='duplicate':document['items'].append(deepcopy(document['items'][0]))
            else:document['items'][0][mutation]='' if mutation=='sub_order_no' else 'conflict'
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);write(root/'supplemental_details.json',[document])
                with self.assertRaises(ValueError,msg=mutation):merge_order_details(root,batch,['123'])

    def test_missing_provenance_net_price_and_raw_cell_mismatch_do_not_create_evidence(self):
        for mutation in ('queried_at','url','source','price_cell','unit_price'):
            document,batch,rows=detail_fixture()
            if mutation in {'queried_at','url'}:document[mutation]=''
            else:
                for item in document['items']:item[mutation]='999'
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);write(root/'supplemental_details.json',[document])
                _,details,_=merge_order_details(root,batch,['123'])
                evidence,diagnostics=derive_amount_evidence(details,rows)
                self.assertEqual(evidence,[],mutation)
                self.assertTrue(diagnostics,mutation)
        document,batch,rows=detail_fixture()
        for i,item in enumerate(document['items']):
            item['unit_price']=['24.29','16.03'][i]
            item['price_cell']=item['unit_price']+'\nx1'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);write(root/'supplemental_details.json',[document])
            _,details,_=merge_order_details(root,batch,['123'])
            self.assertEqual(derive_amount_evidence(details,rows)[0],[])

    def test_equal_amount_and_reused_item_remain_ambiguous(self):
        for mode in ('equal','reused'):
            document,batch,rows=detail_fixture()
            if mode=='equal':
                document['items'][1]['unit_price']='26.10';document['items'][1]['price_cell']='26.10\nx1'
                rows=rows[:1]
            else:rows=[rows[0],{**rows[0],'__source_row':4}]
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder);write(root/'supplemental_details.json',[document])
                _,details,_=merge_order_details(root,batch,['123'])
                self.assertEqual(derive_amount_evidence(details,rows)[0],[],mode)

    def test_conflicting_manual_and_derived_amounts_are_rejected(self):
        item={'order_no':'123','sub_order_no':'124','goods_code':'SKU-0'}
        e={**item,'source':'verified gross','source_amount':'26.10','match_amount_source':'order_detail_gross'}
        attach_amount_evidence([item],[e,e])
        with self.assertRaises(ValueError):attach_amount_evidence([item],[{**e,'source_amount':'17.22'}])
        with self.assertRaises(ValueError):attach_amount_evidence([item],[{**e,'source':''}])

    def test_receipts_and_changed_manifest_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);receipt=root/'jst_part_001.json.receipt.json'
            write(receipt,{'data':[]})
            with self.assertRaises(ValueError):read_part(receipt,'jst')
            part=root/'jst_part_001.json';write(part,{'data':[]})
            record_parts_manifest(root,'jst',[part]);before=(root/'parts_manifest.json').read_bytes()
            write(part,{'data':[{'input_goods_code':'other'}]})
            with self.assertRaises(ValueError):record_parts_manifest(root,'jst',[part])
            self.assertEqual((root/'parts_manifest.json').read_bytes(),before)

    def test_invalid_merge_does_not_register_manifest_or_replace_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);write(root/'goods_codes.json',{'codes':['SKU']})
            write(root/'part.json',{'data':[]})
            result=subprocess.run([sys.executable,str(HERE/'collection_files.py'),'merge-jst','--run-dir',
                                   str(root),'--part',str(root/'part.json')],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/'parts_manifest.json').exists())
            self.assertFalse((root/'jst_query.json').exists())


if __name__=='__main__':
    unittest.main()
