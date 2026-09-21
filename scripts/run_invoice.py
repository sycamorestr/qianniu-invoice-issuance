"""Reproducible local pipeline over saved browser responses; never submits invoices."""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import build_invoice_plan as rules
from template_io import (BASIC, DETAIL, SHEETS, inspect_template, make_output_rows,
                         prepare, finalize, read_rows, require, sheet_paths, Q, ET)

HERE=Path(__file__).resolve().parent
DEFAULT_TEMPLATE=HERE.parent/'assets'/'tax-bureau-template-V260401.xlsx'

def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,data): Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def known_total(invoices):
    values=[i.get('invoice_total_amount') for i in invoices]
    if any(v is None for v in values):return None
    return str(sum((Decimal(v) for v in values),Decimal(0)))

def read_source(path):
    raw=read_rows(path,'开票申请列表')
    header,columns=raw[0]
    required={'申请流水号','订单编号','开票总金额','商品金额','数量','发票类型','抬头类型','发票抬头'}
    require(required <= {v.lstrip('*') for v in columns.values()},'通用模板表头不完整')
    return [{**{label.lstrip('*'):values.get(col,'') for col,label in columns.items() if label},'__source_row':rn}
            for rn,values in raw[1:] if any(values.values())]

def validate_order_pages(batches,requested):
    grouped=defaultdict(list)
    for batch in batches:
        ids=batch['ids']
        require(0<len(ids)<=50 and len(ids)==len(set(ids)),'订单批次编号无效')
        grouped[tuple(ids)].append(batch)
    queried=set()
    for ids,pages in grouped.items():
        require(not queried.intersection(ids),'订单批次查询范围重复')
        queried.update(ids)
        totals={int(page['page']['totalNumber']) for page in pages}
        counts={max(1,int(page['page']['totalPage'])) for page in pages}
        require(len(totals)==len(counts)==1,'订单分页总数变化')
        count=next(iter(counts))
        require([int(page['pageNum']) for page in pages]==list(range(1,count+1)),'订单分页缺失或重复')
        found=[order for page in pages for order in page['order_ids']]
        require(len(found)==len(set(found))==next(iter(totals)) and set(found)<=set(ids),'订单分页返回集合不完整')
    require(queried==set(requested),'尚有订单未完成批量查询')

