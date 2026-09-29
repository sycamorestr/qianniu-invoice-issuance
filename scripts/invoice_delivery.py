"""Package verified invoice deliverables and optionally send one WeCom file message.

Delivery checkpoints are independent of invoice checkpoints. This module never
opens a browser or changes business output, and never logs webhook URLs.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from browser_lock import FileMutex, FileMutexBusy
from invoice_scope import scope_from_record, scope_label
from invoice_delivery_log import render_summary_log

MAX_FILE_BYTES = 20 * 1024 * 1024
REQUEST_TIMEOUT = 45
SUCCESS = {'complete', 'no_applications', 'all_excluded', 'all_blocked', 'plan_only'}
COUNTS = ('selected_count', 'ready_count', 'blocked_count', 'excluded_count')
AMOUNTS = ('ready_amount', 'blocked_amount', 'excluded_amount')
SUMMARY_LOG_NAME = '千牛平台_开票汇总日志.txt'
FAILURE_REASONS = {
    'login_required': '登录已失效，需要在原浏览器中完成登录后恢复',
    'auth_required': '登录认证未通过，需要在原浏览器中处理后恢复',
    'permission_required': '当前账号缺少发票查看权限，需要主账号授权后恢复',
    'context_missing': '业务页面必要信息未就绪，需要恢复原页面后继续',
    'context_changed': '页面店铺或开票主体发生变化，需要核对原环境',
    'context_mismatch': '当前店铺或开票主体与任务不一致，需要核对原环境',
    'profile_locked': '浏览器环境正被其他任务占用，待释放后恢复',
    'page_missing': '业务页面缺失，需要恢复原页面后继续',
    'browser_disconnected': '浏览器连接中断，需要恢复原环境后继续',
    'rate_limited': '平台限制了请求频率，需处理验证或稍后恢复',
    'checkpoint_invalid': '恢复记录或交付证据不完整，需要检查本地作业记录',
    'resume_mismatch': '原任务的环境或文件校验不一致，已停止恢复',
}


class DeliveryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _require(condition, code='verification_failed', message='交付文件校验失败，未发送'):
    if not condition:
        raise DeliveryError(code, message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _read_json(path):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    _require(isinstance(value, dict))
    return value


def _inside(path, root):
    path = Path(path).resolve()
    _require(path != root and path.is_relative_to(root), 'path_outside_run', '文件路径越出当前任务，未发送')
    return path


def _publish(path, data):
    # Keep temporary names short on Windows even when the final file name
    # contains a shop name or a digest. Do not append to the final basename.
    temporary = path.with_name('.delivery-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _proof_map(records, root):
    _require(isinstance(records, list) and records)
    result = {}
    for record in records:
        _require(isinstance(record, dict))
        path = _inside(record.get('path', ''), root)
        digest = record.get('sha256')
        _require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest))
        _require(path not in result or result[path] == digest)
        result[path] = digest
    return result


def _verified_bytes(path, proof):
    _require(path in proof and path.is_file(), message='交付文件缺失或缺少校验记录，未发送')
    data = path.read_bytes()
    _require(_sha(data) == proof[path], message='交付文件哈希不一致，未发送')
    return data


def _safe_name(value):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value)).strip(' .')[:90] or 'shop'


def _summary(manifest):
    # No machine paths, input receipts, error strings or arbitrary nested fields.
    result = {key: manifest.get(key) for key in ('store', 'issuer', 'status')}
    for key in ('store', 'issuer'):
        _require(result[key] is None or isinstance(result[key], str))
    for key in COUNTS:
        value = manifest.get(key)
        _require(value is None or (type(value) is int and value >= 0))
        result[key] = value
    for key in AMOUNTS:
        value = manifest.get(key)
        if value is not None:
            try:
                amount = Decimal(str(value))
                _require(amount.is_finite())
                value = str(amount)
            except InvalidOperation:
                raise DeliveryError('verification_failed', '结果金额无效，未发送') from None
        result[key] = value
    return result


def _exception_reasons(content, summary):
    """Aggregate only the already verified business CSV, never raw errors."""
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'), newline=''), strict=True)
    _require({'申请流水号', '金额', '暂缓原因'} <= set(reader.fieldnames or []),
             message='异常清单缺少汇总日志所需字段，未发送')
    groups, seen = {}, set()
    for row in reader:
        sid = (row.get('申请流水号') or '').strip()
        reason = ' '.join((row.get('暂缓原因') or '').split())
        _require(sid and sid not in seen and reason and None not in row,
                 message='异常清单行不完整或流水号重复，未发送')
        seen.add(sid)
        raw = (row.get('金额') or '').strip()
        try:
            amount = Decimal(raw) if raw else None
        except InvalidOperation:
            raise DeliveryError('verification_failed', '异常清单金额无效，未发送') from None
        _require(amount is None or amount.is_finite(), message='异常清单金额无效，未发送')
        group = groups.setdefault(reason, {'reason': reason, 'count': 0, 'amount': Decimal(0)})
        group['count'] += 1
        group['amount'] = None if amount is None or group['amount'] is None else group['amount'] + amount
    counts = [summary[key] for key in ('blocked_count', 'excluded_count')]
    if all(value is not None for value in counts):
        _require(len(seen) == sum(counts), message='异常清单笔数与结果汇总不一致，未发送')
    return [{**group, 'amount': str(group['amount']) if group['amount'] is not None else None}
            for group in groups.values()]


def _shop_files(run_dir, scope, batch_shop=None):
    state_path = _inside(run_dir / 'run-state.json', run_dir)
    state_data = state_path.read_bytes()
    state = json.loads(state_data)
    _require(state.get('status') == 'complete')
    _require(scope_from_record(state) == scope)
    generated = _inside(state['generated_dir'], run_dir)
    stage = state.get('stages', {}).get('generate', {})
    _require(stage.get('status') == 'complete')
    proof = _proof_map(stage.get('outputs'), run_dir)
    manifest_path = _inside(generated / 'run.json', run_dir)
    manifest_bytes = _verified_bytes(manifest_path, proof)
    manifest = json.loads(manifest_bytes)
    status = manifest.get('status')
    _require(status in SUCCESS and state.get('result_status') == status)
    _require(scope_from_record(manifest) == scope)
    if batch_shop is not None:
        _require(batch_shop['status'] == status)
        outer_proof = _proof_map(batch_shop.get('proof'), run_dir)
        _require(outer_proof.get(state_path) == _sha(state_data))
        _require(outer_proof.get(manifest_path) == _sha(manifest_bytes))
    else:
        outer_proof = proof
    files = {}
    label = scope_label(scope)
    names = ['exceptions.csv']
    common_path = _inside(generated / f'qianniu_common_{label}.xlsx', run_dir)
    common = manifest.get('common_template_output')
    if common is not None:
        _require(_inside(common['path'], run_dir) == common_path)
        _require(common.get('sha256') == proof.get(common_path))
        names.append(common_path.name)
    else:
        _require(status == 'no_applications')
        empty = manifest.get('empty_export', {})
        empty_path = _inside(empty.get('path', ''), run_dir)
        _require(empty_path.is_file() and empty_path.read_bytes() == b'' and empty.get('sha256') == _sha(b''))
    if status == 'complete':
        tax_path = _inside(generated / f'qianniu_invoice_tax_template_{label}.xlsx', run_dir)
        _require(_inside(manifest.get('output', ''), run_dir) == tax_path)
        _require(manifest.get('output_sha256') == proof.get(tax_path))
        names.append(tax_path.name)
    for name in names:
        path = _inside(generated / name, run_dir)
        content = _verified_bytes(path, proof)
        _require(outer_proof.get(path) == _sha(content))
        files[name] = content
    summary = _summary(manifest)
    summary['exception_reasons'] = _exception_reasons(files['exceptions.csv'], summary)
    return files, summary


def _package_content(run_dir):
    batch_path = run_dir / 'batch-state.json'
    entries = {}
    if batch_path.is_file():
        state = _read_json(batch_path)
        _require(state.get('status') in {'complete', 'partial', 'stopped', 'interrupted'},
                 'business_not_finished', '任务仍在执行，暂不交付')
        scope = scope_from_record(state)
        shops = state.get('shops')
        _require(isinstance(shops, list) and shops)
        report = {'type': 'batch', 'status': state['status'], 'query_scope': scope, 'shops': []}
        ids = set()
        plan_only = bool(state.get('plan_only'))
        for shop in shops:
            sid = shop.get('id')
            _require(isinstance(sid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', sid) and sid not in ids)
            ids.add(sid)
            row = {'id': sid, 'store': shop.get('store'), 'status': shop.get('status')}
            _require(isinstance(row['store'], str))
            if row['status'] in SUCCESS:
                child = _inside(shop['run_dir'], run_dir)
                _require(child == (run_dir / 'shops' / sid).resolve())
                files, summary = _shop_files(child, scope, shop)
                row.update(summary)
                folder = f'{sid}_{_safe_name(row["store"])}'
                entries.update({f'{folder}/{name}': data for name, data in files.items()})
                entries[f'{folder}/result.json'] = _json_bytes({'query_scope': scope, **row})
                plan_only |= row['status'] == 'plan_only'
            else:
                # Failure strings often contain local paths or request URLs.
                row['status'] = 'failed' if row['status'] == 'failed' else 'not_executed'
                if row['status'] == 'failed':
                    site = {'qianniu': '千牛：', 'jst': '票聚：'}.get(shop.get('error_site'), '')
                    row['failure_reason'] = site + FAILURE_REASONS.get(
                        shop.get('error_code'), '任务执行失败，需检查本地作业记录后恢复')
            report['shops'].append(row)
    else:
        state = _read_json(run_dir / 'run-state.json')
        scope = scope_from_record(state)
        files, summary = _shop_files(run_dir, scope)
        entries.update(files)
        report = {'type': 'single', 'query_scope': scope, **summary}
        plan_only = summary['status'] == 'plan_only'
    report['note'] = '仅生成模板，未提交开票。通用模板保留平台完整原件；税局模板仅含通过校验的申请。'
    entries['result.json'] = _json_bytes(report)
    entries[SUMMARY_LOG_NAME] = render_summary_log(report)
    return entries, report, plan_only


def _zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _destination(config_path):
    if config_path is None:
        return None, 'not_configured'
    _require(config_path.is_file(), 'configuration', '推送配置文件不存在')
    config = _read_json(config_path)
    _require(config.get('schema_version') == 1, 'configuration', '推送配置版本无效')
    wecom = config.get('wecom')
    _require(isinstance(wecom, dict) and type(wecom.get('enabled')) is bool,
             'configuration', '推送配置格式无效')
    if not wecom['enabled']:
        return None, 'disabled'
    url = wecom.get('webhook_url')
    _require(isinstance(url, str), 'configuration', '企微 Webhook 格式无效')
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    _require(parsed.scheme == 'https' and parsed.netloc == 'qyapi.weixin.qq.com'
             and parsed.path == '/cgi-bin/webhook/send' and not parsed.fragment
             and set(query) == {'key'} and len(query['key']) == 1
             and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', query['key'][0]),
             'configuration', '仅允许有效的企微群机器人 Webhook')
    return 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + query['key'][0], None


def resolve_notification_config(explicit: Path | None = None, legacy: Path | None = None) -> Path | None:
    """Resolve CLI config without reading secrets or changing the API's opt-in behavior."""
    if explicit is not None:
        # A missing explicit file is a configuration error, never a fallback.
        return Path(explicit).resolve()
    local = Path(__file__).resolve().parent.parent / 'notifications.json'
    if local.exists():
        # Disabled or invalid local config must not select a different group.
        return local
    if legacy is not None and Path(legacy).exists():
        return Path(legacy).resolve()
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'redirect blocked', headers, fp)


