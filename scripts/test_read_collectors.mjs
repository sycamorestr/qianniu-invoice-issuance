import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const qianniu=await fs.readFile(new URL('./read_qianniu.js',import.meta.url),'utf8');
const jst=await fs.readFile(new URL('./query_jst_invoice_goods.yingdao.js',import.meta.url),'utf8');
const base={URL,URLSearchParams,TextDecoder,Uint8Array,AbortSignal,setTimeout,btoa};
const response=body=>({ok:true,status:200,url:'https://business.example/read',json:async()=>body,
  text:async()=>JSON.stringify(body),arrayBuffer:async()=>new TextEncoder().encode(JSON.stringify(body)).buffer,
  headers:{get:()=> 'application/json; charset=utf-8'}});
async function run(input,bodies,location={hostname:'myseller.taobao.com',pathname:'/home.htm/merchant-invoice/'}){
  let at=0;
  return await vm.runInNewContext(qianniu.replace('__INPUT__',JSON.stringify(input)),{...base,location,fetch:async()=>response(bodies[at++])});
}
const apps={operation:'applications',date:'2026-01-01',agentId:'0'};
const a={serialNo:'A',tid:'O1',applyStatus:1},b={serialNo:'B',tid:'O2',applyStatus:1};
const result=await run(apps,[{code:200,total:2,data:[a]},{code:200,total:2,data:[b]}]);
assert.equal(result.rows.length,2);
await assert.rejects(()=>run(apps,[{code:200,total:2,data:[a]},{code:200,total:2,data:[a]}]),/duplicate/);
await assert.rejects(()=>run(apps,[{code:200,total:2,data:[a]},{code:200,total:3,data:[b]}]),/changed/);
await assert.rejects(()=>run(apps,[{code:200,total:2,data:[]}]),/incomplete/);
const input={operation:'orders',orders:['O1','O2'],query:{}};
const page=(id)=>({page:{totalNumber:2,totalPage:2},query:{},mainOrders:[{id,subOrders:[{idStr:id+'-S',quantity:1,itemInfo:{title:'商品',extra:[{name:'商家编码',value:'SKU-'+id}]}}]}]});
const orders=await run(input,[page('O1'),page('O2')],{hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'});
assert.equal(orders.items[0].title,'商品');assert.equal(orders.items.length,2);
await assert.rejects(()=>run(input,[page('O1'),page('O1')],{hostname:'myseller.taobao.com',pathname:'/home.htm/trade-platform/tp/sold'}),/duplicate/);
let requestBody;
const fn=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async(url,options)=>{requestBody=JSON.parse(options.body);return response({code:0,act:0,data:[{sku_id:'SKU',enabled:-1,invoice_enabled:true}]});}});
const goods=JSON.parse(await fn(null,{codes:['SKU'],context:{coid:'test',uid:'test'}}));
assert.equal(goods.mapped_count,1);assert.equal('enabled' in requestBody.data,false);assert.equal('sku_type' in requestBody.data,false);
const multi=vm.runInNewContext('('+jst+')',{...base,location:{href:'https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index'},
  fetch:async()=>response({code:0,act:0,page:{pages:2},data:[{sku_id:'SKU'}]})});
assert.equal(JSON.parse(await multi(null,{codes:['SKU'],context:{coid:'test',uid:'test'}})).data[0].reason,'request_failed');
console.log('8 collector checks passed (mock responses; no live requests)');
