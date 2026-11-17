import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const qianniu=await fs.readFile(new URL('./read_qianniu.js',import.meta.url),'utf8');
const jst=await fs.readFile(new URL('./query_jst_invoice_goods.js',import.meta.url),'utf8');
const detail=await fs.readFile(new URL('./read_order_detail.js',import.meta.url),'utf8');
const contextSource=await fs.readFile(new URL('./playwright_context_qianniu.js',import.meta.url),'utf8');
const base={URL,URLSearchParams,TextDecoder,Uint8Array,AbortSignal,AbortController,setTimeout,clearTimeout,setInterval,clearInterval,btoa};
function fakeTimers(clock={elapsed:0,waits:[]}){
  let next=0;const deadlines=new Map(),guards=new Map();
  clock.fireDeadlines=()=>{for(const callback of [...deadlines.values()])callback();};
  clock.tickGuards=()=>{for(const callback of [...guards.values()])callback();};
  return {
    setTimeout:(callback,ms)=>{const id=++next;if(ms===30000)deadlines.set(id,callback);
      else{clock.elapsed+=ms;clock.waits.push(ms);clock.onSleep?.();clock.tickGuards();callback();}return id;},
    clearTimeout:id=>deadlines.delete(id),setInterval:callback=>{const id=++next;guards.set(id,callback);return id;},
    clearInterval:id=>guards.delete(id)
  };
}
const response=(body,opts={})=>({ok:opts.ok===undefined?true:opts.ok,status:opts.status===undefined?200:opts.status,url:opts.url||'https://business.example/read',json:async()=>body,
  text:async()=>JSON.stringify(body),arrayBuffer:async()=>new TextEncoder().encode(JSON.stringify(body)).buffer,
  headers:{get:()=> 'application/json; charset=utf-8'}});
