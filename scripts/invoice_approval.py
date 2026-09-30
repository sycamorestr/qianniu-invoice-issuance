"""Agree to the frozen export selection once, with durable write intents."""
from __future__ import annotations

from pathlib import Path
import json
import uuid

from invoice_scope import scope_from_record, scope_fields

LEGACY_BATCH_SIZE = 20


def validate_response(operation, value, request, *, error_type=None):
    """Bind every write/read result to the exact application IDs and scope."""
    if error_type is None:
        from run_online import OnlineError as error_type
    OnlineError = error_type

    def require(condition, message):
        if not condition:
            raise OnlineError(message, "checkpoint_invalid", site="qianniu")

    require(isinstance(value, dict) and value.get("operation") == operation,
            "批量同意响应操作无效")
    try:
        require(scope_from_record(value) == scope_from_record(request), "批量同意响应范围不符")
    except (ValueError, TypeError, KeyError) as exc:
        raise OnlineError("批量同意响应范围无效", "checkpoint_invalid", site="qianniu") from exc
    rows = value.get("applications")
    expected = request.get("applications")
    require(isinstance(rows, list) and isinstance(expected, list), "批量同意响应缺少申请清单")
    require(all(isinstance(row, dict) and isinstance(row.get("serialNo"), str)
                and isinstance(row.get("tid"), str) for row in rows), "批量同意响应申请格式无效")
    actual_ids = [(row["serialNo"], row["tid"]) for row in rows]
    expected_ids = [(row["serialNo"], row["tid"]) for row in expected]
    require(len(actual_ids) == len(set(actual_ids)) and set(actual_ids) == set(expected_ids),
            "批量同意响应申请范围不符")
    if operation == "approval-status":
        require(all(row.get("status") in {"pending", "agreed", "unknown"} for row in rows),
                "批量同意状态无效")
        require(isinstance(value.get("checked_at"), str) and bool(value["checked_at"]),
                "批量同意状态缺少查询时间")
    else:
        require(type(value.get("code")) is int and value["code"] == 200,
                "批量同意未返回业务成功回执")
        require(isinstance(value.get("approved_at"), str) and bool(value["approved_at"]),
                "批量同意回执缺少操作时间")


