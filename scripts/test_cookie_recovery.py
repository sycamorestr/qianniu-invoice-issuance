"""Offline expiry recovery tests; never read private configuration or a browser."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import playwright_adapter as adapter
from playwright_controller import BrowserControllerError, PageRegistration, PlaywrightBrowserController
from invoice_browser_config import with_invoice_pages


def runner():
    value = object.__new__(adapter.PlaywrightAdapterRunner)
    value.recovery_identity = {"expected_store": "测试店", "expected_account": "测试店:操作员",
                               "expected_issuer": "测试公司"}
    value._cookie_recovery_attempted = False
    value._verified_contexts = {}
    value._operation_tasks = set()
    value.controllers = {"qianniu": Mock(), "jst": Mock()}
    value.progress = Mock()
    value.timeout_ms = 1000
    value.skill_dir = Path(__file__).resolve().parent
    return value


class CookieRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_interrupt_cancels_startup_before_releasing_lock(self):
        config = {"user_data_dir": "synthetic", "browser_sessions": {
            "invoice": "https://myseller.taobao.com/home.htm/merchant-invoice/",
            "orders": "https://myseller.taobao.com/home.htm/trade-platform/tp/sold",
            "goods": "https://fp.erp321.com/setting/goodsManage"}}
        with patch.object(adapter, "load_browser_config", return_value=(config, Path("synthetic"))), \
                patch.object(adapter.PlaywrightAdapterRunner, "_start"):
            value = adapter.PlaywrightAdapterRunner(Path("synthetic"), recovery_identity={"expected_store": "测试店"})
        entered = threading.Event()
        events = []
        async def boot():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("startup-cancelled")
        async def release():
            events.append("close")
        value._boot = AsyncMock(side_effect=boot)
        value._created_controllers = [Mock(close=AsyncMock(side_effect=release))]
        def interrupt(**kwargs):
            self.assertTrue(entered.wait(timeout=2))
            raise KeyboardInterrupt()
        with patch.object(value.ready, "wait", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                value._start()
        self.assertEqual(events, ["startup-cancelled", "close"])
        self.assertTrue(value._closed)
        self.assertFalse(value.thread.is_alive())

    async def test_home_configuration_with_extra_login_page_registers_after_recovery(self):
        value = runner()
        controller, invoice, orders, shared = self.context()
        extra_login = Mock(url="https://loginmyseller.taobao.com/", frames=[], main_frame=object())
        extra_login.is_closed.return_value = False
        extra_login.evaluate = AsyncMock(return_value={"verification": []})
        extra_login.goto = AsyncMock()
        controller.context.pages.append(extra_login)
        config = with_invoice_pages({"user_data_dir": "synthetic", "browser_sessions": {
            "home": "https://myseller.taobao.com/"}})
        real_controller = PlaywrightBrowserController(config)
        real_controller.context = controller.context
        real_controller._profile_lock = controller._profile_lock
        real_controller.registrations = controller.registrations
        real_controller.wait_for_business_ready = AsyncMock()
        for page in (invoice, orders):
            async def navigate(url, *, selected=page, **kwargs):
                selected.url = url
            page.goto.side_effect = navigate
        with patch.object(adapter, "_fetch_shop_cookies", return_value={"cookies": []}):
            await value._restore_cookie_session(real_controller, value.recovery_identity)
        self.assertEqual(set(real_controller.registrations), {"invoice", "orders"})
        self.assertEqual(set(real_controller.specs), {"invoice", "orders"})
        extra_login.goto.assert_not_awaited()
        shared.goto.assert_not_awaited()

    async def test_startup_keeps_same_context_during_recovery_and_never_restarts(self):
        value = object.__new__(PlaywrightBrowserController)
        value.context = object()
        original = value.context
        value.close = AsyncMock()
        error = BrowserControllerError("expired", "login_required", details={"role": "invoice"})
        value.register_roles = AsyncMock(side_effect=[error, {"ready": True}])
        async def recover(controller, source):
            self.assertIs(controller.context, original)
            self.assertIs(source, error)
            value.close.assert_not_awaited()
        value.login_recovery = AsyncMock(side_effect=recover)
        self.assertEqual(await value._register_with_login_recovery(launched=False, open_missing=True),
                         {"ready": True})
        value.login_recovery.assert_awaited_once()
        value.close.assert_not_awaited()

    async def test_shared_login_never_enters_startup_recovery(self):
        value = object.__new__(PlaywrightBrowserController)
        value.close = AsyncMock()
        value.login_recovery = AsyncMock()
        value.register_roles = AsyncMock(side_effect=BrowserControllerError(
            "expired", "login_required", details={"role": "goods"}))
        with self.assertRaises(BrowserControllerError):
            await value._register_with_login_recovery(launched=False, open_missing=True)
        value.login_recovery.assert_not_awaited()
        value.close.assert_awaited_once()

    async def test_expiry_rechecks_both_identities_then_repeats_only_read(self):
        value = runner()
        events = []
        async def invoke(*args):
            events.append("read")
            if events.count("read") == 1:
                raise adapter.PlaywrightAdapterError("expired", "auth_required", site="qianniu")
            return {"rows": []}
        async def restore(*args):
            events.append("cookies")
            value._cookie_recovery_attempted = True
        async def verify(*args):
            events.append("identities")
        value._invoke_once = AsyncMock(side_effect=invoke)
        value._restore_cookie_session = AsyncMock(side_effect=restore)
        value._reverify_after_recovery = AsyncMock(side_effect=verify)
        self.assertEqual(await value._invoke_async("qianniu", "applications", {}, False), {"rows": []})
        self.assertEqual(events, ["read", "cookies", "identities", "read"])

    async def test_writes_unknown_writes_shared_site_and_non_auth_never_recover(self):
        for site, operation, code in (("qianniu", "approve", "auth_required"),
                ("qianniu", "approval-status", "approval_unknown"), ("jst", "query", "auth_required"),
                ("qianniu", "orders", "verification_required"), ("qianniu", "orders", "rate_limited"),
                ("qianniu", "orders", "permission_required"), ("qianniu", "orders", "context_changed"),
                ("qianniu", "orders", "page_not_ready")):
            with self.subTest(site=site, operation=operation, code=code):
                value = runner()
                value._invoke_once = AsyncMock(side_effect=adapter.PlaywrightAdapterError("stop", code, site=site))
                value._restore_cookie_session = AsyncMock()
                with self.assertRaises(adapter.PlaywrightAdapterError):
                    await value._invoke_async(site, operation, {}, False)
                value._restore_cookie_session.assert_not_awaited()
                self.assertEqual(value._invoke_once.await_count, 1)

    async def test_retry_expiry_is_not_a_recovery_loop(self):
        value = runner()
        value._invoke_once = AsyncMock(side_effect=adapter.PlaywrightAdapterError("expired", "auth_required"))
        async def restore(*args):
            value._cookie_recovery_attempted = True
        value._restore_cookie_session = AsyncMock(side_effect=restore)
        value._reverify_after_recovery = AsyncMock()
        for _ in range(2):
            with self.assertRaises(adapter.PlaywrightAdapterError):
                await value._invoke_async("qianniu", "context", {}, False)
        self.assertEqual(value._restore_cookie_session.await_count, 1)
        self.assertEqual(value._invoke_once.await_count, 3)

    async def test_changed_tenant_stops_before_retry_and_keeps_original_identity(self):
        value = runner()
        value._verified_contexts = {
            "qianniu": {"store": "测试店", "agentId": "a", "observed_store": "测试店", "account_nick": "测试店:操作员"},
            "jst": {"issuer": "测试公司", "coid": "c", "uid": "u"}}
        original = {key: dict(item) for key, item in value._verified_contexts.items()}
        async def invoke(site, operation, *_args):
            if operation == "context":
                return original["qianniu"] if site == "qianniu" else {**original["jst"], "uid": "different"}
            raise adapter.PlaywrightAdapterError("expired", "auth_required")
        value._invoke_once = AsyncMock(side_effect=invoke)
        value._restore_cookie_session = AsyncMock()
        with self.assertRaises(adapter.PlaywrightAdapterError) as caught:
            await value._invoke_async("qianniu", "orders", {}, False)
        self.assertEqual(caught.exception.code, "context_changed")
        self.assertEqual(caught.exception.site, "jst")
        self.assertEqual(value._invoke_once.await_count, 3)
        self.assertEqual(value._verified_contexts, original)
        identity = value._invoke_once.await_args_list[1].args[2]
        self.assertEqual(identity["expected_account_nick"], "测试店:操作员")
        self.assertEqual(identity["agent_id"], "a")

    def context(self, challenge=False, injection_error=False):
        def page(url):
            item = Mock(url=url, frames=[], main_frame=object())
            item.is_closed.return_value = False
            item.evaluate = AsyncMock(return_value={"verification": ["slider"] if challenge else []})
            item.goto = AsyncMock()
            return item
        invoice = page("https://login.taobao.com/member/login.jhtml")
        orders = page("https://myseller.taobao.com/home.htm/trade-platform/tp/sold")
        shared = page("https://fp.erp321.com/setting/goodsManage")
        context = Mock(pages=[invoice, orders, shared], add_cookies=AsyncMock(), new_page=AsyncMock())
        if injection_error:
            context.add_cookies.side_effect = RuntimeError("SECRET_COOKIE_VALUE")
        controller = Mock(context=context, registrations={"invoice": PageRegistration("invoice", invoice),
            "orders": PageRegistration("orders", orders)},
            specs={"invoice": SimpleNamespace(url="https://myseller.taobao.com/home.htm/merchant-invoice/"),
                   "orders": SimpleNamespace(url=orders.url)},
            wait_for_business_ready=AsyncMock(), register_roles=AsyncMock())
        controller._profile_lock = SimpleNamespace(owned=True)
        return controller, invoice, orders, shared

    async def test_injects_existing_context_and_navigates_only_qianniu_roles(self):
        value = runner()
        controller, invoice, orders, shared = self.context()
        material = {"cookies": [{"name": "session", "value": "SECRET_COOKIE_VALUE",
                                  "domain": ".taobao.com", "path": "/"}], "source": {"row": 3}}
        with patch.object(adapter, "_fetch_shop_cookies", return_value=material) as fetch:
            await value._restore_cookie_session(controller, value.recovery_identity)
        fetch.assert_called_once_with("测试店", "测试店:操作员")
        controller.context.add_cookies.assert_awaited_once_with(material["cookies"])
        invoice.goto.assert_awaited_once()
        orders.goto.assert_awaited_once()
        shared.goto.assert_not_awaited()
        shared.evaluate.assert_not_awaited()
        controller.context.new_page.assert_not_awaited()
        self.assertNotIn("SECRET_COOKIE_VALUE", str(value.progress.call_args_list))

    async def test_challenge_blocks_fetch_and_injection(self):
        value = runner()
        controller, *_ = self.context(challenge=True)
        with patch.object(adapter, "_fetch_shop_cookies") as fetch:
            with self.assertRaises(adapter.PlaywrightAdapterError) as caught:
                await value._restore_cookie_session(controller, value.recovery_identity)
        self.assertEqual(caught.exception.code, "verification_required")
        fetch.assert_not_called()
        controller.context.add_cookies.assert_not_awaited()

    async def test_cookie_injection_error_is_redacted(self):
        value = runner()
        controller, *_ = self.context(injection_error=True)
        with patch.object(adapter, "_fetch_shop_cookies", return_value={"cookies": []}):
            with self.assertRaises(adapter.PlaywrightAdapterError) as caught:
                await value._restore_cookie_session(controller, value.recovery_identity)
        self.assertNotIn("SECRET_COOKIE_VALUE", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_lost_lock_or_context_during_fetch_never_injects(self):
        for change in ("lock", "context", "closing"):
            with self.subTest(change=change):
                value = runner()
                controller, *_ = self.context()
                original = controller.context
                def fetch(*args):
                    if change == "lock":
                        controller._profile_lock.owned = False
                    elif change == "context":
                        controller.context = Mock()
                    else:
                        value._closed = True
                    return {"cookies": []}
                with patch.object(adapter, "_fetch_shop_cookies", side_effect=fetch):
                    with self.assertRaises(adapter.PlaywrightAdapterError):
                        await value._restore_cookie_session(controller, value.recovery_identity)
                original.add_cookies.assert_not_awaited()

    async def test_shutdown_cancels_recovery_before_releasing_profile(self):
        value = runner()
        controller, *_ = self.context()
        value.controllers = {"qianniu": controller}
        value._created_controllers = [controller]
        value._cleanup_tasks = set()
        value._operation_targets = {}
        entered, waiting = asyncio.Event(), asyncio.Event()
        events = []
        async def waiting_fetch(*args):
            entered.set()
            try:
                await waiting.wait()
            finally:
                events.append("recovery-cancelled")
        async def close():
            events.append("close")
        controller.close = AsyncMock(side_effect=close)
        value._invoke_once = AsyncMock(side_effect=adapter.PlaywrightAdapterError("expired", "auth_required"))
        value._reverify_after_recovery = AsyncMock()
        with patch.object(adapter.asyncio, "to_thread", side_effect=waiting_fetch):
            task = asyncio.create_task(value._invoke_async("qianniu", "context", {}, False))
            await entered.wait()
            self.assertIn(task, value._operation_tasks)
            await value._shutdown()
        self.assertTrue(task.cancelled())
        self.assertEqual(events, ["recovery-cancelled", "close"])
        controller.context.add_cookies.assert_not_awaited()
        value._reverify_after_recovery.assert_not_awaited()
        self.assertEqual(value._invoke_once.await_count, 1)


if __name__ == "__main__":
    unittest.main()
