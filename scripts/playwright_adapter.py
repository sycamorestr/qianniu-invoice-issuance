"""Playwright adapter bridge used by ``run_online.py``.

The runner is synchronous, while Playwright's async API owns an event loop.
This module keeps one controller loop in a background thread for the whole
job. The three page roles may share one browser or use separate Qianniu and
JST browsers; each browser's profile lock is held for the entire job.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import os
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
    def __init__(self, message: str, code: str = "adapter_failed", *,
                 site: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.site = site


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlaywrightAdapterRunner:
    """Run page scripts in one or two persistent browser contexts."""

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

    def __init__(self, config_path: Path, *, jst_config_path: Path | None = None,
                 profile_directory: str | None = None,
                 timeout_ms: int = 30_000,
                 progress: Callable[[str], None] | None = None) -> None:
        try:
            self.config, self.config_path = load_browser_config(config_path, profile_directory=profile_directory)
        except BrowserControllerError as exc:
            raise PlaywrightAdapterError(str(exc), exc.code, site="qianniu") from exc
        self.jst_config: dict[str, Any] | None = None
        self.jst_config_path: Path | None = None
        if jst_config_path is not None:
            try:
                self.jst_config, self.jst_config_path = load_browser_config(jst_config_path)
            except BrowserControllerError as exc:
                raise PlaywrightAdapterError(str(exc), exc.code, site="jst") from exc
            expected_roles = (("qianniu", self.config, {"invoice", "orders"}),
                              ("jst", self.jst_config, {"goods"}))
            for site, config, expected in expected_roles:
                if set(config["browser_sessions"]) != expected:
                    raise PlaywrightAdapterError(
                        f"{site} 独立浏览器配置必须仅包含角色: {', '.join(sorted(expected))}",
                        "configuration", site=site,
                    )
            if self._profile_key(self.config) == self._profile_key(self.jst_config):
                raise PlaywrightAdapterError(
                    "千牛与共享票聚必须使用不同的 user_data_dir", "configuration", site="jst"
                )
        elif set(self.config["browser_sessions"]) != {"invoice", "orders", "goods"}:
            raise PlaywrightAdapterError(
                "单浏览器配置必须包含 invoice、orders、goods 三个角色", "configuration", site="qianniu"
            )
        self.timeout_ms = timeout_ms
        self.progress = progress or (lambda _message: None)
        self.skill_dir = Path(__file__).resolve().parent
        self.loop: asyncio.AbstractEventLoop | None = None
        self.controller: PlaywrightBrowserController | None = None
        self.controllers: dict[str, PlaywrightBrowserController] = {}
        self._created_controllers: list[PlaywrightBrowserController] = []
        self._operation_tasks: set[asyncio.Task[Any]] = set()
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.start_error: BaseException | None = None
        self._startup_task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self._starting_site: str | None = None
        self._shutdown_requested = threading.Event()
        self._closed = False
        self._start()

    @staticmethod
    def _profile_key(config: dict[str, Any]) -> str:
        return os.path.normcase(str(Path(config["user_data_dir"]).expanduser().resolve()))

    async def _boot(self) -> None:
        configurations = [("qianniu", self.config)]
        if self.jst_config is not None:
            configurations.append(("jst", self.jst_config))
        # Deterministic lock acquisition prevents opposite-order deadlocks
        # when several shops use the same shared JST browser.
        for site, config in sorted(configurations, key=lambda item: self._profile_key(item[1])):
            self._starting_site = site
            try:
                controller = PlaywrightBrowserController(config, timeout_ms=self.timeout_ms)
                self._created_controllers.append(controller)
                self.controllers[site] = controller
                if site == "qianniu":
                    self.controller = controller
                await asyncio.wait_for(
                    controller.connect(open_missing=True), timeout=max(0.001, self.timeout_ms / 1000)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, BrowserControllerError) else "browser_launch_failed"
                raise PlaywrightAdapterError(str(exc) or "浏览器连接超时", code, site=site) from exc
        if self.jst_config is None:
            self.controllers["jst"] = self.controllers["qianniu"]
        self._starting_site = None

    async def _shutdown(self) -> None:
        # Cancel outstanding operations before detaching. Detail navigation
        # must not continue after this job has released either profile lock.
        # Playwright also owns background transport tasks in this loop. Leave
        # them running until controller.close() disconnects them cleanly.
        pending = [task for task in self._operation_tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        for controller in reversed(self._created_controllers):
            try:
                await asyncio.wait_for(controller.close(), timeout=10)
            except (Exception, asyncio.CancelledError):
                # close() releases its OS lock in finally even if detaching
                # a disconnected browser or stopping the runtime fails.
                pass

    def _start(self) -> None:
        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop

            async def serve() -> None:
                self._stop_event = asyncio.Event()
                self._startup_task = loop.create_task(self._boot())
                try:
                    await self._startup_task
                except BaseException as exc:  # publish to caller, never hang ready
                    self.start_error = exc
                finally:
                    self.ready.set()
                try:
                    if self.start_error is None and not self._shutdown_requested.is_set():
                        await self._stop_event.wait()
                finally:
                    await self._shutdown()

            try:
                loop.run_until_complete(serve())
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self.thread = threading.Thread(target=worker, name="qianniu-playwright", daemon=True)
        self.thread.start()
        browser_count = 2 if self.jst_config is not None else 1
        if not self.ready.wait(timeout=max(10, browser_count * self.timeout_ms / 1000 + 10)):
            site = self._starting_site
            self.close()
            raise PlaywrightAdapterError("Playwright controller 启动超时", "browser_launch_failed", site=site)
        if self.start_error is not None:
            self.thread.join(timeout=30)
            self._raise_controller_error(self.start_error, site=self._starting_site)

    def _raise_controller_error(self, error: BaseException, *, site: str | None = None) -> None:
        if isinstance(error, PlaywrightAdapterError):
            raise error
        if isinstance(error, BrowserControllerError):
            raise PlaywrightAdapterError(str(error), error.code, site=site) from error
        raise PlaywrightAdapterError(str(error), "browser_launch_failed", site=site) from error

    def _call(self, coroutine: Any, *, site: str | None = None) -> Any:
        if self._closed or self.loop is None:
            if hasattr(coroutine, "close"):
                coroutine.close()
            raise PlaywrightAdapterError("Playwright controller 已关闭", "browser_disconnected", site=site)
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=max(30, self.timeout_ms / 1000 + 30))
        except FutureTimeoutError as exc:
            # A timed-out detail read must not keep running against the shared
            # orders page while the coordinator advances to another stage.
            future.cancel()
            self.close()
            raise PlaywrightAdapterError("Playwright 页面操作超时", "request_failed", site=site) from exc
        except Exception as exc:
            if isinstance(exc, BrowserControllerError):
                self._raise_controller_error(exc, site=site)
            if isinstance(exc, PlaywrightAdapterError):
                raise
            raise PlaywrightAdapterError(f"Playwright 页面操作失败: {exc}", "request_failed", site=site) from exc

    def _controller_for(self, site: str) -> PlaywrightBrowserController:
        controller = self.controllers.get(site)
        if controller is None:
            raise PlaywrightAdapterError("业务浏览器未连接", "browser_disconnected", site=site)
        return controller

    async def _pages_receipt(self) -> dict[str, Any]:
        pages: dict[str, Any] = {}
        for controller in dict.fromkeys(self.controllers.values()):
            result = controller.status()
            pages.update({role: value.get("target_id") for role, value in result.get("roles", {}).items()})
        return pages

    async def _context_qianniu(self, payload: dict[str, Any]) -> dict[str, Any]:
        controller = self._controller_for("qianniu")
        value = await controller.evaluate_file(
            "invoice", self.skill_dir / "playwright_context_qianniu.js", payload
        )
        pages = await self._pages_receipt()
        value["browser_pages"] = {"invoice": pages.get("invoice"),
                                   "orders": pages.get("orders")}
        return value

    async def _context_jst(self, payload: dict[str, Any]) -> dict[str, Any]:
        controller = self._controller_for("jst")
        page = controller.page("goods")
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
        frame_value = await controller.evaluate_file(
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
        task = asyncio.current_task()
        if task is not None:
            self._operation_tasks.add(task)
        try:
            return await self._invoke_for_site(site, operation, payload, binary)
        except PlaywrightAdapterError as exc:
            if exc.site is None:
                exc.site = site
            raise
        except BrowserControllerError as exc:
            raise PlaywrightAdapterError(str(exc), exc.code, site=site) from exc
        except Exception as exc:
            raise PlaywrightAdapterError(f"页面采集失败: {exc}", "request_failed", site=site) from exc
        finally:
            if task is not None:
                self._operation_tasks.discard(task)

    async def _invoke_for_site(self, site: str, operation: str, payload: dict[str, Any],
                               binary: bool) -> Any:
        controller = self._controller_for(site)
        payload = {**payload, "operation": operation}
        if site == "qianniu" and operation == "context":
            return await self._context_qianniu(payload)
        if site == "jst" and operation == "context":
            return await self._context_jst(payload)
        if site == "qianniu":
            role = "orders" if operation in {"orders", "detail"} else "invoice"
            if operation == "orders":
                requested_orders = self._validate_orders_request(payload)
                value = await controller.evaluate_file(
                    role, self.skill_dir / "read_qianniu.js", payload
                )
                return self._validate_orders_response(value, requested_orders)
            if operation == "detail":
                order_no = str(payload.get("order_no") or "").strip()
                if not order_no:
                    raise PlaywrightAdapterError("detail 缺少 order_no", "configuration")
                page = controller.page("orders")
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
                    return await controller.evaluate_file(role, self.skill_dir / "read_order_detail.js", payload)
                finally:
                    restore = self.config["browser_sessions"]["orders"]
                    restore_url = restore.get("url") if isinstance(restore, dict) else restore
                    await page.goto(str(restore_url), wait_until="domcontentloaded", timeout=self.timeout_ms)
            source = self.skill_dir / "read_qianniu.js"
            value = await controller.evaluate_file(role, source, payload)
            if binary:
                if not isinstance(value, dict) or not isinstance(value.get("base64"), str):
                    raise PlaywrightAdapterError("千牛导出没有返回 XLSX 字节", "response_invalid")
                return base64.b64decode(value["base64"])
            return value
        if site == "jst" and operation == "query":
            value = await controller.evaluate_file(
                "goods", self.skill_dir / "query_jst_invoice_goods.js", payload,
                frame_url=GOODS_FRAME_URL,
            )
            if not isinstance(value, dict):
                raise PlaywrightAdapterError("票聚返回不是对象", "response_invalid")
            return value
        raise PlaywrightAdapterError(f"不支持的 Playwright 操作: {site}/{operation}", "configuration")

    def __call__(self, site: str, operation: str, input_path: Path, output_path: Path,
                 binary: bool = False) -> dict[str, Any]:
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlaywrightAdapterError(f"页面采集输入无法读取: {exc}", "configuration", site=site) from exc
        self.progress(f"playwright {site}/{operation}")
        value = self._call(self._invoke_async(site, operation, payload, binary), site=site)
        if binary and isinstance(value, (bytes, bytearray)):
            value = base64.b64encode(bytes(value)).decode("ascii")
        return {"operation": operation, "payload": value, "outputPath": str(output_path),
                "observedAt": _now(), "browser_backend": "playwright"}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown_requested.set()
        if self.loop is not None and not self.loop.is_closed():
            task = self._startup_task
            if task is not None and not task.done():
                self.loop.call_soon_threadsafe(task.cancel)
            elif self._stop_event is not None:
                self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread is not None:
            self.thread.join(timeout=30)