def approve_selected(runner, *, api):
    """Preserve export evidence first; an existing intent never permits a POST."""
    # Use the caller's module even when run_online.py executes as __main__;
    # importing it a second time would create an incompatible OnlineError type.
    OnlineError, atomic_json, chunked = api.OnlineError, api.atomic_json, api.chunked
    file_sha256, read_json = api.file_sha256, api.read_json
    replace_checkpoint, stable_sha256, utc_now = api.replace_checkpoint, api.stable_sha256, api.utc_now
    from collection_files import validate_selection_scope
    from run_invoice import validate_application_snapshot

    if runner.stage_is_done("approval"):
        return
    if (runner.query_scope.get("countdown") != "started"
            or not runner.stage_is_done("applications") or not runner.stage_is_done("export")):
        raise OnlineError("批量同意需要已保全的原件及完整已开始筛选快照", "checkpoint_invalid", site="qianniu")
    runner._prepare_selection()
    selection_path = runner.input_dir / "selection.json"
    selection = read_json(selection_path)
    order_ids = read_json(runner.input_dir / "order_ids.json")
    validate_selection_scope(runner.input_dir, order_ids, "批量同意")
    context = read_json(runner.input_dir / "capture_context.json")
    snapshot = read_json(runner.input_dir / "applications.json")
    observed = validate_application_snapshot(snapshot)
    selected = selection.get("selected_application_ids")
    if (not isinstance(selected, list) or len(selected) != len(set(selected))
            or any(not isinstance(serial, str) or not serial or serial not in observed for serial in selected)):
        raise OnlineError("批量同意选择清单无效", "checkpoint_invalid", site="qianniu")
    applications = [{"serialNo": serial, "tid": str(observed[serial]["tid"])} for serial in selected]
    batch_mode = runner.state.get("approval_batch_mode", "legacy_20")
    if batch_mode not in {"single_request", "legacy_20"}:
        raise OnlineError("批量同意提交模式无效", "checkpoint_invalid", site="qianniu")
    # New jobs send the entire frozen selection. Old jobs retain their exact
    # twenty-row boundaries so existing durable intents can never be rebatched.
    batch_size = max(1, len(applications)) if batch_mode == "single_request" else LEGACY_BATCH_SIZE
    if any(not row["tid"] for row in applications):
        raise OnlineError("批量同意选择缺少订单号", "checkpoint_invalid", site="qianniu")
    if applications and any(not context.get(name) for name in ("observed_store", "account_nick", "agentId")):
        raise OnlineError("批量同意缺少原店铺及完整子账号证据", "checkpoint_invalid", site="qianniu")
    binding = {
        "date": runner.date, **scope_fields(runner.query_scope),
        "store": runner.store, "issuer": runner.issuer, "agentId": context.get("agentId"),
        "input_hashes": {name: file_sha256(runner.input_dir / name) for name in
                         ("capture_context.json", "applications.json", "qianniu_common.xlsx",
                          "selection.json", "order_ids.json")},
    }
    root = runner.run_dir / "approval"
    root.mkdir(exist_ok=True)
    runner.stage_start("approval")
    all_evidence = []
    recovered_count = 0

    def proof(paths):
        return [{"path": str(path.resolve()), "sha256": file_sha256(path)} for path in paths]

    def check_proof(items):
        if not isinstance(items, list) or not items:
            raise OnlineError("批量同意检查点缺少证据", "checkpoint_invalid", site="qianniu")
        for item in items:
            path = Path(item["path"])
            if (not path.is_relative_to(runner.run_dir) or not path.is_file()
                    or file_sha256(path) != item.get("sha256")):
                raise OnlineError("批量同意证据缺失或变化", "resume_mismatch", site="qianniu")

    def status(request, prefix):
        key = f"{prefix}-status-{uuid.uuid4().hex[:12]}"
        path = root / (key + ".json")
        runner.collect("qianniu", "approval-status", request, path, key)
        return read_json(path), path

    for index, batch in enumerate(chunked(applications, batch_size), 1):
        key = f"approval-{index:03d}"
        intent_path = root / (key + ".intent.json")
        complete_path = root / (key + ".complete.json")
        result_path = root / (key + ".response.json")
        request = {
            "agentId": str(context["agentId"]), "applications": batch,
            "expected_observed_store": context["observed_store"],
            "expected_account_nick": context["account_nick"],
            "approval_binding": binding,
        }
        if runner.expected_account is not None:
            request["expected_account"] = runner.expected_account
        identity = {"binding": binding, "applications": batch, "request": request}
        identity_hash = stable_sha256(identity)
        completed_source = (complete_path if complete_path.is_file()
                            else complete_path.with_name(complete_path.name + ".partial"))
        if completed_source.is_file():
            completed = read_json(completed_source)
            if (completed.get("version") != 1 or completed.get("identity_sha256") != identity_hash
                    or completed.get("applications") != batch or completed.get("status") != "agreed"):
                raise OnlineError("批量同意已完成检查点身份变化", "resume_mismatch", site="qianniu")
            check_proof(completed.get("evidence"))
            if completed_source != complete_path:
                replace_checkpoint(completed_source, complete_path)
            all_evidence.extend(Path(item["path"]) for item in completed["evidence"])
            all_evidence.append(complete_path)
            recovered_count += len(batch) if completed.get("recovered") else 0
            continue
        had_intent = intent_path.exists()
        if not had_intent and intent_path.with_name(intent_path.name + ".partial").exists():
            raise OnlineError("批量同意发送意图未完整发布，需人工核对", "approval_unknown", site="qianniu")
        evidence = []
        if had_intent:
            intent = read_json(intent_path)
            if (intent.get("version") != 1 or intent.get("identity_sha256") != identity_hash
                    or intent.get("identity") != identity):
                raise OnlineError("批量同意发送意图身份变化", "resume_mismatch", site="qianniu")
            check_proof(intent.get("preflight_evidence"))
            evidence.extend(Path(item["path"]) for item in intent["preflight_evidence"])
        else:
            current, preflight = status(request, key + "-before")
            if any(row["status"] != "pending" for row in current["applications"]):
                raise OnlineError("选中申请已不是全部待处理，未执行批量同意", "approval_state_changed", site="qianniu")
            evidence.append(preflight)
            atomic_json(intent_path, {"version": 1, "created_at": utc_now(), "identity": identity,
                                      "identity_sha256": identity_hash, "preflight_evidence": proof(evidence)})
        evidence.append(intent_path)
        try:
            # An existing intent means the POST may already have reached the server.
            # Recover complete local publications only; never send it again.
            journal = runner.run_dir / "publications" / (key + ".json")
            receipt = runner.run_dir / "receipts" / (key + ".json")
            recoverable = (journal.is_file() or journal.with_name(journal.name + ".partial").is_file()
                           or (result_path.is_file() and receipt.is_file()))
            if not had_intent or recoverable:
                runner.collect("qianniu", "approve", request, result_path, key)
                evidence.extend((result_path, receipt))
            current, verified = status(request, key + "-after")
            evidence.append(verified)
            if any(row["status"] != "agreed" for row in current["applications"]):
                raise OnlineError("批量同意后未能确认全部申请已同意", "approval_unknown", site="qianniu")
        except Exception as exc:
            raise OnlineError("批量同意已发送或结果可能未知；已保留发送意图，恢复仅核对状态，不重复提交。"
                              + str(exc), "approval_unknown", site="qianniu") from exc
        # Include read receipts and adapter inputs, not merely the returned statuses.
        for path in list(evidence):
            checkpoint = next((value for value in runner.state.get("checkpoints", {}).values()
                               if value.get("outputPath") == str(path.resolve())), None)
            if checkpoint:
                evidence.extend((Path(checkpoint["receiptPath"]), Path(checkpoint["inputPath"])))
        evidence = list(dict.fromkeys(evidence))
        atomic_json(complete_path, {"version": 1, "identity_sha256": identity_hash,
                                   "applications": batch, "status": "agreed", "verified_at": utc_now(),
                                   "recovered": had_intent, "evidence": proof(evidence)})
        all_evidence.extend(evidence + [complete_path])
        recovered_count += len(batch) if had_intent else 0
    summary = {"status": "complete", "selected_count": len(applications),
               "agreed_count": len(applications), "recovered_count": recovered_count,
               "batch_count": (len(applications) + batch_size - 1) // batch_size,
               "batch_mode": batch_mode,
               "binding": binding}
    summary_path = root / "summary.json"
    runner.invoker._commit_saved_bytes(summary_path,
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    runner.state["application_approval"] = summary
    runner.stage_done("approval", list(dict.fromkeys(all_evidence + [summary_path])))
