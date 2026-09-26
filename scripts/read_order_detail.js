(() => {
  const input=__INPUT__;
  const order_no=new URL(location.href).searchParams.get('bizOrderId');
  if(!input.order_no||order_no!==String(input.order_no)||!document.body.innerText.includes(order_no))throw Error('wrong_order_detail');

  const text=value=>String(value??'').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
  const quantity=value=>{
    const valueText=text(value).replace(/,/g,'');
    if(!/^\d+(?:\.\d+)?$/.test(valueText))return '';
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
    const entries=Array.isArray(subOrder?.itemInfo?.extra)?subOrder.itemInfo.extra:[];
    const values=entries.filter(entry=>entry&&entry.name==='商家编码'&&typeof entry.value==='string')
      .map(entry=>text(entry.value)).filter(Boolean);
    return values.length===1?values[0]:null;
  };
  const subOrderFor=(order,domItem)=>{
    const candidates=order.subOrders.filter(subOrder=>{
      const itemInfo=subOrder&&subOrder.itemInfo;
      return itemInfo&&typeof itemInfo.title==='string'&&
        text(itemInfo.title)===text(domItem.title)&&
        runtimeCode(subOrder)===text(domItem.goods_code)&&
        quantity(subOrder.quantity)===quantity(domItem.quantity);
    });
    if(candidates.length!==1)throw Error('runtime_sub_order_not_unique');
    const idStr=candidates[0].idStr;
    // `idStr` is authoritative; numeric id is never a fallback.
    if(typeof idStr!=='string'||!idStr.trim())throw Error('runtime_sub_order_idStr_missing');
    return idStr;
  };

  const rows=Array.from(document.querySelectorAll('tr')).filter(row=>row.innerText.includes('商家编码'));
  const items=rows.map((row,index)=>{
    const cells=Array.from(row.querySelectorAll('td')).map(c=>c.innerText.trim());
    const firstCell=cells[0]||'';
    const codeMatch=/商家编码[:：]\s*([^\n\r]+)/.exec(firstCell);
    const goods_code=codeMatch?.[1]?.trim()||'';
    const title=(codeMatch?firstCell.slice(0,codeMatch.index):firstCell).trim();
    const rawQuantity=/[x×](\d+(?:\.\d+)?)/.exec(cells.at(-1)||'')?.[1]||'';
    const domItem={order_no,goods_code,title,quantity:rawQuantity,
      unit_price:/([\d,.]+)\s*[x×]/.exec(cells.at(-1)||'')?.[1]?.replace(/,/g,'')||'',
      price_cell:cells.at(-1),specification:cells[1]||'',source:'order_detail_dom',source_row:index+1};
    if(!domItem.goods_code||!domItem.title||!quantity(domItem.quantity))throw Error('incomplete_order_detail');
    return {...domItem,sub_order_no:subOrderFor(runtimeOrderFor(row),domItem)};
  });
  if(!items.length)throw Error('incomplete_order_detail');
  return {order_no,url:location.href,items,verified_order:true,queried_at:new Date().toISOString()};
})()