def assemble(args):
    root=args.input_dir
    if not args.replay:
        context=load(root/'capture_context.json')
        require(context.get('date')==args.date and context.get('store')==args.store and context.get('issuer')==args.issuer,
                '本次页面主体记录与运行参数不一致')
        require(bool(context.get('verified_at')) and bool(context.get('invoice_url')) and bool(context.get('jst_url')),
                '缺少本次页面主体核对证据')
    rows=read_source(root/'qianniu_common.xlsx')
    applications=load(root/'applications.json')
    require(applications.get('date')==args.date,'申请列表日期与本次日期不一致')
    live=applications['rows']
    require(len(live)==applications['total']==len({r['serialNo'] for r in live}),'申请列表分页不完整或重复')
    require(all(r.get('applyStatus')==1 for r in live),'列表含非待处理或未知申请状态，需重新采集')
    require(bool(applications.get('queried_at')),'申请列表缺少采集时间')
    selected=[r['serialNo'] for r in live]
    exported={r['申请流水号'] for r in rows}
    require(set(selected)<=exported,'通用模板遗漏已选申请')
    by_serial={r['serialNo']:r for r in live}
    for row in rows:
        app=by_serial.get(row['申请流水号'])
        if app:
            require(str(app['tid'])==row['订单编号'],'申请列表与通用模板订单不一致')
            require(row['申请状态']=='待处理','通用模板与待处理申请快照不一致')
    # Source order is authoritative, independent of list-page ordering.
    selected=[s for s in dict.fromkeys(r['申请流水号'] for r in rows) if s in by_serial]
    if not selected:
        return {'run':{'mode':'preview','store_name':args.store},'selected_application_ids':[],
                'template_rows':rows,'order_items':[],'order_goods':[],'jst_invoice_goods':[]}
    batches=load(root/'order_batches.json')
    requested={r['订单编号'] for r in rows if r['申请流水号'] in by_serial}
    validate_order_pages(batches['batches'],requested)
    items=list(batches['items'])
    old=load(root/'old_details.json') if (root/'old_details.json').exists() else []
    for order in old:
        require(order.get('verified_order') is True,'历史订单详情未核对订单号')
        items.extend(order['items'])
    normalized=[];seen=set()
    for index,item in enumerate(items):
        order=str(item['order_no']); sub=str(item.get('sub_order_no') or '')
        key=(order,sub) if sub else (order,'detail',index)
        require(key not in seen,'重复订单明细: '+str(key));seen.add(key)
        normalized.append({'order_no':order,'sub_order_no':sub or None,'goods_code':item['goods_code'],
                           'source_goods_name':item['title'],'quantity':item['quantity'],
                           'source_specification':item.get('specification',''),
                           'evidence_id':f'order-item-{index+1}'})
    evidence=load(root/'match_evidence.json') if (root/'match_evidence.json').exists() else []
    for e in evidence:
        candidates=[i for i in normalized if i['order_no']==str(e['order_no']) and i['goods_code']==e['goods_code']
                    and (not e.get('sub_order_no') or i['sub_order_no']==e['sub_order_no'])]
        require(len(candidates)==1,'金额证据必须唯一关联订单明细')
        require(bool(e.get('source')),'金额证据缺少来源说明')
        amount_source=e.get('match_amount_source')
        # Explicit migration for the previously verified 9.19 evidence schema.
        if amount_source is None and e['source']=='订单详情同一商品行的单价乘数量':
            amount_source='order_detail_gross'
        require(amount_source in {'order_detail_gross','promotion_detail_gross'},'金额证据优惠口径未验证')
        candidates[0].update(match_amount=e['source_amount'],match_amount_source=amount_source,amount_evidence=e)
    jst=load(root/'jst_query.json')
    codes={i['goods_code'] for i in normalized if i['goods_code']}
    responses=jst['data'];requested=[r['input_goods_code'] for r in responses]
    require(len(requested)==len(set(requested)) and codes<=set(requested),'票聚查询集合不完整或重复')
    require(not any(r.get('reason')=='request_failed' for r in responses),'票聚请求失败，需补查后再生成')
    goods=[]
    for r in responses:
        if r['ok']:
            require(len(r['exact_matches'])==1 and r['exact_matches'][0]['sku_id']==r['input_goods_code'],'票聚精确匹配证据无效')
            goods.extend(r['exact_matches'])
    return {'run':{'mode':'preview','run_id':args.output_dir.name,'apply_date_range':{'start':args.date,'end':args.date},
                   'store_name':args.store,'issuer_name':args.issuer,'application_snapshot_at':applications['queried_at']},
            'selected_application_ids':selected,'template_rows':rows,'order_items':normalized,
            'order_goods':[{'order_no':i['order_no'],'goods_code':i['goods_code']} for i in normalized],
            'jst_invoice_goods':goods}

def build_plan(source,output):
    save(output/'invoice_input.json',source)
    result=subprocess.run([sys.executable,str(HERE/'build_invoice_plan.py'),str(output/'invoice_input.json'),
                           '--output',str(output/'invoice_plan.json')],check=False)
    require(result.returncode in {0,1},'计划构建失败')
    plan=load(output/'invoice_plan.json')
    require(not plan['fatal'] and not plan['errors'],'全局数据错误: '+str(plan['errors']))
    require(len(plan['invoices'])==len(source['selected_application_ids']),'申请集合不完整')
    return plan

