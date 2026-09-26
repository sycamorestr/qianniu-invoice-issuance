from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import playwright_adapter
from playwright_controller import BrowserControllerError


ORDERS_URL = "https://myseller.taobao.com/home.htm/trade-platform/tp/sold"


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
        config = {"user_data_dir": "unused"}
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

        with patch.object(asyncio, "run_coroutine_threadsafe", return_value=future):
            with self.assertRaises(playwright_adapter.PlaywrightAdapterError) as error:
                runner._call(object())

        self.assertTrue(future.cancelled)
        self.assertEqual(error.exception.code, "request_failed")

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
