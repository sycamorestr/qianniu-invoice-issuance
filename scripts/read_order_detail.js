(() => {
  const input=__INPUT__;
  const order_no=new URL(location.href).searchParams.get('bizOrderId');
  if(!input.order_no||order_no!==String(input.order_no)||!document.body.innerText.includes(order_no))throw Error('wrong_order_detail');
  const rows=Array.from(document.querySelectorAll('tr')).filter(row=>row.innerText.includes('商家编码'));
  const items=rows.map((row,index)=>{
    const cells=Array.from(row.querySelectorAll('td')).map(c=>c.innerText.trim());
    const goods_code=/商家编码[:：]\s*([^\n]+)/.exec(cells[0]||'')?.[1]?.trim()||'';
    return {order_no,goods_code,title:(cells[0]||'').split(/商家编码[:：]/)[0].trim(),
      quantity:/[x×](\d+(?:\.\d+)?)/.exec(cells.at(-1)||'')?.[1]||'',
      unit_price:/([\d,.]+)\s*[x×]/.exec(cells.at(-1)||'')?.[1]?.replace(/,/g,'')||'',
      price_cell:cells.at(-1),specification:cells[1]||'',source:'order_detail_dom',source_row:index+1};
  });
  if(!items.length||items.some(i=>!i.goods_code||!i.quantity))throw Error('incomplete_order_detail');
  return {order_no,url:location.href,items,verified_order:true,queried_at:new Date().toISOString()};
})()
