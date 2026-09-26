"""Synthetic OOXML fixtures exercise native preservation and pipeline failure gates."""
import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED
from lxml import etree as ET
import build_invoice_plan as rules

from template_io import (NS,Q,SHEETS,BASIC,DETAIL,inspect_template,prepare,finalize,
                         read_rows,make_output_rows)
from run_invoice import (verify_workbook,assemble,validate_order_pages,known_total,
                         publish_common_template,read_source,verify_sources,main as run_main,DEFAULT_TEMPLATE)

HERE=Path(__file__).parent
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'

def col(number):
    result=''
    while number:number,remainder=divmod(number-1,26);result=chr(65+remainder)+result
    return result

def fixture(path,clean=False,common=False,common_rows=None,status_columns=True):
    """Minimal XML test double, not a user-facing spreadsheet authoring path."""
    workbook=ET.Element(Q('workbook'),nsmap={None:NS,'r':REL});sheets=ET.SubElement(workbook,Q('sheets'))
    rels=ET.Element('{'+PKG+'}Relationships',nsmap={None:PKG})
    names=['开票申请列表'] if common else [*SHEETS,'dictionary']
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for index,name in enumerate(names):
            part=f'xl/worksheets/custom{index+30}.xml';rid=f'rId{index+1}'
            sheet=ET.SubElement(sheets,Q('sheet'),name=name,sheetId=str(index+1))
            sheet.set('{'+REL+'}id',rid)
            if name=='dictionary':sheet.set('state','hidden')
            ET.SubElement(rels,'{'+PKG+'}Relationship',Id=rid,Target=part[3:],Type=REL+'/worksheet')
            xml=ET.Element(Q('worksheet'),nsmap={None:NS});data=ET.SubElement(xml,Q('sheetData'))
            labels=((['申请流水号','订单编号','开票总金额','商品金额','数量','发票类型','抬头类型','发票抬头']+
                     (['开票状态','申请状态'] if status_columns else [])) if common
                    else list(reversed(BASIC)) if index==0 else list(reversed(DETAIL)) if index==1 else ['发票流水号'])
            header=1 if common else 5
            row=ET.SubElement(data,Q('row'),r=str(header))
            for n,label in enumerate(labels,1):
                c=ET.SubElement(row,Q('c'),r=col(n)+str(header),t='inlineStr',s='1')
                ET.SubElement(ET.SubElement(c,Q('is')),Q('t')).text=label
            if common:
                for row_number,values in enumerate(common_rows or [],start=header+1):
                    row=ET.SubElement(data,Q('row'),r=str(row_number))
                    for n,label in enumerate(labels,1):
                        value=str(values.get(label,''))
                        if not value:
                            continue
                        c=ET.SubElement(row,Q('c'),r=col(n)+str(row_number),t='inlineStr',s='1')
                        ET.SubElement(ET.SubElement(c,Q('is')),Q('t')).text=value
            if not clean and not common:
                row=ET.SubElement(data,Q('row'),r='405')
                c=ET.SubElement(row,Q('c'),r='A405',t='inlineStr',s='2')
                ET.SubElement(ET.SubElement(c,Q('is')),Q('t')).text='old-data'
            ET.SubElement(xml,Q('sheetProtection'),sheet='1')
            z.writestr(part,ET.tostring(xml))
        z.writestr('xl/workbook.xml',ET.tostring(workbook));z.writestr('xl/_rels/workbook.xml.rels',ET.tostring(rels))
        z.writestr('xl/styles.xml',b'native-style-fixture')

def test_template_names_headers_and_dirty_data():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);template=root/'template.xlsx';light=root/'light.xlsx';output=root/'final.xlsx'
        fixture(template);schema=inspect_template(template)
        assert schema[SHEETS[0]]['header_row']==5
        assert schema[SHEETS[0]]['columns']['发票流水号']!='A'
        prepare(template,light,schema)
        finalize(template,light,output,schema)
        expected={name:[] for name in SHEETS}
        changed=verify_workbook(template,output,schema,expected)
        assert len(changed)==4
        for name in SHEETS:assert not any(any(v.values()) for rn,v in read_rows(output,name) if rn>5)
        with ZipFile(template) as a,ZipFile(output) as b:
            assert a.read('xl/worksheets/custom34.xml')==b.read('xl/worksheets/custom34.xml')
            assert a.read('xl/styles.xml')==b.read('xl/styles.xml')

def test_verifier_rejects_stale_business_data():
    with tempfile.TemporaryDirectory() as folder:
        template=Path(folder)/'template.xlsx';fixture(template)
        try:verify_workbook(template,template,inspect_template(template),{name:[] for name in SHEETS})
        except ValueError:pass
        else:raise AssertionError('Old business data must fail verification')

def test_output_rows_respect_fatal_gate():
    try:make_output_rows({'fatal':True,'errors':['missing application']})
    except ValueError:pass
    else:raise AssertionError('Fatal plan accepted')

def write(path,data):path.write_text(json.dumps(data),encoding='utf-8')