async function runContext(input, context={isLogin:true,realNick:'account:operator'}, shop={userNick:'account'}, overrides={}, clock={elapsed:0,waits:[]}) {
  return vm.runInNewContext(contextSource.replace('__INPUT__',JSON.stringify({agent_id:'0',...input})), {
    ...base,...fakeTimers(clock),document:{querySelector:()=>null}, performance:{getEntriesByType:()=>[]},
    location:{href:'https://myseller.taobao.com/home.htm/merchant-invoice/'},
    fetch:async url=>response({data:url.endsWith('/context')?context:shop}),...overrides
  });
}
const aliasContext=await runContext({expected_store:'Friendly shop',expected_account:'account:operator'});
assert.equal(aliasContext.store,'Friendly shop');
assert.equal(aliasContext.observed_store,'account');
assert.equal(aliasContext.account_nick,'account:operator');
await assert.rejects(()=>runContext({expected_store:'Friendly shop'}),/context_changed_store/);
await assert.rejects(()=>runContext({expected_store:'account',expected_account:'other:operator'}),/context_changed_account/);
await assert.rejects(()=>runContext({expected_account:'account:operator'},{isLogin:true}),/context_missing_account/);
await assert.rejects(()=>runContext({expected_account:'account:operator',expected_observed_store:'other'}),/context_changed_store/);
await assert.rejects(()=>runContext({expected_account_nick:'other:operator'}),/context_changed_account/);
await assert.rejects(()=>runContext({}, {isLogin:false}),/login_required/);
let loggedOutContextCalls=0;
await assert.rejects(()=>runContext({},undefined,undefined,{fetch:async()=>{
  loggedOutContextCalls++;return response({data:{isLogin:false}});
}}),/login_required/);
assert.equal(loggedOutContextCalls,1);
assert.equal((await runContext({expected_store:'account'})).store,'account');
async function run(input,bodies,location={hostname:'myseller.taobao.com',pathname:'/home.htm/merchant-invoice/'},fetchOverride=null,clock={elapsed:0,waits:[]},overrides={}){
  let at=0;
  // Advance virtual time at each sleep; assert actual fetch timing without
  // imposing production pacing delays on this offline fixture suite.
  return await vm.runInNewContext(qianniu.replace('__INPUT__',JSON.stringify(input)),{...base,...fakeTimers(clock),location,fetch:fetchOverride||(async()=>response(bodies[at++])),...overrides});
}
const apps={operation:'applications',date:'2026-01-01',agentId:'0'};
const allScope={mode:'all_pending',start_date:'2026-07-28',end_date:'2026-09-28'};
const allInput={operation:'applications',date:null,query_scope:allScope,agentId:'0'};
const allUrls=[];
const allResult=await run(allInput,[],undefined,async url=>{
  allUrls.push(new URL(url));return response({code:200,total:2,data:[
    {serialNo:'JULY',tid:'OJ',applyTime:'2026-07-28',applyStatus:1},
    {serialNo:'SEPT',tid:'OS',applyTime:'2026-09-28',applyStatus:1}]});
});
assert.equal(allResult.rows.length,2);assert.equal(allResult.date,null);
assert.equal(JSON.stringify(allResult.query_scope),JSON.stringify(allScope));
const allExport=await run({...allInput,operation:'export'},[],undefined,async url=>{
  allUrls.push(new URL(url));return {ok:true,status:200,url,arrayBuffer:async()=>new Uint8Array([80,75,3,4]).buffer};
});
assert.equal(allExport.base64,'UEsDBA==');
for(const url of allUrls){
  assert.equal(url.searchParams.get('startTime'),'2026-07-28');
  assert.equal(url.searchParams.get('endTime'),'2026-09-28');
  assert.equal(url.searchParams.has('rightsRemainTime'),false);
}
for(const selectedScope of [{...allScope,countdown:'started'},
  {mode:'date',date:'2026-09-25',countdown:'started'}]){
  const filteredUrls=[];
  const filteredInput={date:selectedScope.mode==='date'?selectedScope.date:null,query_scope:selectedScope,agentId:'0'};
  const collected=await run({...filteredInput,operation:'applications'},[],undefined,async url=>{
    filteredUrls.push(new URL(url));return response({code:200,total:0});
  });
  const exported=await run({...filteredInput,operation:'export'},[],undefined,async url=>{
    filteredUrls.push(new URL(url));return {ok:true,status:200,url,arrayBuffer:async()=>new Uint8Array([80,75,3,4]).buffer};
  });
  for(const value of [collected,exported]){
    assert.deepEqual(JSON.parse(JSON.stringify(value.query_scope)),selectedScope);
    assert.equal(value.date,filteredInput.date);
  }
  for(const url of filteredUrls){
    assert.equal(url.searchParams.get('rightsRemainTime'),'100');
    assert.equal(url.searchParams.get('startTime'),selectedScope.start_date??selectedScope.date);
    assert.equal(url.searchParams.get('endTime'),selectedScope.end_date??selectedScope.date);
  }
}
// Invalid or unsupported countdown modes cannot fall back to an unfiltered
// request, including empty values and a dated scope for another date.
for(const badScope of [{...allScope,countdown:'all'}, {...allScope,countdown:null},
  {...allScope,countdown:''}, {...allScope,countdown:100},
  {...allScope,countdown:'started',extra:true},
  {mode:'date',date:'2026-09-24',countdown:'started'}]){
  let calls=0;
  await assert.rejects(()=>run({operation:'applications',date:badScope.mode==='date'?'2026-09-25':null,
    query_scope:badScope,agentId:'0'},[],undefined,async()=>{calls++;return response({code:200,total:0});}),/invalid_query_scope/);
  assert.equal(calls,0);
}
await assert.rejects(()=>run({...allInput,date:'2026-09-28'},[]),/invalid_query_scope/);
await assert.rejects(()=>run({...allInput,query_scope:{...allScope,start_date:'2026-06-28'}},[]),/invalid_query_scope/);
await assert.rejects(()=>run({...allInput,query_scope:{mode:'all_pending'}},[]),/invalid_query_scope/);
await assert.rejects(()=>run({operation:'applications',agentId:'0'},[]),/date_and_current_agentId_required/);
await assert.rejects(()=>run({...apps,date:'2026-02-30'},[]),/date_and_current_agentId_required/);
const leapScope={mode:'all_pending',start_date:'2024-02-29',end_date:'2024-04-30'};
assert.equal((await run({...allInput,query_scope:leapScope},[{code:200,total:0}])).total,0);
const emptyExport=await run({...apps,operation:'export'},[],undefined,async()=>({ok:true,status:200,url:'https://einvoice.taobao.com/export',arrayBuffer:async()=>new ArrayBuffer(0)}));
assert.equal(emptyExport.base64,'');
assert.equal(emptyExport.status,200);
assert.equal((await run(apps,[{code:200,total:0,message:'无数据'}])).total,0);
await assert.rejects(()=>run(apps,[{code:1004,message:'权限不足'}]),/permission_required/);
await assert.rejects(()=>run(apps,[{code:200,total:1,message:'无数据'}]),/invalid_application_response/);
await assert.rejects(()=>run(apps,[{code:500,total:0}]),/invalid_application_response/);
const a={serialNo:'A',tid:'O1',applyStatus:1},b={serialNo:'B',tid:'O2',applyStatus:1};
const result=await run(apps,[{code:200,total:2,data:[a,b]}]);
assert.equal(result.rows.length,2);assert.equal(result.total,2);assert.equal(result.api_total,2);
const c={serialNo:'C',tid:'O3',applyStatus:6};
const filtered=await run(apps,[{code:200,total:2,data:[a,c,b]}]);
assert.equal(filtered.total,3);assert.equal(filtered.api_total,2);assert.equal(filtered.observed_total,3);
assert.equal(filtered.list_non_pending_snapshot_rows.length,1);
assert.equal(filtered.list_non_pending_snapshot_rows[0].serialNo,'C');
assert.equal(filtered.list_non_pending_snapshot_rows[0].applyStatus,6);
const fullPage=[a,...Array.from({length:19},(_,i)=>({serialNo:'P'+i,tid:'OP'+i,applyStatus:1}))];
await assert.rejects(()=>run(apps,[{code:200,total:21,data:fullPage},{code:200,total:21,data:[a]}]),/duplicate/);
const overlapped=await run(allInput,[{code:200,total:21,data:fullPage},{code:200,total:21,data:[a,b,c]}]);
assert.equal(overlapped.total,22);assert.equal(overlapped.api_total,21);
assert.equal(overlapped.duplicate_snapshot_row_count,1);
assert.equal(overlapped.rows.filter(x=>x.serialNo==='A').length,1);
assert.equal(overlapped.list_non_pending_snapshot_rows.length,1);
await assert.rejects(()=>run(apps,[{code:200,total:21,data:fullPage},
  {code:200,total:21,data:[{...a,amount:'9.99'},b]}]),/conflicting_duplicate_application/);
