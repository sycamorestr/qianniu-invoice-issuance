from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import playwright_adapter
from playwright_controller import BrowserControllerError, ProfileLock


ORDERS_URL = "https://myseller.taobao.com/home.htm/trade-platform/tp/sold"
ROLE_URLS = {
    "invoice": "https://myseller.taobao.com/home.htm/merchant-invoice/",
    "orders": ORDERS_URL,
    "goods": "https://fp.erp321.com/setting/goodsManage",
}


def config_for(roles, data_dir):
    return {"schema_version": 1, "browser": "Edge", "user_data_dir": str(data_dir),
            "browser_sessions": {role: {"url": ROLE_URLS[role]} for role in roles}}


def write_config(root, name, roles, directory):
    path = Path(root) / f"{name}.json"
    path.write_text(json.dumps(config_for(roles, Path(root) / directory)), encoding="utf-8")
    return path


class FakePage:
    def __init__(self) -> None:
        self.url = ORDERS_URL
        self.goto_calls: list[tuple[str, dict]] = []
        self.wait_calls: list[tuple[str, str, int | None]] = []

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append((url, kwargs))
        self.url = url

    async def wait_for_function(self, expression: str, *, arg=None, timeout=None) -> None:
        self.wait_calls.append((expression, arg, timeout))


class FakeController:
    def __init__(self, page: FakePage, value) -> None:
        self.page_value = page
        self.value = value
        self.evaluate_calls: list[tuple[str, Path, dict]] = []

    def page(self, role: str):
        assert role == "orders"
        return self.page_value

    async def evaluate_file(self, role: str, source_path: Path, payload: dict):
        self.evaluate_calls.append((role, source_path, payload))
        return self.value


def make_runner(controller: FakeController) -> playwright_adapter.PlaywrightAdapterRunner:
    runner = object.__new__(playwright_adapter.PlaywrightAdapterRunner)
    runner.controller = controller
    runner.controllers = {"qianniu": controller, "jst": controller}
    runner._operation_tasks = set()
    runner.config = {"browser_sessions": {"orders": {"url": ORDERS_URL}}}
    runner.skill_dir = SCRIPT_DIR
    runner.timeout_ms = 30_000
    runner._closed = False
    runner.loop = object()
    return runner


