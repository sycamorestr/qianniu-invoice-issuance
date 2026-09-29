# 浏览器控制

原生 Edge 保存登录态，Playwright 通过 `connect_over_cdp()` 附着。浏览器启动使用固定非零调试端口，仅监听本机回环地址，不使用 `--remote-debugging-port=0`、`--remote-debugging-pipe` 或 `--enable-automation`。这些参数来自当前平台的实测兼容性，不代表所有网站均有相同风控行为。

## 初始化与日常连接

首次准备专用配置并启动：

```powershell
& $python "$skill/scripts/playwright_controller.py" --config $config start
```

`start()` 可以启动浏览器，并一次传入当次控制器配置中的 URL。直接使用工作台配置时通常只有主页；在线开票运行器使用内存补齐后的业务角色配置。启动参数包含 `--no-first-run` 和 `--no-default-browser-check`，减少新环境的欢迎页。控制器等待原生启动页完成初始导航，避免 CDP 刚就绪、页面尚未出现时重复补页。若跳转登录页，保留浏览器供人工登录；同时识别 `loginmyseller.taobao.com` 和 `jstlogin.erp321.com`。日常执行直接使用 `run_online.py`，不依次运行所有诊断命令。

日常 `run_online.py` / `run_batch.py` 默认按需启动。`PlaywrightAdapterRunner` 先复用归属正确的已有专用浏览器；确认环境未运行时，使用既有固定数据目录、Profile 和非零端口正常启动 Edge 一次，再附着。原目录保存的 Cookie、Local Storage、IndexedDB 等仍由浏览器读取，技能不导出或跨店复制登录态。随后复用业务页，按业务 URL 自动补齐缺失角色一次，无需请求用户手工开页。

浏览器工作台与本技能分工：工作台只打开和维护 `https://myseller.taobao.com/` 千牛主页，不负责订单页、发票页。工作台配置可以只含 `home`；本技能在内存中叠加 `invoice/orders` 业务角色和 URL，保留原 `user_data_dir`、Profile、端口、下载目录及配置文件。浏览器只有主页时，运行器复用或补齐业务页一次，不覆盖原主页；不得要求用户先在工作台打开订单或发票页。两者遵守同一目录锁，工作台维护遇到业务任务占用应跳过。技能运行不依赖工作台 HTTP 服务。

若本次只允许使用已运行环境，给在线或批次命令增加 `--connect-only`。它禁止自动启动进程，仍允许正常业务采集和一次性缺页恢复；并不把任务改为只读诊断。原进程存在但端口不通、归属不符或锁被占用时，不能换端口、换目录或重复启动来绕过。

默认 `connect()` 以及 CLI 的 `status`、`register`、`check-login` 是只诊断连接，不启动进程也不补页。诊断命令报告缺页，不意味着日常任务必须要求人工开页。

```powershell
& $python "$skill/scripts/playwright_controller.py" --config $config status
```

## 页面与身份边界

配置兼容工作台 `home` 及业务角色 `invoice`、`orders`、`goods`，未知角色或空配置被拒绝；实际采集分发仍使用 `invoice/orders/goods`。票聚商品数据位于 goods 页内的跨域 iframe。控制器保存真实 Page 对象，适配器在一个后台事件循环中复用连接，接口采集无需标签页切到前台。

不传 `jst_config_path` 时，沿用同一浏览器承担三个业务角色的模式。传入时，千牛原配置可为工作台 `home` 或原 `invoice/orders` 配置，运行时补齐千牛业务角色；共享票聚配置仍须只含 `goods`，两者不能使用同一 `user_data_dir`。`run_online.py` 对应参数为 `--jst-browser-config`。内存补齐只负责千牛业务页，不自动为每店创建独立票聚登录环境。

双配置按规范化后的数据目录排序连接，避免不同任务以相反次序获取锁。一个事件循环维护两个控制器，按 site 分发 context/query/detail；合并页面回执仍使用 invoice/orders/goods 键。第二个连接失败或超时会清理所有已建立连接并释放锁，保留原生浏览器。异常带 `site=qianniu/jst`，供批次判断故障影响范围。

千牛身份核验区分工作台显示名称与平台账号：有清单 `login_username`（单店为 `--expected-account`）时，将已确认的完整子账号与页面 `context.realNick` 精确比较，允许显示名为别名；没有账号时保留原 `store` 精确核验。不能用显示名、账号备注或登录 URL 猜测对应关系。`observed_store/account_nick` 留存原始观察值并在恢复时固定，票聚公司和两站租户身份仍分别核对。该账号不用于填写登录表单。

