from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from playwright_adapter import PlaywrightAdapterError, PlaywrightAdapterRunner


class PlaywrightApprovalTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "operation": "approve", "date": None,
            "query_scope": {"mode": "all_pending", "start_date": "2026-07-30",
                            "end_date": "2026-09-30", "countdown": "started"},
            "agentId": "0", "expected_store": "Friendly shop", "expected_observed_store": "shop",
            "expected_account_nick": "shop:operator",
            "applications": [{"serialNo": "E1", "tid": "9000000000000000001"}],
        }
        self.receipt = {key: deepcopy(self.payload[key]) for key in ("operation", "date", "query_scope", "applications")}
        self.receipt.update(code=200, message="操作成功", approved_at="2026-09-30T00:00:00Z")
        self.controller = Mock(evaluate_file=AsyncMock(side_effect=[{"isLogin": True}, self.receipt]))
        self.runner = object.__new__(PlaywrightAdapterRunner)
        self.runner.skill_dir = Path(__file__).resolve().parent
        self.runner.controllers = {"qianniu": self.controller}

    def invoke(self, payload=None, operation="approve"):
        return asyncio.run(self.runner._invoke_for_site("qianniu", operation, payload or self.payload, False))

    def test_context_check_precedes_exact_approval_request(self):
        self.assertEqual(self.invoke(), self.receipt)
        first, second = self.controller.evaluate_file.await_args_list
        self.assertEqual(first.args[1].name, "playwright_context_qianniu.js")
        self.assertEqual(first.args[2]["expected_account"], "shop:operator")
        self.assertEqual(first.args[2]["expected_observed_store"], "shop")
        self.assertEqual(second.args[1].name, "approve_qianniu.js")
        self.assertEqual(second.args[2], self.payload)

    def test_identity_failure_stops_before_mutation(self):
        self.controller.evaluate_file.side_effect = PlaywrightAdapterError("account changed", "context_changed")
        with self.assertRaises(PlaywrightAdapterError):
            self.invoke()
        self.controller.evaluate_file.assert_awaited_once()

    def test_complete_selection_over_one_page_is_forwarded_once(self):
        rows = [{"serialNo": f"E{i}", "tid": str(9000000000000000001 + i)} for i in range(61)]
        self.payload["applications"] = rows
        self.receipt["applications"] = deepcopy(rows)
        self.assertEqual(self.invoke()["applications"], rows)
        self.assertEqual(self.controller.evaluate_file.await_count, 2)
        self.assertEqual(self.controller.evaluate_file.await_args.args[2]["applications"], rows)

    def test_invalid_targets_and_identity_make_no_browser_calls(self):
        for changes in ({"applications": [{"serialNo": "E1", "tid": 9000000000000000001}]},
                        {"applications": self.payload["applications"] * 2},
                        {"expected_account_nick": ""}, {"expected_observed_store": ""},
                        {"repeatCheck": False}, {"autoCreate": 0}):
            with self.subTest(changes=changes), self.assertRaises(PlaywrightAdapterError):
                self.invoke({**self.payload, **changes})
        self.controller.evaluate_file.assert_not_awaited()

    def test_wrong_response_ids_do_not_become_success(self):
        self.receipt["applications"][0]["tid"] = "another-order"
        with self.assertRaises(PlaywrightAdapterError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.code, "response_invalid")

    def test_status_is_validated_without_reinterpreting_unknown(self):
        receipt = {key: deepcopy(self.payload[key]) for key in ("date", "query_scope", "applications")}
        receipt.update(operation="approval-status", checked_at="2026-09-30T00:00:00Z")
        receipt["applications"][0]["status"] = "unknown"
        self.controller.evaluate_file.side_effect = [{"isLogin": True}, receipt]
        self.assertEqual(self.invoke(operation="approval-status"), receipt)


if __name__ == "__main__":
    unittest.main()
