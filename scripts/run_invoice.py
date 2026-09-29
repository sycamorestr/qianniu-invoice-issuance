"""Reproducible local pipeline over saved browser responses; never submits invoices."""
import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

import build_invoice_plan as rules
from invoice_scope import make_scope, resolve_scope, scope_from_record, scope_label, scope_fields, scope_date_range
from invoice_tax_policy import normalize_policy, resolve_tax_rate_policy
from order_evidence import merge_order_details, derive_amount_evidence, attach_amount_evidence
from template_io import (BASIC, DETAIL, SHEETS, inspect_template, make_output_rows,
                         is_ready_for_export, prepare, finalize, read_rows, require, sheet_paths, Q, ET)

HERE=Path(__file__).resolve().parent
DEFAULT_TEMPLATE=HERE.parent/'assets'/'tax-bureau-template-V260401.xlsx'

def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,data): Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def context_digest(context):
    """Stable hash for the verified page/store/user主体 context."""
    payload={key:value for key,value in context.items() if key!='context_sha256'}
    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def paren_width_key(value):
    """Only the observed ASCII/full-width parentheses are interchangeable."""
    return str(value or '').replace('（','(').replace('）',')')

def template_status(row):
    """Read the export status with the documented blank-value fallback."""
    primary=str(row.get('开票状态') or '').strip()
    return primary or str(row.get('申请状态') or '').strip()


def negative_application_ids(rows):
    """Return ids whose source totals are consistently finite negative.

    An application may span several source rows. Classify it as negative only
    when every row has the same parseable finite ``开票总金额`` below zero;
    missing, invalid, or conflicting totals remain active for normal checks.
    """
    grouped=defaultdict(list)
    for row in rows:
        serial=str(row.get('申请流水号') or '').strip()
        if serial:
            grouped[serial].append(row)
    excluded=set()
    for serial, group in grouped.items():
        raw_values=[str(row.get('开票总金额') or '').strip() for row in group]
        if not raw_values or any(not value for value in raw_values):
            continue
        try:
            values=[Decimal(value.replace(',', '')) for value in raw_values]
        except InvalidOperation:
            continue
        if all(value.is_finite() for value in values) and len(set(values))==1 and values[0] < 0:
            excluded.add(serial)
    return excluded

def validate_application_snapshot(applications):
    """Validate the full observed membership without treating status as a filter."""
    live=applications['rows']
    current_snapshot='list_non_pending_snapshot_rows' in applications
    diagnostics=applications.get('list_non_pending_snapshot_rows',applications.get('excluded_non_pending_rows',[]))
    api_total=applications.get('api_total',applications['total'])
    observed_total=applications.get('observed_total',applications['total'])
    require(isinstance(live,list) and isinstance(diagnostics,list),'申请列表结构无效')
    require(type(api_total) is int and api_total>=0 and type(observed_total) is int and observed_total>=0,
            '申请列表计数无效')
    live_ids=[row.get('serialNo') for row in live]
    require(all(live_ids) and len(live_ids)==len(set(live_ids)),'申请列表流水号重复或为空')
    diagnostic_ids=[row.get('serialNo') for row in diagnostics]
    require(all(diagnostic_ids) and len(diagnostic_ids)==len(set(diagnostic_ids)),
            '申请列表非待处理快照记录重复或为空')
    observed={row['serialNo']:row for row in live}
    if current_snapshot:
        require(set(diagnostic_ids)<=set(live_ids),'申请列表非待处理快照不属于观察行')
    else:
        for row in diagnostics:
            observed.setdefault(row['serialNo'],row)
    require(observed_total==len(observed),'申请列表分页不完整或重复')
    require(bool(applications.get('queried_at')),'申请列表缺少采集时间')
    return observed


