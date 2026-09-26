# 页面接入与接口

本文用于接口或页面故障排查。接口基于已登录业务页面的实际调用，不是平台公开稳定 API；运行时仍须核对身份、查询范围及响应结构。租户值、用户标识和日期均取本次输入或当前页面，不能照抄历史快照。

## 页面环境

| 角色 | 页面 URL |
| --- | --- |
| `invoice` | `https://myseller.taobao.com/home.htm/merchant-invoice/` |
| `orders` | `https://myseller.taobao.com/home.htm/trade-platform/tp/sold` |
| `goods` | `https://fp.erp321.com/setting/goodsManage` |
| 票聚商品 iframe | `https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index` |

`playwright_adapter.py` 在一个后台事件循环中复用控制器。三个角色既可在同一浏览器，也可分为千牛 `invoice/orders` 与共享票聚 `goods` 两个浏览器；脚本依据 site 进入对应页面上下文。日常 `connect(open_missing=True)` 连接已经运行的专用 Edge，先复用页面，再仅按各自配置的角色 URL 补齐缺页一次，不误开另一站；不会启动浏览器进程。生命周期和故障边界见[浏览器控制](playwright-browser.md)。

`playwright_context_qianniu.js` 通过 `/api/context`、`/api/shops` 核对当前千牛登录与店铺。票聚外层页面提供公司显示名称，`playwright_context_jst.js` 从当前商品 iframe 资源记录提取 `coid`、`uid`。公司名和带操作员的标签分别保存，`agentId` 取当前页面运行时或已核对的主体接口值。页面存在、URL 无 login 均不能代替身份核验。

控制器在对应 `Page` 或 `Frame` 执行本地 JS。请求使用 `credentials: "include"` 复用浏览器会话，不导出 Cookie、令牌、完整请求头或 HAR。浏览器页面不需要切到前台。

多店共用票聚只共享其登录会话，不合并各店的订单或申请数据。每店开始都正向核验本店千牛和同一个票聚 issuer；采集结果写入各自作业目录。异常保留 `site=qianniu/jst`，使批次能够区分单店问题与共享故障；接口业务规则保持一致。

## 千牛申请诊断

`read_qianniu.js` 的 `applications` 操作调用：

```text
GET https://einvoice.taobao.com/api/qianniu/invoice/list/apply
?agentId=<本次值>&applyListType=0&pageSize=20
&startTime=YYYY-MM-DD&endTime=YYYY-MM-DD&pageNo=0
```

响应要求 `code=200`，数据在 `data`，页码从 0 开始。读取完整分页，流水号唯一，分页中服务端 total 必须稳定。字段包括 `serialNo`、`tid`、`amount`、`applyStatus`、`applyTime`。服务端 total 与实际观察行数可能不同，因此分别记录 `api_total` 和 `observed_total`。

申请列表只用于诊断和订单关联。`applyStatus`、页面“已准”等状态不能改变导出原件定义的选择范围。标准任务按单日运行，不继承页面上残留的买家、订单等筛选。

## 通用模板导出

`read_qianniu.js` 的 `export` 操作调用：

```text
GET https://einvoice.taobao.com/api/invoice/batch4visitor/apply
?startTime=YYYY-MM-DD&endTime=YYYY-MM-DD&pageNo=0&pageSize=20&agentId=<本次值>
```

该接口具有页面“全选后导出通用模板”的全量语义，无需操作搜索、全选或下载按钮。页面脚本直接读取响应二进制，经 Base64 传给本地编排器，解码保存为 `qianniu_common.xlsx`。没有额外的下载 URL 捕获或浏览器外会话重放步骤。

