"""Single-command online coordinator for the Qianniu invoice workflow.

The browser adapter is deliberately a small in-process boundary: it receives
one request file and returns business data. The runner persists that response
as an immutable raw checkpoint. Everything after that boundary is local and
resumable. This module never
submits an invoice; it only collects the read-only inputs and delegates final
workbook creation to :mod:`run_invoice`.
"""

from __future__ import annotations

import argparse
import base64
import errno
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent
COLLECTOR = HERE / "collection_files.py"
RUN_INVOICE = HERE / "run_invoice.py"

# The JST page script uses eight workers. Forty codes keep each response a
# bounded recovery unit while avoiding unnecessary page-evaluate round trips.
JST_BATCH_SIZE = 40
QIANNIU_BROWSER_CONFIG_ENV = "QIANNIU_BROWSER_CONFIG"
SUPPORTED_BROWSER_CONFIG_SCHEMA = 1


class OnlineError(RuntimeError):
    """A typed, user-actionable online workflow error."""

    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


def load_browser_config(config_path: Path | None = None) -> tuple[dict[str, Any], Path | None]:
    """Load only an explicit path or the documented environment variable."""
    selected = config_path
    if selected is None:
        env_path = os.environ.get(QIANNIU_BROWSER_CONFIG_ENV, "").strip()
        selected = Path(env_path) if env_path else None
    if selected is None:
        return {}, None
    path = Path(selected).expanduser().resolve()
    if not path.is_file():
        raise OnlineError(f"浏览器配置不存在: {path}", "configuration")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnlineError(f"浏览器配置无法读取: {path}: {exc}", "configuration") from exc
    if not isinstance(value, dict):
        raise OnlineError("浏览器配置必须是 JSON 对象", "configuration")
    if value.get("schema_version") != SUPPORTED_BROWSER_CONFIG_SCHEMA:
        raise OnlineError(
            f"浏览器配置 schema_version 必须为 {SUPPORTED_BROWSER_CONFIG_SCHEMA}",
            "configuration",
        )
    missing = [key for key in ("browser", "user_data_dir")
               if not str(value.get(key) or "").strip()]
    if missing:
        raise OnlineError(f"浏览器配置缺少字段: {', '.join(missing)}", "configuration")
    sessions = value.get("browser_sessions")
    if sessions is not None:
        if not isinstance(sessions, dict):
            raise OnlineError("浏览器配置 browser_sessions 必须是对象", "configuration")
        unknown = sorted(set(sessions) - {"invoice", "orders", "goods"})
        if unknown:
            raise OnlineError(f"浏览器配置含未知 browser_sessions: {', '.join(unknown)}", "configuration")
    return dict(value), path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_sha256(payload: dict[str, Any]) -> str:
    """Page targets are transport hints, not the identity of business inputs."""
    return stable_sha256({key: value for key, value in payload.items() if key != "browser_pages"})


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnlineError(f"无法读取 JSON 检查点: {path}: {exc}", "checkpoint_invalid") from exc


