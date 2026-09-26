(async () => {
  const input = __INPUT__;
  const {operation} = input;
  const timestamp = () => new Date().toISOString();
  async function request(url, options={}) {
    for(let attempt=0;attempt<3;attempt++){
      try{
        const response=await fetch(url,{...options,credentials:'include',signal:AbortSignal.timeout(30000)});
        if(response.status===401||response.status===403||/login|passport/i.test(response.url))throw Error('login_required');
        if([429,502,503,504].includes(response.status)&&attempt<2){await new Promise(r=>setTimeout(r,500*(attempt+1)));continue;}
        if(!response.ok)throw Error(`HTTP ${response.status}`);
        return response;
      }catch(error){
        if(attempt===2||!['TypeError','TimeoutError'].includes(error.name))throw error;
      }
    }
  }
  // The sold-order page sends a complete query object even when the user only
  // supplies a date/order filter in the UI. Keep the page defaults here so a
  // sparse caller input cannot accidentally change server-side semantics.
  const ORDER_QUERY_DEFAULTS={
    payDateBegin:0,rateStatus:'',buyerNick:'',orderStatus:'',pageSize:15,
    dateEnd:0,endTimeBegin:0,endTimeEnd:0,rxOldFlag:0,rxSendFlag:0,
    useCheckcode:false,dateBegin:0,tradeTag:0,extra:{},
    action:'itemlist/SoldQueryAction',rxHasSendFlag:0,auctionType:0,close:0,
    sellerNick:'',cartItemDOList:[],notifySendGoodsType:'ALL',sellerMemoFlag:'0',
    useOrderInfo:false,logisticsService:'',isQnNew:true,pageNum:1,
    o2oDeliveryType:'ALL',rxAuditFlag:0,queryOrder:'desc',holdStatus:0,
    rxElectronicAuditFlag:0,queryMore:true,payDateEnd:0,rxWaitSendflag:0,
    sellerMemo:0,tabCode:'latest3Months',rxElectronicAllFlag:0,
    rxSuccessflag:0,unionSearchTotalNum:0,refund:'',unionSearchPageNum:0
  };
  const stableStringify=value=>{
    if(value===null||typeof value!=='object')return JSON.stringify(value);
    if(Array.isArray(value))return '['+value.map(stableStringify).join(',')+']';
    return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stableStringify(value[k])).join(',')+'}';
  };
  if(['applications','export'].includes(operation)){
    if(!['myseller.taobao.com','einvoice.taobao.com'].includes(location.hostname))throw Error('wrong_invoice_page');
    if(!/^\d{4}-\d{2}-\d{2}$/.test(input.date||'')||input.agentId===undefined)throw Error('date_and_current_agentId_required');
    const common={startTime:input.date,endTime:input.date,agentId:String(input.agentId)};
    if(operation==='export'){
      const response=await request('https://einvoice.taobao.com/api/invoice/batch4visitor/apply?'+new URLSearchParams({...common,pageNo:'0',pageSize:'20'}));
      const bytes=new Uint8Array(await response.arrayBuffer());
      if(bytes[0]!==80||bytes[1]!==75)throw Error('not_xlsx');
      let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
      // Keep the browser-session URL out of the saved checkpoint. The local
      // bridge persists only the XLSX bytes and their file hash.
      return {status:response.status,date:input.date,queried_at:timestamp(),base64:btoa(binary)};
    }
    // The export is the authoritative source. Keep every row returned by the
    // list endpoint as a diagnostic snapshot, including rows whose status is
    // not pending; the matching common-template export decides scope.
    const rows=[],list_non_pending_snapshot_rows=[];let api_total=null;const seen=new Set();
    const pageSize=20;
    const finish=()=>({date:input.date,queried_at:timestamp(),total:rows.length,api_total,
      observed_total:seen.size,rows,list_non_pending_snapshot_rows});
    for(let pageNo=0;pageNo<10000;pageNo++){
      const response=await request('https://einvoice.taobao.com/api/qianniu/invoice/list/apply?'+new URLSearchParams({...common,applyListType:'0',pageSize:'20',pageNo:String(pageNo)}));
      const body=await response.json();
      if(body.code!==200||!Array.isArray(body.data)||!Number.isInteger(body.total))throw Error('invalid_application_response');
      if(api_total!==null&&api_total!==body.total)throw Error('applications_changed_during_pagination');
      api_total=body.total;
      for(const row of body.data){
        if(seen.has(row.serialNo))throw Error('duplicate_application');seen.add(row.serialNo);
        const normalized={serialNo:row.serialNo,tid:String(row.tid),amount:row.amount,applyStatus:row.applyStatus,applyTime:row.applyTime,tradeLink:row.tradeLink};
        rows.push(normalized);
        if(row.applyStatus!==1)list_non_pending_snapshot_rows.push(normalized);
      }
      // `total` is the server's pending count, while the response can also
      // contain historical/non-pending rows. Stop on a short page, or on an
      // empty page after all server-counted rows have been observed.
      if(!body.data.length){
        if(seen.size>=api_total)return finish();
        throw Error('incomplete_application_pagination');
      }
      if(body.data.length<pageSize){
        if(seen.size<api_total)throw Error('incomplete_application_pagination');
        return finish();
      }
    }
    throw Error('application_pagination_limit');
  }
  if(operation==='orders'){
    if(location.hostname!=='myseller.taobao.com'||!location.pathname.includes('/tp/sold'))throw Error('wrong_order_page');
    const ids=input.orders;
    if(!Array.isArray(ids)||!ids.length||ids.length>50||new Set(ids).size!==ids.length)throw Error('orders_must_be_1_to_50_unique_strings');
    const items=[],batches=[],seen=new Set();let expected=null;
    const suppliedQuery=(input.query&&typeof input.query==='object')?input.query:{};
    const baseQuery={...ORDER_QUERY_DEFAULTS,...suppliedQuery};
    // A batch request always owns these fields; do not allow stale values from
    // a previous page query to leak into the requested order set.
    baseQuery.bizOrderId=ids.join(',');
    baseQuery.auctionId='';baseQuery.buyerNick='';baseQuery.batchType='bizOrderId';
    baseQuery.isBatchSearch=true;
    const queryFingerprint=stableStringify(baseQuery);
    for(let pageNum=1;pageNum<=100;pageNum++){
      const query={...baseQuery,pageNum};
      const params=new URLSearchParams();for(const [k,v] of Object.entries(query))params.set(k,typeof v==='object'?JSON.stringify(v):String(v));
      const response=await request('https://trade.taobao.com/trade/itemlist/asyncSold.htm?event_submit_do_query=1&_input_charset=utf8',
        {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},body:params.toString()});
      const charset=/charset=([^;]+)/i.exec(response.headers.get('content-type')||'')?.[1]||'gbk';
      const body=JSON.parse(new TextDecoder(charset).decode(await response.arrayBuffer()));
      if(!Array.isArray(body.mainOrders)||!body.page)throw Error('invalid_order_response');
      const count=Number(body.page.totalNumber),pages=Number(body.page.totalPage);
      if(!Number.isInteger(count)||!Number.isInteger(pages))throw Error('missing_order_pagination');
      if(expected!==null&&count!==expected)throw Error('orders_changed_during_pagination');expected=count;
      batches.push({ids,pageNum,page:body.page,query:body.query||query,query_fingerprint:stableStringify({...query,pageNum}),order_ids:body.mainOrders.map(o=>String(o.id))});
      for(const order of body.mainOrders){
        const id=String(order.id);if(!ids.includes(id)||seen.has(id))throw Error('unexpected_or_duplicate_order');seen.add(id);
        for(const item of order.subOrders||[])items.push({order_no:id,sub_order_no:item.idStr||String(item.id||''),
          quantity:String(item.quantity??''),real_total:String(item.priceInfo?.realTotal??''),title:item.itemInfo?.title||'',
          specification:item.itemInfo?.skuText||[],goods_code:(item.itemInfo?.extra||[]).find(e=>e.name==='商家编码')?.value||''});
      }
      if(pageNum>=pages){if(seen.size!==expected)throw Error('incomplete_order_pagination');return {queried_at:timestamp(),batches,items,missing:ids.filter(id=>!seen.has(id)),query_defaults:ORDER_QUERY_DEFAULTS,query_fingerprint:queryFingerprint};}
      if(!body.mainOrders.length)throw Error('empty_order_page_before_end');
    }
    throw Error('order_pagination_limit');
  }
  throw Error('unknown_read_operation');
})()
