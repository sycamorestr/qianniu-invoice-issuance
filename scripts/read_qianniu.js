(async () => {
  const input = __INPUT__;
  const {operation} = input;
  const timestamp = () => new Date().toISOString();
  // Pace every actual order request, including the first page of a new
  // batch and network retries. A delay only in the outer batch loop misses
  // pagination and creates bursts against the sold-order endpoint.
  const ORDER_REQUEST_INTERVAL_MS=3000;
  async function request(url, options={}) {
    for(let attempt=0;attempt<3;attempt++){
      try{
        if(operation==='orders')await new Promise(r=>setTimeout(r,ORDER_REQUEST_INTERVAL_MS));
        const response=await fetch(url,{...options,credentials:'include',signal:AbortSignal.timeout(30000)});
        if(response.status===401||response.status===403||/login|passport/i.test(response.url))throw Error('login_required');
        if(operation==='orders'&&response.status===429)throw Error('rate_limited');
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
    const validDate=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&
      Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;
    let startTime=input.date,endTime=input.date;
    const scope=input.query_scope;
    const countdownStarted=scope?.countdown==='started';
    const scopeKeys=keys=>Object.keys(scope).sort().join(',')===[...keys,
      ...(Object.hasOwn(scope,'countdown')?['countdown']:[])].sort().join(',');
    if(scope!==undefined&&(!scope||typeof scope!=='object'||Array.isArray(scope)||
      (Object.hasOwn(scope,'countdown')&&!countdownStarted)))throw Error('invalid_query_scope');
    if(scope?.mode==='all_pending'){
      if(input.date!=null||!validDate(scope.start_date)||!validDate(scope.end_date)||
        !scopeKeys(['mode','start_date','end_date']))throw Error('invalid_query_scope');
      const end=new Date(scope.end_date+'T00:00:00Z');
      const first=new Date(Date.UTC(end.getUTCFullYear(),end.getUTCMonth()-2,1));
      const days=new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,0)).getUTCDate();
      first.setUTCDate(Math.min(end.getUTCDate(),days));
      if(first.toISOString().slice(0,10)!==scope.start_date)throw Error('invalid_query_scope');
      startTime=scope.start_date;endTime=scope.end_date;
    }else if(scope?.mode==='date'){
      if(!scopeKeys(['mode','date'])||scope.date!==input.date)throw Error('invalid_query_scope');
    }else if(scope!==undefined){
      throw Error('invalid_query_scope');
    }
    if(!validDate(startTime)||!validDate(endTime)||input.agentId===undefined)throw Error('date_and_current_agentId_required');
    const scopeResult=scope?{date:scope.mode==='all_pending'?null:input.date,query_scope:scope}:{date:input.date};
    // UI enum: 已开始=100, 已超时=-1, 未开始=0. Apply the exact same
    // filter to diagnostics and the original export; remainTime is NOT this
    // selector (the live API can return remainTime=0 on started rows).
    const common={startTime,endTime,agentId:String(input.agentId),
      ...(countdownStarted?{rightsRemainTime:'100'}:{})};
    if(operation==='export'){
      const response=await request('https://einvoice.taobao.com/api/invoice/batch4visitor/apply?'+new URLSearchParams({...common,pageNo:'0',pageSize:'20'}));
      const bytes=new Uint8Array(await response.arrayBuffer());
      // A zero-byte HTTP success is the platform's no-data export. Preserve
      // those bytes; the runner must corroborate an explicitly empty list.
      if(bytes.length&&(bytes[0]!==80||bytes[1]!==75))throw Error('not_xlsx');
      let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
      // Keep the browser-session URL out of the saved checkpoint. The local
      // bridge persists only the XLSX bytes and their file hash.
      return {status:response.status,...scopeResult,queried_at:timestamp(),base64:btoa(binary)};
    }
    // The export is the authoritative source. Keep every row returned by the
    // list endpoint as a diagnostic snapshot, including rows whose status is
    // not pending; the matching common-template export decides scope.
    const rows=[],list_non_pending_snapshot_rows=[];let api_total=null,duplicate_snapshot_row_count=0;const seen=new Map();
    const pageSize=20;
    const finish=()=>({...scopeResult,queried_at:timestamp(),total:rows.length,api_total,
      observed_total:seen.size,rows,list_non_pending_snapshot_rows,duplicate_snapshot_row_count});
    for(let pageNo=0;pageNo<10000;pageNo++){
      const response=await request('https://einvoice.taobao.com/api/qianniu/invoice/list/apply?'+new URLSearchParams({...common,applyListType:'0',pageSize:'20',pageNo:String(pageNo)}));
      const body=await response.json();
      if(body.code===1004)throw Error('permission_required');
      // With no applications the live API returns {code:200,total:0,
      // message:'无数据'} without data. Only this explicit zero is empty;
      // missing rows for a nonzero total remain a failed response.
      if(body.code===200&&body.total===0&&body.data==null)body.data=[];
      if(body.code!==200||!Array.isArray(body.data)||!Number.isInteger(body.total))throw Error('invalid_application_response');
      if(api_total!==null&&api_total!==body.total)throw Error('applications_changed_during_pagination');
      api_total=body.total;
      let added=0;
      for(const row of body.data){
        if(!row.serialNo)throw Error('missing_application_id');
        const normalized={serialNo:row.serialNo,tid:String(row.tid),amount:row.amount,applyStatus:row.applyStatus,
          applyTime:row.applyTime??row.applyGmtCreate??row.startTime,tradeLink:row.tradeLink};
        // The live endpoint expands related application history across page
        // boundaries. Identical business rows can recur on adjacent pages.
        // Keep one diagnostic row, but reject a changed record or stuck page.
        const signature=stableStringify(normalized);
        if(seen.has(row.serialNo)){
          if(seen.get(row.serialNo)!==signature)throw Error('conflicting_duplicate_application');
          duplicate_snapshot_row_count++;continue;
        }
        seen.set(row.serialNo,signature);added++;
        rows.push(normalized);
        if(row.applyStatus!==1)list_non_pending_snapshot_rows.push(normalized);
      }
      if(body.data.length&&!added)throw Error('duplicate_application_page');
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