def test_checkpoint_retry_merge_and_success_protection():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        write(root/'goods_codes.json',{'codes':['SKU']})
        write(root/'failed.json',{'data':[{'input_goods_code':'SKU','ok':False,'reason':'request_failed'}]})
        write(root/'success.json',{'data':[{'input_goods_code':'SKU','ok':True,'exact_matches':[{'sku_id':'SKU'}]}]})
        command=[sys.executable,str(HERE/'collection_files.py'),'merge-jst','--run-dir',str(root)]
        result=subprocess.run(command+['--part',str(root/'failed.json'),'--part',str(root/'success.json')],capture_output=True)
        assert result.returncode==0,result.stderr
        assert json.loads((root/'jst_query.json').read_text())['mapped_count']==1
        result=subprocess.run(command+['--part',str(root/'success.json'),'--part',str(root/'failed.json')],capture_output=True)
        assert result.returncode!=0

def test_empty_application_day_needs_no_order_or_jst_files():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);fixture(root/'qianniu_common.xlsx',common=True)
        write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z','total':0,'rows':[]})
        args=SimpleNamespace(input_dir=root,output_dir=root/'output',replay=True,date='2026-01-01',store='test',issuer='test')
        source=assemble(args)
        assert source['selected_application_ids']==[] and source['jst_invoice_goods']==[]

def test_common_template_requires_a_status_column():
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'qianniu_common.xlsx';fixture(path,common=True,status_columns=False)
        try:read_source(path)
        except ValueError:pass
        else:raise AssertionError('Common template without a status column accepted')

def test_collection_orders_short_circuits_when_template_has_no_pending_rows():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        fixture(root/'qianniu_common.xlsx',common=True,common_rows=[
            {'申请流水号':'done','订单编号':'O-D','开票总金额':'10.00','商品金额':'10.00','数量':'1',
             '发票类型':'全电普通发票','抬头类型':'企业','发票抬头':'购方','开票状态':'已准'},
        ])
        write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
              'total':1,'api_total':1,'observed_total':1,
              'rows':[{'serialNo':'done','tid':'O-D','applyStatus':1}],
              'list_non_pending_snapshot_rows':[]})
        result=subprocess.run([sys.executable,str(HERE/'collection_files.py'),'orders','--run-dir',str(root)],
                              capture_output=True,text=True,encoding='utf-8',errors='replace')
        assert result.returncode==0,result.stderr
        assert json.loads((root/'order_ids.json').read_text(encoding='utf-8'))['orders']==[]
        selection=json.loads((root/'selection.json').read_text(encoding='utf-8'))
        assert selection['selected_application_ids']==[] and selection['status_counts']=={'已准':1}

def test_collection_orders_excludes_negative_applications_before_order_query():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        fixture(root/'qianniu_common.xlsx',common=True,common_rows=[
            {'申请流水号':'A-negative','订单编号':'O-negative','开票总金额':'-8.00','商品金额':'-8.00',
             '数量':'-1','发票类型':'全电普通发票','抬头类型':'个人','发票抬头':'购方','开票状态':'待处理'},
            {'申请流水号':'A-positive','订单编号':'O-positive','开票总金额':'12.00','商品金额':'12.00',
             '数量':'1','发票类型':'全电普通发票','抬头类型':'个人','发票抬头':'购方','开票状态':'待处理'},
        ])
        write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
              'total':2,'api_total':2,'observed_total':2,
              'rows':[{'serialNo':'A-negative','tid':'O-negative'}, {'serialNo':'A-positive','tid':'O-positive'}],
              'list_non_pending_snapshot_rows':[]})
        result=subprocess.run([sys.executable,str(HERE/'collection_files.py'),'orders','--run-dir',str(root)],
                              capture_output=True,text=True,encoding='utf-8',errors='replace')
        assert result.returncode==0,result.stderr
        checkpoint=json.loads((root/'order_ids.json').read_text(encoding='utf-8'))
        assert checkpoint['orders']==['O-positive']
        assert checkpoint['selected_application_ids']==['A-negative','A-positive']
        assert checkpoint['active_application_ids']==['A-positive']
        assert checkpoint['excluded_negative_application_ids']==['A-negative']

def test_collection_orders_all_negative_needs_no_query_batches():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        fixture(root/'qianniu_common.xlsx',common=True,common_rows=[
            {'申请流水号':'A-negative','订单编号':'O-negative','开票总金额':'-8.00','商品金额':'-8.00',
             '数量':'-1','发票类型':'全电普通发票','抬头类型':'个人','发票抬头':'购方','开票状态':'待处理'},
        ])
        write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
              'total':1,'api_total':1,'observed_total':1,
              'rows':[{'serialNo':'A-negative','tid':'O-negative'}],
              'list_non_pending_snapshot_rows':[]})
        result=subprocess.run([sys.executable,str(HERE/'collection_files.py'),'orders','--run-dir',str(root)],
                              capture_output=True,text=True,encoding='utf-8',errors='replace')
        assert result.returncode==0,result.stderr
        checkpoint=json.loads((root/'order_ids.json').read_text(encoding='utf-8'))
        assert checkpoint['orders']==[]
        assert checkpoint['active_application_ids']==[]
        assert checkpoint['excluded_negative_application_ids']==['A-negative']
        assert not (root/'order_batches.json').exists()
        assert not (root/'jst_query.json').exists()

