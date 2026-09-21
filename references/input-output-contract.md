# 输入输出与运行

## 架构

浏览器采集 → 原始快照 → 本地组装/匹配 → InvoicePlan → Artifact Tool 写表 → 原模板保护 → 独立复核 → XLSX + 异常清单。

scripts 内是正式维护入口：
- read_qianniu.js：单日申请列表、通用模板导出、最多 50 订单的完整分页查询。
- read_order_detail.js：核对订单号后的历史详情只读采集。
- query_jst_invoice_goods.yingdao.js：票聚精确查询，不限类型/状态。
- run_page_read.ps1：OpenCLI 页面运行桥接，UTF-8 Base64 传输，保存 JSON/XLSX，拒绝覆盖检查点。
- collection_files.py：准备订单清单、合并订单/票聚批次与失败重试结果。
- build_invoice_plan.py：纯本地业务规则、逐票计划。
- run_invoice.py：本地全流程入口，包含独立来源复核。
- render_invoice_template.mjs：Artifact Tool 按表头写值。
- template_io.py：表头定位、轻量作者副本、原生模板保护与输出行结构。
- test_build_invoice_plan.py、test_pipeline.py、test_read_collectors.mjs：业务、管线与模拟接口响应回归。

## 浏览器输入与采集文件

read_qianniu.js 的 __INPUT__ 由结构化 JSON 替换：
- 申请：{operation:"applications", date:"YYYY-MM-DD", agentId:"本次页面值"}。
- 导出：{operation:"export", date:"YYYY-MM-DD", agentId:"本次页面值"}。
- 订单：{operation:"orders", orders:["编号"], query:{本次页面默认查询状态}}。

票聚输入为 {codes:["完整编码"], context:{coid:"本次值",uid:"本次值"}}。每批可取 10～20 个编码控制执行时间；串行请求，不为提速无限并发。单次请求 30 秒超时，失败与未命中分开记录。不要保存 Cookie、令牌或完整 HAR。

本次输入目录：
- capture_context.json：date、store、issuer、verified_at、invoice_url、orders_url、jst_url；由本次页面核对填写，不从历史样例推定。
- qianniu_common.xlsx：原始通用模板，工作表“开票申请列表”。
- applications.json：date、queried_at、total、rows；每行 serialNo、tid、applyStatus、amount、applyTime。
- order_ids.json：collection_files.py orders 生成。
- order_batches.json：合并批次后的 batches、items、missing。items 包含 order_no、sub_order_no、goods_code、title、quantity、specification。
- old_details.json：可选历史详情数组，每项 verified_order=true、order_no、url、items。无历史补查可省略。
- match_evidence.json：可选金额证据数组，含 order_no、goods_code、可选 sub_order_no、source_amount、match_amount_source、source，另保留页面/字段/计算关系证据。
- goods_codes.json：订单汇总后去重编码。
- jst_query.json：data 每项 input_goods_code、ok、reason、rows、exact_matches；必须覆盖查询编码集合。

金额证据 match_amount_source 仅支持 order_detail_gross / promotion_detail_gross。source_amount 为与正商品金额相同口径的已核对值。旧 9.19 快照的 source="订单详情同一商品行的单价乘数量" 可明确迁移为前者；其他缺少口径的旧值拒绝自动转换。

## 标准调用

默认模板是 skill 内的 assets/tax-bureau-template-V260401.xlsx，通过脚本自身位置定位，不依赖用户桌面或工作目录。其他路径都是调用参数。Python、Node 和 node_modules 使用当前 load_workspace_dependencies 返回的 bundled 路径。

```powershell
& $python "$skill/scripts/collection_files.py" orders --run-dir $inputDir
# 在已核对页面完成每批查询，将成功批次保存在不同文件中。
& $python "$skill/scripts/collection_files.py" merge-orders --run-dir $inputDir --part $batch1 --part $batch2
# 历史订单补查写入 old_details.json 后，再运行 merge-orders 更新编码清单。
& $python "$skill/scripts/collection_files.py" merge-jst --run-dir $inputDir --part $goods1 --part $goods2
& $python "$skill/scripts/run_invoice.py" --date $date --store $store --issuer $issuer --input-dir $inputDir --output-dir $newOutputDir --node $node --node-modules $nodeModules
```

