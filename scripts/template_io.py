"""Resolve template sheets/headers and preserve native XLSX parts during authoring."""
from zipfile import ZipFile, ZIP_DEFLATED
from decimal import Decimal
import posixpath
import re
from lxml import etree as ET

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
Q = lambda name: '{' + NS + '}' + name
SHEETS = ('1-发票基本信息', '2-发票明细信息', '3-特定业务信息', '4-附加要素信息')
BASIC = {'发票流水号':'invoice_serial_no','发票类型':'invoice_type','特定业务类型':'special_business_type',
         '是否含税':'tax_included','受票方自然人标识':'recipient_natural_person_indicator',
         '购买方名称':'invoice_title','购买方纳税人识别号':'buyer_tax_id','购买方地址':'buyer_address',
         '购买方电话':'buyer_phone','购买方开户银行':'buyer_bank','购买方银行账号':'buyer_bank_account',
         '是否展示购买方地址电话银行账号':'show_buyer_contact','备注':'remark'}
DETAIL = {'发票流水号':'invoice_serial_no','项目名称':'item_name','商品和服务税收编码':'tax_classification_code',
          '规格型号':'specification','单位':'unit','数量':'quantity','单价':'unit_price','金额':'amount',
          '税率':'tax_rate','折扣金额':'discount_amount'}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def sheet_paths(z):
    rels = {r.get('Id'):r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
    result = {}
    for sheet in ET.fromstring(z.read('xl/workbook.xml')).find(Q('sheets')):
        target = rels[sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
        path = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        result[sheet.get('name')] = (path, sheet.get('state','visible'))
    return result

def strings(z):
    if 'xl/sharedStrings.xml' not in z.namelist(): return []
    return [''.join(s.itertext()) for s in ET.fromstring(z.read('xl/sharedStrings.xml'))]

def cell_text(cell, shared):
    require(cell.find(Q('f')) is None, '业务数据不能包含公式: ' + str(cell.get('r')))
    val = cell.find(Q('v'))
    if cell.get('t') == 'inlineStr':
        return ''.join(cell.find(Q('is')).itertext())
    if val is None: return ''
    return shared[int(val.text)] if cell.get('t') == 's' else (val.text or '')

def rows_from_zip(z, path):
    shared = strings(z)
    return [(int(row.get('r')), {re.sub(r'\d','',c.get('r')):cell_text(c,shared)
             for c in row if c.tag == Q('c')}) for row in ET.fromstring(z.read(path)).find(Q('sheetData'))]

def read_rows(path, sheet_name=None):
    with ZipFile(path) as z:
        paths = sheet_paths(z)
        return rows_from_zip(z, paths[sheet_name or next(iter(paths))][0])

def column_number(col):
    result=0
    for char in col: result=result*26+ord(char)-64
    return result

def inspect_template(path):
    with ZipFile(path) as z:
        paths=sheet_paths(z)
        require([name for name,(_,state) in paths.items() if state=='visible']==list(SHEETS), '模板必须有约定的四张可见业务表')
        result={}
        for name in SHEETS:
            rows=rows_from_zip(z,paths[name][0])
            header_rows=[(rn,values) for rn,values in rows if rn<=10 and '发票流水号' in values.values()]
            require(len(header_rows)==1, '无法唯一定位表头: '+name)
            header,values=header_rows[0]
            required=BASIC if name==SHEETS[0] else DETAIL if name==SHEETS[1] else {'发票流水号':None}
            columns={}
            for label in required:
                matches=[col for col,text in values.items() if text.replace('\n','').strip().lstrip('*')==label]
                require(len(matches)==1, '模板字段缺失或重复: '+name+'/'+label)
                columns[label]=matches[0]
            result[name]={'path':paths[name][0],'header_row':header,'columns':columns,
                          'last_column':max(values,key=column_number)}
        return result

def replace_data(raw,data):
    result,count=re.subn(rb'<sheetData(?:\s[^>]*)?>.*?</sheetData>',lambda _:ET.tostring(data,encoding='utf-8'),raw,count=1,flags=re.S)
    require(count==1,'无法替换模板数据区')
    return result

def prepare(template,light,schema):
    """Remove data in the authoring copy; keep native styles in the original."""
    by_path={v['path']:v for v in schema.values()}
    with ZipFile(template) as original,ZipFile(light,'w',ZIP_DEFLATED) as out:
        for entry in original.infolist():
            raw=original.read(entry.filename)
            if re.fullmatch(r'xl/worksheets/[^/]+\.xml',entry.filename):
                xml=ET.fromstring(raw); data=xml.find(Q('sheetData'))
                header=by_path.get(entry.filename,{}).get('header_row',0)
                for row in list(data):
                    if int(row.get('r'))>header: data.remove(row)
                raw=replace_data(raw,data)
                if not header: raw=re.sub(rb'<dimension\s+ref="[^"]+"\s*/>',b'<dimension ref="A1"/>',raw)
            out.writestr(entry,raw)

def finalize(template,authored,output,schema):
    by_path={v['path']:(name,v) for name,v in schema.items()}
    with ZipFile(template) as original,ZipFile(authored) as written,ZipFile(output,'w',ZIP_DEFLATED) as out:
        paths=sheet_paths(written)
        for entry in original.infolist():
            raw=original.read(entry.filename)
            if entry.filename in by_path:
                name,meta=by_path[entry.filename]; header=meta['header_row']
                xml=ET.fromstring(raw); data=xml.find(Q('sheetData'))
                rows={int(r.get('r')):r for r in data}
                cells={c.get('r'):c for r in data for c in r if c.tag==Q('c')}
                for rn,row in rows.items():
                    if rn>header:
                        for cell in row:
                            cell.attrib.pop('t',None)
                            for child in list(cell):cell.remove(child)
                for rn,values in rows_from_zip(written,paths[name][0]):
                    if rn<=header:continue
                    if name in SHEETS[2:]:
                        require(not any(values.values()),'第三、四表必须为空')
                    for col,text in values.items():
                        if not text:continue
                        if rn not in rows:
                            rows[rn]=ET.SubElement(data,Q('row'),r=str(rn))
                        ref=col+str(rn); cell=cells.get(ref)
                        if cell is None:
                            cell=ET.SubElement(rows[rn],Q('c'),r=ref)
                            base=cells.get(col+str(header+1))
                            if base is not None and base.get('s'):cell.set('s',base.get('s'))
                        cell.set('t','inlineStr')
                        for child in list(cell):cell.remove(child)
                        ET.SubElement(ET.SubElement(cell,Q('is')),Q('t')).text=text
                for row in data:
                    row[:]=sorted(row,key=lambda c:column_number(re.sub(r'\d','',c.get('r'))))
                data[:]=sorted(data,key=lambda r:int(r.get('r')))
                raw=replace_data(raw,data)
            out.writestr(entry,raw)

def is_ready_for_export(invoice):
    """Explicit lifecycle status wins; status-less historical plans stay readable."""
    return not invoice['errors'] and invoice.get('status','ready_for_export')=='ready_for_export'

def make_output_rows(plan):
    require(not plan.get('fatal') and not plan.get('errors'), '全局数据错误，不能生成表格')
    ready=[i for i in plan['invoices'] if is_ready_for_export(i)]
    require(set(plan['selected_application_ids'])=={i['invoice_serial_no'] for i in plan['invoices']},'申请集合不完整')
    basic=[];details=[]
    for invoice in ready:
        amount=Decimal(invoice['invoice_total_amount'])
        require(amount.is_finite(),'申请总金额无效，不能生成表格')
        if amount<0:
            continue
        b={**invoice['basic'],'invoice_serial_no':invoice['invoice_serial_no'],'show_buyer_contact':None}
        basic.append({label:b.get(field) or '' for label,field in BASIC.items()})
        for line in invoice['detail_lines']:
            row={label:'' if line.get(field) is None else line[field] for label,field in DETAIL.items()}
            row['单价']=''
            if row['折扣金额']=='0':row['折扣金额']=''
            details.append(row)
    return {SHEETS[0]:basic,SHEETS[1]:details,SHEETS[2]:[],SHEETS[3]:[]}