def test_common_template_status_selects_scope_across_application_snapshot_versions():
    """The downloaded common template, not a list-page status, decides scope."""
    date='2026-01-01'
    pending='A-export-pending';ignored='B-export-complete'
    current={
        'date':date,'queried_at':'2026-01-02T00:00:00Z','total':2,'api_total':1,'observed_total':2,
        'rows':[
            {'serialNo':pending,'tid':'O-A','applyStatus':6},
            {'serialNo':ignored,'tid':'O-B','applyStatus':1},
        ],
        'list_non_pending_snapshot_rows':[{'serialNo':pending,'tid':'O-A','applyStatus':6}],
    }
    # Older checkpoints retained only the endpoint's pending rows and used
    # `total` for that count. They remain valid diagnostic inputs.
    legacy={
        'date':date,'queried_at':'2026-01-02T00:00:00Z','total':1,
        'rows':[{'serialNo':ignored,'tid':'O-B','applyStatus':1}],
    }
    common_rows=[
        {'申请流水号':pending,'订单编号':'O-A','开票总金额':'10.00','商品金额':'10.00','数量':'1',
         '发票类型':'全电普通发票','抬头类型':'企业','发票抬头':'购方A','开票状态':'  ','申请状态':'待处理'},
        {'申请流水号':ignored,'订单编号':'O-B','开票总金额':'20.00','商品金额':'20.00','数量':'1',
         '发票类型':'全电普通发票','抬头类型':'企业','发票抬头':'购方B','开票状态':'已准'},
    ]
    for label,applications in [('current',current),('legacy',legacy)]:
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            fixture(root/'qianniu_common.xlsx',common=True,common_rows=common_rows)
            write(root/'applications.json',applications)
            result=subprocess.run([sys.executable,str(HERE/'collection_files.py'),'orders','--run-dir',str(root)],capture_output=True,text=True,encoding='utf-8',errors='replace')
            assert result.returncode==0,(label,result.stderr)
            selected=json.loads((root/'order_ids.json').read_text(encoding='utf-8'))
            assert selected['selected_application_ids']==[pending],label
            assert selected['orders']==['O-A'],label
            write(root/'order_batches.json',{'batches':[{'ids':['O-A'],'pageNum':1,
                  'page':{'totalNumber':1,'totalPage':1},'order_ids':['O-A']}],
                  'items':[{'order_no':'O-A','sub_order_no':'S-A','goods_code':'SKU-A','title':'商品A','quantity':'1'}]})
            write(root/'jst_query.json',{'data':[{'input_goods_code':'SKU-A','ok':True,
                  'exact_matches':[{'sku_id':'SKU-A'}]}]})
            source=assemble(SimpleNamespace(input_dir=root,output_dir=root/'output',replay=True,
                                            date=date,store='test',issuer='test'))
            assert source['selected_application_ids']==[pending],label
            assert [row['申请流水号'] for row in source['template_rows']]==[pending],label
            assert [item['order_no'] for item in source['order_items']]==['O-A'],label

def test_wrong_capture_subject_stops_before_processing():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);write(root/'capture_context.json',{'date':'2026-01-01','store':'wrong','issuer':'test'})
        args=SimpleNamespace(input_dir=root,replay=False,date='2026-01-01',store='test',issuer='test')
        try:assemble(args)
        except ValueError:pass
        else:raise AssertionError('Wrong subject accepted')

def jst_assembly_fixture(root, response):
    code=response['input_goods_code']
    fixture(root/'qianniu_common.xlsx',common=True,common_rows=[
        {'申请流水号':'A-1','订单编号':'O-1','开票总金额':'10','商品金额':'10','数量':'1',
         '发票类型':'全电普通发票','抬头类型':'个人','发票抬头':'购方','开票状态':'待处理'},
    ])
    write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
          'total':1,'rows':[{'serialNo':'A-1','tid':'O-1'}]})
    write(root/'order_batches.json',{'batches':[{'ids':['O-1'],'pageNum':1,
          'page':{'totalNumber':1,'totalPage':1},'order_ids':['O-1']}],
          'items':[{'order_no':'O-1','sub_order_no':'S-1','goods_code':code,'title':'商品甲','quantity':'1'}]})
    write(root/'jst_query.json',{'data':[response]})
    return SimpleNamespace(input_dir=root,output_dir=root/'output',replay=True,
                           date='2026-01-01',store='test',issuer='test')

