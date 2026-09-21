"""Prepare order requests and merge immutable browser checkpoints."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from run_invoice import read_source, load, save, validate_order_pages
from template_io import require

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',choices=['orders','merge-orders','merge-jst'])
    p.add_argument('--run-dir',required=True,type=Path)
    p.add_argument('--part',action='append',type=Path,default=[])
    args=p.parse_args();root=args.run_dir
    if args.stage=='orders':
        apps=load(root/'applications.json');live={a['serialNo']:a for a in apps['rows']}
        require(len(live)==apps['total']==len(apps['rows']),'申请分页不完整')
        rows=read_source(root/'qianniu_common.xlsx')
        require(set(live)<={r['申请流水号'] for r in rows},'导出缺申请')
        orders=list(dict.fromkeys(r['订单编号'] for r in rows if r['申请流水号'] in live))
        save(root/'order_ids.json',{'orders':orders})
        print(f'{len(orders)} orders; submit batches of at most 50')
    elif args.stage=='merge-orders':
        require(bool(args.part),'需要明确指定本次批次文件')
        batches=[];items=[];seen=set()
        for path in args.part:
            part=load(path);batches.extend(part['batches'])
            for item in part['items']:
                key=(item['order_no'],item['sub_order_no'])
                require(key not in seen,'重复子订单，检查批次文件');seen.add(key);items.append(item)
        requested=load(root/'order_ids.json')['orders'];found={i['order_no'] for i in items}
        validate_order_pages(batches,requested)
        require(found<=set(requested),'返回未请求订单')
        save(root/'order_batches.json',{'queried_at':datetime.now(timezone.utc).isoformat(),'batches':batches,'items':items,
                                        'missing':[o for o in requested if o not in found]})
        old=load(root/'old_details.json') if (root/'old_details.json').exists() else []
        all_items=items+[i for order in old for i in order['items']]
        save(root/'goods_codes.json',{'codes':list(dict.fromkeys(i['goods_code'] for i in all_items if i['goods_code']))})
        print(f'{len(found)} orders found; {len(set(requested)-found)} need historical detail lookup')
    else:
        require(bool(args.part),'需要明确指定本次批次文件，重试文件按时间顺序在后')
        results={}
        for path in args.part:
            for item in load(path)['data']:
                code=item['input_goods_code']
                if code in results:require(not results[code]['ok'],'成功商品不得被重试结果覆盖')
                results[code]=item
        codes=load(root/'goods_codes.json')['codes']
        require(set(codes)==set(results),'票聚采集未覆盖完整编码集合')
        failures=[results[c] for c in codes if not results[c]['ok']]
        save(root/'jst_query.json',{'queried_at':datetime.now(timezone.utc).isoformat(),'requested_count':len(codes),
                                  'mapped_count':len(codes)-len(failures),'ok':not failures,'failures':failures,
                                  'data':[results[c] for c in codes]})
        print(f'{len(codes)} queried; {len(failures)} unmatched or failed')

if __name__=='__main__':main()
