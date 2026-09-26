"""Playwright adapter bridge used by ``run_online.py``.

The runner is synchronous, while Playwright's async API owns an event loop.
This module keeps one controller loop in a background thread for the whole
job, so all stages reuse the same three pages and browser context.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from playwright_controller import (
    BrowserControllerError,
    PlaywrightBrowserController,
    load_browser_config,
)


GOODS_FRAME_URL = "https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index"


class PlaywrightAdapterError(RuntimeError):
    def __init__(self, message: str, code: str = "adapter_failed") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlaywrightAdapterRunner:
    """Run the existing page scripts inside one Playwright context."""

    accepts_binary = True

    _DETAIL_READY_EXPRESSION = """
orderNo => {
  const expected = String(orderNo || '');
  let current = null;
  try {
    current = new URL(location.href).searchParams.get('bizOrderId');
  } catch (_) {
    return false;
  }
  if (!expected || current !== expected) return false;
  const bodyText = document.body?.innerText || '';
  if (!bodyText.includes(expected)) return false;
  return Array.from(document.querySelectorAll('tr')).some(row => {
    const rowText = row.innerText || '';
    if (!rowText.includes('商家编码')) return false;
    const firstCell = row.querySelector('td')?.innerText || '';
    const match = /商家编码[:：]\\s*([^\\n\\r]+)/.exec(firstCell);
    if (!match || !match[1].trim()) return false;
    return Object.getOwnPropertyNames(row).some(name => name.startsWith('__reactFiber$'));
  });
}
"""

    def __init__(self, config_path: Path, *, profile_directory: str | None = None,
                 timeout_ms: int = 30_000,
                 progress: Callable[[str], None] | None = None) -> None:
        self.config, self.config_path = load_browser_config(config_path, profile_directory=profile_directory)
        self.timeout_ms = timeout_ms
        self.progress = progress or (lambda _message: None)
        self.skill_dir = Path(__file__).resolve().parent
        self.loop: asyncio.AbstractEventLoop | None = None
        self.controller: PlaywrightBrowserController | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.start_error: BaseException | None = None
        self._closed = False
        self._start()

    def _start(self) -> None:
        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            try:
                async def boot() -> None:
                    self.controller = PlaywrightBrowserController(
                        self.config, timeout_ms=self.timeout_ms
                    )
                    await self.controller.connect(open_missing=True)
                loop.run_until_complete(boot())
            except BaseException as exc:  # publish to the caller, never hang ready
                self.start_error = exc
            finally:
                self.ready.set()
            if self.start_error is None:
                try:
                    loop.run_forever()
                finally:
                    loop.close()
            else:
                loop.close()

        self.thread = threading.Thread(target=worker, name="qianniu-playwright", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=max(10, self.timeout_ms / 1000 + 10)):
            raise PlaywrightAdapterError("Playwright controller 启动超时", "browser_launch_failed")
        if self.start_error is not None:
            self._raise_controller_error(self.start_error)

    def _raise_controller_error(self, error: BaseException) -> None:
        if isinstance(error, BrowserControllerError):
            raise PlaywrightAdapterError(str(error), error.code) from error
        raise PlaywrightAdapterError(str(error), "browser_launch_failed") from error

    def _call(self, coroutine: Any) -> Any:
        if self._closed or self.loop is None:
            raise PlaywrightAdapterError("Playwright controller 已关闭", "browser_disconnected")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=max(30, self.timeout_ms / 1000 + 30))
        except FutureTimeoutError as exc:
            # A timed-out detail read must not keep running against the shared
            # orders page while the coordinator advances to another stage.
            future.cancel()
            raise PlaywrightAdapterError("Playwright 页面操作超时", "request_failed") from exc
        except Exception as exc:
            if isinstance(exc, BrowserControllerError):
                self._raise_controller_error(exc)
            if isinstance(exc, PlaywrightAdapterError):
                raise
            raise PlaywrightAdapterError(f"Playwright 页面操作失败: {exc}", "request_failed") from exc

    async def _pages_receipt(self) -> dict[str, Any]:
        assert self.controller is not None
        result = self.controller.status()
        return {role: value.get("target_id") for role, value in result.get("roles", {}).items()}

    async def _context_qianniu(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.controller is not None
        value = await self.controller.evaluate_file(
            "invoice", self.skill_dir / "playwright_context_qianniu.js", payload
        )
        pages = await self._pages_receipt()
        value["browser_pages"] = {"invoice": pages.get("invoice"),
                                   "orders": pages.get("orders")}
        return value

    async def _context_jst(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.controller is not None
        page = self.controller.page("goods")
        label = await page.evaluate("""() => {
          const values = [...document.querySelectorAll('.companyInfoBox__name')]
            .map(e => (e.textContent || '').trim()).filter(Boolean);
          if (values.length === 1) return values[0];
          const match = (document.body?.innerText || '').match(/([^\\n]*?(?:有限公司|公司)(?:\\[[^\\]]*\\])?)/);
          return match ? match[1].trim() : '';
        }""")
        if not label:
            raise PlaywrightAdapterError("票聚主体未就绪", "context_missing")
        issuer = str(label).rsplit("[", 1)[0].strip()
        expected = payload.get("expected_issuer", payload.get("issuer"))
        if expected and str(expected) not in {issuer, str(label)}:
            raise PlaywrightAdapterError("票聚主体与指定主体不一致", "context_changed")
        frame_value = await self.controller.evaluate_file(
            "goods", self.skill_dir / "playwright_context_jst.js", payload,
            frame_url=GOODS_FRAME_URL,
        )
        pages = await self._pages_receipt()
        return {"isLogin": True, "is_login": True, "issuer": issuer,
                "issuer_label": str(label), "coid": str(frame_value["coid"]),
                "uid": str(frame_value["uid"]), "jst_url": str(page.url),
                "checked_at": frame_value.get("checked_at") or _now(),
                "browser_pages": {"goods": pages.get("goods")}}

    @staticmethod
    def _validate_orders_request(payload: dict[str, Any]) -> list[str]:
        values = payload.get("orders")
        if (not isinstance(values, list) or not values or len(values) > 50
                or any(type(value) is not str or not value.strip() for value in values)
                or len(set(values)) != len(values)):
            raise PlaywrightAdapterError(
                "orders 必须是 1 到 50 个不重复的字符串订单号", "configuration"
            )
        return values

    @staticmethod
    def _validate_orders_response(value: Any, requested: list[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlaywrightAdapterError("千牛订单查询返回不是对象", "response_invalid")
        batches = value.get("batches")
        items = value.get("items")
        missing = value.get("missing")
        if (not isinstance(batches, list) or not isinstance(items, list)
                or not isinstance(missing, list)):
            raise PlaywrightAdapterError("千牛订单查询缺少批次、明细或 missing", "response_invalid")

        requested_set = set(requested)
        returned: set[str] = set()
        for batch in batches:
            if not isinstance(batch, dict) or not isinstance(batch.get("order_ids"), list):
                raise PlaywrightAdapterError("千牛订单批次缺少 order_ids", "response_invalid")
            for order_no in batch["order_ids"]:
                if (type(order_no) is not str or order_no not in requested_set
                        or order_no in returned):
                    raise PlaywrightAdapterError(
                        "千牛订单批次包含未请求或重复的订单号", "response_invalid"
                    )
                returned.add(order_no)

        for item in items:
            if not isinstance(item, dict):
                raise PlaywrightAdapterError("千牛订单明细不是对象", "response_invalid")
            order_no = item.get("order_no")
            sub_order_no = item.get("sub_order_no")
            if (type(order_no) is not str or order_no not in requested_set
                    or type(sub_order_no) is not str or not sub_order_no.strip()):
                raise PlaywrightAdapterError(
                    "千牛订单明细的订单号或子订单号无效", "response_invalid"
                )

        if any(type(order_no) is not str or order_no not in requested_set
               or order_no in returned for order_no in missing):
            raise PlaywrightAdapterError("千牛订单 missing 范围无效", "response_invalid")
        missing_set = set(missing)
        if len(missing_set) != len(missing):
            raise PlaywrightAdapterError("千牛订单 missing 含重复订单号", "response_invalid")
        if returned | missing_set != requested_set or returned & missing_set:
            raise PlaywrightAdapterError("千牛订单返回范围与请求不一致", "response_invalid")
        return value

    async def _invoke_async(self, site: str, operation: str, payload: dict[str, Any],
                            binary: bool) -> Any:
        assert self.controller is not None
        payload = {**payload, "operation": operation}
        if site == "qianniu" and operation == "context":
            return await self._context_qianniu(payload)
        if site == "jst" and operation == "context":
            return await self._context_jst(payload)
        if site == "qianniu":
            role = "orders" if operation in {"orders", "detail"} else "invoice"
            if operation == "orders":
                requested_orders = self._validate_orders_request(payload)
                value = await self.controller.evaluate_file(
                    role, self.skill_dir / "read_qianniu.js", payload
                )
                return self._validate_orders_response(value, requested_orders)
            if operation == "detail":
                order_no = str(payload.get("order_no") or "").strip()
                if not order_no:
                    raise PlaywrightAdapterError("detail 缺少 order_no", "configuration")
                page = self.controller.page("orders")
                target = f"https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId={order_no}"
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    # The detail shell mounts asynchronously after the route
                    # resolves.  The collector also requires a mounted React
                    # row with a non-empty 商家编码, so wait for that exact
                    # readiness contract before evaluating it.
                    await page.wait_for_function(
                        self._DETAIL_READY_EXPRESSION,
                        arg=order_no,
                        timeout=min(self.timeout_ms, 10_000),
                    )
                    return await self.controller.evaluate_file(role, self.skill_dir / "read_order_detail.js", payload)
                finally:
                    restore = self.config["browser_sessions"]["orders"]
                    restore_url = restore.get("url") if isinstance(restore, dict) else restore
                    await page.goto(str(restore_url), wait_until="domcontentloaded", timeout=self.timeout_ms)
            source = self.skill_dir / "read_qianniu.js"
            value = await self.controller.evaluate_file(role, source, payload)
            if binary:
                if not isinstance(value, dict) or not isinstance(value.get("base64"), str):
                    raise PlaywrightAdapterError("千牛导出没有返回 XLSX 字节", "response_invalid")
                return base64.b64decode(value["base64"])
            return value
        if site == "jst" and operation == "query":
            value = await self.controller.evaluate_file(
                "goods", self.skill_dir / "query_jst_invoice_goods.js", payload,
                frame_url=GOODS_FRAME_URL,
            )
            if not isinstance(value, dict):
                raise PlaywrightAdapterError("票聚返回不是对象", "response_invalid")
            return value
        raise PlaywrightAdapterError(f"不支持的 Playwright 操作: {site}/{operation}", "configuration")

    def __call__(self, site: str, operation: str, input_path: Path, output_path: Path,
                 binary: bool = False) -> dict[str, Any]:
        payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
        self.progress(f"playwright {site}/{operation}")
        value = self._call(self._invoke_async(site, operation, payload, binary))
        if binary and isinstance(value, (bytes, bytearray)):
            value = base64.b64encode(bytes(value)).decode("ascii")
        return {"operation": operation, "payload": value, "outputPath": str(output_path),
                "observedAt": _now(), "browser_backend": "playwright"}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.loop is not None and self.controller is not None:
            future = asyncio.run_coroutine_threadsafe(self.controller.close(), self.loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=10)
