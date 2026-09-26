---
name: qianniu-invoice-issuance
description: "按申请日期为单店或多店导出千牛通用模板，复用已登录千牛与票聚页面核对商品，生成税局模板、异常清单及原始通用模板副本。"
---

# 千牛税局模板生成

用户说“开某天的票”时，交付校验后的税局模板、原始通用模板副本、异常清单和结果报告。本技能只生成文件，不提交实际开票。

## 执行入口

单店使用 `scripts/run_online.py`，多店使用 `scripts/run_batch.py`，不手工串联采集阶段。先确认日期、店铺/店铺清单、票聚开票主体及配置；缺少的必需信息不能从历史样例推定。

复用能导入 `playwright` 和 `lxml` 的 Python；缺依赖时按 `requirements.txt` 安装。通过 `load_workspace_dependencies` 获取本次 Codex bundled Node 和 Artifact Tool 的 `node_modules` 路径。不要因 bundled Python 缺包而反复安装或切换已可用解释器。

```powershell
& $python "$skill/scripts/run_online.py" `
  --date 'YYYY-MM-DD' --store '本次千牛店铺' --issuer '本次票聚主体' `
  --browser-config $config --output-root "$work/outputs" `
  --node $node --node-modules $nodeModules
```

多店配置为每店独立千牛浏览器加一个共享票聚浏览器，所有店铺核验同一 issuer，按店串行执行。使用 `run_batch.py --registry <shops.json> --date YYYY-MM-DD`，Node 参数同上；新批次默认处理原清单及旁侧 `.browser-workbench-shops.json` 合并后的所有当前店铺，`--shops` 可指定店铺 id。先读[多店配置与运行](references/multi-shop.md)，不要为工作台已有店铺再建 Profile，也不要为每店再建票聚环境。单店参数 `--jst-browser-config` 可选择共享票聚；省略时保持三角色单浏览器。

唯一在线路线是原生 Edge + Playwright `connect_over_cdp()`，不安装其他桥接后端或浏览器扩展。首次安装/初始化见 [README](README.md)；只在配置、恢复、接口或业务解释需要时读取对应参考文档：

- 参数、检查点、终态、恢复和测试：[输入输出与运行](references/input-output-contract.md)。
- 浏览器启动、页面恢复、锁及多店边界：[浏览器控制](references/playwright-browser.md)。
- 接口或页面故障：[页面接入](references/page-integration.md)。
- 修改/解释业务判断：[详细规则](references/detailed-rules.md)；修改/核对工作簿字段：[字段映射](references/flow-and-field-mapping.md)。

## 正常流程与恢复

输出作业锁 → 浏览器数据目录锁 → 复用 Edge 或按原配置启动一次 → 复用三个业务页并补齐缺页一次 → 核验店铺/票聚主体 → 申请诊断与通用模板导出 → 订单批查 → 票聚查询 → 必要详情补证 → 本地生成与独立复核 → 发布结果。

- `run_online.py` / `run_batch.py` 默认按需启动：已运行则复用；确认未运行时，按现有固定 `user_data_dir`、Profile 和非零端口正常启动 Edge 一次，读取该目录保存的 Cookie、Local Storage 等。无需先手工开浏览器；冲突、锁占用和断连不触发换目录、换端口或循环重启。`--connect-only` 禁止启动进程，仍执行业务采集与缺页恢复；诊断 `status` 保持只读。
- 工作台只维护千牛 `home`。技能兼容工作台的主页配置，在内存中补入 `invoice/orders` 业务角色和 URL，复用已有业务页或补齐缺页一次，不改配置文件、不覆盖原主页、不关闭用户标签。
- 页面存在、URL 不含 login 均不是登录证明。清单提供已确认的 `login_username` 时，以页面 `context.realNick` 精确核验完整子账号，允许 `store` 保留工作台显示别名；未提供时仍按原 `store` 精确核验。单店可用 `--expected-account` 提供同一账号约束。账号不是密码或任意备注，不猜别名，不自动输入账密；原观察值 `observed_store/account_nick` 保留并在恢复时固定。采集前仍核对票聚公司及两站租户标识；登录失效、验证码、主体不符、页面丢失或断连时保存进度并报告具体问题，停止无意义尝试。
- 订单每批最多 50 单，取完整分页；只对缺失或匹配歧义项补详情，之后恢复同一个订单列表页。票聚每个恢复批次最多 40 编码、8 路并发，接口参数和重试由代码管理。
- 中断后用 `--resume <原作业目录>`；恢复重新核对两站主体及哈希，只继续未完成工作。连接、锁、文件发布错误不能触发重复成功采集。不要手改检查点或删除锁文件。
- `--replay-input <原始输入目录>` 仅离线复核；`--plan-only` 不写税局 XLSX。结束时断开控制连接，浏览器继续运行。
- 多店共享票聚锁覆盖整个单店任务。明确的千牛单店认证/环境故障记录后可继续下一店；共享票聚或未知故障停止整批，不重复触发。新批次冻结选中店铺、顺序及环境身份；后续新增未选店铺不扩入原批次，也不阻断其恢复。恢复先校验成功店铺文件哈希并跳过，只恢复未完成店铺；旧 v1 批次沿用原来的全清单身份检查，不手改检查点升级。

## 不可变业务约定

1. 通用模板原件定义范围：优先“开票状态”，为空才回退“申请状态”，仅精确为“待处理”的源行进入生成。`applications.json` 的 `applyStatus` 只作诊断，不能改变范围。
2. 申请流水号为发票主键。数量、金额、折扣取通用模板；订单和票聚金额只能用作已证明口径的匹配证据。匹配不唯一或资料缺失时整票暂缓，不猜配、不丢部分商品凑平金额。
3. 负总金额发票整票排除，基本信息与明细两个 sheet 都不写。正额申请中相邻负折扣仍作为正商品的折扣；确保 `selected = ready + blocked + excluded`。
4. 价外费用累加到同申请、同订单唯一正商品金额，数量和折扣保持不变，保留费用源行证据；归属不明或冲突时整票暂缓。
5. 票聚不按商品类型或商品状态过滤，仍要求 `invoice_enabled=true`。编码精确匹配；仅全角/半角括号差异可归一后唯一匹配并保留原编码。
6. 规格取 `properties_value`，不取 `invoice_spec`；明确零税率写文本 `0`，未知税率暂缓。单价留空；“是否展示购买方地址电话银行账号”留空，地址/电话/银行/账号本身照常填写。
7. 保留原模板四张可见业务表、隐藏字典、格式和校验。第三、四表数据区为空，不增加说明或审计 sheet，不修改内置空白模板。

## 交付与报告

平台返回通用模板时，必须交付字节一致副本 `qianniu_common_日期.xlsx`，以及 `exceptions.csv`、`run.json`；有可生成申请时再交付 `qianniu_invoice_tax_template_日期.xlsx`。唯一无原件例外：申请列表已明确为空，且仍尝试一次导出后得到 HTTP 成功的零字节响应，才以 `no_applications` 交付查询快照、空响应 `common-export.bin`、异常表头和报告，不制造 XLSX。列表为零但实际导出非空时仍以通用模板为准。给出可生成、暂缓、排除的数量与金额，并链接实际存在的文件。

终态为 `complete`、`no_applications`、`all_excluded`、`all_blocked`、`plan_only` 或 `failed`。无可生成申请时不制造空业务票。失败如已取得原件则保留原件及检查点，不宣称完整交付。根 `run.json` 表示最新作业状态，历史失败保留在 `attempts/`。

多店逐店交付同样文件，再交付 `batch-summary.json` / `batch-summary.csv`，明确已完成、失败、未执行的店铺及数量金额；汇总仅包含已完成店铺。批次 `complete` 表示所有选中店铺进入成功终态，不代表所有申请均可开票。

报告校验的实际范围：数据和模板结构检查不等于图像视觉检查或税局上传通过；离线模拟不等于线上验收；命令运行时间不等于代理任务总时间。每个独立浏览器使用不同数据目录、调试端口和下载目录，每店输出隔离，不能把单店或双浏览器连接测试当成 9 店业务或隔夜验收。AI 继续负责异常诊断与规则维护；登录过期或验证码需人工处理，不自动输入账密，不承诺免登录。

## 维护

`scripts/` 是维护入口，`assets/tax-bureau-template-V260401.xlsx` 是空白模板。业务规则修改同步详细规则、字段映射和相关测试；发布时排除业务快照、运行输出、登录资料和缓存。调试端口和进程登记仅保存在本地配置/运行时文件，不放入 Agent/UI 状态、错误或公开回执。