def select_template_rows(rows, common_template, query_scope=None, *, applications_path=None):
    """Select pending export rows, intersecting verified countdown membership."""
    pending=[];ignored=[];status_counts=Counter()
    for row in rows:
        status=template_status(row)
        status_counts[status or '(空白)']+=1
        if status=='待处理':
            require(bool(str(row.get('申请流水号') or '').strip()),'待处理源行缺少申请流水号')
            pending.append(row)
        else:
            ignored.append(row)
    countdown_audit={}
    if query_scope is not None and 'countdown' in query_scope:
        require(query_scope.get('countdown')=='started','开票倒计时筛选仅支持 started')
        require(applications_path is not None and Path(applications_path).is_file(),
                '开票倒计时筛选缺少完整申请列表证据')
        applications=load(applications_path)
        require(scope_from_record(applications)==query_scope,'开票倒计时筛选与申请列表查询范围不一致')
        validate_application_snapshot(applications)
        require(all(type(applications.get(key)) is int and applications[key]>=0
                    for key in ('total','api_total','observed_total')),
                '开票倒计时申请列表缺少有效完整计数')
        require(applications['observed_total']==applications['total'] and
                applications['observed_total']>=applications['api_total'],
                '开票倒计时申请列表分页不完整')
        # The export endpoint currently ignores rightsRemainTime. Use the
        # complete filtered list's membership, never applyStatus/remainTime.
        eligible={row['serialNo'] for row in applications['rows']}
        raw_pending=pending
        pending=[row for row in raw_pending if row['申请流水号'] in eligible]
        excluded=[row for row in raw_pending if row['申请流水号'] not in eligible]
        countdown_audit={
            'query_scope':dict(query_scope),
            'applications_sha256':sha(applications_path),
            'raw_pending_source_row_count':len(raw_pending),
            'raw_pending_application_ids':list(dict.fromkeys(row['申请流水号'] for row in raw_pending)),
            'filtered_out_countdown_application_ids':list(dict.fromkeys(row['申请流水号'] for row in excluded)),
            'filtered_out_countdown_rows':[
                {'source_row':row['__source_row'],'serialNo':row['申请流水号'],'status':template_status(row)}
                for row in excluded
            ],
        }
    selected=list(dict.fromkeys(row['申请流水号'] for row in pending))
    selection={
        'common_template':'qianniu_common.xlsx',
        'common_template_sha256':sha(common_template),
        'source_row_count':len(rows),
        'status_rule':{'primary_column':'开票状态','fallback_column':'申请状态','selected_value':'待处理'},
        'status_counts':dict(sorted(status_counts.items())),
        'pending_source_row_count':len(pending),
        'selected_application_ids':selected,
        'ignored_template_rows':[
            {'source_row':row['__source_row'],'serialNo':row.get('申请流水号',''),'status':template_status(row)}
            for row in ignored
        ],
    }
    # Preserve old checkpoint bytes. New selection hashes bind both the
    # untouched broad export and the complete countdown-filtered list.
    selection.update(countdown_audit)
    return pending,ignored,selection

def validate_selection_binding(checkpoint, selection, selection_path, label):
    """Reject a batch checkpoint that was prepared for another export scope."""
    binding_keys=('common_template_sha256','selection_sha256','context_sha256')
    if not any(key in checkpoint for key in binding_keys):
        return
    require(selection_path.is_file(),f'{label} 绑定了选择范围但缺少 selection.json')
    require(checkpoint.get('common_template_sha256')==selection['common_template_sha256'],
            f'{label} 与通用模板选择范围不一致')
    require(checkpoint.get('selection_sha256')==sha(selection_path),
            f'{label} 与 selection.json 不一致')
    context_hash=checkpoint.get('context_sha256')
    if context_hash:
        context_path=selection_path.parent/'capture_context.json'
        require(context_path.is_file(),f'{label} 缺少页面主体上下文')
        context=load(context_path)
        require(context_hash==context_digest(context),f'{label} 与页面主体上下文不一致')

def publish_common_template(input_dir, output_dir, invoice_date):
    """Publish an immutable, byte-checked copy for manual reconciliation."""
    source=Path(input_dir)/'qianniu_common.xlsx'
    target=Path(output_dir)/f'qianniu_common_{invoice_date}.xlsx'
    partial=target.with_suffix(target.suffix+'.partial')
    require(source.is_file(),'缺少通用模板原始文件')
    require(not target.exists(),'通用模板输出已存在，需使用新输出目录')
    require(not partial.exists(),'通用模板临时副本已存在，需使用新输出目录')
    try:
        shutil.copyfile(source,partial)
        require(sha(source)==sha(partial),'通用模板副本哈希不一致')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target

def known_total(invoices):
    values=[i.get('invoice_total_amount') for i in invoices]
    if any(v is None for v in values):return None
    return str(sum((Decimal(v) for v in values),Decimal(0)))