assert.equal((await run(apps,[{code:200,total:1,data:[{...a,applyGmtCreate:'2026-01-01 10:00:00'}]}])).rows[0].applyTime,'2026-01-01 10:00:00');
await assert.rejects(()=>run(apps,[{code:200,total:21,data:fullPage},{code:200,total:22,data:[c]}]),/changed/);
await assert.rejects(()=>run(apps,[{code:200,total:2,data:[]}]),/incomplete/);
const input={operation:'orders',orders:['O1','O2'],query:{}};
const page=(id)=>({page:{totalNumber:2,totalPage:2},query:{},mainOrders:[{id,subOrders:[{idStr:id+'-S',quantity:1,itemInfo:{title:'商品',extra:[{name:'商家编码',value:'SKU-'+id}]}}]}]});
const orders=await run(input,[page('O1'),page('O2')],{hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'});
assert.equal(orders.items[0].title,'商品');assert.equal(orders.items.length,2);
await assert.rejects(()=>run(input,[page('O1'),page('O1')],{hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'}),/duplicate/);

// The pacing contract covers real fetch attempts, not just outer batches.
const soldLocation={hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'};
const pacingClock={elapsed:0,waits:[]},fetchTimes=[];
const pacedFetch=async()=>{fetchTimes.push(pacingClock.elapsed);return response(page(fetchTimes.length%2?'O1':'O2'));};
await run(input,[],soldLocation,pacedFetch,pacingClock);
await run(input,[],soldLocation,pacedFetch,pacingClock);
assert.deepEqual(fetchTimes,[10000,20000,30000,40000]);
for(const failure of ['network','server']){
  const clock={elapsed:0,waits:[]},times=[];
  await run(input,[],soldLocation,async()=>{
    times.push(clock.elapsed);
    if(times.length===1){
      if(failure==='network')throw new TypeError('temporary network failure');
      return response({}, {status:503,ok:false});
    }
    return response(page(times.length===2?'O1':'O2'));
  },clock);
  assert.equal(times.length,3);
  assert.equal(times[0],10000);
  assert.equal(times[1]-times[0],10000);
  assert.equal(times[2]-times[1],10000);
}
// A platform throttle ends this collection without retrying or requesting
// the next page. Invoice/export operations use the same stop behavior.
for(const status of [429,403]){
  const clock={elapsed:0,waits:[]},times=[];
  await assert.rejects(()=>run(input,[],soldLocation,async()=>{
    times.push(clock.elapsed);return response({}, {status,ok:false});
  },clock),status===429?/rate_limited/:/permission_required/);
  assert.deepEqual(times,[10000]);
}
const invoiceClock={elapsed:0,waits:[]};
await run(apps,[{code:200,total:0}],undefined,null,invoiceClock);
assert.deepEqual(invoiceClock.waits,[5000]);

// All invoice reads are paced, including pagination, exports and transient
// retries. An HTTP throttle, login, or challenge never becomes empty data.
const invoicePagesClock={elapsed:0,waits:[]};
await run(apps,[{code:200,total:21,data:fullPage},{code:200,total:21,data:[b]}],undefined,null,invoicePagesClock);
assert.deepEqual(invoicePagesClock.waits,[5000,5000]);
const exportClock={elapsed:0,waits:[]};
await run({...apps,operation:'export'},[],undefined,async()=>({ok:true,status:200,arrayBuffer:async()=>new Uint8Array([80,75]).buffer}),exportClock);
assert.deepEqual(exportClock.waits,[5000]);
for(const failure of ['network','server']){
  const clock={elapsed:0,waits:[]},times=[];
  await run(apps,[],undefined,async()=>{
    times.push(clock.elapsed);
    if(times.length===1){if(failure==='network')throw new TypeError('temporary');return response({}, {ok:false,status:503});}
    return response({code:200,total:0});
  },clock);
  assert.deepEqual(times,[5000,10000]);
}
const blockedResponses=[
  {reply:response({}, {ok:false,status:429}),reason:/rate_limited/},
  {reply:response({}, {ok:false,status:403}),reason:/permission_required/},
  {reply:response({}, {url:'https://trade.taobao.com/_____tmd_____/punish'}),reason:/verification_required/},
  {reply:response({code:200,total:0,message:'请完成滑块验证'}),reason:/verification_required/},
  {reply:response({code:200,total:0,ret:['FAIL_SYS_USER_VALIDATE']}),reason:/verification_required/},
  {reply:response({code:200,total:0,data:{url:'https://trade.taobao.com/_____tmd_____/punish'}}),reason:/verification_required/},
  {reply:response({message:'请重新登录'}),reason:/login_required/},
  {reply:{...response({}),text:async()=>'<html><body>请完成安全验证</body></html>',
    arrayBuffer:async()=>new TextEncoder().encode('<html><body>请完成安全验证</body></html>').buffer},reason:/verification_required/}
];
for(const {reply,reason} of blockedResponses){
  for(const requestInput of [apps,{...apps,operation:'export'},input]){
    let calls=0;
    await assert.rejects(()=>run(requestInput,[],requestInput.operation==='orders'?soldLocation:undefined,
      async()=>{calls++;return reply;}),reason);
    assert.equal(calls,1);
  }
  let contextCalls=0;
  await assert.rejects(()=>runContext({},undefined,undefined,{fetch:async()=>{contextCalls++;return reply;}}),reason);
  assert.equal(contextCalls,1);
}
// A product title mentioning verification is business data, not a block.
assert.equal((await run(apps,[{code:200,total:1,data:[{...a,title:'验证码测试商品'}]}])).total,1);
const contextClock={elapsed:0,waits:[]};
await runContext({},undefined,undefined,{},contextClock);
assert.deepEqual(contextClock.waits,[5000,5000]);
const agentResources=ids=>({getEntriesByType:()=>ids.map(id=>({name:'https://einvoice.taobao.com/api/test?agentId='+id}))});
const emptyAgentDom={querySelector:()=>({value:''})};
assert.equal((await runContext({agent_id:null},undefined,undefined,
  {document:emptyAgentDom,performance:agentResources(['8','8'])})).agentId,'8');
assert.equal((await runContext({agent_id:null},undefined,undefined,
  {document:{querySelector:()=>({getAttribute:key=>key==='data-agent-id'?'8':null})},performance:agentResources(['8'])})).agentId,'8');
await assert.rejects(()=>runContext({agent_id:null},undefined,undefined,{document:emptyAgentDom,performance:agentResources(['8','9'])}),/context_ambiguous_agent_id/);
await assert.rejects(()=>runContext({agent_id:'0'},undefined,undefined,{document:emptyAgentDom,performance:agentResources(['8'])}),/context_changed_agent_id/);

// Challenge discovery in the wait prevents dispatch. Discovery while a body
// is pending aborts it; the next page/retry is never issued. Tests use no network.
const challengeNode={tagName:'IFRAME',src:'https://trade.taobao.com/_____tmd_____/punish',
  getBoundingClientRect:()=>({width:400,height:300})};
let showChallenge=false;
const challengeDocument={querySelector:()=>null,querySelectorAll:()=>showChallenge?[challengeNode]:[]};
const waitClock={elapsed:0,waits:[],onSleep:()=>{showChallenge=true;}};
let waitCalls=0;
await assert.rejects(()=>run(apps,[],undefined,async()=>{waitCalls++;return response({code:200,total:0});},waitClock,
  {document:challengeDocument}),/verification_required/);
assert.equal(waitCalls,0);
for(const mode of ['timeout','challenge']){
  const clock={elapsed:0,waits:[]};let calls=0,aborts=0;
  showChallenge=false;
  await assert.rejects(()=>run(input,[],soldLocation,async(url,options)=>{
    calls++;return {...response({}),arrayBuffer:()=>new Promise((resolve,reject)=>{
      options.signal.addEventListener('abort',()=>{aborts++;reject(Object.assign(Error('aborted'),{name:'AbortError'}));},{once:true});
      if(mode==='timeout')clock.fireDeadlines();else{showChallenge=true;clock.tickGuards();}
    })};
  },clock,{document:challengeDocument}),mode==='timeout'?/aborted/:/verification_required/);
  assert.equal(calls,mode==='timeout'?3:1);assert.equal(aborts,calls);
  assert.deepEqual(clock.waits,new Array(calls).fill(10000));
}
showChallenge=false;
let stoppedCalls=0;
await assert.rejects(()=>run(apps,[],undefined,async()=>{stoppedCalls++;return response({code:200,total:0});},undefined,
  {__qianniuInvoiceStopRequested:true}),/verification_required/);
assert.equal(stoppedCalls,0);
const identityTimeoutClock={elapsed:0,waits:[]};let identityTimeoutCalls=0;
await assert.rejects(()=>runContext({},undefined,undefined,{fetch:async(url,options)=>{
  identityTimeoutCalls++;return {...response({}),text:()=>new Promise((resolve,reject)=>{
    options.signal.addEventListener('abort',()=>reject(Object.assign(Error('body_timeout'),{name:'AbortError'})),{once:true});
    identityTimeoutClock.fireDeadlines();
  })};
}},identityTimeoutClock),/body_timeout/);
assert.equal(identityTimeoutCalls,1);

// The controller can clear its page flag for a newer operation while an old
// evaluation is sleeping. Its stop event must permanently cancel that old run.
for(const sourceName of ['read','context'])for(const phase of ['wait','body']){
  const listeners=new Set();
  const fakeGlobal={__qianniuInvoiceStopRequested:false,
    addEventListener:(name,callback)=>{assert.equal(name,'qianniu-invoice-stop');listeners.add(callback);},
    removeEventListener:(name,callback)=>listeners.delete(callback)};
  const cancel=()=>{
    fakeGlobal.__qianniuInvoiceStopRequested=true;for(const listener of listeners)listener();
    fakeGlobal.__qianniuInvoiceStopRequested=false;
  };
  const clock={elapsed:0,waits:[],onSleep:phase==='wait'?cancel:undefined};
  let calls=0,aborts=0;
  const overrides={globalThis:fakeGlobal,fetch:async(url,options)=>{calls++;return {...response({}),
    text:()=>new Promise((resolve,reject)=>{
      options.signal.addEventListener('abort',()=>{aborts++;reject(Object.assign(Error('event_aborted'),{name:'AbortError'}));},{once:true});
      cancel();
    })};}};
  await assert.rejects(()=>sourceName==='read'?run(apps,[],undefined,null,clock,overrides):
    runContext({},undefined,undefined,overrides,clock),/verification_required/);
  assert.equal(calls,phase==='wait'?0:1);assert.equal(aborts,calls);assert.equal(listeners.size,0);
}

// Detail rows expose the true suborder id on a React fiber
// ancestor. It must remain a string because a 19-digit numeric id is lossy.
const detailOrderNo='9000000000000000001';
const detailTitle='测试商品';
const detailCode='TEST-SKU-250g';
const detailRuntime=(overrides={})=>({id:detailOrderNo,subOrders:[{
  idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle,extra:[{name:'商家编码',value:detailCode}],skuText:[]}
}],...overrides});
function detailRow(order,options={}){
  const cells=[
    {innerText:options.firstCell??`${detailTitle}\n商家编码:${detailCode}`,
      querySelectorAll:selector=>selector==='a[href]'?(options.snapshotLinks??[]).map(link=>({
        innerText:link.title??detailTitle,getAttribute:name=>name==='href'?link.href:null
      })):[]},
    {innerText:options.specification??'规格: 250g'},
    {innerText:'交易成功'},
    {innerText:options.priceCell??'140.00\nx1'}
  ];
  const row={innerText:cells.map(cell=>cell.innerText).join('\n'),querySelectorAll:selector=>selector==='td'?cells:[]};
  if(!options.noFiber)Object.defineProperty(row,'__reactFiber$unit',{value:{memoizedProps:{className:'order-item'},return:{memoizedProps:order,return:null}}});
  return row;
}
function runDetail(order=detailRuntime(),options={}){
  const rows=options.rows??[detailRow(order,options)];
  return vm.runInNewContext(detail.replace('__INPUT__',JSON.stringify({order_no:detailOrderNo})),{
    ...base,location:{href:`https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId=${detailOrderNo}`},
    document:{body:{innerText:`订单编号 ${detailOrderNo}`},querySelectorAll:selector=>selector==='tr'?rows:[]}
  });
}
const detailed=runDetail();
assert.equal(detailed.items[0].sub_order_no,detailOrderNo);
assert.equal(detailed.items[0].title,detailTitle);
assert.equal(detailed.items[0].goods_code,detailCode);
assert.equal(detailed.items[0].quantity,'1');
assert.equal(detailed.items[0].unit_price,'140.00');
assert.throws(()=>runDetail(detailRuntime({id:9000000000000000001})),/runtime_order_id_not_string/);
assert.throws(()=>runDetail(detailRuntime({subOrders:[
  {idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle,extra:[{name:'商家编码',value:detailCode}]}},
  {idStr:'9000000000000000002',quantity:'1',itemInfo:{title:detailTitle,extra:[{name:'商家编码',value:detailCode}]}}
]})),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(detailRuntime({subOrders:[
  {idStr:9000000000000000001,quantity:'1',itemInfo:{title:detailTitle,extra:[{name:'商家编码',value:detailCode}]}}
]})),/runtime_sub_order_idStr_missing/);
// Identical titles/codes/quantities are disambiguated by the product link's
// exact string suborder ID, regardless of DOM order or displayed price.
const snapshotHref=id=>`//trade.taobao.com/trade/detail/tradeSnap.htm?tradeID=${id}&mi_id=private-fixture`;
const snapshotLink=id=>({href:snapshotHref(id)});
const sameFields=detailRuntime({subOrders:[detailRuntime().subOrders[0],
  {...detailRuntime().subOrders[0],idStr:'9000000000000000002'}]});
const snapshotRows=[
  detailRow(sameFields,{snapshotLinks:[snapshotLink(detailOrderNo)],priceCell:'19.90 x1',specification:'规格: 超大特级'}),
  detailRow(sameFields,{snapshotLinks:[snapshotLink('9000000000000000002')],priceCell:'18.80 x1',specification:'规格: 一级'})
];
const snapshotDetail=runDetail(sameFields,{rows:snapshotRows});
assert.deepEqual(Array.from(snapshotDetail.items,item=>[item.sub_order_no,item.unit_price,item.specification]),[
  [detailOrderNo,'19.90','规格: 超大特级'],['9000000000000000002','18.80','规格: 一级']]);
assert.deepEqual(Array.from(runDetail(sameFields,{rows:[...snapshotRows].reverse()}).items,item=>item.sub_order_no),
  ['9000000000000000002',detailOrderNo]);
assert.equal(snapshotDetail.items[0].sub_order_id_source,'product_snapshot_tradeID');
assert.equal(snapshotDetail.items[0].snapshot_url,`https://trade.taobao.com/trade/detail/tradeSnap.htm?tradeID=${detailOrderNo}`);
assert.equal(JSON.stringify(snapshotDetail).includes('private-fixture'),false);
assert.throws(()=>runDetail(sameFields,{rows:snapshotRows.slice(0,1)}),/incomplete_order_detail/);
assert.throws(()=>runDetail(sameFields,{rows:[snapshotRows[0],snapshotRows[0]]}),/duplicate_order_detail_row/);
assert.throws(()=>runDetail(sameFields,{snapshotLinks:[snapshotLink(detailOrderNo),snapshotLink('9000000000000000002')]}),
  /snapshot_sub_order_not_unique/);
assert.equal(runDetail(detailRuntime(),{snapshotLinks:[snapshotLink(detailOrderNo),snapshotLink(detailOrderNo)]}).verified_order,true);
assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[snapshotLink('9000000000000000003')]}),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[{href:snapshotHref(detailOrderNo),title:'错误标题'}]}),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[snapshotLink(detailOrderNo)],priceCell:'19.90 x2'}),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[snapshotLink(detailOrderNo)],firstCell:`${detailTitle}\n商家编码:OTHER`}),/runtime_sub_order_not_unique/);
for(const href of [
  `https://trade.taobao.com.evil.example/trade/detail/tradeSnap.htm?tradeID=${detailOrderNo}`,
  `https://trade.taobao.com/other/tradeSnap.htm?tradeID=${detailOrderNo}`,
  `https://trade.taobao.com:8443/trade/detail/tradeSnap.htm?tradeID=${detailOrderNo}`,
  `https://name@trade.taobao.com/trade/detail/tradeSnap.htm?tradeID=${detailOrderNo}`,
  `ftp://trade.taobao.com/trade/detail/tradeSnap.htm?tradeID=${detailOrderNo}`
])assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[{href}]}),/snapshot_url_invalid/);
for(const id of ['', '0', '09000000000000000001', '9000000000000000001x', '9e18', `${detailOrderNo}&tradeID=${detailOrderNo}`]){
  assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[snapshotLink(id)]}),/snapshot_sub_order_id_invalid/);
}
assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[{href:snapshotHref(detailOrderNo),title:''}]}),/snapshot_title_missing/);
assert.throws(()=>runDetail(detailRuntime(),{snapshotLinks:[snapshotLink(detailOrderNo),{href:snapshotHref(detailOrderNo),title:'另一标题'}]}),/snapshot_title_conflict/);
assert.equal(runDetail(detailRuntime(),{snapshotLinks:[{href:'https://item.taobao.com/item.htm?id=123'}]}).verified_order,true);
// The live code-less product row still has a complete React order, title,
// positive quantity and the original DOM price cell. Its notice is not title.
const notice='当前订单无发货时间和发货倒计时信息';
const noCodeRuntime=detailRuntime({subOrders:[{idStr:detailOrderNo,quantity:'4800',
  itemInfo:{title:detailTitle,extra:[{visible:'SOLID',value:notice}],skuText:[]}}]});
