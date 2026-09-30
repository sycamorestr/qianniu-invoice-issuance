"""Mutation boundaries and uncertain-write recovery; no browser or network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_online
from invoice_approval import validate_response
from invoice_scope import scope_fields


class ApprovalAdapter:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.timeout = False
        self.commit = True
        self.post_status = None

    def __call__(self, site, operation, input_path, output_path):
        request = run_online.read_json(input_path)
        self.calls.append((operation, request))
        base = {"operation": operation, "date": request["date"],
                "query_scope": request["query_scope"]}
        if operation == "approval-status":
            return {"payload": {**base, "checked_at": "2026-09-30T00:00:00Z",
                    "applications": [{**row, "status": self.states.get(row["serialNo"], "pending")}
                                     for row in request["applications"]]}}
        if operation == "approve":
            assert (output_path.parent / (input_path.stem + ".intent.json")).is_file()
            if self.commit:
                self.states.update({row["serialNo"]: self.post_status or "agreed"
                                    for row in request["applications"]})
            if self.timeout:
                raise TimeoutError("simulated response loss")
            return {"payload": {**base, "applications": request["applications"],
                    "code": 200, "message": "成功", "approved_at": "2026-09-30T00:00:00Z"}}
        raise AssertionError(operation)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = ApprovalAdapter()

    def make_runner(self, **kwargs):
        return run_online.OnlineRunner(date="2026-09-30", store="店铺别名", issuer="主体",
                                       output_root=self.root / "outputs", adapter_runner=self.adapter,
                                       **kwargs)

    def prepared(self, count=1, **kwargs):
        runner = self.make_runner(**kwargs)
        root = runner.input_dir
        scope = {"date": runner.date, **scope_fields(runner.query_scope)}
        context = {**scope, "store": runner.store, "issuer": runner.issuer, "agentId": "agent-1",
                   "observed_store": "平台店名", "account_nick": "平台店名:子账号"}
        rows = [{"serialNo": f"A-{i}", "tid": f"T-{i}"} for i in range(count)]
        run_online.atomic_json(root / "capture_context.json", context)
        run_online.atomic_json(root / "applications.json", {**scope, "rows": rows,
            "total": count, "api_total": count, "observed_total": count, "queried_at": "now",
            "list_non_pending_snapshot_rows": []})
        (root / "qianniu_common.xlsx").write_bytes(b"immutable original pending export")
        selection = {"selected_application_ids": [row["serialNo"] for row in rows],
            "common_template_sha256": run_online.file_sha256(root / "qianniu_common.xlsx"),
            "applications_sha256": run_online.file_sha256(root / "applications.json"),
            "query_scope": runner.query_scope}
        run_online.atomic_json(root / "selection.json", selection)
        run_online.atomic_json(root / "order_ids.json", {
            "selected_application_ids": selection["selected_application_ids"], "active_application_ids": [],
            "orders": [], "common_template_sha256": selection["common_template_sha256"],
            "selection_sha256": run_online.file_sha256(root / "selection.json")})
        runner.stage_done("applications", [root / "applications.json"])
        runner.stage_done("export", [root / "qianniu_common.xlsx"])
        runner.stage_done("orders_prepare", [root / "selection.json", root / "order_ids.json"])
        return runner

    def writes(self):
        return [request for operation, request in self.adapter.calls if operation == "approve"]

    def test_batches_all_selected_after_preserving_original_and_reuses_success(self):
        runner = self.prepared(count=61)
        original = (runner.input_dir / "qianniu_common.xlsx").read_bytes()
        runner._approve_applications()
        self.assertEqual([len(item["applications"]) for item in self.writes()], [61])
        self.assertEqual([op for op, _ in self.adapter.calls],
                         ["approval-status", "approve", "approval-status"])
        self.assertEqual(self.writes()[0]["expected_account_nick"], "平台店名:子账号")
        self.assertEqual(runner.state["application_approval"]["agreed_count"], 61)
        self.assertEqual(runner.state["application_approval"]["batch_count"], 1)
        self.assertEqual((runner.input_dir / "qianniu_common.xlsx").read_bytes(), original)
        self.adapter.calls.clear()
        self.make_runner(resume=runner.run_dir)._approve_applications()
        self.assertEqual(self.adapter.calls, [])

    def test_lost_response_recovers_only_by_reading_all_agreed(self):
        runner = self.prepared(count=61)
        self.adapter.timeout = True
        with self.assertRaises(run_online.OnlineError) as error:
            runner._approve_applications()
        self.assertEqual(error.exception.code, "approval_unknown")
        self.adapter.calls.clear()
        resumed = self.make_runner(resume=runner.run_dir)
        resumed._approve_applications()
        self.assertEqual([op for op, _ in self.adapter.calls], ["approval-status"])
        self.assertEqual(resumed.state["application_approval"]["recovered_count"], 61)

    def test_legacy_job_keeps_twenty_row_batches_when_recovering_first_intent(self):
        runner = self.prepared(count=21)
        runner.state.pop("approval_batch_mode")
        runner._write_state()
        self.adapter.timeout = True
        with self.assertRaises(run_online.OnlineError):
            runner._approve_applications()
        self.assertEqual([len(item["applications"]) for item in self.writes()], [20])
        self.adapter.calls.clear()
        self.adapter.timeout = False
        resumed = self.make_runner(resume=runner.run_dir)
        resumed._approve_applications()
        self.assertEqual([len(item["applications"]) for item in self.writes()], [1])
        self.assertEqual(resumed.state["application_approval"]["batch_count"], 2)
        self.assertEqual(resumed.state["application_approval"]["recovered_count"], 20)

    def test_lost_response_still_pending_never_reposts_on_resume(self):
        runner = self.prepared()
        self.adapter.timeout, self.adapter.commit = True, False
        with self.assertRaises(run_online.OnlineError):
            runner._approve_applications()
        for _ in range(2):
            self.adapter.calls.clear()
            resumed = self.make_runner(resume=runner.run_dir)
            with self.assertRaises(run_online.OnlineError) as error:
                resumed._approve_applications()
            self.assertEqual(error.exception.code, "approval_unknown")
            self.assertEqual([op for op, _ in self.adapter.calls], ["approval-status"])

    def test_success_response_requires_postcondition_and_recovery_never_reposts(self):
        runner = self.prepared()
        self.adapter.post_status = "unknown"
        with self.assertRaises(run_online.OnlineError) as error:
            runner._approve_applications()
        self.assertEqual(error.exception.code, "approval_unknown")
        self.adapter.states["A-0"] = "agreed"
        self.adapter.calls.clear()
        self.make_runner(resume=runner.run_dir)._approve_applications()
        self.assertEqual([op for op, _ in self.adapter.calls], ["approval-status"])

    def test_local_receipt_publish_failure_recovers_without_reposting(self):
        runner = self.prepared()
        replace = run_online.replace_checkpoint
        def fail_receipt(source, target):
            if target.parent.name == "receipts" and target.name == "approval-001.json":
                raise run_online.OnlineError("simulated disk rename failure", "checkpoint_write_failed")
            return replace(source, target)
        with patch.object(run_online, "replace_checkpoint", side_effect=fail_receipt):
            with self.assertRaises(run_online.OnlineError):
                runner._approve_applications()
        self.adapter.calls.clear()
        resumed = self.make_runner(resume=runner.run_dir)
        resumed._approve_applications()
        self.assertEqual([op for op, _ in self.adapter.calls], ["approval-status"])
        self.assertTrue((runner.run_dir / "receipts/approval-001.json").is_file())

    def test_preflight_changed_state_and_missing_export_never_write(self):
        runner = self.prepared()
        self.adapter.states["A-0"] = "agreed"
        with self.assertRaises(run_online.OnlineError) as error:
            runner._approve_applications()
        self.assertEqual(error.exception.code, "approval_state_changed")
        self.assertEqual(self.writes(), [])
        runner.state["stages"].pop("export")
        with self.assertRaises(run_online.OnlineError):
            runner._approve_applications()
        self.assertEqual(self.writes(), [])

    def test_zero_selection_has_no_browser_write_or_status_call(self):
        runner = self.prepared(count=0)
        runner._approve_applications()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(runner.state["application_approval"]["agreed_count"], 0)

    def test_policy_new_default_legacy_resume_and_plan_replay_are_read_only(self):
        runner = self.make_runner()
        self.assertTrue(runner.state["approve_applications"])
        runner.state.pop("approve_applications")
        runner._write_state()
        resumed = self.make_runner(resume=runner.run_dir)
        self.assertFalse(resumed.approve_applications)
        resumed._approve_applications()
        with self.assertRaises(run_online.OnlineError):
            self.make_runner(resume=runner.run_dir, approve_applications=True)
        planned = self.make_runner(plan_only=True)
        self.assertFalse(planned.state["approve_applications"])
        planned._approve_applications()
        source = self.root / "replay-input"
        source.mkdir()
        replay = self.make_runner(replay_input=source)
        self.assertFalse(replay.state["approve_applications"])
        replay._approve_applications()
        self.assertEqual(self.adapter.calls, [])

    def test_corrupt_completed_evidence_stops_before_any_browser_request(self):
        runner = self.prepared()
        runner._approve_applications()
        intent = runner.run_dir / "approval/approval-001.intent.json"
        intent.write_text("{}", encoding="utf-8")
        self.adapter.calls.clear()
        with self.assertRaises(run_online.OnlineError):
            self.make_runner(resume=runner.run_dir)._approve_applications()
        self.assertEqual(self.adapter.calls, [])

    def test_incomplete_completion_publication_is_recovered_without_browser_calls(self):
        runner = self.prepared()
        original = run_online.replace_checkpoint
        def fail_completion(source, target):
            if target.name == "approval-001.complete.json":
                raise run_online.OnlineError("simulated completion rename", "checkpoint_write_failed")
            return original(source, target)
        with patch.object(run_online, "replace_checkpoint", side_effect=fail_completion):
            with self.assertRaises(run_online.OnlineError):
                runner._approve_applications()
        self.adapter.calls.clear()
        resumed = self.make_runner(resume=runner.run_dir)
        resumed._approve_applications()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(resumed.state["application_approval"]["agreed_count"], 1)

    def test_corrupt_intent_binding_stops_before_recovery_query(self):
        runner = self.prepared()
        self.adapter.timeout = True
        with self.assertRaises(run_online.OnlineError):
            runner._approve_applications()
        intent_path = runner.run_dir / "approval/approval-001.intent.json"
        intent = run_online.read_json(intent_path)
        intent["identity"]["applications"][0]["tid"] = "other-order"
        intent_path.write_text(json.dumps(intent), encoding="utf-8")
        self.adapter.calls.clear()
        with self.assertRaises(run_online.OnlineError) as error:
            self.make_runner(resume=runner.run_dir)._approve_applications()
        self.assertEqual(error.exception.code, "resume_mismatch")
        self.assertEqual(self.adapter.calls, [])

    def test_response_validation_rejects_wrong_identity_or_status(self):
        request = {"date": "2026-09-30", "query_scope": {"mode": "date", "date": "2026-09-30",
            "countdown": "started"}, "applications": [{"serialNo": "A", "tid": "T"}]}
        response = {**request, "operation": "approval-status", "checked_at": "now",
                    "applications": [{"serialNo": "A", "tid": "T", "status": "pending"}]}
        validate_response("approval-status", response, request)
        for change in ({"tid": "wrong"}, {"status": "unexpected"}):
            changed = {**response, "applications": [{**response["applications"][0], **change}]}
            with self.assertRaises(run_online.OnlineError):
                validate_response("approval-status", changed, request)


if __name__ == "__main__":
    unittest.main()
