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
HOME_URL = "https://myseller.taobao.com/"


def config_for(roles, data_dir):
    return {"schema_version": 1, "browser": "Edge", "user_data_dir": str(data_dir),
            "browser_sessions": {role: {"url": HOME_URL if role == "home" else ROLE_URLS[role]} for role in roles}}


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
    def test_daily_adapter_uses_native_start_entry_point_without_connect_fallback(self):
        controller = type("AttachedController", (), {
            "connect": AsyncMock(), "close": AsyncMock(), "start": AsyncMock(),
        })()
        config = config_for(ROLE_URLS, "unused")
        with patch.object(playwright_adapter, "load_browser_config", return_value=(config, Path("unused"))), \
                patch.object(playwright_adapter, "PlaywrightBrowserController", return_value=controller) as create:
            runner = playwright_adapter.PlaywrightAdapterRunner(Path("unused"))
            runner.close()
        self.assertEqual(create.call_args.kwargs["lock_wait_ms"], 5_000)
        controller.start.assert_awaited_once_with(open_missing=True)
        controller.connect.assert_not_awaited()
        controller.close.assert_awaited_once()

    def test_connect_only_adapter_never_calls_start(self):
        controller = Mock(connect=AsyncMock(), close=AsyncMock(), start=AsyncMock())
        config = config_for(ROLE_URLS, "unused")
        with patch.object(playwright_adapter, "load_browser_config", return_value=(config, Path("unused"))), \
                patch.object(playwright_adapter, "PlaywrightBrowserController", return_value=controller) as create:
            runner = playwright_adapter.PlaywrightAdapterRunner(Path("unused"), launch_if_needed=False)
            runner.close()
        self.assertEqual(create.call_args.kwargs["lock_wait_ms"], 5_000)
        controller.connect.assert_awaited_once_with(open_missing=True)
        controller.start.assert_not_awaited()

    def test_connect_only_failure_is_not_retried_as_native_start(self):
        controller = Mock(connect=AsyncMock(side_effect=BrowserControllerError("未启动", "browser_disconnected")),
                          close=AsyncMock(), start=AsyncMock())
        config = config_for(ROLE_URLS, "unused")
        with patch.object(playwright_adapter, "load_browser_config", return_value=(config, Path("unused"))), \
                patch.object(playwright_adapter, "PlaywrightBrowserController", return_value=controller):
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                playwright_adapter.PlaywrightAdapterRunner(Path("unused"), launch_if_needed=False)
        self.assertEqual(error.exception.code, "browser_disconnected")
        controller.connect.assert_awaited_once()
        controller.start.assert_not_awaited()
        controller.close.assert_awaited_once()

    def test_workbench_home_config_is_expanded_only_in_memory_and_shared_goods_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("home",), "shop")
            goods = write_config(root, "goods", ("goods",), "shared")
            original_shop, original_goods = shop.read_bytes(), goods.read_bytes()
            configurations = []
            def create(config, **_kwargs):
                configurations.append(config)
                return Mock(start=AsyncMock(), close=AsyncMock())
            with patch.object(playwright_adapter, "PlaywrightBrowserController", side_effect=create):
                runner = playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods)
                runner.close()
            self.assertEqual(shop.read_bytes(), original_shop)
            self.assertEqual(goods.read_bytes(), original_goods)
            self.assertEqual({frozenset(value["browser_sessions"]) for value in configurations},
                             {frozenset({"invoice", "orders"}), frozenset({"goods"})})
            qianniu = next(value for value in configurations if "invoice" in value["browser_sessions"])
            self.assertEqual(qianniu["browser_sessions"]["invoice"]["url"], ROLE_URLS["invoice"])
            self.assertEqual(qianniu["browser_sessions"]["orders"]["url"], ROLE_URLS["orders"])

    def test_invalid_invoice_config_conversion_is_typed_before_creating_controller(self):
        with patch.object(playwright_adapter, "load_browser_config", return_value=(config_for(ROLE_URLS, "unused"), Path("unused"))), \
                patch.object(playwright_adapter, "with_invoice_pages", side_effect=ValueError("无效主页配置")), \
                patch.object(playwright_adapter, "PlaywrightBrowserController") as create:
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                playwright_adapter.PlaywrightAdapterRunner(Path("unused"))
        self.assertEqual(error.exception.code, "configuration")
        self.assertEqual(error.exception.site, "qianniu")
        create.assert_not_called()

    def test_detail_waits_for_exact_order_and_complete_runtime_rows(self):
        order_no = "9000000000000000001"
        page = FakePage()
        controller = FakeController(
            page,
            {"order_no": order_no, "verified_order": True, "items": []},
        )
        runner = make_runner(controller)

        with patch.object(playwright_adapter.asyncio, "sleep", new_callable=AsyncMock):
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
        self.assertIn("runtime_order_snapshot_conflict", expression)
        self.assertIn("goods_code_missing", expression)
        self.assertEqual(controller.evaluate_calls[0][1].name, "read_order_detail.js")

    def test_verified_empty_goods_code_is_preserved_and_orders_page_restored(self):
        order_no = "9000000000000000001"
        page = FakePage()
        evidence = {"order_no": order_no, "verified_order": True, "items": [{
            "order_no": order_no, "sub_order_no": order_no, "title": "补差价测试",
            "quantity": "4800", "goods_code": "", "goods_code_missing": True,
            "price_cell": "0.01\n\nx4800", "unit_price": "0.01",
        }]}
        runner = make_runner(FakeController(page, evidence))
        with patch.object(playwright_adapter.asyncio, "sleep", new_callable=AsyncMock):
            value = asyncio.run(runner._invoke_async("qianniu", "detail", {"order_no": order_no}, False))
        self.assertEqual(value, evidence)
        self.assertEqual(len(page.wait_calls), 1)
        self.assertEqual([url for url, _ in page.goto_calls], [
            f"https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId={order_no}", ORDERS_URL])

    def test_incomplete_detail_wait_failure_leaves_page_without_more_requests(self):
        page = FakePage()
        page.wait_for_function = AsyncMock(side_effect=TimeoutError("incomplete DOM/runtime"))
        controller = FakeController(page, None)
        runner = make_runner(controller)
        with patch.object(playwright_adapter.asyncio, "sleep", new_callable=AsyncMock) as delay, \
                self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
            asyncio.run(runner._invoke_async("qianniu", "detail", {"order_no": "9000000000000000001"}, False))
        self.assertEqual(error.exception.code, "request_failed")
        self.assertEqual(controller.evaluate_calls, [])
        self.assertEqual(len(page.goto_calls), 1)
        self.assertIn("bizOrderId=9000000000000000001", page.url)
        delay.assert_awaited_once_with(3)

    def test_consecutive_details_wait_before_each_navigation(self):
        order_ids = ("9000000000000000001", "9000000000000000002")
        events = []
        clock = 0
        page = FakePage()
        controller = FakeController(page, None)
        runner = make_runner(controller)

        async def delay(seconds):
            nonlocal clock
            events.append(("wait", seconds))
            clock += seconds

        async def navigate(url, **kwargs):
            events.append(("navigate", clock, url))
            await FakePage.goto(page, url, **kwargs)

        async def collect(role, source, payload):
            events.append(("collect", clock, payload["order_no"]))
            return {"order_no": payload["order_no"], "verified_order": True, "items": []}

        page.goto = AsyncMock(side_effect=navigate)
        controller.evaluate_file = AsyncMock(side_effect=collect)

        async def exercise():
            for order_no in order_ids:
                await runner._invoke_async("qianniu", "detail", {"order_no": order_no}, False)

        with patch.object(playwright_adapter.asyncio, "sleep", side_effect=delay):
            asyncio.run(exercise())

        self.assertEqual(events, [
            ("wait", 3),
            ("navigate", 3, f"https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId={order_ids[0]}"),
            ("collect", 3, order_ids[0]),
            ("wait", 3), ("navigate", 6, ORDERS_URL),
            ("wait", 3),
            ("navigate", 9, f"https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId={order_ids[1]}"),
            ("collect", 9, order_ids[1]),
            ("wait", 3), ("navigate", 12, ORDERS_URL),
        ])

    def test_detail_navigation_or_collection_failure_never_restores_page(self):
        for failure_at in ("goto", "collector"):
            with self.subTest(failure_at=failure_at):
                page = FakePage()
                controller = FakeController(page, None)
                runner = make_runner(controller)
                if failure_at == "goto":
                    page.goto = AsyncMock(side_effect=TimeoutError("navigation failed"))
                else:
                    controller.evaluate_file = AsyncMock(side_effect=BrowserControllerError(
                        "Page.evaluate: Error: rate_limited", "script_failed"))
                with patch.object(playwright_adapter.asyncio, "sleep", new_callable=AsyncMock) as delay, \
                        self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                    asyncio.run(runner._invoke_async(
                        "qianniu", "detail", {"order_no": "9000000000000000001"}, False))
                delay.assert_awaited_once_with(3)
                if failure_at == "goto":
                    page.goto.assert_awaited_once()
                    self.assertEqual(controller.evaluate_calls, [])
                    self.assertEqual(page.wait_calls, [])
                    self.assertEqual(error.exception.code, "request_failed")
                else:
                    self.assertEqual(len(page.goto_calls), 1)
                    self.assertIn("bizOrderId=9000000000000000001", page.url)
                    self.assertEqual(error.exception.code, "rate_limited")
                self.assertEqual(error.exception.site, "qianniu")

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

    def test_order_rate_limit_is_typed_and_stops_after_one_collection_attempt(self):
        page = FakePage()
        source = BrowserControllerError("Page.evaluate: Error: rate_limited", "script_failed")
        controller = FakeController(page, None)
        controller.evaluate_file = AsyncMock(side_effect=source)
        runner = make_runner(controller)
        with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
            asyncio.run(runner._invoke_async("qianniu", "orders", {"orders": ["A"]}, False))
        self.assertEqual(error.exception.code, "rate_limited")
        self.assertEqual(error.exception.site, "qianniu")
        self.assertIs(error.exception.__cause__, source)
        controller.evaluate_file.assert_awaited_once()
        self.assertEqual(page.goto_calls, [])

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

    def test_sync_operations_allow_pacing_without_relaxing_unrelated_watchdogs(self):
        with tempfile.TemporaryDirectory() as root:
            input_path = Path(root) / "input.json"
            input_path.write_text("{}", encoding="utf-8")
            output_path = Path(root) / "output.json"
            runner = make_runner(FakeController(FakePage(), None))
            runner.progress = Mock()
            # A plain sentinel avoids creating an unawaited coroutine while
            # exercising the public adapter entry point and actual _call.
            runner._invoke_async = Mock(return_value=object())
            cases = (("qianniu", "orders", 120), ("qianniu", "detail", 90),
                     ("qianniu", "applications", 60), ("jst", "query", 60))
            for site, operation, expected_timeout in cases:
                with self.subTest(site=site, operation=operation):
                    future = Mock()
                    future.result.return_value = {"test": True}
                    with patch.object(asyncio, "run_coroutine_threadsafe", return_value=future):
                        result = runner(site, operation, input_path, output_path)
                    future.result.assert_called_once_with(timeout=expected_timeout)
                    self.assertEqual(result["payload"], {"test": True})

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
                async def start(**kwargs):
                    self.assertEqual(kwargs, {"open_missing": True})
                    events.append(("start", site))
                async def close():
                    events.append(("close", site))
                instance.start = AsyncMock(side_effect=start)
                instance.connect = AsyncMock()
                instance.close = AsyncMock(side_effect=close)
                instance.status.return_value = {"roles": {role: {"target_id": f"target-{role}"}
                                                         for role in config["browser_sessions"]}}
                instances[site] = instance
                return instance

            with patch.object(playwright_adapter, "PlaywrightBrowserController", side_effect=create):
                runner = playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods)
                try:
                    self.assertEqual(events, [("start", "jst"), ("start", "qianniu")])
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
                instance.connect.assert_not_awaited()

    def test_second_browser_connect_failure_detaches_both_and_preserves_site(self):
        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("invoice", "orders"), "a-shop")
            goods = write_config(root, "goods", ("goods",), "z-shared")
            first = Mock(start=AsyncMock(), close=AsyncMock())
            second = Mock(start=AsyncMock(side_effect=BrowserControllerError("共享会话失效", "login_required")),
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
            first.start.assert_awaited_once()
            second.start.assert_awaited_once()
            first.connect.assert_not_called()
            second.connect.assert_not_called()

    def test_shared_profile_mutex_covers_whole_job_and_failed_shop_releases_its_lock(self):
        class LockedController:
            def __init__(self, config, **_kwargs):
                self.lock = ProfileLock(Path(config["user_data_dir"]), "Default")

            async def start(self, **_kwargs):
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
            first = Mock(start=AsyncMock(), close=AsyncMock())
            second = Mock(start=AsyncMock(side_effect=never_ready), close=AsyncMock())
            with patch.object(playwright_adapter, "PlaywrightBrowserController", side_effect=[first, second]):
                with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                    playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods, timeout_ms=5)
            self.assertEqual(error.exception.site, "jst")
            self.assertEqual(error.exception.code, "browser_launch_failed")
            self.assertEqual(cancelled, [True])
            first.close.assert_awaited_once()
            second.close.assert_awaited_once()

    def test_native_start_timeout_releases_real_profile_locks_without_stopping_browsers(self):
        instances = []
        class LockedController:
            def __init__(self, config, **_kwargs):
                self.lock = ProfileLock(Path(config["user_data_dir"]), "Default")
                self.goods = "goods" in config["browser_sessions"]
                self.cancelled = False
                self.stop = Mock()
                instances.append(self)

            async def start(self, **_kwargs):
                self.lock.acquire()
                if self.goods:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.cancelled = True

            async def close(self):
                self.lock.release()

        with tempfile.TemporaryDirectory() as root:
            shop = write_config(root, "shop", ("invoice", "orders"), "a-shop")
            goods = write_config(root, "goods", ("goods",), "z-shared")
            with patch.object(playwright_adapter, "PlaywrightBrowserController", LockedController):
                with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                    playwright_adapter.PlaywrightAdapterRunner(shop, jst_config_path=goods, timeout_ms=10)
            self.assertEqual(error.exception.site, "jst")
            self.assertTrue(instances[1].cancelled)
            for instance in instances:
                self.assertFalse(instance.lock.owned)
                instance.lock.acquire()
                instance.lock.release()
                instance.stop.assert_not_called()

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

        with patch.object(playwright_adapter.asyncio, "sleep", new_callable=AsyncMock):
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

    def test_known_script_errors_are_typed_without_losing_site_or_original_cause(self):
        tokens = {
            "login_required": "auth_required",
            "permission_required": "permission_required",
            "rate_limited": "rate_limited",
            "context_changed_store": "context_changed",
            "context_changed_account": "context_changed",
            "context_missing_store": "context_missing",
            "context_missing_agent_id": "context_missing",
            "context_missing_account": "context_missing",
        }
        for site, operation in (("qianniu", "context"), ("jst", "query")):
            for token, expected in tokens.items():
                with self.subTest(site=site, token=token):
                    source = BrowserControllerError(
                        f"页面脚本执行失败: Page.evaluate: Error: {token}\n    at eval (eval:1:1)",
                        "script_failed",
                    )
                    controller = Mock(evaluate_file=AsyncMock(side_effect=source))
                    runner = make_runner(controller)
                    with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as caught:
                        asyncio.run(runner._invoke_async(site, operation, {}, False))
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(caught.exception.site, site)
                    self.assertEqual(str(caught.exception), str(source))
                    self.assertIs(caught.exception.__cause__, source)

    def test_unrecognized_script_errors_remain_fatal(self):
        messages = (
            "Page.evaluate: Error: login_required_extra",
            "Page.evaluate: Error: context_changed_storefront",
            "Page.evaluate: Error: context_missing_tenant",
            "Page.evaluate: TypeError: login_required",
            "Page.evaluate: Error: unknown_failure\n    Error: login_required",
            "Page.evaluate: Error: upstream response mentions login_required",
            "Page.evaluate: Error: 'login_required'",
            "Page.evaluate: Error: login_required: unrelated details",
            "Page.evaluate: Error: rate_limited_extra",
        )
        for message in messages:
            with self.subTest(message=message):
                controller = Mock(evaluate_file=AsyncMock(side_effect=BrowserControllerError(
                    "页面脚本执行失败: " + message, "script_failed")))
                runner = make_runner(controller)
                with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as caught:
                    asyncio.run(runner._invoke_async("qianniu", "context", {}, False))
                self.assertEqual(caught.exception.code, "script_failed")
                self.assertEqual(caught.exception.site, "qianniu")

    def test_error_text_does_not_override_an_existing_controller_error_code(self):
        controller = Mock(evaluate_file=AsyncMock(side_effect=BrowserControllerError(
            "Page.evaluate: Error: login_required", "page_missing")))
        runner = make_runner(controller)
        with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as caught:
            asyncio.run(runner._invoke_async("qianniu", "context", {}, False))
        self.assertEqual(caught.exception.code, "page_missing")

    def test_sync_call_classifies_known_script_error_and_preserves_site(self):
        class FailedFuture:
            def result(self, timeout):
                raise BrowserControllerError("Page.evaluate: Error: login_required", "script_failed")

        runner = object.__new__(playwright_adapter.PlaywrightAdapterRunner)
        runner._closed = False
        runner.loop = object()
        runner.timeout_ms = 30_000
        with patch.object(asyncio, "run_coroutine_threadsafe", return_value=FailedFuture()):
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as caught:
                runner._call(object(), site="jst")
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertEqual(caught.exception.site, "jst")

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