def replace_checkpoint(source: Path, target: Path) -> None:
    """Retry only Windows sharing failures; never repeat the business request."""
    delays = (0.05, 0.1, 0.2, 0.4)
    for attempt in range(len(delays) + 1):
        try:
            source.replace(target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) in {5, 32, 33} and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            raise OnlineError(
                f"无法发布检查点，已保留原文件与临时文件: {target}",
                "checkpoint_write_failed",
            ) from exc


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        # A process can die after writing a recovery snapshot but before the
        # atomic rename.  The committed state is still authoritative; move
        # only this single stale state fragment aside so resume can continue.
        # Raw business checkpoints remain fail-closed because their partial
        # bytes could otherwise be mistaken for a complete response.
        if path.name == "run-state.json" and path.is_file():
            stale = path.with_name(
                f"{path.name}.partial.stale-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            )
            replace_checkpoint(partial, stale)
        else:
            raise OnlineError(f"检测到遗留临时检查点: {partial}", "checkpoint_invalid")
    # Mutable manifests use distinct staging files. Raw checkpoint paths stay
    # fail-closed so an interrupted response cannot be confused with new data.
    if path.name in {"run-state.json", "run.json"}:
        partial = path.with_name(f"{path.name}.partial-{uuid.uuid4().hex}")
    try:
        with partial.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise OnlineError(f"无法写入检查点: {partial}", "checkpoint_write_failed") from exc
    replace_checkpoint(partial, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def validate_raw_payload(site: str, operation: str, value: Any,
                         request: dict[str, Any]) -> None:
    """Do not publish transport receipts or failed authentication as data."""
    if not isinstance(value, dict) or {"outputPath", "sha256", "operation"} <= set(value):
        raise OnlineError("原始检查点不是业务数据（可能误用了回执文件）", "checkpoint_invalid")
    if value.get("blocked") is True:
        raise OnlineError(str(value.get("reason") or "页面采集被阻断"),
                          str(value.get("reason") or "adapter_failed"))
    if site == "jst" and operation == "query":
        rows = value.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise OnlineError("票聚批次缺少 data 数组", "checkpoint_invalid")
        codes = [str(row.get("input_goods_code") or "") for row in rows]
        if (len(codes) != len(set(codes)) or set(codes) != set(request.get("codes", []))
                or any(not isinstance(row.get("ok"), bool) for row in rows)):
            raise OnlineError("票聚批次编码范围或结果类型无效", "checkpoint_invalid")


class ActiveLock:
    """Create one lock per output root, with an explicit owner marker."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".qianniu-invoice-online.lock"
        self._owned = False
        self.recovered_stale_lock = False
        self.stale_lock_reason: str | None = None
        self._mutex: Any = None
        self._owner_token = uuid.uuid4().hex

    @staticmethod
    def _owner_alive(pid: int) -> bool:
        if sys.platform == "win32":
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                # ERROR_ACCESS_DENIED means the process exists but is not
                # inspectable; invalid/not-found handles indicate a dead PID.
                if error == 5:
                    return True
                if error in {2, 3, 87}:
                    return False
                return True
            exit_code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return getattr(exc, "errno", None) != errno.ESRCH
        return True

    def _read_owner(self) -> tuple[bool, str, tuple[int, int, int] | None]:
        try:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return True, "lock_changed", None
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(owner.get("pid"))
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            # A process can be between create and write. Keep a fresh
            # malformed lock for a short grace period instead of deleting it.
            if time.time() - stat.st_mtime < 5:
                return True, "malformed_recent_lock", signature
            return False, "malformed_lock", signature
        if pid <= 0:
            return False, "invalid_pid", signature
        alive = self._owner_alive(pid)
        return alive, ("active_process" if alive else "dead_process"), signature

    def __enter__(self) -> "ActiveLock":
        from browser_lock import FileMutex, FileMutexBusy
        self._mutex = FileMutex(self.path.with_suffix(".guard"))
        try:
            self._mutex.acquire()
        except FileMutexBusy as exc:
            raise OnlineError(f"已有在线执行正在运行: {self.path}", "already_running") from exc
        try:
            return self._acquire_owner_marker()
        except BaseException:
            self._mutex.release()
            raise

    def _acquire_owner_marker(self) -> "ActiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                alive, reason, signature = self._read_owner()
                if alive:
                    raise OnlineError(f"已有在线执行正在运行: {self.path}", "already_running") from exc
                try:
                    current = self.path.stat()
                    current_signature = (current.st_ino, current.st_mtime_ns, current.st_size)
                except FileNotFoundError:
                    continue
                if signature is not None and current_signature != signature:
                    continue
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as unlink_exc:
                    raise OnlineError(f"无法清理失效执行锁: {self.path}", "already_running") from unlink_exc
                self.recovered_stale_lock = True
                self.stale_lock_reason = reason
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "started_at": utc_now(),
                                     "owner_token": self._owner_token}))
        self._owned = True
        return self

    def __exit__(self, *_: Any) -> None:
        try:
            if self._owned:
                try:
                    owner = json.loads(self.path.read_text(encoding="utf-8"))
                    if owner.get("owner_token") == self._owner_token:
                        self.path.unlink(missing_ok=True)
                except (OSError, ValueError, AttributeError):
                    pass
                self._owned = False
        finally:
            if self._mutex is not None:
                self._mutex.release()


class AdapterInvoker:
    """Persist requests and publish responses from the browser adapter.

    ``adapter_runner`` has the signature ``(site, operation, input_path,
    output_path)``. The production Playwright adapter is connected once by
    OnlineRunner; tests and offline simulations can inject a callable.
    """

    def __init__(self, run_dir: Path,
                 adapter_runner: Callable[[str, str, Path, Path], Any] | None = None,
                 progress: Callable[[str], None] | None = None) -> None:
        self.run_dir = run_dir
        self.input_dir = run_dir / "adapter-inputs"
        self.adapter_runner = adapter_runner
        self.progress = progress or (lambda _message: None)
        self.input_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _commit_saved_bytes(path: Path, content: bytes) -> None:
        """Finish a local publication without repeating the browser request."""
        if path.exists():
            if path.read_bytes() != content:
                raise OnlineError(f"已发布检查点与保存的响应不一致: {path}", "resume_mismatch")
            return
        partial = path.with_name(path.name + ".partial")
        if partial.exists():
            if partial.read_bytes() != content:
                raise OnlineError(f"临时检查点不完整或与保存的响应不一致: {partial}", "checkpoint_invalid")
            replace_checkpoint(partial, path)
        else:
            atomic_bytes(path, content)

    def recover_publication(self, site: str, operation: str, payload: dict[str, Any],
                            output_path: Path, key: str, binary: bool = False) -> dict[str, Any] | None:
        """Recover only a complete, input-bound response journal; fail closed otherwise."""
        output_path = output_path.resolve()
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
        journal_path = self.run_dir / "publications" / f"{safe_key}.json"
        receipt_path = self.run_dir / "receipts" / f"{safe_key}.json"
        candidate = journal_path if journal_path.exists() else journal_path.with_name(journal_path.name + ".partial")
        if not candidate.exists():
            fragments = [output_path.with_name(output_path.name + ".partial"),
                         receipt_path.with_name(receipt_path.name + ".partial")]
            if output_path.exists() or receipt_path.exists() or any(path.exists() for path in fragments):
                raise OnlineError("业务响应缺少完整发布记录；已保留文件，请人工核对，不能重复采集", "checkpoint_invalid")
            return None
        journal = read_json(candidate)
        try:
            receipt = journal["receipt"]
            content = base64.b64decode(journal["content_base64"], validate=True)
            input_path = Path(receipt["inputPath"])
            valid = (journal.get("version") == 1
                     and receipt["site"] == site and receipt["operation"] == operation
                     and Path(receipt["outputPath"]).resolve() == output_path
                     and Path(receipt["receiptPath"]).resolve() == receipt_path.resolve()
                     and receipt["sha256"] == hashlib.sha256(content).hexdigest()
                     and receipt["payloadSha256"] == request_sha256(payload)
                     and input_path.is_file()
                     and receipt["inputSha256"] == file_sha256(input_path)
                     and request_sha256(read_json(input_path)) == request_sha256(payload))
        except (KeyError, TypeError, ValueError) as exc:
            raise OnlineError(f"业务响应发布记录不完整: {candidate}", "checkpoint_invalid") from exc
        if not valid:
            raise OnlineError(f"业务响应发布记录与本次输入不一致: {candidate}", "resume_mismatch")
        if not binary:
            try:
                value = json.loads(content.decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise OnlineError("保存的业务响应不是完整 JSON", "checkpoint_invalid") from exc
            validate_raw_payload(site, operation, value, payload)
        if candidate != journal_path:
            replace_checkpoint(candidate, journal_path)
        self._commit_saved_bytes(output_path, content)
        receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._commit_saved_bytes(receipt_path, receipt_bytes)
        return receipt

    def invoke(self, site: str, operation: str, payload: dict[str, Any], output_path: Path,
               key: str | None = None, binary: bool = False) -> dict[str, Any]:
        output_path = output_path.resolve()
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key or operation)
        recovered = self.recover_publication(site, operation, payload, output_path, safe_key, binary)
        if recovered is not None:
            return recovered
        input_path = self.input_dir / f"{safe_key}.json"
        if input_path.exists():
            previous_input = read_json(input_path)
            if request_sha256(previous_input) != request_sha256(payload):
                # A failed adapter invocation can leave only its immutable
                # input file behind. It is safe to retry with a new request
                # identity when no output or receipt was published; keep the
                # old input for diagnosis instead of blocking recovery after
                # a compatible adapter/schema upgrade.
                receipt_paths = (
                    self.run_dir / "receipts" / f"{safe_key}.json",
                    self.run_dir / f"{safe_key}.receipt.json",
                )
                if output_path.exists() or any(path.exists() for path in receipt_paths):
                    raise OnlineError(f"适配器重试输入变化: {input_path}", "checkpoint_exists")
                input_path = self.input_dir / f"{safe_key}-retry-{uuid.uuid4().hex[:8]}.json"
                atomic_json(input_path, payload)
            elif previous_input != payload:
                input_path = self.input_dir / f"{safe_key}-{uuid.uuid4().hex[:8]}.json"
                atomic_json(input_path, payload)
        else:
            atomic_json(input_path, payload)
        input_hash = file_sha256(input_path)
        self.progress(f"adapter {site}/{operation}: {key or operation}")
        returned: Any = None
        if self.adapter_runner is None:
            raise OnlineError("浏览器采集器尚未连接", "configuration")
        if getattr(self.adapter_runner, "accepts_binary", False):
            returned = self.adapter_runner(site, operation, input_path, output_path, binary)
        else:
            returned = self.adapter_runner(site, operation, input_path, output_path)
        # An injected adapter may return the payload instead of writing the raw
        # checkpoint.  This keeps the contract easy to fake without weakening
        # the production requirement that checkpoints are files.
        content = output_path.read_bytes() if output_path.is_file() else None
        if content is None and returned is not None:
            if isinstance(returned, (bytes, bytearray)):
                content = bytes(returned)
            elif isinstance(returned, dict) and "payload" in returned:
                value = returned["payload"]
                if binary and isinstance(value, str):
                    content = base64.b64decode(value, validate=True)
                else:
                    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if content is None:
            raise OnlineError(f"适配器未写入检查点: {output_path}", "adapter_protocol")
        if not binary:
            validate_raw_payload(site, operation, json.loads(content.decode("utf-8-sig")), payload)
        digest = hashlib.sha256(content).hexdigest()
        receipt = dict(returned) if isinstance(returned, dict) else {}
        # The raw file already stores the response. Keep sidecars/state small
        # so every progress update does not rewrite customer rows or XLSX bytes.
        receipt.pop("payload", None)
        if not receipt or "operation" not in receipt:
            receipt = {"operation": operation, "rowCount": None, "outputPath": str(output_path),
                       "sha256": digest, "observedAt": utc_now()}
        if receipt.get("sha256") and str(receipt.get("sha256")) != digest:
            raise OnlineError(f"适配器回执哈希与文件不一致: {output_path}", "adapter_protocol")
        if receipt.get("operation") and receipt.get("operation") != operation:
            raise OnlineError("适配器回执操作不一致", "adapter_protocol")
        if receipt.get("outputPath") and Path(str(receipt["outputPath"])).resolve() != output_path:
            raise OnlineError("适配器回执输出路径不一致", "adapter_protocol")
        receipt.update({"outputPath": str(output_path), "sha256": digest,
                        "inputSha256": input_hash, "payloadSha256": request_sha256(payload),
                        "inputPath": str(input_path), "site": site,
                        "observedAt": receipt.get("observedAt") or utc_now()})
        receipt_path = self.run_dir / "receipts" / f"{safe_key}.json"
        if receipt_path.exists():
            raise OnlineError(f"拒绝覆盖已有检查点回执: {receipt_path}", "checkpoint_exists")
        receipt["receiptPath"] = str(receipt_path.resolve())
        # The durable response journal includes the bytes needed to finish a
        # failed raw/receipt rename. A partial journal is accepted only after
        # full JSON, input identity and content hash validation on resume.
        journal_path = self.run_dir / "publications" / f"{safe_key}.json"
        atomic_json(journal_path, {"version": 1, "receipt": receipt,
                                  "content_base64": base64.b64encode(content).decode("ascii")})
        return self.recover_publication(site, operation, payload, output_path, safe_key, binary)


class OnlineRunner:
    """Stateful coordinator.  All mutations are checkpointed after each stage."""

    def __init__(self, *, date: str, store: str, issuer: str, output_root: Path,
                 agent_id: str | None = None,
                 run_dir: Path | None = None, resume: Path | None = None,
                 replay_input: Path | None = None,
                 browser_config: Path | None = None,
                 browser_backend: str | None = None,
                 node: str | None = None, node_modules: str | None = None,
                 plan_only: bool = False,
                 adapter_runner: Callable[[str, str, Path, Path], Any] | None = None) -> None:
        if resume and replay_input:
            raise OnlineError("--resume 与 --replay-input 不能同时使用", "configuration")
        self.date = date
        self.store = store
        self.issuer = issuer
        self.agent_id = str(agent_id) if agent_id not in (None, "") else None
        self.requested_issuer = issuer
        self._stage_started: dict[str, float] = {}
        self.output_root = Path(output_root).resolve()
        self.replay = replay_input is not None
        self.plan_only = plan_only
        self.node = node
        self.node_modules = node_modules
        self.browser_backend = str(browser_backend or "playwright").strip().lower()
        if self.browser_backend != "playwright":
            raise OnlineError("--browser-backend 只支持 playwright", "configuration")
        self._browser_runner: Any = None
        self._resume = resume.resolve() if resume else None
        self.browser_config: dict[str, Any] = {}
        self.browser_config_path: Path | None = None
        if self._resume:
            self.run_dir = self._resume
            # Resume must reacquire the lock next to the original run, even
            # when the caller omitted the original --output-root.
            self.output_root = self.run_dir.parent
            state_path = self.run_dir / "run-state.json"
            if not state_path.is_file():
                raise OnlineError(f"恢复目录缺少 run-state.json: {self.run_dir}", "checkpoint_invalid")
            old = read_json(state_path)
            if old.get("mode") != "replay":
                if str(old.get("browser_backend") or "").strip().lower() != "playwright":
                    raise OnlineError(
                        "旧浏览器后端作业不支持在线恢复；请新建 Playwright 作业，"
                        "或使用 --replay-input 离线重放已采集数据",
                        "resume_backend_unsupported",
                    )
                saved_config_path = old.get("browser_config_path")
                config, config_path = load_browser_config(
                    browser_config or (Path(saved_config_path) if saved_config_path else None))
                saved_config = old.get("browser_config") or {}
                if config and saved_config:
                    for key, default in (("user_data_dir", ""), ("profile_directory", "Default")):
                        previous = str(saved_config.get(key) or default)
                        current = str(config.get(key) or default)
                        if key == "user_data_dir":
                            previous = os.path.normcase(str(Path(previous).resolve()))
                            current = os.path.normcase(str(Path(current).resolve()))
                        if previous != current:
                            raise OnlineError(f"恢复时浏览器环境已变化: {key}", "resume_mismatch")
                self.browser_config = config or (old.get("browser_config")
                                                  if isinstance(old.get("browser_config"), dict)
                                                  else {})
                self.browser_config_path = config_path
            for key, value in (("date", date), ("store", store)):
                if old.get(key) != value:
                    raise OnlineError(f"恢复参数与原运行不一致: {key}", "resume_mismatch")
            if issuer not in {old.get("issuer"), old.get("requested_issuer", old.get("issuer"))}:
                raise OnlineError("恢复参数与原运行不一致: issuer", "resume_mismatch")
            self.issuer = old["issuer"]
            self.requested_issuer = old.get("requested_issuer", self.issuer)
            self.input_dir = Path(old["input_dir"]).resolve()
            self.generated_dir = Path(old["generated_dir"]).resolve()
            self.state = old
            self.replay = old.get("mode") == "replay"
            self.plan_only = bool(old.get("plan_only", False))
            self.node = self.node or old.get("node")
            self.node_modules = self.node_modules or old.get("node_modules")
            if self.agent_id is not None and self.agent_id != old.get("agent_id"):
                raise OnlineError("恢复参数与原运行不一致: agent_id", "resume_mismatch")
            self.agent_id = self.agent_id or old.get("agent_id")
        elif replay_input:
            source = Path(replay_input).resolve()
            if not source.is_dir():
                raise OnlineError(f"重放输入目录不存在: {source}", "input_invalid")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.run_dir = (Path(run_dir).resolve() if run_dir else self.output_root / f"replay-{date}-{stamp}")
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.input_dir = source
            self.generated_dir = self.run_dir / "generated"
            self.state = self._new_state("replay")
        else:
            self.browser_config, self.browser_config_path = load_browser_config(browser_config)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            store_key = hashlib.sha256(store.encode("utf-8")).hexdigest()[:10]
            self.run_dir = (Path(run_dir).resolve() if run_dir else self.output_root / f"online-{store_key}-{date}-{stamp}")
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.input_dir = self.run_dir
            self.generated_dir = self.run_dir / "generated"
            self.state = self._new_state("online")
        self.generated_dir.parent.mkdir(parents=True, exist_ok=True)
        if self.browser_config:
            self.state["browser_config"] = self.browser_config
        if self.browser_config_path:
            self.state["browser_config_path"] = str(self.browser_config_path)
        self.state["browser_backend"] = self.browser_backend
        self.state.update({"node": self.node, "node_modules": self.node_modules})
        if not self._resume:
            self._write_state()
        self._needs_browser = adapter_runner is None and not self.replay
        if self._needs_browser and not self.browser_config_path:
            raise OnlineError("Playwright 后端需要 --browser-config", "configuration")
        self.invoker = AdapterInvoker(self.run_dir, adapter_runner=adapter_runner, progress=self.progress)
        if self._resume:
            self._validate_input_hashes()

    def _connect_browser(self) -> None:
        """Called only with the job lock held, before any live page work."""
        if self._needs_browser and self._browser_runner is None:
            try:
                from playwright_adapter import PlaywrightAdapterRunner
                self._browser_runner = PlaywrightAdapterRunner(
                    self.browser_config_path, progress=self.progress
                )
            except Exception as exc:
                code = getattr(exc, "code", "dependency_missing")
                raise OnlineError(str(exc), code) from exc
            self.invoker.adapter_runner = self._browser_runner

    def _new_state(self, mode: str) -> dict[str, Any]:
        return {"version": 1, "mode": mode, "date": self.date, "store": self.store,
                "agent_id": self.agent_id,
                "plan_only": self.plan_only,
                "issuer": self.issuer, "requested_issuer": self.requested_issuer,
                "run_dir": str(self.run_dir),
                "input_dir": str(self.input_dir), "generated_dir": str(self.generated_dir),
                "status": "created", "started_at": utc_now(), "stages": {},
                "checkpoints": {}, "progress": []}

    def _write_state(self) -> None:
        atomic_json(self.run_dir / "run-state.json", self.state)

    def progress(self, message: str) -> None:
        line = f"[{utc_now()}] {message}"
        print(line, file=sys.stderr, flush=True)
        self.state.setdefault("progress", []).append(line)
        # Progress is observational. Only stage/checkpoint commits rewrite the
        # recovery state; a transient log failure cannot invalidate collection.
        try:
            with (self.run_dir / "progress.log").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    def _stage_attempts(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        if "attempts" not in record:
            # Preserve the original attempt when resuming a version-1 run.
            previous = {key: value for key, value in record.items()
                        if key != "started_monotonic"}
            record["attempts"] = [previous] if previous else []
        return record["attempts"]

    def stage_start(self, name: str, *, kind: str | None = None) -> None:
        record = self.state.setdefault("stages", {}).setdefault(name, {})
        attempts = self._stage_attempts(record)
        if attempts and attempts[-1].get("status") == "running":
            self._finish_attempt(name, "interrupted", error_code="interrupted")
        now = utc_now()
        attempt = {"number": len(attempts) + 1, "kind": kind or ("initial" if not attempts else "retry"),
                   "status": "running", "started_at": now,
                   "checkpoints_collected": 0, "checkpoints_reused": 0}
        attempts.append(attempt)
        for key in ("finished_at", "started_monotonic", "error", "error_code"):
            record.pop(key, None)
        record.update({"status": "running", "started_at": now})
        self._stage_started[name] = time.monotonic()
        self.state["status"] = "running"
        for key in ("finished_at", "error", "error_code"):
            self.state.pop(key, None)
        self._write_state()

    def _finish_attempt(self, name: str, status: str, **extra: Any) -> None:
        record = self.state["stages"][name]
        attempts = self._stage_attempts(record)
        attempt = attempts[-1]
        finished = utc_now()
        started = self._stage_started.pop(name, None)
        if started is not None:
            duration = max(0, time.monotonic() - started)
        else:
            # Monotonic clocks cannot be compared across process restarts.
            try:
                duration = max(0, (datetime.fromisoformat(finished) -
                                   datetime.fromisoformat(attempt["started_at"])).total_seconds())
            except (KeyError, ValueError):
                duration = 0
            attempt["duration_basis"] = "wall_clock_recovery"
        attempt.update({"status": status, "finished_at": finished,
                        "duration_seconds": round(duration, 3), **extra})
        record.update({"status": status, "finished_at": finished,
                       "last_duration_seconds": attempt["duration_seconds"],
                       "duration_seconds": round(sum(float(item.get("duration_seconds", 0))
                                                     for item in attempts), 3), **extra})
        record.pop("started_monotonic", None)

    def stage_done(self, name: str, outputs: list[Path] | None = None, **extra: Any) -> None:
        record = self.state.setdefault("stages", {}).setdefault(name, {})
        if record.get("status") != "running":
            self.stage_start(name, kind="local_checkpoint")
        if outputs:
            extra["outputs"] = [{"path": str(path.resolve()), "sha256": file_sha256(path)} for path in outputs]
        self._finish_attempt(name, "complete", **extra)
        self._validate_input_hashes(check=False)
        self._write_state()

    def stage_is_done(self, name: str) -> bool:
        record = self.state.get("stages", {}).get(name, {})
        if record.get("status") != "complete":
            return False
        for item in record.get("outputs", []):
            path = Path(item["path"])
            if not path.is_file() or file_sha256(path) != item.get("sha256"):
                raise OnlineError(f"检查点哈希变化，不能恢复: {path}", "resume_mismatch")
        return True

    def run_script(self, args: list[str], label: str) -> None:
        self.progress(f"local {label}")
        try:
            result = subprocess.run([sys.executable, *args], cwd=str(self.run_dir), shell=False,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", check=False, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise OnlineError(f"本地阶段超时（300 秒）: {label}", "timeout") from exc
        if result.stdout.strip():
            self.progress(result.stdout.strip().splitlines()[-1])
        if result.returncode:
            message = (result.stderr or result.stdout or f"{label} failed").strip()
            raise OnlineError(message, "local_stage_failed")

    def collect(self, site: str, operation: str, payload: dict[str, Any], path: Path,
                key: str, binary: bool = False) -> Path:
        """Reuse only a hash- and input-bound raw checkpoint."""
        path = path.resolve()
        if ".receipt." in path.name or "receipts" in path.relative_to(self.run_dir).parts:
            raise OnlineError("回执路径不能用作原始数据检查点", "checkpoint_invalid")
        pages = self.state.get("browser_pages", {}).get(site)
        # Keep this request shape stable for existing Playwright checkpoint
        # hashes. Page registration and missing-page recovery are owned by
        # the browser controller, not this saved transport hint.
        request = {"date": self.date, "expected_store": self.store,
                   "expected_issuer": self.issuer,
                   "rebind_if_missing": True, **payload}
        if pages:
            request["browser_pages"] = pages
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
        receipt_path = self.run_dir / "receipts" / f"{safe_key}.json"
        if not receipt_path.exists():
            legacy_receipt = path.with_name(path.name + ".receipt.json")
            if legacy_receipt.exists():
                receipt_path = legacy_receipt
        reused = path.exists() and receipt_path.is_file()
        if not reused:
            recovered = self.invoker.recover_publication(site, operation, request, path, safe_key, binary)
            if recovered is not None:
                reused = True
        if receipt_path.exists() and not path.exists():
            raise OnlineError(f"回执存在但原始数据缺失: {path}", "resume_mismatch")
        if path.exists():
            if not binary:
                validate_raw_payload(site, operation, read_json(path), request)
            if not receipt_path.is_file():
                raise OnlineError("业务响应缺少完整回执，不能重复采集", "checkpoint_invalid")
            else:
                receipt = read_json(receipt_path)
                if (not isinstance(receipt, dict)
                        or not receipt.get("outputPath")
                        or Path(receipt["outputPath"]).resolve() != path
                        or receipt.get("sha256") != file_sha256(path)
                        or receipt.get("payloadSha256") != request_sha256(request)
                        or receipt.get("site") != site or receipt.get("operation") != operation
                        or not Path(receipt.get("inputPath", "")).is_file()
                        or receipt.get("inputSha256") != file_sha256(Path(receipt["inputPath"]))):
                    raise OnlineError(f"检查点哈希或查询身份变化: {path}", "resume_mismatch")
        if not path.exists():
            receipt = self.invoker.invoke(site, operation, request, path, key, binary)
        if not binary:
            response = read_json(path)
            page_targets = response.get("browser_pages") if isinstance(response, dict) else None
            if isinstance(page_targets, dict):
                self.state.setdefault("browser_pages", {}).setdefault(site, {}).update(page_targets)
        self.state.setdefault("checkpoints", {})[key] = receipt
        for record in self.state.get("stages", {}).values():
            if record.get("status") == "running" and record.get("attempts"):
                attempt = record["attempts"][-1]
                field = "checkpoints_reused" if reused else "checkpoints_collected"
                attempt[field] = attempt.get(field, 0) + 1
        self._write_state()
        return path

    @staticmethod
    def _verified_identity(context: dict[str, Any], site: str, expected: str) -> str:
        label = "千牛" if site == "qianniu" else "票聚"
        if context.get("isLogin") is False:
            raise OnlineError(f"{label}登录态失效，请人工介入", "auth_required")
        if context.get("isLogin") is not True:
            raise OnlineError(f"{label}缺少登录态正向证据", "context_missing")
        fields = ("store", "agentId") if site == "qianniu" else ("issuer", "coid", "uid")
        if any(context.get(field) in (None, "") for field in fields):
            raise OnlineError(f"{label}主体上下文字段缺失", "context_missing")
        observed = str(context[fields[0]])
        # The operator's UI label is accepted only when it was actually
        # observed alongside the canonical company field, never regex-guessed.
        permitted = {observed}
        if site == "jst" and context.get("issuer_label"):
            permitted.add(str(context["issuer_label"]))
        if expected not in permitted:
            raise OnlineError(f"{label}主体不匹配: 预期 {expected}，当前 {observed}", "context_mismatch")
        return observed

    def _refresh_context(self, *, allow_partial: bool = False) -> None:
        """A resume rechecks login/identity without changing source provenance."""
        self.stage_start("context_refresh")
        capture_path = self.input_dir / "capture_context.json"
        if capture_path.is_file():
            saved = read_json(capture_path)
        elif allow_partial:
            # Initial context can fail after just one site's checkpoint was
            # committed. Its identity is still binding, but never substitutes
            # for a fresh check of both currently logged-in sites on resume.
            saved = {"store": self.store, "issuer": self.issuer}
            for site, filename, fields, expected in (
                ("qianniu", "context_qianniu.json", ("store", "agentId"), self.store),
                ("jst", "context_piaoju.json", ("issuer", "issuer_label", "coid", "uid"), self.issuer),
            ):
                path = self.input_dir / filename
                if path.is_file():
                    previous = read_json(path)
                    self._verified_identity(previous, site, expected)
                    saved.update({field: previous[field] for field in fields if field in previous})
        else:
            raise OnlineError("恢复上下文缺少原主体证据", "checkpoint_invalid")
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        outputs = []
        for site, operation, names in (("qianniu", "context", ("store", "agentId")),
                                       ("jst", "context", ("issuer", "coid", "uid"))):
            path = self.run_dir / f"resume-context-{site}-{suffix}.json"
            request = {"issuer": self.issuer, "expected_issuer": self.issuer} if site == "jst" else {
                "prepare_orders": True,
            }
            if site == "qianniu" and saved.get("agentId"):
                request["agent_id"] = saved["agentId"]
            self.collect(site, operation, request, path, f"resume-{site}-{suffix}")
            outputs.append(path)
            current = read_json(path)
            try:
                self._verified_identity(current, site, saved.get("store") if site == "qianniu" else
                                        saved.get("issuer_company", saved.get("issuer")))
            except OnlineError as exc:
                if exc.code == "context_mismatch":
                    raise OnlineError(str(exc), "context_changed") from exc
                raise
            for name in names:
                if name not in saved:
                    continue
                if name == "issuer":
                    observed = current.get("issuer")
                    expected = saved.get("issuer_company", saved.get("issuer"))
                    if expected == current.get("issuer_label"):
                        expected = current.get("issuer")  # old display-label snapshot
                else:
                    observed = current.get(name)
                    expected = saved.get(name)
                if str(observed) != str(expected):
                    raise OnlineError(f"恢复时页面主体变化: {name}", "context_changed")
            if current.get("browser_pages"):
                self.state.setdefault("browser_pages", {})[site] = current["browser_pages"]
        self.stage_done("context_refresh", outputs)

    def _context(self) -> None:
        if self.stage_is_done("context"):
            if self._resume:
                self._refresh_context()
            return
        if self._resume:
            self._refresh_context(allow_partial=True)
        self.stage_start("context")
        qpath = self.input_dir / "context_qianniu.json"
        ppath = self.input_dir / "context_piaoju.json"
        qrequest = {"expected_issuer": self.requested_issuer, "prepare_orders": True}
        if self.agent_id is not None:
            qrequest["agent_id"] = self.agent_id
        self.collect("qianniu", "context", qrequest, qpath, "context-qianniu")
        qctx = read_json(qpath)
        self._verified_identity(qctx, "qianniu", self.store)
        self.collect("jst", "context", {"issuer": self.requested_issuer, "expected_issuer": self.requested_issuer}, ppath, "context-piaoju")
        pctx = read_json(ppath)
        self.issuer = self._verified_identity(pctx, "jst", self.requested_issuer)
        self.state["issuer"] = self.issuer
        for site, value in (("qianniu", qctx), ("jst", pctx)):
            if value.get("browser_pages"):
                self.state.setdefault("browser_pages", {})[site] = value["browser_pages"]
        verified_at = qctx.get("checked_at") or pctx.get("checked_at") or utc_now()
        context = {"date": self.date, "store": self.store, "issuer": self.issuer,
                   "issuer_company": self.issuer, "issuer_label": pctx.get("issuer_label"),
                   "browser_backend": self.browser_backend,
                   "profile_id": self.browser_config.get("profile_id", self.store)
                   if isinstance(self.browser_config, dict) else self.store,
                   "user_data_dir": self.browser_config.get("user_data_dir")
                   if isinstance(self.browser_config, dict) else None,
                   "profile_directory": self.browser_config.get("profile_directory", "Default")
                   if isinstance(self.browser_config, dict) else "Default",
                   "context_reused": False,
                   "verified_at": verified_at, "invoice_url": qctx.get("invoice_url") or
                   "https://myseller.taobao.com/home.htm/merchant-invoice/",
                   "orders_url": qctx.get("orders_url") or
                   "https://myseller.taobao.com/home.htm/trade-platform/tp/sold",
                   "jst_url": pctx.get("jst_url") or
                   "https://fp.erp321.com/setting/goodsManage", "agentId": str(qctx["agentId"]),
                   "coid": str(pctx["coid"]), "uid": str(pctx["uid"])}
        context["context_sha256"] = stable_sha256(context)
        atomic_json(self.input_dir / "capture_context.json", context)
        self.stage_done("context", [qpath, ppath, self.input_dir / "capture_context.json"],
                        input_sha256=stable_sha256({"store": self.store, "issuer": self.issuer}))

    def _applications_export(self) -> None:
        context = read_json(self.input_dir / "capture_context.json")
        if not self.stage_is_done("applications"):
            self.stage_start("applications")
            path = self.input_dir / "applications.json"
            self.collect("qianniu", "applications", {
                    "date": self.date, "agentId": context["agentId"],
                    "expected_store": self.store, "expected_issuer": self.issuer,
                }, path, "applications")
            data = read_json(path)
            if data.get("date") != self.date or not isinstance(data.get("rows"), list):
                raise OnlineError("申请列表日期或结构无效", "checkpoint_invalid")
            self.stage_done("applications", [path])
        if not self.stage_is_done("export"):
            self.stage_start("export")
            path = self.input_dir / "qianniu_common.xlsx"
            self.collect("qianniu", "export", {
                    "date": self.date, "agentId": context["agentId"],
                    "expected_store": self.store, "expected_issuer": self.issuer,
                }, path, "export", binary=True)
            try:
                with ZipFile(path) as archive:
                    if archive.testzip() is not None:
                        raise OnlineError("通用模板 ZIP 校验失败", "checkpoint_invalid")
            except OSError as exc:
                raise OnlineError(f"通用模板不是有效 XLSX: {exc}", "checkpoint_invalid") from exc
            self.stage_done("export", [path])

    def _orders(self, force: bool = False) -> None:
        if self.stage_is_done("orders") and not force:
            return
        self.stage_start("orders", kind="remerge" if force else None)
        order_ids_path = self.input_dir / "order_ids.json"
        if not self.stage_is_done("orders_prepare"):
            self.stage_start("orders_prepare")
            self.run_script([str(COLLECTOR), "orders", "--run-dir", str(self.input_dir)], "selection")
            self.stage_done("orders_prepare", [order_ids_path, self.input_dir / "selection.json"])
        order_ids = read_json(order_ids_path)
        orders = list(order_ids.get("orders", []))
        if not orders:
            self.stage_done("orders", [order_ids_path])
            return
        context = read_json(self.input_dir / "capture_context.json")
        parts = []
        for index, values in enumerate(chunked([str(v) for v in orders], 50), 1):
            path = self.input_dir / f"orders_part_{index:03d}.json"
            parts.append(path)
            self.collect("qianniu", "orders", {
                "orders": values, "agentId": context["agentId"],
                "expected_store": self.store, "expected_issuer": self.issuer,
            }, path, f"orders-{index:03d}")
        # This first merge reveals any historical orders requiring detail.
        self.run_script([str(COLLECTOR), "merge-orders", "--run-dir", str(self.input_dir),
                         *sum((["--part", str(path)] for path in parts), [])], "orders merge")
        batches = read_json(self.input_dir / "order_batches.json")
        missing = [str(value) for value in batches.get("missing", [])]
        old_path = self.input_dir / "old_details.json"
        old = read_json(old_path) if old_path.exists() else []
        old_by_order = {str(item.get("order_no")): item for item in old}
        unresolved = [order_no for order_no in missing if order_no not in old_by_order]
        if unresolved:
            self.stage_start("details")
            detail_paths = []
            for order_no in unresolved:
                path = self.input_dir / f"detail_{order_no}.json"
                detail_paths.append(path)
                self.collect("qianniu", "detail", {
                        "order_no": order_no, "agentId": context["agentId"],
                        "expected_store": self.store, "expected_issuer": self.issuer,
                        "reuse_orders_page": True,
                    }, path, f"detail-{order_no}")
                detail = read_json(path)
                if str(detail.get("order_no")) != order_no or detail.get("verified_order") is not True or not isinstance(detail.get("items"), list):
                    raise OnlineError(f"订单详情未能核对订单号: {order_no}", "checkpoint_invalid")
                old_by_order[order_no] = detail
            atomic_json(old_path, [old_by_order[key] for key in sorted(old_by_order)])
            self.stage_done("details", [old_path, *detail_paths])
            self.run_script([str(COLLECTOR), "merge-orders", "--run-dir", str(self.input_dir),
                             *sum((["--part", str(path)] for path in parts), [])], "orders merge with details")
        elif old_path.exists() and not self.stage_is_done("details"):
            self.stage_done("details", [old_path])
        self.stage_done("orders", [*parts, self.input_dir / "order_batches.json",
                                    self.input_dir / "goods_codes.json"] + ([old_path] if old_path.exists() else []))

    def _jst(self, force: bool = False) -> None:
        codes_path = self.input_dir / "goods_codes.json"
        if not codes_path.exists():
            return
        if self.stage_is_done("jst") and not force:
            saved_codes = self.state["stages"]["jst"].get("codes_sha256")
            if saved_codes == file_sha256(codes_path):
                return
        codes_data = read_json(codes_path)
        codes = [str(code) for code in codes_data.get("codes", [])]
        if not codes:
            return
        self.stage_start("jst", kind="remerge" if force else None)
        context = read_json(self.input_dir / "capture_context.json")
        # Keep previously successful parts in the merge input.  A resumed run
        # may only query newly discovered codes after detail enrichment, while
        # collection_files.merge-jst intentionally requires the complete
        # requested set in one immutable union.
        parts: list[Path] = sorted((path for path in self.input_dir.iterdir()
                                  if path.is_file() and re.fullmatch(r"jst_part_\d+\.json", path.name)),
                                 key=lambda path: int(path.stem.rsplit("_", 1)[1]))
        missing_codes = set(codes)
        completed_codes: set[str] = set()
        for part in parts:
            index = int(part.stem.rsplit("_", 1)[1])
            key = f"jst-{index:03d}"
            saved_input = read_json(self.run_dir / "adapter-inputs" / f"{key}.json")
            if not set(saved_input.get("codes", [])) <= set(codes):
                raise OnlineError(f"票聚批次含当前范围外编码: {part}", "resume_mismatch")
            self.collect("jst", "query", saved_input, part, key)
            # Read raw successes even if a previous process died before merge.
            # Failed rows remain available to the union, with newer retry
            # parts superseding only those failures in collection_files.py.
            for row in read_json(part)["data"]:
                code = str(row["input_goods_code"])
                if code in completed_codes:
                    raise OnlineError(f"已完成商品被重复批次覆盖: {code}", "checkpoint_invalid")
                # A successful query with no match is a business outcome, not
                # a transport failure. Only unfinished requests are retried.
                if row.get("reason") != "request_failed":
                    completed_codes.add(code)
                    missing_codes.discard(code)
        # Each bounded page-evaluate batch is an immutable recovery unit.
        # Eight concurrent fetches keep the usual case fast; request retries
        # remain within the adapter deadline without redoing completed codes.
        for values in chunked(sorted(missing_codes), JST_BATCH_SIZE):
            next_index = max([int(path.stem.rsplit("_", 1)[1]) for path in parts] + [0]) + 1
            path = self.input_dir / f"jst_part_{next_index:03d}.json"
            parts.append(path)
            if path.exists():
                continue
            self.collect("jst", "query", {
                "codes": values, "context": {"coid": context["coid"], "uid": context["uid"]},
                "expected_store": self.store, "expected_issuer": self.issuer,
                "reuse_context": True,
            }, path, f"jst-{next_index:03d}")
        if parts:
            self.run_script([str(COLLECTOR), "merge-jst", "--run-dir", str(self.input_dir),
                             *sum((["--part", str(path)] for path in parts), [])], "票聚商品合并")
            merged = read_json(self.input_dir / "jst_query.json")
            failed = [row.get("input_goods_code") for row in merged.get("data", [])
                      if row.get("reason") == "request_failed"]
            if failed:
                raise OnlineError("票聚请求未完成，可恢复后只补失败编码: " + ",".join(failed), "request_failed")
            self.stage_done("jst", [*parts, self.input_dir / "jst_query.json"],
                            codes_sha256=file_sha256(codes_path))

    @staticmethod
    def _detail_orders_from_plan(plan_path: Path, template_path: Path) -> set[str]:
        if not plan_path.exists():
            return set()
        plan = read_json(plan_path)
        terms = ("唯一", "歧义", "匹配", "订单明细", "候选", "商品关联")
        result: set[str] = set()
        rows_by_serial: dict[str, set[str]] = {}
        try:
            from run_invoice import read_source
            for row in read_source(template_path):
                rows_by_serial.setdefault(str(row.get("申请流水号")), set()).add(str(row.get("订单编号")))
        except Exception:
            return result
        for invoice in plan.get("invoices", []):
            errors = "；".join(str(error) for error in invoice.get("errors", []))
            if errors and any(term in errors for term in terms):
                result.update(rows_by_serial.get(str(invoice.get("invoice_serial_no")), set()))
        return {order for order in result if order and order != "None"}

    def _probe_and_details(self) -> None:
        # A plan-only pass is intentionally cheap and produces the evidence
        # needed to decide whether a historical detail read is worthwhile.
        if not (self.input_dir / "goods_codes.json").exists():
            return
        if self.stage_is_done("detail_enrichment"):
            saved = self.state["stages"]["detail_enrichment"]
            if saved.get("attempted_orders") and not saved.get("dependencies_committed"):
                # Older runs marked detail collection complete before the
                # derived code merge and JST supplement were committed.
                outputs = [Path(item["path"]) for item in saved.get("outputs", [])]
                self.stage_start("detail_enrichment", kind="recovery_merge")
                self._orders(force=True)
                self._jst(force=True)
                self.stage_done("detail_enrichment", outputs, dependencies_committed=True)
            return
        def complete_probe(path: Path) -> bool:
            if not path.is_dir():
                return False
            plan_path, manifest_path = path / "invoice_plan.json", path / "run.json"
            if not plan_path.is_file() or not manifest_path.is_file():
                return False
            try:
                plan = read_json(plan_path)
                manifest = read_json(manifest_path)
            except OnlineError:
                return False
            return (isinstance(plan, dict) and isinstance(plan.get("invoices"), list)
                    and manifest.get("status") == "plan_only")

        def next_probe_retry(path: Path) -> Path:
            match = re.match(r"^(.*)-retry(\d+)$", path.name)
            base_name = match.group(1) if match else path.name
            start = int(match.group(2)) + 1 if match else 2
            candidate = path.with_name(f"{base_name}-retry{start}")
            while candidate.exists():
                start += 1
                candidate = path.with_name(f"{base_name}-retry{start}")
            return candidate

        saved_probe = self.state.get("probe", {})
        saved_path = Path(saved_probe.get("path")) if saved_probe.get("path") else None
        if saved_path and complete_probe(saved_path):
            probe = saved_path
        elif saved_path and saved_path.exists():
            # A previously started probe is immutable. Do not turn a partial
            # directory into a successful checkpoint on resume.
            probe = next_probe_retry(saved_path)
        else:
            base = self.run_dir / "plan-probe"
            if complete_probe(base):
                probe = base
            elif base.exists():
                probe = next_probe_retry(base)
            else:
                probe = base
        if not complete_probe(probe):
            self.state["probe"] = {"path": str(probe.resolve()), "status": "running"}
            self._write_state()
            self._run_invoice(probe, plan_only=True)
            if not complete_probe(probe):
                raise OnlineError(f"计划探测未生成完整检查点: {probe}", "checkpoint_invalid")
            plan_digest = file_sha256(probe / "invoice_plan.json")
            self.state["probe"] = {"path": str(probe.resolve()), "status": "complete",
                                     "plan_sha256": plan_digest,
                                     "manifest_sha256": file_sha256(probe / "run.json")}
            self._write_state()
        else:
            if (saved_probe.get("plan_sha256")
                    and file_sha256(probe / "invoice_plan.json") != saved_probe["plan_sha256"]):
                raise OnlineError(f"计划探测检查点哈希变化: {probe}", "resume_mismatch")
            if (saved_probe.get("manifest_sha256")
                    and file_sha256(probe / "run.json") != saved_probe["manifest_sha256"]):
                raise OnlineError(f"计划探测运行回执哈希变化: {probe}", "resume_mismatch")
            if (saved_probe.get("path") != str(probe.resolve())
                    or saved_probe.get("status") != "complete"):
                self.state["probe"] = {"path": str(probe.resolve()), "status": "complete",
                                        "plan_sha256": file_sha256(probe / "invoice_plan.json"),
                                        "manifest_sha256": file_sha256(probe / "run.json")}
                self._write_state()
        needed = self._detail_orders_from_plan(probe / "invoice_plan.json", self.input_dir / "qianniu_common.xlsx")
        old_path = self.input_dir / "old_details.json"
        old = read_json(old_path) if old_path.exists() else []
        known = {str(item.get("order_no")) for item in old}
        needed -= known
        if not needed:
            self.stage_done("detail_enrichment", [probe / "invoice_plan.json"],
                            attempted_orders=[],
                            note="计划未发现需补读的订单明细")
            return
        context = read_json(self.input_dir / "capture_context.json")
        additions = []
        evidence_path = self.input_dir / "match_evidence.json"
        evidence = read_json(evidence_path) if evidence_path.exists() else []
        self.stage_start("detail_enrichment")
        for order_no in sorted(needed):
            path = self.input_dir / f"detail_enrich_{order_no}.json"
            self.collect("qianniu", "detail", {
                "order_no": order_no, "agentId": context["agentId"],
                "expected_store": self.store, "expected_issuer": self.issuer,
                "reuse_orders_page": True,
            }, path, f"detail-enrich-{order_no}")
            detail = read_json(path)
            if str(detail.get("order_no")) != order_no or detail.get("verified_order") is not True:
                raise OnlineError(f"补读订单详情未能核对订单号: {order_no}", "checkpoint_invalid")
            additions.append(detail)
            # The adapter may explicitly expose gross/promotion evidence; an
            # arbitrary displayed realTotal is never promoted to gross here.
            for row in detail.get("match_evidence", []):
                if (str(row.get("order_no")) != order_no or not row.get("source")
                        or row.get("match_amount_source") not in {"order_detail_gross", "promotion_detail_gross"}
                        or not row.get("sub_order_no") or not row.get("goods_code")
                        or row.get("source_amount") is None):
                    raise OnlineError("详情返回了未经核对的金额证据", "checkpoint_invalid")
                if row not in evidence:
                    evidence.append(row)
        supplemental = self.input_dir / "supplemental_details.json"
        atomic_json(supplemental, additions)
        outputs = [supplemental]
        if evidence:
            atomic_json(evidence_path, evidence)
            outputs.append(evidence_path)
        # New detail can reveal a code that was absent from the batch list.
        # Re-merge immutable order parts and query only newly discovered JST
        # codes; successful prior checkpoints remain reusable.
        self._orders(force=True)
        self._jst(force=True)
        self.stage_done("detail_enrichment", outputs,
                        attempted_orders=sorted(needed), dependencies_committed=True,
                        note="仅采纳明确核对口径的金额证据；详情未提供证据时保留原逐票暂缓")

    def _run_invoice(self, output_dir: Path, plan_only: bool = False) -> Path:
        if output_dir.exists():
            # run_invoice deliberately refuses to overwrite an output.  A
            # failed resumable run therefore gets a fresh sibling directory;
            # successful output remains immutable and reviewable.
            if plan_only:
                raise OnlineError(f"本地计划目录已存在: {output_dir}", "checkpoint_exists")
            suffix = 2
            candidate = output_dir.with_name(output_dir.name + f"-retry{suffix}")
            while candidate.exists():
                suffix += 1
                candidate = output_dir.with_name(output_dir.name + f"-retry{suffix}")
            output_dir = candidate
            self.generated_dir = output_dir
        if not plan_only:
            self.generated_dir = output_dir
            self.state["generated_dir"] = str(output_dir.resolve())
            self._write_state()
        args = [str(RUN_INVOICE), "--date", self.date, "--store", self.store,
                "--issuer", self.issuer, "--input-dir", str(self.input_dir),
                "--output-dir", str(output_dir)]
        if plan_only:
            args.append("--plan-only")
        if self.replay:
            args.append("--replay")
        if self.node:
            args.extend(["--node", self.node])
        if self.node_modules:
            args.extend(["--node-modules", self.node_modules])
        self.run_script(args, "run_invoice" + (" plan-only" if plan_only else ""))
        return output_dir

    def _validate_input_hashes(self, check: bool = True) -> None:
        if check:
            for item in self.state.get("input_hashes", []):
                path = Path(item["path"])
                if not path.is_file() or file_sha256(path) != item["sha256"]:
                    raise OnlineError(f"恢复输入文件已变化: {path}", "resume_mismatch")
            for item in self.state.get("checkpoints", {}).values():
                path = Path(item["outputPath"])
                if not path.is_file() or file_sha256(path) != item["sha256"]:
                    raise OnlineError(f"原始检查点已变化: {path}", "resume_mismatch")
                input_path = Path(item.get("inputPath", ""))
                if (not input_path.is_file()
                        or file_sha256(input_path) != item.get("inputSha256")):
                    raise OnlineError(f"适配器输入检查点已变化: {input_path}", "resume_mismatch")
                receipt_path = Path(item.get("receiptPath", str(path) + ".receipt.json"))
                if not receipt_path.is_file() or read_json(receipt_path) != item:
                    raise OnlineError(f"适配器回执已变化: {receipt_path}", "resume_mismatch")
        hashes = []
        for name in ("capture_context.json", "applications.json", "qianniu_common.xlsx",
                     "selection.json", "order_ids.json", "order_batches.json", "old_details.json",
                     "supplemental_details.json", "match_evidence.json", "parts_manifest.json",
                     "goods_codes.json", "jst_query.json"):
            path = self.input_dir / name
            if path.is_file() and not any(item["path"] == str(path.resolve()) for item in hashes):
                hashes.append({"path": str(path.resolve()), "sha256": file_sha256(path)})
        self.state["input_hashes"] = hashes

    def run(self) -> dict[str, Any]:
        with ActiveLock(self.output_root) as lock:
            try:
                if lock.recovered_stale_lock:
                    self.state["recovered_stale_lock"] = {
                        "recovered_at": utc_now(), "reason": lock.stale_lock_reason,
                    }
                self._write_state()
                self._connect_browser()
                if self.replay:
                    self.progress("replay-input: offline only")
                else:
                    self._context()
                    self._applications_export()
                    self._orders()
                    self._jst()
                    self._probe_and_details()
                if not self.stage_is_done("generate"):
                    self.stage_start("generate")
                    self._run_invoice(self.generated_dir, plan_only=self.plan_only)
                    outputs = [self.generated_dir / "run.json", self.generated_dir / f"qianniu_common_{self.date}.xlsx"]
                    final = self.generated_dir / f"qianniu_invoice_tax_template_{self.date}.xlsx"
                    if final.exists():
                        outputs.append(final)
                    self.stage_done("generate", outputs)
                self._validate_input_hashes(check=False)
                generated_manifest = read_json(self.generated_dir / "run.json")
                self.state.update({"status": "complete", "finished_at": utc_now(),
                                   "result_status": generated_manifest.get("status")})
                self._write_state()
                result = {"status": generated_manifest.get("status", "complete"),
                          "run_dir": str(self.run_dir), "generated_dir": str(self.generated_dir),
                          "run_manifest": str(self.generated_dir / "run.json"),
                          **{key: generated_manifest.get(key) for key in
                             ("selected_count", "ready_count", "blocked_count", "excluded_count",
                              "ready_amount", "blocked_amount", "excluded_amount")}}
                self._publish_run_report({**self.state, **result})
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return result
            except Exception as exc:
                for name, record in self.state.get("stages", {}).items():
                    if record.get("status") == "running":
                        self._finish_attempt(name, "failed", error=str(exc),
                                             error_code=getattr(exc, "code", "failed"))
                self.state.update({"status": "failed", "finished_at": utc_now(),
                                   "error": str(exc), "error_code": getattr(exc, "code", "failed")})
                try:
                    self._validate_input_hashes(check=False)
                except Exception as hash_exc:
                    self.state["input_validation_error"] = str(hash_exc)
                # Reporting failure must not mask the original stage error.
                for publish in (self._write_state, lambda: self._publish_run_report(self.state)):
                    try:
                        publish()
                    except Exception as report_exc:
                        print(f"checkpoint_write_failed: 无法保存失败回执: {report_exc}",
                              file=sys.stderr, flush=True)
                raise
            finally:
                if self._browser_runner is not None:
                    self._browser_runner.close()

    def _publish_run_report(self, report: dict[str, Any]) -> None:
        """Keep latest status consistent while retaining each failed attempt."""
        destination = self.run_dir / "run.json"
        if destination.is_file():
            previous = read_json(destination)
            if previous.get("status") == "failed":
                archive = self.run_dir / "attempts" / f"failed-{file_sha256(destination)[:16]}.json"
                if not archive.exists():
                    atomic_bytes(archive, destination.read_bytes())
        if report.get("status") == "failed":
            archive = self.run_dir / "attempts" / f"failed-{stable_sha256(report)[:16]}.json"
            if not archive.exists():
                atomic_json(archive, report)
        atomic_json(destination, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=False)
    parser.add_argument("--store", required=False)
    parser.add_argument("--issuer", required=False)
    parser.add_argument("--agent-id", required=False,
                        help="可选；当千牛页面不暴露 agentId 时传入当前接口已核对的值")
    parser.add_argument("--output-root", type=Path, default=Path("outputs"))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--replay-input", type=Path)
    parser.add_argument("--browser-config", type=Path,
                        help=f"浏览器配置 JSON；未传时读取 {QIANNIU_BROWSER_CONFIG_ENV}")
    parser.add_argument("--browser-backend", choices=("playwright",),
                        default="playwright",
                        help="兼容参数；唯一浏览器后端为 Playwright")
    parser.add_argument("--node", help="explicit Node executable for workbook rendering")
    parser.add_argument("--node-modules")
    parser.add_argument("--plan-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.resume:
            state = read_json(args.resume / "run-state.json")
            date = args.date or state.get("date")
            store = args.store or state.get("store")
            issuer = args.issuer or state.get("issuer")
            agent_id = args.agent_id or state.get("agent_id")
        elif args.replay_input:
            context_path = args.replay_input / "capture_context.json"
            context = read_json(context_path) if context_path.exists() else {}
            date = args.date or context.get("date")
            store = args.store or context.get("store")
            issuer = args.issuer or context.get("issuer")
            agent_id = args.agent_id or context.get("agentId")
        else:
            date, store, issuer, agent_id = args.date, args.store, args.issuer, args.agent_id
        if not date or not store or not issuer:
            raise OnlineError("--date、--store、--issuer 为必填参数（--resume/--replay-input 可从快照推断）", "configuration")
        runner = OnlineRunner(date=date, store=store, issuer=issuer, agent_id=agent_id, output_root=args.output_root,
                              run_dir=args.run_dir, resume=args.resume, replay_input=args.replay_input,
                              browser_config=args.browser_config, browser_backend=args.browser_backend,
                              node=args.node,
                              node_modules=args.node_modules,
                              plan_only=args.plan_only)
        try:
            runner.run()
        finally:
            if getattr(runner, "_browser_runner", None) is not None:
                runner._browser_runner.close()
    except OnlineError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
