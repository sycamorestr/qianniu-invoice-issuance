(async () => {
  const input = __INPUT__;
  const {operation} = input;
  const timestamp = () => new Date().toISOString();
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
  async function request(url, options = {}, mutation = false) {
    // Never retry a mutation: a dropped response can follow a successful POST.
    let response;
    try {
      response = await fetch(url, {...options, credentials: 'include', signal: AbortSignal.timeout(30000)});
    } catch (error) {
      if (mutation) throw Error('approval_unknown');
      throw error;
    }
    if (response.status === 401 || response.status === 403 || /login|passport/i.test(response.url)) throw Error('login_required');
    if (response.status === 429) throw Error(mutation ? 'approval_unknown' : 'rate_limited');
    if (!response.ok) throw Error(mutation ? 'approval_unknown' : `HTTP ${response.status}`);
    try { return await response.json(); }
    catch (error) { if (mutation) throw Error('approval_unknown'); throw error; }
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
  async function list(agreed) {
    const rows = new Map(); let total = null;
    for (let pageNo = 0; pageNo < 10000; pageNo++) {
      const common = {startTime, endTime, agentId: String(input.agentId), pageSize: '20', pageNo: String(pageNo)};
      if (!agreed) Object.assign(common, {applyListType: '0', rightsRemainTime: '100'});
      // Already agreed rows can change their countdown. Their exact serialNo
      // and tid must be verified without applying the pending countdown filter.
      const body = await request('https://einvoice.taobao.com/api/qianniu/invoice/list/apply' +
        (agreed ? '/agreed' : '') + '?' + new URLSearchParams(common));
      if (body?.code === 1004) throw Error('permission_required');
      if (body?.code === 200 && body.total === 0 && body.data == null) body.data = [];
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
        const normalized = {serialNo: row.serialNo, tid: row.tid, applyStatus: row.applyStatus};
        if (rows.has(row.serialNo)) {
          if (stable(rows.get(row.serialNo)) !== stable(normalized)) throw Error('approval_target_conflict');
          continue;
        }
        rows.set(row.serialNo, normalized); added++;
      }
      if (body.data.length && !added) throw Error('duplicate_application_page');
      if (body.data.length < 20) {
        if (rows.size < total) throw Error('incomplete_application_pagination');
        return rows;
      }
    }
    throw Error('application_pagination_limit');
  }
  const pending = await list(false), agreed = await list(true);
  const states = applications.map(target => {
    const before = pending.get(target.serialNo), after = agreed.get(target.serialNo);
    if ([before, after].some(row => row && row.tid !== target.tid)) throw Error('approval_target_conflict');
    if (before && after && before.applyStatus !== after.applyStatus) throw Error('approval_target_conflict');
    let status = 'unknown';
    if (before?.applyStatus === 1 && !after) status = 'pending';
    if (after?.applyStatus === 2 && (!before || before.applyStatus === 2)) status = 'agreed';
    return {...target, status};
  });
  return {...result, applications: states, checked_at: timestamp()};
})()