def read_source(path):
    raw=read_rows(path,'开票申请列表')
    header,columns=raw[0]
    required={'申请流水号','订单编号','开票总金额','商品金额','数量','发票类型','抬头类型','发票抬头'}
    labels={v.lstrip('*').strip() for v in columns.values() if v}
    require(required <= labels,'通用模板表头不完整')
    require(bool({'开票状态','申请状态'}&labels),'通用模板缺少开票状态或申请状态列')
    return [{**{label.lstrip('*').strip():values.get(col,'') for col,label in columns.items() if label},'__source_row':rn}
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

def scope_from_args(args):
    """Inherit both date ranges and filters from the collected snapshot."""
    all_pending=getattr(args,'all_pending',False)
    saved=None
    for name in ('capture_context.json','applications.json'):
        path=args.input_dir/name
        if path.is_file():
            candidate=load(path)
            if name=='capture_context.json' and not candidate.get('date') and 'query_scope' not in candidate:
                continue
            saved=candidate
            break
    if all_pending:
        require(saved is not None,'全部待处理生成缺少已采集的查询范围')
    return resolve_scope(args.date,all_pending,saved=saved)

def assemble(args, common_template=None):
    root=args.input_dir
    policy_file=getattr(args,'tax_rate_policy_file',None)
    policy_config=getattr(args,'tax_rate_config',None)
    require(not (policy_file and policy_config),'税率配置与冻结策略不能同时指定')
    tax_policy=(normalize_policy(load(policy_file)) if policy_file
                else resolve_tax_rate_policy(args.store,policy_config))
    scope=scope_from_args(args)
    context_path=root/'capture_context.json'
    if 'countdown' in scope:
        require(context_path.is_file(),'开票倒计时筛选缺少页面查询范围证据')
        require(scope_from_record(load(context_path))==scope,'开票倒计时筛选与页面查询范围不一致')
    if not args.replay or context_path.exists():
        context=load(root/'capture_context.json')
        # Historical replay contexts can contain identity only. Explicit scope
        # evidence still binds the replay and must never be ignored.
        if not args.replay or context.get('date') or 'query_scope' in context:
            require(scope_from_record(context)==scope,'页面查询范围与运行参数不一致')
    if not args.replay:
        require(context.get('store')==args.store and context.get('issuer')==args.issuer,
                '本次页面主体记录与运行参数不一致')
        require(bool(context.get('verified_at')) and bool(context.get('invoice_url')) and bool(context.get('jst_url')),
                '缺少本次页面主体核对证据')
        if context.get('context_sha256'):
            require(context['context_sha256']==context_digest(context),'页面主体上下文哈希不一致')
    common_template=Path(common_template or root/'qianniu_common.xlsx')
    rows=read_source(common_template)
    pending_rows,ignored_rows,selection=select_template_rows(rows,common_template,scope,
                                                             applications_path=root/'applications.json')
    selection_path=root/'selection.json'
    if selection_path.exists():
        require(load(selection_path)==selection,'通用模板选择检查点与原始文件不一致')
    applications=load(root/'applications.json')
    require(scope_from_record(applications)==scope,'申请列表查询范围与本次范围不一致')
    observed=validate_application_snapshot(applications)
    selected=selection['selected_application_ids']
    selected_source_rows=[row for row in rows if row.get('申请流水号') in selection['selected_application_ids']]
    excluded_negative=negative_application_ids(selected_source_rows)
    active_pending_rows=[row for row in pending_rows if row['申请流水号'] not in excluded_negative]
    requested_orders=list(dict.fromkeys(row['订单编号'] for row in active_pending_rows))
    order_ids_path=root/'order_ids.json'
    if order_ids_path.exists():
        order_ids=load(order_ids_path)
        validate_selection_binding(order_ids,selection,selection_path,'订单清单')
        require(order_ids.get('selected_application_ids')==selected,'订单清单申请范围与通用模板不一致')
        expected_active=[serial for serial in selected if serial not in excluded_negative]
        expected_excluded=[serial for serial in selected if serial in excluded_negative]
        if 'active_application_ids' in order_ids:
            require(order_ids.get('active_application_ids')==expected_active,'订单清单活跃申请范围与通用模板不一致')
        if 'excluded_negative_application_ids' in order_ids:
            require(order_ids.get('excluded_negative_application_ids')==expected_excluded,
                    '订单清单负数排除范围与通用模板不一致')
        require(order_ids.get('orders')==requested_orders,'订单清单与通用模板订单范围不一致')
    by_serial=observed
    for row in pending_rows:
        app=by_serial.get(row['申请流水号'])
        if app:
            require(str(app['tid'])==row['订单编号'],'申请列表与通用模板订单不一致')
    # Source order is authoritative, independent of list-page ordering.
    if not selected:
        empty_run={'mode':'preview','store_name':args.store,'tax_rate_policy':tax_policy}
        if scope_fields(scope):
            empty_run.update(**scope_fields(scope),apply_date_range=scope_date_range(scope))
        return {'run':empty_run,'selected_application_ids':[],
                'template_rows':pending_rows,'ignored_template_rows':ignored_rows,
                'order_items':[],'order_goods':[],'jst_invoice_goods':[],'selection':selection}
    # Negative applications stay in the selected scope for plan accounting,
    # but do not need order or JST detail checkpoints. This permits an
    # all-negative run to finish without creating empty query files.
    if not active_pending_rows:
        return {'run':{'mode':'preview','run_id':args.output_dir.name,
                       'apply_date_range':scope_date_range(scope),**scope_fields(scope),
                       'store_name':args.store,'issuer_name':args.issuer,'tax_rate_policy':tax_policy,
                       'application_snapshot_at':applications['queried_at']},
                'selected_application_ids':selected,'template_rows':pending_rows,
                'ignored_template_rows':ignored_rows,'order_items':[],'order_goods':[],
                'jst_invoice_goods':[],'selection':selection}
    batches=load(root/'order_batches.json')
    validate_selection_binding(batches,selection,selection_path,'订单批次')
    requested=set(requested_orders)
    validate_order_pages(batches['batches'],requested)
    items,detail_evidence,detail_orders=merge_order_details(root,batches['items'],requested)
    batch_missing={str(order) for order in batches.get('missing',[]) if order}
    require(batch_missing<=detail_orders,'订单批次缺少历史详情补查: '+','.join(sorted(batch_missing-detail_orders)))
    normalized=[];seen=set()
    for index,item in enumerate(items):
        order=str(item['order_no']); sub=str(item.get('sub_order_no') or '')
        require(order in requested,'订单明细包含未请求订单: '+order)
        require(bool(sub),'订单明细缺少子订单号: '+order)
        key=(order,sub)
        require(key not in seen,'重复订单明细: '+str(key));seen.add(key)
        goods_code=str(item.get('goods_code') or '').strip()
        title=str(item.get('title') or '').strip()
        # Retain missing-code rows: the planner blocks their whole invoice,
        # while unrelated complete orders can still be generated.
        require(title,'订单明细缺少商品标题: '+str(key))
        try: quantity=Decimal(str(item.get('quantity') or ''))
        except InvalidOperation: quantity=None
        require(quantity is not None and quantity.is_finite() and quantity>0,'订单明细数量无效: '+str(key))
        normalized.append({'order_no':order,'sub_order_no':sub,'goods_code':goods_code,
                           'source_goods_name':title,'quantity':str(item['quantity']),
                           'source_specification':item.get('specification',''),
                           'evidence_id':f'order-item-{index+1}'})
    derived_evidence,evidence_diagnostics=derive_amount_evidence(detail_evidence,active_pending_rows)
    evidence=load(root/'match_evidence.json') if (root/'match_evidence.json').exists() else []
    attach_amount_evidence(normalized,[*evidence,*derived_evidence])
    codes={i['goods_code'] for i in normalized if i['goods_code']}
    jst=load(root/'jst_query.json') if codes or (root/'jst_query.json').exists() else {'data':[]}
    validate_selection_binding(jst,selection,selection_path,'票聚批次')
    responses=jst['data'];requested=[r['input_goods_code'] for r in responses]
    require(len(requested)==len(set(requested)) and codes<=set(requested),'票聚查询集合不完整或重复')
    require(not any(r.get('reason')=='request_failed' for r in responses),'票聚请求失败，需补查后再生成')
    goods=[]
    for r in responses:
        if r['ok']:
            require(len(r['exact_matches'])==1,'票聚精确匹配证据无效')
            match=r['exact_matches'][0]
            basis=r.get('match_basis','sku_id_exact')
            if basis=='sku_id_exact':
                require(match.get('sku_id')==r['input_goods_code'],'票聚精确匹配证据无效')
            elif basis=='sku_id_paren_width':
                require(paren_width_key(match.get('sku_id'))==paren_width_key(r['input_goods_code']),'票聚括号宽度规范化匹配证据无效')
            else:
                require(False,'票聚精确匹配依据无效')
            if 'match_basis' in r:
                # Recompute uniqueness from the saved raw response, rather than
                # trusting the collector's selected exact_matches list.
                require(isinstance(r.get('rows'),list),'票聚匹配证据缺少原始候选')
                candidates=[row for row in r['rows']
                            if paren_width_key(row.get('sku_id'))==paren_width_key(r['input_goods_code'])]
                require(len(candidates)==1 and candidates[0]==match,'票聚规范化候选不唯一或匹配证据被改写')
            goods.append({**match,'_input_goods_code':r['input_goods_code'],'_match_basis':basis})
    return {'run':{'mode':'preview','run_id':args.output_dir.name,'apply_date_range':scope_date_range(scope),**scope_fields(scope),
                   'store_name':args.store,'issuer_name':args.issuer,'tax_rate_policy':tax_policy,
                   'application_snapshot_at':applications['queried_at']},
            'selected_application_ids':selected,'template_rows':pending_rows,'order_items':normalized,
            'order_goods':[{'order_no':i['order_no'],'goods_code':i['goods_code']} for i in normalized],
            'jst_invoice_goods':goods,'selection':selection,
            'derived_match_evidence':derived_evidence,'match_evidence_diagnostics':evidence_diagnostics}

def build_plan(source,output):
    save(output/'invoice_input.json',source)
    result=subprocess.run([sys.executable,str(HERE/'build_invoice_plan.py'),str(output/'invoice_input.json'),
                           '--output',str(output/'invoice_plan.json')],check=False)
    require(result.returncode in {0,1},'计划构建失败')
    plan=load(output/'invoice_plan.json')
    query_scope=source.get('run',{}).get('query_scope')
    if isinstance(query_scope,dict):
        plan.update(**scope_fields(query_scope),apply_date_range=scope_date_range(query_scope))
        save(output/'invoice_plan.json',plan)
    require(not plan['fatal'] and not plan['errors'],'全局数据错误: '+str(plan['errors']))
    require(len(plan['invoices'])==len(source['selected_application_ids']),'申请集合不完整')
    return plan

def verify_sources(source,plan):
    """Check each mapped line against independent raw normalized evidence."""
    source_run=source.get('run') or {}
    policy=normalize_policy(source_run.get('tax_rate_policy'))
    fixed_rate=policy['source']=='fixed'
    if 'tax_rate_policy' in plan:
        require(normalize_policy(plan['tax_rate_policy'])==policy,'计划税率策略与输入不一致')
    raw={r['__source_row']:r for r in source['template_rows']}
    ready=[i for i in plan['invoices'] if is_ready_for_export(i)]
    for invoice in plan['invoices']:
        if invoice.get('status')!='excluded_negative':
            continue
        selected_rows=[r for r in source['template_rows'] if r['申请流水号']==invoice['invoice_serial_no']]
        source_totals={Decimal(r['开票总金额']) for r in selected_rows if r.get('开票总金额')}
        require(len(source_totals)==1 and all(amount.is_finite() and amount<0 for amount in source_totals),
                '负数排除必须对应源表一致的负开票总金额')
        require(source_totals=={Decimal(invoice['invoice_total_amount'])},'负数排除金额与源表不一致')
        require(invoice.get('red_reversal') is True,'负数排除缺少红冲诊断标识')
        require(invoice.get('detail_lines')==[] and invoice.get('errors')==[],
                '负数排除不能包含商品明细或暂缓错误')
        require(invoice.get('exclusion_reason')=='负数发票按规则不开具','负数排除缺少规则原因')
    for invoice in ready:
        used=set(); used_fees=set(); serial=invoice['invoice_serial_no'];total=Decimal(0)
        selected_rows=[r for r in source['template_rows'] if r['申请流水号']==serial]
        source_totals={Decimal(r['开票总金额']) for r in selected_rows if r.get('开票总金额')}
        require(len(source_totals)==1 and all(amount.is_finite() for amount in source_totals),
                '源表申请总金额缺失、冲突或无效')
        require(next(iter(source_totals))>=0,'负数发票不能进入可导出集合')
        require(invoice.get('red_reversal',False) is False,'红冲标识与源表总金额不一致')
        amounts={r['__source_row']:Decimal(r['商品金额']) for r in selected_rows}
        require(all(amount.is_finite() and amount!=0 for amount in amounts.values()),'源商品金额无效')
        fee_rows={r['__source_row']:r for r in selected_rows
                  if rules.clean_text(rules.value(r,'source_goods_name'))=='价外费用'}
        require(all(amounts[rn]>0 for rn in fee_rows),'价外费用必须为正金额')
        for field,key in [('invoice_title','发票抬头'),('buyer_tax_id','购方税号'),('buyer_address','企业地址'),
                          ('buyer_phone','企业电话'),('buyer_bank','开户行'),('buyer_bank_account','开户账号'),('remark','发票备注')]:
            values={r.get(key,'').strip() for r in selected_rows if r.get(key,'').strip()}
            require(len(values)<=1 and (invoice['basic'][field] or '')==(next(iter(values)) if values else ''),
                    '购买方字段与源表不一致: '+key)
        require(invoice['basic']['show_buyer_contact'] is None and invoice['basic']['tax_included']=='是','基本信息固定值不正确')
        positives=[r['__source_row'] for r in selected_rows if amounts[r['__source_row']]>0 and r['__source_row'] not in fee_rows]
        require(positives==[l['source_row'] for l in invoice['detail_lines']],'正商品源行覆盖不完整')
        for line in invoice['detail_lines']:
            row=raw[line['source_row']]; evidence=line.get('order_item_evidence')
            require(evidence is not None,'缺少子订单匹配证据')
            require(evidence in source['order_items'],'订单证据不在本次采集集合')
            require(evidence['evidence_id'] not in used,'同一申请重复使用订单明细')
            used.add(evidence['evidence_id'])
            require(evidence['order_no']==row['订单编号'] and evidence['goods_code']==line['goods_code'],'订单商品关联错误')
            require(rules.clean_text(line.get('sub_order_no'))==rules.clean_text(evidence.get('sub_order_no')),
                    '计划子订单号与原始订单证据不一致')
            require(rules.normalized_match_text(row['货物名称'])==rules.normalized_match_text(evidence['source_goods_name']),'商品标题不一致')
            source_quantity=Decimal(row['数量'])
            require(source_quantity==Decimal(line['quantity']),'商品数量不一致')
            require(source_quantity==Decimal(evidence['quantity']),'商品数量不一致')
            original=Decimal(row['商品金额'])
            require(Decimal(line['original_amount'])==original,'原商品金额被改写')
            fee_refs=line['extra_fee_source_rows']
            require(isinstance(fee_refs,list) and all(type(rn) is int for rn in fee_refs),
                    '价外费用源行记录无效')
            require(len(fee_refs)==len(set(fee_refs)) and not used_fees.intersection(fee_refs),
                    '价外费用源行重复计入')
            fee_total=Decimal(0)
            for rn in fee_refs:
                require(rn in fee_rows,'价外费用源行不属于本票')
                fee=fee_rows[rn]
                order_no=rules.clean_text(fee.get('订单编号'))
                require(order_no is not None and order_no==rules.clean_text(row.get('订单编号')),
                        '价外费用订单归属不一致')
                targets=[n for n in positives if rules.clean_text(raw[n].get('订单编号'))==order_no]
                require(targets==[line['source_row']],'价外费用无法唯一归属商品')
                for field,expected in [('goods_code',line['goods_code']),('sub_order_no',line.get('sub_order_no'))]:
                    explicit=rules.clean_text(rules.value(fee,field))
                    require(explicit is None or explicit==expected,'价外费用商品标识冲突')
                fee_total+=amounts[rn]
            used_fees.update(fee_refs)
            require(Decimal(line['extra_fee_amount'])==fee_total,'价外费用合计不一致')
            require(Decimal(line['amount'])==original+fee_total,'商品金额与原金额加价外费用不一致')
            source_spec=rules.normalized_match_text(rules.value(row,'source_specification'))
            item_spec=rules.normalized_match_text(evidence.get('source_specification'))
            require(not source_spec or not item_spec or source_spec==item_spec,'订单规格不一致')
            if evidence.get('match_amount') is not None:
                require(Decimal(evidence['match_amount'])==original,'订单核对金额不一致')
            goods=[g for g in source['jst_invoice_goods']
                   if g.get('_input_goods_code',g.get('sku_id'))==line['goods_code']]
            require(len(goods)==1 and goods[0]['invoice_enabled'] is True,'票聚商品映射无效')
            g=goods[0]
            basis=g.get('_match_basis','sku_id_exact')
            raw_code=g.get('sku_id')
            if basis=='sku_id_exact':
                require(raw_code==line['goods_code'],'票聚原始编码与订单编码不一致')
            elif basis=='sku_id_paren_width':
                require(paren_width_key(raw_code)==paren_width_key(line['goods_code']),
                        '票聚原始编码与括号宽度匹配证据不一致')
            else:
                require(False,'票聚匹配依据无效')
            for key,field in [('item_name','invoice_name'),('tax_classification_code','tax_code'),('specification','properties_value'),('unit','issuing_office')]:
                require((line[key] or '')==(g.get(field) or '').strip(),'票聚字段来源不一致: '+field)
                if key!='specification':
                    require(bool((g.get(field) or '').strip()),'票聚必需字段为空: '+field)
            if fixed_rate:
                rate=Decimal(policy['rate'])
                expected_rate_source='store_fixed'
            else:
                raw_rate=rules.value(g,'tax_rate')
                category=rules.clean_text(rules.value(g,'virtual_category')) or ''
                if rules.clean_text(raw_rate) is None:
                    expected_rate_source='jst_virtual_category'
                    if category=='零税率':rate=Decimal(0)
                    else:
                        match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*%\s*(?:税率)?',category)
                        require(match is not None,'无法独立核对税率')
                        rate=Decimal(match[1])/100
                else:
                    expected_rate_source='jst_tax_rate'
                    try:rate=Decimal(str(raw_rate))
                    except InvalidOperation:raise ValueError('无法独立核对税率') from None
                require(rate.is_finite() and Decimal(0)<=rate<=Decimal(1),'票聚税率必须为0到1的有限小数')
                require(category!='零税率' or rate==0,'票聚零税率分类与显式税率冲突')
                source_rate=rules.value(row,'tax_rate')
                if rules.clean_text(source_rate) is not None:
                    errors=[]
                    require(rules.parse_tax_rate(source_rate,errors)==rate and not errors,'商品源行税率不一致')
            require(line['tax_rate']==rules.decimal_text(rate),'税率不一致')
            if fixed_rate or 'tax_rate_policy' in source_run or 'tax_rate_source' in line or 'tax_rate_effective' in line:
                require(line.get('tax_rate_source')==expected_rate_source,'税率来源不一致')
                require(line.get('tax_rate_effective')==rules.decimal_text(rate),'有效税率不一致')
            for rn in fee_refs:
                fee_rate=rules.value(fee_rows[rn],'tax_rate')
                if not fixed_rate and rules.clean_text(fee_rate) is not None:
                    errors=[]
                    require(rules.parse_tax_rate(fee_rate,errors)==rate and not errors,'价外费用税率不一致')
            discount=Decimal(line['discount_amount']); dr=line['discount_source_row']
            if dr is not None:
                require(dr==line['source_row']+1 and raw[dr]['申请流水号']==serial,'折扣源行归属不正确')
                require(discount==Decimal(raw[dr]['商品金额']),'折扣金额被改写')
                discount_rate=rules.value(raw[dr],'tax_rate')
                if not fixed_rate and rules.clean_text(discount_rate) is not None:
                    errors=[]
                    require(rules.parse_tax_rate(discount_rate,errors)==rate and not errors,'折扣税率不一致')
            else: require(discount==0,'折扣没有源行')
            total+=Decimal(line['amount'])+discount
        require(used_fees==set(fee_rows),'价外费用源行覆盖不完整')
        require(total==Decimal(invoice['invoice_total_amount']),'逐票金额不平')
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
    query=p.add_mutually_exclusive_group(required=True)
    query.add_argument('--date')
    query.add_argument('--all-pending',action='store_true',help='使用采集时冻结的最近两个月范围，按通用模板待处理状态选择')
    p.add_argument('--store',required=True);p.add_argument('--issuer',required=True)
    tax=p.add_mutually_exclusive_group()
    tax.add_argument('--tax-rate-config',type=Path,help='按店税率配置；默认使用skill根tax-rates.json')
    tax.add_argument('--tax-rate-policy-file',type=Path,help=argparse.SUPPRESS)
    p.add_argument('--input-dir',required=True,type=Path);p.add_argument('--output-dir',required=True,type=Path)
    p.add_argument('--template',type=Path,default=DEFAULT_TEMPLATE,help='可选；默认使用skill内置V260401税局模板')
    p.add_argument('--node',type=Path);p.add_argument('--node-modules',type=Path)
    p.add_argument('--plan-only',action='store_true')
    p.add_argument('--replay',action='store_true',help='离线重放历史快照，不声明当前页面状态')
    args=p.parse_args()
    label='all-pending' if args.all_pending else scope_label(make_scope(args.date))
    args.output_dir.mkdir(parents=True,exist_ok=False)
    manifest={'date':args.date,'store':args.store,'issuer':args.issuer,'started_at':datetime.now(timezone.utc).isoformat(),
              'stage':'inputs','status':'running','replay':args.replay,'inputs':{}}
    try:
        # Publish the browser-exported original before any downstream input or
        # template check so even a failed build remains manually reconcilable.
        common_output=publish_common_template(args.input_dir,args.output_dir,label)
        manifest['common_template_output']={'path':str(common_output.resolve()),'sha256':sha(common_output)}
        scope=scope_from_args(args)
        manifest.update(**scope_fields(scope))
        context_path=args.input_dir/'capture_context.json'
        if context_path.exists():
            context=load(context_path)
            manifest['context_sha256']=context_digest(context)
        for name in ['capture_context.json','qianniu_common.xlsx','applications.json','selection.json','order_batches.json','old_details.json','supplemental_details.json','match_evidence.json','jst_query.json']:
            path=args.input_dir/name
            if path.exists():
                manifest['inputs'][name]={'path':str(path.resolve()),
                                           'sha256':manifest['common_template_output']['sha256'] if name=='qianniu_common.xlsx' else sha(path)}
        manifest['template_sha256']=sha(args.template)
        manifest['template_path']=str(args.template.resolve())
        source=assemble(args,common_output)
        manifest['tax_rate_policy']=source['run']['tax_rate_policy']
        save(args.output_dir/'derived_match_evidence.json',{
            'evidence':source.get('derived_match_evidence',[]),
            'diagnostics':source.get('match_evidence_diagnostics',[])})
        save(args.output_dir/'selection.json',source['selection'])
        manifest['selection']=source['selection']
        plan=build_plan(source,args.output_dir)
        ready=verify_sources(source,plan)
        blocked=[i for i in plan['invoices'] if i['errors']]
        excluded=[i for i in plan['invoices'] if i.get('status')=='excluded_negative']
        require(len(source['selected_application_ids'])==len(ready)+len(blocked)+len(excluded),'申请数量不平')
        manifest.update(stage='plan',status='validated',selected_count=len(source['selected_application_ids']),ready_count=len(ready),blocked_count=len(blocked),
                        ready_amount=known_total(ready),blocked_amount=known_total(blocked),
                        excluded_count=len(excluded),excluded_amount=known_total(excluded),
                        excluded_application_ids=[i['invoice_serial_no'] for i in excluded])
        with (args.output_dir/'exceptions.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['申请流水号','金额','暂缓原因'])
            for invoice in blocked:writer.writerow([invoice['invoice_serial_no'],invoice['invoice_total_amount'],'；'.join(invoice['errors'])])
            for invoice in excluded:writer.writerow([invoice['invoice_serial_no'],invoice['invoice_total_amount'],invoice['exclusion_reason']])
        if args.plan_only or not ready:
            manifest['status']=('plan_only' if args.plan_only else 'no_applications' if not plan['invoices']
                                else 'all_excluded' if len(excluded)==len(plan['invoices']) else 'all_blocked')
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
            final=args.output_dir/f'qianniu_invoice_tax_template_{label}.xlsx'
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
