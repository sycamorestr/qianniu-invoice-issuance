from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

import playwright_controller as controller
from test_playwright_controller import CONFIG, FakeContext, FakePage


def evidence(ready=False, **values):
    return {"ready": ready, "ready_state": "complete", "verification": [],
            "signals": [], **values}


class ProbePage(FakePage):
    def __init__(self, url):
        super().__init__(url)
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.value = evidence()
        self.calls = 0
        self.goto = AsyncMock()

    async def evaluate(self, source, payload=None):
        self.calls += 1
        return self.value


class ProbeFrame:
    def __init__(self, url, *, visible=True, value=None):
        self.url = url
        self.value = value or evidence()
        self.evaluate = AsyncMock(side_effect=lambda *args: self.value)
        self.frame_element = AsyncMock(return_value=type("Element", (), {
            "is_visible": AsyncMock(return_value=visible)})())


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.poll_patch = patch.object(controller, "BUSINESS_READY_POLL_SECONDS", 0.005)
        self.stable_patch = patch.object(controller, "BUSINESS_READY_STABLE_SECONDS", 0.025)
        self.poll_patch.start()
        self.stable_patch.start()
        self.addCleanup(self.poll_patch.stop)
        self.addCleanup(self.stable_patch.stop)

    def instance(self, role="invoice", *, timeout=300):
        instance = controller.PlaywrightBrowserController(CONFIG, timeout_ms=timeout)
        page = ProbePage(CONFIG["browser_sessions"][role]["url"])
        instance.context = FakeContext([page])
        instance.context.new_page = AsyncMock()
        instance.registrations[role] = controller.PageRegistration(role, page)
        return instance, page

    async def test_waits_for_late_invoice_data_and_stability_without_navigation(self):
        instance, page = self.instance()
        asyncio.get_running_loop().call_later(0.015, setattr, page, "value",
                                              evidence(True, stable_token="agent-1"))
        began = asyncio.get_running_loop().time()
        value = await instance.wait_for_business_ready("invoice")
        self.assertTrue(value["ready"])
        self.assertGreaterEqual(asyncio.get_running_loop().time() - began, 0.035)
        self.assertGreater(page.calls, 2)
        self.assertNotIn("agent-1", json.dumps(value))
        page.goto.assert_not_awaited()
        instance.context.new_page.assert_not_awaited()

    async def test_late_goods_iframe_then_late_tenant_are_both_required(self):
        instance, page = self.instance("goods")
        page.value = evidence(True, signals=["issuer_label"], stable_token="issuer")
        frame = ProbeFrame(controller.GOODS_READINESS_FRAME_URL)
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, page.frames.append, frame)
        loop.call_later(0.025, setattr, frame, "value",
                        evidence(True, signals=["unique_tenant"], stable_token="1:2"))
        value = await instance.wait_for_business_ready("goods")
        self.assertEqual(value["signals"], ["issuer_label", "unique_tenant", "goods_iframe"])
        self.assertGreater(frame.evaluate.await_count, 2)
        page.goto.assert_not_awaited()

    async def test_readiness_drop_resets_stable_window(self):
        instance, page = self.instance()
        loop = asyncio.get_running_loop()
        resumed_at = None
        async def sequence(*_args):
            nonlocal resumed_at
            page.calls += 1
            if page.calls == 3:
                return evidence(False)
            if page.calls == 4:
                resumed_at = loop.time()
            return evidence(True, stable_token="agent")
        page.evaluate = sequence
        await instance.wait_for_business_ready("invoice")
        self.assertIsNotNone(resumed_at)
        self.assertGreaterEqual(loop.time() - resumed_at, 0.02)

    async def test_wait_times_out_without_business_calls_or_new_pages(self):
        instance, page = self.instance("orders", timeout=20)
        with self.assertRaises(controller.BrowserControllerError) as caught:
            await instance.wait_for_business_ready("orders")
        self.assertEqual(caught.exception.code, "page_not_ready")
        page.goto.assert_not_awaited()
        instance.context.new_page.assert_not_awaited()

    async def test_visible_challenge_iframe_interrupts_even_when_dom_probe_hangs(self):
        instance, page = self.instance("orders")
        page.frames.append(ProbeFrame("https://trade.taobao.com/_____tmd_____/punish?x=1"))
        page.evaluate = AsyncMock(side_effect=lambda *_: asyncio.Event().wait())
        with self.assertRaises(controller.BrowserControllerError) as caught:
            await instance.raise_if_verification_required("orders")
        self.assertEqual(caught.exception.code, "verification_required")
        page.evaluate.assert_not_awaited()

    async def test_hidden_challenge_frame_does_not_stop_clean_business_page(self):
        instance, page = self.instance("orders")
        page.frames.append(ProbeFrame("https://trade.taobao.com/_____tmd_____/punish", visible=False))
        await instance.raise_if_verification_required("orders")

    async def test_navigation_context_loss_is_inconclusive_and_next_poll_detects_challenge(self):
        for message in ("Execution context was destroyed, most likely because of a navigation",
                        "Cannot find context with specified id", "Frame was detached"):
            with self.subTest(message=message):
                instance, page = self.instance("orders")
                page.evaluate = AsyncMock(side_effect=RuntimeError(message))
                await instance.raise_if_verification_required("orders")
                page.frames.append(ProbeFrame("https://trade.taobao.com/_____tmd_____/punish"))
                with self.assertRaises(controller.BrowserControllerError) as caught:
                    await instance.raise_if_verification_required("orders")
                self.assertEqual(caught.exception.code, "verification_required")
                self.assertEqual(page.evaluate.await_count, 1)

    async def test_navigation_probe_does_not_suppress_closed_page_or_other_errors(self):
        for close_page in (False, True):
            with self.subTest(close_page=close_page):
                instance, page = self.instance("orders")
                async def fail(*_args):
                    page.closed = close_page
                    raise RuntimeError("Execution context was destroyed" if close_page else "unexpected protocol error")
                page.evaluate = fail
                with self.assertRaises(controller.BrowserControllerError) as caught:
                    await instance.raise_if_verification_required("orders")
                self.assertEqual(caught.exception.code, "page_missing" if close_page else "page_not_ready")

    async def test_late_verification_stops_wait_immediately(self):
        instance, page = self.instance()
        asyncio.get_running_loop().call_later(0.01, setattr, page, "value",
                                              evidence(verification=["challenge_title"]))
        with self.assertRaises(controller.BrowserControllerError) as caught:
            await instance.wait_for_business_ready("invoice")
        self.assertEqual(caught.exception.code, "verification_required")

    async def test_ambiguous_identity_stops_instead_of_waiting_or_guessing(self):
        for role in ("invoice", "goods"):
            with self.subTest(role=role):
                instance, page = self.instance(role)
                page.value = evidence(ambiguous_context=True)
                with self.assertRaises(controller.BrowserControllerError) as caught:
                    await instance.wait_for_business_ready(role)
                self.assertEqual(caught.exception.code, "context_changed")
                self.assertEqual(page.calls, 1)

    async def test_login_redirect_closed_page_and_disconnect_have_typed_errors(self):
        for condition, code in (("login", "login_required"), ("closed", "page_missing"),
                                ("disconnected", "browser_disconnected")):
            with self.subTest(condition=condition):
                instance, page = self.instance()
                if condition == "login":
                    page.url = "https://login.taobao.com/member/login.jhtml"
                elif condition == "closed":
                    page.closed = True
                else:
                    instance.browser = type("Browser", (), {"is_connected": lambda self: False})()
                with self.assertRaises(controller.BrowserControllerError) as caught:
                    await instance.wait_for_business_ready("invoice")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(page.calls, 0)

    async def test_login_inside_late_goods_frame_stops(self):
        instance, page = self.instance("goods")
        page.value = evidence(True)
        page.frames.append(ProbeFrame("https://jstlogin.erp321.com/login"))
        with self.assertRaises(controller.BrowserControllerError) as caught:
            await instance.wait_for_business_ready("goods")
        self.assertEqual(caught.exception.code, "login_required")

    async def test_wait_remains_cancellable_without_navigation(self):
        instance, page = self.instance()
        task = asyncio.create_task(instance.wait_for_business_ready("invoice"))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        calls = page.calls
        await asyncio.sleep(0.01)
        self.assertEqual(page.calls, calls)
        page.goto.assert_not_awaited()


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the passive page-probe fixtures")
class PageProbeJavascriptTests(unittest.TestCase):
    def test_business_dom_and_tenant_evidence_and_visible_challenges(self):
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
function fixture(role, options={}) {
  const selectors = options.selectors || {};
  const sandbox = {
    document: {readyState: options.state || 'complete', title: options.title || '',
      body: {innerText: options.body || ''}, querySelectorAll: selector => selectors[selector] || []},
    location: {href: options.url || 'https://example.test/'}, URL,
    performance: {getEntriesByType: () => (options.resources || []).map(name => ({name}))},
    getComputedStyle: element => ({display: element.hidden ? 'none' : 'block', visibility: 'visible', opacity: '1'}),
    fetch: () => {throw Error('readiness must not fetch')},
  };
  return vm.runInNewContext(`(${source})`, sandbox)({role, tenant_frame: options.tenant_frame});
}
function element(values={}) {
  return {getBoundingClientRect: () => ({width: 20, height: 20}),
    getAttribute: key => (values.attributes || {})[key] || '', ...values};
}
const agents = '[name="agentId"],[name="agent_id"],[data-agent-id],[data-agentid]';
assert.strictEqual(fixture('invoice').ready, false);
assert.strictEqual(fixture('invoice', {state:'loading', resources:['https://einvoice.taobao.com/a?agentId=1']}).ready, false);
assert.strictEqual(fixture('invoice', {resources:['https://einvoice.taobao.com/a?agentId=1']}).ready, true);
assert.strictEqual(fixture('invoice', {selectors:{[agents]:[element({value:'1'})]}, resources:['https://einvoice.taobao.com/a?agentId=1']}).ready, true);
assert.strictEqual(fixture('invoice', {resources:['https://einvoice.taobao.com/a?agentId=1','https://einvoice.taobao.com/a?agentId=2']}).ambiguous_context, true);
assert.strictEqual(fixture('goods', {tenant_frame:true}).ready, false);
assert.strictEqual(fixture('goods', {tenant_frame:true, resources:['https://jstweb.cn-hangzhou.log.aliyuncs.com/a?co_id=1&user_id=2']}).ready, true);
assert.strictEqual(fixture('goods', {tenant_frame:true, resources:['https://jstweb.cn-hangzhou.log.aliyuncs.com/a?co_id=1&user_id=2','https://jstweb.cn-hangzhou.log.aliyuncs.com/a?co_id=3&user_id=4']}).ambiguous_context, true);
assert.strictEqual(fixture('orders', {body:'已卖出宝贝'}).ready, false);
const orderForm = {body:'已卖出宝贝 订单编号 搜索', selectors:{'input,textarea':[element({placeholder:'请输入订单号'})]}};
assert.strictEqual(fixture('orders', orderForm).ready, true);
assert.strictEqual(fixture('orders', {...orderForm, state:'interactive'}).ready, false);
assert.strictEqual(fixture('', {title:'验证码拦截'}).verification[0], 'challenge_title');
const challenge = element({src:'https://trade.taobao.com/_____tmd_____/punish'});
assert.strictEqual(fixture('', {selectors:{iframe:[challenge]}}).verification[0], 'challenge_iframe');
assert.strictEqual(fixture('', {selectors:{iframe:[{...challenge, hidden:true}]}}).verification.length, 0);
assert.strictEqual(fixture('', {body:'欢迎登录，淘宝登录'}).login_required, false);
assert.strictEqual(fixture('', {body:'登录已过期'}).login_required, true);
"""
        result = subprocess.run([shutil.which("node"), "-e", harness,
                                 str(Path(controller.__file__).with_name("playwright_page_probe.js"))],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
