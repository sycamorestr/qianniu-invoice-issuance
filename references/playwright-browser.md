# 浏览器控制

原生 Edge 保存登录态，Playwright 通过 `connect_over_cdp()` 附着。浏览器启动使用固定非零调试端口，仅监听本机回环地址，不使用 `--remote-debugging-port=0`、`--remote-debugging-pipe` 或 `--enable-automation`。这些参数来自当前平台的实测兼容性，不代表所有网站均有相同风控行为。

## 初始化与日常连接

首次准备专用配置并启动：

```powershell
& $python "$skill/scripts/playwright_controller.py" --config $config start
```

`start()` 可以启动浏览器，并一次传入三个业务 URL。控制器等待原生启动页完成初始导航，避免 CDP 刚就绪、页面尚未出现时重复补页。若跳转登录页，保留浏览器供人工登录。日常执行直接使用 `run_online.py`，不依次运行所有诊断命令。

日常 `PlaywrightAdapterRunner` 使用 `connect(open_missing=True)`：只附着归属正确的已有专用浏览器，复用现有发票、订单、票聚角色页，按配置 URL 自动补齐缺失角色一次。缺页恢复属于正常任务，无需请求用户手动开页。

默认 `connect()` 以及 CLI 的 `status`、`register`、`check-login` 是只诊断连接，不启动进程也不补页。诊断命令报告缺页，不意味着日常任务必须要求人工开页。

```powershell
& $python "$skill/scripts/playwright_controller.py" --config $config status
```

## 页面与身份边界

三个角色为 `invoice`、`orders`、`goods`。票聚商品数据位于 goods 页内的跨域 iframe。控制器保存真实 Page 对象，适配器在一个后台事件循环中复用连接，接口采集无需标签页切到前台。

- 启动时缺页只恢复一次，不重复打开已有页，不关闭空白页或用户无关标签，不循环增加页面。
- 登录跳转、验证码或权限问题不能当缺页处理。页面存在、URL 无 login 只是定位线索，正式采集仍须正向核对店铺、公司和租户标识。
- 已登记页面中途失效时，在同一个 context 找回符合角色的现有页；找不到则 `page_missing`，停止并保留检查点。
- 订单详情仅在必要时复用订单页，读取结束恢复列表；不逐单新开详情标签。
- 浏览器断连返回 `browser_disconnected`，不循环启动浏览器或切换其他 Profile。

常见错误包括 `login_required` / `auth_required`、`context_missing`、`context_changed` / `context_mismatch`、`page_missing`、`profile_locked`。按错误码报告原因；解决后恢复原任务。详细接口证据见[页面接入](page-integration.md)。

## 配置与多店

配置示例在 `assets/browser-config.example.json`，`schema_version=1`。`user_data_dir` 是浏览器用户数据根目录；`profile_directory` 默认为 `Default`；三个 `browser_sessions` 定义固定角色 URL。相对文件路径以配置所在目录为基准，未实现环境变量字符串展开，不能把 `%LOCALAPPDATA%` 等字面量直接写成路径。

`download_dir` 可省略或包含 `{profile_directory}`；`executable_path` 指定浏览器程序。现有 `download_root`、`browser_executable` 及嵌套 `playwright` 设置仍可归一读取。具体参数见 CLI `--help`。

多店后台常驻建议每店独立配置、`user_data_dir`、固定非零端口、下载及输出目录。一个用户数据根目录下的多个 Chromium Profile 共用根进程，不能当成已验证的并行隔离方案。每次运行仍核对店铺和开票主体。

同一 `output-root` 的作业目前串行互斥；默认作业目录虽然含店铺标识，并不意味着已实现多店调度。多店并行、隔夜会话、异常断网分别需要真实验收。

## 锁与关闭

先获取输出作业锁，再连接浏览器并获取整个用户数据根目录的 `.qianniu-browser.lock`。`FileMutex` 在 Windows 使用字节锁，其他系统使用 flock，持有文件句柄直到结束。不同 Profile 也互斥；进程退出由操作系统释放锁。锁文件保留，其存在不是占用证明，不能删除文件解锁。

`close()` 只释放控制连接及锁，保留 Edge、页面和本地 runtime，便于下次附着。实例 `stop()` 只允许终止该实例启动且仍拥有的浏览器；日常任务不调用它。

本地 runtime 保存进程归属及连接信息；状态、错误和公开回执不暴露调试端口或 WebSocket 地址。浏览器数据、runtime、配置和业务输出均留在本机，不随仓库分发。

## 恢复与验证

`resume` 重新查询两站身份，比较原日期、店铺、主体及环境，校验输入和成功检查点哈希，再继续未完成阶段。端口连通、页面枚举成功不等于登录有效；本地发布失败不能重复成功业务请求。事务细节见[输入输出与运行](input-output-contract.md#原子发布和恢复)。

离线测试覆盖启动/连接区分、一次性缺页恢复、页面复用、认证跳转、锁竞争、进程退出释放及错误分类。在线验收应记录真实 CDP target ID、命令墙钟时间、首次失败及恢复次数；随机角色标识不能证明标签页相同。顺序重连成功、异常断网恢复和隔夜会话是不同的验收项。
