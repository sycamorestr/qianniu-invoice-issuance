"""Merge saved detail evidence by suborder and derive conservative gross matches.

No network access, no realTotal inference, and no changes to source checkpoints.
"""
import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import build_invoice_plan as rules
from template_io import require


def number(raw):
    try:
        value=Decimal(str(raw).replace(',', ''))
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def text(raw):
    """The old collector's JS object string is absent evidence, not a spec."""
    value=rules.normalized_match_text(raw)
    return None if value and '[objectobject]' in value else value


def specification_text(raw):
    """Compare list-shaped SKU specs with the detail page's text form.

    The sold-order API returns ``[{name, value}]`` while the detail DOM
    collector returns strings such as ``name:value``.  They are the same
    source fact, so canonicalize both forms before conflict checks.
    """
    if isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                name = item.get('name') or item.get('label')
                value = item.get('value')
                if name is not None and value is not None:
                    parts.append(f'{name}:{value}')
                elif value is not None:
                    parts.append(str(value))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        raw = ' '.join(parts)
    value = text(raw)
    return value.replace(':', '').replace('：', '') if value else value


def comparable(raw, field):
    return specification_text(raw) if field == 'specification' else text(raw)


def item_key(item):
    return (str(item.get('order_no') or ''), str(item.get('sub_order_no') or ''))


def merge_order_details(root, batch_items, requested):
    """Return unique items plus provenance from old and supplemental details.

    Batch duplicates and duplicates within one detail response are corrupt.
    Cross-response overlap is idempotent only when all meaningful fields agree.
    """
    requested=set(requested);merged={};details=[];detail_orders=set()
    for item in batch_items:
        key=item_key(item)
        require(key not in merged, '重复订单明细: '+str(key))
        merged[key]=deepcopy(item)
    for name in ('old_details.json','supplemental_details.json'):
        path=Path(root)/name
        if not path.exists():
            continue
        raw=path.read_bytes();documents=json.loads(raw.decode('utf-8-sig'))
        require(isinstance(documents,list),name+' 必须是详情数组')
        digest=hashlib.sha256(raw).hexdigest()
        for document in documents:
            order=str(document.get('order_no') or '')
            require(document.get('verified_order') is True,'订单详情未核对订单号')
            require(order in requested,'订单详情包含未请求订单: '+order)
            require(isinstance(document.get('items'),list),'订单详情缺少明细数组')
            detail_orders.add(order);seen=set()
            for item in document['items']:
                key=item_key(item)
                require(key[0]==order and key[1], '订单详情子订单标识缺失或订单不一致: '+str(key))
                quantity=number(item.get('quantity'))
                require(quantity is not None and quantity>0,'订单详情数量无效: '+str(key))
                require(bool(text(item.get('goods_code'))) and bool(text(item.get('title'))),
                        '订单详情缺少商品编码或标题: '+str(key))
                require(key not in seen,'同一详情响应重复子订单: '+str(key));seen.add(key)
                if key in merged:
                    previous=merged[key]
                    for field in ('goods_code','title','specification','quantity'):
                        a=number(previous.get(field)) if field=='quantity' else comparable(previous.get(field), field)
                        b=number(item.get(field)) if field=='quantity' else comparable(item.get(field), field)
                        require(a is None or b is None or a==b,
                                f'订单列表与详情字段冲突: {key} {field}')
                        if a is None and b is not None:
                            previous[field]=deepcopy(item[field])
                else:
                    merged[key]=deepcopy(item)
                details.append({'item':deepcopy(item),'document':document,'checkpoint':name,
                                'checkpoint_sha256':digest})
    return list(merged.values()),details,detail_orders


def compatible(row, item):
    if str(rules.value(row,'order_no') or '')!=str(item.get('order_no') or ''):
        return False
    for field, item_field in (('goods_code','goods_code'),('sub_order_no','sub_order_no'),
                              ('source_goods_name','title'),('source_specification','specification')):
        a=comparable(rules.value(row,field), 'specification' if item_field == 'specification' else field)
        b=comparable(item.get(item_field), 'specification' if item_field == 'specification' else field)
        if a and b and a!=b:
            return False
    return number(rules.value(row,'quantity'))==number(item.get('quantity'))


