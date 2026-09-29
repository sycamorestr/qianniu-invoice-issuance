(() => {
  const input=__INPUT__;
  const order_no=new URL(location.href).searchParams.get('bizOrderId');
  if(!input.order_no||order_no!==String(input.order_no)||!document.body.innerText.includes(order_no))throw Error('wrong_order_detail');

  const text=value=>String(value??'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  const quantity=value=>{
    const valueText=text(value).replace(/,/g,'');
    if(!/^\d+(?:\.\d+)?$/.test(valueText)||!Number.isFinite(Number(valueText))||Number(valueText)<=0)return '';
    const [integer,fraction='']=valueText.split('.');
    const whole=integer.replace(/^0+(?=\d)/,'')||'0';
    const decimal=fraction.replace(/0+$/,'');
    return decimal?`${whole}.${decimal}`:whole;
  };
  const fiberFor=row=>{
    const key=Object.getOwnPropertyNames(row).find(name=>name.startsWith('__reactFiber$'));
    return key?row[key]:null;
  };
  const runtimeOrderFor=row=>{
    let fiber=fiberFor(row);
    if(!fiber)throw Error('react_fiber_missing');
    const seen=new Set(),matches=[],nonStringIds=[];
    while(fiber&&!seen.has(fiber)){
      seen.add(fiber);
      const props=fiber.memoizedProps;
      if(props&&typeof props==='object'&&Array.isArray(props.subOrders)&&Object.prototype.hasOwnProperty.call(props,'id')){
        if(typeof props.id!=='string')nonStringIds.push(props);
        else if(props.id===order_no&&!matches.includes(props))matches.push(props);
      }
      fiber=fiber.return;
    }
    if(matches.length===1)return matches[0];
    if(matches.length>1)throw Error('runtime_order_ancestor_not_unique');
    // Do not stringify a numeric React id: 19-digit order numbers lose
    // precision before this script sees them.
    if(nonStringIds.length)throw Error('runtime_order_id_not_string');
    throw Error('runtime_order_id_mismatch');
  };
  const runtimeCode=subOrder=>{
    const entries=subOrder?.itemInfo?.extra;
    // Live detail responses omit optional extra when the rendered product
    // has no code. Accept only after the full DOM/runtime match below;
    // a malformed explicit value must still fail closed.
    if(!Object.prototype.hasOwnProperty.call(subOrder.itemInfo,'extra'))return '';
    if(!Array.isArray(entries))throw Error('runtime_goods_code_not_loaded');
    const codes=entries.filter(entry=>entry&&entry.name==='商家编码');
    if(codes.length>1)throw Error('runtime_goods_code_conflict');
    if(!codes.length)return '';
    if(typeof codes[0].value!=='string')throw Error('runtime_goods_code_invalid');
    return text(codes[0].value);
  };
  const runtimeOrders=[];
  const validateRuntimeOrder=order=>{
    if(!order.subOrders.length)throw Error('incomplete_order_detail');
    const ids=new Set();
    for(const sub of order.subOrders){
      if(typeof sub?.idStr!=='string'||!sub.idStr.trim())throw Error('runtime_sub_order_idStr_missing');
      if(ids.has(sub.idStr))throw Error('runtime_sub_order_idStr_duplicate');
      ids.add(sub.idStr);
      if(typeof sub.itemInfo?.title!=='string'||!text(sub.itemInfo.title)||!quantity(sub.quantity))throw Error('incomplete_runtime_order_detail');
      runtimeCode(sub);
    }
    runtimeOrders.push(order);
  };
  const subOrderFor=(order,domItem,firstCell,hasCodeLabel)=>{
    validateRuntimeOrder(order);
    const candidates=order.subOrders.filter(subOrder=>{
      const itemInfo=subOrder&&subOrder.itemInfo;
      if(runtimeCode(subOrder)!==text(domItem.goods_code)||
          quantity(subOrder.quantity)!==quantity(domItem.quantity))return false;
      if(hasCodeLabel)return text(itemInfo.title)===text(domItem.title);
      // Code-less rows can include runtime item notices below the title.
      // Match the entire rendered first cell; never infer a title from only
      // a price/quantity row or ignore unexplained DOM text.
      const rendered=[itemInfo.title,...(itemInfo.extra??[]).map(entry=>entry?.value??'')].map(text).filter(Boolean).join(' ');
      return text(firstCell)===text(rendered);
    });
    if(candidates.length!==1)throw Error('runtime_sub_order_not_unique');
    return candidates[0];
  };

  const rows=Array.from(document.querySelectorAll('tr')).filter(row=>{
    if(row.innerText.includes('商家编码'))return true;
    const cells=Array.from(row.querySelectorAll('td'));
    return cells.length>=4&&fiberFor(row)&&/[x×]\s*\d+(?:\.\d+)?/.test(cells.at(-1)?.innerText||'');
  });
  const items=rows.map((row,index)=>{
    const cells=Array.from(row.querySelectorAll('td')).map(c=>c.innerText.trim());
    const firstCell=cells[0]||'';
    const codeMatch=/商家编码[:：][^\S\r\n]*([^\n\r]*)/.exec(firstCell);
    const goods_code=codeMatch?.[1]?.trim()||'';
    const title=(codeMatch?firstCell.slice(0,codeMatch.index):firstCell).trim();
    const rawQuantity=/[x×]\s*(\d+(?:\.\d+)?)/.exec(cells.at(-1)||'')?.[1]||'';
    const domItem={order_no,goods_code,title,quantity:rawQuantity,
      unit_price:/([\d,.]+)\s*[x×]/.exec(cells.at(-1)||'')?.[1]?.replace(/,/g,'')||'',
      price_cell:cells.at(-1),specification:cells[1]||'',source:'order_detail_dom',source_row:index+1};
    if(!domItem.title||!quantity(domItem.quantity))throw Error('incomplete_order_detail');
    const subOrder=subOrderFor(runtimeOrderFor(row),domItem,firstCell,Boolean(codeMatch));
    return {...domItem,title:text(subOrder.itemInfo.title),sub_order_no:subOrder.idStr,
      ...(!goods_code?{goods_code_missing:true}:{})};
  });
  if(!items.length)throw Error('incomplete_order_detail');
  const itemIds=items.map(item=>item.sub_order_no);
  if(new Set(itemIds).size!==items.length)throw Error('duplicate_order_detail_row');
  const fingerprint=order=>JSON.stringify(order.subOrders.map(sub=>[
    sub.idStr,text(sub.itemInfo.title),quantity(sub.quantity),runtimeCode(sub)
  ]).sort((a,b)=>a[0].localeCompare(b[0])));
  const expected=fingerprint(runtimeOrders[0]);
  for(const order of runtimeOrders){
    if(fingerprint(order)!==expected)throw Error('runtime_order_snapshot_conflict');
    if(order.subOrders.length!==items.length||order.subOrders.some(sub=>!itemIds.includes(sub.idStr)))throw Error('incomplete_order_detail');
  }
  return {order_no,url:location.href,items,verified_order:true,queried_at:new Date().toISOString()};
})()
