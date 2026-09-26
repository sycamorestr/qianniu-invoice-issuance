# 浏览器控制

原生 Edge 保存登录态，Playwright 通过 `connect_over_cdp()` 附着。浏览器启动使用固定非零调试端口，仅监听本机回环地址，不使用 `--remote-debugging-port=0`、`--remote-debugging-pipe` 或 `--enable-automation`。这些参数来自当前平台的实测兼容性，不代表所有网站均有相同风控行为。

## 初始化与日常连接

首次准备专用配置并启动：

```powershell
& $python "$skill/scripts/playwright_controller.py" --config $config start
```

`start()` 可以启动浏览器，并一次传入该配置的业务 URL；千牛独立环境两个、共享票聚一个、旧单浏览器三个。启动参数包含 `--no-first-run` 和 `--no-default-browser-check`，减少新环境的欢迎页。控制器等待原生启动页完成初始导航，避免 CDP 刚就绪、页面尚未出现时重复补页。若跳转登录页，保留浏览器供人工登录；同时识别 `loginmyseller.taobao.com` 和 `jstlogin.erp321.com`。日常执行直接使用 `run_online.py`，不依次运行所有诊断命令。

日常 `PlaywrightAdapterRunner` 使用 `connect(open_missing=True)`：只附着归属正确的已有专用浏览器，复用该配置包含的角色页，按配置 URL 自动补齐缺失角色一次。缺页恢复属于正常任务，无需请求用户手动开页。

默认 `connect()` 以及 CLI 的 `status`、`register`、`check-login` 是只诊断连接，不启动进程也不补页。诊断命令报告缺页，不意味着日常任务必须要求人工开页。

```powershell
& $python "$skill/scripts/playwright_controller.py" --config $config status
```

## 页面与身份边界

合法角色为 `invoice`、`orders`、`goods`；控制器允许非空角色子集，未知角色或空配置被拒绝。票聚商品数据位于 goods 页内的跨域 iframe。控制器保存真实 Page 对象，适配器在一个后台事件循环中复用连接，接口采集无需标签页切到前台。

不传 `jst_config_path` 时，适配器要求同一配置包含三个角色。传入时，千牛配置必须恰好包含 `invoice/orders`，共享票聚配置必须恰好包含 `goods`；两者不能使用同一 `user_data_dir`。`run_online.py` 对应参数为 `--jst-browser-config`。

双配置按规范化后的数据目录排序连接，避免不同任务以相反次序获取锁。一个事件循环维护两个控制器，按 site 分发 context/query/detail；合并页面回执仍使用 invoice/orders/goods 键。第二个连接失败或超时会清理所有已建立连接并释放锁，保留原生浏览器。异常带 `site=qianniu/jst`，供批次判断故障影响范围。

- 启动时缺页只恢复一次，不重复打开已有页，不关闭空白页或用户无关标签，不循环增加页面。
- 登录跳转、验证码或权限问题不能当缺页处理。页面存在、URL 无 login 只是定位线索，正式采集仍须正向核对店铺、公司和租户标识。
- 已登记页面中途失效时，在同一个 context 找回符合角色的现有页；找不到则 `page_missing`，停止并保留检查点。
- 订单详情仅在必要时复用订单页，读取结束恢复列表；不逐单新开详情标签。
- 浏览器断连返回 `browser_disconnected`，不循环启动浏览器或切换其他 Profile。

常见错误包括 `login_required` / `auth_required`、`context_missing`、`context_changed` / `context_mismatch`、`page_missing`、`profile_locked`。按错误码报告原因；解决后恢复原任务。详细接口证据见[页面接入](page-integration.md)。

## 配置与多店

单浏览器配置示例在 `assets/browser-config.example.json`，`schema_version=1`。`user_data_dir` 是浏览器用户数据根目录；`profile_directory` 默认为 `Default`；`browser_sessions` 定义本浏览器角色 URL。相对文件路径以配置所在目录为基准，未实现环境变量字符串展开，不能把 `%LOCALAPPDATA%` 等字面量直接写成路径。

`download_dir` 可省略或包含 `{profile_directory}`；`executable_path` 指定浏览器程序。现有 `download_root`、`browser_executable` 及嵌套 `playwright` 设置仍可归一读取。具体参数见 CLI `--help`。

共享票聚布局为每店一套千牛配置、独立 `user_data_dir`，另配一套票聚浏览器。9 店对应 10 个数据根目录；各自固定非零端口和下载目录。这里的“独立 Profile”指独立用户数据根目录，通常都使用 `Default` 子目录，不是同一根目录下的 `Profile 1/2/...`。每次运行仍核对当前店铺和共同票聚主体。

`manage_browsers.py` 创建私有环境并显式启动所选浏览器；`status` 只检查连接/角色，不证明认证。`run_batch.py` 按店串行调度，不并发占用共享票聚。完整初始化、启动和运行命令见[多店配置与运行](multi-shop.md)。

## 锁与关闭

先获取输出作业锁，再连接浏览器并获取整个用户数据根目录的 `.qianniu-browser.lock`。双浏览器时，两把锁都覆盖整个单店任务，包含本地生成阶段，不在每次票聚请求后释放。`FileMutex` 在 Windows 使用字节锁，其他系统使用 flock，持有文件句柄直到结束。不同 Profile 也互斥；进程退出由操作系统释放锁。锁文件保留，其存在不是占用证明，不能删除文件解锁。

`close()` 只释放控制连接及锁，保留 Edge、页面和本地 runtime，便于下次附着。实例 `stop()` 只允许终止该实例启动且仍拥有的浏览器；日常任务不调用它。

本地 runtime 保存进程归属及连接信息；状态、错误和公开回执不暴露调试端口或 WebSocket 地址。浏览器数据、runtime、配置和业务输出均留在本机，不随仓库分发。

## 恢复与验证

`resume` 重新查询两站身份，比较原日期、店铺、主体及环境，校验输入和成功检查点哈希，再继续未完成阶段。端口连通、页面枚举成功不等于登录有效；本地发布失败不能重复成功业务请求。事务细节见[输入输出与运行](input-output-contract.md#原子发布和恢复)。

离线测试覆盖启动/连接区分、角色子集不误开另一站、跨浏览器路由、连接失败/超时清理、一次性缺页恢复、共享锁竞争及错误分类。在线验收应记录真实 CDP target ID、命令墙钟时间、首次失败及恢复次数；随机角色标识不能证明标签页相同。首版不能凭这些测试宣称真实 9 店采集已经验收；顺序重连、异常断网恢复、隔夜会话和常驻浏览器资源容量也分别需要验证。
