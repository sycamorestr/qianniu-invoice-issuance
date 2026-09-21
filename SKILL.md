---
name: qianniu-invoice-issuance
description: "按申请日期导出千牛通用模板、批量查询订单商品编码及聚水潭票聚信息，生成并校验税局模板表格和异常清单。"
---

# 千牛税局模板生成

用户说“开某天的票”即完成税局模板 XLSX 和异常清单。不包含实际提交开具发票，也不询问是否实际开具。

## 使用路径

- 执行完整流程：先读 [详细规则](references/detailed-rules.md) 和 [页面接入](references/page-integration.md)，采集前再读 [输入输出与运行](references/input-output-contract.md)。
- 修改字段或核对输出：读 [字段映射](references/flow-and-field-mapping.md)。
- 本地重放历史数据：使用 scripts/run_invoice.py --replay；不得宣称历史快照代表当前页面。

## 执行流程

1. 按已记录 URL 自行打开缺失页面，复用登录态。核对千牛店铺、票聚主体及 iframe，将结果写入本次 capture_context.json。不要固定日期、店铺、租户 ID 或浏览器会话名。
2. 在千牛发票页按申请日期查询完整待处理列表，并导出同日通用模板。通用模板是必要来源，税局模板是输出基底。按申请流水号核对集合；保留原始文件和源行顺序。
3. scripts/collection_files.py orders 提取订单号。每批最多 50 个订单号，取完服务端分页；漏查历史订单补读详情。每个批次单独保存，成功批次不重复采集。
4. 收集每条订单明细的编码、标题、规格、数量。多商品仍有歧义时，补充同口径金额证据；不按金额大小、行序或首个结果猜配。用 collection_files.py merge-orders 汇总并生成去重编码清单。
5. 票聚商品类型、商品状态都不限。用 scripts/query_jst_invoice_goods.yingdao.js 按 @@完整编码查询，data 不含 sku_type、enabled；不得在返回端按商品状态过滤。仍要求 invoice_enabled=true。失败项补查后用 collection_files.py merge-jst 汇总。
6. 运行 scripts/run_invoice.py，明确传入日期、店铺、主体、原始数据目录和新输出目录。默认使用内置 assets/tax-bureau-template-V260401.xlsx，不要求用户另行提供模板；用户指定其他版本时才传 --template。该入口执行组装、匹配、逐票校验、Artifact Tool 写表、原生模板保护和独立复核。依赖路径从当前环境的 load_workspace_dependencies 获取。
7. 交付校验后的 XLSX、exceptions.csv 和数量/金额摘要。整次数据错误停止生成；个别发票异常整票暂缓。零申请正常结束，不制造空业务票或询问实际开具。

## 不可变业务约定

- 申请流水号是发票主键；基本信息每票一行，明细每个正商品源行一行，重复填写流水号。
- 金额、数量与折扣只取通用模板；紧随正金额的负金额写该行折扣。订单金额仅作匹配证据。单价留空。
- 规格只取票聚 properties_value（颜色及规格），不取 invoice_spec。明确零税率输出空白，未知税率阻断。
- 基本信息“是否展示购买方地址电话银行账号”始终留空；地址、电话、开户行和账号本身按原字段填写。
- 四张可见业务表及隐藏字典、格式、校验保留。第三、四表的数据区始终为空。不添加说明或审计 sheet。
- 价外费用暂不映射，含该项整票暂缓。

## 工程边界

assets/tax-bureau-template-V260401.xlsx 来自用户提供的 V260401 原始空白模板，四张业务表无数据，保留隐藏字典、格式和校验。分享时连同 assets 一起复制；生成文件只写到输出目录，不能修改内置模板。模板版本更新时先核对字段、空白数据区及隐藏辅助页，再替换资源并更新默认路径。

浏览器采集由代理在已核对的登录页面执行；本地 CLI 不管理登录，也不自动提交。优先使用当前环境可用的浏览器工具；采用 OpenCLI 时先读取其 usage/browser skill，使用 scripts/run_page_read.ps1 的显式会话参数。

只有本 skill/scripts 中的文件是维护入口；工作区历史试验脚本不是生产入口。规则变更同时更新对应参考文档、构建器及相关测试。运行 test_build_invoice_plan.py、test_pipeline.py、test_read_collectors.mjs 和 skill-creator 的 quick_validate.py。
