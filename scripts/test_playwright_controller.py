from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import playwright_controller as controller


CONFIG = {
    "schema_version": 1,
    "browser": "Edge",
    "user_data_dir": "profile",
    "browser_sessions": {
        "invoice": {"url": "https://myseller.taobao.com/home.htm/merchant-invoice/"},
        "orders": {"url": "https://myseller.taobao.com/home.htm/trade-platform/tp/sold"},
        "goods": {"url": "https://fp.erp321.com/setting/goodsManage"},
    },
}


class FakeLocator:
    def __init__(self, visible: bool):
        self.visible = visible

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=None):
        return self.visible


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def close(self):
        self.closed = True

    def locator(self, selector):
        return FakeLocator(False)


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    async def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class ControllerTests(unittest.TestCase):
    def test_cdp_port_is_concrete_nonzero_and_has_safe_default(self):
        self.assertEqual(controller._configured_debug_port(CONFIG), 9222)
        for value in (0, -1, 65536, "not-a-port"):
            config = dict(CONFIG)
            config["remote_debugging_port"] = value
            with self.assertRaises(controller.BrowserControllerError) as error:
                controller._configured_debug_port(config)
            self.assertEqual(error.exception.code, "configuration")
        nested = dict(CONFIG)
        nested["playwright"] = {"remote_debugging_port": 0}
        with self.assertRaises(controller.BrowserControllerError):
            controller._configured_debug_port(nested)

    def test_runtime_state_is_local_and_contains_port_only_on_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = controller._runtime_state_path(root, "Profile 1")
            self.assertEqual(path.parent, root)
            self.assertTrue(path.name.endswith(".runtime.json"))
            controller._runtime_write(
                path,
                pid=1234,
                port=9222,
                data_dir=root,
                profile="Profile 1",
            )
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["port"], 9222)
            controller._runtime_remove(path)
            self.assertFalse(path.exists())

    def test_status_does_not_expose_cdp_port_or_runtime_path(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance._cdp_port = 9222
        instance._runtime_path = Path("C:/private/.qianniu-playwright-Default.runtime.json")
        payload = json.dumps(instance.status(), ensure_ascii=False)
        self.assertNotIn("9222", payload)
        self.assertNotIn("remote_debugging_port", payload)
        self.assertNotIn("runtime.json", payload)

    def test_error_envelope_redacts_cdp_endpoint_details_and_messages(self):
        error = controller.BrowserControllerError(
            "连接 http://127.0.0.1:9222 失败",
            details={"port": 9222, "endpoint": "http://localhost:9222", "nested": {"cdp_port": 9222}},
        )
        payload = json.dumps(error.to_dict(), ensure_ascii=False)
        self.assertNotIn("9222", payload)
        self.assertNotIn('"port"', payload)
        self.assertNotIn('"endpoint"', payload)

    def test_close_does_not_delete_runtime_owned_by_another_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".qianniu-playwright-Default.runtime.json"
            path.write_text(json.dumps({"pid": 7, "port": 9222}), encoding="utf-8")
            instance = controller.PlaywrightBrowserController(CONFIG)
            instance._runtime_path = path
            instance._browser_owned = False
            asyncio.run(instance.close())
            self.assertTrue(path.exists())

    def test_close_preserves_owned_browser_and_runtime_for_next_job(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".qianniu-playwright-Default.runtime.json"
            path.write_text(json.dumps({"pid": 7, "port": 9222}), encoding="utf-8")
            instance = controller.PlaywrightBrowserController(CONFIG)
            instance.browser = Mock(close=AsyncMock())
            instance._browser_process = Mock()
            instance._runtime_path = path
            instance._runtime_owned = True
            instance._browser_owned = True
            with patch.object(controller, "_terminate_owned_process", new_callable=AsyncMock) as terminate:
                asyncio.run(instance.close())
                terminate.assert_not_awaited()
            self.assertTrue(path.exists())
            self.assertTrue(instance._browser_owned)

    def test_stop_terminates_only_owned_browser_and_cleans_its_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".qianniu-playwright-Default.runtime.json"
            path.write_text(json.dumps({"pid": 7, "port": 9222}), encoding="utf-8")
            instance = controller.PlaywrightBrowserController(CONFIG)
            process = Mock()
            instance._browser_process = process
            instance._runtime_path = path
            instance._runtime_owned = True
            instance._browser_owned = True
            with patch.object(controller, "_terminate_owned_process", new_callable=AsyncMock) as terminate:
                asyncio.run(instance.stop())
                terminate.assert_awaited_once_with(process, None)
            self.assertFalse(path.exists())
            self.assertFalse(instance._browser_owned)

    def test_stop_leaves_attached_external_browser_alive(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance._browser_process = Mock()
        instance._browser_owned = False
        with patch.object(controller, "_terminate_owned_process", new_callable=AsyncMock) as terminate:
            asyncio.run(instance.stop())
            terminate.assert_not_awaited()

    def test_native_edge_attach_contract_has_no_pipe_or_automation_flags(self):
        source = inspect.getsource(controller.PlaywrightBrowserController._open)
        self.assertIn("connect_over_cdp", source)
        self.assertIn("--remote-debugging-port={debug_port}", source)
        self.assertNotIn("--remote-debugging-pipe", source)
        self.assertNotIn("--enable-automation", source)
        self.assertNotIn("--load-extension", source)
        self.assertNotIn("--disable-extensions-except", source)

    def test_attach_only_missing_endpoint_never_launches_browser(self):
        for open_missing in (False, True):
            with self.subTest(open_missing=open_missing):
                self._assert_missing_endpoint_never_launches_browser(open_missing)

    def test_job_lock_defaults_to_no_wait_and_never_starts_runtime_when_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            instance = controller.PlaywrightBrowserController({**CONFIG, "user_data_dir": temp})
            owner = controller.ProfileLock(Path(temp), "Default")
            owner.acquire()
            factory = Mock()
            try:
                with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                        patch.object(controller.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaises(controller.BrowserControllerError) as caught:
                        asyncio.run(instance.start())
                self.assertEqual(instance.lock_wait_ms, 0)
                self.assertEqual(caught.exception.code, "profile_locked")
                sleep.assert_not_awaited()
                factory.assert_not_called()
                self.assertIsNone(instance._profile_lock)
                self.assertTrue(owner.owned)
                self.assertTrue(owner.path.exists())
            finally:
                owner.release()

    def test_real_job_mutex_release_allows_start_and_connect_without_new_browser(self):
        for entrypoint in ("start", "connect"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as temp:
                instance = controller.PlaywrightBrowserController(
                    {**CONFIG, "user_data_dir": temp}, lock_wait_ms=500)
                owner = controller.ProfileLock(Path(temp), "Default")
                owner.acquire()
                context = FakeContext([FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()])
                browser = SimpleNamespace(contexts=[context], close=AsyncMock())
                runtime = SimpleNamespace(stop=AsyncMock(), chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
                async def start_runtime():
                    self.assertFalse(owner.owned)
                    self.assertTrue(instance._profile_lock.owned)
                    return runtime
                factory = Mock(return_value=SimpleNamespace(start=AsyncMock(side_effect=start_runtime)))
                process = {"pid": 99, "port": 9222, "profile_directory": "Default"}
                async def exercise():
                    asyncio.get_running_loop().call_later(0.025, owner.release)
                    result = await getattr(instance, entrypoint)()
                    await instance.close()
                    return result
                try:
                    with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                            patch.object(controller, "_cdp_version_sync", return_value={}) as endpoint, \
                            patch.object(controller, "_running_profile_process", return_value=process), \
                            patch.object(controller.subprocess, "Popen") as launch:
                        result = asyncio.run(exercise())
                    self.assertTrue(result["ok"])
                    factory.assert_called_once()
                    endpoint.assert_called_once()
                    launch.assert_not_called()
                    self.assertTrue(owner.path.exists())
                    self.assertIsNone(instance._profile_lock)
                finally:
                    owner.release()

    def test_job_lock_timeout_preserves_owner_and_never_contacts_browser(self):
        for entrypoint in ("start", "connect"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as temp:
                instance = controller.PlaywrightBrowserController(
                    {**CONFIG, "user_data_dir": temp}, lock_wait_ms=30)
                owner = controller.ProfileLock(Path(temp), "Default")
                owner.acquire()
                metadata = owner.path.stat()
                factory = Mock()
                try:
                    with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                            patch.object(controller, "_cdp_version_sync") as endpoint, \
                            patch.object(controller.subprocess, "Popen") as launch:
                        with self.assertRaises(controller.BrowserControllerError) as caught:
                            asyncio.run(getattr(instance, entrypoint)())
                    self.assertEqual(caught.exception.code, "profile_locked")
                    factory.assert_not_called()
                    endpoint.assert_not_called()
                    launch.assert_not_called()
                    self.assertIsNone(instance._profile_lock)
                    self.assertTrue(owner.owned)
                    current = owner.path.stat()
                    self.assertEqual((current.st_ino, current.st_size, current.st_mtime_ns),
                                     (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns))
                finally:
                    owner.release()

    def test_cancel_while_waiting_preserves_owner_and_releases_no_foreign_handle(self):
        with tempfile.TemporaryDirectory() as temp:
            instance = controller.PlaywrightBrowserController(
                {**CONFIG, "user_data_dir": temp}, lock_wait_ms=5_000)
            owner = controller.ProfileLock(Path(temp), "Default")
            owner.acquire()
            metadata = owner.path.stat()
            factory = Mock()
            async def exercise():
                task = asyncio.create_task(instance.connect())
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            try:
                with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}):
                    asyncio.run(exercise())
                factory.assert_not_called()
                self.assertIsNone(instance._profile_lock)
                self.assertTrue(owner.owned)
                current = owner.path.stat()
                self.assertEqual((current.st_ino, current.st_size, current.st_mtime_ns),
                                 (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns))
            finally:
                owner.release()
            owner.acquire()
            owner.release()

    def test_cancel_during_runtime_start_releases_acquired_job_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            instance = controller.PlaywrightBrowserController({**CONFIG, "user_data_dir": temp}, lock_wait_ms=5_000)
            async def exercise():
                entered = asyncio.Event()
                async def start_runtime():
                    entered.set()
                    await asyncio.Event().wait()
                factory = Mock(return_value=SimpleNamespace(start=AsyncMock(side_effect=start_runtime)))
                with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}):
                    task = asyncio.create_task(instance.start())
                    await entered.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            asyncio.run(exercise())
            self.assertIsNone(instance._profile_lock)
            successor = controller.ProfileLock(Path(temp), "Default")
            successor.acquire()
            successor.release()
            self.assertTrue(successor.path.exists())

    def test_lock_wait_never_retries_errors_without_real_mutex_busy_cause(self):
        for code in ("profile_locked", "context_changed", "login_required"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                instance = controller.PlaywrightBrowserController({**CONFIG, "user_data_dir": temp}, lock_wait_ms=5_000)
                factory = Mock()
                with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                        patch.object(controller.ProfileLock, "acquire", side_effect=controller.BrowserControllerError("unrelated", code)) as acquire, \
                        patch.object(controller.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaises(controller.BrowserControllerError) as caught:
                        asyncio.run(instance.start())
                self.assertEqual(caught.exception.code, code)
                acquire.assert_called_once()
                sleep.assert_not_awaited()
                factory.assert_not_called()

    def _assert_missing_endpoint_never_launches_browser(self, open_missing):
        with tempfile.TemporaryDirectory() as temp:
            config = {**CONFIG, "user_data_dir": temp}
            instance = controller.PlaywrightBrowserController(config)
            runtime = SimpleNamespace(stop=AsyncMock())
            factory = Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime)))
            with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                    patch.object(controller, "_cdp_version_sync", return_value=None), \
                    patch.object(controller, "_running_profile_process", return_value=None), \
                    patch.object(controller.subprocess, "Popen") as launch:
                with self.assertRaises(controller.BrowserControllerError) as error:
                    asyncio.run(instance.connect(open_missing=open_missing))
            self.assertEqual(error.exception.code, "browser_disconnected")
            launch.assert_not_called()
            self.assertIsNone(instance._profile_lock)

    def test_explicit_native_start_opens_urls_once_without_extensions(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {**CONFIG, "user_data_dir": temp}
            instance = controller.PlaywrightBrowserController(config, timeout_ms=1000)
            blank = FakePage("about:blank")
            context = FakeContext([blank])
            context.new_page = AsyncMock()
            browser = SimpleNamespace(contexts=[context], close=AsyncMock())
            async def connect(_endpoint):
                # CDP becomes ready before native startup targets appear.
                asyncio.get_running_loop().call_soon(
                    context.pages.extend,
                    [FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()],
                )
                return browser
            runtime = SimpleNamespace(
                stop=AsyncMock(), chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=connect)),
            )
            factory = Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime)))
            process = {"pid": 99, "port": 9222, "profile_directory": "Default"}
            async def start_and_detach():
                result = await instance.start()
                await instance.close()
                return result
            with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                    patch.object(controller, "_cdp_version_sync", side_effect=[None, {}]), \
                    patch.object(controller, "_running_profile_process", side_effect=[None, process]), \
                    patch.object(controller, "_resolve_browser_executable", return_value=Path(temp) / "edge.exe"), \
                    patch.object(controller.subprocess, "Popen", return_value=Mock(pid=99)) as launch, \
                    patch.object(controller, "_terminate_owned_process", new_callable=AsyncMock) as terminate:
                result = asyncio.run(start_and_detach())
            launch.assert_called_once()
            args = launch.call_args.args[0]
            self.assertIn("--no-first-run", args)
            self.assertIn("--no-default-browser-check", args)
            self.assertFalse(any("extension" in arg for arg in args))
            for item in CONFIG["browser_sessions"].values():
                self.assertEqual(args.count(item["url"]), 1)
            self.assertEqual(len(result["roles"]), 3)
            self.assertTrue(all(not role["created"] for role in result["roles"].values()))
            context.new_page.assert_not_awaited()
            self.assertFalse(blank.closed)
            terminate.assert_not_awaited()

    def test_start_reuses_correct_existing_edge_without_spawning_or_duplicate_tabs(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {**CONFIG, "user_data_dir": temp}
            instance = controller.PlaywrightBrowserController(config)
            pages = [FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()]
            context = FakeContext(pages)
            context.new_page = AsyncMock()
            browser = SimpleNamespace(contexts=[context], close=AsyncMock())
            runtime = SimpleNamespace(stop=AsyncMock(), chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
            factory = Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime)))
            process = {"pid": 99, "port": 9222, "profile_directory": "Default"}
            async def run():
                value = await instance.start(open_missing=True)
                await instance.close()
                return value
            with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                    patch.object(controller, "_cdp_version_sync", return_value={}), \
                    patch.object(controller, "_running_profile_process", return_value=process), \
                    patch.object(controller.subprocess, "Popen") as launch:
                value = asyncio.run(run())
            self.assertTrue(value["ok"])
            self.assertEqual(set(value["roles"]), set(CONFIG["browser_sessions"]))
            launch.assert_not_called()
            context.new_page.assert_not_awaited()
            runtime.chromium.connect_over_cdp.assert_awaited_once()
            self.assertTrue(all(not page.closed for page in pages))
            self.assertIsNone(instance._profile_lock)

    def test_start_does_not_launch_over_existing_wrong_port_or_unavailable_cdp(self):
        for port, expected_code in ((9999, "profile_locked"), (9222, "browser_disconnected")):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as temp:
                instance = controller.PlaywrightBrowserController({**CONFIG, "user_data_dir": temp}, lock_wait_ms=5_000)
                runtime = SimpleNamespace(stop=AsyncMock())
                factory = Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=runtime)))
                process = {"pid": 99, "port": port, "profile_directory": "Default"}
                with patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=factory)}), \
                        patch.object(controller, "_cdp_version_sync", return_value=None), \
                        patch.object(controller, "_running_profile_process", return_value=process) as profile_process, \
                        patch.object(controller.subprocess, "Popen") as launch:
                    with self.assertRaises(controller.BrowserControllerError) as error:
                        asyncio.run(instance.start(open_missing=True))
                self.assertEqual(error.exception.code, expected_code)
                profile_process.assert_called_once()
                launch.assert_not_called()
                self.assertIsNone(instance._profile_lock)

    def test_home_role_matches_home_redirect_without_claiming_invoice_or_order_tabs(self):
        home = "https://myseller.taobao.com/"
        for path in ("", "home.htm", "home.htm/QnworkbenchHome/"):
            self.assertTrue(controller._same_role_page(home + path, home))
        for role in ("invoice", "orders"):
            self.assertFalse(controller._same_role_page(CONFIG["browser_sessions"][role]["url"], home))
        config = {**CONFIG, "browser_sessions": {"home": {"url": home}}}
        page = FakePage(home + "home.htm/QnworkbenchHome/")
        instance = controller.PlaywrightBrowserController(config)
        instance.context = FakeContext([page])
        instance.context.new_page = AsyncMock()
        result = asyncio.run(instance.connect())
        self.assertEqual(set(result["roles"]), {"home"})
        self.assertIs(instance.page("home"), page)
        instance.context.new_page.assert_not_awaited()

    def test_cold_start_waits_for_native_tabs_without_creating_or_closing(self):
        config = dict(CONFIG)
        blank = FakePage("about:blank")
        instance = controller.PlaywrightBrowserController(config, timeout_ms=100)
        instance.context = FakeContext([blank])
        instance.context.new_page = AsyncMock()
        async def native_targets_arrive(_delay):
            instance.context.pages.extend(FakePage(item["url"]) for item in CONFIG["browser_sessions"].values())
        with patch.object(controller.asyncio, "sleep", side_effect=native_targets_arrive):
            result = asyncio.run(instance._wait_for_startup_roles())
        self.assertEqual(len(result["roles"]), 3)
        instance.context.new_page.assert_not_awaited()
        self.assertFalse(blank.closed)
        self.assertEqual(len(instance.context.pages), 4)

    def test_login_redirect_is_reported_without_creating_replacement_pages(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        pages = [FakePage("https://login.taobao.com/member/login.jhtml")]
        instance.context = FakeContext(pages)
        instance.context.new_page = AsyncMock()
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.register_roles(open_missing=True))
        self.assertEqual(error.exception.code, "login_required")
        instance.context.new_page.assert_not_awaited()
        self.assertFalse(pages[0].closed)

    def test_later_role_login_blocks_earlier_missing_role_creation(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([FakePage("https://fp.erp321.com/login")])
        instance.context.new_page = AsyncMock()
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.register_roles(open_missing=True))
        self.assertEqual(error.exception.code, "login_required")
        instance.context.new_page.assert_not_awaited()

    def test_initial_document_redirect_is_reclassified_after_loading(self):
        pages = [FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()]
        async def redirect(*args, **kwargs):
            pages[0].url = "https://login.taobao.com/member/login.jhtml"
        pages[0].wait_for_load_state = redirect
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext(pages)
        instance.context.new_page = AsyncMock()
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance._wait_for_startup_roles())
        self.assertEqual(error.exception.code, "login_required")
        instance.context.new_page.assert_not_awaited()

    def test_no_login_url_is_not_authentication_evidence(self):
        page = FakePage(CONFIG["browser_sessions"]["invoice"]["url"])
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([page])
        instance.registrations["invoice"] = controller.PageRegistration("invoice", page)
        result = asyncio.run(instance.check_login("invoice"))
        self.assertFalse(result["is_logged_in"])
        self.assertEqual(result["code"], "context_missing")

    def test_current_site_login_hosts_stop_without_opening_more_tabs(self):
        for role, url in (("goods", "https://jstlogin.erp321.com/"),
                          ("invoice", "https://loginmyseller.taobao.com/")):
            with self.subTest(role=role):
                instance = controller.PlaywrightBrowserController(
                    {**CONFIG, "browser_sessions": {role: CONFIG["browser_sessions"][role]}})
                instance.context = FakeContext([FakePage(url)])
                instance.context.new_page = AsyncMock()
                with self.assertRaises(controller.BrowserControllerError) as error:
                    asyncio.run(instance.connect(open_missing=True))
                self.assertEqual(error.exception.code, "login_required")
                instance.context.new_page.assert_not_awaited()

    def test_evaluate_file_preserves_iife_and_invokes_function_contract(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        page = FakePage(CONFIG["browser_sessions"]["goods"]["url"])
        page.evaluate = AsyncMock(return_value={"ok": True})
        instance.context = FakeContext([page])
        instance.registrations["goods"] = controller.PageRegistration("goods", page)
        cases = (
            ("(async()=>({ok:true}))()", None, "(async()=>({ok:true}))()"),
            ("(async()=>({input:__INPUT__}))()", {"code": "A1"}, '(async()=>({input:{"code":"A1"}}))()'),
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "page.js"
            for source, input_value, expected in cases:
                path.write_text(source, encoding="utf-8")
                asyncio.run(instance.evaluate_file("goods", path, input_value))
                page.evaluate.assert_awaited_with(expected)
            path.write_text("async function(element,input){return input;}", encoding="utf-8")
            asyncio.run(instance.evaluate_file("goods", path, {"code": "A1"}))
            generated = page.evaluate.await_args.args[0]
            self.assertIn('(null,{"code":"A1"})', generated)
            self.assertEqual(generated, '(async()=>await (async function(element,input){return input;})(null,{"code":"A1"}))()')
    def test_load_config_resolves_profile_and_download_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.json"
            value = dict(CONFIG)
            value["download_dir"] = "downloads/{profile_directory}"
            path.write_text(json.dumps(value), encoding="utf-8")
            loaded, resolved = controller.load_browser_config(path, profile_directory="Profile 2")
            self.assertEqual(resolved, path.resolve())
            self.assertEqual(loaded["profile_directory"], "Profile 2")
            self.assertTrue(loaded["user_data_dir"].endswith("profile"))
            self.assertTrue(loaded["download_dir"].replace("\\", "/").endswith("downloads/Profile 2"))

    def test_config_accepts_role_subsets_and_rejects_empty_or_unknown_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.json"
            supported = {**CONFIG["browser_sessions"], "home": {"url": "https://myseller.taobao.com/"}}
            for roles in (("invoice", "orders"), ("goods",), ("home",), controller.ROLE_NAMES):
                value = {**CONFIG, "browser_sessions": {role: supported[role] for role in roles}}
                path.write_text(json.dumps(value), encoding="utf-8")
                loaded, _ = controller.load_browser_config(path)
                self.assertEqual(set(controller.role_specs(loaded)), set(roles))
            for sessions in ({}, {"unknown": "https://example.com"}):
                path.write_text(json.dumps({**CONFIG, "browser_sessions": sessions}), encoding="utf-8")
                with self.assertRaises(controller.BrowserControllerError) as error:
                    controller.load_browser_config(path)
                self.assertEqual(error.exception.code, "configuration")

    def test_role_subsets_open_and_check_only_their_configured_pages(self):
        for roles in (("invoice", "orders"), ("goods",)):
            config = {**CONFIG, "browser_sessions": {role: CONFIG["browser_sessions"][role] for role in roles}}
            instance = controller.PlaywrightBrowserController(config)
            instance.context = FakeContext([])
            result = asyncio.run(instance.connect(open_missing=True))
            self.assertEqual(set(result["roles"]), set(roles))
            self.assertEqual(len(instance.context.pages), len(roles))
            self.assertEqual({page.url for page in instance.context.pages},
                             {CONFIG["browser_sessions"][role]["url"] for role in roles})
            checked = asyncio.run(instance.check_logins())
            self.assertEqual({item["role"] for item in checked["roles"]}, set(roles))

    def test_close_releases_profile_lock_even_when_runtime_shutdown_is_cancelled(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        lock = Mock()
        instance._profile_lock = lock
        instance._stop_playwright = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(instance.close())
        lock.release.assert_called_once()
        self.assertIsNone(instance._profile_lock)

    def test_role_matching_does_not_conflate_invoice_and_orders(self):
        self.assertTrue(controller._same_role_page(
            "https://myseller.taobao.com/home.htm/trade-platform/tp/sold?x=1",
            CONFIG["browser_sessions"]["orders"]["url"],
        ))
        self.assertFalse(controller._same_role_page(
            CONFIG["browser_sessions"]["invoice"]["url"],
            CONFIG["browser_sessions"]["orders"]["url"],
        ))

    def test_register_roles_reuses_existing_pages(self):
        config = dict(CONFIG)
        config["user_data_dir"] = "profile"
        pages = [FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()]
        instance = controller.PlaywrightBrowserController(config)
        instance.context = FakeContext(pages)
        result = asyncio.run(instance.register_roles(open_missing=False))
        self.assertEqual(set(result["roles"]), set(CONFIG["browser_sessions"]))
        self.assertFalse(any(item.created for item in instance.registrations.values()))

    def test_connect_recovers_only_missing_orders_page_and_reuses_others(self):
        invoice = FakePage(CONFIG["browser_sessions"]["invoice"]["url"])
        goods = FakePage(CONFIG["browser_sessions"]["goods"]["url"])
        blank = FakePage("about:blank")
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([invoice, goods, blank])
        instance.context.new_page = AsyncMock(wraps=instance.context.new_page)
        result = asyncio.run(instance.connect(open_missing=True))
        self.assertIs(instance.page("invoice"), invoice)
        self.assertIs(instance.page("goods"), goods)
        self.assertEqual(instance.page("orders").url, CONFIG["browser_sessions"]["orders"]["url"])
        self.assertTrue(result["roles"]["orders"]["created"])
        self.assertEqual(len(instance.context.pages), 4)
        self.assertFalse(any(page.closed for page in instance.context.pages))
        asyncio.run(instance.connect(open_missing=True))
        instance.context.new_page.assert_awaited_once()
        self.assertEqual(len(instance.context.pages), 4)

    def test_connect_recovery_does_not_add_pages_when_all_roles_exist(self):
        pages = [FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()]
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext(pages)
        instance.context.new_page = AsyncMock()
        result = asyncio.run(instance.connect(open_missing=True))
        self.assertFalse(any(item["created"] for item in result["roles"].values()))
        self.assertEqual([instance.page(role) for role in CONFIG["browser_sessions"]], pages)
        instance.context.new_page.assert_not_awaited()

    def test_default_connect_does_not_recover_missing_pages(self):
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([])
        instance.context.new_page = AsyncMock()
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.connect())
        self.assertEqual(error.exception.code, "page_missing")
        instance.context.new_page.assert_not_awaited()

    def test_connect_recovery_login_redirect_stops_before_opening_later_roles(self):
        invoice = FakePage(CONFIG["browser_sessions"]["invoice"]["url"])
        recovered = FakePage("about:blank")
        async def redirect(_url, **_kwargs):
            recovered.url = "https://login.taobao.com/member/login.jhtml"
        recovered.goto = redirect
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([invoice])
        instance.context.new_page = AsyncMock(return_value=recovered)
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.connect(open_missing=True))
        self.assertEqual(error.exception.code, "login_required")
        self.assertEqual(error.exception.details["role"], "orders")
        instance.context.new_page.assert_awaited_once()
        self.assertFalse(recovered.closed)
        self.assertFalse(invoice.closed)

    def test_closed_registered_page_is_not_recreated_during_same_job(self):
        pages = [FakePage(item["url"]) for item in CONFIG["browser_sessions"].values()]
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext(pages)
        instance.context.new_page = AsyncMock()
        asyncio.run(instance.connect(open_missing=True))
        pages[1].closed = True
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.connect(open_missing=True))
        self.assertEqual(error.exception.code, "page_missing")
        instance.context.new_page.assert_not_awaited()

    def test_page_creation_failure_never_closes_existing_user_pages(self):
        invoice = FakePage(CONFIG["browser_sessions"]["invoice"]["url"])
        goods = FakePage(CONFIG["browser_sessions"]["goods"]["url"])
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([invoice, goods])
        instance.context.new_page = AsyncMock(side_effect=RuntimeError("target creation failed"))
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.connect(open_missing=True))
        self.assertEqual(error.exception.code, "page_navigation_failed")
        self.assertFalse(invoice.closed)
        self.assertFalse(goods.closed)

    def test_recovery_timeout_on_login_keeps_login_page_and_stops(self):
        invoice = FakePage(CONFIG["browser_sessions"]["invoice"]["url"])
        recovered = FakePage("about:blank")
        async def redirect_then_timeout(_url, **_kwargs):
            recovered.url = "https://login.taobao.com/member/login.jhtml"
            raise TimeoutError("navigation timed out")
        recovered.goto = redirect_then_timeout
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([invoice])
        instance.context.new_page = AsyncMock(return_value=recovered)
        for _ in range(2):
            with self.assertRaises(controller.BrowserControllerError) as error:
                asyncio.run(instance.connect(open_missing=True))
            self.assertEqual(error.exception.code, "login_required")
        instance.context.new_page.assert_awaited_once()
        self.assertFalse(recovered.closed)
        self.assertFalse(invoice.closed)

    def test_failed_recovery_navigation_is_not_repeated_in_same_controller(self):
        invoice = FakePage(CONFIG["browser_sessions"]["invoice"]["url"])
        recovered = FakePage("about:blank")
        recovered.goto = AsyncMock(side_effect=TimeoutError("navigation timed out"))
        instance = controller.PlaywrightBrowserController(CONFIG)
        instance.context = FakeContext([invoice])
        instance.context.new_page = AsyncMock(return_value=recovered)
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.connect(open_missing=True))
        self.assertEqual(error.exception.code, "page_navigation_failed")
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.connect(open_missing=True))
        self.assertEqual(error.exception.code, "page_missing")
        instance.context.new_page.assert_awaited_once()
        self.assertTrue(recovered.closed)
        self.assertFalse(invoice.closed)

    def test_login_check_returns_structured_login_required(self):
        config = dict(CONFIG)
        instance = controller.PlaywrightBrowserController(config)
        page = FakePage("https://login.taobao.com/member/login.jhtml")
        instance.context = FakeContext([page])
        instance.registrations["invoice"] = controller.PageRegistration("invoice", page)
        result = asyncio.run(instance.check_login("invoice"))
        self.assertFalse(result["is_logged_in"])
        self.assertEqual(result["code"], "login_required")

    def test_registered_same_site_wrong_route_is_not_reused(self):
        config = dict(CONFIG)
        pages = [FakePage(CONFIG["browser_sessions"]["invoice"]["url"])]
        instance = controller.PlaywrightBrowserController(config)
        instance.context = FakeContext(pages)
        instance.registrations["orders"] = controller.PageRegistration("orders", pages[0])
        with self.assertRaises(controller.BrowserControllerError) as error:
            asyncio.run(instance.register_roles(open_missing=True))
        self.assertEqual(error.exception.code, "page_missing")

    def test_download_root_alias_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "browser.json"
            value = dict(CONFIG)
            value["download_root"] = "downloads"
            path.write_text(json.dumps(value), encoding="utf-8")
            loaded, _ = controller.load_browser_config(path)
            self.assertTrue(loaded["download_dir"].endswith("downloads"))
            self.assertEqual(loaded["download_root"], loaded["download_dir"])


if __name__ == "__main__":
    unittest.main()