const noCodeDom={firstCell:`${detailTitle}\n${notice}`,priceCell:'0.01\n\nx4800'};
const noCodeDetail=runDetail(noCodeRuntime,noCodeDom);
assert.equal(noCodeDetail.verified_order,true);
assert.equal(noCodeDetail.items[0].goods_code,'');
assert.equal(noCodeDetail.items[0].goods_code_missing,true);
assert.equal(noCodeDetail.items[0].title,detailTitle);
assert.equal(noCodeDetail.items[0].quantity,'4800');
assert.equal(noCodeDetail.items[0].price_cell,'0.01\n\nx4800');
assert.equal(noCodeDetail.items[0].unit_price,'0.01');
assert.equal(runDetail(noCodeRuntime,{...noCodeDom,snapshotLinks:[snapshotLink(detailOrderNo)]}).items[0].goods_code_missing,true);
const noCodePlain=detailRuntime({subOrders:[{idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle,extra:[]}}]});
assert.equal(runDetail(noCodePlain,{firstCell:detailTitle}).items[0].goods_code,'');
const explicitEmptyCode=detailRuntime({subOrders:[{idStr:detailOrderNo,quantity:'1',
  itemInfo:{title:detailTitle,extra:[{name:'商家编码',value:''}]}}]});
assert.equal(runDetail(explicitEmptyCode,{firstCell:`${detailTitle}\n商家编码:`}).items[0].goods_code,'');
assert.throws(()=>runDetail(noCodeRuntime,{...noCodeDom,rows:[]}),/incomplete_order_detail/);
assert.throws(()=>runDetail(noCodeRuntime,{...noCodeDom,noFiber:true}),/incomplete_order_detail/);
assert.throws(()=>runDetail(noCodeRuntime,{...noCodeDom,priceCell:'0.01\nx0'}),/incomplete_order_detail/);
assert.throws(()=>runDetail(noCodeRuntime,{...noCodeDom,firstCell:`${detailTitle}\n未知提示`}),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(noCodePlain),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(detailRuntime(),{firstCell:detailTitle}),/runtime_sub_order_not_unique/);
// Live responses may omit optional extra entirely. This is only accepted
// when every rendered product has a unique complete runtime counterpart.
const omittedExtra=detailRuntime({subOrders:[{idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle}}]});
const omittedDetail=runDetail(omittedExtra,{firstCell:detailTitle});
assert.equal(omittedDetail.items[0].goods_code_missing,true);
assert.equal(omittedDetail.items[0].goods_code,'');
assert.equal(omittedDetail.items[0].sub_order_no,detailOrderNo);
assert.throws(()=>runDetail(omittedExtra),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(omittedExtra,{firstCell:`${detailTitle}\n未知提示`}),/runtime_sub_order_not_unique/);
for(const extra of [null,{},'unloaded',undefined]){
  assert.throws(()=>runDetail(detailRuntime({subOrders:[{idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle,extra}}]}),
    {firstCell:detailTitle}),/runtime_goods_code_not_loaded/);
}
const omittedMulti=detailRuntime({subOrders:[...omittedExtra.subOrders,
  {idStr:'9000000000000000002',quantity:'2',itemInfo:{title:'另一商品'}}]});
