"""Prepare order requests and merge immutable browser checkpoints."""
import argparse
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from run_invoice import (context_digest, load, read_source, save, select_template_rows, sha,
                         validate_order_pages, negative_application_ids)
from template_io import require
from order_evidence import merge_order_details

def save_checkpoint(path, data):
    """Never replace a successful deterministic selection checkpoint."""
    if path.exists():
        require(load(path)==data,f'检查点已存在且内容不同: {path.name}；请使用新运行目录')
        return
    atomic_save(path,data)

def atomic_save(path, data):
    """Publish JSON through a sibling partial file and an atomic rename."""
    path=Path(path)
    partial=path.with_name(path.name+'.partial')
    if partial.exists():
        partial.unlink()
    save(partial,data)
    partial.replace(path)

def record_parts_manifest(root, stage, paths, publish=True):
    """Record immutable part hashes so a rerun can reuse successful parts."""
    manifest_path=root/'parts_manifest.json'
    manifest=load(manifest_path) if manifest_path.exists() else {'version':1,'stages':{}}
    entries=manifest.setdefault('stages',{}).setdefault(stage,[])
    known={(entry.get('path'),entry.get('sha256')) for entry in entries}
    for path in paths:
        path=Path(path)
        require(path.is_file(),f'批次文件不存在: {path}')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        key=(str(path.resolve()),digest)
        require(not any(entry.get('path')==key[0] and entry.get('sha256')!=digest
                        for stage_entries in manifest['stages'].values() for entry in stage_entries),
                f'已登记批次文件内容变化: {path.name}')
        if key not in known:
            entries.append({'path':str(path.resolve()),'name':path.name,'sha256':digest,
                            'recorded_at':datetime.now(timezone.utc).isoformat()})
            known.add(key)
    if publish:
        atomic_save(manifest_path,manifest)
    return manifest

def read_part(path, stage):
    """Sidecar receipts and accidental path globs are never business batches."""
    path=Path(path)
    require('.receipt.' not in path.name.lower(),'回执文件不得作为采集批次: '+path.name)
    part=load(path)
    require(isinstance(part,dict),'批次格式必须是对象: '+path.name)
    fields=('batches','items') if stage=='orders' else ('data',)
    require(all(isinstance(part.get(key),list) for key in fields),'批次结构无效: '+path.name)
    require(not ({'input_sha256','output_sha256'} <= set(part) or
                 {'inputSha256','outputPath','sha256'} <= set(part)),
            '回执结构不得作为采集批次: '+path.name)
    return part

def validate_order_items(items, requested):
    """Reject incomplete order rows before their codes reach the JST stage."""
    seen=set()
    for item in items:
        order=str(item.get('order_no') or '')
        sub=str(item.get('sub_order_no') or '')
        require(order in requested,'订单明细包含未请求订单: '+order)
        require(sub,'订单明细缺少子订单号: '+order)
        key=(order,sub)
        require(key not in seen,'重复子订单，检查批次文件: '+str(key));seen.add(key)
        require(str(item.get('goods_code') or '').strip(),'订单明细缺少商品编码: '+str(key))
        require(str(item.get('title') or '').strip(),'订单明细缺少商品标题: '+str(key))
        try: quantity=Decimal(str(item.get('quantity') or ''))
        except InvalidOperation: quantity=None
        require(quantity is not None and quantity.is_finite() and quantity>0,'订单明细数量无效: '+str(key))

def selection_binding(checkpoint):
    return {key:checkpoint[key] for key in ('common_template_sha256','selection_sha256','context_sha256') if key in checkpoint}