def test_jst_assembly_preserves_raw_sku_and_rechecks_normalized_uniqueness():
    code='测试组合-【甲+乙+丙+丁】400g*4包(测试盒)'
    match={'sku_id':code.replace('(','（').replace(')','）'),'invoice_enabled':True}
    response={'input_goods_code':code,'ok':True,'match_basis':'sku_id_paren_width',
              'rows':[match],'exact_matches':[match]}
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);args=jst_assembly_fixture(root,response)
        goods=assemble(args)['jst_invoice_goods'][0]
        assert goods['sku_id']==match['sku_id'] and goods['_input_goods_code']==code
        for basis,selected,rows in [
            ('sku_id_paren_width',match,[match,{**match,'sku_id':code}]),
            ('sku_id_exact',{**match,'sku_id':code},[match,{**match,'sku_id':code}]),
            ('sku_id_paren_width',{**match,'sku_id':'错误商品'},[{**match,'sku_id':'错误商品'}]),
        ]:
            changed={**response,'match_basis':basis,'exact_matches':[selected],'rows':rows}
            write(root/'jst_query.json',{'data':[changed]})
            try:assemble(args)
            except ValueError as exc:assert '票聚' in str(exc)
            else:raise AssertionError('Contradictory or ambiguous normalized SKU accepted')
        changed={**response};changed.pop('rows')
        write(root/'jst_query.json',{'data':[changed]})
        try:assemble(args)
        except ValueError as exc:assert '缺少原始候选' in str(exc)
        else:raise AssertionError('New match evidence without raw candidates accepted')

def test_incomplete_order_pages_are_global_errors():
    page={'ids':['O1','O2'],'pageNum':1,'page':{'totalNumber':2,'totalPage':2},'order_ids':['O1']}
    try:validate_order_pages([page],['O1','O2'])
    except ValueError:pass
    else:raise AssertionError('Missing page accepted')
    second={**page,'pageNum':2,'order_ids':['O2']}
    validate_order_pages([page,second],['O1','O2'])
    try:validate_order_pages([page,second],['O1','O2','O3'])
    except ValueError:pass
    else:raise AssertionError('Unqueried order accepted')

def test_unknown_blocked_total_is_not_zero():
    assert known_total([{'invoice_total_amount':None}]) is None
    assert known_total([{'invoice_total_amount':'10.25'},{'invoice_total_amount':'2.10'}])=='12.35'

def fee_verification_fixture():
    evidence={'evidence_id':'item-1','order_no':'O-1','sub_order_no':'SO-1','goods_code':'SKU-1',
              'source_goods_name':'商品甲','quantity':'7','match_amount':'69.30'}
    source={'template_rows':[
        {'__source_row':2,'申请流水号':'A-1','订单编号':'O-1','开票总金额':'71.30',
         '货物名称':'商品甲','数量':'7','商品金额':'69.30','发票抬头':'购方'},
        {'__source_row':3,'申请流水号':'A-1','订单编号':'O-1','商品金额':'-4.00'},
        {'__source_row':4,'申请流水号':'A-1','订单编号':'O-1','货物名称':'价外费用','数量':'1','商品金额':'5.00','税率':'13%'},
        {'__source_row':5,'申请流水号':'A-1','订单编号':'O-1','货物名称':'价外费用','数量':'1','商品金额':'1.00'},
    ],'order_items':[evidence],'jst_invoice_goods':[
        {'sku_id':'SKU-1','invoice_enabled':True,'invoice_name':'开票商品甲','tax_code':'101',
         'properties_value':'规格甲','issuing_office':'件','tax_rate':'0.13'},
    ]}
    basic={key:None for key in ['buyer_tax_id','buyer_address','buyer_phone','buyer_bank','buyer_bank_account','remark','show_buyer_contact']}
    basic.update(invoice_title='购方',tax_included='是')
    plan={'invoices':[{'invoice_serial_no':'A-1','invoice_total_amount':'71.30','errors':[],
          'basic':basic,'detail_lines':[
        {'source_row':2,'order_no':'O-1','goods_code':'SKU-1','sub_order_no':'SO-1',
         'order_item_evidence':evidence,'quantity':'7','original_amount':'69.30',
         'extra_fee_amount':'6.00','extra_fee_source_rows':[4,5],'amount':'75.30',
         'item_name':'开票商品甲','tax_classification_code':'101','specification':'规格甲','unit':'件',
         'tax_rate':'0.13','discount_amount':'-4.00','discount_source_row':3},
    ]}]}
    return source,plan

def assert_source_verification_rejects(source,plan,expected):
    try:verify_sources(source,plan)
    except (ValueError,KeyError) as exc:
        assert expected in str(exc),(expected,str(exc))
    else:raise AssertionError('Invalid source mapping accepted')

def red_verification_fixture():
    source,plan=fee_verification_fixture()
    source['template_rows']=source['template_rows'][:1]
    source['template_rows'][0].update({'开票总金额':'-18.90','商品金额':'-18.90','数量':'-1',
                                      '发票类型':'全电普通发票','抬头类型':'个人'})
    source['order_items'][0].update(quantity='1',match_amount='18.90',
                                    match_amount_source='order_detail_gross')
    invoice=plan['invoices'][0]
    invoice.update(invoice_total_amount='-18.90',red_reversal=True)
    invoice['detail_lines'][0].update(invoice_serial_no='A-1',quantity='-1',original_amount='-18.90',
                                     amount='-18.90',extra_fee_source_rows=[],extra_fee_amount='0',
                                     discount_amount='0',discount_source_row=None)
    plan['selected_application_ids']=['A-1']
    return source,plan

