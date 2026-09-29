# 打包与企微推送

## 配置一次，任务结束后自动发送

将 `assets/notifications.example.json` 复制到技能根目录，命名为 `notifications.json`，填写自己的 Webhook 并启用推送：

```json
{
  "schema_version": 1,
  "wecom": {
    "enabled": true,
    "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的机器人密钥"
  }
}
```

单店、多店及独立补发 CLI 默认读取相对于脚本所在技能根目录的 `notifications.json`，不依赖启动命令时的工作目录。读取优先级为：显式参数 → 技能根目录配置 → 旧位置兼容。单店和多店的显式参数为 `--notification-config <私有配置路径>`，独立补发为 `--config`。

仅当技能内配置不存在时，多店才兼容 `--registry` 文件旁的 `notifications.json`，单店才兼容实际输出根目录上级的旧配置，包括恢复原作业时。独立补发不搜索旧工作目录。已有配置即使 `enabled=false`，也不再回退别的机器人；显式指定的路径不存在时报告错误。无配置、`enabled=false` 或 `--no-notify` 时只打包，不发消息。不要在命令行传 Webhook 本身。

本机迁移时复制整个技能目录，保留其中的真实 `notifications.json`。公开 GitHub 版本只提交 `assets/notifications.example.json`：保留配置结构，但 `enabled=false`、`webhook_url=""`。真实文件由 `.gitignore` 排除；发布前检查待提交文件，不能使用强制添加绕过忽略规则。同步或更新代码时保留本机已有配置，不覆盖为空值。Webhook 不进入运行回执、异常文本、业务 ZIP 或公开文件。

首次从公开仓库安装时，只在配置尚不存在时复制模板：

```powershell
if (-not (Test-Path -LiteralPath "$skill/notifications.json")) {
  Copy-Item "$skill/assets/notifications.example.json" "$skill/notifications.json"
}
```

## ZIP 的范围

`scripts/invoice_delivery.py` 在业务控制连接与锁释放后运行，独立写入作业目录的 `delivery/`。不修改现有业务报告、检查点及文件哈希。

- 单店：正式税局模板（有可生成申请时）、完整通用模板（平台实际提供时）、异常清单、简明结果报告。
- 多店：上述文件按店铺分目录，另有批次结果汇总；失败和未执行店铺在报告中明确列出，不混入未验证的税局模板。
- 无申请、全负数排除、全暂缓：保留真实存在的原件、异常和结果报告，不制造税局空票。

打包前核对成功产物哈希。只读白名单交付文件，不递归压缩运行目录。原始接口快照、本机配置、浏览器 Profile、Cookie、Webhook、调试连接信息和恢复检查点不入包。通用模板包含原始业务记录，保持字节一致；结果报告采用字段白名单，不把含本机绝对路径的原 `run.json` 整份外发。

多店完成、部分完成或停止后统一打包一次，报告区分真实终态；不由每个子店再发送一份。`--plan-only` 不自动触发交付。离线重放只打包，需明确使用独立发送命令才外发。

## 上传、回执与补发

通过[企微官方群机器人接口](https://developer.work.weixin.qq.com/document/path/91770)先上传普通文件，取得 `media_id`，再发送 `msgtype=file` 消息。群内收到的是 ZIP 文件附件。实现只使用 Python 标准库，不需要浏览器或新的第三方依赖。

官方普通文件大小上限为 20 MB，上传后的 `media_id` 有效期为三天。超限时保留本地 ZIP 并明确提示，不改成外链、不丢文件、不自动拆分成多条群消息。

独立回执使用压缩包内容哈希和目标摘要判重。相同内容已成功发送则直接复用回执；后续恢复产生新结果时视为新包。上传或发送明确失败时保留业务文件、ZIP 和错误类别，单独补发：

```powershell
& $python "$skill/scripts/invoice_delivery.py" `
  --run-dir "$work/outputs/原作业或批次目录"
```

默认复用技能根目录配置；需要覆盖时追加 `--config <配置路径>`。只检查并打包、不外发：同一命令追加 `--package-only`。底层 Python `deliver_result(..., config_path=None)` 仍只打包，默认配置解析仅由 CLI 执行。

发送超时、进程在发送中中断或服务器回复无法判断时，回执标记为未知。此时企微可能已经收到，禁止自动循环重发。先核对群消息；用户确认未收到后，才对独立命令追加 `--retry-unknown`。此选项不重跑订单或票聚采集。

本地业务状态和推送状态独立。业务成功但通知失败/未知时，CLI 会单独提示并返回通知错误退出码；不能将这个退出码解释成税局模板生成失败。
