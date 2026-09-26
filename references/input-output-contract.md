# 输入输出与运行

## 代码边界

```text
run_online.py：锁、阶段、采集检查点、恢复、最终报告
  → playwright_adapter.py：一个后台事件循环，分发到固定页面
    → playwright_controller.py：连接 Edge、页面角色、执行 JS
      → 页面接口 / 必要订单详情
  → collection_files.py：选择范围、订单及票聚批次合并
  → order_evidence.py：详情合并、同口径金额证据
  → run_invoice.py + build_invoice_plan.py：组装、逐票规则、独立核验
  → render_invoice_template.mjs + template_io.py：写表、保护原模板、校验成品
```

只有 Playwright 在线后端。`--browser-backend playwright` 是可省略的兼容参数，不能在一次任务内切换浏览器路线。旧后端作业在线 `resume` 拒绝执行；已有业务输入可通过 `--replay-input` 离线复核。

## 参数和运行环境

新任务必须提供日期、店铺、票聚主体和浏览器配置。配置也可通过 `QIANNIU_BROWSER_CONFIG` 指定。`--run-dir` 可指定尚不存在的新作业目录，默认目录由店铺标识、日期和微秒时间组成。

```powershell
& $python "$skill/scripts/run_online.py" `
  --date 'YYYY-MM-DD' --store '本次店铺' --issuer '本次公司' `
  --browser-config $config --output-root "$work/outputs" `
  --node $node --node-modules $nodeModules
