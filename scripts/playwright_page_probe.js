// Passive page evidence only. Never initiate requests, navigation or DOM changes.
({role = '', tenant_frame = false} = {}) => {
  const text = value => String(value ?? '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      style.visibility !== 'collapse' && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const body = String(document.body?.innerText || '');
  const title = text(document.title);
  const challengeUrl = value => /\/_____tmd_____\/punish(?:[/?#]|$)/i.test(String(value || ''));
  const verification = [];
  if (challengeUrl(location.href)) verification.push('challenge_url');
  if (/验证码拦截|滑块验证|访问验证|安全验证/.test(title)) verification.push('challenge_title');
  if ([...document.querySelectorAll('iframe')].some(el => visible(el) && challengeUrl(el.src)))
    verification.push('challenge_iframe');
  const sliderText = /请.{0,8}拖动滑块|请.{0,8}按住滑块|拖动.{0,8}滑块|向右滑动.{0,8}验证|滑动验证/.test(body);
  const sliderElements = [...document.querySelectorAll(
    '#nc_1_n1z,#nc_2_n1z,[id^="nc_"][id$="_n1z"],#nocaptcha,.nc-container,.nc_wrapper,' +
    '#baxia-dialog-content,#baxia-dialog,.baxia-dialog')];
  if (sliderText && sliderElements.some(visible)) verification.push('visible_slider');
  // Plain login links in the seller shell are not a login failure.
  const loginRequired = /请先登录|登录失效|登录已过期|登录超时/.test(body);
  const result = {ready_state: document.readyState, verification, login_required: loginRequired, signals: []};
  if (!role) return result;
  const settled = document.readyState === 'interactive' || document.readyState === 'complete';
  if (!settled || verification.length || loginRequired) return {...result, ready: false};
  const resources = performance.getEntriesByType('resource').map(entry => String(entry.name || ''));
  if (role === 'invoice') {
    const agents = new Set();
    // These are the same supported DOM/resource sources as the context probe.
    for (const element of document.querySelectorAll('[name="agentId"],[name="agent_id"],[data-agent-id],[data-agentid]')) {
      const value = text(element.value);
      if (value) agents.add(value);
    }
    for (const resource of resources) {
      try {
        const url = new URL(resource);
        if (url.hostname === 'einvoice.taobao.com') {
          const agent = text(url.searchParams.get('agentId'));
          if (agent) agents.add(agent);
        }
      } catch (_) {}
    }
    if (agents.size === 1) result.signals.push('unique_agent_id');
    return {...result, ready: agents.size === 1, ambiguous_context: agents.size > 1,
      stable_token: agents.size === 1 ? [...agents][0] : ''};
  }
  if (role === 'goods' && tenant_frame) {
    const pairs = new Set();
    for (const resource of resources) {
      try {
        const url = new URL(resource);
        if (url.hostname === 'jstweb.cn-hangzhou.log.aliyuncs.com') {
          const coid = text(url.searchParams.get('co_id'));
          const uid = text(url.searchParams.get('user_id'));
          if (coid && uid) pairs.add(`${coid}:${uid}`);
        }
      } catch (_) {}
    }
    if (pairs.size === 1) result.signals.push('unique_tenant');
    return {...result, ready: pairs.size === 1, ambiguous_context: pairs.size > 1,
      stable_token: pairs.size === 1 ? [...pairs][0] : ''};
  }
  if (role === 'goods') {
    const values = [...document.querySelectorAll('.companyInfoBox__name')]
      .map(el => text(el.textContent)).filter(Boolean);
    const match = body.match(/([^\n]*?(?:有限公司|公司)(?:\[[^\]]*\])?)/);
    const label = values.length === 1 ? values[0] : text(match?.[1]);
    if (label) result.signals.push('issuer_label');
    return {...result, ready: Boolean(label), stable_token: label};
  }
  if (role === 'orders') {
    const queryControl = [...document.querySelectorAll('input,textarea')].some(element => {
      if (!visible(element) || element.disabled) return false;
      const hint = text([element.placeholder, element.getAttribute('aria-label'),
        element.name, element.id].join(' '));
      return /订单|买家|收货|商品|order|buyer|trade/i.test(hint);
    });
    const queryText = /订单编号|订单号|买家昵称|买家账号|买家名称|批量搜索/.test(body);
    // Empty result lists still contain a working query form. A menu label for
    // 已卖出宝贝 alone does not establish that the embedded app is loaded.
    if (queryControl) result.signals.push('order_query_control');
    if (queryText) result.signals.push('order_query_labels');
    return {...result, ready: document.readyState === 'complete' && queryControl && queryText};
  }
  return {...result, ready: settled};
}
