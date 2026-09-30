import fs from 'node:fs/promises';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = await fs.readFile(new URL('./approve_qianniu.js', import.meta.url), 'utf8');
const targets = [{serialNo: 'TEST-APPLICATION-1', tid: '9000000000000000001'}];
const scope = {mode: 'all_pending', start_date: '2026-07-30', end_date: '2026-09-30', countdown: 'started'};
const input = {operation: 'approval-status', date: null, query_scope: scope, agentId: '0',
  applications: targets, expected_store: 'Friendly shop', expected_observed_store: 'shop',
  expected_account_nick: 'shop:operator'};
const row = (serialNo, tid, applyStatus = 1) => ({serialNo, tid, applyStatus});
const response = (body, options = {}) => ({ok: true, status: 200, url: 'https://einvoice.taobao.com/api/test',
  json: async () => body, ...options});
const context = {data: {isLogin: true, realNick: 'shop:operator', realBizRoles: ['VISITOR']}};
const clone = value => JSON.parse(JSON.stringify(value));
async function run(payload, bodies, failure) {
  const calls = [];
  const promise = vm.runInNewContext(source.replace('__INPUT__', JSON.stringify(payload)), {
    URL, URLSearchParams, AbortSignal,
    location: {hostname: 'myseller.taobao.com'},
    fetch: async (url, options) => {
      calls.push({url: new URL(url), options});
      if (failure && url.endsWith('/agree')) throw failure;
      const result = bodies.shift();
      if (result === undefined) throw Error('unexpected_request');
      return result?.json ? result : response(result);
    }
  });
  return {promise, calls};
}
async function expectFailure(payload, bodies, pattern, expectedCalls) {
  const {promise, calls} = await run(payload, bodies);
  await assert.rejects(promise, pattern);
  assert.equal(calls.length, expectedCalls);
  return calls;
}

// Exact identifiers and both list queries are retained; agreed deliberately
// removes the countdown filter so approved rows remain visible after transition.
const status = await run(input, [
  {code: 200, total: 1, data: [row(targets[0].serialNo, targets[0].tid)]},
  {code: 200, total: 0}
]);
assert.deepEqual(clone((await status.promise).applications), [{...targets[0], status: 'pending'}]);
assert.equal(status.calls.length, 2);
for (const call of status.calls) {
  assert.equal(call.url.searchParams.get('startTime'), scope.start_date);
  assert.equal(call.url.searchParams.get('endTime'), scope.end_date);
  assert.equal(call.url.searchParams.get('agentId'), '0');
  assert.equal(call.url.searchParams.get('pageSize'), '20');
}
assert.equal(status.calls[0].url.searchParams.get('rightsRemainTime'), '100');
assert.equal(status.calls[0].url.searchParams.get('applyListType'), '0');
assert.equal(status.calls[1].url.pathname, '/api/qianniu/invoice/list/apply/agreed');
assert.equal(status.calls[1].url.searchParams.has('rightsRemainTime'), false);

// The target appears only after a full page in each independent list.
const full = Array.from({length: 20}, (_, i) => row('P' + i, '900' + i, 2));
const paged = await run(input, [
  {code: 200, total: 21, data: full}, {code: 200, total: 21, data: [row(targets[0].serialNo, targets[0].tid, 2)]},
  {code: 200, total: 21, data: full}, {code: 200, total: 21, data: [row(targets[0].serialNo, targets[0].tid, 2)]}
]);
assert.equal((await paged.promise).applications[0].status, 'agreed');
assert.deepEqual(paged.calls.map(call => call.url.searchParams.get('pageNo')), ['0', '1', '0', '1']);
const unknown = await run(input, [{code: 200, total: 0}, {code: 200, total: 0}]);
assert.equal((await unknown.promise).applications[0].status, 'unknown');
await expectFailure(input, [
  {code: 200, total: 1, data: [row(targets[0].serialNo, 'different-order')]}, {code: 200, total: 0}
], /approval_target_conflict/, 2);
await expectFailure(input, [
  {code: 200, total: 1, data: [row(targets[0].serialNo, targets[0].tid)]},
  {code: 200, total: 1, data: [row(targets[0].serialNo, targets[0].tid, 2)]}
], /approval_target_conflict/, 2);
await expectFailure(input, [{code: 200, total: 1, data: [row(targets[0].serialNo, 9000000000000000001)]}],
  /invalid_approval_application_response/, 1);
await expectFailure(input, [{code: 200, total: 2, data: [row('one', '1')]}], /incomplete_application_pagination/, 1);
await expectFailure(input, [{code: 200, total: 21, data: full}, {code: 200, total: 21, data: full}], /duplicate_application_page/, 2);
await expectFailure(input, [{code: 200, total: 21, data: full}, {code: 200, total: 20, data: []}],
  /applications_changed_during_pagination/, 2);

