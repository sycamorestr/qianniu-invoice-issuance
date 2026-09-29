# 输入输出与运行

## 代码边界

```text
shop_registry.py：读取原清单及工作台新增登记，校验隔离与环境身份
  → run_batch.py（多店可选）：冻结所选店铺、串行调度、文件校验、批次汇总
  → run_online.py：单店锁、阶段、采集检查点、恢复、最终报告
  → playwright_adapter.py：一个后台事件循环，分发到固定页面
    → playwright_controller.py：按原配置启动或连接 Edge、页面角色、执行 JS
      → 页面接口 / 必要订单详情
  → collection_files.py：选择范围、订单及票聚批次合并
  → order_evidence.py：详情合并、同口径金额证据
  → run_invoice.py + build_invoice_plan.py：组装、逐票规则、独立核验
  → render_invoice_template.mjs + template_io.py：写表、保护原模板、校验成品
```

只有 Playwright 在线后端。`--browser-backend playwright` 是可省略的兼容参数，不能在一次任务内切换浏览器路线。旧后端作业在线 `resume` 拒绝执行；已有业务输入可通过 `--replay-input` 离线复核。

## 参数和运行环境

新任务必须提供查询范围、店铺、票聚主体和浏览器配置。范围显式二选一：`--date YYYY-MM-DD` 或 `--all-pending`，不能同时提供，也不能都省略后默认为全量。配置也可通过 `QIANNIU_BROWSER_CONFIG` 指定。`--run-dir` 可指定尚不存在的新作业目录，默认目录由店铺标识、范围标签和微秒时间组成。

`--all-pending` 按约定查询最近两个日历月：以创建时的北京时间今日为结束日期，向前两个日历月为开始日期，包含首尾两天。例如 2026-09-28 创建的任务固定为 2026-07-28 至 2026-09-28。新任务默认只处理开票倒计时已开始的申请，保存 `date=null` 及 `query_scope={"mode":"all_pending","start_date":"2026-07-28","end_date":"2026-09-28","countdown":"started"}`；文件及目录范围标签使用 `all-pending`。新单日任务保存 `date` 及 `query_scope={"mode":"date","date":"YYYY-MM-DD","countdown":"started"}`。倒计时筛选在创建时冻结，旧任务缺字段则恢复原无筛选范围，不静默改变旧检查点或输出。

单店可传 `--expected-account <已确认的完整千牛子账号>`；多店自动使用该店记录的 `login_username` 作为同一约束。提供时须与页面 `context.realNick` 精确匹配，允许 `store` 为显示别名；未提供时仍用 `store` 做原来的精确核验。账号是核验依据，不是密码或自动登录输入，也不能从显示名、普通备注猜出账号。

`--jst-browser-config <配置>` 可指定独立共享票聚浏览器，此时千牛配置可为工作台 `home` 或原 `invoice/orders` 配置，运行器在内存中补齐业务角色而不改写原配置；票聚配置只含 `goods`。省略时保持原有单浏览器三角色模式。

多店入口 `run_batch.py --registry <shops.json> --date YYYY-MM-DD` 或 `run_batch.py --registry <shops.json> --all-pending` 合并原清单和旁侧 `.browser-workbench-shops.json`，在同一 issuer 下串行调用单店入口。新批次默认选择合并清单中的所有当前店铺；`--shops` 限定店铺 id，`--resume` 恢复原批次冻结的店铺与日期范围。最近两个月的开始、结束日期在批次创建时只计算一次，所有店铺沿用，不逐店移动窗口。完整契约见[多店配置与运行](multi-shop.md)。

两个在线入口默认按需启动：已有环境复用，确认未运行时按现有固定数据目录、Profile 和端口启动正常 Edge 一次，读取浏览器保存的会话。`--connect-only` 禁止启动进程，但仍执行采集和一次性缺页恢复。该选项不是诊断模式；`status` 等诊断命令保持只读。配置冲突、进程归属不符或采集中途断连不触发换端口或循环重启。

```powershell
& $python "$skill/scripts/run_online.py" `
  --date 'YYYY-MM-DD' --store '本次店铺' --issuer '本次公司' `
  --browser-config $config --output-root "$work/outputs" `
  --node $node --node-modules $nodeModules