def test_negative_invoice_is_excluded_from_both_output_sheets():
    source,expected=red_verification_fixture()
    build=rules.InvoiceBuild('A-1',[(row['__source_row'],row) for row in source['template_rows']])
    invoice=rules.build_invoice(build,{'O-1':['SKU-1']},rules.make_jst_index(source['jst_invoice_goods']),
                                {'mode':'preview','store_name':'test'},
                                rules.make_order_items_index(source['order_items']))
    assert not invoice['errors'],invoice['errors']
    plan={**expected,'invoices':[invoice]}
    assert invoice['status']=='excluded_negative'
    assert invoice['exclusion_reason']=='负数发票按规则不开具'
    assert invoice['detail_lines']==[]
    assert verify_sources(source,plan)==[]
    assert make_output_rows(plan)=={name:[] for name in SHEETS}
    assert invoice['red_reversal'] is True and source['order_items'][0]['quantity']=='1'

def excluded_verification_fixture():
    source,plan=red_verification_fixture()
    plan['invoices'][0].update(status='excluded_negative',detail_lines=[],
                               exclusion_reason='负数发票按规则不开具')
    return source,plan

def test_negative_exclusion_verifier_checks_source_total_and_empty_details():
    source,plan=excluded_verification_fixture()
    assert verify_sources(source,plan)==[]
    for value in ['18.90','0','NaN','Infinity','']:
        changed=deepcopy(source);changed['template_rows'][0]['开票总金额']=value
        assert_source_verification_rejects(changed,plan,'负数排除必须对应源表一致的负开票总金额')
    for fields,expected in [
        ({'invoice_total_amount':'-1'},'负数排除金额与源表不一致'),
        ({'detail_lines':[{}]},'负数排除不能包含商品明细或暂缓错误'),
        ({'errors':['old error']},'负数排除不能包含商品明细或暂缓错误'),
        ({'exclusion_reason':''},'负数排除缺少规则原因'),
        ({'red_reversal':False},'负数排除缺少红冲诊断标识'),
    ]:
        changed=deepcopy(plan);changed['invoices'][0].update(fields)
        assert_source_verification_rejects(source,changed,expected)
    changed=deepcopy(source)
    changed['template_rows'].append({**changed['template_rows'][0],'__source_row':3,'开票总金额':'-19.90'})
    assert_source_verification_rejects(changed,plan,'负数排除必须对应源表一致的负开票总金额')

def test_negative_invoice_cannot_be_forced_into_ready_output():
    source,plan=red_verification_fixture()
    for status in [None,'ready_for_export']:
        changed=deepcopy(plan)
        if status is not None:changed['invoices'][0]['status']=status
        assert_source_verification_rejects(source,changed,'负数发票不能进入可导出集合')
        assert make_output_rows(changed)=={name:[] for name in SHEETS}

def test_mixed_positive_and_negative_invoices_only_output_positive_rows():
    source,plan=fee_verification_fixture()
    negative_source,negative_plan=excluded_verification_fixture()
    negative=negative_plan['invoices'][0];negative['invoice_serial_no']='A-negative'
    negative_source['template_rows'][0].update({'申请流水号':'A-negative','__source_row':6})
    source['template_rows'].extend(negative_source['template_rows'])
    plan['invoices'].append(negative)
    plan['selected_application_ids']=['A-1','A-negative']
    assert verify_sources(source,plan)==[plan['invoices'][0]]
    rows=make_output_rows(plan)
    assert len(rows[SHEETS[0]])==len(rows[SHEETS[1]])==1
    assert rows[SHEETS[0]][0]['发票流水号']=='A-1'
    assert rows[SHEETS[1]][0]['金额']=='75.30'
    assert rows[SHEETS[1]][0]['折扣金额']=='-4.00'

def test_zero_tax_rate_is_explicit_and_blank_is_rejected():
    source,plan=fee_verification_fixture()
    source['jst_invoice_goods'][0]['tax_rate']='0'
    source['template_rows'][2]['税率']='0'
    plan['invoices'][0]['detail_lines'][0]['tax_rate']='0'
    plan['selected_application_ids']=['A-1']
    assert verify_sources(source,plan)==plan['invoices']
    assert make_output_rows(plan)[SHEETS[1]][0]['税率']=='0'
    for value in [None,'']:
        changed=deepcopy(plan);changed['invoices'][0]['detail_lines'][0]['tax_rate']=value
        assert_source_verification_rejects(source,changed,'税率不一致')
    plan['invoices'][0]['detail_lines'][0]['tax_rate']=0
    assert make_output_rows(plan)[SHEETS[1]][0]['税率']==0