OpenCLI 桥接需显式 --Session（PowerShell 参数用 -Session），不固定浏览器标签或 frame。先按已登录页面确认绑定，再调用：
```powershell
& "$skill/scripts/run_page_read.ps1" -SourcePath "$skill/scripts/read_qianniu.js" -Session $session -InputPath $queryJson -OutputPath $checkpoint
# 导出加 -Download；票聚使用查询函数文件并加 -Jst；需要 iframe 时传 -Frame。
```

本地参数：
- --template：可选，仅在使用其他模板版本时传入；不传则使用内置 V260401 模板。run.json 记录实际模板路径和 SHA-256。
- --plan-only：组装、计划、来源复核和异常清单，不写 XLSX。
- --replay：离线重放历史快照，可没有 capture_context.json；run.json 明确记录 replay=true。不是当前线上状态验证。
- --output-dir 必须不存在，避免覆盖。失败目录保留，重跑选择新目录。

作者脚本使用 Artifact Tool；在 Codex 中生成工作簿前依 spreadsheets skill 执行一次 operation marker。模板保护脚本只把作者结果的值转回原模板业务表，保留原有字典和其他 ZIP 部件。Windows 图像渲染曾异常退出；结构/值校验与可视检查分别记录，不把未渲染说成视觉通过。

分享时打包整个 skill 目录（包括 assets），排除 __pycache__；无需另发税局模板，也无需复制历史采集目录。内置模板是干净原件，没有历史业务行或未引用共享字符串。通用模板仍需按本次日期从千牛导出。

## 计划契约与兼容边界

build_invoice_plan.py input.json --output plan.json：
- mode 只允许 preview。ready_for_export 表示可生成表格，不表示可提交开票。
- selected_application_ids 明确申请集合；空集合合法。
- template_rows 保留 __source_row 或 source_row、源行顺序及原始字段。
- order_goods 是订单→编码集合；order_items 是订单明细证据，规范字段 order_no、sub_order_no、goods_code、source_goods_name、source_specification、quantity、match_amount、match_amount_source。
- jst_invoice_goods 保留原始 sku_id、invoice_name、tax_code、properties_value、issuing_office、tax_rate、vc_name、invoice_enabled。
- 单独构建器兼容中文别名和旧 order_goods-only 输入；完整生成入口要求每条通过明细有订单项证据，不能靠旧输入绕过匹配核验。
- detail_lines 的 order_item_evidence、sub_order_no、source_row、discount_source_row 记录关联来源。税率输出和 tax_rate_effective 分开保存。
- fatal / errors 是全局错误；invoices[].errors 是逐票异常。退出码 0 正常，1 有业务/全局错误（必须读 fatal 区分），2 输入格式错误。
- plan_hash 是数据指纹，idempotency_key 仅是兼容的计划指纹字段，不表示已实现提交去重或已开具登记。

## 交付与测试

最终产物为 qianniu_invoice_tax_template_日期.xlsx、exceptions.csv、run.json。中间计划、作者副本、payload 是本地检查材料，不放到工作簿新 sheet 中。

测试运行：
```powershell
& $python "$skill/scripts/test_build_invoice_plan.py"
& $python "$skill/scripts/test_pipeline.py"
& $node "$skill/scripts/test_read_collectors.mjs"
```

主要覆盖：多商品及折扣、金额核对、编码/标题/数量冲突、子订单重复、规格来源、零税率、未知状态、非有限数、申请缺漏、空集合、模板重排/脏数据区及批次合并。线上接口版本变化需重新验证页面证据；离线测试不代表真实登录接口已经复测。
