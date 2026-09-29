# 多店配置与运行

本模式面向多个千牛店铺共用一个票聚开票主体，例如 9 个独立千牛浏览器 + 1 个共享票聚浏览器。各店按顺序执行同一套采集与生成流程，所有申请、检查点和交付文件按店隔离。

| 环境 | 用户数据目录 | 页面角色 | 身份要求 |
| --- | --- | --- | --- |
| shop01 至 shop09 | 每店独立 `user_data_dir` | 采集使用 `invoice`、`orders`；工作台 `home` 保留 | 有 `login_username` 时精确核验完整子账号，否则精确核验 `store` |
| piaoju | 独立共享 `user_data_dir` | `goods` 及商品 iframe | 每店均核验同一 issuer |

这里的独立 Profile 指独立用户数据根目录，每个通常使用 `Default` 子目录。浏览器会话保存在本机：已开的浏览器直接附着，关闭的浏览器在任务需要时按原配置正常启动一次，读取保存的 Cookie、Local Storage 等。通常可继续使用原登录态，但不承诺免登录；登录过期或验证码仍需人工处理，技能不自动输入账密。

## 已有浏览器工作台：直接接入

使用工作台已配置的私有 `shops.json`，不重新初始化店铺数据目录。技能将同目录的 `.browser-workbench-shops.json` 与原清单合并，因此工作台「新建店铺」登记的环境也能被识别；原清单与新增登记文件均保持不变。相对配置路径按各登记文件所在目录解析。

首次接入前确认原清单提供本次共同开票主体 `issuer`。工作台显示名和账号备注不代替技能对店铺及票聚主体的正式核验；工作台公开示例若尚未填写 `issuer`，应先在私有清单中补齐用户确认的开票公司。不能依据显示名或账号备注猜测平台身份，也不能将工作台显示的“已登录”视为已通过本次业务核验。

每店记录有 `login_username` 时，批次将其作为已确认的完整千牛子账号，传给 `OnlineRunner` 与本次页面 `context.realNick` 精确比较；匹配后允许 `store` 保留工作台友好显示名，无需把它改成接口昵称。没有 `login_username` 时，保持原来的 `store` 精确核验。这个字段是账号而非密码，也不能填普通备注后靠 AI 猜对应关系；若原工作台把它用作备注，应先由用户确认真实完整账号。原始接口店铺昵称和账号分别保存在 `observed_store/account_nick`，不会被显示别名覆盖。

工作台店铺配置可以只有 `home`。技能在内存中补入发票、订单 URL，随后复用或补齐业务页，保留主页，不改写工作台配置。共享票聚仍使用清单中的 `jst_browser_config`，只需要一个 `goods` 环境。工作台是可选的本机管理界面，技能不依赖它的 HTTP 服务；两者共用已登记的数据目录、固定端口和目录锁。

日常显式选择 `--date` 单日或 `--all-pending` 最近两个日历月范围，运行下文批次命令。既可让全部店铺常驻，也可让技能按需启动；无需在工作台逐店先打开订单或发票页。新批次默认包括合并清单的所有店铺；只想处理其中几店时显式传入 `--shops`。

新批次默认仅处理“开票倒计时＝已开始”的待处理申请，冻结 `query_scope.countdown="started"` 并传给每个店铺。筛后完整申请清单与原件待处理流水号取交集，原始通用模板仍原样交付。旧批次恢复保持原筛选，包括此前尚未启动的店铺，不能逐店套用新默认造成同批次口径混用。

## 初始化私有环境

尚无浏览器环境时才使用本节。依赖与 `$skill`、`$python`、`$node`、`$nodeModules` 变量按 [README](../README.md) 准备。下面的 `$work` 由使用者选择，放在仓库之外；已有工作台的用户沿用自己的清单路径。

复制 `assets/shops.example.json` 到私有工作目录，再编辑副本。样例只有两条合成店铺；9 店使用时增加至 9 条，分别填写唯一的 `id` 和店铺名称，并按上文选择账号或店铺名核验方式。店铺 id 以小写字母开头，只使用字母、数字、下划线和连字符，不能使用 `shared` 或 `piaoju`。不要保存密码。

```powershell
New-Item -ItemType Directory -Path $work -Force | Out-Null
if (-not (Test-Path "$work/shops-input.json")) {
  Copy-Item "$skill/assets/shops.example.json" "$work/shops-input.json"
}
# 编辑 shops-input.json，填好本次店铺后运行：
& $python "$skill/scripts/manage_browsers.py" init `
  --root "$work/environments" --shops-file "$work/shops-input.json" `
  --issuer '本次票聚开票公司'
```

`init` 创建浏览器环境和配置，不启动浏览器、不执行登录或采集。若已有完整且一致的清单则直接复用；不一致或只有部分旧环境时停止，避免覆盖现有浏览器数据。

