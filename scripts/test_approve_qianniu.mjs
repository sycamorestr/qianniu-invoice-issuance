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
  text: async () => JSON.stringify(body), ...options});
const context = {data: {isLogin: true, realNick: 'shop:operator', realBizRoles: ['VISITOR']}};
const clone = value => JSON.parse(JSON.stringify(value));
async function run(payload, bodies, failure, overrides = {}, clock = {elapsed: 0, waits: []}) {
  const calls = [];
  let next = 0; const deadlines = new Map(), guards = new Map();
  clock.fireDeadlines = () => { for (const callback of [...deadlines.values()]) callback(); };
  clock.tickGuards = () => { for (const callback of [...guards.values()]) callback(); };
  const promise = vm.runInNewContext(source.replace('__INPUT__', JSON.stringify(payload)), {
    URL, URLSearchParams, AbortSignal, AbortController,
    setTimeout: (callback, ms) => { const id = ++next; if (ms === 30000) deadlines.set(id, callback);
      else { clock.elapsed += ms; clock.waits.push(ms); clock.onSleep?.(); clock.tickGuards(); callback(); } return id; },
    clearTimeout: id => deadlines.delete(id),
    setInterval: callback => { const id = ++next; guards.set(id, callback); return id; },
    clearInterval: id => guards.delete(id),
    location: {hostname: 'myseller.taobao.com'},
    fetch: async (url, options) => {
      calls.push({url: new URL(url), options, at: clock.elapsed});
      if (failure && url.endsWith('/agree')) throw failure;
      const result = bodies.shift();
      if (result === undefined) throw Error('unexpected_request');
      return result?.text ? result : response(result);
    }, ...overrides
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
assert.deepEqual(status.calls.map(call => call.at), [5000, 10000]);
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
assert.deepEqual(paged.calls.map(call => call.at), [5000, 10000, 15000, 20000]);
const unknown = await run(input, Array.from({length: 4}, () => ({code: 200, total: 0})));
assert.equal((await unknown.promise).applications[0].status, 'unknown');
assert.equal(unknown.calls.length, 4);
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

// Live agreed lists with exactly three full pages return an explicit no-data
// tail without data, while retaining total=60. Reconcile all 55 selected rows
// after the five pre-existing rows, without sending any mutation.
const sixty = Array.from({length: 60}, (_, i) => row('EXACT-' + i, '91000000000000000' + String(i).padStart(2, '0'), 2));
const exactTargets = sixty.slice(5).map(({serialNo, tid}) => ({serialNo, tid}));
const terminal = {code: 200, total: 60, message: '无数据'};
const exactEnd = await run({...input, applications: exactTargets}, [
  {code: 200, total: 0},
  ...[0, 20, 40].map(offset => ({code: 200, total: 60, data: sixty.slice(offset, offset + 20)})),
  terminal
]);
assert.deepEqual(clone((await exactEnd.promise).applications), exactTargets.map(target => ({...target, status: 'agreed'})));
assert.deepEqual(exactEnd.calls.map(call => call.url.searchParams.get('pageNo')), ['0', '0', '1', '2', '3']);
assert.equal(exactEnd.calls.every(call => !call.options.method || call.options.method === 'GET'), true);
assert.deepEqual(exactEnd.calls.map(call => call.at), [5000, 10000, 15000, 20000, 25000]);

// The live 60 raw rows contain 59 unique rows: one equal record overlaps
// adjacent pages. Its exact serial/order/status is checked before deduplication.
const fiftyNine = [...sixty.slice(0, 4), ...sixty.slice(5)];
const overlappingSixty = [...fiftyNine.slice(0, 20), fiftyNine[19], ...fiftyNine.slice(20)];
const overlappingBodies = () => [
  {code: 200, total: 0},
  ...[0, 20, 40].map(offset => ({code: 200, total: 60, data: overlappingSixty.slice(offset, offset + 20)})),
  {code: 200, total: 60, message: '无数据'}
];
const overlapEnd = await run({...input, applications: exactTargets}, overlappingBodies());
assert.deepEqual(clone((await overlapEnd.promise).applications), exactTargets.map(target => ({...target, status: 'agreed'})));
assert.equal(overlapEnd.calls.length, 5);
assert.equal(overlapEnd.calls.every(call => !call.options.method || call.options.method === 'GET'), true);
const conflictingBodies = overlappingBodies();
conflictingBodies[2].data[0] = {...fiftyNine[19], tid: 'conflicting-order'};
await expectFailure({...input, applications: exactTargets}, conflictingBodies, /approval_target_conflict/, 3);
const missingTarget = {serialNo: 'ABSENT-FROM-COMPLETE-LIST', tid: '9999999999999999999'};
const absentEnd = await run({...input, applications: [...exactTargets, missingTarget]}, [
  ...overlappingBodies(), {code: 200, total: 0}, {code: 200, total: 0}
]);
assert.deepEqual(clone((await absentEnd.promise).applications), [
  ...exactTargets.map(target => ({...target, status: 'agreed'})), {...missingTarget, status: 'unknown'}
]);
assert.equal(absentEnd.calls.length, 7);
const nineteenUnique = [...full.slice(0, 19), full[18]];
// The raw-count exception is proven only for the agreed endpoint.
await expectFailure(input, [
  {code: 200, total: 20, data: nineteenUnique}, {code: 200, total: 20, message: '无数据'}
], /invalid_application_response/, 2);
await expectFailure(input, [
  {code: 200, total: 0},
  {code: 200, total: 21, data: nineteenUnique}, {code: 200, total: 21, message: '无数据'}
], /invalid_application_response/, 3);
const explicitEmptyTail = overlappingBodies();
explicitEmptyTail[4] = {code: 200, total: 60, data: []};
await expectFailure({...input, applications: exactTargets}, explicitEmptyTail, /incomplete_application_pagination/, 5);

// A nonempty agreed short page can cover the raw total while still having
// fewer unique rows. Require one more real terminal response before accepting it.
const shortRows = [full[19], {...targets[0], applyStatus: 2}];
const shortAgreedBodies = tail => [
  {code: 200, total: 0},
  {code: 200, total: 22, data: full},
  {code: 200, total: 22, data: shortRows},
  tail ?? {code: 200, total: 22, message: '无数据'}
];
const shortEnd = await run(input, shortAgreedBodies());
assert.deepEqual(clone((await shortEnd.promise).applications), [{...targets[0], status: 'agreed'}]);
assert.equal(shortEnd.calls.length, 4);
assert.deepEqual(shortEnd.calls.map(call => call.url.searchParams.get('pageNo')), ['0', '0', '1', '2']);
assert.deepEqual(shortEnd.calls.map(call => call.at), [5000, 10000, 15000, 20000]);
for (const [index, call] of shortEnd.calls.entries()) {
  assert.equal(!call.options.method || call.options.method === 'GET', true);
  assert.equal(call.url.searchParams.get('startTime'), scope.start_date);
  assert.equal(call.url.searchParams.get('endTime'), scope.end_date);
  assert.equal(call.url.searchParams.get('agentId'), input.agentId);
  assert.equal(call.url.searchParams.get('pageSize'), '20');
  assert.equal(call.url.searchParams.get('rightsRemainTime'), index === 0 ? '100' : null);
}
await expectFailure(input, [
  {code: 200, total: 22, data: full.map(item => ({...item, applyStatus: 1}))},
  {code: 200, total: 22, data: shortRows.map(item => ({...item, applyStatus: 1}))}
], /incomplete_application_pagination/, 2);
await expectFailure(input, [
  {code: 200, total: 0}, {code: 200, total: 23, data: full},
  {code: 200, total: 23, data: shortRows}
], /incomplete_application_pagination/, 3);
const newConfirmationRow = row('CONFIRMATION-ROW', '9200000000000000001', 2);
const shortCompleted = await run(input, shortAgreedBodies({code: 200, total: 22, data: [newConfirmationRow]}));
assert.equal((await shortCompleted.promise).applications[0].status, 'agreed');
assert.equal(shortCompleted.calls.length, 4);
assert.equal(shortCompleted.calls.every(call => !call.options.method || call.options.method === 'GET'), true);
for (const [tail, reason] of [
  [{code: 200, total: 22, data: []}, /incomplete_application_pagination/],
  [{code: 200, total: 22, message: 'unexpected'}, /invalid_application_response/],
  [{code: 200, total: 22, message: '无数据', data: null}, /invalid_application_response/],
  [{code: 200, total: 23, data: []}, /applications_changed_during_pagination/],
  [{code: 200, total: 22, data: [full[0]]}, /duplicate_application_page/],
  [{code: 200, total: 22, data: [{...full[0], tid: 'conflicting-order'}]}, /approval_target_conflict/],
  [{code: 200, total: 22, data: Array.from({length: 20}, (_, i) =>
    row('UNEXPECTED-FULL-' + i, '93000000000000000' + String(i).padStart(2, '0'), 2))}, /incomplete_application_pagination/]
]) {
  const calls = await expectFailure(input, shortAgreedBodies(tail), reason, 4);
  assert.equal(calls.every(call => !call.options.method || call.options.method === 'GET'), true);
}
const stillIncomplete = shortAgreedBodies({code: 200, total: 22, data: [newConfirmationRow]});
stillIncomplete[1].data = nineteenUnique;
stillIncomplete[2].data = [nineteenUnique[18], {...targets[0], applyStatus: 2}];
await expectFailure(input, stillIncomplete, /incomplete_application_pagination/, 4);
const shortUnknown = await run({...input, applications: [targets[0], missingTarget]}, [
  ...shortAgreedBodies(), {code: 200, total: 0}, {code: 200, total: 0}
]);
assert.deepEqual(clone((await shortUnknown.promise).applications), [
  {...targets[0], status: 'agreed'}, {...missingTarget, status: 'unknown'}
]);
assert.equal(shortUnknown.calls.length, 6);
assert.equal(shortUnknown.calls.every(call => !call.options.method || call.options.method === 'GET'), true);
// Ordinary short pages retain the existing unique-row completeness check.
await expectFailure(input, [
  {code: 200, total: 21, data: nineteenUnique},
  {code: 200, total: 21, data: [row(targets[0].serialNo, targets[0].tid, 2)]}
], /incomplete_application_pagination/, 2);

// The same exact terminal response is valid for a fully read pending list.
const pendingFull = [{...targets[0], applyStatus: 1}, ...full.slice(1).map(item => ({...item, applyStatus: 1}))];
const pendingEnd = await run(input, [
  {code: 200, total: 20, data: pendingFull}, {code: 200, total: 20, message: '无数据'},
  {code: 200, total: 0}
]);
assert.equal((await pendingEnd.promise).applications[0].status, 'pending');
assert.equal(pendingEnd.calls.length, 3);

// API total can be smaller than the observed unique rows. A target beyond
// that count must still be read and checked before declaring the list complete.
const beyondTotal = await run(input, [
  {code: 200, total: 0},
  {code: 200, total: 20, data: full},
  {code: 200, total: 20, data: [full[0], row(targets[0].serialNo, targets[0].tid, 2)]}
]);
assert.equal((await beyondTotal.promise).applications[0].status, 'agreed');
assert.equal(beyondTotal.calls.length, 3);

// Missing rows remain invalid without every observed terminal condition:
// never accept a nonzero first page, fewer raw rows than total, changed
// total, an absent/wrong message, or an explicit null data value.
await expectFailure(input, [{code: 200, total: 20, message: '无数据'}], /invalid_application_response/, 1);
await expectFailure(input, [
  {code: 200, total: 21, data: full}, {code: 200, total: 21, message: '无数据'}
], /invalid_application_response/, 2);
for (const invalidTail of [
  {code: 200, total: 19, message: '无数据'},
  {code: 200, total: 20},
  {code: 200, total: 20, message: 'unexpected'},
  {code: 200, total: 20, message: '无数据', data: null},
  {code: 500, total: 20, message: '无数据'}
]) await expectFailure(input, [{code: 200, total: 20, data: full}, invalidTail], /invalid_application_response/, 2);
for (const [tail, reason] of [
  [response({code: 200, total: 20, message: '无数据'}, {status: 429, ok: false}), /rate_limited/],
  [{code: 200, total: 20, message: '无数据', ret: ['FAIL_SYS_USER_VALIDATE']}, /verification_required/]
]) await expectFailure(input, [{code: 200, total: 20, data: full}, tail], reason, 2);

// Full lists can omit a saved target even though the exact order query returns
// it. Supplement only targets absent from both complete lists, once per tid.
const sameOrderTargets = [targets[0], {serialNo: 'SECOND-APPLICATION', tid: targets[0].tid}];
const targeted = await run({...input, applications: sameOrderTargets}, [
  {code: 200, total: 0}, {code: 200, total: 0}, {code: 200, total: 0},
  {code: 200, total: 2, data: sameOrderTargets.map(target => ({...target, applyStatus: 2}))}
]);
const targetedResult = await targeted.promise;
assert.deepEqual(clone(targetedResult.applications), sameOrderTargets.map(target => ({...target, status: 'agreed'})));
assert.equal(targeted.calls.length, 4);
assert.deepEqual(targeted.calls.map(call => call.at), [5000, 10000, 15000, 20000]);
assert.deepEqual(clone(targetedResult.targeted_reconciliation.order_ids), [targets[0].tid]);
assert.equal(targetedResult.targeted_reconciliation.reason, 'absent_from_both_complete_lists');
assert.deepEqual(clone(targetedResult.targeted_reconciliation.query_scope), scope);
assert.deepEqual(clone(targetedResult.targeted_reconciliation.expected_identity),
  {observed_store: input.expected_observed_store, account_nick: input.expected_account_nick, agentId: input.agentId});
for (const [index, call] of targeted.calls.entries()) {
  assert.equal(call.options.method === undefined || call.options.method === 'GET', true);
  assert.equal(call.url.searchParams.get('tid'), index < 2 ? null : targets[0].tid);
  assert.equal(call.url.searchParams.get('startTime'), scope.start_date);
  assert.equal(call.url.searchParams.get('endTime'), scope.end_date);
  assert.equal(call.url.searchParams.get('agentId'), '0');
  assert.equal(call.url.searchParams.get('rightsRemainTime'), index % 2 === 0 ? '100' : null);
  if (index >= 2) {
    const evidence = targetedResult.targeted_reconciliation.queries[index - 2];
    assert.deepEqual(clone(evidence.query), Object.fromEntries(call.url.searchParams));
    assert.equal(evidence.raw_row_count, index === 2 ? 0 : 2);
  }
}
assert.deepEqual(clone(targetedResult.targeted_reconciliation.queries[1].rows),
  sameOrderTargets.map(target => ({...target, applyStatus: 2})));
const targetedPage = full.map(item => ({...item, tid: targets[0].tid}));
const targetedPaged = await run(input, [
  {code: 200, total: 0}, {code: 200, total: 0}, {code: 200, total: 0},
  {code: 200, total: 21, data: targetedPage},
  {code: 200, total: 21, data: [{...targets[0], applyStatus: 2}]}
]);
assert.equal((await targetedPaged.promise).applications[0].status, 'agreed');
assert.deepEqual(targetedPaged.calls.map(call => call.url.searchParams.get('pageNo')), ['0', '0', '0', '0', '1']);
// Another application on the same order cannot stand in for the exact serial.
const anotherSerial = await run(input, [
  {code: 200, total: 0}, {code: 200, total: 0}, {code: 200, total: 0},
  {code: 200, total: 1, data: [row('OTHER-SERIAL', targets[0].tid, 2)]}
]);
assert.equal((await anotherSerial.promise).applications[0].status, 'unknown');
const abnormal = await run(input, [
  {code: 200, total: 1, data: [{...targets[0], applyStatus: 9}]}, {code: 200, total: 0}
]);
assert.equal((await abnormal.promise).applications[0].status, 'unknown');
assert.equal(abnormal.calls.length, 2);
await expectFailure(input, [
  {code: 200, total: 0}, {code: 200, total: 0},
  {code: 200, total: 1, data: [row('OTHER-SERIAL', 'wrong-order')]}
], /approval_target_conflict/, 3);
await expectFailure(input, [
  {code: 200, total: 0}, {code: 200, total: 0},
  {code: 200, total: 1, data: [{...targets[0], applyStatus: 1}]},
  {code: 200, total: 1, data: [{...targets[0], applyStatus: 2}]}
], /approval_target_conflict/, 4);
await expectFailure({...input, applications: sameOrderTargets}, [
  {code: 200, total: 1, data: [{...targets[0], applyStatus: 1}]}, {code: 200, total: 0},
  {code: 200, total: 1, data: [{...targets[0], applyStatus: 2}]}
], /approval_target_conflict/, 3);
for (const [failure, reason] of [
  [response({}, {status: 429, ok: false}), /rate_limited/],
  [{code: 200, total: 0, message: '请完成滑块验证'}, /verification_required/]
]) await expectFailure(input, [{code: 200, total: 0}, {code: 200, total: 0}, failure], reason, 3);

// One visitor request exactly reproduces the observed manual-entry UI action.
const approveInput = {...input, operation: 'approve'};
const accepted = await run(approveInput, [context, {code: 200, message: '操作成功'}]);
const receipt = await accepted.promise;
assert.equal(receipt.code, 200);
assert.deepEqual(clone(receipt.applications), targets);
assert.equal(accepted.calls.length, 2);
assert.deepEqual(accepted.calls.map(call => call.at), [5000, 10000]);
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
  response({}, {text: async () => {throw SyntaxError('bad_json');}})]) {
  const failedCalls = await expectFailure(approveInput, [context, remote], /approval_(duplicate_check|rejected|unknown)/, 2);
  assert.equal(JSON.parse(failedCalls[1].options.body).repeatCheck, true);
}
for (const errorName of ['TimeoutError', 'TypeError']) {
  const error = new Error('response lost'); error.name = errorName;
  const failed = await run(approveInput, [context], error);
  await assert.rejects(failed.promise, /approval_unknown/);
  assert.equal(failed.calls.filter(call => call.options.method === 'POST').length, 1);
}

// Authentication and verification failures on a read stop immediately. Once
// POST was dispatched they remain unknown, requiring read-only reconciliation.
for (const remote of [
  response({}, {status: 401, ok: false}),
  response({}, {url: 'https://passport.taobao.com/login'}),
  response({}, {url: 'https://einvoice.taobao.com/_____tmd_____/punish'}),
  response({code: 200, total: 0, message: '请完成滑块验证'}),
  response({code: 200, total: 0, ret: ['FAIL_SYS_USER_VALIDATE']}),
  response({}, {text: async () => '<html><body>请完成安全验证</body></html>'})
]) {
  await expectFailure(input, [remote], /login_required|verification_required/, 1);
  await expectFailure(approveInput, [remote], /login_required|verification_required/, 1);
  await expectFailure(approveInput, [context, remote], /approval_unknown/, 2);
}
await expectFailure(input, [response({}, {status: 429, ok: false})], /rate_limited/, 1);
await expectFailure(input, [response({}, {status: 503, ok: false})], /HTTP 503/, 1);

// A visible challenge found during the conservative wait prevents dispatch;
// during response consumption it cancels the active request, with no retry.
const challengeNode = {tagName: 'IFRAME', src: 'https://trade.taobao.com/_____tmd_____/punish',
  getBoundingClientRect: () => ({width: 400, height: 300})};
let challenge = false;
const challengeDocument = {querySelectorAll: () => challenge ? [challengeNode] : []};
const blockedClock = {elapsed: 0, waits: [], onSleep: () => {challenge = true;}};
const blockedWait = await run(input, [], null, {document: challengeDocument}, blockedClock);
await assert.rejects(blockedWait.promise, /verification_required/);
assert.equal(blockedWait.calls.length, 0);
for (const operation of ['approve', 'approval-status']) {
  const stopped = await run({...input, operation}, [], null, {__qianniuInvoiceStopRequested: true});
  await assert.rejects(stopped.promise, /verification_required/);
  assert.equal(stopped.calls.length, 0);
}
for (const mode of ['timeout', 'challenge']) {
  for (const operation of ['approve', 'approval-status']) {
    const clock = {elapsed: 0, waits: []}; let calls = 0, aborts = 0;
    challenge = false;
    const interrupted = await run({...input, operation}, [], null, {
      document: challengeDocument,
      fetch: async (url, options) => {
        calls++;
        if (operation === 'approve' && url.endsWith('/context')) return response(context);
        return response({}, {text: () => new Promise((resolve, reject) => {
          options.signal.addEventListener('abort', () => {aborts++; reject(Object.assign(Error('body_aborted'), {name: 'AbortError'}));}, {once: true});
          if (mode === 'timeout') clock.fireDeadlines(); else {challenge = true; clock.tickGuards();}
        })});
      }
    }, clock);
    await assert.rejects(interrupted.promise, operation === 'approve' ? /approval_unknown/ : mode === 'challenge' ? /verification_required/ : /body_aborted/);
    assert.equal(calls, operation === 'approve' ? 2 : 1);
    assert.equal(aborts, 1);
    assert.deepEqual(clock.waits, new Array(calls).fill(5000));
  }
}
for (const operation of ['approve', 'approval-status']) for (const phase of ['wait', 'body']) {
  const listeners = new Set();
  const fakeGlobal = {__qianniuInvoiceStopRequested: false,
    addEventListener: (name, callback) => {assert.equal(name, 'qianniu-invoice-stop'); listeners.add(callback);},
    removeEventListener: (name, callback) => listeners.delete(callback)};
  const cancel = () => {
    fakeGlobal.__qianniuInvoiceStopRequested = true; for (const listener of listeners) listener();
    fakeGlobal.__qianniuInvoiceStopRequested = false;
  };
  const clock = {elapsed: 0, waits: [], onSleep: phase === 'wait' ? cancel : undefined};
  let calls = 0, aborts = 0;
  const cancelled = await run({...input, operation}, [], null, {globalThis: fakeGlobal,
    fetch: async (url, options) => {
      calls++;
      if (operation === 'approve' && url.endsWith('/context')) return response(context);
      return response({}, {text: () => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => {aborts++; reject(Object.assign(Error('event_aborted'), {name: 'AbortError'}));}, {once: true});
        cancel();
      })});
    }
  }, clock);
  await assert.rejects(cancelled.promise, phase === 'body' && operation === 'approve' ? /approval_unknown/ : /verification_required/);
  assert.equal(calls, phase === 'wait' ? 0 : operation === 'approve' ? 2 : 1);
  assert.equal(aborts, phase === 'wait' ? 0 : 1); assert.equal(listeners.size, 0);
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
const daily = await run({...input, date: singleDate.date, query_scope: singleDate}, Array.from({length: 4}, () => ({code: 200, total: 0})));
assert.equal((await daily.promise).date, singleDate.date);
const leap = await run({...input, query_scope: {...scope, start_date: '2024-02-29', end_date: '2024-04-30'}},
  Array.from({length: 4}, () => ({code: 200, total: 0})));
await leap.promise;
console.log('approve_qianniu: exact scope, pagination, manual-entry roles and no-retry checks passed');