const omittedRows=[detailRow(omittedMulti,{firstCell:detailTitle}),
  detailRow(omittedMulti,{firstCell:'另一商品',priceCell:'10.00 x2'})];
assert.equal(runDetail(omittedMulti,{rows:omittedRows}).items.length,2);
assert.throws(()=>runDetail(omittedMulti,{rows:omittedRows.slice(0,1)}),/incomplete_order_detail/);
assert.throws(()=>runDetail(detailRuntime({subOrders:[...omittedExtra.subOrders,
  {...omittedExtra.subOrders[0],idStr:'9000000000000000002'}]}),{firstCell:detailTitle}),/runtime_sub_order_not_unique/);
assert.throws(()=>runDetail(detailRuntime({subOrders:[...noCodePlain.subOrders,
  {idStr:'9000000000000000002',quantity:'1',itemInfo:{title:'另一商品',extra:[]}}]}),
  {firstCell:detailTitle}),/incomplete_order_detail/);
assert.throws(()=>runDetail(noCodePlain,{rows:[detailRow(noCodePlain,{firstCell:detailTitle}),
  detailRow(noCodePlain,{firstCell:detailTitle})]}),/duplicate_order_detail_row/);
assert.throws(()=>runDetail(detailRuntime({subOrders:[...noCodePlain.subOrders,...noCodePlain.subOrders]}),
  {firstCell:detailTitle}),/runtime_sub_order_idStr_duplicate/);