def test_final_xlsx_preserves_zero_tax_as_required_template_text():
    """V260401 requests text input; the finalizer must never blank a real zero."""
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);template=root/'template.xlsx';authored=root/'authored.xlsx';output=root/'final.xlsx'
        fixture(template,clean=True);schema=inspect_template(template)
        name=SHEETS[1];meta=schema[name];rate_col=meta['columns']['税率']
        serial_col=meta['columns']['发票流水号']
        expected={sheet:[] for sheet in SHEETS}
        expected[name]=[
            {'发票流水号':'00012345678901234567','税率':'0'},
            {'发票流水号':'00012345678901234568','税率':'0.13'},
            {'发票流水号':'00012345678901234569','税率':''},
        ]
        with ZipFile(template) as original,ZipFile(authored,'w',ZIP_DEFLATED) as written:
            for entry in original.infolist():
                raw=original.read(entry.filename)
                if entry.filename==meta['path']:
                    xml=ET.fromstring(raw);data=xml.find(Q('sheetData'))
                    for rn,values in enumerate(expected[name],meta['header_row']+1):
                        row=ET.SubElement(data,Q('row'),r=str(rn))
                        serial=ET.SubElement(row,Q('c'),r=serial_col+str(rn),t='inlineStr')
                        ET.SubElement(ET.SubElement(serial,Q('is')),Q('t')).text=values['发票流水号']
                        if values['税率']!='':
                            # Final template input stays text even when the
                            # intermediate author supplies a numeric zero.
                            rate=ET.SubElement(row,Q('c'),r=rate_col+str(rn),t='n')
                            ET.SubElement(rate,Q('v')).text=values['税率']
                    raw=ET.tostring(xml)
                written.writestr(entry,raw)
        finalize(template,authored,output,schema)
        verify_workbook(template,output,schema,expected)
        with ZipFile(output) as z:
            xml=ET.fromstring(z.read(meta['path']))
            cells={cell.get('r'):cell for cell in xml.iter(Q('c'))}
            for offset,value in enumerate(['0','0.13'],1):
                cell=cells[rate_col+str(meta['header_row']+offset)]
                assert cell.get('t')=='inlineStr'
                assert cell.find(Q('is')).find(Q('t')).text==value
            assert rate_col+str(meta['header_row']+3) not in cells


def test_regular_discount_remains_discount_and_cannot_claim_red_reversal():
    source,plan=fee_verification_fixture()
    plan['selected_application_ids']=['A-1']
    assert verify_sources(source,plan)==plan['invoices']
    rows=make_output_rows(plan)[SHEETS[1]]
    assert len(rows)==1 and rows[0]['数量']=='7' and rows[0]['折扣金额']=='-4.00'
    changed=deepcopy(plan);changed['invoices'][0]['red_reversal']=True
    assert_source_verification_rejects(source,changed,'红冲标识与源表总金额不一致')

def test_parentheses_alias_builds_and_verifier_rejects_forged_alias():
    source,_=fee_verification_fixture()
    code='测试组合-【甲*2+乙+丙+丁】400g*5包(测试盒)'
    raw_code=code.replace('(','（').replace(')','）')
    source['template_rows'][0].update({'发票类型':'全电普通发票','抬头类型':'个人'})
    source['order_items'][0]['goods_code']=code
    source['order_goods']=[{'order_no':'O-1','goods_code':code}]
    source['jst_invoice_goods'][0].update(sku_id=raw_code,_input_goods_code=code,
                                        _match_basis='sku_id_paren_width')
    build=rules.InvoiceBuild('A-1',[(row['__source_row'],row) for row in source['template_rows']])
    invoice=rules.build_invoice(build,rules.make_order_goods_index(source['order_goods']),
                                rules.make_jst_index(source['jst_invoice_goods']),
                                {'mode':'preview','store_name':'test'},
                                rules.make_order_items_index(source['order_items']))
    assert not invoice['errors'],invoice['errors']
    plan={'invoices':[invoice]}
    assert verify_sources(source,plan)==[invoice]
    assert invoice['detail_lines'][0]['goods_code']==code
    assert source['jst_invoice_goods'][0]['sku_id']==raw_code
    for raw,basis,expected in [
        ('测试组合-另一商品（测试盒）','sku_id_paren_width','原始编码与括号宽度匹配证据不一致'),
        (raw_code,'sku_id_exact','原始编码与订单编码不一致'),
        (raw_code,'unknown','匹配依据无效'),
    ]:
        changed=deepcopy(source)
        changed['jst_invoice_goods'][0].update(sku_id=raw,_match_basis=basis)
        assert_source_verification_rejects(changed,plan,expected)

def test_fee_verifier_keeps_source_quantity_discount_and_matching_amount():
    source,plan=fee_verification_fixture()
    assert verify_sources(source,plan)==plan['invoices']
    # The matching evidence is 69.30, while the delivered amount is 75.30.
    # Fee quantities are never added to the source product's seven units.
    for field,value,expected in [
        ('quantity','9','商品数量不一致'),
        ('original_amount','75.30','原商品金额被改写'),
        ('amount','69.30','商品金额与原金额加价外费用不一致'),
        ('discount_amount','-10.00','折扣金额被改写'),
    ]:
        changed=deepcopy(plan);changed['invoices'][0]['detail_lines'][0][field]=value
        assert_source_verification_rejects(source,changed,expected)