```

`--resume <作业目录>` 恢复原任务；日期、店铺、主体、浏览器数据目录及 Profile 不可改变。`--replay-input <输入目录>` 不连接浏览器，结果标为离线。`--plan-only` 输出计划、异常和原件副本，不写税局 XLSX。

### 运行环境选择

复用已有可导入 Playwright 和 lxml 的 Python。已验证 Python 3.13、Playwright 1.63.0、lxml 6.1.1，依赖以 `requirements.txt` 为准。不要只因 Codex bundled Python 缺包就重复安装或切换已可用环境。

Node 与 Artifact Tool 的 `node_modules` 取当前 `load_workspace_dependencies` 返回值，显式传入 `--node` 和 `--node-modules`。已验证 Node 24.19.0。Artifact Tool 不随仓库分发，不能假设可通过普通 npm 安装。当前浏览器连接方式不需要下载 Playwright Chromium。

税局模板默认按脚本位置定位 `assets/tax-bureau-template-V260401.xlsx`；底层 `run_invoice.py --template` 可指定已验证的新版本，不能默默替换模板。

## 采集输入

脚本由控制器直接从本地文件读取，在已经核验的业务页面中执行；输入通过结构化 JSON 传入，返回对象或导出的 XLSX 字节，不通过 shell 拼接大段 JS。

| 操作 | 输入要点 | 输出 |
| --- | --- | --- |
| 千牛申请 | `operation: applications`、日期、当前 `agentId` | 全分页申请诊断快照 |
| 千牛导出 | `operation: export`、日期、当前 `agentId` | 原始 XLSX 字节 |
| 千牛订单 | `operation: orders`、1–50 个字符串订单号、可选查询条件 | 完整分页、明细、明确缺失集合 |
| 订单详情 | 当前字符串订单号 | 已核对订单号的 DOM/运行时明细 |
| 票聚商品 | 1–40 个编码、当前 `coid` / `uid` | 保持输入顺序的逐编码查询结果 |

订单号和子订单号必须是字符串，不能先转数值再转回。返回订单须属于本批输入，不能重复；返回集合与缺失集合共同覆盖请求范围。票聚结果必须覆盖请求编码集合；未命中/多匹配属于业务结果，`request_failed` 才进入失败补查。

## 原始检查点

| 文件 | 契约 |
| --- | --- |
| `capture_context.json` | 日期、店铺、公司、主体核验时间、业务 URL、当前 agentId/coid/uid、Profile、`context_sha256` |
| `qianniu_common.xlsx` | 页面导出的完整原件；工作表“开票申请列表”；唯一范围及金额来源 |
| `applications.json` | 全部观察行、`api_total`、`observed_total` 及非待处理诊断子集；`applyStatus` 不参与选择 |
| `selection.json` | 原件哈希、状态分布、源行和原序去重的待处理流水号；源范围不可变 |
| `order_ids.json` | 选择哈希、活跃申请、负数排除申请、需要查询的订单号 |
| `order_batches.json` | 合并后的批次、订单明细、缺失订单，绑定原件及选择哈希 |
| `old_details.json` / `supplemental_details.json` | 已核对订单号的详情；两者合并消费，可靠子订单键发生字段冲突则停止 |
| `goods_codes.json` | 合并订单后去重的编码，继承原件和选择哈希 |
| `jst_query.json` | 原始编码、查询编码、匹配依据、候选及失败结果，覆盖全部请求编码 |
| `parts_manifest.json` | 业务批次路径、SHA-256、阶段和登记时间 |

通用模板状态优先“开票状态”，为空才回退“申请状态”，仅精确为“待处理”的源行进入选择。缺两列时停止。负总额发票仍留在 `selected`，但不进入订单/票聚查询，因此 `selected = ready + blocked + excluded`。

`issuer_company` 保存公司名，`issuer_label` 保存观察到的完整显示标签；允许用户输入其中已核验的一种，输出主体使用公司名。`context_sha256` 绑定业务身份。当前浏览器页面标识保存在 `run-state.json` 的 `browser_pages`，不参与业务身份哈希；重新连接页面不会导致重查已成功批次。

自动金额证据保存在生成目录的 `derived_match_evidence.json`。来源必须说明订单、子订单、详情 URL/时间、原始价格单元格、数量及相同金额口径；列表 `realTotal` 不能自动升级为证据。可选 `match_evidence.json` 与推导证据冲突时停止。详细匹配约束见[详细规则](detailed-rules.md)。

## 原子发布和恢复

先获取输出目录作业锁，再连接浏览器并获取整个 `user_data_dir` 的文件锁。锁由操作系统持有句柄，进程退出自动释放；锁文件存在不等于被占用，不能删除文件解锁。

成功业务检查点不可覆盖。采集响应完成后，`publications/<批次>.json` 保存响应字节、请求及输出哈希和回执，数据与 `receipts/<批次>.json` 随后提交。若本地发布失败，恢复仅凭完整且身份匹配的事务补发布，不再次发出已保存成功响应的业务请求。没有完整事务证据的残片返回 `checkpoint_invalid`，不能把 `.partial` 当成功文件。

回执绑定 site、operation、输入路径/哈希、请求哈希、输出路径/哈希。业务合并只识别完整批次文件名并校验集合及 manifest；回执、失败现场、临时文件都不是业务数据。原件、输入或回执哈希变化返回 `resume_mismatch`。

可变 `run-state.json`、`run.json` 使用独立临时文件后原子替换。Windows 替换错误 5/32/33 按 50、100、200、400 毫秒有限退避，耗尽返回 `checkpoint_write_failed`，保留旧正式文件与临时文件。文件错误只重试本地发布。

所有在线恢复都重新查询千牛和票聚身份，包括首次 context 只成功一站就中断的情况。新核验记录单独保存，原始成功身份不覆盖；店铺、公司或已有租户字段变化阻断后续申请和导出。恢复仅继续未完成阶段。

详情补证只有在“详情保存 → 订单重合并 → 新编码票聚补查”均完成后，才提交 `dependencies_committed`。恢复旧检查点时补齐缺失依赖，不重复读取成功详情或已成功编码。

## 本地计划与工作簿

`run_invoice.py` 先复制原始通用模板并核对哈希，然后组装 `invoice_input.json`，生成 `invoice_plan.json`、`derived_match_evidence.json` 和异常清单。逐票计划保留源行、订单项证据、折扣源行、原商品金额、价外费用金额及费用源行。

负数整票标为 `excluded_negative`，无输出明细；其他票按资料完整性判定可生成或暂缓。单独计划构建器的 `ready_for_export` 仅表示可生成文件，计划指纹不是提交去重或已开具登记。

`verify_sources()` 独立复核源字段、关联和金额。Artifact Tool 按表头批量写入作者副本，再由 `template_io.py` 以原始模板为基底替换数据区；`verify_workbook()` 逐单元格核验数据及模板其他部件。通过后才发布正式文件。原模板要求全部数据文本输入，明确零税率为文本 `0`。

## 终态、交付与测试

| 状态 | 含义 |
| --- | --- |
| `complete` | 有可生成申请且最终 XLSX 校验通过 |
| `no_applications` | 没有待处理申请 |
| `all_excluded` | 所选申请全部负数排除 |
| `all_blocked` | 无可生成申请，存在资料问题暂缓 |
| `plan_only` | 已完成计划检查，未生成税局 XLSX |
| `failed` | 身份、输入、连接、检查点或生成校验失败 |

根 `run.json` 发布最新终态；`attempts/` 保留历史失败；生成目录的 `run.json` 记录文件级结果。`progress.log` 追加进度，阶段 attempts 保存开始/结束时间、类型、状态和耗时；本地重合并不等于接口失败重试。

成功交付原件副本、税局模板、异常清单和报告。无可生成申请时不发布可导入税局模板；若采集失败，保留已取得的原件和检查点。数据/结构校验不等于视觉或税局上传通过。

在仓库根目录运行离线检查：

```powershell
python scripts/test_build_invoice_plan.py
python scripts/test_pipeline.py
python -m unittest discover -s scripts -p 'test_*.py'
node scripts/test_read_collectors.mjs
```

Windows CI 执行上述离线检查，不访问业务网站。私下保存的真实快照可另行验证编排和恢复：

```powershell
& $python "$skill/scripts/test_online_snapshot.py" `
  --snapshot "$work/private-snapshot" --output-root "$work/snapshot-check" `
  --expected-ready 2 --expected-amount '100.00' --expected-blocked 1
```

期望值为合成示例，必须替换为所选快照的独立核对结果。该验收夹具目前要求一个订单批次；输出明确标记 `simulation`，验证源文件不变及恢复仅重核身份，不能代表实时接口已经复测。业务快照、登录资料和运行输出不随仓库分享。
