(async () => {
  const input = __INPUT__;
  const {operation} = input;
  const timestamp = () => new Date().toISOString();
  let stopRequested = false;
  const activeControllers = new Set();
  const stop = () => { stopRequested = true; for (const controller of activeControllers) controller.abort(); };
  globalThis.addEventListener?.('qianniu-invoice-stop', stop);
  try {
  if (!['approval-status', 'approve'].includes(operation)) throw Error('unknown_approval_operation');
  if (!['myseller.taobao.com', 'einvoice.taobao.com'].includes(location.hostname)) throw Error('wrong_invoice_page');
  const validText = value => typeof value === 'string' && value.length > 0 && value.trim() === value;
  const applications = input.applications;
  if (!Array.isArray(applications) ||
      applications.some(row => !row || typeof row !== 'object' || Array.isArray(row) ||
        Object.keys(row).sort().join(',') !== 'serialNo,tid' || !validText(row.serialNo) || !validText(row.tid)) ||
      new Set(applications.map(row => row.serialNo)).size !== applications.length)
    throw Error('invalid_approval_applications');
  // Keep the same date semantics as read_qianniu.js. This new mutation only
  // operates on new jobs with the explicitly frozen started countdown filter.
  const validDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(value + 'T00:00:00Z')) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  const scope = input.query_scope;
  if (!scope || typeof scope !== 'object' || Array.isArray(scope) || scope.countdown !== 'started')
    throw Error('invalid_query_scope');
  const keysAre = keys => Object.keys(scope).sort().join(',') === [...keys, 'countdown'].sort().join(',');
  let startTime = input.date, endTime = input.date;
  if (scope.mode === 'all_pending') {
    if (input.date != null || !validDate(scope.start_date) || !validDate(scope.end_date) ||
        !keysAre(['mode', 'start_date', 'end_date'])) throw Error('invalid_query_scope');
    const end = new Date(scope.end_date + 'T00:00:00Z');
    const first = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth() - 2, 1));
    const days = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
    first.setUTCDate(Math.min(end.getUTCDate(), days));
    if (first.toISOString().slice(0, 10) !== scope.start_date) throw Error('invalid_query_scope');
    startTime = scope.start_date; endTime = scope.end_date;
  } else if (scope.mode === 'date') {
    if (!keysAre(['mode', 'date']) || scope.date !== input.date) throw Error('invalid_query_scope');
  } else throw Error('invalid_query_scope');
  if (!validDate(startTime) || !validDate(endTime) || !/^\d+$/.test(String(input.agentId ?? '')))
    throw Error('date_and_current_agentId_required');
  const result = {operation, date: scope.mode === 'all_pending' ? null : input.date, query_scope: scope};
  // The adapter verifies the captured shop/account immediately before running
  // this file. Pin the account again with the live role evidence before POST.
  if (!validText(input.expected_observed_store) || !validText(input.expected_account_nick))
    throw Error('approval_identity_required');
  if (Object.hasOwn(input, 'repeatCheck') || Object.hasOwn(input, 'autoCreate'))
    throw Error('invalid_approval_options');
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
  async function request(url, options = {}, mutation = false) {
    // Never retry a mutation: a dropped response can follow a successful POST.
    const controller = new AbortController();
    activeControllers.add(controller);
    let timer = null, interrupted = false, sent = false;
    const guard = () => { if (challengeVisible()) { interrupted = true; controller.abort(); } };
    guard();
    const watcher = setInterval(guard, 500);
    try {
      if (interrupted) throw Error('verification_required');
      await new Promise(resolve => setTimeout(resolve, REQUEST_INTERVAL_MS));
      guard(); if (interrupted) throw Error('verification_required');
      timer = setTimeout(() => controller.abort(), 30000);
      sent = true;
      const response = await fetch(url, {...options, credentials: 'include', signal: controller.signal});
      guard(); if (interrupted) throw Error('verification_required');
      if (verificationPattern.test(String(response.url || ''))) throw Error('verification_required');
      if (response.status === 429) throw Error('rate_limited');
      if (response.status === 401 || /login|passport/i.test(response.url)) throw Error('login_required');
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
          body?.isLogin === false || body?.isLogin === 0 || Number(body?.code) === 401) throw Error('login_required');
      if (response.status === 403 || Number(body?.code) === 403) throw Error('permission_required');
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      if (body === undefined) throw Error('invalid_json_response');
      return body;
    } catch (error) {
      guard();
      // After dispatch even a login/verification response cannot prove that
      // the POST had no effect. Recovery must only reconcile its saved targets.
      if (mutation && sent) throw Error('approval_unknown' + (interrupted || error.message === 'verification_required' ? ': verification_required' : ''));
      if (interrupted) throw Error('verification_required');
      throw error;
    } finally { if (timer !== null) clearTimeout(timer); clearInterval(watcher); controller.abort(); activeControllers.delete(controller); }
  }
  if (operation === 'approve') {
    if (!applications.length) return {...result, applications: [], code: 200, message: 'empty', approved_at: timestamp()};
    const context = await request('https://einvoice.taobao.com/api/context');
    const current = {...(context?.data || {}), ...(context || {})};
    if (![true, 1, '1', 'true'].includes(current.isLogin)) throw Error('login_required');
    const account = String(current.realNick ?? '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    if (account !== input.expected_account_nick) throw Error('context_changed_account');
    // Observed UI: VISITOR omits autoCreate and goes to manual invoice entry.
    // In the non-visitor modal, mode 0 means SYSTEM AUTOMATIC ISSUANCE and
    // mode 1 explicitly means manual upload in the pending invoice entry list.
    const roles = current.realBizRoles;
    const knownRoles = ['MASTER', 'SUB_ADMIN', 'STORE_ADMIN', 'STORE_OPERATOR',
      'ALI_EINVOICE_EA_JXS_MASTER', 'ALI_EINVOICE_EA_JXS_USER'];
    if (!Array.isArray(roles) || (!roles.includes('VISITOR') && !roles.some(role => knownRoles.includes(role))))
      throw Error('approval_role_unsupported');
    const agreeBody = {agreeList: applications, repeatCheck: true,
      ...(!roles.includes('VISITOR') ? {autoCreate: 1} : {})};
    const body = await request('https://einvoice.taobao.com/api/invoice/apply/agree', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(agreeBody)
    }, true);
    if (body?.code === 1020) throw Error('approval_duplicate_check');
    if (body?.code === 1004) throw Error('permission_required');
    if (body?.code !== 200) throw Error('approval_rejected');
    // Acceptance is not completion: the coordinator must query approval-status.
    return {...result, applications, code: 200, message: String(body.message ?? ''), approved_at: timestamp()};
  }
  if (!applications.length) return {...result, applications: [], checked_at: timestamp()};
  const stable = value => JSON.stringify([value.serialNo, value.tid, value.applyStatus]);
  const targetedQueries = [];
  async function list(agreed, tid = null) {
    const rows = new Map(); let total = null, rawRowCount = 0, confirmingShortTail = false;
    for (let pageNo = 0; pageNo < 10000; pageNo++) {
      const common = {startTime, endTime, agentId: String(input.agentId), pageSize: '20', pageNo: String(pageNo)};
      if (!agreed) Object.assign(common, {applyListType: '0', rightsRemainTime: '100'});
      if (tid !== null) common.tid = tid;
      // Already agreed rows can change their countdown. Their exact serialNo
      // and tid must be verified without applying the pending countdown filter.
      const body = await request('https://einvoice.taobao.com/api/qianniu/invoice/list/apply' +
        (agreed ? '/agreed' : '') + '?' + new URLSearchParams(common));
      if (body?.code === 1004) throw Error('permission_required');
      if (body?.code === 200 && body.total === 0 && body.data == null) body.data = [];
      // A full final page is followed by an explicit no-data response that
      // retains the nonzero total. The agreed endpoint can count repeated
      // rows in that total; keep pending's unique-row requirement unchanged.
      const explicitTerminal = body?.code === 200 && pageNo > 0 && !Object.hasOwn(body, 'data') &&
        body.message === '无数据' && total === body.total &&
        (rows.size >= total || (agreed && rawRowCount >= total));
      if (explicitTerminal) body.data = [];
      if (body?.code !== 200 || !Array.isArray(body.data) || !Number.isInteger(body.total) || body.total < 0)
        throw Error('invalid_application_response');
      if (total !== null && total !== body.total) throw Error('applications_changed_during_pagination');
      total = body.total;
      let added = 0;
      for (const row of body.data) {
        // Refuse numeric long order IDs; converting a rounded number to a
        // string cannot prove that it is the exact exported order identifier.
        if (!validText(row?.serialNo) || !validText(row?.tid) || !Number.isInteger(row.applyStatus))
          throw Error('invalid_approval_application_response');
        if (tid !== null && row.tid !== tid) throw Error('approval_target_conflict');
        const normalized = {serialNo: row.serialNo, tid: row.tid, applyStatus: row.applyStatus};
        if (rows.has(row.serialNo)) {
          if (stable(rows.get(row.serialNo)) !== stable(normalized)) throw Error('approval_target_conflict');
          continue;
        }
        rows.set(row.serialNo, normalized); added++;
      }
      if (body.data.length && !added) throw Error('duplicate_application_page');
      rawRowCount += body.data.length;
      if (tid !== null) targetedQueries.push({list: agreed ? 'agreed' : 'pending', query: {...common},
        total, raw_row_count: body.data.length, explicit_terminal: explicitTerminal,
        rows: body.data.map(({serialNo, tid, applyStatus}) => ({serialNo, tid, applyStatus})), checked_at: timestamp()});
      if (body.data.length < 20) {
        if (rows.size < total && !explicitTerminal) {
          // Equal overlapping rows can fill the raw total on a short agreed
          // page. Confirm one real terminal page before accepting that count.
          if (agreed && !confirmingShortTail && body.data.length > 0 && rawRowCount >= total) {
            confirmingShortTail = true;
            continue;
          }
          throw Error('incomplete_application_pagination');
        }
        return rows;
      }
      if (confirmingShortTail) throw Error('incomplete_application_pagination');
    }
    throw Error('application_pagination_limit');
  }
  const pending = await list(false), agreed = await list(true);
  const stateFor = target => {
    const before = pending.get(target.serialNo), after = agreed.get(target.serialNo);
    if ([before, after].some(row => row && row.tid !== target.tid)) throw Error('approval_target_conflict');
    if (before && after && before.applyStatus !== after.applyStatus) throw Error('approval_target_conflict');
    let status = 'unknown';
    if (before?.applyStatus === 1 && !after) status = 'pending';
    if (after?.applyStatus === 2 && (!before || before.applyStatus === 2)) status = 'agreed';
    return {...target, status};
  };
  // Fail existing conflicts before supplementing only genuinely absent targets.
  applications.forEach(stateFor);
  const missingTids = [...new Set(applications.filter(target =>
    !pending.has(target.serialNo) && !agreed.has(target.serialNo)).map(target => target.tid))];
  const merge = (destination, supplement) => {
    for (const [serial, row] of supplement) {
      if (destination.has(serial) && stable(destination.get(serial)) !== stable(row))
        throw Error('approval_target_conflict');
      destination.set(serial, row);
    }
  };
  for (const tid of missingTids) {
    // One complete pair of read-only queries per missing order; never retry or
    // widen the frozen date/countdown scope, and never infer a missing status.
    merge(pending, await list(false, tid));
    merge(agreed, await list(true, tid));
    applications.forEach(stateFor);
  }
  return {...result, applications: applications.map(stateFor), checked_at: timestamp(),
    ...(targetedQueries.length ? {targeted_reconciliation: {
      reason: 'absent_from_both_complete_lists', query_scope: scope,
      expected_identity: {observed_store: input.expected_observed_store,
        account_nick: input.expected_account_nick, agentId: String(input.agentId)},
      order_ids: missingTids, queries: targetedQueries}} : {})};
  } finally { globalThis.removeEventListener?.('qianniu-invoice-stop', stop); }
})()
