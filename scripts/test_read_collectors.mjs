import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const qianniu=await fs.readFile(new URL('./read_qianniu.js',import.meta.url),'utf8');
const jst=await fs.readFile(new URL('./query_jst_invoice_goods.js',import.meta.url),'utf8');
const detail=await fs.readFile(new URL('./read_order_detail.js',import.meta.url),'utf8');
const contextSource=await fs.readFile(new URL('./playwright_context_qianniu.js',import.meta.url),'utf8');
const base={URL,URLSearchParams,TextDecoder,Uint8Array,AbortSignal,AbortController,setTimeout,clearTimeout,btoa};
const response=(body,opts={})=>({ok:opts.ok===undefined?true:opts.ok,status:opts.status===undefined?200:opts.status,url:opts.url||'https://business.example/read',json:async()=>body,
  text:async()=>JSON.stringify(body),arrayBuffer:async()=>new TextEncoder().encode(JSON.stringify(body)).buffer,
  headers:{get:()=> 'application/json; charset=utf-8'}});
async function runContext(input, context={isLogin:true,realNick:'account:operator'}, shop={userNick:'account'}) {
  return vm.runInNewContext(contextSource.replace('__INPUT__',JSON.stringify({agent_id:'0',...input})), {
    ...base, document:{querySelector:()=>null}, performance:{getEntriesByType:()=>[]},
    location:{href:'https://myseller.taobao.com/home.htm/merchant-invoice/'},
    fetch:async url=>response({data:url.endsWith('/context')?context:shop})
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
assert.equal((await runContext({expected_store:'account'})).store,'account');
async function run(input,bodies,location={hostname:'myseller.taobao.com',pathname:'/home.htm/merchant-invoice/'},fetchOverride=null){
  let at=0;
  return await vm.runInNewContext(qianniu.replace('__INPUT__',JSON.stringify(input)),{...base,location,fetch:fetchOverride||(async()=>response(bodies[at++]))});
}
const apps={operation:'applications',date:'2026-01-01',agentId:'0'};
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
await assert.rejects(()=>run(apps,[{code:200,total:21,data:fullPage},{code:200,total:22,data:[c]}]),/changed/);
await assert.rejects(()=>run(apps,[{code:200,total:2,data:[]}]),/incomplete/);
const input={operation:'orders',orders:['O1','O2'],query:{}};
const page=(id)=>({page:{totalNumber:2,totalPage:2},query:{},mainOrders:[{id,subOrders:[{idStr:id+'-S',quantity:1,itemInfo:{title:'商品',extra:[{name:'商家编码',value:'SKU-'+id}]}}]}]});
const orders=await run(input,[page('O1'),page('O2')],{hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'});
assert.equal(orders.items[0].title,'商品');assert.equal(orders.items.length,2);
await assert.rejects(()=>run(input,[page('O1'),page('O1')],{hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'}),/duplicate/);

// Detail rows expose the true suborder id on a React fiber
// ancestor. It must remain a string because a 19-digit numeric id is lossy.
const detailOrderNo='9000000000000000001';
const detailTitle='测试商品';
const detailCode='TEST-SKU-250g';
const detailRuntime=(overrides={})=>({id:detailOrderNo,subOrders:[{
  idStr:detailOrderNo,quantity:'1',itemInfo:{title:detailTitle,extra:[{name:'商家编码',value:detailCode}],skuText:[]}
}],...overrides});
function detailRow(order){
  const cells=[
    {innerText:`${detailTitle}\n商家编码:${detailCode}`},
    {innerText:'规格: 250g'},
    {innerText:'交易成功'},
    {innerText:'140.00\nx1'}
  ];
  const row={innerText:cells.map(cell=>cell.innerText).join('\n'),querySelectorAll:selector=>selector==='td'?cells:[]};
  Object.defineProperty(row,'__reactFiber$unit',{value:{memoizedProps:{className:'order-item'},return:{memoizedProps:order,return:null}}});
  return row;
}
function runDetail(order=detailRuntime()){
  const row=detailRow(order);
  return vm.runInNewContext(detail.replace('__INPUT__',JSON.stringify({order_no:detailOrderNo})),{
    ...base,location:{href:`https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId=${detailOrderNo}`},
    document:{body:{innerText:`订单编号 ${detailOrderNo}`},querySelectorAll:selector=>selector==='tr'?[row]:[]}
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
console.log('collector checks passed (mock responses; includes partial results, auth cancellation, and body timeout)');