def derive_amount_evidence(details, source_rows):
    """Prove a unique positive source row from detail unit price × quantity.

    Require an attributable detail URL, capture time, exact raw price-cell
    structure and positive operands. Equal-price candidates stay ambiguous.
    A discount/net price never becomes gross merely because it is available.
    """
    candidates={};diagnostics=[]
    for detail in details:
        item=detail['item'];document=detail['document'];key=item_key(item)
        price=number(item.get('unit_price'));quantity=number(item.get('quantity'))
        url=document.get('url','');parsed=urlparse(url)
        url_order=parse_qs(parsed.query).get('bizOrderId',[])
        raw_match=re.fullmatch(r'\s*[¥￥]?\s*((?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)\s*[xX×]\s*([0-9]+(?:\.[0-9]+)?)\s*',
                               str(item.get('price_cell') or ''))
        valid=(item.get('source')=='order_detail_dom' and parsed.hostname in {'qn.taobao.com','trade.taobao.com'}
               and url_order==[key[0]] and bool(document.get('queried_at'))
               and price is not None and price>0 and quantity is not None and quantity>0
               and raw_match and number(raw_match[1])==price and number(raw_match[2])==quantity)
        if not valid:
            diagnostics.append({'order_no':key[0],'sub_order_no':key[1],
                                'reason':'detail_gross_source_incomplete','checkpoint':detail['checkpoint']})
            continue
        gross=price*quantity
        if key in candidates:
            require(candidates[key]['amount']==gross,'重复详情的单价或数量金额证据冲突: '+str(key))
            continue
        candidates[key]={'item':item,'detail':detail,'amount':gross}
    positives=[row for row in source_rows if number(rules.value(row,'goods_amount')) is not None
               and number(rules.value(row,'goods_amount'))>0
               and rules.clean_text(rules.value(row,'source_goods_name'))!='价外费用']
    matched=[]
    for row in positives:
        matches=[(key,c) for key,c in candidates.items() if compatible(row,c['item'])
                 and rules.money_equal(c['amount'],number(rules.value(row,'goods_amount')))]
        if len(matches)==1:
            matched.append((row,*matches[0]))
        elif matches:
            diagnostics.append({'source_row':rules.value(row,'source_row'),
                                'reason':'detail_gross_multiple_candidates'})
    matched_keys={key for row,key,c in matched}
    for key in candidates.keys()-matched_keys:
        diagnostics.append({'order_no':key[0],'sub_order_no':key[1],
                            'reason':'detail_gross_no_unique_source_match'})
    uses=Counter((str(rules.value(row,'application_no')),key) for row,key,c in matched)
    evidence=[]
    for row,key,candidate in matched:
        if uses[(str(rules.value(row,'application_no')),key)]!=1:
            diagnostics.append({'source_row':rules.value(row,'source_row'),
                                'reason':'detail_gross_reused_within_application'})
            continue
        item=candidate['item'];detail=candidate['detail'];document=detail['document']
        evidence.append({'order_no':key[0],'sub_order_no':key[1],'goods_code':item['goods_code'],
                         'source_amount':str(candidate['amount']),'match_amount_source':'order_detail_gross',
                         'source':'详情原始价格单元格单价×数量，与通用模板正商品金额唯一一致；折扣单独保留',
                         'url':document['url'],'queried_at':document['queried_at'],
                         'checkpoint':detail['checkpoint'],'checkpoint_sha256':detail['checkpoint_sha256'],
                         'price_cell':item['price_cell'],'unit_price':str(item['unit_price']),
                         'quantity':str(item['quantity']),'calculation':str(item['unit_price'])+' × '+str(item['quantity']),
                         'application_no':str(rules.value(row,'application_no')),
                         'source_row':rules.value(row,'source_row')})
    return evidence,diagnostics


def attach_amount_evidence(normalized, evidence):
    """Reject conflict instead of allowing later manual/derived evidence to win."""
    for e in evidence:
        candidates=[i for i in normalized if i['order_no']==str(e['order_no']) and i['goods_code']==e['goods_code']
                    and (not e.get('sub_order_no') or i['sub_order_no']==str(e['sub_order_no']))]
        require(len(candidates)==1,'金额证据必须唯一关联订单明细')
        require(bool(e.get('source')),'金额证据缺少来源说明')
        amount_source=e.get('match_amount_source')
        if amount_source is None and e['source']=='订单详情同一商品行的单价乘数量':
            amount_source='order_detail_gross'
        require(amount_source in {'order_detail_gross','promotion_detail_gross'},'金额证据优惠口径未验证')
        amount=number(e.get('source_amount'))
        require(amount is not None and amount>0,'金额证据必须是有效正数')
        item=candidates[0]
        if 'match_amount' in item:
            require(number(item['match_amount'])==amount,'同一子订单金额证据冲突')
            continue
        item.update(match_amount=str(amount),match_amount_source=amount_source,amount_evidence=e)
