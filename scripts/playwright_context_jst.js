(async () => {
  const body = String(document.body?.innerText || '');
  if (/login|passport/i.test(location.href) || /请先登录|登录失效/.test(body)) throw Error('login_required');
  const resources = performance.getEntriesByType('resource').map(entry => String(entry.name || ''));
  const pairs = new Map();
  for (const resource of resources) {
    try {
      const url = new URL(resource);
      if (url.hostname === 'jstweb.cn-hangzhou.log.aliyuncs.com') {
        const coid = url.searchParams.get('co_id');
        const uid = url.searchParams.get('user_id');
        if (coid && uid) pairs.set(`${coid}:${uid}`, {coid, uid});
      }
    } catch (_) {}
  }
  if (pairs.size !== 1) throw Error('context_missing_tenant');
  return {...[...pairs.values()][0], checked_at: new Date().toISOString(),
    frame_url: location.href};
})()