生成目录：

```text
environments/
  shops.json                 私有店铺清单与共同 issuer
  config/piaoju.json         共享票聚配置
  config/shop01.json ...     各店千牛配置
  profiles/piaoju/           共享票聚登录数据
  profiles/shop01/ ...       各店千牛登录数据
  downloads/                各环境独立下载目录
  outputs/                  批次与单店结果
```

清单及配置中的相对路径相对于各自文件位置解析。注册器验证角色集合、店铺名称/id 唯一、浏览器数据根目录互不相同且不嵌套、下载目录与固定非零端口互不冲突。所有店铺必须使用同一个 issuer；另一开票主体应另建批次环境。

## 启动与人工登录

先启动共享票聚和一个店铺，便于核验机器资源和登录流程：

```powershell
& $python "$skill/scripts/manage_browsers.py" start `
  --registry "$work/environments/shops.json" --targets piaoju shop01
```

确认后可按需增加其他店铺，或明确启动清单中的全部环境：

```powershell
& $python "$skill/scripts/manage_browsers.py" start `
  --registry "$work/environments/shops.json" --all
```

每个千牛浏览器登录对应店铺，票聚浏览器只登录一次共同开票公司。登录后可保持浏览器运行，也可通过工作台保存会话后正常关闭；下次技能按原配置启动并重新检查实际登录。是否同时常驻全部环境取决于机器资源，创建 10 个目录并不代表应立即启动 10 个进程。

连接检查：

```powershell
& $python "$skill/scripts/manage_browsers.py" status `
  --registry "$work/environments/shops.json" --all
```

`status` 是只读诊断，不启动浏览器、不补页，也不证明已经认证；`authentication: not_checked` 只表示此命令检查了连接和页面角色。浏览器关闭、登录页或缺页可能报告需处理状态，不应因此要求用户先补开业务页。日常任务会按需启动并按固定 URL 自动补缺页一次，随后正式核验当前店铺与票聚主体。

## 日常按店串行执行

首次人工登录后，无论所选浏览器当前已开还是已关闭，都可直接运行：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --date 'YYYY-MM-DD' `
  --node $node --node-modules $nodeModules
```

处理约定的“全部待处理”时：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --all-pending `
  --node $node --node-modules $nodeModules
```

此模式按创建时的北京时间今日向前两个日历月、首尾都包含。例如 2026-09-28 创建批次，固定查询 2026-07-28 至 2026-09-28。所有店铺共享同一冻结范围，九店仍串行调用、共用一个票聚环境，不逐店重算日期。新任务的 `--date` 与 `--all-pending` 必须二选一；不同时给，也不省略后默认查全部。

省略 `--shops` 按合并清单顺序执行新批次开始时的全部店铺，包括工作台后来登记的有效店铺。指定部分店铺时，按参数顺序执行：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --date 'YYYY-MM-DD' `
  --shops shop01 shop02 --node $node --node-modules $nodeModules