// One visitor request exactly reproduces the observed manual-entry UI action.
const approveInput = {...input, operation: 'approve'};
const accepted = await run(approveInput, [context, {code: 200, message: '操作成功'}]);
const receipt = await accepted.promise;
assert.equal(receipt.code, 200);
assert.deepEqual(clone(receipt.applications), targets);
assert.equal(accepted.calls.length, 2);
assert.equal(accepted.calls[1].url.href, 'https://einvoice.taobao.com/api/invoice/apply/agree');
assert.equal(accepted.calls[1].options.method, 'POST');
assert.deepEqual(JSON.parse(accepted.calls[1].options.body), {agreeList: targets, repeatCheck: true});
assert.equal(accepted.calls[1].options.credentials, 'include');
// UI list pagination is independent from the selected agreeList payload.
const allTargets = Array.from({length: 61}, (_, i) => ({serialNo: 'E' + i, tid: '90000000000000000' + String(i).padStart(2, '0')}));
const allAccepted = await run({...approveInput, applications: allTargets}, [context, {code: 200, message: '操作成功'}]);
assert.deepEqual(clone((await allAccepted.promise).applications), allTargets);
const posts = allAccepted.calls.filter(call => call.options.method === 'POST');
assert.equal(posts.length, 1);
assert.deepEqual(JSON.parse(posts[0].options.body), {agreeList: allTargets, repeatCheck: true});
for (const role of ['MASTER', 'SUB_ADMIN', 'STORE_ADMIN', 'STORE_OPERATOR', 'ALI_EINVOICE_EA_JXS_MASTER', 'ALI_EINVOICE_EA_JXS_USER']) {
  const manual = await run(approveInput, [
    {data: {...context.data, realBizRoles: [role]}}, {code: 200, message: '操作成功'}
  ]);
  await manual.promise;
  assert.deepEqual(JSON.parse(manual.calls[1].options.body), {agreeList: targets, repeatCheck: true, autoCreate: 1});
}
await expectFailure(approveInput, [{data: {...context.data, realBizRoles: []}}], /approval_role_unsupported/, 1);
await expectFailure(approveInput, [{data: {...context.data, realBizRoles: ['NEW_ROLE']}}], /approval_role_unsupported/, 1);
await expectFailure(approveInput, [{data: {...context.data, realNick: 'another:operator'}}], /context_changed_account/, 1);
await expectFailure(approveInput, [{data: {...context.data, isLogin: false}}], /login_required/, 1);

// Response loss, throttling, duplicate-invoice prompts and server errors all
// stop after exactly one POST. No repeated request disables repeatCheck.
for (const remote of [{code: 1020, message: '重复开票'}, {code: 500},
  response({}, {ok: false, status: 429}), response({}, {ok: false, status: 503}),
  response({}, {json: async () => {throw SyntaxError('bad_json');}})]) {
  const failedCalls = await expectFailure(approveInput, [context, remote], /approval_(duplicate_check|rejected|unknown)/, 2);
  assert.equal(JSON.parse(failedCalls[1].options.body).repeatCheck, true);
}
for (const errorName of ['TimeoutError', 'TypeError']) {
  const error = new Error('response lost'); error.name = errorName;
  const failed = await run(approveInput, [context], error);
  await assert.rejects(failed.promise, /approval_unknown/);
  assert.equal(failed.calls.filter(call => call.options.method === 'POST').length, 1);
}

for (const applications of [[{...targets[0], tid: 1}], [targets[0], targets[0]], [{serialNo: 'empty'}]])
  await expectFailure({...approveInput, applications}, [], /invalid_approval_applications/, 0);
for (const overrides of [{autoCreate: 0}, {autoCreate: 1}, {repeatCheck: false}])
  await expectFailure({...approveInput, ...overrides}, [], /invalid_approval_options/, 0);
for (const badScope of [{...scope, countdown: 'all'}, {...scope, start_date: '2026-07-29'},
  {...scope, extra: true}, {...scope, countdown: undefined}])
  await expectFailure({...approveInput, query_scope: badScope}, [], /invalid_query_scope/, 0);
await expectFailure({...approveInput, expected_account_nick: ''}, [], /approval_identity_required/, 0);
for (const operation of ['approve', 'approval-status']) {
  const empty = await run({...input, operation, applications: []}, []);
  assert.deepEqual(clone((await empty.promise).applications), []);
  assert.equal(empty.calls.length, 0);
}
const singleDate = {mode: 'date', date: '2026-09-30', countdown: 'started'};
const daily = await run({...input, date: singleDate.date, query_scope: singleDate}, [{code: 200, total: 0}, {code: 200, total: 0}]);
assert.equal((await daily.promise).date, singleDate.date);
const leap = await run({...input, query_scope: {...scope, start_date: '2024-02-29', end_date: '2024-04-30'}},
  [{code: 200, total: 0}, {code: 200, total: 0}]);
await leap.promise;
console.log('approve_qianniu: exact scope, pagination, manual-entry roles and no-retry checks passed');
