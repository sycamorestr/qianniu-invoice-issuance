# 千牛税局模板生成 Skill

按申请日期导出千牛通用模板，关联订单商品编码和聚水潭票聚开票资料，生成经过校验的税局导入表格、异常清单及原始通用模板副本。只生成文件，不提交实际开票。

浏览器使用原生 Edge 保存登录态，Playwright 通过 CDP 连接并复用业务页面。既支持一个浏览器中的三个页面，也支持每店独立千牛浏览器与一个共享票聚浏览器，例如 9 店 + 1 票聚。采集脚本在页面内调用接口，本地 Python 应用业务规则，Artifact Tool 填写工作簿。日常任务结束后浏览器继续运行。

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

下面是单浏览器三页面模式。需要多个店铺共享票聚时，直接按照[多店配置与运行](references/multi-shop.md)初始化，无需为每店重复准备票聚。

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

保持专用 Edge 运行，让 Codex 执行：

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

日期、店铺、公司名称必须换为本次实际值。程序会核对当前登录主体，复用发票、订单、票聚三个页面；启动时缺少业务页会按配置 URL 补开一次。后续采集按订单最多 50 单一批、票聚最多 40 编码一个检查点和 8 路并发执行。只有缺失或歧义项需要补读订单详情。

浏览器未运行时，先使用首次准备中的 `start` 命令。登录失效、验证码、主体不符或连接失败时，程序保留进度并停止，避免重复尝试。

## 多店与共享票聚

多店采用每店独立 `user_data_dir`：店铺浏览器只保留千牛发票/订单两个页面，共享票聚浏览器只保留商品页。所有店铺在同一已核验的开票公司下按店串行执行，每店独立生成文件。

准备店铺清单、创建 9 + 1 环境、启动并人工登录，见[多店配置与运行](references/multi-shop.md)。日常用一个批次命令：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --date '2026-01-01' `
  --node $node --node-modules $nodeModules
```

可用 `--shops shop01 shop02` 只处理部分店铺。局限于单店的明确登录/环境故障会记录后继续下一店；共享票聚或未知故障停止整批。恢复时先校验已完成店铺文件的哈希，然后跳过这些店铺，不重新采集。每店保留完整交付物，批次另有 `batch-summary.json` / `batch-summary.csv`。

单独执行某店也可给 `run_online.py` 增加 `--jst-browser-config <共享票聚配置>`，此时主配置须仅含 `invoice/orders`，共享配置须仅含 `goods`。省略此参数仍支持原来的三页面单浏览器模式。

## 恢复与离线复核

修复登录或环境问题后，用原作业目录恢复。不要编辑原始检查点：

```powershell
& $python "$skill/scripts/run_online.py" `
  --resume "$work/outputs/原作业目录" `
  --node $node --node-modules $nodeModules
```

恢复会重新核对两站主体和文件哈希，复用已成功批次。只想重做本地规则和表格时：

```powershell
& $python "$skill/scripts/run_online.py" `
  --replay-input "$work/outputs/原作业目录" `
  --output-root "$work/replays" `
  --node $node --node-modules $nodeModules
```

离线重放不访问浏览器，也不代表当前线上状态。增加 `--plan-only` 可只检查计划、数量、金额和异常，不写税局 XLSX。旧后端作业不能直接在线恢复；已有业务快照仍可离线重放。

## 交付与规则

完整成功后，作业的 `generated` 目录包含：

- `qianniu_invoice_tax_template_日期.xlsx`：可开票申请的税局导入表格。
- `qianniu_common_日期.xlsx`：与页面导出原件字节一致，供人工核对。
- `exceptions.csv`：暂缓与负数排除原因。
- `run.json`：范围、数量、金额、输入哈希和校验结果。

通用模板中精确为“待处理”的源行定义范围；负总额整票排除。数量、商品金额和折扣取通用模板；价外费用只累加到同申请、同订单的唯一正商品。编码匹配必须唯一，票聚资料不完整则整票暂缓。规格取“颜色及规格”，明确零税率填写文本 `0`。原模板四张业务表、隐藏字典、样式及校验保留。

无可生成申请时不输出可导入税局模板，仍保留原件副本、异常和报告。采集中途失败则保留已取得的原件与检查点；失败不能称为完整交付。

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

当前校验涵盖数据、金额和原模板结构，没有图像视觉检查或税局实际上传验证。多店首版提供串行调度与恢复，不能以离线测试或单店成功宣称真实 9 店全流程已验收；隔夜会话、异常断网及机器同时常驻多个浏览器的资源容量还需验证。AI 负责理解任务、解释异常及维护规则，登录和验证码仍由人工完成。