class PlaywrightAdapterTests(unittest.TestCase):
    def test_daily_adapter_restores_missing_pages_without_starting_browser(self):
        controller = type("AttachedController", (), {
            "connect": AsyncMock(), "close": AsyncMock(), "start": AsyncMock(),
        })()
        config = config_for(ROLE_URLS, "unused")
        with patch.object(playwright_adapter, "load_browser_config", return_value=(config, Path("unused"))), \
                patch.object(playwright_adapter, "PlaywrightBrowserController", return_value=controller):
            runner = playwright_adapter.PlaywrightAdapterRunner(Path("unused"))
            runner.close()
        controller.connect.assert_awaited_once_with(open_missing=True)
        controller.start.assert_not_awaited()

    def test_detail_waits_for_exact_order_and_ready_goods_code_row(self):
        order_no = "9000000000000000001"
        page = FakePage()
        controller = FakeController(
            page,
            {"order_no": order_no, "verified_order": True, "items": []},
        )
        runner = make_runner(controller)

        value = asyncio.run(runner._invoke_async(
            "qianniu", "detail", {"order_no": order_no}, False
        ))

        self.assertEqual(value["order_no"], order_no)
        self.assertEqual(len(page.goto_calls), 2)
        self.assertIn("bizOrderId=" + order_no, page.goto_calls[0][0])
        self.assertEqual(page.goto_calls[1][0], ORDERS_URL)
        self.assertEqual(len(page.wait_calls), 1)
        expression, arg, timeout = page.wait_calls[0]
        self.assertEqual(arg, order_no)
        self.assertEqual(timeout, 10_000)
        self.assertIn("new URL(location.href).searchParams.get('bizOrderId')", expression)
        self.assertIn("querySelectorAll('tr')", expression)
        self.assertIn("商家编码", expression)
        self.assertIn("__reactFiber$", expression)
        self.assertEqual(controller.evaluate_calls[0][1].name, "read_order_detail.js")

    def test_orders_rejects_non_string_request_before_page_script(self):
        page = FakePage()
        controller = FakeController(page, None)
        runner = make_runner(controller)

        with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
            asyncio.run(runner._invoke_async(
                "qianniu", "orders", {"orders": [9000000000000000001]}, False
            ))

        self.assertEqual(error.exception.code, "configuration")
        self.assertEqual(controller.evaluate_calls, [])

    def test_orders_response_must_map_exactly_to_requested_ids(self):
        page = FakePage()
        controller = FakeController(
            page,
            {
                "batches": [{"order_ids": ["A"]}],
                "items": [],
                "missing": [],
            },
        )
        runner = make_runner(controller)

        with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
            asyncio.run(runner._invoke_async(
                "qianniu", "orders", {"orders": ["A", "B"]}, False
            ))

        self.assertEqual(error.exception.code, "response_invalid")

    def test_orders_response_accepts_complete_string_mapping(self):
        page = FakePage()
        response = {
            "batches": [{"order_ids": ["A"]}],
            "items": [{"order_no": "A", "sub_order_no": "A-1"}],
            "missing": ["B"],
        }
        controller = FakeController(page, response)
        runner = make_runner(controller)

        value = asyncio.run(runner._invoke_async(
            "qianniu", "orders", {"orders": ["A", "B"]}, False
        ))

        self.assertIs(value, response)

    def test_goods_query_uses_frame_and_structured_result(self):
        response = {"ok": True, "data": [{"input_goods_code": "TEST-SKU"}]}
        controller = FakeController(FakePage(), response)
        controller.evaluate_file = AsyncMock(return_value=response)
        runner = make_runner(controller)
        payload = {"codes": ["TEST-SKU"], "context": {"coid": "test", "uid": "test"}}

        value = asyncio.run(runner._invoke_async("jst", "query", payload, False))

        self.assertIs(value, response)
        controller.evaluate_file.assert_awaited_once_with(
            "goods", SCRIPT_DIR / "query_jst_invoice_goods.js",
            {**payload, "operation": "query"},
            frame_url=playwright_adapter.GOODS_FRAME_URL,
        )

    def test_goods_query_rejects_non_object_result(self):
        controller = FakeController(FakePage(), None)
        controller.evaluate_file = AsyncMock(return_value='{"ok":true}')
        runner = make_runner(controller)
        with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
            asyncio.run(runner._invoke_async("jst", "query", {"codes": ["TEST-SKU"]}, False))
        self.assertEqual(error.exception.code, "response_invalid")

    def test_call_cancels_background_future_after_timeout(self):
        class TimedOutFuture:
            def __init__(self) -> None:
                self.cancelled = False

            def result(self, timeout):
                self.timeout = timeout
                raise FutureTimeoutError()

            def cancel(self):
                self.cancelled = True
                return True

        future = TimedOutFuture()
        runner = object.__new__(playwright_adapter.PlaywrightAdapterRunner)
        runner._closed = False
        runner.loop = object()
        runner.timeout_ms = 30_000
        runner.close = Mock()

        with patch.object(asyncio, "run_coroutine_threadsafe", return_value=future):
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                runner._call(object())

        self.assertTrue(future.cancelled)
        runner.close.assert_called_once()
        self.assertEqual(error.exception.code, "request_failed")

    def test_split_browser_configuration_rejects_mixed_roles_or_shared_data_root(self):
        with tempfile.TemporaryDirectory() as root:
            cases = (
                (("invoice", "orders", "goods"), ("goods",), "shop", "shared", "qianniu"),
                (("invoice", "orders"), ("goods", "orders"), "shop", "shared", "jst"),
                (("invoice", "orders"), ("goods",), "same", "same/../same", "jst"),
            )
            for shop_roles, goods_roles, shop_dir, goods_dir, site in cases:
                shop = write_config(root, "shop", shop_roles, shop_dir)
                goods = write_config(root, "goods", goods_roles, goods_dir)
                with patch.object(playwright_adapter, "PlaywrightBrowserController") as create:
                    with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                        playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods)
                self.assertEqual(error.exception.code, "configuration")
                self.assertEqual(error.exception.site, site)
                create.assert_not_called()

    def test_single_browser_requires_all_roles_without_shared_config(self):
        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("invoice", "orders"), "shop")
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                playwright_adapter.PlaywrightAdapterRunner(shop)
            self.assertEqual(error.exception.code, "configuration")
            self.assertEqual(error.exception.site, "qianniu")

    def test_split_start_uses_sorted_profiles_and_holds_both_until_close(self):
        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("invoice", "orders"), "z-shop")
            goods = write_config(root, "goods", ("goods",), "a-shared")
            events = []
            instances = {}

            def create(config, **_kwargs):
                site = "jst" if "goods" in config["browser_sessions"] else "qianniu"
                instance = Mock()
                async def connect(**kwargs):
                    self.assertEqual(kwargs, {"open_missing": True})
                    events.append(("connect", site))
                async def close():
                    events.append(("close", site))
                instance.connect = AsyncMock(side_effect=connect)
                instance.close = AsyncMock(side_effect=close)
                instance.status.return_value = {"roles": {role: {"target_id": f"target-{role}"}
                                                         for role in config["browser_sessions"]}}
                instances[site] = instance
                return instance

            with patch.object(playwright_adapter, "PlaywrightBrowserController", side_effect=create):
                runner = playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods)
                try:
                    self.assertEqual(events, [("connect", "jst"), ("connect", "qianniu")])
                    self.assertEqual(runner._call(runner._pages_receipt()),
                                     {role: f"target-{role}" for role in ROLE_URLS})
                    for instance in instances.values():
                        instance.close.assert_not_awaited()
                finally:
                    runner.close()
                self.assertFalse(runner.thread.is_alive())
            self.assertEqual(events[-2:], [("close", "qianniu"), ("close", "jst")])
            for instance in instances.values():
                instance.stop.assert_not_called()

    def test_second_browser_connect_failure_detaches_both_and_preserves_site(self):
        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("invoice", "orders"), "a-shop")
            goods = write_config(root, "goods", ("goods",), "z-shared")
            first = Mock(connect=AsyncMock(), close=AsyncMock())
            second = Mock(connect=AsyncMock(side_effect=BrowserControllerError("共享会话失效", "login_required")),
                          close=AsyncMock())
            with patch.object(playwright_adapter, "PlaywrightBrowserController", side_effect=[first, second]):
                with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                    playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods)
            self.assertEqual(error.exception.code, "login_required")
            self.assertEqual(error.exception.site, "jst")
            first.close.assert_awaited_once()
            second.close.assert_awaited_once()
            first.stop.assert_not_called()
            second.stop.assert_not_called()

    def test_shared_profile_mutex_covers_whole_job_and_failed_shop_releases_its_lock(self):
        class LockedController:
            def __init__(self, config, **_kwargs):
                self.lock = ProfileLock(Path(config["user_data_dir"]), "Default")

            async def connect(self, **_kwargs):
                self.lock.acquire()

            async def close(self):
                self.lock.release()

        with tempfile.TemporaryDirectory() as root:
            shop1 = write_config(root, "shop1", ("invoice", "orders"), "a-shop1")
            shop2 = write_config(root, "shop2", ("invoice", "orders"), "b-shop2")
            goods = write_config(root, "goods", ("goods",), "z-shared")
            with patch.object(playwright_adapter, "PlaywrightBrowserController", LockedController):
                first = playwright_adapter.PlaywrightAdapterRunner(shop1, jst_config_path=goods)
                try:
                    with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                        playwright_adapter.PlaywrightAdapterRunner(shop2, jst_config_path=goods)
                    self.assertEqual(error.exception.site, "jst")
                    self.assertEqual(error.exception.code, "profile_locked")
                    own_shop_lock = ProfileLock(Path(root) / "b-shop2", "Default")
                    own_shop_lock.acquire()
                    own_shop_lock.release()
                    self.assertTrue(first.controllers["jst"].lock.owned)
                finally:
                    first.close()
                second = playwright_adapter.PlaywrightAdapterRunner(shop2, jst_config_path=goods)
                second.close()

    def test_startup_timeout_cancels_second_connection_and_releases_both(self):
        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("invoice", "orders"), "a-shop")
            goods = write_config(root, "goods", ("goods",), "z-shared")
            cancelled = []
            async def never_ready(**_kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            first = Mock(connect=AsyncMock(), close=AsyncMock())
            second = Mock(connect=AsyncMock(side_effect=never_ready), close=AsyncMock())
            with patch.object(playwright_adapter, "PlaywrightBrowserController", side_effect=[first, second]):
                with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                    playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods, timeout_ms=5)
            self.assertEqual(error.exception.site, "jst")
            self.assertEqual(error.exception.code, "browser_launch_failed")
            self.assertEqual(cancelled, [True])
            first.close.assert_awaited_once()
            second.close.assert_awaited_once()

    def test_split_context_query_and_detail_route_to_their_own_browser(self):
        order_no = "9000000000000000001"
        order_page = FakePage()
        goods_page = Mock(url=ROLE_URLS["goods"], evaluate=AsyncMock(return_value="示例公司[测试员]"))
        shop = FakeController(order_page, None)
        goods = Mock()
        shop.status = Mock(return_value={"roles": {role: {"target_id": "shop-" + role}
                                                  for role in ("invoice", "orders")}})
        goods.status.return_value = {"roles": {"goods": {"target_id": "shared-goods"}}}
        goods.page.return_value = goods_page

        async def read_shop(role, source, payload, **_kwargs):
            if source.name == "playwright_context_qianniu.js":
                return {"isLogin": True, "store": "示例店铺"}
            if source.name == "read_order_detail.js":
                return {"order_no": order_no, "verified_order": True, "items": []}
            return {"rows": []}

        async def read_goods(role, source, payload, **_kwargs):
            if source.name == "playwright_context_jst.js":
                return {"coid": "test-company", "uid": "test-user"}
            return {"ok": True, "data": []}

        shop.evaluate_file = AsyncMock(side_effect=read_shop)
        goods.evaluate_file = AsyncMock(side_effect=read_goods)
        runner = make_runner(shop)
        runner.controllers = {"qianniu": shop, "jst": goods}

        async def exercise():
            qianniu = await runner._invoke_async("qianniu", "context", {}, False)
            jst = await runner._invoke_async("jst", "context", {"issuer": "示例公司"}, False)
            await runner._invoke_async("qianniu", "applications", {}, False)
            await runner._invoke_async("qianniu", "detail", {"order_no": order_no}, False)
            await runner._invoke_async("jst", "query", {"codes": ["TEST-SKU"]}, False)
            return qianniu, jst

        qianniu, jst = asyncio.run(exercise())
        self.assertEqual(qianniu["browser_pages"], {"invoice": "shop-invoice", "orders": "shop-orders"})
        self.assertEqual(jst["browser_pages"], {"goods": "shared-goods"})
        self.assertEqual(jst["issuer"], "示例公司")
        self.assertEqual([call.args[0] for call in shop.evaluate_file.await_args_list],
                         ["invoice", "invoice", "orders"])
        self.assertEqual([call.args[0] for call in goods.evaluate_file.await_args_list], ["goods", "goods"])
        self.assertEqual(order_page.url, ORDERS_URL)
        goods.page.assert_called_once_with("goods")

    def test_operation_errors_preserve_site_for_shared_failure_classification(self):
        shop = FakeController(FakePage(), None)
        goods = Mock(evaluate_file=AsyncMock(side_effect=BrowserControllerError("会话失效", "auth_required")))
        runner = make_runner(shop)
        runner.controllers["jst"] = goods
        with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
            asyncio.run(runner._invoke_async("jst", "query", {"codes": ["TEST-SKU"]}, False))
        self.assertEqual(error.exception.code, "auth_required")
        self.assertEqual(error.exception.site, "jst")

    def test_call_preserves_typed_controller_error(self):
        class FailedFuture:
            def result(self, timeout):
                raise BrowserControllerError("缺少页面", "page_missing")

        runner = object.__new__(playwright_adapter.PlaywrightAdapterRunner)
        runner._closed = False
        runner.loop = object()
        runner.timeout_ms = 30_000

        with patch.object(asyncio, "run_coroutine_threadsafe", return_value=FailedFuture()):
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                runner._call(object())

        self.assertEqual(error.exception.code, "page_missing")


if __name__ == "__main__":
    unittest.main()