def test_fee_verifier_rejects_missing_duplicate_and_unrelated_fee_rows():
    source,plan=fee_verification_fixture()
    changes=[
        ({'extra_fee_source_rows':[4,4,5]},'价外费用源行重复计入'),
        ({'extra_fee_source_rows':[4],'extra_fee_amount':'5','amount':'74.30'},'价外费用源行覆盖不完整'),
        ({'extra_fee_source_rows':[3,4,5]},'价外费用源行不属于本票'),
        ({'extra_fee_amount':'7'},'价外费用合计不一致'),
    ]
    for fields,expected in changes:
        changed=deepcopy(plan);changed['invoices'][0]['detail_lines'][0].update(fields)
        assert_source_verification_rejects(source,changed,expected)
    for field,value,expected in [
        ('申请流水号','A-2','价外费用源行不属于本票'),
        ('订单编号','O-2','价外费用订单归属不一致'),
        ('订单编号','','价外费用订单归属不一致'),
        ('税率','9%','价外费用税率不一致'),
        ('商品编码','SKU-2','价外费用商品标识冲突'),
        ('子订单号','SO-2','价外费用商品标识冲突'),
        ('商品金额','-5','价外费用必须为正金额'),
    ]:
        changed=deepcopy(source);changed['template_rows'][2][field]=value
        assert_source_verification_rejects(changed,plan,expected)

def test_fee_verifier_rejects_changed_suborder_even_when_fee_agrees():
    source,plan=fee_verification_fixture()
    source['template_rows'][2]['子订单号']='SO-2'
    plan['invoices'][0]['detail_lines'][0]['sub_order_no']='SO-2'
    assert_source_verification_rejects(source,plan,'计划子订单号与原始订单证据不一致')


def test_fee_verifier_rejects_ambiguous_same_order_merchandise():
    source,plan=fee_verification_fixture()
    second={**source['template_rows'][0],'__source_row':6,'商品金额':'10','数量':'1'}
    second.pop('开票总金额')
    source['template_rows'].append(second)
    plan['invoices'][0]['detail_lines'].append({**plan['invoices'][0]['detail_lines'][0],'source_row':6})
    assert_source_verification_rejects(source,plan,'价外费用无法唯一归属商品')

def test_published_common_template_is_byte_identical():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);source=root/'qianniu_common.xlsx';output=root/'output'
        fixture(source,common=True)
        output.mkdir()
        published=publish_common_template(root,output,'2026-01-01')
        assert published.name=='qianniu_common_2026-01-01.xlsx'
        assert source.read_bytes()==published.read_bytes()
        try:publish_common_template(root,output,'2026-01-01')
        except ValueError:pass
        else:raise AssertionError('Common template output was overwritten')

def test_assemble_uses_the_published_common_template_snapshot():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);source=root/'qianniu_common.xlsx';output=root/'output';output.mkdir()
        fixture(source,common=True)
        published=publish_common_template(root,output,'2026-01-01')
        fixture(source,common=True,common_rows=[
            {'申请流水号':'late-change','订单编号':'O1','开票总金额':'10.00','商品金额':'10.00','数量':'1',
             '发票类型':'全电普通发票','抬头类型':'企业','发票抬头':'购方','开票状态':'待处理'},
        ])
        write(root/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
              'total':0,'api_total':0,'observed_total':0,'rows':[],
              'list_non_pending_snapshot_rows':[]})
        args=SimpleNamespace(input_dir=root,output_dir=output,replay=True,date='2026-01-01',store='test',issuer='test')
        assert assemble(args,published)['selected_application_ids']==[]

def test_failed_run_keeps_published_common_template_and_manifest():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);input_dir=root/'input';output_dir=root/'output';input_dir.mkdir()
        source=input_dir/'qianniu_common.xlsx';fixture(source,common=True)
        previous_argv=sys.argv[:]
        sys.argv=['run_invoice.py','--date','2026-01-01','--store','test','--issuer','test',
                  '--input-dir',str(input_dir),'--output-dir',str(output_dir),'--replay',
                  '--template',str(root/'missing-template.xlsx')]
        try:
            try:run_main()
            except FileNotFoundError:pass
            else:raise AssertionError('Invalid template path accepted')
        finally:sys.argv=previous_argv
        published=output_dir/'qianniu_common_2026-01-01.xlsx'
        manifest=json.loads((output_dir/'run.json').read_text(encoding='utf-8'))
        assert published.read_bytes()==source.read_bytes()
        assert manifest['status']=='failed'
        assert manifest['common_template_output']['sha256']