```

`--resume <作业目录>` 恢复原任务，可从快照读取范围；日期模式、开始/结束日期、店铺、账号约束、主体、浏览器数据目录及 Profile 不可改变。最近两个月模式跨日恢复仍沿用原 `query_scope`，不按恢复当天重算。双浏览器任务还绑定共享票聚配置路径和环境身份；不能在恢复中改为另一浏览器或临时增加共享配置。`--replay-input <输入目录>` 不连接浏览器，结果标为离线。`--plan-only` 输出计划、异常和原件副本，不写税局 XLSX。

兼容旧失败作业时，仅在原账号约束为空、千牛 context 尚未保存且不存在其检查点、回执或发布事务证据时，代码允许首次绑定明确账号；已有身份证据后禁止增加或更改账号约束。不得手改检查点触发这一兼容分支。

### 运行环境选择

复用已有可导入 Playwright 和 lxml 的 Python。已验证 Python 3.13、Playwright 1.63.0、lxml 6.1.1，依赖以 `requirements.txt` 为准。不要只因 Codex bundled Python 缺包就重复安装或切换已可用环境。

Node 与 Artifact Tool 的 `node_modules` 取当前 `load_workspace_dependencies` 返回值，显式传入 `--node` 和 `--node-modules`。已验证 Node 24.19.0。Artifact Tool 不随仓库分发，不能假设可通过普通 npm 安装。当前浏览器连接方式不需要下载 Playwright Chromium。

税局模板默认按脚本位置定位 `assets/tax-bureau-template-V260401.xlsx`；底层 `run_invoice.py --template` 可指定已验证的新版本，不能默默替换模板。

## 采集输入

脚本由控制器直接从本地文件读取，在已经核验的业务页面中执行；输入通过结构化 JSON 传入，返回对象或导出的 XLSX 字节，不通过 shell 拼接大段 JS。

| 操作 | 输入要点 | 输出 |
| --- | --- | --- |
| 千牛申请 | `operation: applications`、单日或冻结的 `query_scope`、当前 `agentId` | 本次日期范围的全分页申请诊断快照 |
| 千牛导出 | `operation: export`、同一日期范围、当前 `agentId` | 原始响应字节；非空须为 XLSX，零字节须与明确空列表联合核验 |
| 千牛订单 | `operation: orders`、1–50 个字符串订单号、可选查询条件 | 完整分页、明细、明确缺失集合 |
| 订单详情 | 当前字符串订单号 | 已核对订单号的 DOM/运行时明细 |
| 票聚商品 | 1–40 个编码、当前 `coid` / `uid` | 保持输入顺序的逐编码查询结果 |

订单号和子订单号必须是字符串，不能先转数值再转回。返回订单须属于本批输入，不能重复；返回集合与缺失集合共同覆盖请求范围。票聚结果必须覆盖请求编码集合；未命中/多匹配属于业务结果，`request_failed` 才进入失败补查。

申请与导出接口都显式传入 `startTime/endTime`：单日为同一天，`all_pending` 为冻结的 `start_date/end_date`。不能省略日期、传空值或扩大超过已确认的两个月范围。导出保留该范围完整原件，再按原件状态选择，不把申请列表状态替代业务范围。

`countdown="started"` 时两接口还传 `rightsRemainTime=100`。导出端实际不执行此倒计时筛选，选择阶段必须以完整筛后列表的流水号集合与通用模板“待处理”集合取交集。列表状态不作筛选依据，也不能凭缺失/不完整列表认定无申请。原件含未入选记录仍原样交付，筛选证据与原件一并绑定选择哈希。

## 原始检查点

| 文件 | 契约 |
| --- | --- |
| `capture_context.json` | 日期或冻结的 `query_scope`、店铺显示名、原始 `observed_store/account_nick`、账号约束、公司、主体核验时间、业务 URL、当前 agentId/coid/uid、Profile、`context_sha256` |
| `common-export.bin` | 新导出的不可变原始下载检查点，保留响应原字节；可以是已核验的零字节响应 |
| `qianniu_common.xlsx` | 非空下载通过 ZIP 校验后的字节一致原件；工作表“开票申请列表”；状态、数量及金额来源，倒计时资格另据筛后申请列表；旧作业可直接以此为下载检查点 |
| `applications.json` | 本次筛选下全部观察行、`api_total`、`observed_total` 及非待处理诊断子集；`applyStatus` 不参与选择，started模式流水号作为倒计时资格证据 |
| `selection.json` | 原件哈希、状态分布、源行及入选流水号；started模式额外绑定范围、筛后列表哈希及倒计时未入选记录；源范围不可变 |
| `order_ids.json` | 选择哈希、活跃申请、负数排除申请、需要查询的订单号 |
| `order_batches.json` | 合并后的批次、订单明细、缺失订单，绑定原件及选择哈希 |
| `old_details.json` / `supplemental_details.json` | 已核对订单号的详情；两者合并消费，可靠子订单键发生字段冲突则停止 |
| `goods_codes.json` | 合并订单后去重的编码，继承原件和选择哈希 |
| `jst_query.json` | 原始编码、查询编码、匹配依据、候选及失败结果，覆盖全部请求编码 |
| `parts_manifest.json` | 业务批次路径、SHA-256、阶段和登记时间 |

通用模板状态优先“开票状态”，为空才回退“申请状态”，仅精确为“待处理”的源行可进入选择；started模式还须出现在完整筛后申请清单中。缺状态列或筛选证据不完整时停止。选中的负总额发票仍留在 `selected`，但不进入订单/票聚查询，因此 `selected = ready + blocked + excluded`。未开始倒计时的申请属于未入选范围，不混入业务异常或负票排除计数。

申请列表为零也必须尝试一次通用模板导出。只有同一本次日期范围的 `applications.json` 已验证 `rows=[]`，`total/api_total/observed_total` 均明确为数值零，且导出 HTTP 成功、无登录跳转、响应确为零字节，才允许没有 XLSX 的 `no_applications` 终态。保留空 `common-export.bin`、列表快照、导出请求及哈希回执；不把缺失计数、错误页或非空非 ZIP 响应当作无申请。列表为零但导出非空时保留原件；started模式交集为空，不因导出含其他申请而扩大范围。

该空响应分支跳过订单、票聚商品、详情计划及 `run_invoice.py`，生成目录只写 `run.json` 和表头 `exceptions.csv`。报告数量和金额均为零，`output/common_template_output=null`，`empty_export={path, sha256, reason}` 指向原始空响应，原因是 `applications_and_export_empty`；没有可供人工核对的源工作簿，不制造替代表格。已下载 XLSX 但无待处理源行的 `no_applications` 仍交付原件副本。

`issuer_company` 保存公司名，`issuer_label` 保存观察到的完整显示标签；允许用户输入其中已核验的一种，输出主体使用公司名。`context_sha256` 绑定业务身份。当前浏览器页面标识保存在 `run-state.json` 的 `browser_pages`，不参与业务身份哈希；重新连接页面不会导致重查已成功批次。

千牛 `observed_store` 保存接口原始店铺昵称，`account_nick` 保存本次页面观察到的完整账号，不以工作台显示名覆盖。提供账号约束仅改变首次核验所用依据，不取消正向认证、agentId 或后续恢复的一致性检查；恢复必须固定原观察值，账号或原始身份变化不能只因显示名称相同而放行。

自动金额证据保存在生成目录的 `derived_match_evidence.json`。来源必须说明订单、子订单、详情 URL/时间、原始价格单元格、数量及相同金额口径；列表 `realTotal` 不能自动升级为证据。可选 `match_evidence.json` 与推导证据冲突时停止。详细匹配约束见[详细规则](detailed-rules.md)。

## 原子发布和恢复

先获取输出目录作业锁，再连接浏览器并获取整个 `user_data_dir` 的文件锁。双浏览器按规范化数据目录排序连接，共享票聚锁一直持有到本店任务结束。锁由操作系统持有句柄，进程退出自动释放；锁文件存在不等于被占用，不能删除文件解锁。

成功业务检查点不可覆盖。采集响应完成后，`publications/<批次>.json` 保存响应字节、请求及输出哈希和回执，数据与 `receipts/<批次>.json` 随后提交。若本地发布失败，恢复仅凭完整且身份匹配的事务补发布，不再次发出已保存成功响应的业务请求。没有完整事务证据的残片返回 `checkpoint_invalid`，不能把 `.partial` 当成功文件。

新导出落盘 `common-export.bin`，非空通过 ZIP 校验后原字节复制为 `qianniu_common.xlsx`；两者及回执均绑定哈希。`resume` 复用已完成下载，包括零字节响应，不重复导出；旧 XLSX 下载检查点沿用原路径和回执。空响应终态也须校验列表、空文件及报告哈希后恢复或跳过。

回执绑定 site、operation、输入路径/哈希、请求哈希、输出路径/哈希。业务合并只识别完整批次文件名并校验集合及 manifest；回执、失败现场、临时文件都不是业务数据。原件、输入或回执哈希变化返回 `resume_mismatch`。

可变 `run-state.json`、`run.json` 使用独立临时文件后原子替换。Windows 替换错误 5/32/33 按 50、100、200、400 毫秒有限退避，耗尽返回 `checkpoint_write_failed`，保留旧正式文件与临时文件。文件错误只重试本地发布。

进入单店在线恢复时重新查询千牛和票聚身份，包括首次 context 只成功一站就中断的情况；批次中已完成店铺经文件校验后直接跳过，不重新进入其单店流程。新核验记录单独保存，原始成功身份不覆盖；店铺、公司或已有租户字段变化阻断后续申请和导出。恢复仅继续未完成阶段，登录过期或验证码等待人工处理，不自动输入账密。

详情补证只有在“详情保存 → 订单重合并 → 新编码票聚补查”均完成后，才提交 `dependencies_committed`。恢复旧检查点时补齐缺失依赖，不重复读取成功详情或已成功编码。

## 本地计划与工作簿

`run_invoice.py` 先复制原始通用模板并核对哈希，然后组装 `invoice_input.json`，生成 `invoice_plan.json`、`derived_match_evidence.json` 和异常清单。逐票计划保留源行、订单项证据、折扣源行、原商品金额、价外费用金额及费用源行。

负数整票标为 `excluded_negative`，无输出明细；其他票按资料完整性判定可生成或暂缓。单独计划构建器的 `ready_for_export` 仅表示可生成文件，计划指纹不是提交去重或已开具登记。

本技能不提交实际开票，也没有跨任务已开票登记。新建最近两个月批次会重新导出当时仍待处理的申请，可能与旧交付重叠；不能将不同批次文件解释为已自动去重。恢复原批次只继续其固定范围与原件快照，不把新增申请混入成功检查点。

`verify_sources()` 独立复核源字段、关联和金额。Artifact Tool 按表头批量写入作者副本，再由 `template_io.py` 以原始模板为基底替换数据区；`verify_workbook()` 逐单元格核验数据及模板其他部件。通过后才发布正式文件。原模板要求全部数据文本输入，明确零税率为文本 `0`。

## 终态、交付与测试

正式 CLI 的业务终态保存并释放连接/锁后，调用独立 `invoice_delivery.py` 生成 ZIP，并默认读取技能根目录的私有 `notifications.json` 推送企微；显式配置优先，旧工作目录仅在技能内未配置时兼容。打包与推送回执只写 `delivery/`，不改以下业务终态或成功文件哈希。多店在批次层调用一次；单店 CLI 在单店层调用，`OnlineRunner` 子流程不自行发消息。`--no-notify` 和离线重放只打包，`--plan-only` 不触发。发送失败单独补发，无需恢复业务采集。详见[打包与企微推送](notifications.md)。

| 状态 | 含义 |
| --- | --- |
| `complete` | 有可生成申请且最终 XLSX 校验通过 |
| `no_applications` | 没有待处理申请 |
| `all_excluded` | 所选申请全部负数排除 |
| `all_blocked` | 无可生成申请，存在资料问题暂缓 |
| `plan_only` | 已完成计划检查，未生成税局 XLSX |
| `failed` | 身份、输入、连接、检查点或生成校验失败 |

根 `run.json` 发布最新终态；`attempts/` 保留历史失败；生成目录的 `run.json` 记录文件级结果。`progress.log` 追加进度，阶段 attempts 保存开始/结束时间、类型、状态和耗时；本地重合并不等于接口失败重试。

成功交付已取得的原件副本、税局模板、异常清单和报告。无可生成申请时不发布可导入税局模板；上述有完整证据的零字节导出终态改为交付查询快照、空响应证据、异常表头和报告，文件路径为 `null` 不表示失败。若采集失败，保留已取得的原件和检查点。数据/结构校验不等于视觉或税局上传通过。

多店增加 `batch-state.json`、`batch-summary.json`、`batch-summary.csv`，每店产物保持独立。新批次冻结日期模式及开始/结束日期、选中店铺及顺序、所选账号约束、所选店铺和共享票聚的环境身份；后来新增未选中的有效店铺既不加入旧批次，也不使其恢复失配。旧 v1 批次沿用原来的全清单身份检查，不手改检查点升级。

批次恢复在任何新浏览器请求前校验全部已完成店铺的输入/输出哈希，通过后跳过，不重新采集这些店铺；未完成店铺沿用各自的单店恢复。共享票聚、未知或未明确归类的错误停止批次，只有标为 qianniu 的明确单店环境/认证错误可记录后继续。批次状态与详情见[多店配置与运行](multi-shop.md#状态恢复与交付)。

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