响应须通过 HTTP、登录跳转、ZIP 文件头、工作表和必要字段检查。数据以事务检查点发布，规则见[原子发布和恢复](input-output-contract.md#原子发布和恢复)。原始 XLSX 必须保留全部源行及顺序。

只有通用模板决定范围：优先“开票状态”，为空才回退“申请状态”，精确为“待处理”的源行进入生成。其他状态保留在原件中，不查询其订单或写入税局模板。

税局空白模板来自 `assets/tax-bureau-template-V260401.xlsx`，日常无需重新下载。模板版本更换需单独核验字段、隐藏字典及数据区，不能把通用模板当税局模板。

## 千牛订单批量查询

`read_qianniu.js` 的 `orders` 操作调用：

```text
POST https://trade.taobao.com/trade/itemlist/asyncSold.htm
?event_submit_do_query=1&_input_charset=utf8
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

bizOrderId=<最多50个逗号分隔订单号>
batchType=bizOrderId
isBatchSearch=true
pageNum=1
```

脚本补齐已验证的页面查询默认值，并记录响应 `query` 和查询指纹。当前默认范围为 `latest3Months`，不能把未返回项直接称为历史订单。若需历史批查，必须先验证相应参数；没有已验证参数时仅对缺失项补详情。

| 响应字段 | 用途 |
| --- | --- |
| `mainOrders[].id` | 主订单号 |
| `subOrders[].idStr` | 字符串子订单号 |
| `subOrders[].quantity` | 匹配数量 |
| `itemInfo.title` / `skuText[]` | 标题与规格 |
| `itemInfo.extra` 中“商家编码”的 `value` | 完整商品编码 |
| `priceInfo.realTotal` | 辅助信息，未经金额口径核验不能匹配源金额 |

按响应字符集解码，缺失时使用已验证的 GBK 默认值。读取 `page.totalNumber/totalPage` 指定的完整分页，检查订单属于本次输入、没有重复、返回与缺失集合覆盖请求。不能只取当前可见 DOM 列表。

## 必要订单详情补证

批量缺失或本地计划仍有歧义时，复用同一个 `orders` 页导航到：

```text
https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId=<订单号>
```

等待 URL 和页面订单号一致、商品行及商家编码就绪，再执行 `read_order_detail.js`。该脚本读取 DOM 商品行，并核验 React 运行时中的字符串子订单号，避免 19 位数字精度损失。读取结束后恢复同一页面的订单列表 URL。

`old_details.json`、`supplemental_details.json` 必须合并消费，字段冲突不能静默覆盖。`order_evidence.py` 检查同一商品行原始价格、数量、来源 URL/时间及与源商品金额的唯一对应，推导证据写入 `derived_match_evidence.json`。列表金额、行顺序、金额大小均不能替代可靠关联；页面结构变化或证据不足则暂缓相关整票。

## 票聚商品查询

`query_jst_invoice_goods.js` 在商品 iframe 中执行，接收对象 `{codes, context: {coid, uid}}`，直接返回对象。请求为：

```text
POST https://apiweb.erp321.com/webapi/ItemApi/ItemSku/GetPageListV2
Content-Type: application/json
```

```json
{
  "page": {"currentPage": 1, "pageSize": 50, "hasPageInfo": false, "pageAction": 1},
  "data": {"sku_id": "@@完整编码", "queryFlds": ["sku_id", "properties_value", "invoice_name", "issuing_office", "tax_code", "tax_rate", "tax_rate_zero", "invoice_enabled"]},
  "ip": "",
  "coid": "当前主体标识",
  "uid": "当前用户标识"
}
```

上例列出主要返回字段，完整 `queryFlds` 以脚本为准。`sku_type`、`enabled` 可以请求返回，但不作为筛选条件。商品类型和商品状态均不限，最后仍要求 `invoice_enabled=true`；否则会漏掉组合装或停用但允许开票的商品。

成功要求 `code=0` 且 `act=0`，数组在 `data`。完整 `sku_id` 必须唯一；仅全角/半角括号可归一后唯一匹配，保留原始编码、输入编码及 `match_basis`。多候选仍暂缓。若返回后续页或达到当前页上限，不得凭第一页宣称唯一。

编排器每批最多 40 编码；脚本逐编码查询，8 路并发，结果保持输入顺序。每请求 8 秒超时覆盖响应体读取，网络超时/Abort 和 429/502/503/504 最多重试 2 次。认证失效响应立即取消其余请求并终止阶段；其他异常记录为逐编码 `request_failed`。普通未命中和多匹配不反复查询。成功项与失败项一起保存，便于只补失败编码。

商品名称、规格、单位、税码、税率的业务解释见[字段映射](flow-and-field-mapping.md)。本技能不调用商品编辑、导入或开票提交接口。