def validate_selection_scope(root, checkpoint, label):
    binding=selection_binding(checkpoint)
    if not binding:
        return binding
    selection_path=root/'selection.json'
    require(selection_path.is_file(),f'{label} 绑定了选择范围但缺少 selection.json')
    selection=load(selection_path)
    require(binding.get('common_template_sha256')==selection.get('common_template_sha256'),
            f'{label} 与通用模板选择范围不一致')
    require(binding.get('selection_sha256')==sha(selection_path),f'{label} 与 selection.json 不一致')
    if binding.get('context_sha256'):
        context_path=root/'capture_context.json'
        require(context_path.is_file(),f'{label} 缺少页面主体上下文')
        require(binding['context_sha256']==context_digest(load(context_path)),f'{label} 与页面主体上下文不一致')
    return binding

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',choices=['orders','merge-orders','merge-jst'])
    p.add_argument('--run-dir',required=True,type=Path)
    p.add_argument('--part',action='append',type=Path,default=[])
    args=p.parse_args();root=args.run_dir
    if args.stage=='orders':
        apps=load(root/'applications.json');live_rows=apps['rows']
        live_ids=[row.get('serialNo') for row in live_rows]
        require(all(live_ids) and len(live_ids)==len(set(live_ids)),'申请列表流水号重复或为空')
        current_snapshot='list_non_pending_snapshot_rows' in apps
        diagnostics=apps.get('list_non_pending_snapshot_rows',apps.get('excluded_non_pending_rows',[]))
        diagnostic_ids=[row.get('serialNo') for row in diagnostics]
        require(all(diagnostic_ids) and len(diagnostic_ids)==len(set(diagnostic_ids)),
                '申请列表非待处理快照记录重复或为空')
        live={row['serialNo']:row for row in live_rows}
        if current_snapshot:
            require(set(diagnostic_ids)<=set(live_ids),'申请列表非待处理快照不属于观察行')
        else:
            for app in diagnostics: live.setdefault(app['serialNo'],app)
        require(len(live)==apps.get('observed_total',apps['total']),'申请分页不完整')
        rows=read_source(root/'qianniu_common.xlsx')
        pending,ignored,selection=select_template_rows(rows,root/'qianniu_common.xlsx')
        selection_path=root/'selection.json';save_checkpoint(selection_path,selection)
        for row in pending:
            app=live.get(row['申请流水号'])
            if app: require(str(app['tid'])==str(row['订单编号']),'申请列表与通用模板订单不一致')
        selected_source_rows=[row for row in rows if row.get('申请流水号') in selection['selected_application_ids']]
        excluded_negative=negative_application_ids(selected_source_rows)
        active_pending=[row for row in pending if row['申请流水号'] not in excluded_negative]
        orders=list(dict.fromkeys(r['订单编号'] for r in active_pending))
        context_path=root/'capture_context.json'
        context_hash=context_digest(load(context_path)) if context_path.exists() else None
        order_ids={
            'common_template_sha256':selection['common_template_sha256'],
            'selection_sha256':sha(selection_path),
            'orders':orders,
            'selected_application_ids':selection['selected_application_ids'],
            'active_application_ids':[serial for serial in selection['selected_application_ids']
                                     if serial not in excluded_negative],
            'excluded_negative_application_ids':[serial for serial in selection['selected_application_ids']
                                                 if serial in excluded_negative],
            'ignored_template_rows':selection['ignored_template_rows'],
        }
        if context_hash: order_ids['context_sha256']=context_hash
        save_checkpoint(root/'order_ids.json',order_ids)
        if not pending:
            print('0 orders; 0 pending applications; no order or Jst query required')
            return
        if not active_pending:
            print(f"0 orders; {len(excluded_negative)} negative applications excluded before order query; no order or Jst query required")
            return
        print(f"{len(orders)} orders; {len(selection['selected_application_ids'])} pending applications ({len(excluded_negative)} negative excluded); submit batches of at most 50")
    elif args.stage=='merge-orders':
        require(bool(args.part),'需要明确指定本次批次文件')
        batches=[];items=[];seen=set()
        for path in args.part:
            part=read_part(path,'orders');batches.extend(part['batches'])
            for item in part['items']:
                key=(item['order_no'],item['sub_order_no'])
                require(key not in seen,'重复子订单，检查批次文件');seen.add(key);items.append(item)
        order_ids=load(root/'order_ids.json');binding=validate_selection_scope(root,order_ids,'订单清单')
        requested=order_ids['orders'];found={i['order_no'] for i in items}
        validate_order_pages(batches,requested)
        validate_order_items(items, set(requested))
        require(found<=set(requested),'返回未请求订单')
        all_items,_,detail_orders=merge_order_details(root,items,requested)
        validate_order_items(all_items, set(requested))
        manifest=record_parts_manifest(root,'orders',args.part,publish=False)
        atomic_save(root/'order_batches.json',{**binding,'queried_at':datetime.now(timezone.utc).isoformat(),'batches':batches,'items':items,
                                               'missing':[o for o in requested if o not in found]})
        atomic_save(root/'goods_codes.json',{**binding,'codes':list(dict.fromkeys(i['goods_code'] for i in all_items if i['goods_code']))})
        atomic_save(root/'parts_manifest.json',manifest)
        unresolved=set(requested)-found-detail_orders
        print(f'{len(found)} orders found in batches; {len(detail_orders-found)} resolved by details; {len(unresolved)} unresolved')
    else:
        require(bool(args.part),'需要明确指定本次批次文件，重试文件按时间顺序在后')
        results={}
        for path in args.part:
            for item in read_part(path,'jst')['data']:
                code=item['input_goods_code']
                if code in results:require(not results[code]['ok'],'成功商品不得被重试结果覆盖')
                results[code]=item
        goods_codes=load(root/'goods_codes.json');binding=validate_selection_scope(root,goods_codes,'商品编码清单');codes=goods_codes['codes']
        require(set(codes)==set(results),'票聚采集未覆盖完整编码集合')
        failures=[results[c] for c in codes if not results[c]['ok']]
        manifest=record_parts_manifest(root,'jst',args.part,publish=False)
        atomic_save(root/'jst_query.json',{**binding,'queried_at':datetime.now(timezone.utc).isoformat(),'requested_count':len(codes),
                                           'mapped_count':len(codes)-len(failures),'ok':not failures,'failures':failures,
                                           'data':[results[c] for c in codes]})
        atomic_save(root/'parts_manifest.json',manifest)
        print(f'{len(codes)} queried; {len(failures)} unmatched or failed')

if __name__=='__main__':main()