def test_no_application_run_delivers_common_template_for_reconciliation():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);input_dir=root/'input';output_dir=root/'output';input_dir.mkdir()
        source=input_dir/'qianniu_common.xlsx';fixture(source,common=True)
        write(input_dir/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
              'total':0,'api_total':0,'observed_total':0,'rows':[],
              'list_non_pending_snapshot_rows':[]})
        previous_argv=sys.argv[:]
        sys.argv=['run_invoice.py','--date','2026-01-01','--store','test','--issuer','test',
                  '--input-dir',str(input_dir),'--output-dir',str(output_dir),'--replay']
        try:assert run_main()==0
        finally:sys.argv=previous_argv
        assert (output_dir/'qianniu_common_2026-01-01.xlsx').read_bytes()==source.read_bytes()
        assert (output_dir/'exceptions.csv').is_file()
        assert (output_dir/'selection.json').is_file()
        assert json.loads((output_dir/'run.json').read_text(encoding='utf-8'))['status']=='no_applications'

def test_all_negative_run_delivers_exclusion_without_tax_workbook():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);output=root/'output'
        args=jst_assembly_fixture(root,{'input_goods_code':'SKU-1','ok':True,
                                        'exact_matches':[{'sku_id':'SKU-1'}]})
        # The prefilter means a fully negative scope does not need either
        # detail checkpoint; assemble must short-circuit before loading them.
        (root/'order_batches.json').unlink()
        (root/'jst_query.json').unlink()
        fixture(root/'qianniu_common.xlsx',common=True,common_rows=[
            {'申请流水号':'A-1','订单编号':'O-1','开票总金额':'-18.90','商品金额':'-18.90','数量':'-1',
             '开票状态':'待处理'},
        ])
        previous_argv=sys.argv[:]
        sys.argv=['run_invoice.py','--date',args.date,'--store','test','--issuer','test',
                  '--input-dir',str(root),'--output-dir',str(output),'--replay']
        try:assert run_main()==0
        finally:sys.argv=previous_argv
        manifest=json.loads((output/'run.json').read_text(encoding='utf-8'))
        assert manifest['status']=='all_excluded'
        assert manifest['selected_count']==manifest['excluded_count']==1
        assert manifest['ready_count']==manifest['blocked_count']==0
        assert manifest['excluded_amount']=='-18.9'
        assert manifest['excluded_application_ids']==['A-1']
        assert '负数发票按规则不开具' in (output/'exceptions.csv').read_text(encoding='utf-8-sig')
        assert not (output/'qianniu_invoice_tax_template_2026-01-01.xlsx').exists()
        assert (output/'qianniu_common_2026-01-01.xlsx').read_bytes()==(root/'qianniu_common.xlsx').read_bytes()

def test_mismatched_selection_checkpoint_stops_after_common_template_publication():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);input_dir=root/'input';output_dir=root/'output';input_dir.mkdir()
        source=input_dir/'qianniu_common.xlsx';fixture(source,common=True)
        write(input_dir/'selection.json',{'common_template_sha256':'stale'})
        write(input_dir/'applications.json',{'date':'2026-01-01','queried_at':'2026-01-02T00:00:00Z',
              'total':0,'api_total':0,'observed_total':0,'rows':[],
              'list_non_pending_snapshot_rows':[]})
        previous_argv=sys.argv[:]
        sys.argv=['run_invoice.py','--date','2026-01-01','--store','test','--issuer','test',
                  '--input-dir',str(input_dir),'--output-dir',str(output_dir),'--replay']
        try:
            try:run_main()
            except ValueError:pass
            else:raise AssertionError('Stale selection checkpoint accepted')
        finally:sys.argv=previous_argv
        assert (output_dir/'qianniu_common_2026-01-01.xlsx').read_bytes()==source.read_bytes()
        assert json.loads((output_dir/'run.json').read_text(encoding='utf-8'))['status']=='failed'

def test_bundled_template_is_blank_and_self_contained():
    from template_io import sheet_paths,strings
    assert DEFAULT_TEMPLATE.is_file()
    schema=inspect_template(DEFAULT_TEMPLATE)
    for name,meta in schema.items():
        assert not any(any(values.values()) for rn,values in read_rows(DEFAULT_TEMPLATE,name) if rn>meta['header_row'])
    with ZipFile(DEFAULT_TEMPLATE) as z:
        paths=sheet_paths(z)
        assert any(state=='hidden' for _,state in paths.values())
        shared=strings(z);references=set()
        for part,state in paths.values():
            for cell in ET.fromstring(z.read(part)).iter(Q('c')):
                if cell.get('t')=='s':references.add(int(cell.find(Q('v')).text))
        assert references==set(range(len(shared))), 'Unused shared strings must not carry historical data'

def test_bundled_template_explicitly_requires_text_input_for_tax_rates():
    schema=inspect_template(DEFAULT_TEMPLATE)
    meta=schema[SHEETS[1]]
    instructions=[values for rn,values in read_rows(DEFAULT_TEMPLATE,SHEETS[1]) if rn<meta['header_row']]
    assert any('全部数据使用文本输入' in text for row in instructions for text in row.values())
    assert any('0.13' in row.get(meta['columns']['税率'],'') for row in instructions)


if __name__=='__main__':
    tests=[fn for name,fn in globals().copy().items() if name.startswith('test_') and callable(fn)]
    for test in tests:test()
    print(f'{len(tests)} pipeline tests passed')