def verify_sources(source,plan):
    """Check each mapped line against independent raw normalized evidence."""
    raw={r['__source_row']:r for r in source['template_rows']}
    ready=[i for i in plan['invoices'] if not i['errors']]
    for invoice in ready:
        used=set(); serial=invoice['invoice_serial_no'];total=Decimal(0)
        selected_rows=[r for r in source['template_rows'] if r['申请流水号']==serial]
        for field,key in [('invoice_title','发票抬头'),('buyer_tax_id','购方税号'),('buyer_address','企业地址'),
                          ('buyer_phone','企业电话'),('buyer_bank','开户行'),('buyer_bank_account','开户账号'),('remark','发票备注')]:
            values={r.get(key,'').strip() for r in selected_rows if r.get(key,'').strip()}
            require(len(values)<=1 and (invoice['basic'][field] or '')==(next(iter(values)) if values else ''),
                    '购买方字段与源表不一致: '+key)
        require(invoice['basic']['show_buyer_contact'] is None and invoice['basic']['tax_included']=='是','基本信息固定值不正确')
        positives=[r['__source_row'] for r in selected_rows if Decimal(r['商品金额'])>0]
        require(positives==[l['source_row'] for l in invoice['detail_lines']],'正商品源行覆盖不完整')
        for line in invoice['detail_lines']:
            row=raw[line['source_row']]; evidence=line.get('order_item_evidence')
            require(evidence is not None,'缺少子订单匹配证据')
            require(evidence in source['order_items'],'订单证据不在本次采集集合')
            require(evidence['evidence_id'] not in used,'同一申请重复使用订单明细')
            used.add(evidence['evidence_id'])
            require(evidence['order_no']==row['订单编号'] and evidence['goods_code']==line['goods_code'],'订单商品关联错误')
            require(rules.normalized_match_text(row['货物名称'])==rules.normalized_match_text(evidence['source_goods_name']),'商品标题不一致')
            require(Decimal(row['数量'])==Decimal(evidence['quantity'])==Decimal(line['quantity']),'商品数量不一致')
            require(Decimal(row['商品金额'])==Decimal(line['amount']),'商品金额被改写')
            source_spec=rules.normalized_match_text(rules.value(row,'source_specification'))
            item_spec=rules.normalized_match_text(evidence.get('source_specification'))
            require(not source_spec or not item_spec or source_spec==item_spec,'订单规格不一致')
            if evidence.get('match_amount') is not None:
                require(Decimal(evidence['match_amount'])==Decimal(row['商品金额']),'订单核对金额不一致')
            goods=[g for g in source['jst_invoice_goods'] if g['sku_id']==line['goods_code']]
            require(len(goods)==1 and goods[0]['invoice_enabled'] is True,'票聚商品映射无效')
            g=goods[0]
            for key,field in [('item_name','invoice_name'),('tax_classification_code','tax_code'),('specification','properties_value'),('unit','issuing_office')]:
                require((line[key] or '')==(g.get(field) or '').strip(),'票聚字段来源不一致: '+field)
            rate=g.get('tax_rate')
            if rate is None:
                category=g.get('vc_name','')
                if category=='零税率':rate=Decimal(0)
                else:
                    match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*%\s*(?:税率)?',category)
                    require(match is not None,'无法独立核对税率')
                    rate=Decimal(match[1])/100
            else: rate=Decimal(str(rate))
            require(line['tax_rate']==(None if rate==0 else rules.decimal_text(rate)),'税率不一致')
            discount=Decimal(line['discount_amount']); dr=line['discount_source_row']
            if dr is not None:
                require(dr==line['source_row']+1 and raw[dr]['申请流水号']==serial,'折扣源行归属不正确')
                require(discount==Decimal(raw[dr]['商品金额']),'折扣金额被改写')
            else: require(discount==0,'折扣没有源行')
            total+=Decimal(line['amount'])+discount
        require(total==Decimal(invoice['invoice_total_amount']),'逐票金额不平')
        source_totals={Decimal(r['开票总金额']) for r in selected_rows if r.get('开票总金额')}
        require(source_totals=={total},'源表申请总金额与明细合计不一致')
    return ready