```

默认每店的 `OnlineRunner` 复用本店千牛和共享票聚；确认环境未运行时按原数据目录、Profile 和固定端口启动一次。端口冲突、其他进程占用或归属不符不能当作“未运行”绕过。若只允许连接已打开环境，给批次命令增加 `--connect-only`，该选项传递给各单店；它只禁止启动进程，仍会补业务页和执行采集。

两个控制器按规范化数据目录排序获取锁，共享票聚锁覆盖整个单店任务，包括本地文件生成。完成后断开控制连接，保留浏览器，再进入下一店。首版按店串行，不能并发复用共享票聚；工作台维护遇到同目录任务占用应跳过。

每店再次核验当前千牛店铺及共同票聚 issuer、coid、uid。只共享浏览器登录环境，各店原件、订单、商品映射和生成文件不混用。业务规则与单店一致。

各店税率通过技能根目录 `tax-rates.json` 独立配置，也可传 `--tax-rate-config`。批次在开始时一次读取配置，以每店完整 `store` 选择例外或默认规则并冻结；某店可取票聚，其他店可设固定小数税率。后续修改配置不改变本批次尚未开始的店铺，恢复沿用原规则，旧批次缺少规则时仍取票聚。详见[税率配置](tax-rates.md)。

明确归属于千牛的单店认证、权限或环境问题，如登录失效、店铺不符、页面缺失、断连或 profile 锁冲突，会记录该店失败后继续。申请列表接口平台 `code=1004` 表示缺少发票列表查看权限，记录为 `permission_required` 并继续其他店铺；由主账号为此子账号授权后，使用原批次 `--resume` 恢复该店。共享票聚故障、未知站点故障、检查点/文件/数据等未明确归类错误停止整批，避免对同一共享故障重复尝试。

## 状态、恢复与交付

批次创建 `outputs/batch-日期-时间/`，最近两个月模式使用 `outputs/batch-all-pending-时间/`，包含 `batch-state.json`、`batch-summary.json`、`batch-summary.csv` 和 `shops/<店铺id>/`。最近两个月模式保存 `date=null` 和包含开始、结束日期的 `query_scope`，每店 XLSX 使用 `all-pending` 文件标签，报告仍明确真实日期范围。

每店成功时独立交付：税局模板 XLSX、字节一致的原始通用模板 XLSX、`exceptions.csv`、`run.json`。没有可生成申请时按单店规则不生成税局 XLSX，但保留原件、异常和报告。批次汇总记录各店状态、数量、金额、错误码/站点和产物路径；总计只包含已完成店铺，不能当作全部店铺总额。

这里只生成文件，不提交实际开票，也没有跨任务已开票登记。再次新建批次可能包含平台仍标为待处理的同一申请；不能宣称新批次会自动去重开票。继续当前工作应恢复原批次，其成功导出及已完成店铺不重复采集。

明确空列表且实际导出 HTTP 成功、响应为零字节的店铺正常完成为 `no_applications`：保留 `applications.json`、空下载检查点 `common-export.bin`、异常表头和报告，不制造两种 XLSX。该店汇总数量、金额为零，通用模板和税局模板路径为 `null`，空响应路径与哈希作为完成证据；这不是失败，也不阻断下一店。列表为零但导出非空时保留原件；started模式交集为空，不扩大入选范围。恢复校验这些证据后直接跳过完成店铺，不为取得一个空表再次导出。完整判定见[输入输出](input-output-contract.md#原始检查点)。

| 批次状态 | 含义 |
| --- | --- |
| `complete` | 所选店铺均进入单店成功终态，可能包含整店暂缓或无申请 |
| `partial` | 全部所选店铺已尝试，仍有明确的单店失败 |
| `stopped` | 共享或未明确归类的故障使剩余店铺未执行 |
| `interrupted` | 批次控制或检查点等异常导致中断 |

修复登录或连接等故障后，保留本批次选中环境及检查点，恢复原批次：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" `
  --resume "$work/environments/outputs/原批次目录" `
  --node $node --node-modules $nodeModules
```

新批次冻结日期模式及开始/结束日期、选中店铺及顺序、所选账号约束、这些店铺和共享票聚的浏览器环境身份；恢复还核对原始 `observed_store/account_nick`，不能临时改账号或用另一个别名放宽检查。`--all-pending` 跨日恢复也沿用创建时的范围，不重算今日、不移动窗口或切换日期模式。后来新增但未被该批次选中的有效店铺不会造成恢复失配，也不会自动加入旧批次。要处理新店，应创建新批次或指定新店运行。旧 v1 批次继续沿用原来的全清单身份检查，不能手改版本或哈希放宽恢复条件。

程序在任何新采集前校验所有成功店铺的保存文件哈希；通过后直接跳过，不重新连接、核验身份或采集这些店铺。未完成店铺使用自己的原作业目录恢复，必要时按原配置启动，再重新核验两站身份并复用成功批次。文件缺失或变化则停止，不能把重采结果覆盖成原成功记录。恢复命令也可使用 `--connect-only` 禁止启动进程。

单独运行一店也可使用共享票聚：

```powershell
& $python "$skill/scripts/run_online.py" `
 --date 'YYYY-MM-DD' --store '本次千牛店铺' --issuer '本次票聚开票公司' `
  --expected-account '已确认的完整千牛子账号' `
  --browser-config "$work/environments/config/shop01.json" `
  --jst-browser-config "$work/environments/config/piaoju.json" `
  --output-root "$work/single-shop-check" `
  --node $node --node-modules $nodeModules
```

没有账号约束时省略 `--expected-account`，此时 `--store` 按原规则精确核验；提供账号后它可以是显示别名。省略 `--jst-browser-config` 时，原来的三角色单浏览器配置仍可使用。不能在单店恢复过程中改变布局、账号约束或原始身份记录。

## 验收边界

自动化程序管理固定流程、数据规则和检查点；AI 仍可解释异常、检查新接口变化和维护规则，不会因浏览器分离而失去介入能力。登录和验证码由人工完成，不把账号密码写入公开技能或运行脚本。

已完成共享票聚 + 一店的真实采集、表格生成及批次恢复验证；完成店铺恢复时未重采，原交付文件和两浏览器的页面标识均保持不变。九店顺序调度、故障分流、检查点变化阻断由合成测试覆盖，尚未完成真实 9 店全流程验收。继续扩展店铺后，再分别验证全部店铺、隔夜登录、断网恢复及机器资源容量。连接成功不等于业务全流程成功，单店成功也不等于 9 店验收通过。