assert.throws(()=>runDetail(detailRuntime({subOrders:[{idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle,
  extra:[{name:'商家编码',value:detailCode},{name:'商家编码',value:'conflict'}]}}]})),/runtime_goods_code_conflict/);
let sparseBody;
const onePage={page:{totalNumber:1,totalPage:1},query:{},mainOrders:[{id:'O1',subOrders:[]}]};
const sparse=await run({operation:'orders',orders:['O1'],query:{tabCode:'latest3Months'}},[onePage],
  {hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'},
  async(url,options)=>{sparseBody=new URLSearchParams(options.body);return response(onePage);});
assert.equal(sparse.items.length,0);
assert.equal(sparseBody.get('action'),'itemlist/SoldQueryAction');
assert.equal(sparseBody.get('pageSize'),'15');
assert.equal(sparseBody.get('bizOrderId'),'O1');
assert.equal(sparseBody.get('isBatchSearch'),'true');
assert.ok(sparse.query_fingerprint.includes('itemlist/SoldQueryAction'));
let requestBody;
const fn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>{requestBody=JSON.parse(options.body);return response({code:0,act:0,data:[{sku_id:'SKU',enabled:-1,invoice_enabled:true}]});}});
const goods=await fn(null,{codes:['SKU'],context:{coid:'test',uid:'test'}});
assert.equal(goods.mapped_count,1);assert.equal('enabled' in requestBody.data,false);assert.equal('sku_type' in requestBody.data,false);
assert.equal(typeof goods.queried_at,'string');assert.equal('rawInput' in goods,false);
for (const invalidInput of [null, [], 'SKU', JSON.stringify({codes:['SKU']})]) {
  assert.equal((await fn(null,invalidInput)).reason,'input_invalid');
}
const normalized=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async()=>response({code:0,act:0,data:[{sku_id:'SKU（红盒）',invoice_enabled:true}]})});
const normalizedResult=await normalized(null,{codes:['SKU(红盒)'],context:{coid:'test',uid:'test'}});
assert.equal(normalizedResult.data[0].ok,true);assert.equal(normalizedResult.data[0].match_basis,'sku_id_paren_width');
const collision=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async()=>response({code:0,act:0,data:[{sku_id:'SKU(红盒)'},{sku_id:'SKU（红盒）'}]})});
const collisionResult=await collision(null,{codes:['SKU(红盒)'],context:{coid:'test',uid:'test'}});
assert.equal(collisionResult.data[0].ok,false);assert.equal(collisionResult.data[0].reason,'multiple_exact_matches');
assert.equal(collisionResult.ok,true);assert.equal(collisionResult.blocked,false);
assert.equal(collisionResult.partial,true);
assert.equal(collisionResult.has_failures,true);assert.equal(collisionResult.all_matched,false);
const mixedFn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>response({code:0,act:0,data:JSON.parse(options.body).data.sku_id==='@@FOUND'?[{sku_id:'FOUND'}]:[]})});
const mixed=await mixedFn(null,{codes:['FOUND','MISSING'],context:{coid:'test',uid:'test'}});
assert.equal(mixed.ok,true);assert.equal(mixed.blocked,false);assert.equal(mixed.partial,true);assert.equal(mixed.has_failures,true);assert.equal(mixed.all_matched,false);
assert.equal(mixed.mapped_count,1);assert.equal(mixed.data[0].ok,true);assert.equal(mixed.data[1].reason,'no_exact_match');
assert.equal(goods.has_failures,false);assert.equal(goods.all_matched,true);
const noContext=await mixedFn(null,{codes:['FOUND']});
assert.equal(noContext.ok,false);assert.equal(noContext.blocked,true);assert.equal(noContext.reason,'request_context_missing');
const multi=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async()=>response({code:0,act:0,page:{pages:2},data:[{sku_id:'SKU'}]})});
assert.equal((await multi(null,{codes:['SKU'],context:{coid:'test',uid:'test'}})).data[0].reason,'request_failed');

