# 多店配置与运行

本模式面向多个千牛店铺共用一个票聚开票主体，例如 9 个独立千牛浏览器 + 1 个共享票聚浏览器。各店按顺序执行同一套采集与生成流程，所有申请、检查点和交付文件按店隔离。

| 环境 | 用户数据目录 | 页面角色 | 身份要求 |
| --- | --- | --- | --- |
| shop01 至 shop09 | 每店独立 `user_data_dir` | `invoice`、`orders` | 当前千牛店铺须与清单一致 |
| piaoju | 独立共享 `user_data_dir` | `goods` 及商品 iframe | 每店均核验同一 issuer |

这里的独立 Profile 指独立用户数据根目录，每个通常使用 `Default` 子目录。浏览器会话保存在本机，运行任务时通过 CDP 附着，不每天重新登录。登录过期或验证码仍需人工处理。

## 初始化私有环境

依赖与 `$skill`、`$python`、`$node`、`$nodeModules` 变量按 [README](../README.md) 准备。下面的 `$work` 由使用者选择，放在仓库之外。

复制 `assets/shops.example.json` 到私有工作目录，再编辑副本。样例只有两条合成店铺；9 店使用时增加至 9 条，分别填写唯一的 `id` 和真实店铺名称。店铺 id 以小写字母开头，只使用字母、数字、下划线和连字符，不能使用 `shared` 或 `piaoju`。不要保存密码。

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

每个千牛浏览器登录对应店铺，票聚浏览器只登录一次共同开票公司。浏览器保持运行；是否同时常驻全部环境取决于机器资源，创建 10 个目录并不代表应立即启动 10 个进程。

连接检查：

```powershell
& $python "$skill/scripts/manage_browsers.py" status `
  --registry "$work/environments/shops.json" --all
```

`status` 不启动浏览器，也不证明已经认证；`authentication: not_checked` 只表示此命令检查了连接和页面角色。登录页或缺页可能报告 `needs_attention`。日常任务启动时会按固定 URL 自动补缺页一次，并正式核验当前店铺与票聚主体。

## 日常按店串行执行

准备好所选店铺和共享票聚的浏览器后：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --date 'YYYY-MM-DD' `
  --node $node --node-modules $nodeModules
```

省略 `--shops` 按清单顺序执行全部店铺。指定部分店铺时，按参数顺序执行：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" --date 'YYYY-MM-DD' `
  --shops shop01 shop02 --node $node --node-modules $nodeModules
```

每店的 `OnlineRunner` 连接本店千牛和共享票聚；两个控制器按规范化数据目录排序获取锁，共享票聚锁覆盖整个单店任务，包括本地文件生成。完成后断开控制连接，保留浏览器，再进入下一店。首版按店串行，不能并发复用共享票聚。

每店再次核验当前千牛店铺及共同票聚 issuer、coid、uid。只共享浏览器登录环境，各店原件、订单、商品映射和生成文件不混用。业务规则与单店一致。

明确归属于千牛的单店认证或环境问题，如登录失效、店铺不符、页面缺失、断连或 profile 锁冲突，会记录该店失败后继续。共享票聚故障、未知站点故障、检查点/文件/数据等未明确归类错误停止整批，避免对同一共享故障重复尝试。

## 状态、恢复与交付

批次创建 `outputs/batch-日期-时间/`，包含 `batch-state.json`、`batch-summary.json`、`batch-summary.csv` 和 `shops/<店铺id>/`。

每店成功时独立交付：税局模板 XLSX、字节一致的原始通用模板 XLSX、`exceptions.csv`、`run.json`。没有可生成申请时按单店规则不生成税局 XLSX，但保留原件、异常和报告。批次汇总记录各店状态、数量、金额、错误码/站点和产物路径；总计只包含已完成店铺，不能当作全部店铺总额。

| 批次状态 | 含义 |
| --- | --- |
| `complete` | 所选店铺均进入单店成功终态，可能包含整店暂缓或无申请 |
| `partial` | 全部所选店铺已尝试，仍有明确的单店失败 |
| `stopped` | 共享或未明确归类的故障使剩余店铺未执行 |
| `interrupted` | 批次控制或检查点等异常导致中断 |

修复故障后，保留原清单、配置和文件，恢复原批次：

```powershell
& $python "$skill/scripts/run_batch.py" `
  --registry "$work/environments/shops.json" `
  --resume "$work/environments/outputs/原批次目录" `
  --node $node --node-modules $nodeModules
```

恢复时日期、选中店铺及顺序、浏览器配置身份不能变更。程序在任何新采集前校验所有成功店铺的保存文件哈希；通过后直接跳过，不重新连接、核验身份或采集这些店铺。未完成店铺使用自己的原作业目录恢复，重新核验两站身份并复用成功批次。文件缺失或变化则停止，不能把重采结果覆盖成原成功记录。

单独运行一店也可使用共享票聚：

```powershell
& $python "$skill/scripts/run_online.py" `
  --date 'YYYY-MM-DD' --store '本次千牛店铺' --issuer '本次票聚开票公司' `
  --browser-config "$work/environments/config/shop01.json" `
  --jst-browser-config "$work/environments/config/piaoju.json" `
  --output-root "$work/single-shop-check" `
  --node $node --node-modules $nodeModules
```

省略 `--jst-browser-config` 时，原来的三角色单浏览器配置仍可使用。不能在单店恢复过程中改变这一布局。

## 验收边界

自动化程序管理固定流程、数据规则和检查点；AI 仍可解释异常、检查新接口变化和维护规则，不会因浏览器分离而失去介入能力。登录和验证码由人工完成，不把账号密码写入公开技能或运行脚本。

已完成共享票聚 + 一店的真实采集、表格生成及批次恢复验证；完成店铺恢复时未重采，原交付文件和两浏览器的页面标识均保持不变。九店顺序调度、故障分流、检查点变化阻断由合成测试覆盖，尚未完成真实 9 店全流程验收。继续扩展店铺后，再分别验证全部店铺、隔夜登录、断网恢复及机器资源容量。连接成功不等于业务全流程成功，单店成功也不等于 9 店验收通过。
