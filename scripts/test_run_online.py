"""Unit tests for the resumable online coordinator.

These tests fake the in-process adapter boundary; no browser or network is
required. The important contract here is the immutable file
checkpoint and the fact that a resume consumes it without another browser call.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_online


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, site: str, operation: str, input_path: Path, output_path: Path):
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        self.calls.append((site, operation))
        if operation == "context" and site == "qianniu":
            value = {"store": payload["expected_store"], "agentId": "agent-1",
                     "isLogin": True, "invoice_url": "https://seller/invoice",
                     "checked_at": "2026-09-23T00:00:00+00:00"}
        elif operation == "context" and site == "jst":
            value = {"issuer": payload["expected_issuer"], "coid": "co-1", "uid": "u-1",
                     "isLogin": True, "jst_url": "https://piaoju/goods",
                     "checked_at": "2026-09-23T00:00:01+00:00"}
        elif operation == "query" and site == "jst":
            value = {"data": [{"input_goods_code": code, "ok": True,
                               "rows": [], "reason": None} for code in payload["codes"]]}
        else:
            value = {"operation": operation}
        run_online.atomic_json(output_path, value)
        return {"operation": operation, "rowCount": 1,
                "outputPath": str(output_path), "sha256": run_online.file_sha256(output_path),
                "observedAt": "2026-09-23T00:00:02+00:00"}


class RunOnlineTests(unittest.TestCase):
    def make_runner(self, root, fake=None, **kwargs):
        return run_online.OnlineRunner(date="2026-09-22", store="店", issuer="主体",
                                       output_root=root / "outputs", adapter_runner=fake,
                                       **kwargs)

    def prepare_jst(self, runner, codes):
        run_online.atomic_json(runner.input_dir / "capture_context.json", {"coid": "c", "uid": "u"})
        run_online.atomic_json(runner.input_dir / "goods_codes.json", {"codes": codes})

    def test_chunked_has_no_oversized_batch(self):
        batches = list(run_online.chunked([str(i) for i in range(101)], 50))
        self.assertEqual([50, 50, 1], [len(batch) for batch in batches])

    def test_live_run_requires_config_but_mock_run_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(run_online.OnlineError) as error:
                run_online.OnlineRunner(date="2026-09-22", store="店", issuer="主体",
                                        output_root=Path(temp) / "outputs")
            self.assertEqual(error.exception.code, "configuration")
            runner = self.make_runner(Path(temp), FakeAdapter())
            self.assertEqual(runner.browser_backend, "playwright")

    def test_only_playwright_backend_is_supported(self):
        parser = run_online.build_parser()
        self.assertEqual(parser.parse_args([]).browser_backend, "playwright")
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["--browser-backend", "opencli"])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(run_online.OnlineError) as error:
                self.make_runner(Path(temp), FakeAdapter(), browser_backend="opencli")
            self.assertEqual(error.exception.code, "configuration")

    def test_browser_config_precedence_and_resume_profile_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "browser.json"
            original = {
                "schema_version": 1, "browser": "Edge",
                "user_data_dir": str(root / "profile-a"), "profile_directory": "Default",
                "browser_sessions": {"invoice": "invoice", "orders": "orders", "goods": "goods"},
            }
            config.write_text(json.dumps(original), encoding="utf-8")
            with patch.dict(os.environ, {run_online.QIANNIU_BROWSER_CONFIG_ENV: str(root / "absent.json")}):
                runner = self.make_runner(root, FakeAdapter(), browser_config=config)
            self.assertEqual(runner.state["browser_config"], original)
            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            self.assertEqual(resumed.browser_backend, "playwright")
            for key, replacement in (("user_data_dir", str(root / "profile-b")),
                                     ("profile_directory", "Profile 2")):
                with self.subTest(key=key):
                    config.write_text(json.dumps({**original, key: replacement}), encoding="utf-8")
                    with self.assertRaises(run_online.OnlineError) as error:
                        self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
                    self.assertEqual(error.exception.code, "resume_mismatch")

    def test_old_online_backend_cannot_resume_even_with_injected_adapter(self):
        for saved_backend in ("opencli", None):
            with self.subTest(saved_backend=saved_backend), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runner = self.make_runner(root, FakeAdapter())
                if saved_backend is None:
                    runner.state.pop("browser_backend")
                else:
                    runner.state["browser_backend"] = saved_backend
                runner._write_state()
                before = (runner.run_dir / "run-state.json").read_bytes()
                fake = FakeAdapter()
                with self.assertRaises(run_online.OnlineError) as error:
                    self.make_runner(root, fake, resume=runner.run_dir)
                self.assertEqual(error.exception.code, "resume_backend_unsupported")
                self.assertEqual(fake.calls, [])
                self.assertEqual((runner.run_dir / "run-state.json").read_bytes(), before)

    def test_legacy_data_can_be_replayed_offline_without_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "snapshot"
            source.mkdir()
            run_online.atomic_json(source / "capture_context.json", {"browser_backend": "opencli"})
            runner = self.make_runner(root, replay_input=source)
            self.assertTrue(runner.replay)
            self.assertFalse(runner._needs_browser)
            # A saved offline replay is also resumable regardless of its
            # historical transport metadata; no live connection is involved.
            runner.state["browser_backend"] = "opencli"
            runner._write_state()
            resumed = self.make_runner(root, resume=runner.run_dir)
            self.assertTrue(resumed.replay)
            self.assertFalse(resumed._needs_browser)

    def test_missing_adapter_never_falls_back_to_a_shell_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invoker = run_online.AdapterInvoker(root)
            with patch.object(run_online.subprocess, "run") as subprocess_run:
                with self.assertRaises(run_online.OnlineError) as error:
                    invoker.invoke("qianniu", "context", {}, root / "context.json")
                self.assertEqual(error.exception.code, "configuration")
                subprocess_run.assert_not_called()
            self.assertFalse((root / "context.json").exists())

    def test_stale_run_state_partial_is_quarantined_on_next_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "run-state.json"
            partial = root / "run-state.json.partial"
            run_online.atomic_json(state, {"status": "failed"})
            partial.write_text('{"status":"running"}\n', encoding="utf-8")
            run_online.atomic_json(state, {"status": "running"})
            self.assertEqual(run_online.read_json(state)["status"], "running")
            stale = list(root.glob("run-state.json.partial.stale-*"))
            self.assertEqual(len(stale), 1)

    def test_atomic_publish_retries_only_transient_windows_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "run-state.json"
            state.write_text('{"status":"old"}', encoding="utf-8")
            original = Path.replace
            sharing = PermissionError("sharing violation")
            sharing.winerror = 32
            calls = []

            def replace(source, target):
                calls.append(source)
                if len(calls) < 3:
                    self.assertEqual(run_online.read_json(state)["status"], "old")
                    raise sharing
                return original(source, target)

            with patch.object(Path, "replace", replace), patch.object(run_online.time, "sleep") as delay:
                run_online.atomic_json(state, {"status": "new"})
            self.assertEqual(len(calls), 3)
            self.assertEqual(delay.call_count, 2)
            self.assertEqual(run_online.read_json(state)["status"], "new")
            self.assertFalse(list(Path(temp).glob("*.partial-*")))

    def test_atomic_publish_exhaustion_preserves_committed_and_staged_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "run-state.json"
            state.write_text('{"status":"old"}', encoding="utf-8")
            denied = PermissionError("denied")
            denied.winerror = 5
            with patch.object(Path, "replace", side_effect=denied) as publish, patch.object(run_online.time, "sleep"):
                with self.assertRaises(run_online.OnlineError) as error:
                    run_online.atomic_json(state, {"status": "new"})
            self.assertEqual(error.exception.code, "checkpoint_write_failed")
            self.assertEqual(publish.call_count, 5)
            self.assertEqual(run_online.read_json(state)["status"], "old")
            staged = list(Path(temp).glob("*.partial-*"))
            self.assertEqual(len(staged), 1)
            self.assertEqual(run_online.read_json(staged[0])["status"], "new")

    def test_atomic_publish_does_not_retry_unrelated_io_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(Path, "replace", side_effect=OSError("disk error")) as publish:
                with self.assertRaises(run_online.OnlineError):
                    run_online.atomic_json(Path(temp) / "data.json", {"ok": True})
            self.assertEqual(publish.call_count, 1)

    def test_resume_finishes_response_publication_without_requery(self):
        for failed_part, binary in ((part, binary) for part in ("journal", "raw", "receipt")
                                    for binary in (False, True)):
            with self.subTest(failed_part=failed_part, binary=binary), tempfile.TemporaryDirectory() as temp:
                calls = []
                exported_bytes = b"PK\x03\x04-test-export-bytes\x00\xff"
                def adapter(site, operation, input_path, output_path):
                    calls.append((site, operation))
                    value = (run_online.base64.b64encode(exported_bytes).decode("ascii")
                             if binary else {"result": "complete response"})
                    return {"operation": operation, "payload": value}
                runner = self.make_runner(Path(temp), adapter)
                output = runner.run_dir / "raw.json"
                targets = {"journal": runner.run_dir / "publications" / "raw.json",
                           "raw": output, "receipt": runner.run_dir / "receipts" / "raw.json"}
                original = Path.replace
                sharing = PermissionError("simulated sharing failure")
                sharing.winerror = 32
                def replace(source, target):
                    if Path(target) == targets[failed_part]:
                        raise sharing
                    return original(source, target)
                with patch.object(Path, "replace", replace), patch.object(run_online.time, "sleep"):
                    with self.assertRaises(run_online.OnlineError) as error:
                        runner.collect("qianniu", "test", {}, output, "raw", binary=binary)
                self.assertEqual(error.exception.code, "checkpoint_write_failed")
                self.assertEqual(len(calls), 1)
                resumed = self.make_runner(Path(temp), adapter, resume=runner.run_dir)
                resumed.collect("qianniu", "test", {}, output, "raw", binary=binary)
                self.assertEqual(len(calls), 1)
                if binary:
                    self.assertEqual(output.read_bytes(), exported_bytes)
                else:
                    self.assertEqual(run_online.read_json(output), {"result": "complete response"})
                receipt = run_online.read_json(targets["receipt"])
                self.assertEqual(receipt["sha256"], run_online.file_sha256(output))
                self.assertEqual(resumed.state["checkpoints"]["raw"], receipt)

    def test_unproven_response_fragments_stop_before_adapter(self):
        for fragment in ("raw.json.partial", "raw.json", "publications/raw.json.partial"):
            with self.subTest(fragment=fragment), tempfile.TemporaryDirectory() as temp:
                fake = FakeAdapter()
                runner = self.make_runner(Path(temp), fake)
                path = runner.run_dir / fragment
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"incomplete":', encoding="utf-8")
                with self.assertRaises(run_online.OnlineError) as error:
                    runner.collect("qianniu", "test", {}, runner.run_dir / "raw.json", "raw")
                self.assertEqual(error.exception.code, "checkpoint_invalid")
                self.assertEqual(fake.calls, [])
                self.assertEqual(path.read_text(encoding="utf-8"), '{"incomplete":')

    def test_recovery_rejects_changed_saved_response_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def adapter(site, operation, input_path, output_path):
                calls.append(operation)
                return {"operation": operation, "payload": {"result": "complete"}}
            runner = self.make_runner(Path(temp), adapter)
            output = runner.run_dir / "raw.json"
            original = Path.replace
            denied = PermissionError("simulated disk failure")
            def replace(source, target):
                if Path(target) == output:
                    raise denied
                return original(source, target)
            with patch.object(Path, "replace", replace), self.assertRaises(run_online.OnlineError):
                runner.collect("qianniu", "test", {"range": "old"}, output, "raw")
            with self.assertRaises(run_online.OnlineError) as error:
                runner.collect("qianniu", "test", {"range": "new"}, output, "raw")
            self.assertEqual(error.exception.code, "resume_mismatch")
            self.assertEqual(len(calls), 1)
            self.assertFalse(output.exists())

    def test_progress_does_not_rewrite_recovery_state(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = self.make_runner(Path(temp), FakeAdapter())
            before = (runner.run_dir / "run-state.json").read_bytes()
            runner.progress("only progress")
            self.assertEqual(before, (runner.run_dir / "run-state.json").read_bytes())
            self.assertIn("only progress", (runner.run_dir / "progress.log").read_text(encoding="utf-8"))

    def test_output_lock_precedes_browser_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "browser.json"
            config.write_text(json.dumps({"schema_version": 1, "browser": "Edge",
                                          "user_data_dir": str(root / "profile")}), encoding="utf-8")
            with patch("playwright_adapter.PlaywrightAdapterRunner") as browser:
                runner = self.make_runner(root, browser_backend="playwright", browser_config=config)
                browser.assert_not_called()
                with run_online.ActiveLock(runner.output_root):
                    with self.assertRaises(run_online.OnlineError) as error:
                        runner.run()
                self.assertEqual(error.exception.code, "already_running")
                browser.assert_not_called()

    def test_lock_release_does_not_remove_replaced_owner_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = run_online.ActiveLock(Path(temp))
            with lock:
                lock.path.write_text(json.dumps({"pid": os.getpid(), "owner_token": "other"}), encoding="utf-8")
            self.assertTrue(lock.path.exists())

    def test_resume_success_replaces_failed_latest_report_and_archives_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter())
            with patch.object(runner, "_context", side_effect=run_online.OnlineError("first failure", "request_failed")):
                with self.assertRaises(run_online.OnlineError):
                    runner.run()
            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            resumed.generated_dir.mkdir()
            run_online.atomic_json(resumed.generated_dir / "run.json", {"status": "complete", "ready_count": 1})
            with patch.object(resumed, "_context"), patch.object(resumed, "_applications_export"), \
                 patch.object(resumed, "_orders"), patch.object(resumed, "_jst"), \
                 patch.object(resumed, "_probe_and_details"), patch.object(resumed, "stage_is_done", return_value=True):
                resumed.run()
            latest = run_online.read_json(resumed.run_dir / "run.json")
            self.assertEqual(latest["status"], "complete")
            self.assertEqual(latest["ready_count"], 1)
            archives = list((resumed.run_dir / "attempts").glob("*.json"))
            self.assertTrue(archives)
            self.assertTrue(all(run_online.read_json(path)["status"] == "failed" for path in archives))

    def test_injected_adapter_writes_and_hashes_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            invoker = run_online.AdapterInvoker(root, adapter_runner=fake)
            output = root / "context.json"
            receipt = invoker.invoke("qianniu", "context",
                                     {"expected_store": "店铺", "expected_issuer": "主体"},
                                     output, "context")
            self.assertEqual(receipt["sha256"], run_online.file_sha256(output))
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["store"], "店铺")
            self.assertEqual(fake.calls, [("qianniu", "context")])
            with self.assertRaises(run_online.OnlineError):
                invoker.invoke("qianniu", "context", {}, output, "context-again")

    def test_context_resume_does_not_call_adapter_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = run_online.OnlineRunner(
                date="2026-09-22", store="示例旗舰店", issuer="示例电子商务有限公司[测试员工]",
                output_root=root / "outputs", adapter_runner=fake,
            )
            runner._context()
            self.assertEqual(fake.calls, [("qianniu", "context"), ("jst", "context")])
            context = json.loads((runner.input_dir / "capture_context.json").read_text(encoding="utf-8"))
            self.assertEqual(context["agentId"], "agent-1")
            self.assertTrue(context["context_sha256"])
            resumed = run_online.OnlineRunner(
                date="2026-09-22", store="示例旗舰店", issuer="示例电子商务有限公司[测试员工]",
                output_root=root / "outputs", resume=runner.run_dir, adapter_runner=fake,
            )
            resumed._context()
            self.assertEqual(fake.calls, [("qianniu", "context"), ("jst", "context"),
                                          ("qianniu", "context"), ("jst", "context")])

    def test_lock_rejects_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = run_online.ActiveLock(root)
            first.__enter__()
            try:
                with self.assertRaises(run_online.OnlineError) as caught:
                    with run_online.ActiveLock(root):
                        pass
                self.assertEqual(caught.exception.code, "already_running")
            finally:
                first.__exit__(None, None, None)
            with run_online.ActiveLock(root):
                self.assertTrue((root / ".qianniu-invoice-online.lock").exists())

    def test_lock_reclaims_dead_owner_and_run_records_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock_path = root / ".qianniu-invoice-online.lock"
            lock_path.write_text(json.dumps({"pid": 999999999, "started_at": "old"}), encoding="utf-8")
            lock = run_online.ActiveLock(root)
            with lock:
                self.assertTrue(lock.recovered_stale_lock)
                self.assertEqual(lock.stale_lock_reason, "dead_process")
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

            runner = self.make_runner(root, FakeAdapter())
            runner_lock = runner.output_root / ".qianniu-invoice-online.lock"
            runner_lock.write_text(json.dumps({"pid": 999999999, "started_at": "old"}), encoding="utf-8")
            with patch.object(runner, "_context",
                              side_effect=run_online.OnlineError("stop", "auth_required")):
                with self.assertRaises(run_online.OnlineError):
                    runner.run()
            self.assertEqual(runner.state["recovered_stale_lock"]["reason"], "dead_process")

    def test_dead_lock_is_recovered_but_live_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / ".qianniu-invoice-online.lock"
            lock.write_text(json.dumps({"pid": 999999999, "started_at": run_online.utc_now()}), encoding="utf-8")
            with run_online.ActiveLock(root):
                self.assertTrue(lock.exists())
            live = run_online.ActiveLock(root)
            live.__enter__()
            try:
                with self.assertRaises(run_online.OnlineError) as error:
                    with run_online.ActiveLock(root):
                        pass
                self.assertEqual(error.exception.code, "already_running")
            finally:
                live.__exit__(None, None, None)

    def test_resume_rejects_changed_checkpoint_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "input.json"
            path.write_text("original", encoding="utf-8")
            runner = run_online.OnlineRunner(
                date="2026-09-22", store="店", issuer="主体", output_root=root / "outputs",
                replay_input=root,
            )
            runner.state["input_hashes"] = [{"path": str(path.resolve()),
                                              "sha256": run_online.file_sha256(path)}]
            runner._write_state()
            path.write_text("changed", encoding="utf-8")
            with self.assertRaises(run_online.OnlineError) as caught:
                runner._validate_input_hashes()
            self.assertEqual(caught.exception.code, "resume_mismatch")

    def test_empty_detail_probe_is_checkpointed_for_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = run_online.OnlineRunner(
                date="2026-09-22", store="店", issuer="主体", output_root=root / "outputs",
                adapter_runner=fake,
            )
            (runner.input_dir / "goods_codes.json").write_text('{"codes": []}', encoding="utf-8")
            probe = runner.run_dir / "plan-probe"
            probe.mkdir()
            (probe / "invoice_plan.json").write_text('{"invoices": []}', encoding="utf-8")
            (probe / "run.json").write_text('{"status": "plan_only"}', encoding="utf-8")
            with patch.object(run_online.OnlineRunner, "_detail_orders_from_plan", return_value=set()):
                runner._probe_and_details()
            self.assertTrue(runner.stage_is_done("detail_enrichment"))
            resumed = run_online.OnlineRunner(
                date="2026-09-22", store="店", issuer="主体", output_root=root / "outputs",
                resume=runner.run_dir, adapter_runner=fake,
            )
            with patch.object(run_online.OnlineRunner, "_detail_orders_from_plan", return_value=set()):
                resumed._probe_and_details()
            self.assertTrue(resumed.stage_is_done("detail_enrichment"))

    def test_resume_completes_detail_dependencies_before_marking_complete(self):
        for legacy_complete in (False, True):
            with self.subTest(legacy_complete=legacy_complete), tempfile.TemporaryDirectory() as temp:
                fake = FakeAdapter()
                detail_calls = []
                def adapter(site, operation, input_path, output_path):
                    if operation == "detail":
                        detail_calls.append(operation)
                        return {"operation": operation, "payload": {
                            "order_no": "O", "verified_order": True,
                            "items": [{"order_no": "O", "goods_code": "B"}]}}
                    return fake(site, operation, input_path, output_path)
                runner = self.make_runner(Path(temp), adapter)
                self.prepare_jst(runner, ["A"])
                context = runner.input_dir / "capture_context.json"
                run_online.atomic_json(context, {"agentId": "a", "coid": "c", "uid": "u"})
                codes = runner.input_dir / "goods_codes.json"
                runner.stage_done("orders", [codes])
                runner._jst()
                probe = runner.run_dir / "plan-probe"
                probe.mkdir()
                run_online.atomic_json(probe / "invoice_plan.json", {"invoices": []})
                run_online.atomic_json(probe / "run.json", {"status": "plan_only"})
                with patch.object(runner, "_detail_orders_from_plan", return_value={"O"}), \
                        patch.object(runner, "_orders", side_effect=KeyboardInterrupt("after detail collection")):
                    with self.assertRaises(KeyboardInterrupt):
                        runner._probe_and_details()
                self.assertNotEqual(runner.state["stages"]["detail_enrichment"]["status"], "complete")
                if legacy_complete:
                    runner.stage_done("detail_enrichment", [runner.input_dir / "supplemental_details.json"],
                                      attempted_orders=["O"])
                resumed = self.make_runner(Path(temp), adapter, resume=runner.run_dir)
                # A real resume reaches these ordinary stages before enrichment.
                resumed._orders()
                resumed._jst()
                def remerge(force=False):
                    self.assertTrue(force)
                    run_online.atomic_json(codes, {"codes": ["A", "B"]})
                    resumed.stage_done("orders", [codes])
                with patch.object(resumed, "_detail_orders_from_plan", return_value={"O"}), \
                        patch.object(resumed, "_orders", side_effect=remerge):
                    resumed._probe_and_details()
                self.assertEqual(detail_calls, ["detail"])
                requests = [run_online.read_json(path)["codes"] for path in
                            sorted((runner.run_dir / "adapter-inputs").glob("jst-*.json"))]
                self.assertEqual(requests, [["A"], ["B"]])
                self.assertEqual(run_online.read_json(codes)["codes"], ["A", "B"])
                self.assertTrue(resumed.stage_is_done("detail_enrichment"))
                self.assertTrue(resumed.state["stages"]["detail_enrichment"]["dependencies_committed"])

    def test_resume_partial_context_rechecks_both_sites_before_collection(self):
        for changed_site in (None, "qianniu", "jst"):
            with self.subTest(changed_site=changed_site), tempfile.TemporaryDirectory() as temp:
                calls = []
                fail_jst = True
                def adapter(site, operation, input_path, output_path):
                    calls.append((site, operation))
                    if operation != "context":
                        raise AssertionError("Business collection must not pass an incomplete identity gate")
                    if site == "jst" and fail_jst:
                        raise run_online.OnlineError("login required", "auth_required")
                    if site == "qianniu":
                        value = {"isLogin": True, "store": "店", "agentId": "a"}
                    else:
                        value = {"isLogin": True, "issuer": "主体", "coid": "c", "uid": "u"}
                    if not fail_jst and changed_site == site:
                        value["store" if site == "qianniu" else "issuer"] = "另一主体"
                    return {"operation": operation, "payload": value}
                runner = self.make_runner(Path(temp), adapter)
                with self.assertRaises(run_online.OnlineError):
                    runner._context()
                original = runner.input_dir / "context_qianniu.json"
                original_bytes = original.read_bytes()
                receipt = runner.run_dir / "receipts" / "context-qianniu.json"
                receipt_bytes = receipt.read_bytes()
                fail_jst = False
                calls.clear()
                resumed = self.make_runner(Path(temp), adapter, resume=runner.run_dir)
                if changed_site:
                    with self.assertRaises(run_online.OnlineError) as error:
                        resumed._context()
                    self.assertEqual(error.exception.code, "context_changed")
                    self.assertFalse(resumed.stage_is_done("context"))
                    self.assertFalse((resumed.input_dir / "capture_context.json").exists())
                else:
                    resumed._context()
                    self.assertTrue(resumed.stage_is_done("context_refresh"))
                    self.assertTrue(resumed.stage_is_done("context"))
                    self.assertEqual(calls[:2], [("qianniu", "context"), ("jst", "context")])
                self.assertTrue(calls)
                self.assertTrue(all(operation == "context" for _, operation in calls))
                self.assertEqual(original.read_bytes(), original_bytes)
                self.assertEqual(receipt.read_bytes(), receipt_bytes)

    def test_context_company_and_operator_label_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()

            def adapter(site, operation, input_path, output_path):
                receipt = fake(site, operation, input_path, output_path)
                if site == "jst":
                    value = run_online.read_json(output_path)
                    value.update(issuer="主体", issuer_label="主体[员工甲]")
                    run_online.atomic_json(output_path, value)
                    receipt["sha256"] = run_online.file_sha256(output_path)
                return receipt

            runner = self.make_runner(root, adapter)
            runner._context()
            context = run_online.read_json(runner.input_dir / "capture_context.json")
            self.assertEqual(context["issuer"], "主体")
            self.assertEqual(context["issuer_label"], "主体[员工甲]")
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            resumed._context()
            self.assertEqual(resumed.issuer, "主体")

    def test_observed_display_label_input_is_canonicalized_without_guessing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()

            def adapter(site, operation, input_path, output_path):
                receipt = fake(site, operation, input_path, output_path)
                if site == "jst":
                    value = run_online.read_json(output_path)
                    value.update(issuer="主体", issuer_label="主体[员工甲]")
                    run_online.atomic_json(output_path, value)
                    receipt["sha256"] = run_online.file_sha256(output_path)
                return receipt

            runner = run_online.OnlineRunner(date="2026-09-22", store="店", issuer="主体[员工甲]",
                                              output_root=root / "outputs", adapter_runner=adapter)
            runner._context()
            self.assertEqual(runner.issuer, "主体")
            self.assertEqual(runner.state["requested_issuer"], "主体[员工甲]")
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            resumed._context()
            with self.assertRaises(run_online.OnlineError) as error:
                runner._verified_identity({"isLogin": True, "issuer": "主体", "coid": "c", "uid": "u"},
                                          "jst", "主体[未观察到的员工]")
            self.assertEqual(error.exception.code, "context_mismatch")

    def test_context_error_classification(self):
        cases = [({"isLogin": False}, "auth_required"),
                 ({"isLogin": True, "issuer": "主体"}, "context_missing"),
                 ({"isLogin": True, "issuer": "另一主体", "coid": "c", "uid": "u"}, "context_mismatch")]
        for context, code in cases:
            with self.subTest(code=code), self.assertRaises(run_online.OnlineError) as error:
                run_online.OnlineRunner._verified_identity(context, "jst", "主体")
            self.assertEqual(error.exception.code, code)

    def test_adapter_identity_error_is_not_misclassified_as_login_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def adapter(*args):
                raise run_online.OnlineError("login evidence lacks company", "context_missing")
            invoker = run_online.AdapterInvoker(root, adapter_runner=adapter)
            with self.assertRaises(run_online.OnlineError) as error:
                invoker.invoke("jst", "context", {}, root / "context.json")
            self.assertEqual(error.exception.code, "context_missing")

    def test_jst_force_and_resume_never_requery_success_or_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = self.make_runner(root, fake)
            self.prepare_jst(runner, ["A", "B"])
            runner._jst()
            part = runner.input_dir / "jst_part_001.json"
            receipt = runner.run_dir / "receipts" / "jst-001.json"
            before = (part.read_bytes(), receipt.read_bytes())
            # A legacy receipt sidecar must never enter the business part set.
            sidecar = part.with_name(part.name + ".receipt.json")
            run_online.atomic_json(sidecar, run_online.read_json(receipt))
            runner._jst(force=True)
            self.assertEqual(fake.calls, [("jst", "query")])
            self.assertEqual(before, (part.read_bytes(), receipt.read_bytes()))
            self.assertFalse(list(runner.input_dir.glob("*.failed-*")))
            resumed = self.make_runner(root, fake, resume=runner.run_dir)
            resumed._jst()
            self.assertEqual(fake.calls, [("jst", "query")])
            stage = resumed.state["stages"]["jst"]
            self.assertEqual(len(stage["attempts"]), 2)
            self.assertEqual(stage["attempts"][1]["checkpoints_reused"], 1)
            self.assertEqual(stage["attempts"][1]["checkpoints_collected"], 0)

    def test_jst_enrichment_queries_only_new_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = self.make_runner(root, fake)
            self.prepare_jst(runner, ["A"])
            runner._jst()
            run_online.atomic_json(runner.input_dir / "goods_codes.json", {"codes": ["A", "B"]})
            # Changed upstream scope invalidates only the merge, including when
            # interruption happened before force=True was reached.
            runner._jst()
            requests = [run_online.read_json(path)["codes"]
                        for path in sorted((runner.run_dir / "adapter-inputs").glob("jst-*.json"))]
            self.assertEqual(requests, [["A"], ["B"]])
            self.assertEqual(len(fake.calls), 2)

    def test_jst_resume_retries_only_unfinished_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            query_count = 0

            def adapter(site, operation, input_path, output_path):
                nonlocal query_count
                receipt = fake(site, operation, input_path, output_path)
                query_count += 1
                if query_count == 1:
                    run_online.atomic_json(output_path, {"data": [
                        {"input_goods_code": "A", "ok": True, "reason": None},
                        {"input_goods_code": "B", "ok": False, "reason": "request_failed"},
                        {"input_goods_code": "C", "ok": False, "reason": "no_exact_match"},
                    ]})
                    receipt["sha256"] = run_online.file_sha256(output_path)
                return receipt

            runner = self.make_runner(root, adapter)
            self.prepare_jst(runner, ["A", "B", "C"])
            with self.assertRaises(run_online.OnlineError) as error:
                runner._jst()
            self.assertEqual(error.exception.code, "request_failed")
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            resumed._jst()
            self.assertEqual(run_online.read_json(resumed.run_dir / "adapter-inputs" / "jst-002.json")["codes"], ["B"])
            self.assertEqual(query_count, 2)
            self.assertEqual(resumed.state["stages"]["jst"]["attempts"][0]["status"], "interrupted")
            resumed._jst(force=True)
            self.assertEqual(query_count, 2)

    def test_failed_merge_does_not_publish_a_parts_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = self.make_runner(root, fake)
            self.prepare_jst(runner, ["A", "B"])
            part = runner.input_dir / "jst_part_001.json"
            runner.collect("jst", "query", {"codes": ["A"]}, part, "jst-001")
            with self.assertRaises(run_online.OnlineError):
                runner.run_script([str(run_online.COLLECTOR), "merge-jst", "--run-dir", str(runner.input_dir),
                                   "--part", str(part)], "incomplete merge")
            self.assertFalse((runner.input_dir / "parts_manifest.json").exists())
            self.assertFalse((runner.input_dir / "jst_query.json").exists())

    def test_receipt_path_is_rejected_without_any_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = self.make_runner(root, fake)
            path = runner.run_dir / "jst_part_001.json.receipt.json"
            run_online.atomic_json(path, {"operation": "query", "outputPath": "other", "sha256": "x"})
            original = path.read_bytes()
            with self.assertRaises(run_online.OnlineError) as error:
                runner.collect("jst", "query", {"codes": ["A"]}, path, "jst-001")
            self.assertEqual(error.exception.code, "checkpoint_invalid")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(fake.calls, [])

    def test_wrong_receipt_output_path_stops_without_requery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            runner = self.make_runner(root, fake)
            path = runner.run_dir / "context.json"
            runner.collect("qianniu", "context", {}, path, "context")
            receipt_path = runner.run_dir / "receipts" / "context.json"
            receipt = run_online.read_json(receipt_path)
            receipt["outputPath"] = str(root / "wrong.json")
            run_online.atomic_json(receipt_path, receipt)
            with self.assertRaises(run_online.OnlineError) as error:
                runner.collect("qianniu", "context", {}, path, "context")
            self.assertEqual(error.exception.code, "resume_mismatch")
            self.assertEqual(len(fake.calls), 1)

    def test_stage_attempts_preserve_cumulative_time_and_close_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = self.make_runner(Path(temp), FakeAdapter())
            with patch.object(run_online.time, "monotonic", side_effect=[1.0, 3.0, 5.0, 9.0]):
                runner.stage_start("orders")
                runner.stage_done("orders")
                runner.stage_start("orders", kind="remerge")
                self.assertNotIn("finished_at", runner.state["stages"]["orders"])
                runner.stage_done("orders")
            stage = runner.state["stages"]["orders"]
            self.assertEqual(stage["duration_seconds"], 6)
            self.assertEqual([item["duration_seconds"] for item in stage["attempts"]], [2, 4])

            def fail():
                runner.stage_start("orders")
                runner.stage_start("details")
                raise run_online.OnlineError("test failure", "request_failed")

            with patch.object(runner, "_context", side_effect=fail):
                with self.assertRaises(run_online.OnlineError):
                    runner.run()
            self.assertEqual(runner.state["status"], "failed")
            for name in ("orders", "details"):
                stage = runner.state["stages"][name]
                self.assertEqual(stage["status"], "failed")
                self.assertEqual(stage["attempts"][-1]["status"], "failed")
                self.assertGreaterEqual(stage["finished_at"], stage["started_at"])

    def test_browser_target_change_does_not_invalidate_business_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAdapter()
            runner = self.make_runner(Path(temp), fake)
            runner.state["browser_pages"] = {"jst": {"goods": "old-target"}}
            path = runner.run_dir / "jst_part_001.json"
            runner.collect("jst", "query", {"codes": ["A"]}, path, "jst-001")
            runner.state["browser_pages"]["jst"]["goods"] = "new-target"
            runner.collect("jst", "query", {"codes": ["A"]}, path, "jst-001")
            self.assertEqual(len(fake.calls), 1)

    def test_collect_persists_browser_targets_from_detail_responses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()

            def adapter(site, operation, input_path, output_path):
                receipt = fake(site, operation, input_path, output_path)
                value = run_online.read_json(output_path)
                value["browser_pages"] = {"detail": "detail-target-1"}
                run_online.atomic_json(output_path, value)
                receipt["sha256"] = run_online.file_sha256(output_path)
                return receipt

            runner = self.make_runner(root, adapter)
            runner.collect("qianniu", "detail", {"order_no": "O-1"},
                           runner.run_dir / "detail.json", "detail-O-1")
            self.assertEqual(runner.state["browser_pages"]["qianniu"]["detail"], "detail-target-1")

    def test_invoice_retry_persists_actual_generated_directory_for_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter())
            original = runner.generated_dir
            original.mkdir(parents=True)
            with patch.object(runner, "run_script") as run_script:
                actual = runner._run_invoice(original, plan_only=False)
            self.assertNotEqual(actual, original)
            self.assertEqual(runner.generated_dir, actual)
            self.assertEqual(Path(runner.state["generated_dir"]), actual)
            run_script.assert_called_once()
            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            self.assertEqual(resumed.generated_dir, actual)

    def test_incomplete_probe_gets_immutable_retry_then_is_reused_on_next_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter())
            run_online.atomic_json(runner.input_dir / "goods_codes.json", {"codes": []})
            base = runner.run_dir / "plan-probe"
            base.mkdir()
            first_calls = []

            def interrupted(path, plan_only=False):
                first_calls.append(path)
                path.mkdir(parents=True, exist_ok=True)
                raise run_online.OnlineError("simulated interruption", "interrupted")

            with patch.object(runner, "_run_invoice", side_effect=interrupted), \
                    self.assertRaises(run_online.OnlineError):
                runner._probe_and_details()
            self.assertEqual(runner.state["probe"]["status"], "running")
            self.assertEqual(Path(runner.state["probe"]["path"]), base.with_name("plan-probe-retry2"))

            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            second_calls = []

            def complete(path, plan_only=False):
                second_calls.append(path)
                path.mkdir(parents=True, exist_ok=True)
                run_online.atomic_json(path / "invoice_plan.json", {"invoices": []})
                run_online.atomic_json(path / "run.json", {"status": "plan_only"})
                return path

            with patch.object(resumed, "_run_invoice", side_effect=complete), \
                    patch.object(resumed, "_detail_orders_from_plan", return_value=set()):
                resumed._probe_and_details()
            retry = base.with_name("plan-probe-retry2")
            first_retry = base.with_name("plan-probe-retry2")
            second_retry = base.with_name("plan-probe-retry3")
            self.assertEqual(first_calls, [first_retry])
            self.assertEqual(second_calls, [second_retry])
            self.assertEqual(Path(resumed.state["probe"]["path"]), second_retry)
            self.assertEqual(resumed.state["probe"]["status"], "complete")
            self.assertTrue(resumed.stage_is_done("detail_enrichment"))

            resumed_again = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            with patch.object(resumed_again, "_run_invoice", side_effect=AssertionError("probe rerun")), \
                    patch.object(resumed_again, "_detail_orders_from_plan", return_value=set()):
                resumed_again._probe_and_details()
            self.assertEqual(Path(resumed_again.state["probe"]["path"]), second_retry)

    def test_probe_manifest_mutation_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter())
            run_online.atomic_json(runner.input_dir / "goods_codes.json", {"codes": []})
            probe = runner.run_dir / "plan-probe"
            probe.mkdir()
            run_online.atomic_json(probe / "invoice_plan.json", {"invoices": []})
            run_online.atomic_json(probe / "run.json", {"status": "plan_only"})
            runner.state["probe"] = {"path": str(probe.resolve()), "status": "complete",
                                      "plan_sha256": run_online.file_sha256(probe / "invoice_plan.json"),
                                      "manifest_sha256": run_online.file_sha256(probe / "run.json")}
            runner._write_state()
            run_online.atomic_json(probe / "run.json", {"status": "plan_only", "changed": True})
            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            with patch.object(resumed, "_run_invoice", side_effect=AssertionError("must not silently reuse")):
                with self.assertRaises(run_online.OnlineError) as error:
                    resumed._probe_and_details()
            self.assertEqual(error.exception.code, "resume_mismatch")


if __name__ == "__main__":
    unittest.main()
