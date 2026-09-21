# 千牛税局模板生成 Skill

按申请日期导出千牛通用模板，批量查询订单商品编码和聚水潭票聚商品信息，生成税局导入表格及异常清单。支持一张发票多个商品、折扣行匹配和逐票校验。

本项目生成表格，不提交实际开具发票。

## 安装

建议使用 Windows 和 Codex 桌面端。将整个仓库放到 Codex 的技能目录，默认位置为：

```text
%USERPROFILE%\.codex\skills\qianniu-invoice-issuance
```

如果设置了 `CODEX_HOME`，使用其下的 `skills/qianniu-invoice-issuance`。可以下载 ZIP 后解压，也可以克隆：

```powershell
git clone https://github.com/sycamorestr/qianniu-invoice-issuance.git "$env:USERPROFILE/.codex/skills/qianniu-invoice-issuance"
```

复制时保留 `assets`，内置 V260401 空白税局模板，无需另行提供模板。安装后在新一轮对话中调用：

```text
使用 $qianniu-invoice-issuance，先检查依赖和浏览器连接，
然后按指定申请日期生成税局模板表格和异常清单。
```

## 依赖

| 组件 | 作用 | 已验证版本 |
| --- | --- | --- |
| Python | 数据处理和校验 | 3.12.14 |
| lxml | 保留模板原生 XML 结构 | 6.1.1 |
| Node.js | 执行表格生成及测试 | 24.19.0 |
| @oai/artifact-tool | 生成 XLSX | 2.8.59 |
| PowerShell | 页面采集桥接 | 7.6.5 |
| OpenCLI 与浏览器扩展 | 连接已登录业务页面 | OpenCLI 1.8.6 |

以上是已验证组合，不是最低版本声明。Python、Node、Artifact Tool 优先使用 Codex 的 `load_workspace_dependencies` 提供的 bundled 环境。`requirements.txt` 仅声明 Python 第三方依赖，不包含 Node 依赖。

**Artifact Tool 不随仓库分发。** 首次使用需要确认同事的 Codex 环境能提供 `@oai/artifact-tool`。普通 Python 和 Node 安装本身不足以生成最终表格；没有该库时需先配置兼容环境。无需安装 Microsoft Excel。

使用现有 PowerShell 桥接时，需要 OpenCLI 和相应浏览器扩展。CLI 可通过 `npm install -g @jackwener/opencli@1.8.6` 安装；浏览器扩展连接后运行 `opencli doctor` 检查。建议同时配备 `opencli-usage`、`opencli-browser` 和 Spreadsheets skill。其他浏览器工具可以执行同样的只读页面脚本，但应单独验证连接方式。

每位使用者在自己的浏览器登录千牛和票聚，并具备申请导出、订单查看和商品查询权限。登录态、Cookie、令牌不放进仓库。

## 业务规则

- 申请流水号为发票主键，基本信息每票一行，商品明细逐行重复流水号。
- 金额、数量和折扣取千牛通用模板；订单金额仅用于匹配核对。
- 票聚商品类型、商品状态均不限；商品编码精确匹配且明确允许开票。
- 规格取“颜色及规格”，明确零税率输出空白，单价留空。
- “是否展示购买方地址电话银行账号”留空，地址、电话、银行和账号字段正常填写。
- 四张可见业务表保留，第三、四表数据区为空；隐藏字典、格式和校验保留。
- 价外费用等未定义映射整票暂缓，进入异常清单。

完整规则见 [详细规则](references/detailed-rules.md)、[字段映射](references/flow-and-field-mapping.md)。

## 运行和输出

浏览器采集由 Codex 在已核对的登录页面执行；本地入口 `scripts/run_invoice.py` 负责处理保存的采集快照。日期、店铺、主体、输入和输出目录均显式传入，默认模板相对 skill 目录定位；需要其他模板版本时可传 `--template`。

命令参数和采集文件结构见 [输入输出与运行](references/input-output-contract.md)，页面及接口记录见 [页面接入](references/page-integration.md)。

最终交付：

- `qianniu_invoice_tax_template_日期.xlsx`：通过校验的税局导入表格。
- `exceptions.csv`：暂缓申请及具体原因。
- `run.json`：范围、数量、金额、输入哈希和校验状态。

没有申请或全部暂缓时，不生成可导入的发票数据文件。原始采集和中间文件留在本地作业目录，不在本仓库发布。

## 验证

在已配置的运行环境中执行：

```powershell
python scripts/test_build_invoice_plan.py
python scripts/test_pipeline.py
node scripts/test_read_collectors.mjs
```

当前包含 25 项计划测试、9 项管线测试及 8 项模拟采集检查。模拟检查不访问线上接口。内置模板已用于历史快照完整重放；换电脑首次使用应再进行少量真实申请验证。

Windows 下 Artifact Tool 图像渲染曾异常退出，当前输出通过数据和原生模板结构校验；这不等同于完成图像视觉检查或税局实际上传验证。

## 分享与维护

分享整个仓库即可，无需附带历史订单、发票、运行输出或认证资料。只在本地使用独立作业目录保存业务数据。

本仓库维护 skill 源文件。规则或页面字段更新时，同步更新对应脚本、说明及测试；发布前检查内置模板仍为空白。没有自动安装全部依赖或无人值守登录机制。
