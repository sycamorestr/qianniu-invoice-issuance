(async () => {
  const input = __INPUT__ || {};
  const text = value => String(value ?? '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  let stopRequested = false;
  const activeControllers = new Set();
  const stop = () => { stopRequested = true; for (const controller of activeControllers) controller.abort(); };
  globalThis.addEventListener?.('qianniu-invoice-stop', stop);
  try {
  const REQUEST_INTERVAL_MS = 5000;
  const verificationPattern = /_____tmd_____\/punish|FAIL_SYS_USER_VALIDATE|RGV587_ERROR|滑块验证|拖动滑块|请完成验证|安全验证|访问验证|验证码|访问受限|(?:访问|请求|操作|调用)过于频繁/i;
  function challengeVisible() {
    if (stopRequested || globalThis.__qianniuInvoiceStopRequested === true) { stopRequested = true; return true; }
    if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function') return false;
    return [...document.querySelectorAll('iframe,.nc-container,[id^="nc_"][id$="_wrapper"],#baxia-dialog-content')].some(node => {
      const rect = node.getBoundingClientRect?.();
      if (!rect || rect.width <= 0 || rect.height <= 0) return false;
      const style = typeof getComputedStyle === 'function' ? getComputedStyle(node) : null;
      if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
      return String(node.tagName || '').toLowerCase() !== 'iframe' ||
        verificationPattern.test([node.src, node.title, node.getAttribute?.('src')].join(' '));
    });
  }
  const request = async url => {
    const controller = new AbortController();
    activeControllers.add(controller);
    let timer = null, interrupted = false;
    const guard = () => { if (challengeVisible()) { interrupted = true; controller.abort(); } };
    guard();
    const watcher = setInterval(guard, 500);
    try {
      if (interrupted) throw Error('verification_required');
      await new Promise(resolve => setTimeout(resolve, REQUEST_INTERVAL_MS));
      guard(); if (interrupted) throw Error('verification_required');
      timer = setTimeout(() => controller.abort(), 30000);
      const response = await fetch(url, {credentials: 'include', signal: controller.signal});
      guard(); if (interrupted) throw Error('verification_required');
      if (verificationPattern.test(String(response.url || ''))) throw Error('verification_required');
      if (response.status === 401 || response.status === 403 || /login|passport/i.test(response.url)) throw Error('login_required');
      if (response.status === 429) throw Error('rate_limited');
      const content = await response.text();
      guard(); if (interrupted) throw Error('verification_required');
      let body; try { body = JSON.parse(content); } catch (_) {}
      const html = /<(?:!doctype|html|head|body|script)[\s>]/i.test(content);
      const diagnostic = body && typeof body === 'object' ?
        [body.message, body.msg, body.error, body.errorMsg, body.ret, body.url, body.redirectUrl,
          body.data?.url, body.data?.redirectUrl].filter(value => value != null).join(' ') : content;
      if (verificationPattern.test(diagnostic) || (html && verificationPattern.test(content))) throw Error('verification_required');
      if ((html && /login|passport|请先登录|重新登录/i.test(content)) ||
          /未登录|登录失效|请先登录|请重新登录|登录超时|login required|session expired|token expired/i.test(diagnostic) ||
          body?.isLogin === false || body?.isLogin === 0 || [401, 403].includes(Number(body?.code))) throw Error('login_required');
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      if (body === undefined) throw Error('invalid_json_response');
      return body;
    } catch (error) {
      guard();
      if (interrupted) throw Error('verification_required');
      throw error;
    } finally { if (timer !== null) clearTimeout(timer); clearInterval(watcher); controller.abort(); activeControllers.delete(controller); }
  };
  const context = await request('https://einvoice.taobao.com/api/context');
  const contextData = {...(context?.data || {}), ...(context || {})};
  const loggedIn = [true, 1, '1', 'true'].includes(contextData.isLogin);
  if (!loggedIn) throw Error('login_required');
  const shops = await request('https://einvoice.taobao.com/api/shops');
  const shopsData = shops?.data || shops || {};
  const observedStore = text(shopsData.userNick || shopsData.shopName || contextData.shopName || contextData.store);
  if (!observedStore) throw Error('context_missing_store');
  const accountNick = text(contextData.realNick);
  const expectedAccount = input.expected_account;
  if (expectedAccount !== undefined) {
    if (!accountNick) throw Error('context_missing_account');
    if (String(expectedAccount) !== accountNick) throw Error('context_changed_account');
  }
  if (input.expected_observed_store !== undefined && String(input.expected_observed_store) !== observedStore)
    throw Error('context_changed_store');
  if (input.expected_account_nick !== undefined && String(input.expected_account_nick) !== accountNick)
    throw Error('context_changed_account');
  const resourceAgentIds = performance.getEntriesByType('resource').map(entry => String(entry.name || ''))
    .map(name => { try { return new URL(name); } catch (_) { return null; } })
    .filter(url => url && url.hostname === 'einvoice.taobao.com' && url.searchParams.has('agentId'))
    .map(url => text(url.searchParams.get('agentId'))).filter(Boolean);
  const agentNode = document.querySelector('[name="agentId"],[name="agent_id"],[data-agent-id],[data-agentid]');
  const domAgentIds = [agentNode?.value, agentNode?.getAttribute?.('data-agent-id'),
    agentNode?.getAttribute?.('data-agentid')].map(text).filter(Boolean);
  // An empty input field is not identity evidence and must not hide the
  // resource value. Conflicting nonempty evidence is never resolved by order.
  const observedAgentIds = [...new Set([...domAgentIds, ...resourceAgentIds])];
  if (observedAgentIds.length > 1) throw Error('context_ambiguous_agent_id');
  const explicitAgentIds = [...new Set([input.agent_id, input.agentId].map(text).filter(Boolean))];
  if (explicitAgentIds.length > 1 || (explicitAgentIds.length && observedAgentIds.length &&
      explicitAgentIds[0] !== observedAgentIds[0])) throw Error('context_changed_agent_id');
  const agentId = explicitAgentIds[0] || observedAgentIds[0];
  if (!agentId) throw Error('context_missing_agent_id');
  const expected = input.expected_store ?? input.expectedStore;
  if (expected !== undefined && String(expected) !== observedStore && expectedAccount === undefined)
    throw Error('context_changed_store');
  // Workbench names may be friendly labels. A different label is accepted
  // only after the explicitly configured full login account matched realNick.
  const store = expected === undefined ? observedStore : String(expected);
  return {isLogin: true, is_login: true, store, observed_store: observedStore,
    account_nick: accountNick, agentId, agent_id: agentId,
    invoice_url: location.href, checked_at: new Date().toISOString()};
  } finally { globalThis.removeEventListener?.('qianniu-invoice-stop', stop); }
})()
