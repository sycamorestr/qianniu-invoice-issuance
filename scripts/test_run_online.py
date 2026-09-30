"""Unit tests for the resumable online coordinator.

These tests fake the in-process adapter boundary; no browser or network is
required. The important contract here is the immutable file
checkpoint and the fact that a resume consumes it without another browser call.
"""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from datetime import date as calendar_date
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import run_online
import invoice_scope


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
        kwargs.setdefault("date", "2026-09-22")
        return run_online.OnlineRunner(store="店", issuer="主体",
                                       output_root=root / "outputs", adapter_runner=fake,
                                       **kwargs)

    def prepare_jst(self, runner, codes):
        run_online.atomic_json(runner.input_dir / "capture_context.json", {"coid": "c", "uid": "u"})
        run_online.atomic_json(runner.input_dir / "goods_codes.json", {"codes": codes})

    def test_approval_batch_mode_is_frozen_and_legacy_resume_stays_twenty(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for mode in (None, "legacy_20"):
                with self.subTest(mode=mode):
                    runner = self.make_runner(root, FakeAdapter(), approval_batch_mode=mode)
                    expected = mode or "single_request"
                    self.assertEqual(runner.state["approval_batch_mode"], expected)
                    resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
                    self.assertEqual(resumed.approval_batch_mode, expected)
                    changed = "legacy_20" if expected == "single_request" else "single_request"
                    with self.assertRaises(run_online.OnlineError) as caught:
                        self.make_runner(root, FakeAdapter(), resume=runner.run_dir,
                                         approval_batch_mode=changed)
                    self.assertEqual(caught.exception.code, "resume_mismatch")
            runner = self.make_runner(root, FakeAdapter())
            runner.state.pop("approval_batch_mode")
            runner._write_state()
            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            self.assertEqual(resumed.approval_batch_mode, "legacy_20")
            self.assertNotIn("approval_batch_mode", resumed.state)
            with self.assertRaises(run_online.OnlineError) as caught:
                self.make_runner(root, FakeAdapter(), resume=runner.run_dir,
                                 approval_batch_mode="single_request")
            self.assertEqual(caught.exception.code, "resume_mismatch")

    def test_invalid_approval_batch_mode_fails_before_browser_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for value in ("", "all", 20, [], {}):
                with self.subTest(value=value):
                    with self.assertRaises(run_online.OnlineError) as caught:
                        self.make_runner(root, FakeAdapter(), approval_batch_mode=value)
                    self.assertEqual(caught.exception.code, "configuration")
            runner = self.make_runner(root, FakeAdapter())
            for value in (None, "", "all", 20, [], {}):
                with self.subTest(saved=value):
                    runner.state["approval_batch_mode"] = value
                    runner._write_state()
                    with self.assertRaises(run_online.OnlineError) as caught:
                        self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
                    self.assertEqual(caught.exception.code, "checkpoint_invalid")

    def export_adapter(self, calls, content=b"", applications=None):
        fake = FakeAdapter()
        def adapter(site, operation, input_path, output_path):
            calls.append((site, operation))
            if operation == "applications":
                request = run_online.read_json(input_path)
                payload = {"date": request["date"], "rows": [], "total": 0,
                           "api_total": 0, "observed_total": 0}
                if "query_scope" in request:
                    payload["query_scope"] = request["query_scope"]
                payload.update(applications or {})
                return {"operation": operation, "payload": payload}
            if operation == "export":
                return content
            return fake(site, operation, input_path, output_path)
        return adapter

    def workbook_bytes(self):
        content = io.BytesIO()
        with ZipFile(content, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
        return content.getvalue()

    def prepare_missing_code_orders(self, runner):
        run_online.atomic_json(runner.input_dir / "capture_context.json",
                               {"agentId": "agent-1", "coid": "co-1", "uid": "u-1"})
        path = runner.input_dir / "order_ids.json"
        run_online.atomic_json(path, {"orders": ["O-CODE", "O-GOOD", "O-OLD"]})
        runner.stage_start("orders_prepare")
        runner.stage_done("orders_prepare", [path])

    def missing_code_adapter(self, requests, *, code="REPAIRED", failure=None):
        fake = FakeAdapter()
        def item(order, goods):
            return {"order_no": order, "sub_order_no": "S-" + order, "goods_code": goods,
                    "title": "商品 " + order, "quantity": "1"}
        def adapter(site, operation, input_path, output_path):
            payload = run_online.read_json(input_path)
            requests.append((site, operation, payload))
            if operation == "orders":
                return {"payload": {"batches": [{"ids": payload["orders"], "pageNum": 1,
                    "page": {"totalNumber": 2, "totalPage": 1}, "order_ids": ["O-CODE", "O-GOOD"]}],
                    "items": [item("O-CODE", ""), item("O-GOOD", "GOOD")]}}
            if operation == "detail":
                if failure:
                    raise run_online.OnlineError("synthetic detail failure", failure, site="qianniu")
                order = payload["order_no"]
                return {"payload": {"order_no": order, "verified_order": True,
                    "items": [item(order, code if order == "O-CODE" else "OLD")]}}
            return fake(site, operation, input_path, output_path)
        return adapter

    def test_orders_missing_codes_and_absent_orders_get_one_detail_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root, requests = Path(temp), []
            adapter = self.missing_code_adapter(requests)
            runner = self.make_runner(root, adapter)
            self.prepare_missing_code_orders(runner)
            runner._orders()
            self.assertEqual({call[2]["order_no"] for call in requests if call[1] == "detail"},
                             {"O-CODE", "O-OLD"})
            self.assertEqual(sum(call[1] == "orders" for call in requests), 1)
            self.assertEqual(sum(call[1] == "detail" for call in requests), 2)
            codes = run_online.read_json(runner.input_dir / "goods_codes.json")["codes"]
            self.assertEqual(set(codes), {"REPAIRED", "GOOD", "OLD"})
            self.assertEqual(run_online.read_json(runner.input_dir / "order_batches.json")["missing_goods_code_orders"], [])
            runner._jst()
            queried = [call[2]["codes"] for call in requests if call[:2] == ("jst", "query")]
            self.assertEqual(queried, [["GOOD", "OLD", "REPAIRED"]])
            originals = {path: path.read_bytes() for path in runner.input_dir.glob("orders_part_*.json")}
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            resumed._orders(force=True)
            resumed._jst()
            self.assertEqual(sum(call[1] == "orders" for call in requests), 1)
            self.assertEqual(sum(call[1] == "detail" for call in requests), 2)
            self.assertEqual(sum(call[1] == "query" for call in requests), 1)
            self.assertTrue(all(path.read_bytes() == content for path, content in originals.items()))

    def test_orders_unresolved_code_is_retained_without_repeating_successful_detail(self):
        with tempfile.TemporaryDirectory() as temp:
            root, requests = Path(temp), []
            adapter = self.missing_code_adapter(requests, code="")
            runner = self.make_runner(root, adapter)
            self.prepare_missing_code_orders(runner)
            runner._orders()
            batches = run_online.read_json(runner.input_dir / "order_batches.json")
            self.assertEqual(batches["missing_goods_code_orders"], ["O-CODE"])
            self.assertEqual(batches["items"][0]["goods_code"], "")
            self.assertEqual(set(run_online.read_json(runner.input_dir / "goods_codes.json")["codes"]), {"GOOD", "OLD"})
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            resumed._orders(force=True)
            self.assertEqual(sum(call[1] == "orders" for call in requests), 1)
            self.assertEqual(sum(call[1] == "detail" for call in requests), 2)

    def test_orders_detail_global_failures_are_not_downgraded_to_missing_code(self):
        for code in ("auth_required", "context_mismatch", "browser_disconnected", "checkpoint_invalid"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                requests = []
                runner = self.make_runner(Path(temp), self.missing_code_adapter(requests, failure=code))
                self.prepare_missing_code_orders(runner)
                with self.assertRaises(run_online.OnlineError) as error:
                    runner._orders()
                self.assertEqual(error.exception.code, code)
                self.assertEqual(error.exception.site, "qianniu")
                self.assertFalse(runner.stage_is_done("orders"))
                self.assertFalse((runner.input_dir / "old_details.json").exists())

    def test_scope_all_requests_use_batch_frozen_range_and_hash(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28), countdown='started')
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            with patch.object(invoice_scope, "datetime") as clock:
                clock.now.side_effect = AssertionError("batch scope must not be recalculated")
                runner = self.make_runner(Path(temp), self.export_adapter(calls, self.workbook_bytes()),
                                          date=None, all_pending=True, query_scope=frozen)
                runner._context()
                runner._applications_export()
            self.assertEqual(runner.query_scope, frozen)
            self.assertIsNone(runner.date)
            self.assertIn("all-pending", runner.run_dir.name)
            self.assertEqual(runner.state["query_scope"], frozen)
            capture = run_online.read_json(runner.input_dir / "capture_context.json")
            self.assertEqual(capture["query_scope"], frozen)
            self.assertIsNone(capture["date"])
            for path in (runner.run_dir / "adapter-inputs").glob("*.json"):
                request = run_online.read_json(path)
                self.assertEqual(request["query_scope"], frozen)
                self.assertIsNone(request["date"])
            request = run_online.read_json(runner.run_dir / "adapter-inputs/applications.json")
            receipt = run_online.read_json(runner.run_dir / "receipts/applications.json")
            self.assertEqual(receipt["payloadSha256"], run_online.request_sha256(request))
            changed = {**request, "query_scope": invoice_scope.make_scope(
                all_pending=True, today=calendar_date(2026, 9, 29))}
            self.assertNotEqual(run_online.request_sha256(request), run_online.request_sha256(changed))

    def test_scope_dated_requests_preserve_legacy_shape_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = self.make_runner(Path(temp), self.export_adapter([], self.workbook_bytes()),
                                      query_scope=invoice_scope.make_scope('2026-09-22'))
            runner._context()
            runner._applications_export()
            legacy = {"date": "2026-09-22", "expected_store": "店", "expected_issuer": "主体",
                      "rebind_if_missing": True, "agentId": "agent-1"}
            for key in ("applications", "export"):
                request = run_online.read_json(runner.run_dir / "adapter-inputs" / f"{key}.json")
                receipt = run_online.read_json(runner.run_dir / "receipts" / f"{key}.json")
                self.assertEqual(request, legacy)
                self.assertEqual(receipt["payloadSha256"], run_online.request_sha256(legacy))
            self.assertNotIn("query_scope", runner.state)
            self.assertNotIn("query_scope", run_online.read_json(runner.input_dir / "capture_context.json"))

    def test_new_dated_run_defaults_to_countdown_and_resume_preserves_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root, calls = Path(temp), []
            runner = self.make_runner(root, self.export_adapter(calls, self.workbook_bytes()))
            runner._context()
            runner._applications_export()
            expected = {'mode': 'date', 'date': '2026-09-22', 'countdown': 'started'}
            self.assertEqual(runner.query_scope, expected)
            for name in ('capture_context.json', 'applications.json'):
                self.assertEqual(run_online.read_json(runner.input_dir / name)['query_scope'], expected)
            for key in ('applications', 'export'):
                request = run_online.read_json(runner.run_dir / 'adapter-inputs' / f'{key}.json')
                self.assertEqual(request['query_scope'], expected)
                unfiltered = {key: value for key, value in request.items() if key != 'query_scope'}
                self.assertNotEqual(run_online.request_sha256(request), run_online.request_sha256(unfiltered))
            resumed = self.make_runner(root, self.export_adapter(calls, self.workbook_bytes()), resume=runner.run_dir)
            self.assertEqual(resumed.query_scope, expected)
            before = list(calls)
            resumed._applications_export()
            self.assertEqual(calls, before)

    def test_scope_empty_all_pending_resume_keeps_window_and_does_not_recollect(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        with tempfile.TemporaryDirectory() as temp:
            root, calls = Path(temp), []
            adapter = self.export_adapter(calls)
            runner = self.make_runner(root, adapter, date=None, all_pending=True, query_scope=frozen)
            with patch.object(runner, "_run_invoice", side_effect=AssertionError("empty must not generate XLSX")):
                self.assertEqual(runner.run()["status"], "no_applications")
            manifest_path = runner.generated_dir / "run.json"
            original = manifest_path.read_bytes()
            self.assertEqual(run_online.read_json(manifest_path)["query_scope"], frozen)
            self.assertEqual(run_online.read_json(runner.run_dir / "run.json")["query_scope"], frozen)
            self.assertFalse(list(runner.run_dir.rglob("*.xlsx")))
            for flags in ({}, {"all_pending": True}):
                with self.subTest(flags=flags), patch.object(invoice_scope, "datetime") as clock:
                    clock.now.side_effect = AssertionError("resume must preserve previous day")
                    resumed = self.make_runner(root, adapter, date=None, resume=runner.run_dir, **flags)
                    self.assertEqual(resumed.run()["status"], "no_applications")
                    self.assertEqual(resumed.query_scope, frozen)
                self.assertEqual(manifest_path.read_bytes(), original)
            self.assertEqual(calls.count(("qianniu", "applications")), 1)
            self.assertEqual(calls.count(("qianniu", "export")), 1)

    def test_scope_raw_applications_wrong_window_or_mode_stops_before_export(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        different = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 29))
        cases = ({"query_scope": different}, {"query_scope": None},
                 {"date": "2026-09-22", "query_scope": frozen})
        for invalid in cases:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                calls = []
                runner = self.make_runner(Path(temp), self.export_adapter(calls, applications=invalid),
                                          date=None, all_pending=True, query_scope=frozen)
                runner._context()
                with self.assertRaises(run_online.OnlineError) as error:
                    runner._applications_export()
                self.assertEqual(error.exception.code, "checkpoint_invalid")
                self.assertNotIn(("qianniu", "export"), calls)
                self.assertFalse(runner.stage_is_done("applications"))

    def test_scope_resume_rejects_switched_mode_or_conflicting_batch_window(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        different = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 29))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter(), date=None, all_pending=True, query_scope=frozen)
            original = (runner.run_dir / "run-state.json").read_bytes()
            for kwargs in ({"date": "2026-09-22"}, {"date": None, "all_pending": True, "query_scope": different}):
                with self.subTest(kwargs=kwargs), self.assertRaises(run_online.OnlineError) as error:
                    self.make_runner(root, FakeAdapter(), resume=runner.run_dir, **kwargs)
                self.assertEqual(error.exception.code, "resume_mismatch")
                self.assertEqual((runner.run_dir / "run-state.json").read_bytes(), original)
            dated = self.make_runner(root, FakeAdapter())
            with self.assertRaises(run_online.OnlineError) as error:
                self.make_runner(root, FakeAdapter(), date=None, all_pending=True, resume=dated.run_dir)
            self.assertEqual(error.exception.code, "resume_mismatch")

    def test_scope_replay_infers_frozen_window_and_remains_offline(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "snapshot"
            run_online.atomic_json(source / "capture_context.json",
                                   {"date": None, "query_scope": frozen, "store": "店", "issuer": "主体"})
            for flags in ({}, {"all_pending": True}):
                with self.subTest(flags=flags), patch.object(invoice_scope, "datetime") as clock:
                    clock.now.side_effect = AssertionError("replay must preserve captured window")
                    runner = self.make_runner(root, date=None, replay_input=source, **flags)
                self.assertEqual(runner.query_scope, frozen)
                self.assertTrue(runner.replay)
                self.assertFalse(runner._needs_browser)
                self.assertTrue(runner.run_dir.name.startswith("replay-all-pending-"))
                with patch.object(runner, "run_script") as script:
                    runner._run_invoice(runner.generated_dir, plan_only=True)
                argv = script.call_args.args[0]
                self.assertIn("--all-pending", argv)
                self.assertIn("--replay", argv)
                self.assertNotIn("--date", argv)

    def test_scope_replay_legacy_context_inherits_application_window(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'snapshot'
            run_online.atomic_json(source / 'capture_context.json', {'store': '店', 'issuer': '主体'})
            run_online.atomic_json(source / 'applications.json', {'date': None, 'query_scope': frozen})
            with patch.object(invoice_scope, 'datetime') as clock:
                clock.now.side_effect = AssertionError('replay cannot recalculate today')
                runner = self.make_runner(root, date=None, all_pending=True, replay_input=source)
            self.assertEqual(runner.query_scope, frozen)
            with patch.object(run_online, 'OnlineRunner') as factory:
                self.assertEqual(run_online.main(['--replay-input', str(source), '--all-pending']), 0)
                self.assertEqual(factory.call_args.kwargs['query_scope'], frozen)

    def test_scope_generated_all_pending_outputs_are_in_checkpoint_proof(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        with tempfile.TemporaryDirectory() as temp:
            runner = self.make_runner(Path(temp), FakeAdapter(), date=None,
                                      all_pending=True, query_scope=frozen)
            def generate(output, plan_only=False):
                run_online.atomic_json(output / "run.json", {"status": "complete", "date": None,
                    "query_scope": frozen, "ready_count": 1, "ready_amount": "1.00"})
                (output / "exceptions.csv").write_text("synthetic", encoding="utf-8")
                (output / "qianniu_common_all-pending.xlsx").write_bytes(b"synthetic original")
                (output / "qianniu_invoice_tax_template_all-pending.xlsx").write_bytes(b"synthetic result")
                return output
            with patch.object(runner, "_context"), patch.object(runner, "_applications_export"), \
                    patch.object(runner, "_approve_applications"), \
                    patch.object(runner, "_orders"), patch.object(runner, "_jst"), \
                    patch.object(runner, "_probe_and_details"), patch.object(runner, "_run_invoice", side_effect=generate):
                self.assertEqual(runner.run()["status"], "complete")
            proof = {Path(item["path"]).name for item in runner.state["stages"]["generate"]["outputs"]}
            self.assertEqual(proof, {"run.json", "exceptions.csv", "qianniu_common_all-pending.xlsx",
                                     "qianniu_invoice_tax_template_all-pending.xlsx"})

    def test_scope_cli_missing_or_conflicting_arguments_stop_before_runner(self):
        for argv in ([], ["--store", "店", "--issuer", "主体"],
                     ["--all-pending", "--store", "店"], ["--all-pending", "--issuer", "主体"]):
            with self.subTest(argv=argv), patch.object(run_online, "OnlineRunner") as runner, patch("sys.stderr"):
                self.assertEqual(run_online.main(argv), 2)
                runner.assert_not_called()
        with patch.object(run_online, "OnlineRunner") as runner, patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            run_online.main(["--all-pending", "--date", "2026-09-22"])
        self.assertEqual(error.exception.code, 2)
        runner.assert_not_called()

    def test_scope_cli_resume_infers_all_scope_before_constructing_runner(self):
        frozen = invoice_scope.make_scope(all_pending=True, today=calendar_date(2026, 9, 28))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_online.atomic_json(root / "run-state.json", {"date": None, "query_scope": frozen,
                "store": "店", "issuer": "主体", "agent_id": "agent-1"})
            with patch.object(run_online, "OnlineRunner") as runner, patch("sys.stdout"):
                runner.return_value.run.return_value = {"status": "no_applications"}
                self.assertEqual(run_online.main(["--resume", str(root)]), 0)
            self.assertEqual(runner.call_args.kwargs["query_scope"], frozen)
            self.assertTrue(runner.call_args.kwargs["all_pending"])
            self.assertIsNone(runner.call_args.kwargs["date"])

    def test_zero_applications_and_empty_export_complete_without_business_workbooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []
            adapter = self.export_adapter(calls)
            runner = self.make_runner(root, adapter)
            with patch.object(runner, "_orders") as orders, patch.object(runner, "_jst") as jst, \
                    patch.object(runner, "_probe_and_details") as probe, \
                    patch.object(runner, "_run_invoice") as generate:
                result = runner.run()
                for stage in (orders, jst, probe, generate):
                    stage.assert_not_called()
            self.assertEqual(result["status"], "no_applications")
            self.assertEqual(calls.count(("qianniu", "export")), 1)
            self.assertEqual((runner.input_dir / "common-export.bin").read_bytes(), b"")
            self.assertFalse(list(runner.run_dir.rglob("*.xlsx")))
            self.assertTrue(runner.state["stages"]["export"]["empty_export"])
            manifest_path = runner.generated_dir / "run.json"
            manifest_bytes = manifest_path.read_bytes()
            manifest = run_online.read_json(manifest_path)
            self.assertIsNone(manifest["output"])
            self.assertIsNone(manifest["common_template_output"])
            self.assertEqual(manifest["empty_export"]["sha256"], run_online.file_sha256(runner.input_dir / "common-export.bin"))
            self.assertEqual(manifest["empty_export"]["reason"], "applications_and_export_empty")
            for key in ("selected_count", "ready_count", "blocked_count", "excluded_count", "detail_rows"):
                self.assertEqual(manifest[key], 0)
            for key in ("ready_amount", "blocked_amount", "excluded_amount"):
                self.assertEqual(manifest[key], "0.00")
            self.assertEqual((runner.generated_dir / "exceptions.csv").read_text(encoding="utf-8-sig").splitlines(),
                             ["申请流水号,金额,暂缓原因"])
            self.assertEqual({Path(item["path"]).name for item in runner.state["stages"]["generate"]["outputs"]},
                             {"run.json", "exceptions.csv"})
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            with patch.object(resumed, "_run_invoice", side_effect=AssertionError("must not generate")):
                self.assertEqual(resumed.run()["status"], "no_applications")
            self.assertEqual(calls.count(("qianniu", "applications")), 1)
            self.assertEqual(calls.count(("qianniu", "export")), 1)
            self.assertEqual(manifest_path.read_bytes(), manifest_bytes)

    def test_empty_export_requires_all_explicit_zero_list_evidence(self):
        cases = ({"total": 1}, {"api_total": None}, {"observed_total": 1},
                 {"total": False}, {"total": "0"}, {"rows": [{"id": "unexpected"}]})
        for invalid in cases:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                calls = []
                runner = self.make_runner(Path(temp), self.export_adapter(calls, applications=invalid))
                runner._context()
                with self.assertRaises(run_online.OnlineError) as error:
                    runner._applications_export()
                self.assertEqual(error.exception.code, "empty_export_unverified")
                self.assertEqual(calls.count(("qianniu", "export")), 1)
                self.assertFalse(runner.stage_is_done("export"))
                self.assertFalse(runner.generated_dir.exists())
                self.assertEqual((runner.input_dir / "common-export.bin").read_bytes(), b"")

    def test_zero_list_still_exports_and_preserves_nonempty_workbook_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            workbook = self.workbook_bytes()
            adapter = self.export_adapter(calls, workbook)
            runner = self.make_runner(Path(temp), adapter)
            runner._context()
            runner._applications_export()
            self.assertEqual(calls.count(("qianniu", "export")), 1)
            self.assertEqual((runner.input_dir / "common-export.bin").read_bytes(), workbook)
            self.assertEqual((runner.input_dir / "qianniu_common.xlsx").read_bytes(), workbook)
            self.assertIsNone(runner._empty_export_evidence())
            resumed = self.make_runner(Path(temp), adapter, resume=runner.run_dir)
            resumed._applications_export()
            self.assertEqual(calls.count(("qianniu", "export")), 1)

    def test_nonempty_nonzip_export_never_becomes_no_applications(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            runner = self.make_runner(Path(temp), self.export_adapter(calls, b"<html>login</html>"))
            runner._context()
            with self.assertRaises(run_online.OnlineError) as error:
                runner._applications_export()
            self.assertEqual(error.exception.code, "checkpoint_invalid")
            self.assertEqual((runner.input_dir / "common-export.bin").read_bytes(), b"<html>login</html>")
            self.assertFalse((runner.input_dir / "qianniu_common.xlsx").exists())

    def test_legacy_export_receipt_keeps_xlsx_path_without_requery(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            workbook = self.workbook_bytes()
            adapter = self.export_adapter(calls, workbook)
            runner = self.make_runner(Path(temp), adapter)
            runner._context()
            original = runner.input_dir / "qianniu_common.xlsx"
            runner.collect("qianniu", "export", {"date": runner.date, "agentId": "agent-1",
                           "expected_store": runner.store, "expected_issuer": runner.issuer},
                           original, "export", binary=True)
            receipt = (runner.run_dir / "receipts" / "export.json").read_bytes()
            resumed = self.make_runner(Path(temp), adapter, resume=runner.run_dir)
            resumed._applications_export()
            self.assertEqual(calls.count(("qianniu", "export")), 1)
            self.assertEqual(original.read_bytes(), workbook)
            self.assertFalse((runner.input_dir / "common-export.bin").exists())
            self.assertEqual((runner.run_dir / "receipts" / "export.json").read_bytes(), receipt)
            self.assertIsNone(resumed._empty_export_evidence())

    def test_empty_generation_recovers_after_manifest_publication_without_export_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            adapter = self.export_adapter(calls)
            runner = self.make_runner(Path(temp), adapter)
            original_done = runner.stage_done
            def interrupt(name, *args, **kwargs):
                if name == "generate":
                    raise run_online.OnlineError("interrupted after generation", "interrupted")
                return original_done(name, *args, **kwargs)
            with patch.object(runner, "stage_done", side_effect=interrupt), self.assertRaises(run_online.OnlineError):
                runner.run()
            original = (runner.generated_dir / "run.json").read_bytes()
            resumed = self.make_runner(Path(temp), adapter, resume=runner.run_dir)
            self.assertEqual(resumed.run()["status"], "no_applications")
            self.assertEqual(calls.count(("qianniu", "export")), 1)
            self.assertEqual((resumed.generated_dir / "run.json").read_bytes(), original)

    def split_configs(self, root):
        qianniu = root / "qianniu-browser.json"
        jst = root / "jst-browser.json"
        qianniu.write_text(json.dumps({
            "schema_version": 1, "browser": "Edge", "user_data_dir": str(root / "shop-data"),
            "browser_sessions": {"invoice": "https://myseller.taobao.com/home.htm/merchant-invoice/",
                                 "orders": "https://myseller.taobao.com/home.htm/trade-platform/tp/sold"},
        }), encoding="utf-8")
        jst.write_text(json.dumps({
            "schema_version": 1, "browser": "Edge", "user_data_dir": str(root / "shared-data"),
            "profile_directory": "Default", "remote_debugging_port": 19371,
            "browser_sessions": {"goods": "https://fp.erp321.com/setting/goodsManage"},
        }), encoding="utf-8")
        return qianniu, jst

    def account_adapter(self, requests, *, observed_store="tb-store", account_nick="tb-store:operator"):
        def adapter(site, operation, input_path, output_path):
            payload = run_online.read_json(input_path)
            requests.append((site, operation, payload))
            if site == "qianniu" and operation == "context":
                value = {"isLogin": True, "store": payload["expected_store"], "agentId": "agent-1",
                         "observed_store": observed_store, "account_nick": account_nick}
            elif site == "jst" and operation == "context":
                value = {"isLogin": True, "issuer": payload["expected_issuer"], "coid": "co-1", "uid": "u-1"}
            else:
                value = {"operation": operation}
            return {"operation": operation, "payload": value}
        return adapter

    def test_expected_account_is_saved_and_sent_only_to_qianniu_context(self):
        requests = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, self.account_adapter(requests), expected_account="tb-store:operator")
            runner._context()
            runner.collect("qianniu", "test", {}, runner.run_dir / "other.json", "other")
            self.assertEqual(runner.state["expected_account"], "tb-store:operator")
            capture = run_online.read_json(runner.input_dir / "capture_context.json")
            self.assertEqual(capture["store"], "店")
            self.assertEqual(capture["observed_store"], "tb-store")
            self.assertEqual(capture["account_nick"], "tb-store:operator")
            self.assertEqual(requests[0][2]["expected_account"], "tb-store:operator")
            for site, operation, payload in requests[1:]:
                self.assertNotIn("expected_account", payload, (site, operation))
            resumed = self.make_runner(root, self.account_adapter(requests), resume=runner.run_dir)
            self.assertEqual(resumed.expected_account, "tb-store:operator")
            resumed._context()
            refresh_request = next(payload for site, operation, payload in requests[3:] if site == "qianniu")
            self.assertEqual(refresh_request["expected_observed_store"], "tb-store")
            self.assertEqual(refresh_request["expected_account_nick"], "tb-store:operator")
            self.assertEqual(refresh_request["expected_account"], "tb-store:operator")
        self.assertEqual(run_online.build_parser().parse_args(["--expected-account", "tb-store:operator"]).expected_account,
                         "tb-store:operator")

    def test_expected_account_cannot_change_after_it_was_fixed_even_before_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter(), expected_account="store:operator")
            before = (runner.run_dir / "run-state.json").read_bytes()
            with self.assertRaises(run_online.OnlineError) as error:
                self.make_runner(root, FakeAdapter(), resume=runner.run_dir, expected_account="store:other")
            self.assertEqual(error.exception.code, "resume_mismatch")
            self.assertEqual((runner.run_dir / "run-state.json").read_bytes(), before)

    def test_legacy_failed_context_can_add_first_account_without_changing_other_request_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            def fail_context(site, operation, input_path, output_path):
                if site == "qianniu" and operation == "context":
                    raise run_online.OnlineError("store mismatch", "context_changed_store")
                return fake(site, operation, input_path, output_path)
            runner = self.make_runner(root, fail_context)
            other_path = runner.run_dir / "other.json"
            runner.collect("qianniu", "test", {}, other_path, "other")
            other_receipt = runner.run_dir / "receipts" / "other.json"
            original_receipt = other_receipt.read_bytes()
            with self.assertRaises(run_online.OnlineError):
                runner._context()
            self.assertFalse((runner.input_dir / "context_qianniu.json").exists())
            state = run_online.read_json(runner.run_dir / "run-state.json")
            state.pop("expected_account", None)
            run_online.atomic_json(runner.run_dir / "run-state.json", state)
            requests = []
            resumed = self.make_runner(root, self.account_adapter(requests), resume=runner.run_dir,
                                       expected_account="tb-store:operator")
            resumed.collect("qianniu", "test", {}, other_path, "other")
            self.assertEqual(requests, [], "successful non-context checkpoint must be reused")
            self.assertEqual(other_receipt.read_bytes(), original_receipt)
            resumed._context()
            self.assertTrue(resumed.stage_is_done("context"))
            self.assertEqual(run_online.read_json(resumed.run_dir / "run-state.json")["expected_account"], "tb-store:operator")
            self.assertTrue(all(payload.get("expected_account") == "tb-store:operator"
                                for site, operation, payload in requests if site == "qianniu" and operation == "context"))

    def test_legacy_committed_context_cannot_add_account_on_resume(self):
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                fake = FakeAdapter()
                def adapter(site, operation, input_path, output_path):
                    if partial and site == "jst":
                        raise run_online.OnlineError("login required", "auth_required")
                    return fake(site, operation, input_path, output_path)
                runner = self.make_runner(root, adapter)
                if partial:
                    with self.assertRaises(run_online.OnlineError):
                        runner._context()
                else:
                    runner._context()
                before = (runner.run_dir / "run-state.json").read_bytes()
                with self.assertRaises(run_online.OnlineError) as error:
                    self.make_runner(root, adapter, resume=runner.run_dir, expected_account="tb-store:operator")
                self.assertEqual(error.exception.code, "resume_mismatch")
                self.assertEqual((runner.run_dir / "run-state.json").read_bytes(), before)

    def test_resume_rejects_changed_observed_store_or_account_even_if_display_store_matches(self):
        for changed in ({"observed_store": "another-store"}, {"account_nick": "tb-store:another-operator"}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runner = self.make_runner(root, self.account_adapter([]), expected_account="tb-store:operator")
                runner._context()
                resumed = self.make_runner(root, self.account_adapter([], **changed), resume=runner.run_dir)
                with self.assertRaises(run_online.OnlineError) as error:
                    resumed._context()
                self.assertEqual(error.exception.code, "context_changed")
                self.assertEqual(error.exception.site, "qianniu")

    def test_legacy_snapshot_without_observed_identity_fields_still_refreshes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self.make_runner(root, FakeAdapter())
            runner._context()
            requests = []
            resumed = self.make_runner(root, self.account_adapter(requests), resume=runner.run_dir)
            resumed._context()
            self.assertTrue(resumed.stage_is_done("context_refresh"))
            request = next(payload for site, operation, payload in requests if site == "qianniu")
            self.assertNotIn("expected_observed_store", request)
            self.assertNotIn("expected_account_nick", request)

    def test_connect_only_is_explicit_and_homepage_config_remains_unchanged(self):
        self.assertFalse(run_online.build_parser().parse_args([]).connect_only)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            config = json.loads(qianniu.read_text(encoding='utf-8'))
            config['browser_sessions'] = {'home': {'url': 'https://myseller.taobao.com/'}}
            qianniu.write_text(json.dumps(config), encoding='utf-8')
            original = qianniu.read_bytes()
            with patch('playwright_adapter.PlaywrightAdapterRunner') as factory:
                runner = self.make_runner(root, browser_config=qianniu, jst_browser_config=jst,
                                          connect_only=True)
                self.assertEqual(set(runner.browser_config['browser_sessions']), {'invoice', 'orders'})
                runner._connect_browser()
                self.assertFalse(factory.call_args.kwargs['launch_if_needed'])
                self.assertEqual(qianniu.read_bytes(), original)

    def test_split_browser_option_routes_to_adapter_and_preserves_single_default(self):
        parser = run_online.build_parser()
        self.assertIsNone(parser.parse_args([]).jst_browser_config)
        self.assertEqual(parser.parse_args(["--jst-browser-config", "shared.json"]).jst_browser_config,
                         Path("shared.json"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            for shared in (None, jst):
                with self.subTest(shared=shared), patch("playwright_adapter.PlaywrightAdapterRunner") as factory:
                    runner = self.make_runner(root, browser_config=qianniu, jst_browser_config=shared)
                    factory.assert_not_called()
                    with run_online.ActiveLock(runner.output_root):
                        runner._connect_browser()
                    expected = {"progress": runner.progress}
                    if shared is not None:
                        expected["jst_config_path"] = jst.resolve()
                    factory.assert_called_once_with(qianniu.resolve(), **expected)
                    self.assertIs(runner.invoker.adapter_runner, factory.return_value)

    def test_split_context_digest_binds_shared_browser_without_exposing_port(self):
        from run_invoice import context_digest
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            fake = FakeAdapter()
            runner = self.make_runner(root, fake, browser_config=qianniu, jst_browser_config=jst)
            runner._context()
            context = run_online.read_json(runner.input_dir / "capture_context.json")
            self.assertEqual(context["context_sha256"], context_digest(context))
            self.assertEqual(context["jst_browser_identity_sha256"], run_online.stable_sha256(runner.jst_browser_identity))
            self.assertNotIn("19371", json.dumps(context))
            self.assertEqual(runner.state["jst_browser_config_path"], str(jst.resolve()))
            self.assertEqual(runner.state["jst_browser_config"]["remote_debugging_port"], 19371)
            resumed = self.make_runner(root, fake, resume=runner.run_dir)
            self.assertEqual(resumed.jst_browser_config_path, jst.resolve())
            resumed._context()
            self.assertEqual(fake.calls, [("qianniu", "context"), ("jst", "context")]*2)
            single = self.make_runner(root, FakeAdapter())
            single._context()
            single_context = run_online.read_json(single.input_dir / "capture_context.json")
            self.assertNotIn("jst_browser_identity_sha256", single_context)

    def test_shared_browser_resume_accepts_equivalent_normalized_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            runner = self.make_runner(root, FakeAdapter(), browser_config=qianniu, jst_browser_config=jst)
            updated = run_online.read_json(jst)
            updated["user_data_dir"] = "./shared-data"
            updated["browser"] = "edge"
            updated["playwright"] = {"profile_directory": updated.pop("profile_directory"),
                                     "remote_debugging_port": str(updated.pop("remote_debugging_port"))}
            updated["browser_sessions"]["goods"] = {"href": updated["browser_sessions"]["goods"]}
            jst.write_text(json.dumps(updated), encoding="utf-8")
            resumed = self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
            self.assertEqual(resumed.jst_browser_identity, runner.jst_browser_identity)

    def test_shared_browser_resume_rejects_changed_environment_or_config_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            original = run_online.read_json(jst)
            runner = self.make_runner(root, FakeAdapter(), browser_config=qianniu, jst_browser_config=jst)
            before = (runner.run_dir / "run-state.json").read_bytes()
            replacements = (("user_data_dir", str(root / "other-shared-data")),
                            ("profile_directory", "Profile 2"), ("remote_debugging_port", 19372),
                            ("browser_sessions", {"goods": "https://fp.erp321.com/other"}))
            for key, value in replacements:
                with self.subTest(key=key):
                    jst.write_text(json.dumps({**original, key: value}), encoding="utf-8")
                    with self.assertRaises(run_online.OnlineError) as error:
                        self.make_runner(root, FakeAdapter(), resume=runner.run_dir)
                    self.assertEqual(error.exception.code, "resume_mismatch")
                    self.assertEqual(error.exception.site, "jst")
            alternate = root / "copied-shared.json"
            alternate.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaises(run_online.OnlineError) as error:
                self.make_runner(root, FakeAdapter(), resume=runner.run_dir, jst_browser_config=alternate)
            self.assertEqual(error.exception.code, "resume_mismatch")
            self.assertEqual(error.exception.site, "jst")
            self.assertEqual((runner.run_dir / "run-state.json").read_bytes(), before)

    def test_single_browser_resume_cannot_switch_to_shared_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, jst = self.split_configs(root)
            runner = self.make_runner(root, FakeAdapter())
            with self.assertRaises(run_online.OnlineError) as error:
                self.make_runner(root, FakeAdapter(), resume=runner.run_dir, jst_browser_config=jst)
            self.assertEqual(error.exception.code, "resume_mismatch")
            self.assertEqual(error.exception.site, "jst")

    def test_browser_start_failure_keeps_site_and_redacts_configuration_from_report(self):
        from playwright_adapter import PlaywrightAdapterError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            failure = PlaywrightAdapterError("shared browser disconnected", "browser_disconnected", site="jst")
            runner = self.make_runner(root, browser_config=qianniu, jst_browser_config=jst)
            with patch("playwright_adapter.PlaywrightAdapterRunner", side_effect=failure):
                with self.assertRaises(run_online.OnlineError) as error:
                    runner.run()
            self.assertEqual((error.exception.code, error.exception.site), ("browser_disconnected", "jst"))
            report = run_online.read_json(runner.run_dir / "run.json")
            self.assertEqual(report["error_site"], "jst")
            self.assertNotIn("jst_browser_config", report)
            self.assertNotIn("browser_config", report)
            self.assertNotIn("19371", json.dumps(report))
            self.assertEqual(run_online.read_json(runner.run_dir / "run-state.json")["error_site"], "jst")

    def test_site_error_in_data_collection_is_recorded_for_queue_policy(self):
        from playwright_adapter import PlaywrightAdapterError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = FakeAdapter()
            def adapter(site, operation, input_path, output_path):
                if site == "jst":
                    raise PlaywrightAdapterError("login required", "auth_required", site="jst")
                return fake(site, operation, input_path, output_path)
            runner = self.make_runner(root, adapter)
            with self.assertRaises(PlaywrightAdapterError):
                runner.run()
            self.assertEqual(runner.state["error_site"], "jst")
            attempt = runner.state["stages"]["context"]["attempts"][-1]
            self.assertEqual(attempt["error_site"], "jst")

    def test_split_pages_are_merged_by_business_site(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            qianniu, jst = self.split_configs(root)
            def adapter(site, operation, input_path, output_path):
                if site == "qianniu":
                    value = {"isLogin": True, "store": "店", "agentId": "test-agent",
                             "browser_pages": {"invoice": "invoice-target", "orders": "orders-target"}}
                else:
                    value = {"isLogin": True, "issuer": "主体", "coid": "test-coid", "uid": "test-uid",
                             "browser_pages": {"goods": "goods-target"}}
                return {"payload": value}
            runner = self.make_runner(root, adapter, browser_config=qianniu, jst_browser_config=jst)
            runner._context()
            expected = {"qianniu": {"invoice": "invoice-target", "orders": "orders-target"},
                        "jst": {"goods": "goods-target"}}
            self.assertEqual(runner.state["browser_pages"], expected)
            resumed = self.make_runner(root, adapter, resume=runner.run_dir)
            resumed._context()
            self.assertEqual(resumed.state["browser_pages"], expected)

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
