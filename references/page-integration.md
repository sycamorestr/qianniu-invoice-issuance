# 页面接入与接口证据

以下接口在原流程中已用登录页面验证。运行时仍须核对页面、主体、响应结构及查询条件，不能把历史版本号或租户值当默认值。此文档只描述采集和文件生成。

## 页面入口

| 页面 | URL |
| --- | --- |
| 千牛发票申请 | https://myseller.taobao.com/home.htm/merchant-invoice/ |
| 千牛订单 | https://myseller.taobao.com/home.htm/trade-platform/tp/sold |
| 票聚商品管理 | https://fp.erp321.com/setting/goodsManage |
| 票聚商品 iframe | https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index |

页面缺失时自行打开 URL 并复用登录态。核对页面标题、千牛店铺和票聚主体。票聚实际商品管理在跨域 iframe，不能把顶层空壳当成商品页。错误绑定解除后绑定正确页面。coid、uid、agentId 从当前已核对主体的真实请求/运行时取得。

## 千牛申请与导出

API 基址 https://einvoice.taobao.com，请求使用浏览器 credentials:include。

申请列表：
```text
GET /api/qianniu/invoice/list/apply
?agentId=<本次值>&applyListType=0&pageSize=20
&startTime=YYYY-MM-DD&endTime=YYYY-MM-DD&pageNo=0
```

成功 code=200，数组 data，总数 total，另有 hasMoreCount。字段 serialNo、tid、amount、applyStatus、applyTime、tradeLink。待处理状态实测 applyStatus=1。pageNo 从 0 起；完整分页数和唯一流水号数必须等于 total。

页面 searchContent 还可能包含 status、rightsRemainTime、rightsFlag、payerName、tid。按日期生成全日文件时确认没有残留买家、订单等额外筛选；若用户指定额外范围，应将该范围作为明确输入，不能默默继承。日期跨度不得超过页面允许的两个月，标准脚本逐日运行。

通用模板：
```text
GET /api/invoice/batch4visitor/apply
?startTime=YYYY-MM-DD&endTime=YYYY-MM-DD&pageNo=0&pageSize=20&agentId=<本次值>
```

它是必要源文件，包含申请流水号、订单编号、总金额、状态、货物名称、数量、正负商品金额、税率、编码、抬头/税号、地址/电话/银行/账号及备注。一个申请可能多行，必须保留源序。

税局模板基底默认使用 skill 内置 assets/tax-bureau-template-V260401.xlsx，无需每次下载或让用户提供。用户明确提供其他版本时通过 --template 指定并验证结构。需要更新版本时也可从页面取得：
```text
GET /api/invoice/tax-bureau-export/exportTaxBureauInvoiceInfo
?<当前筛选>&agentId=<本次值>&tabCode=APPLY
POST /api/invoice/tax-bureau-export/downloadTemplate
```

第二个是下载模板及规则的空参数 POST。导出接口不改变申请状态。通用模板不能被税局模板替代，因为它保留了源商品、折扣和订单关联。

导出返回带会话的 Blob/XLSX；实测无会话 PowerShell 直接请求返回 302。稳定方式是在登录页 fetch，再将文件字节经 Base64 交本地保存。检查 HTTP 成功、没有跳到登录页、ZIP 文件头、工作簿必需表头及申请覆盖，记录 SHA-256。不要提取 Cookie 以拼接下载命令。

可用的只读主体接口：GET /api/context、GET /api/shops。保留业务主体摘要，不记录认证信息。

## 千牛批量订单

```text
POST https://trade.taobao.com/trade/itemlist/asyncSold.htm
?event_submit_do_query=1&_input_charset=utf8
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

bizOrderId=<最多50个逗号分隔订单号>
auctionId=
buyerNick=
batchType=bizOrderId
isBatchSearch=true
pageNum=1
```

合并当前页面的必要查询默认值；近三个月页 tabCode=latest3Months 是已验证样例，历史订单不能假定仍在此页。响应 query 是规范化回显，不能当作免登录 API。

响应 mainOrders 和 page.totalNumber/totalPage：
- mainOrders[].id → 主订单号。
- subOrders[].idStr → 子订单号。
- subOrders[].quantity → 数量。
- subOrders[].priceInfo.realTotal → 接口金额，未核对口径不能用于匹配。
- subOrders[].itemInfo.title / skuText[] → 标题 / 规格。
- subOrders[].itemInfo.extra 中 name="商家编码" 的 value → 完整编码。

接口已观察到 GBK 内容。按响应字符集解码，缺失时使用已验证 GBK，不能 response.json() 后把乱码当缺字段。每批读完分页，校验返回订单属于输入、没有重复、总数完整；未命中的编号另查历史详情。

历史详情实测入口 https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId=<订单号>。read_order_detail.js 只读 DOM，先核对 URL 参数及页面订单号，再提取同一商品行的编码、标题、数量、规格、单价×数量文本。DOM 结构变化或无法提取时停止该项，不推测字段位置。

多商品金额样例曾出现列表接口值与详情值不同；因此只用已解释的详情/优惠口径建立 match_evidence，不按金额排序配对。

## 票聚商品

```text
POST https://apiweb.erp321.com/webapi/ItemApi/ItemSku/GetPageListV2
Content-Type: application/json
```

直接 fetch HTTP body：
```json
{
  "page":{"currentPage":1,"pageSize":50,"hasPageInfo":false,"pageAction":1},
  "data":{"sku_id":"@@完整编码","queryFlds":["sku_id","properties_value","invoice_name","invoice_spec","issuing_office","tax_code","tax_rate","tax_rate_zero","invoice_enabled","vc_name","enabled"]},
  "ip":"",
  "coid":"当前主体",
  "uid":"当前用户"
}
```

不传 sku_type、enabled。页面运行时还存在 data/query/isMainDB 包装层，不要把包装层直接当作 HTTP body。默认普通商品过滤会漏组合装；默认启用过滤会漏停用但 invoice_enabled=true 的商品。两项不限后再判断不存在。

成功 body.code=0 且 body.act=0，数组 body.data。body.page.count 实测可能 -1，不能据此判断不存在。逐条比较完整 sku_id，要求唯一；若响应显示仍有后续页，不得仅凭第一页唯一结果宣称唯一。

字段映射见 flow-and-field-mapping.md。invoice_qty、商品单价和 invoice_spec 只作来源信息，不覆盖源数量、单价或规格。商品编辑、导入、修改等写接口不在本技能范围。

## 故障处理

登录失效、验证码、权限不足或主体变更时停止对应采集并报告具体问题。网络超时或 429/502/503/504 可有限重试只读请求；不无限重试。保存已成功检查点，失败编号单独补查，新的采集结果不得覆盖同名文件。

查询脚本需先经当前工具允许的页面上下文执行。登录仍在但页面未打开不构成阻断。记录实际运行的日期、URL、核对时间、查询条件和原始业务响应，不保存完整 HAR 或认证信息。
