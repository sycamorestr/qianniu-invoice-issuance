"""Playwright adapter bridge used by ``run_online.py``.

The runner is synchronous, while Playwright's async API owns an event loop.
This module keeps one controller loop in a background thread for the whole
job. The three page roles may share one browser or use separate Qianniu and
JST browsers; each browser's profile lock is held for the entire job.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from invoice_browser_config import with_invoice_pages
from playwright_controller import (
    BrowserControllerError,
    PlaywrightBrowserController,
    load_browser_config,
    PageRegistration,
    VERIFICATION_URL_RE,
    _same_role_page,
    _url_is_login,
)


GOODS_FRAME_URL = "https://src.erp321.com/erp-web-group/erp-scm-invoice-goods/index"
ORDER_NAVIGATION_DELAY_SECONDS = 10.0
VERIFICATION_POLL_SECONDS = 0.5
RECOVERABLE_READS = {"context", "applications", "export", "orders", "detail", "approval-status"}


def _fetch_shop_cookies(shop_name: str, expected_account: str | None) -> dict[str, Any]:
    """Load the shared private credential reader only after explicit expiry."""
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = root / "skills" / "_shared" / "qianniu_auth" / "feishu_cookie_recovery.py"
    try:
        spec = importlib.util.spec_from_file_location("_invoice_feishu_cookie_recovery", path)
        if spec is None or spec.loader is None:
            raise ValueError("shared reader unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.fetch_shop_cookies(shop_name=shop_name, expected_account=expected_account)
    except Exception:
        # Even import/browser errors must never expose cookies or credentials.
        raise PlaywrightAdapterError("千牛 Cookie 恢复资料不可用，请更新飞书资料后恢复原任务",
                                     "auth_required", site="qianniu") from None


class PlaywrightAdapterError(RuntimeError):
    def __init__(self, message: str, code: str = "adapter_failed", *,
                 site: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.site = site


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _controller_error_code(error: BrowserControllerError) -> str:
    """Recover only known JS error tokens from the exception headline."""
    if error.code != "script_failed":
        return error.code
    codes = {
        "login_required": "auth_required",
        "rate_limited": "rate_limited",
        "permission_required": "permission_required",
        "verification_required": "verification_required",
        "page_not_ready": "page_not_ready",
        "context_changed_store": "context_changed",
        "context_changed_account": "context_changed",
        "context_changed_agent_id": "context_changed",
        "context_ambiguous_agent_id": "context_changed",
        "context_missing_store": "context_missing",
        "context_missing_agent_id": "context_missing",
        "context_missing_account": "context_missing",
        "approval_identity_required": "context_missing",
        "approval_role_unsupported": "approval_role_unsupported",
        "approval_duplicate_check": "approval_duplicate_check",
        "approval_unknown": "approval_unknown",
        "approval_rejected": "approval_rejected",
        "approval_target_conflict": "approval_target_conflict",
    }
    # Playwright appends a stack after its first line. Do not reinterpret a
    # quoted token, a longer token, or a token appearing only in that stack.
    headline = str(error).splitlines()[0] if str(error) else ""
    match = re.search(r"(?:^|:\s*)Error:\s*([a-z_]+)\s*$", headline)
    return codes.get(match.group(1), error.code) if match else error.code


class PlaywrightAdapterRunner:
    """Run scripts in one or two profiles, starting missing browsers on demand.

    ``launch_if_needed=False`` requires already-running native browsers while
    still preparing missing business pages for this invoice job.
    """

    accepts_binary = True

    @staticmethod
    def _detail_ready_expression(source: Path) -> str:
        # Readiness and collection must accept exactly the same complete
        # runtime/DOM evidence, including a proven absence of a merchant code.
        collector = source.read_text(encoding="utf-8").strip().rstrip(";")
        collector = collector.replace("__INPUT__", "{order_no: orderNo}")
        return "orderNo => { try { return (" + collector + ").verified_order === true; } catch (_) { return false; } }"

    def __init__(self, config_path: Path, *, jst_config_path: Path | None = None,
                 profile_directory: str | None = None,
                 timeout_ms: int = 30_000,
                 launch_if_needed: bool = True,
                 recovery_identity: dict[str, Any] | None = None,
                 progress: Callable[[str], None] | None = None) -> None:
        try:
            self.config, self.config_path = load_browser_config(config_path, profile_directory=profile_directory)
            self.config = with_invoice_pages(self.config)
        except BrowserControllerError as exc:
            raise PlaywrightAdapterError(str(exc), exc.code, site="qianniu") from exc
        except ValueError as exc:
            raise PlaywrightAdapterError(str(exc), "configuration", site="qianniu") from exc
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
        self.launch_if_needed = bool(launch_if_needed)
        self.progress = progress or (lambda _message: None)
        self.recovery_identity = dict(recovery_identity or {})
        self._cookie_recovery_attempted = False
        self._verified_contexts: dict[str, dict[str, Any]] = {}
        self.skill_dir = Path(__file__).resolve().parent
        self.loop: asyncio.AbstractEventLoop | None = None
        self.controller: PlaywrightBrowserController | None = None
        self.controllers: dict[str, PlaywrightBrowserController] = {}
        self._created_controllers: list[PlaywrightBrowserController] = []
        self._operation_tasks: set[asyncio.Task[Any]] = set()
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._operation_targets: dict[asyncio.Task[Any], list[Any]] = {}
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
                controller = PlaywrightBrowserController(config, timeout_ms=self.timeout_ms, lock_wait_ms=5_000)
                self._created_controllers.append(controller)
                self.controllers[site] = controller
                if site == "qianniu":
                    self.controller = controller
                    if self.recovery_identity.get("expected_store"):
                        controller.login_recovery = self._recover_startup_login
                # start() validates ownership and reuses a correct running
                # environment itself. Never retry a failed connect by blindly
                # starting another browser or bypassing a login/profile error.
                open_browser = controller.start if self.launch_if_needed else controller.connect
                await asyncio.wait_for(
                    open_browser(open_missing=True), timeout=max(0.001, self.timeout_ms / 1000)
                    + (120 if site == "qianniu" and self.recovery_identity.get("expected_store") else 0)
                )
            except asyncio.CancelledError:
                raise
            except PlaywrightAdapterError:
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
        cancelled = False
        pending = [task for task in self._operation_tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            cancelled |= await self._wait_cleanup_uninterrupted(
                asyncio.gather(*pending, return_exceptions=True))
        # Cleanup is independent of the wrapper task: a second cancellation
        # must not release a profile while page JS or its child task still runs.
        pending_cleanup = list(self._cleanup_tasks)
        if pending_cleanup:
            cancelled |= await self._wait_cleanup_uninterrupted(
                asyncio.gather(*pending_cleanup, return_exceptions=True))
        for controller in reversed(self._created_controllers):
            try:
                close_task = asyncio.create_task(asyncio.wait_for(controller.close(), timeout=10))
                cancelled |= await self._wait_cleanup_uninterrupted(
                    asyncio.gather(close_task, return_exceptions=True))
            except (Exception, asyncio.CancelledError):
                # close() releases its OS lock in finally even if detaching
                # a disconnected browser or stopping the runtime fails.
                pass
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _wait_cleanup_uninterrupted(task: asyncio.Future) -> bool:
        """Defer caller cancellation until cleanup finishes, without losing it."""
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                return cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True

    async def _stop_page_requests(self, site: str, role: str, controller: Any,
                                  captured_targets: list[Any] | None = None) -> None:
        targets = list(captured_targets or [])
        try:
            page = controller.page(role)
            targets.append(page)
            if site == "jst":
                targets.extend(frame for frame in page.frames
                               if frame is not page.main_frame and GOODS_FRAME_URL in str(frame.url or ""))
        except Exception:
            pass
        targets = list({id(target): target for target in targets}.values())

        async def signal(target: Any) -> None:
            await asyncio.wait_for(target.evaluate(
                "() => { globalThis.__qianniuInvoiceStopRequested = true; "
                "globalThis.dispatchEvent(new Event('qianniu-invoice-stop')); }"), timeout=2)

        # An outer-page failure must not prevent cancelling the shared goods
        # iframe. Each context has its own latch and active fetch controllers.
        await asyncio.gather(*(signal(target) for target in targets), return_exceptions=True)

    async def _cancel_operation(self, operation_task: asyncio.Task[Any],
                                site: str, role: str, controller: Any) -> None:
        try:
            await self._stop_page_requests(site, role, controller,
                                           self._operation_targets.get(operation_task))
        finally:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)

    def _start(self) -> None:
        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop

            async def serve() -> None:
                self._stop_event = asyncio.Event()
                self._startup_task = loop.create_task(self._boot())
                # close() can race construction before the worker has a loop
                # or startup task to cancel. Honour that request before boot.
                if self._shutdown_requested.is_set():
                    self._startup_task.cancel()
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
        startup_recovery_budget = 120 if self.recovery_identity.get("expected_store") else 0
        try:
            if not self.ready.wait(timeout=max(10, browser_count * self.timeout_ms / 1000 + 10)
                                   + startup_recovery_budget):
                raise PlaywrightAdapterError("Playwright controller 启动超时", "browser_launch_failed",
                                             site=self._starting_site)
            if self.start_error is not None:
                self.thread.join(timeout=30)
                self._raise_controller_error(self.start_error, site=self._starting_site)
        except BaseException:
            # A constructor interrupted before returning has no caller-owned
            # adapter to close. Stop its startup recovery and join cleanup here.
            self.close()
            raise

    def _raise_controller_error(self, error: BaseException, *, site: str | None = None) -> None:
        if isinstance(error, PlaywrightAdapterError):
            raise error
        if isinstance(error, BrowserControllerError):
            raise PlaywrightAdapterError(str(error), _controller_error_code(error), site=site) from error
        raise PlaywrightAdapterError(str(error), "browser_launch_failed", site=site) from error

    def _call(self, coroutine: Any, *, site: str | None = None,
              timeout_seconds: float | None = None) -> Any:
        if self._closed or self.loop is None:
            if hasattr(coroutine, "close"):
                coroutine.close()
            raise PlaywrightAdapterError("Playwright controller 已关闭", "browser_disconnected", site=site)
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=max(30, self.timeout_ms / 1000 + 30,
                                             timeout_seconds or 0))
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
        await controller.wait_for_business_ready("invoice")
        if payload.get("prepare_orders"):
            await controller.wait_for_business_ready("orders")
        value = await controller.evaluate_file(
            "invoice", self.skill_dir / "playwright_context_qianniu.js", payload, reset_stop=True
        )
        pages = await self._pages_receipt()
        value["browser_pages"] = {"invoice": pages.get("invoice"),
                                   "orders": pages.get("orders")}
        self._remember_context("qianniu", value)
        return value

    async def _context_jst(self, payload: dict[str, Any]) -> dict[str, Any]:
        controller = self._controller_for("jst")
        await controller.wait_for_business_ready("goods")
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
        value = {"isLogin": True, "is_login": True, "issuer": issuer,
                "issuer_label": str(label), "coid": str(frame_value["coid"]),
                "uid": str(frame_value["uid"]), "jst_url": str(page.url),
                "checked_at": frame_value.get("checked_at") or _now(),
                "browser_pages": {"goods": pages.get("goods")}}
        self._remember_context("jst", value)
        return value

    def _remember_context(self, site: str, value: dict[str, Any]) -> None:
        if not hasattr(self, "_verified_contexts"):
            self._verified_contexts = {}
        self._verified_contexts[site] = dict(value)

    async def _recover_startup_login(self, controller: Any, error: Any) -> None:
        await self._restore_cookie_session(controller, self.recovery_identity)
        # The ordinary context stage still checks both sites before any work.

    async def _restore_cookie_session(self, controller: Any, payload: dict[str, Any]) -> None:
        if getattr(self, "_cookie_recovery_attempted", False):
            raise PlaywrightAdapterError("千牛登录恢复已尝试一次，请人工处理后恢复原任务",
                                         "auth_required", site="qianniu")
        identity = {**getattr(self, "recovery_identity", {}), **payload}
        store = identity.get("expected_store")
        if not isinstance(store, str) or not store.strip():
            raise PlaywrightAdapterError("千牛登录恢复缺少精确店铺名", "auth_required", site="qianniu")
        context, profile_lock = controller.context, controller._profile_lock

        def still_owned() -> None:
            if getattr(self, "_closed", False) or context is None or controller.context is not context:
                raise PlaywrightAdapterError("千牛恢复期间浏览器连接已变化", "browser_disconnected", site="qianniu")
            if (profile_lock is None or controller._profile_lock is not profile_lock
                    or profile_lock.owned is not True):
                raise PlaywrightAdapterError("千牛恢复期间已失去原浏览器锁", "profile_locked", site="qianniu")

        still_owned()
        self._cookie_recovery_attempted = True
        pages = [page for page in context.pages if not page.is_closed()
                 and (urlsplit(str(page.url)).hostname or "").endswith(".taobao.com")]
        if not pages or any(item.page.is_closed() for role, item in controller.registrations.items()
                            if role in {"invoice", "orders"}):
            raise PlaywrightAdapterError("千牛原业务页面已丢失", "page_missing", site="qianniu")
        probe = (self.skill_dir / "playwright_page_probe.js").read_text(encoding="utf-8")
        # Inspect the login page directly: the ordinary readiness gate stops
        # at its URL before it can see a simultaneous slider challenge.
        for page in pages:
            if VERIFICATION_URL_RE.search(str(page.url)):
                raise PlaywrightAdapterError("千牛页面需要人工验证", "verification_required", site="qianniu")
            for frame in page.frames:
                if frame is page.main_frame or not VERIFICATION_URL_RE.search(str(frame.url)):
                    continue
                if await (await frame.frame_element()).is_visible():
                    raise PlaywrightAdapterError("千牛页面需要人工验证", "verification_required", site="qianniu")
            evidence = await page.evaluate(probe, {})
            if not isinstance(evidence, dict):
                raise PlaywrightAdapterError("千牛登录页验证状态未确认", "page_not_ready", site="qianniu")
            if evidence.get("verification"):
                raise PlaywrightAdapterError("千牛页面需要人工验证", "verification_required", site="qianniu")
        material = await asyncio.to_thread(_fetch_shop_cookies, store,
            identity.get("expected_account") or identity.get("expected_account_nick")
            or getattr(self, "_verified_contexts", {}).get("qianniu", {}).get("account_nick"))
        still_owned()
        try:
            await context.add_cookies(material["cookies"])
        except Exception:
            raise PlaywrightAdapterError("千牛 Cookie 写入失败，请人工处理后恢复原任务",
                                         "auth_required", site="qianniu") from None
        finally:
            material = None
        used: set[int] = set()
        for role in ("invoice", "orders"):
            still_owned()
            spec = controller.specs[role]
            registered = controller.registrations.get(role)
            page = registered.page if registered and not registered.page.is_closed() else None
            if page is None:
                page = next((item for item in pages if id(item) not in used
                             and _same_role_page(str(item.url), spec.url)), None)
            if page is None:
                page = next((item for item in pages if id(item) not in used
                             and _url_is_login(str(item.url))), None)
            if page is None:
                page = await context.new_page()
            used.add(id(page))
            controller.registrations[role] = PageRegistration(role, page)
            await page.goto(spec.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await controller.wait_for_business_ready(role)
        await controller.register_roles(open_missing=False)
        self.progress("千牛登录 Cookie 已更新，正在重新核验原店铺及两站主体")

    async def _reverify_after_recovery(self, payload: dict[str, Any]) -> None:
        saved = {site: dict(value) for site, value in getattr(self, "_verified_contexts", {}).items()}
        identity = {**getattr(self, "recovery_identity", {}), **payload, "prepare_orders": True}
        previous = saved.get("qianniu", {})
        for field, target in (("agentId", "agent_id"), ("observed_store", "expected_observed_store"),
                              ("account_nick", "expected_account_nick")):
            if previous.get(field) is not None:
                identity[target] = previous[field]
        # Recheck issuer/coid/uid without injecting cookies or navigating JST.
        current = {"qianniu": await self._invoke_once("qianniu", "context", identity, False),
                   "jst": await self._invoke_once("jst", "context", identity, False)}
        for site, names in (("qianniu", ("store", "agentId", "observed_store", "account_nick")),
                            ("jst", ("issuer", "coid", "uid"))):
            for name in names:
                if name in saved.get(site, {}) and str(saved[site][name]) != str(current[site].get(name)):
                    self._verified_contexts = saved
                    raise PlaywrightAdapterError("登录恢复后原主体发生变化", "context_changed", site=site)

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
            return await self._invoke_with_recovery(site, operation, payload, binary)
        finally:
            if task is not None:
                self._operation_tasks.discard(task)

    async def _invoke_with_recovery(self, site: str, operation: str, payload: dict[str, Any],
                                    binary: bool) -> Any:
        try:
            return await self._invoke_once(site, operation, payload, binary)
        except PlaywrightAdapterError as exc:
            identity = {**getattr(self, "recovery_identity", {}), **payload}
            if (site != "qianniu" or operation not in RECOVERABLE_READS
                    or exc.code not in {"login_required", "auth_required"}
                    or not identity.get("expected_store")
                    or getattr(self, "_cookie_recovery_attempted", False)):
                raise
            saved = {key: dict(value) for key, value in getattr(self, "_verified_contexts", {}).items()}
            try:
                await self._restore_cookie_session(self._controller_for(site), identity)
                await self._reverify_after_recovery(identity)
            except BaseException:
                self._verified_contexts = saved
                raise
            # Only this failed read is repeated; approve and unknown writes
            # never enter this branch. Existing durable checkpoints are intact.
            return await self._invoke_once(site, operation, payload, binary)

    async def _invoke_once(self, site: str, operation: str, payload: dict[str, Any],
                           binary: bool) -> Any:
        operation_task = None
        controller = None
        role = "goods" if site == "jst" else ("orders" if operation in {"orders", "detail"} else "invoice")
        try:
            controller = self._controller_for(site)
            await controller.raise_if_verification_required(role)
            operation_task = asyncio.create_task(self._invoke_for_site(site, operation, payload, binary))
            self._operation_targets[operation_task] = []
            while True:
                completed, _ = await asyncio.wait({operation_task}, timeout=VERIFICATION_POLL_SECONDS)
                if completed:
                    return await operation_task
                # Frame.url sees cross-origin challenge navigations even when
                # the outer iframe's src attribute has not changed.
                await controller.raise_if_verification_required(role)
        except PlaywrightAdapterError as exc:
            if exc.site is None:
                exc.site = site
            raise
        except BrowserControllerError as exc:
            code = _controller_error_code(exc)
            message = f"verification_required: {exc}" if code == "verification_required" else str(exc)
            raise PlaywrightAdapterError(message, code, site=site) from exc
        except Exception as exc:
            raise PlaywrightAdapterError(f"页面采集失败: {exc}", "request_failed", site=site) from exc
        finally:
            cleanup_task = None
            try:
                if operation_task is not None and not operation_task.done():
                    # Cancelling Playwright's await does not cancel page JS.
                    # A shielded, tracked task signals it and joins the child
                    # even if timeout handling and shutdown both cancel us.
                    cleanup_task = asyncio.create_task(
                        self._cancel_operation(operation_task, site, role, controller))
                    self._cleanup_tasks.add(cleanup_task)
                    if await self._wait_cleanup_uninterrupted(cleanup_task):
                        raise asyncio.CancelledError
            finally:
                if cleanup_task is not None and cleanup_task.done():
                    self._cleanup_tasks.discard(cleanup_task)
                if operation_task is not None:
                    self._operation_targets.pop(operation_task, None)

    async def _approval(self, operation: str, payload: dict[str, Any], binary: bool) -> dict[str, Any]:
        """Pin the captured identity before any approval read or write."""
        applications = payload.get("applications")
        valid_text = lambda value: isinstance(value, str) and bool(value) and value.strip() == value
        if (binary or not isinstance(applications, list)
                or any(not isinstance(row, dict) or set(row) != {"serialNo", "tid"}
                       or not valid_text(row.get("serialNo")) or not valid_text(row.get("tid"))
                       for row in applications)
                or len({row["serialNo"] for row in applications}) != len(applications)
                or any(not valid_text(payload.get(name)) for name in (
                    "expected_store", "expected_observed_store", "expected_account_nick"))
                or "repeatCheck" in payload or "autoCreate" in payload):
            raise PlaywrightAdapterError("批量同意缺少精确申请或原店铺身份", "configuration", site="qianniu")
        controller = self._controller_for("qianniu")
        # approval-status runs before the durable send intent; fail a passive
        # readiness/challenge check there before any approval can be sent.
        await controller.wait_for_business_ready("invoice")
        # Existing workbench aliases are valid only with the exact saved login.
        context_payload = {**payload, "expected_account": payload.get("expected_account")
                           or payload["expected_account_nick"]}
        await controller.evaluate_file("invoice", self.skill_dir / "playwright_context_qianniu.js",
                                       context_payload, reset_stop=True)
        value = await controller.evaluate_file("invoice", self.skill_dir / "approve_qianniu.js", payload)
        if (not isinstance(value, dict) or value.get("operation") != operation
                or value.get("date") != payload.get("date")
                or value.get("query_scope") != payload.get("query_scope")
                or not isinstance(value.get("applications"), list)):
            raise PlaywrightAdapterError("批量同意返回范围不一致", "response_invalid", site="qianniu")
        expected = [(row["serialNo"], row["tid"]) for row in applications]
        actual = value["applications"]
        if (any(not isinstance(row, dict) for row in actual)
                or [(row.get("serialNo"), row.get("tid")) for row in actual] != expected
                or (operation == "approval-status" and (
                    not valid_text(value.get("checked_at"))
                    or any(row.get("status") not in {"pending", "agreed", "unknown"} for row in actual)))
                or (operation == "approve" and (
                    type(value.get("code")) is not int or value["code"] != 200
                    or not isinstance(value.get("message"), str) or not valid_text(value.get("approved_at"))))):
            raise PlaywrightAdapterError("批量同意返回申请或状态不一致", "response_invalid", site="qianniu")
        return value

    async def _invoke_for_site(self, site: str, operation: str, payload: dict[str, Any],
                               binary: bool) -> Any:
        controller = self._controller_for(site)
        payload = {**payload, "operation": operation}
        if site == "qianniu" and operation == "context":
            return await self._context_qianniu(payload)
        if site == "jst" and operation == "context":
            return await self._context_jst(payload)
        if site == "qianniu":
            if operation in {"approval-status", "approve"}:
                return await self._approval(operation, payload, binary)
            role = "orders" if operation in {"orders", "detail"} else "invoice"
            if operation == "orders":
                requested_orders = self._validate_orders_request(payload)
                await controller.wait_for_business_ready(role)
                value = await controller.evaluate_file(
                    role, self.skill_dir / "read_qianniu.js", payload, reset_stop=True
                )
                return self._validate_orders_response(value, requested_orders)
            if operation == "detail":
                order_no = str(payload.get("order_no") or "").strip()
                if not order_no:
                    raise PlaywrightAdapterError("detail 缺少 order_no", "configuration")
                page = controller.page("orders")
                target = f"https://qn.taobao.com/home.htm/trade-platform/tp/detail?bizOrderId={order_no}"
                await asyncio.sleep(ORDER_NAVIGATION_DELAY_SECONDS)
                await page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
                # Wait for the complete verified runtime/DOM row set;
                # a missing merchant code is evidence, not a load spinner.
                await page.wait_for_function(
                    self._detail_ready_expression(self.skill_dir / "read_order_detail.js"),
                    arg=order_no,
                    timeout=min(self.timeout_ms, 10_000),
                )
                value = await controller.evaluate_file(role, self.skill_dir / "read_order_detail.js", payload)
                # Restore only after successful collection. On a failed read
                # leave the page intact for inspection/manual verification;
                # a finally navigation would issue more business requests.
                restore = self.config["browser_sessions"]["orders"]
                restore_url = restore.get("url") if isinstance(restore, dict) else restore
                await asyncio.sleep(ORDER_NAVIGATION_DELAY_SECONDS)
                await page.goto(str(restore_url), wait_until="domcontentloaded", timeout=self.timeout_ms)
                return value
            source = self.skill_dir / "read_qianniu.js"
            await controller.wait_for_business_ready(role)
            value = await controller.evaluate_file(role, source, payload, reset_stop=True)
            if binary:
                if not isinstance(value, dict) or not isinstance(value.get("base64"), str):
                    raise PlaywrightAdapterError("千牛导出没有返回 XLSX 字节", "response_invalid")
                return base64.b64decode(value["base64"])
            return value
        if site == "jst" and operation == "query":
            await controller.wait_for_business_ready("goods")
            operation_task = asyncio.current_task()
            def capture_target(target: Any) -> None:
                if operation_task is not None:
                    self._operation_targets.setdefault(operation_task, []).append(target)
            value = await controller.evaluate_file(
                "goods", self.skill_dir / "query_jst_invoice_goods.js", payload,
                frame_url=GOODS_FRAME_URL, reset_stop=True, on_target=capture_target,
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
        # Allow pacing/pagination and the two detail navigations without
        # consuming the old single-operation watchdog. Network/navigation
        # timeouts still apply and the overall operation remains bounded.
        budget = ({"context": 150, "applications": 900, "export": 180,
                   "orders": 600, "detail": 180, "approval-status": 900,
                   "approve": 150}.get(operation) if site == "qianniu" else 120)
        value = self._call(self._invoke_async(site, operation, payload, binary),
                           site=site, timeout_seconds=(budget or 0) + (240 if site == "qianniu"
                               and operation in RECOVERABLE_READS
                               and getattr(self, "recovery_identity", {}).get("expected_store") else 0))
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
