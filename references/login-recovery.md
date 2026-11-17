# 千牛登录失效兜底

正常入口、查询范围、同意申请、模板生成、通知与断点恢复流程保持原样。登录有效时不读飞书；只有正式在线任务检测到千牛 `login_required` / `auth_required`，才在原店铺已核验且持锁的 BrowserContext 内尝试一次 Cookie 恢复。诊断命令、离线重放、票聚认证问题不触发。

飞书读取共用 `$CODEX_HOME/skills/_shared/qianniu_auth/feishu_cookie_recovery.py`，未设置 `CODEX_HOME` 时使用用户目录 `.codex`。配置说明见[共享飞书 Cookie 配置](../../_shared/qianniu_auth/configuration.md)。私有配置由共享模块管理；不把 App Secret、访问令牌、Cookie 写进技能正文、项目配置、命令行、业务输入、报告或 ZIP。按本次 `expected_store` 精确匹配店铺；不能猜别名、选第一行或借用其他店铺 Cookie。有完整子账号约束时同时传 `expected_account` / 原 `account_nick`。空、重复、缺失、过期或不合法资料交给共享模块拒绝，保留原任务待人工修复。

只向当前千牛 context 添加共享模块验证过的 Cookie，不清空整个 Cookie 容器、不创建 Profile、不换端口、不重启浏览器、不改登记身份。复用原 invoice/orders 页，按原配置 URL 各导航一次，缺少角色才在原 context 补页。票聚页不注入、不导航。启动阶段完成页恢复后，仍走原先两站身份门槛；运行中恢复则重新核验千牛店铺、完整账号、agentId，以及票聚主体、coid、uid，所有已有观察值必须与原值一致。

可继续的仅为失败的只读 `context/applications/export/orders/detail/approval-status` 调用，每个单店运行实例合计最多一次恢复。原成功文件、检查点、请求范围和哈希不重建。恢复失败或主体发生变化，不发布新的业务快照。`approve` 不进入自动重试，即使错误表现为登录失效也按原记录停止；发送结果不明仍只能走原作业只读核对，不能重发、拆批重发或借恢复重新同意。

验证码、滑块、HTTP 429、权限不足、主体不符、页面未就绪、断连均不触发 Cookie 恢复。HTTP 403 只有同时具备明确登录失效证据才作为认证失效；普通 403 保持权限故障。注入前再次检查千牛页面可见验证，出现验证立即停止。资料不可用或一次恢复后仍失效时，更新飞书 Cookie / 人工完成登录后使用原作业 `--resume`，不得通过新建业务任务绕过不明写入。

离线行为测试见 `scripts/test_cookie_recovery.py`，覆盖一次恢复、原身份复核、票聚隔离、写请求不重放、验证码不取 Cookie、失败脱敏。离线通过不代表真实登录恢复或线上开票验收。