- 启动时缺页只恢复一次，不重复打开已有页，不关闭空白页或用户无关标签，不循环增加页面。
- 登录跳转、验证码或权限问题不能当缺页处理。页面存在、URL 无 login 只是定位线索，正式采集仍须正向核对店铺、公司和租户标识。
- 已登记页面中途失效时，在同一个 context 找回符合角色的现有页；找不到则 `page_missing`，停止并保留检查点。
- 订单详情仅在必要时复用订单页，前往和成功返回列表前各等待3秒；失败保留当前页面供检查或人工验证，不额外导航。显式恢复时按原页面规则复用或补齐列表；不逐单新开详情标签。
- 采集中途浏览器断连返回 `browser_disconnected`，保留检查点，不循环启动浏览器或切换其他 Profile。修复后恢复原任务；默认恢复入口仍可按原配置启动已关闭环境。

常见错误包括 `login_required` / `auth_required`、`rate_limited`、`context_missing`、`context_changed` / `context_mismatch`、`page_missing`、`profile_locked`。订单HTTP 429的 `rate_limited` 停止整批，不自动继续其他店。按错误码报告原因；解决后恢复原任务。验证码未必有专用错误码，也可能体现为响应格式错误或详情就绪超时，不能循环尝试。详细接口证据见[页面接入](page-integration.md)。

## 配置与多店

单浏览器配置示例在 `assets/browser-config.example.json`，`schema_version=1`。`user_data_dir` 是浏览器用户数据根目录；`profile_directory` 默认为 `Default`；`browser_sessions` 定义本浏览器角色 URL。相对文件路径以配置所在目录为基准，未实现环境变量字符串展开，不能把 `%LOCALAPPDATA%` 等字面量直接写成路径。

`download_dir` 可省略或包含 `{profile_directory}`；`executable_path` 指定浏览器程序。现有 `download_root`、`browser_executable` 及嵌套 `playwright` 设置仍可归一读取。具体参数见 CLI `--help`。

共享票聚布局为每店一套千牛配置、独立 `user_data_dir`，另配一套票聚浏览器。9 店对应 10 个数据根目录；各自固定非零端口和下载目录。这里的“独立 Profile”指独立用户数据根目录，通常都使用 `Default` 子目录，不是同一根目录下的 `Profile 1/2/...`。每次运行仍核对当前店铺和共同票聚主体。

`manage_browsers.py` 可创建私有环境并显式启动所选浏览器；已有工作台环境无需重新初始化。清单加载会合并旁侧 `.browser-workbench-shops.json` 中的新店，保留原目录和配置。`status` 只检查连接/角色，不启动、不补页，也不证明认证。`run_batch.py` 默认按需启动、按店串行调度，不并发占用共享票聚。完整初始化、启动和运行命令见[多店配置与运行](multi-shop.md)。

## 锁与关闭

先获取输出作业锁，再连接浏览器并获取整个用户数据根目录的 `.qianniu-browser.lock`。双浏览器时，两把锁都覆盖整个单店任务，包含本地生成阶段，不在每次票聚请求后释放。`FileMutex` 在 Windows 使用字节锁，其他系统使用 flock，持有文件句柄直到结束。不同 Profile 也互斥；进程退出由操作系统释放锁。锁文件保留，其存在不是占用证明，不能删除文件解锁。

默认在线适配器在获取这把本地作业互斥锁时最多等待 5 秒，以容纳工作台自动保存 Cookie 等操作的短暂占用；低级控制器默认等待 0 秒。等待仅发生在业务工作开始前的锁获取阶段，耗尽仍报 `profile_locked`。原生浏览器 Profile 冲突、登录或身份错误不进入此等待重试，不因此重新连接、重启或重复采集。

`close()` 只释放控制连接及锁，保留 Edge、页面和本地 runtime，便于下次附着。实例 `stop()` 只允许终止该实例启动且仍拥有的浏览器；日常任务不调用它。

本地 runtime 保存进程归属及连接信息；状态、错误和公开回执不暴露调试端口或 WebSocket 地址。浏览器数据、runtime、配置和业务输出均留在本机，不随仓库分发。

## 恢复与验证

单店 `resume` 重新查询两站身份，比较原日期、店铺、主体及环境，校验输入和成功检查点哈希，再继续未完成阶段；批次已完成的店铺经文件校验后直接跳过。新批次冻结所选环境身份，后续新增未选中的有效店铺不影响原批次恢复，旧 v1 批次沿用原有全清单检查。端口连通、页面枚举成功不等于登录有效；本地发布失败不能重复成功业务请求。登录失效或验证码仍需人工处理，正常重开保存过会话的 Profile 不等于保证免登录。事务细节见[输入输出与运行](input-output-contract.md#原子发布和恢复)。

离线测试覆盖启动/连接区分、角色子集不误开另一站、跨浏览器路由、连接失败/超时清理、一次性缺页恢复、共享锁竞争及错误分类。在线验收应记录真实 CDP target ID、命令墙钟时间、首次失败及恢复次数；随机角色标识不能证明标签页相同。首版不能凭这些测试宣称真实 9 店采集已经验收；顺序重连、异常断网恢复、隔夜会话和常驻浏览器资源容量也分别需要验证。
