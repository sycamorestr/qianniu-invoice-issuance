(async () => {
  const input = __INPUT__ || {};
  const text = value => String(value ?? '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const request = async url => {
    const response = await fetch(url, {credentials: 'include'});
    if (response.status === 401 || response.status === 403 || /login|passport/i.test(response.url)) throw Error('login_required');
    if (!response.ok) throw Error(`HTTP ${response.status}`);
    return response.json();
  };
  const context = await request('https://einvoice.taobao.com/api/context');
  const shops = await request('https://einvoice.taobao.com/api/shops');
  const contextData = {...(context?.data || {}), ...(context || {})};
  const shopsData = shops?.data || shops || {};
  const loggedIn = [true, 1, '1', 'true'].includes(contextData.isLogin);
  if (!loggedIn) throw Error('login_required');
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
  const resourceAgentId = performance.getEntriesByType('resource').map(entry => String(entry.name || ''))
    .map(name => { try { return new URL(name); } catch (_) { return null; } })
    .filter(url => url && url.hostname === 'einvoice.taobao.com' && url.searchParams.has('agentId'))
    .map(url => url.searchParams.get('agentId'))
    .find(value => value !== null && value !== '');
  const agent = input.agent_id ?? input.agentId ??
    document.querySelector('[name="agentId"],[name="agent_id"],[data-agent-id],[data-agentid]')?.value ??
    resourceAgentId;
  const agentId = text(agent);
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
})()