def verify_workbook(template,output,schema,expected):
    for name,meta in schema.items():
        rows=[values for rn,values in read_rows(output,name) if rn>meta['header_row'] and any(values.values())]
        require(len(rows)==len(expected[name]),'工作表行数不一致: '+name)
        for actual,exp in zip(rows,expected[name]):
            desired={meta['columns'][label]:str(value) for label,value in exp.items() if value!=''}
            require({k:v for k,v in actual.items() if v}==desired,'工作表存在缺漏、错值或多余数据: '+name)
    with ZipFile(template) as a,ZipFile(output) as b:
        require(a.namelist()==b.namelist(),'工作簿部件集合发生变化')
        allowed={v['path'] for v in schema.values()}
        changed=[n for n in a.namelist() if a.read(n)!=b.read(n)]
        require(set(changed)<=allowed,'隐藏字典或模板样式部件被改动')
        for meta in schema.values():
            old=ET.fromstring(a.read(meta['path']));new=ET.fromstring(b.read(meta['path']))
            # Headers and all worksheet metadata must remain identical.
            for root in (old,new):
                data=root.find(Q('sheetData'))
                for row in list(data):
                    if int(row.get('r'))>meta['header_row']:data.remove(row)
            require(ET.tostring(old,method='c14n',exclusive=True)==ET.tostring(new,method='c14n',exclusive=True),
                    '表头或模板元数据发生变化')
    return changed

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True);p.add_argument('--store',required=True);p.add_argument('--issuer',required=True)
    p.add_argument('--input-dir',required=True,type=Path);p.add_argument('--output-dir',required=True,type=Path)
    p.add_argument('--template',type=Path,default=DEFAULT_TEMPLATE,help='可选；默认使用skill内置V260401税局模板')
    p.add_argument('--node',type=Path);p.add_argument('--node-modules',type=Path)
    p.add_argument('--plan-only',action='store_true')
    p.add_argument('--replay',action='store_true',help='离线重放历史快照，不声明当前页面状态')
    args=p.parse_args();date.fromisoformat(args.date)
    args.output_dir.mkdir(parents=True,exist_ok=False)
    manifest={'date':args.date,'store':args.store,'issuer':args.issuer,'started_at':datetime.now(timezone.utc).isoformat(),
              'stage':'inputs','status':'running','replay':args.replay,'inputs':{}}
    try:
        for name in ['capture_context.json','qianniu_common.xlsx','applications.json','order_batches.json','old_details.json','match_evidence.json','jst_query.json']:
            path=args.input_dir/name
            if path.exists():manifest['inputs'][name]={'path':str(path.resolve()),'sha256':sha(path)}
        manifest['template_sha256']=sha(args.template)
        manifest['template_path']=str(args.template.resolve())
        source=assemble(args);plan=build_plan(source,args.output_dir)
        ready=verify_sources(source,plan)
        blocked=[i for i in plan['invoices'] if i['errors']]
        require(len(source['selected_application_ids'])==len(ready)+len(blocked),'申请数量不平')
        manifest.update(stage='plan',status='validated',selected_count=len(source['selected_application_ids']),ready_count=len(ready),blocked_count=len(blocked),
                        ready_amount=known_total(ready),blocked_amount=known_total(blocked))
        with (args.output_dir/'exceptions.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['申请流水号','金额','暂缓原因'])
            for invoice in blocked:writer.writerow([invoice['invoice_serial_no'],invoice['invoice_total_amount'],'；'.join(invoice['errors'])])
        if args.plan_only or not ready:
            manifest['status']='plan_only' if args.plan_only else 'no_applications' if not plan['invoices'] else 'all_blocked'
        else:
            require(args.node and args.node_modules,'生成表格需传入 bundled --node 和 --node-modules')
            schema=inspect_template(args.template);expected=make_output_rows(plan)
            save(args.output_dir/'payload.json',{'schema':schema,'rows':expected})
            light=args.output_dir/'authoring-template.xlsx';authored=args.output_dir/'authored.xlsx'
            prepare(args.template,light,schema)
            env={**os.environ,'INVOICE_NODE_MODULES':str(args.node_modules.resolve())}
            result=subprocess.run([str(args.node),str(HERE/'render_invoice_template.mjs'),str(light),str(args.output_dir/'payload.json'),str(authored)],env=env,check=False)
            require(result.returncode==0 and authored.exists(),'Artifact Tool 写表失败')
            pending=args.output_dir/'invoice.pending.xlsx'
            finalize(args.template,authored,pending,schema)
            changed=verify_workbook(args.template,pending,schema,expected)
            final=args.output_dir/f'qianniu_invoice_tax_template_{args.date}.xlsx'
            pending.rename(final)
            manifest.update(stage='verified',status='complete',output=str(final.resolve()),output_sha256=sha(final),
                            detail_rows=len(expected[SHEETS[1]]),changed_parts=changed,
                            visual_review='未执行图像渲染；原模板样式、表头及其他部件校验通过')
    except Exception as exc:
        manifest.update(status='failed',error=str(exc));raise
    finally:
        save(args.output_dir/'run.json',manifest)
    print(json.dumps(manifest,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
