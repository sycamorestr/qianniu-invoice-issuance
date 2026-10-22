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


def named_specification_pairs(raw):
    """Recognize complete, uniquely named attributes without guessing delimiters."""
    if isinstance(raw, str):
        parts = [part for part in raw.splitlines() if part.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = raw
    else:
        return None
    pairs = []
    names = set()
    for part in parts:
        if isinstance(part, dict):
            name = part.get('name') or part.get('label')
            value = part.get('value')
            if not isinstance(name, str) or not isinstance(value, str):
                return None
        elif isinstance(part, str):
            fields = re.split(r'[:：]', part)
            if len(fields) != 2:
                return None
            name, value = fields
        else:
            return None
        if re.search(r'[:：\r\n]', name) or re.search(r'[:：\r\n]', value):
            return None
        name, value = text(name), text(value)
        if not name or not value or name in names:
            return None
        names.add(name)
        pairs.append((name, value))
    return tuple(sorted(pairs)) if pairs else None


def specification_text(raw):
    """Compare list-shaped SKU specs with the detail page's text form.

    The sold-order API returns ``[{name, value}]`` while the detail DOM
    collector returns strings such as ``name:value``.  They are the same
    source fact. Only complete named attributes may ignore presentation order;
    unstructured or ambiguous text retains the existing ordered comparison.
    """
    pairs = named_specification_pairs(raw)
    if pairs is not None:
        return ('named_attributes', pairs)
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
                require(bool(text(item.get('title'))), '订单详情缺少商品标题: '+str(key))
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
        valid=(bool(text(item.get('goods_code'))) and item.get('source')=='order_detail_dom' and parsed.hostname in {'qn.taobao.com','trade.taobao.com'}
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


def attach_detail_gross_evidence(normalized, details, root=None):
    """Keep raw detail proof without claiming that any source row is unique."""
    by_key = {item_key(item): item for item in normalized}
    for detail in details:
        key = item_key(detail['item'])
        require(key in by_key, '详情金额原件不在订单明细集合')
        record = deepcopy(detail)
        if root is not None:
            record['checkpoint_path'] = str((Path(root) / record['checkpoint']).resolve())
        by_key[key].setdefault('detail_gross_evidence', []).append(record)


def _source_group_signature(row, discount):
    """All source-dependent match/output facts, excluding row identity."""
    result = {}
    for label, current in (('positive', row), ('discount', discount)):
        if current is None:
            result[label] = None
            continue
        fields = {}
        for field in ('goods_code', 'sub_order_no', 'source_goods_name',
                      'source_specification', 'tax_rate_zero'):
            fields[field] = (rules.normalize_identifier(rules.value(current, field))
                             if field in {'goods_code', 'sub_order_no'} else
                             comparable(rules.value(current, field),
                                        'specification' if field == 'source_specification' else field))
        for field in ('goods_amount', 'quantity'):
            raw = rules.value(current, field)
            parsed = number(raw)
            if rules.clean_text(raw) is not None and parsed is None:
                return None
            fields[field] = rules.decimal_text(parsed) if parsed is not None else None
        raw_rate = rules.value(current, 'tax_rate')
        errors = []
        rate = rules.parse_tax_rate(raw_rate, errors) if rules.clean_text(raw_rate) else None
        if errors:
            return None
        fields['tax_rate'] = rules.decimal_text(rate) if rate is not None else None
        result[label] = fields
    return json.loads(json.dumps(result, ensure_ascii=False))


def _raw_group_item_proof(item, order_items):
    """Recheck raw DOM provenance and complete detail membership from scratch."""
    records = item.get('detail_gross_evidence')
    if not isinstance(records, list) or not records:
        return None
    expected = {item_key(candidate): candidate for candidate in order_items}
    proof_hashes = []
    gross = None
    for record in records:
        if not isinstance(record, dict):
            return None
        raw = record.get('item'); document = record.get('document')
        if not isinstance(raw, dict) or not isinstance(document, dict):
            return None
        key = item_key(raw)
        if key != item_key(item) or document.get('verified_order') is not True:
            return None
        if str(document.get('order_no') or '') != key[0] or not document.get('queried_at'):
            return None
        if record.get('checkpoint') not in {'old_details.json', 'supplemental_details.json'}:
            return None
        if not re.fullmatch(r'[0-9a-f]{64}', str(record.get('checkpoint_sha256') or '')):
            return None
        # A recomputed JSON proof alone cannot authenticate its claimed raw
        # checkpoint. Bind the retained document to the unchanged saved bytes.
        checkpoint_path = Path(str(record.get('checkpoint_path') or ''))
        if not checkpoint_path.is_absolute() or checkpoint_path.name != record['checkpoint']:
            return None
        try:
            checkpoint_bytes = checkpoint_path.read_bytes()
            checkpoint_documents = json.loads(checkpoint_bytes.decode('utf-8-sig'))
        except (OSError, ValueError, UnicodeError):
            return None
        if (hashlib.sha256(checkpoint_bytes).hexdigest() != record['checkpoint_sha256']
                or not isinstance(checkpoint_documents, list) or document not in checkpoint_documents):
            return None
        parsed = urlparse(str(document.get('url') or ''))
        if (parsed.scheme != 'https' or parsed.hostname not in {'qn.taobao.com', 'trade.taobao.com'}
                or parse_qs(parsed.query).get('bizOrderId') != [key[0]]):
            return None
        raw_items = document.get('items')
        if not isinstance(raw_items, list) or not all(isinstance(i, dict) for i in raw_items):
            return None
        if (len(raw_items) != len(expected) or len({item_key(i) for i in raw_items}) != len(raw_items)
                or {item_key(i) for i in raw_items} != set(expected) or raw not in raw_items):
            return None
        # Every raw detail record must agree with the normalized list identity.
        for detail_item in raw_items:
            candidate = expected[item_key(detail_item)]
            if (not detail_item.get('sub_order_no') or not detail_item.get('goods_code')
                    or str(detail_item['goods_code']).strip() != candidate.get('goods_code')
                    or not text(detail_item.get('title'))
                    or text(detail_item['title']) != text(candidate.get('source_goods_name'))
                    or number(detail_item.get('quantity')) != number(candidate.get('quantity'))
                    or specification_text(detail_item.get('specification')) !=
                    specification_text(candidate.get('source_specification'))):
                return None
        price = number(raw.get('unit_price')); quantity = number(raw.get('quantity'))
        price_cell = re.fullmatch(
            r'\s*[¥￥]?\s*((?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)\s*[xX×]\s*([0-9]+(?:\.[0-9]+)?)\s*',
            str(raw.get('price_cell') or ''))
        if (raw.get('source') != 'order_detail_dom' or price is None or price <= 0
                or quantity is None or quantity <= 0 or not price_cell
                or number(price_cell[1]) != price or number(price_cell[2]) != quantity):
            return None
        current_gross = price * quantity
        if gross is not None and gross != current_gross:
            return None
        if item.get('match_amount') is not None and number(item['match_amount']) != current_gross:
            return None
        gross = current_gross
        proof_hashes.append(rules.canonical_hash(record))
    return {'gross': gross, 'raw_evidence_sha256': sorted(set(proof_hashes))}


def equivalent_group_assignments(rows, items):
    """Prove a whole-order permutation invariant, never a source-row identity.

    This deliberately accepts only all-to-all groups of equal economic source
    rows. Every distinct order item is used exactly once with the same quantity,
    gross amount and adjacent discount, so any permutation has the same complete
    business output multiset. No price-only inference or partial subgroup joins.
    """
    if not rows or len({str(rules.value(row, 'application_no') or '') for _, row in rows}) != 1:
        return {}
    serial = str(rules.value(rows[0][1], 'application_no') or '')
    if not serial:
        return {}
    # A discount with no order identity cannot be silently left out of a group.
    if any(number(rules.value(row, 'goods_amount')) is not None
           and number(rules.value(row, 'goods_amount')) < 0
           and not rules.clean_text(rules.value(row, 'order_no')) for _, row in rows):
        return {}
    by_order = {}
    for rn, row in rows:
        order = str(rules.value(row, 'order_no') or '')
        if order:
            by_order.setdefault(order, []).append((rn, row))
    result = {}
    for order, source_rows in by_order.items():
        candidates = [item for item in items if str(item.get('order_no') or '') == order]
        keys = [item_key(item) for item in candidates]
        if (len(candidates) < 2 or len(set(keys)) != len(keys)
                or any(not key[1] for key in keys)
                or any(not rules.clean_text(item.get('goods_code')) for item in candidates)):
            continue
        pairs = []; invalid = False
        for rn, row in source_rows:
            amount = number(rules.value(row, 'goods_amount'))
            if (amount is None or amount == 0
                    or rules.clean_text(rules.value(row, 'source_goods_name')) == '价外费用'):
                invalid = True; break
            if amount > 0:
                quantity = number(rules.value(row, 'quantity'))
                if quantity is None or quantity <= 0 or not text(rules.value(row, 'source_goods_name')):
                    invalid = True; break
                pairs.append([rn, row, None, None])
            else:
                if (not pairs or pairs[-1][2] is not None or rn != pairs[-1][0] + 1
                        or abs(amount) > number(rules.value(pairs[-1][1], 'goods_amount'))):
                    invalid = True; break
                pairs[-1][2:] = [rn, row]
        if invalid or len(pairs) != len(candidates):
            continue
        signatures = [_source_group_signature(row, discount) for _, row, _, discount in pairs]
        if signatures[0] is None or any(signature != signatures[0] for signature in signatures):
            continue
        item_proofs = [_raw_group_item_proof(item, candidates) for item in candidates]
        if any(proof is None for proof in item_proofs):
            continue
        # Check every edge, including explicit discount identities. Equality of
        # a total alone cannot establish any of these per-item facts.
        for rn, row, dr, discount in pairs:
            for item, item_proof in zip(candidates, item_proofs):
                raw_item = item['detail_gross_evidence'][0]['item']
                if (not compatible(row, raw_item)
                        or item_proof['gross'] != number(rules.value(row, 'goods_amount'))):
                    invalid = True; break
                for field in ('goods_code', 'sub_order_no'):
                    constraint = rules.normalize_identifier(rules.value(row, field))
                    if constraint and constraint != rules.normalize_identifier(raw_item.get(field)):
                        invalid = True; break
                if invalid:
                    break
                if discount is not None:
                    for field, raw_field in (('goods_code', 'goods_code'), ('sub_order_no', 'sub_order_no'),
                                             ('source_goods_name', 'title'), ('source_specification', 'specification')):
                        kind = 'specification' if raw_field == 'specification' else field
                        constraint = (rules.normalize_identifier(rules.value(discount, field))
                                      if field in {'goods_code', 'sub_order_no'} else
                                      comparable(rules.value(discount, field), kind))
                        actual = (rules.normalize_identifier(raw_item.get(raw_field))
                                  if field in {'goods_code', 'sub_order_no'} else
                                  comparable(raw_item.get(raw_field), kind))
                        if constraint and constraint != actual:
                            invalid = True; break
            if invalid:
                break
        if invalid:
            continue
        ordered_pairs = sorted(pairs, key=lambda pair: pair[0])
        ordered_items = sorted(candidates, key=lambda item: item_key(item)[1])
        assignments = [{'source_row': pair[0], 'sub_order_no': item['sub_order_no'],
                        'goods_code': item['goods_code']} for pair, item in zip(ordered_pairs, ordered_items)]
        proof = {'version': 1, 'method': 'group_equivalence', 'application_no': serial, 'order_no': order,
                 'source_rows': [pair[0] for pair in ordered_pairs],
                 'discount_source_rows': [pair[2] for pair in ordered_pairs],
                 'sub_order_nos': [item['sub_order_no'] for item in ordered_items],
                 'economic_signature': signatures[0], 'assignments': assignments,
                 'raw_evidence_sha256': {item['sub_order_no']: item_proof['raw_evidence_sha256']
                                         for item, item_proof in zip(candidates, item_proofs)}}
        proof['proof_sha256'] = rules.canonical_hash(proof)
        for pair, item in zip(ordered_pairs, ordered_items):
            result[pair[0]] = {'item': item, 'proof': proof}
    return result