// JST requests run in a bounded eight-worker pool and keep response order even
// when the server answers out of order.
let active=0,maxActive=0;
const orderFn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>{
    active+=1;maxActive=Math.max(maxActive,active);
    const body=JSON.parse(options.body), code=body.data.sku_id.slice(2);
    await new Promise(resolve=>setTimeout(resolve, (10-Number(code))*2));
    active-=1;
    return response({code:0,act:0,data:[{sku_id:code}]});
  }});
const ordered=await orderFn(null,{codes:['1','2','3','4','5','6','7','8'],context:{coid:'test',uid:'test'}});
assert.equal(ordered.ok,true);assert.equal(ordered.concurrency,8);assert.equal(maxActive,8);
assert.deepEqual(Array.from(ordered.data,item=>item.input_goods_code),['1','2','3','4','5','6','7','8']);

// Transient network errors are retried twice and expose stable classification
// and attempt counts in the per-code result.
let retryAttempts=0;
const retryFn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async()=>{ retryAttempts+=1; if(retryAttempts<3) throw Object.assign(new TypeError('temporary network'),{name:'TypeError'}); return response({code:0,act:0,data:[{sku_id:'RETRY'}]}); }});
const retried=await retryFn(null,{codes:['RETRY'],context:{coid:'test',uid:'test'}});
assert.equal(retried.ok,true);assert.equal(retried.retry_count,2);assert.equal(retried.data[0].attempts,3);assert.equal(retried.data[0].retry_count,2);
let statusAttempts=0;
const statusFn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async()=>{ statusAttempts+=1; if(statusAttempts<3) return response({message:'busy'},{status:429,ok:false}); return response({code:0,act:0,data:[{sku_id:'RATE'}]}); }});
const rateLimited=await statusFn(null,{codes:['RATE'],context:{coid:'test',uid:'test'}});
assert.equal(rateLimited.ok,true);assert.equal(rateLimited.data[0].attempts,3);assert.equal(rateLimited.data[0].retry_count,2);

