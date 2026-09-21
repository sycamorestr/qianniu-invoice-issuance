"""Synthetic OOXML fixtures exercise native preservation and pipeline failure gates."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED
from lxml import etree as ET

from template_io import (NS,Q,SHEETS,BASIC,DETAIL,inspect_template,prepare,finalize,
                         read_rows,make_output_rows)
from run_invoice import verify_workbook,assemble,validate_order_pages,known_total,DEFAULT_TEMPLATE

HERE=Path(__file__).parent
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'

def col(number):
    result=''
    while number:number,remainder=divmod(number-1,26);result=chr(65+remainder)+result
    return result

def fixture(path,clean=False,common=False):
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
            labels=(['申请流水号','订单编号','开票总金额','商品金额','数量','发票类型','抬头类型','发票抬头'] if common
                    else list(reversed(BASIC)) if index==0 else list(reversed(DETAIL)) if index==1 else ['发票流水号'])
            header=1 if common else 5
            row=ET.SubElement(data,Q('row'),r=str(header))
            for n,label in enumerate(labels,1):
                c=ET.SubElement(row,Q('c'),r=col(n)+str(header),t='inlineStr',s='1')
                ET.SubElement(ET.SubElement(c,Q('is')),Q('t')).text=label
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

def test_wrong_capture_subject_stops_before_processing():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);write(root/'capture_context.json',{'date':'2026-01-01','store':'wrong','issuer':'test'})
        args=SimpleNamespace(input_dir=root,replay=False,date='2026-01-01',store='test',issuer='test')
        try:assemble(args)
        except ValueError:pass
        else:raise AssertionError('Wrong subject accepted')

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

if __name__=='__main__':
    tests=[fn for name,fn in globals().copy().items() if name.startswith('test_') and callable(fn)]
    for test in tests:test()
    print(f'{len(tests)} pipeline tests passed')