def _request(url, body, content_type):
    request = urllib.request.Request(url, data=body, headers={'Content-Type': content_type}, method='POST')
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
        _require(response.status == 200, 'response_invalid', '企微响应状态无效')
        payload = response.read(1024 * 1024 + 1)
    _require(len(payload) <= 1024 * 1024, 'response_invalid', '企微响应超出预期')
    data = json.loads(payload)
    _require(isinstance(data, dict) and type(data.get('errcode')) is int,
             'response_invalid', '企微响应格式无效')
    return data


def _upload(url, content, filename):
    boundary = '----invoice-' + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="media"; '
            f'filename="{filename}"\r\nContent-Type: application/zip\r\n\r\n').encode('utf-8') + content + f'\r\n--{boundary}--\r\n'.encode()
    target = url.replace('/webhook/send?', '/webhook/upload_media?') + '&type=file'
    data = _request(target, body, f'multipart/form-data; boundary={boundary}')
    _require(data['errcode'] == 0, 'upload_rejected', f'企微上传被拒绝（错误码 {data["errcode"]}）')
    media_id = data.get('media_id')
    _require(isinstance(media_id, str) and media_id, 'upload_invalid', '企微上传缺少文件标识')
    return media_id


def deliver_result(run_dir: Path, config_path: Path | None = None, *, package_only=False, retry_unknown=False) -> dict:
    """Return a delivery result; errors never alter or invalidate invoice results."""
    result = {'status': 'failed'}
    mutex = None
    receipt_path = None
    try:
        run_dir = Path(run_dir).resolve()
        _require(run_dir.is_dir(), 'run_missing', '交付任务目录不存在')
        delivery_dir = _inside(run_dir / 'delivery', run_dir)
        _require(delivery_dir == run_dir / 'delivery', 'path_outside_delivery', '交付目录不能指向业务产物目录')
        delivery_dir.mkdir(exist_ok=True)
        mutex = FileMutex(_inside(delivery_dir / '.delivery.lock', delivery_dir))
        mutex.acquire()
        entries, report, plan_only = _package_content(run_dir)
        content = _zip(entries)
        digest = _sha(content)
        scope = report['query_scope']
        period = scope['date'] if scope['mode'] == 'date' else f'{scope["start_date"]}_{scope["end_date"]}'
        stores = report.get('shops', [report])
        name = _safe_name(stores[0]['store']) if len(stores) == 1 else f'{len(stores)}店铺'
        zip_path = _inside(delivery_dir / f'千牛平台_{name}_开票汇总_{period}_{digest[:12]}.zip', delivery_dir)
        _publish(zip_path, content)
        _publish(_inside(delivery_dir / 'result.json', delivery_dir), _json_bytes(report))
        _publish(_inside(delivery_dir / SUMMARY_LOG_NAME, delivery_dir), entries[SUMMARY_LOG_NAME])
        result.update(zip_path=str(zip_path), zip_sha256=digest, zip_bytes=len(content))
        if package_only or plan_only:
            result['status'] = 'plan_only' if plan_only else 'packaged'
            return result
        # Only the completed batch may send. Enforce this here as well as in
        # the batch CLI so standalone delivery/retry cannot send an interim
        # package or one of the batch's child shops.
        if report['type'] == 'batch' and (
                report['status'] != 'complete'
                or any(shop['status'] not in SUCCESS for shop in report['shops'])):
            result.update(status='deferred', code='batch_incomplete',
                          reason='批次尚未全部完成，压缩包仅保存在本地；恢复完成后统一发送最终汇总')
            return result
        if report['type'] == 'single' and run_dir.parent.name == 'shops' and (
                run_dir.parent.parent / 'batch-state.json').exists():
            result.update(status='deferred', code='batch_child',
                          reason='批次内店铺不单独推送，请使用整批最终汇总')
            return result
        url, inactive = _destination(Path(config_path) if config_path is not None else None)
        if inactive:
            result['status'] = inactive
            return result
        destination = _sha(url.encode())
        receipt_id = _sha((digest + ':' + destination).encode('ascii'))
        receipt_path = _inside(delivery_dir / f'receipt-{receipt_id}.json', delivery_dir)
        result.update(receipt_path=str(receipt_path), destination_sha256=destination)

        def save(status, **extra):
            result.update(status=status, **extra)
            _publish(receipt_path, _json_bytes({**result, 'updated_at': datetime.now(timezone.utc).isoformat()}))

        if receipt_path.exists():
            saved = _read_json(receipt_path)
            _require(saved.get('zip_sha256') == digest and saved.get('destination_sha256') == destination)
            if saved.get('status') == 'sent':
                result.update(status='sent', already_sent=True)
                return result
            if saved.get('status') in {'sending', 'unknown'} and not retry_unknown:
                save('unknown', code='send_unconfirmed', reason='上次发送结果不明；请先核对群消息，确认需要重发时使用 --retry-unknown')
                return result
        if len(content) > MAX_FILE_BYTES:
            save('failed', code='file_too_large', reason='压缩包超过企微 20 MiB 文件上限，文件已保留在本地')
            return result
        save('uploading')
        try:
            media_id = _upload(url, content, zip_path.name)
        except DeliveryError as exc:
            save('failed', code=exc.code, reason=str(exc))
            return result
        except Exception:
            save('failed', code='upload_failed', reason='企微上传失败；可独立重试推送，无需重新执行开票采集')
            return result
        # Persist before the side effect. Crash or ambiguity must never cause
        # an automatic duplicate message on the next invocation.
        save('sending')
        try:
            data = _request(url, _json_bytes({'msgtype': 'file', 'file': {'media_id': media_id}}), 'application/json')
        except Exception:
            save('unknown', code='send_unconfirmed', reason='企微发送结果不明；请先核对群消息，确认需要重发时使用 --retry-unknown')
            return result
        if data['errcode'] != 0:
            save('failed', code='send_rejected', reason=f'企微发送被拒绝（错误码 {data["errcode"]}）')
        else:
            save('sent')
        return result
    except FileMutexBusy:
        result.update(status='busy', code='delivery_locked', reason='另一交付任务正在运行')
    except DeliveryError as exc:
        result.update(status='failed', code=exc.code, reason=str(exc))
    except Exception:
        # Do not echo exception text: urllib/config exceptions can contain keys.
        result.update(status='failed', code='delivery_error', reason='文件交付失败，请检查本地配置与交付文件；业务结果保持不变')
    finally:
        if mutex is not None:
            mutex.release()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--config', type=Path, help='企微推送配置；默认读取 skill 根目录的 notifications.json')
    parser.add_argument('--package-only', action='store_true')
    parser.add_argument('--retry-unknown', action='store_true')
    args = parser.parse_args(argv)
    result = deliver_result(args.run_dir, resolve_notification_config(args.config),
                            package_only=args.package_only, retry_unknown=args.retry_unknown)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['status'] in {'failed', 'unknown', 'busy'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