// Authentication loss aborts the stage instead of retrying or scanning the
// remaining codes. The error is machine-readable for the coordinator.
let authCalls=0;
const authFn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>{ authCalls+=1; if(authCalls===1) return response({message:'unauthorized'}, {status:401,ok:false,url:'https://passport.example/login'}); await new Promise(resolve=>setTimeout(resolve,20)); if(options.signal && options.signal.aborted) throw Object.assign(new Error('aborted'),{name:'AbortError'}); return response({code:0,act:0,data:[{sku_id:'OTHER'}]}); }});
await assert.rejects(()=>authFn(null,{codes:['AUTH','OTHER1','OTHER2','OTHER3','OTHER4','OTHER5','OTHER6','OTHER7','OTHER8','OTHER9'],context:{coid:'test',uid:'test'}}),error=>error && error.code==='auth_required');
assert.ok(authCalls<=8,`auth failure started ${authCalls} requests; expected bounded fail-fast pool`);

// Cancellation still applies after headers arrive. An authentication failure
// aborts pending response bodies without retrying their cancelled requests.
let bodyCalls=0,bodyAborts=0,authBodyRead=false;
const abortingBody=(signal)=>new Promise((resolve,reject)=>{
  const abort=()=>{bodyAborts+=1;reject(Object.assign(new Error('body aborted'),{name:'AbortError'}));};
  if(signal.aborted) abort(); else signal.addEventListener('abort',abort,{once:true});
});
const authBodyFn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>{
    bodyCalls+=1;
    if(JSON.parse(options.body).data.sku_id==='@@AUTH') return {...response({}, {status:401,ok:false}),text:async()=>{authBodyRead=true;return '';}};
    return {...response({}),text:()=>abortingBody(options.signal)};
  }});
await assert.rejects(()=>authBodyFn(null,{codes:['SLOW','AUTH','SLOW2','SLOW3','SLOW4','SLOW5','SLOW6','SLOW7','UNSTARTED'],context:{coid:'test',uid:'test'}}),error=>error && error.code==='auth_required');
assert.equal(authBodyRead,false);assert.equal(bodyCalls,8);assert.equal(bodyAborts,7);

// The deadline must cover response.text(), not only receipt of headers.
let timeoutCalls=0;
const timeoutFn=vm.runInNewContext('('+jst+')',{...base,setTimeout:(fn)=>setTimeout(fn,5),location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>{timeoutCalls+=1;return {...response({}),text:()=>abortingBody(options.signal)};}});
const timedOut=await timeoutFn(null,{codes:['SLOW'],context:{coid:'test',uid:'test'}});
assert.equal(timeoutCalls,3);assert.equal(timedOut.ok,true);assert.equal(timedOut.blocked,false);assert.equal(timedOut.partial,true);assert.equal(timedOut.has_failures,true);assert.equal(timedOut.all_matched,false);
assert.equal(timedOut.data[0].reason,'request_failed');assert.equal(timedOut.data[0].error_class,'timeout');
assert.equal(timedOut.data[0].attempts,3);assert.equal(timedOut.data[0].retry_count,2);assert.equal(timedOut.data[0].retry_exhausted,true);

// Controller cancellation is delivered to the executing goods iframe. Abort
// every in-flight worker, keep the cancellation latched after a flag reset,
// and never retry cancelled requests or start the ninth code.
for(const phase of ['before','fetch','body']){
  const listeners=new Set(),timers=new Map();let timerId=0;
  const fakeGlobal={__qianniuInvoiceStopRequested:phase==='before',
    addEventListener:(name,callback)=>{assert.equal(name,'qianniu-invoice-stop');listeners.add(callback);},
    removeEventListener:(name,callback)=>listeners.delete(callback)};
  const cancel=()=>{
    fakeGlobal.__qianniuInvoiceStopRequested=true;for(const listener of listeners)listener();
    fakeGlobal.__qianniuInvoiceStopRequested=false;
  };
  let calls=0,pending=0,aborts=0,success=false;
  const blocked=signal=>new Promise((resolve,reject)=>{
    signal.addEventListener('abort',()=>{aborts++;reject(Object.assign(Error('cancelled'),{name:'AbortError'}));},{once:true});
    pending++;if(pending===8)cancel();
  });
  const cancelledJst=vm.runInNewContext('('+jst+')',{...base,globalThis:fakeGlobal,
    setTimeout:(callback,ms)=>{assert.equal(ms,8000);const id=++timerId;timers.set(id,callback);return id;},
    clearTimeout:id=>timers.delete(id),
    location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
    fetch:async(url,options)=>{
      calls++;
      if(success)return response({code:0,act:0,data:[{sku_id:JSON.parse(options.body).data.sku_id.slice(2)}]});
      if(phase==='fetch')return blocked(options.signal);
      return {...response({}),text:()=>blocked(options.signal)};
    }});
  await assert.rejects(()=>cancelledJst(null,{codes:Array.from({length:10},(_,index)=>String(index)),
    context:{coid:'test',uid:'test'}}),error=>error.code==='verification_required');
  assert.equal(calls,phase==='before'?0:8);assert.equal(aborts,calls);
  assert.equal(listeners.size,0);assert.equal(timers.size,0);
  // A new invocation may legitimately run after manual recovery; it cannot
  // inherit either the previous latch or a leaked event listener.
  fakeGlobal.__qianniuInvoiceStopRequested=false;success=true;calls=0;
  const resumed=await cancelledJst(null,{codes:['RESUMED'],context:{coid:'test',uid:'test'}});
  assert.equal(resumed.mapped_count,1);assert.equal(calls,1);
  assert.equal(listeners.size,0);assert.equal(timers.size,0);
}
console.log('collector checks passed (mock responses; includes partial results, auth cancellation, and body timeout)');
