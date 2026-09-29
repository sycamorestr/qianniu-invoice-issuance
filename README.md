# 千牛税局模板生成 Skill

按指定申请日期，或最近两个日历月，处理开票倒计时已开始的待处理申请，关联订单商品编码和聚水潭票聚开票资料，生成经过校验的税局导入表格、异常清单及原始通用模板副本。只生成文件，不提交实际开票。

浏览器使用原生 Edge 保存登录态，Playwright 通过 CDP 连接并复用业务页面。既支持一个浏览器中的三个页面，也支持每店独立千牛浏览器与一个共享票聚浏览器，例如 9 店 + 1 票聚。可直接使用[浏览器工作台](https://github.com/sycamorestr/browser-workbench)登记的环境：已开则复用，未开则按原 Profile 和固定端口启动一次。采集脚本在页面内调用接口，本地 Python 应用业务规则，Artifact Tool 填写工作簿。日常任务结束后浏览器继续运行。

已有工作台的日常使用路径是：完成首次人工登录并保存会话 → 告诉 Codex 本次查询范围和清单 → 技能按需启动、补齐业务页并核验两站身份 → 按店生成文件 → 核对原始通用模板及异常。无需每天先把所有浏览器和业务页手动打开；登录过期或验证码仍需人工处理。

## 安装与依赖

推荐 Windows + Codex 桌面端。将仓库克隆到技能目录；设置了 `CODEX_HOME` 时，改用其下的 `skills` 目录：

```powershell
$skill = Join-Path $env:USERPROFILE '.codex/skills/qianniu-invoice-issuance'
git clone https://github.com/sycamorestr/qianniu-invoice-issuance.git $skill
```

仓库包含 V260401 空白税局模板，请保留 `assets`。不需要 Microsoft Excel。

| 依赖 | 用途 | 已验证版本 |
| --- | --- | --- |
| Python | 编排、业务规则、校验 | 3.13 |
| Playwright Python | 连接本机 Edge | 1.63.0 |
| lxml | 保护原始 XLSX XML 结构 | 6.1.1 |
| Node.js | 执行工作簿作者程序与 JS 测试 | 24.19.0 |
| Codex bundled Artifact Tool | 写入 XLSX | 当前 Codex 配套环境 |
| Microsoft Edge | 保存会话、运行页面接口 | 本机已安装版本 |

推荐 Python 3.13 或更新版本；表中是已验证组合，不是所有版本的兼容性承诺。优先复用已有解释器：

```powershell
$python = (Get-Command python).Source
& $python -m pip install -r "$skill/requirements.txt"
& $python -c 'import playwright, lxml; print("Python dependencies ready")'
```

连接已有 Edge 不需要执行 `playwright install` 下载另一套浏览器。

**Artifact Tool 必须由当前 Codex 环境提供，不随仓库分发，也不假设能通过普通 npm 安装。** 让 Codex 调用 `load_workspace_dependencies`，将返回的 Node 可执行文件和包含 `@oai/artifact-tool` 的 `node_modules` 路径分别作为 `$node`、`$nodeModules`。没有该依赖可以运行规则测试和 `--plan-only`，不能生成最终税局 XLSX。

## 单店首次准备浏览器

下面是单浏览器三页面模式。已有工作台时直接阅读[已有工作台接入](references/multi-shop.md#已有浏览器工作台直接接入)，沿用私有清单和数据目录，不重新建 Profile。尚无环境且需要多个店铺共享票聚时，按照[多店配置与运行](references/multi-shop.md)初始化，无需为每店重复准备票聚。

配置与业务输出放在技能目录之外。以下为 PowerShell 示例，`$work` 可改为自己的工作目录：

```powershell
$work = Join-Path $env:USERPROFILE 'qianniu-invoice-work'
$config = Join-Path $work 'browser-config.json'
New-Item -ItemType Directory -Path $work -Force | Out-Null
if (-not (Test-Path -LiteralPath $config)) {
  Copy-Item "$skill/assets/browser-config.example.json" $config
}
& $python "$skill/scripts/playwright_controller.py" --config $config start
```

示例配置中的相对路径以配置文件所在目录为基准。它会为专用 Edge 保存独立用户数据，不使用日常浏览器的数据目录。可设置 `executable_path` 指定浏览器程序。

首次启动后，在千牛和票聚完成人工登录，并确认是本次店铺和开票主体。登录页可能使启动命令报告 `login_required`；浏览器会保留，完成登录后即可运行日常命令。登录信息保存在本机用户数据目录，后续通常直接复用；会话过期或验证码仍需人工处理。

## 日常执行

首次登录后，让 Codex 执行；专用 Edge 可以保持运行，也可以已正常关闭：

```text
使用 $qianniu-invoice-issuance，按指定申请日期、店铺和开票主体生成税局模板，
同时交付原始通用模板与异常清单。
```

或在上述变量已配置的 PowerShell 中运行：

```powershell
& $python "$skill/scripts/run_online.py" `
  --date '2026-01-01' `
  --store '示例店铺' `
  --issuer '示例开票公司' `
  --browser-config $config `
  --output-root "$work/outputs" `
  --node $node --node-modules $nodeModules
```

日期、店铺、公司名称必须换为本次实际值。程序默认复用已开的浏览器；确认环境未运行时，使用现有固定 `user_data_dir`、Profile 和非零端口正常启动 Edge 一次，读取保存的 Cookie、Local Storage 等。随后核对当前登录主体，复用发票、订单、票聚三个页面；缺少业务页会按业务 URL 补开一次。后续采集按订单最多 50 单一批、票聚最多 40 编码一个检查点和 8 路并发执行。只有缺失或歧义项需要补读订单详情。

处理“全部待处理”时，用 `--all-pending` 替换 `--date`，或告诉 Codex“生成最近两个日历月内全部待处理申请的税局模板”。这里的全部按约定限定为北京时间今日向前两个日历月，包含首尾日期。例如 2026-09-28 创建的任务查询 2026-07-28 至 2026-09-28。新任务默认再限定“开票倒计时＝已开始”，单日与多店也一样，无需额外参数。必须显式选择一个日期范围参数；两者不能同时使用，也不能都省略。

工作台显示名称可以保留自己的别名。单店命令增加 `--expected-account '已确认的完整千牛子账号'` 时，技能将该账号与页面 `context.realNick` 精确核验；不传时仍按 `--store` 做原来的精确核验。这里的账号不是密码，不用于自动登录，不能填写猜测的别名或普通备注。

若本次只允许连接已经打开的环境，增加 `--connect-only`；它禁止启动进程，仍会补业务页和采集数据。诊断 `status` 命令保持只读，不会因查看状态而启动或补页。端口冲突、进程归属不符或目录锁占用时停止，不更换端口或 Profile 绕过；登录失效、验证码、主体不符或采集中途断连时保留进度，不循环重试。

## 多店与共享票聚

多店采用每店独立 `user_data_dir`，另用一个共享票聚环境。工作台只打开和维护千牛主页 `home`；技能在内存中补入 `invoice/orders` 业务角色，复用或补开业务页，保留原主页，不改写工作台配置。所有店铺在同一已核验的开票公司下按店串行执行，每店独立生成文件，共享票聚只需一份登录环境。

已有工作台时直接使用其私有 `shops.json`，技能自动合并旁侧 `.browser-workbench-shops.json` 中新增店铺。清单仍需提供本次共同开票主体 `issuer`。店铺记录已有 `login_username` 时，批次将它作为完整子账号约束传给单店运行器，允许显示名称与平台店铺昵称不同；没有账号时保留原店铺名精确核验。工作台的“已登录”状态不能代替本次正式身份核验。尚无环境时的创建和首次人工登录步骤见[多店配置与运行](references/multi-shop.md)。日常用一个批次命令，将路径替换为自己的清单：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --date '2026-01-01' `
  --node $node --node-modules $nodeModules
```

多店处理约定的全部待处理：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --all-pending `
  --node $node --node-modules $nodeModules
```

整个批次创建时固定同一组开始、结束日期，九店仍按顺序执行，共享一个票聚环境。不会逐店重新计算今日，也不会在接口中省略日期而扩大为全历史。

新批次默认执行合并清单中的所有当前店铺，也可用 `--shops shop01 shop02` 只处理部分店铺。`--connect-only` 可禁止整个批次启动浏览器。局限于单店的明确登录/环境故障会记录后继续下一店；共享票聚或未知故障停止整批。每店都持有本店与共享票聚的目录锁，按店串行执行；工作台后台不是运行技能的必需依赖。

批次开始时冻结选中店铺、顺序、账号约束和相关环境身份，原始观察到的店铺昵称与账号也留存并用于恢复核对。后来新增未选中的有效店铺不加入旧批次，也不阻断其恢复。恢复先校验已完成店铺文件的哈希并跳过，不重新采集；旧 v1 批次仍沿用原来的全清单检查。每店保留完整交付物，批次另有 `batch-summary.json` / `batch-summary.csv`。

单独执行某店也可给 `run_online.py` 增加 `--jst-browser-config <共享票聚配置>`，此时主配置可只含工作台 `home`，由技能在内存中补齐业务角色，也兼容原 `invoice/orders` 配置；共享配置须仅含 `goods`。省略此参数仍支持原来的三页面单浏览器模式。

## 恢复与离线复核

修复登录或环境问题后，用原作业目录恢复。不要编辑原始检查点：

```powershell
& $python "$skill/scripts/run_online.py" `
  --resume "$work/outputs/原作业目录" `
  --node $node --node-modules $nodeModules
```

进入单店恢复后会重新核对两站主体和文件哈希，复用已成功的采集批次；默认仍可按原配置启动已关闭浏览器，增加 `--connect-only` 可禁止启动。恢复沿用创建时冻结的查询范围，`--all-pending` 跨日恢复也不移动两个月窗口；不能切换为单日或改范围。多店恢复入口及跳过已完成店铺的规则见[多店恢复](references/multi-shop.md#状态恢复与交付)。只想重做本地规则和表格时：

```powershell
& $python "$skill/scripts/run_online.py" `
  --replay-input "$work/outputs/原作业目录" `
  --output-root "$work/replays" `
  --node $node --node-modules $nodeModules
```

离线重放不访问浏览器，也不代表当前线上状态。增加 `--plan-only` 可只检查计划、数量、金额和异常，不写税局 XLSX。旧后端作业不能直接在线恢复；已有业务快照仍可离线重放。

## 交付与规则

正式任务结束后自动将交付文件整理为 ZIP。需要推送到企业微信群时，将 `assets/notifications.example.json` 复制为技能根目录的 `notifications.json`，填入群机器人 `webhook_url` 并将 `enabled` 改为 `true`。单店、多店和独立补发默认读取此配置，位置相对于技能脚本，不依赖当前工作目录。配置一次后，迁移时复制整个技能目录即可携带企微配置。

`--notification-config` 可显式指定其他配置；独立补发对应 `--config`。仅当技能内没有配置时，多店兼容清单旁的旧配置，单店兼容实际输出根目录上级的旧配置；技能内显式关闭推送时不回退。真实 `notifications.json` 由 Git 忽略，GitHub 只保留 Webhook 留空、推送关闭的示例。更新或发布 skill 时保留本机私有配置，不把它提交，也不以空模板覆盖。

企微只发送最终汇总文件消息。多店批次全部完成后，按店铺分目录推送一个包；部分完成、停止或中断只保留本地 ZIP，恢复完成后再发，不发送逐店或中间消息。批次子店单独恢复或补发也不会外发；独立单店任务仍发送本店最终包。

压缩包命名为 `千牛平台_<店铺或N店铺>_开票汇总_<日期或范围>_<内容哈希前12位>.zip`。相同内容与目标已发送成功时复用回执；仅更名不改变 ZIP 内容，也不重新发送。`--no-notify` 保留本地 ZIP、暂停本次外发。离线重放不自动外发，`--plan-only` 不打包推送。推送失败不改变已完成的开票文件，可只重试推送最终汇总而不查询业务页面。配置、包内文件和补发命令见[打包与企微推送](references/notifications.md)。

ZIP 根目录的 `千牛平台_开票汇总日志.txt` 是 UTF-8 中文人工核对日志，说明查询日期与倒计时条件、整批状态、哪些店已生成税局模板、哪些店未生成及其原因。逐店列出生成、暂缓、负数排除的笔数与金额，并按已校验的异常清单汇总原因。失败或未执行店铺显示结果未确认，不以零笔代替；日志明确“已生成模板”不代表实际提交开票。日志随最终 ZIP 一次发送，不另外发送文本消息。给历史已发送包补加日志时，只在本地重新打包，不自动重推。

完整成功后，作业的 `generated` 目录包含：

- `qianniu_invoice_tax_template_日期.xlsx`：可开票申请的税局导入表格。
- `qianniu_common_日期.xlsx`：与页面导出原件字节一致，供人工核对。
- `exceptions.csv`：暂缓与负数排除原因。
- `run.json`：范围、数量、金额、输入哈希和校验结果。

`--all-pending` 使用 `qianniu_common_all-pending.xlsx` 和 `qianniu_invoice_tax_template_all-pending.xlsx`，报告保存真实开始、结束日期。它只生成文件；新建任务可能再次包含平台仍标为待处理的申请，技能没有跨任务已开票登记，不会自动消除不同批次之间的重复开票风险。继续未完成工作应恢复原批次。

通用模板中精确为“待处理”的源行，再与倒计时“已开始”的完整申请清单取交集，才进入生成；负总额整票排除。平台导出会忽略倒计时参数，因此不能只加导出条件，交付原件仍可能包含未开始倒计时的申请。selection.json 记录筛选快照哈希及未入选流水号。数量、商品金额和折扣取通用模板；价外费用只累加到同申请、同订单的唯一正商品。编码匹配必须唯一，票聚资料不完整则整票暂缓。规格取“颜色及规格”，明确零税率填写文本 `0`。原模板四张业务表、隐藏字典、样式及校验保留。

倒计时条件保存为 `query_scope.countdown="started"`，对应页面“已开始”，不包含另列的“已超时”“未开始”。恢复或重放旧的无筛选任务保持旧范围，采用新条件须新建任务；此前交付的文件不会被自动重写。

无可生成申请时不输出可导入税局模板，仍保留已取得的原件副本、异常和报告。仅当平台申请列表明确为空，且实际导出请求 HTTP 成功、响应为零字节时，以 `no_applications` 交付 `applications.json`、空响应 `common-export.bin`、仅含表头的 `exceptions.csv` 和报告，不制造通用模板或税局 XLSX；报告中的文件路径为 `null` 是该终态的正常结果。列表为零但导出非空时保留真实原件；started模式交集为空，不把其他申请纳入。采集中途失败则保留已取得的原件与检查点；失败不能称为完整交付。

新导出先保存不可变下载检查点 `common-export.bin`，非空内容通过 ZIP 校验后原字节复制为 `qianniu_common.xlsx`。恢复复用下载检查点，不再次触发成功导出；旧作业直接保存的 XLSX 检查点继续兼容。空响应判定及交付字段见[输入输出](references/input-output-contract.md#原始检查点)。

完整业务规则见 [详细规则](references/detailed-rules.md) 和 [字段映射](references/flow-and-field-mapping.md)。实现与恢复契约见 [输入输出](references/input-output-contract.md)，浏览器生命周期见 [浏览器控制](references/playwright-browser.md)，接口说明见 [页面接入](references/page-integration.md)。

## 测试与边界

在仓库根目录执行：

```powershell
python scripts/test_build_invoice_plan.py
python scripts/test_pipeline.py
python -m unittest discover -s scripts -p 'test_*.py'
node scripts/test_read_collectors.mjs
```

Windows GitHub Actions 运行离线回归，不需要业务账号。真实快照与登录资料不进入仓库。换电脑或站点接口发生变化后，还需进行少量真实申请验收。

当前校验涵盖数据、金额和原模板结构，没有图像视觉检查或税局实际上传验证。多店提供串行调度与恢复，工作台接入及按需启动的真实九店全流程仍需依据实际运行结果验收，不能以离线测试或单店成功替代；隔夜会话、异常断网及机器同时常驻多个浏览器的资源容量也需分别验证。AI 负责理解任务、解释异常及维护规则，登录和验证码由人工完成，不自动输入账密，不承诺保存后永久免登录。
