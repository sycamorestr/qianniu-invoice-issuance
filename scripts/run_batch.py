"""Sequential, resumable shop jobs using one shared Piaoju browser."""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from browser_lock import FileMutex, FileMutexBusy
from invoice_scope import resolve_scope, scope_fields, scope_from_record, scope_label
from invoice_tax_policy import load_tax_rate_config, policy_for_store
from run_online import (OnlineRunner, OnlineError, replace_checkpoint, file_sha256, read_json,
                        utc_now, stable_sha256, finalize_delivery, frozen_tax_rate_policy,
                        APPROVAL_BATCH_MODES)
from shop_registry import load_registry

SUCCESS = {'complete', 'no_applications', 'all_excluded', 'all_blocked', 'plan_only'}
SHOP_FAILURES = {'auth_required', 'login_required', 'permission_required', 'context_changed', 'context_mismatch',
                 'context_missing', 'page_missing', 'browser_disconnected', 'profile_locked',
                 'browser_launch_failed', 'browser_identity_unverified'}


def selected_identity(registry: dict, ids: list[str]) -> str:
    """Freeze this batch, allowing later additions to the workbench catalog."""
    available = {shop['id']: shop for shop in registry['shops']}
    if not ids or len(set(ids)) != len(ids) or set(ids) - available.keys():
        raise OnlineError('批次选中店铺缺失或重复', 'resume_mismatch')
    return stable_sha256({
        'issuer': registry['issuer'], 'jst_browser_config': registry['jst_browser_config'],
        'shared_identity_sha256': registry['shared_identity_sha256'],
        'shops': [{key: available[sid][key] for key in
                   ('id', 'store', 'browser_config', 'browser_identity_sha256')} for sid in ids],
    })


def publish(path: Path, content: bytes):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    temporary.write_bytes(content)
    replace_checkpoint(temporary, path)


def publish_json(path: Path, value):
    publish(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


class BatchRunner:
    def __init__(self, *, registry: Path, date: str | None = None, output_root: Path | None = None,
                 resume: Path | None = None, shops: list[str] | None = None,
                 node: str | None = None, node_modules: str | None = None,
                 plan_only: bool = False, connect_only: bool = False, all_pending: bool = False,
                 tax_rate_config: Path | None = None,
                 approval_batch_mode: str | None = None,
                 runner_factory=OnlineRunner):
        self.registry = load_registry(registry)
        self.node, self.node_modules = node, node_modules
        self.runner_factory = runner_factory
        self.connect_only = connect_only
        if approval_batch_mode is not None and (
                not isinstance(approval_batch_mode, str) or approval_batch_mode not in APPROVAL_BATCH_MODES):
            raise OnlineError('approval_batch_mode 无效', 'configuration')
        self.approval_batch_mode = approval_batch_mode or 'single_request'
        # Read once before any shop starts. A later edit must affect only a
        # new batch, including when this batch still has unstarted shops.
        try:
            tax_config = load_tax_rate_config(tax_rate_config) if tax_rate_config is not None or not resume else None
        except (ValueError, OSError, TypeError) as exc:
            raise OnlineError(f'税率配置无效: {exc}', 'configuration') from exc
        if resume:
            self.run_dir = resume.resolve()
            self.state = read_json(self.run_dir / 'batch-state.json')
            try:
                self.query_scope = resolve_scope(date=date, all_pending=all_pending, saved=self.state)
            except ValueError as exc:
                raise OnlineError(f'批次查询范围与保存记录不符: {exc}', 'resume_mismatch') from exc
            version = self.state.get('version')
            current_identity = (selected_identity(self.registry, self.state.get('selected_shop_ids', []))
                                if version == 2 else self.registry['identity_sha256'])
            if (version not in {1, 2} or
                    self.state['registry_identity_sha256'] != current_identity or
                    (shops is not None and shops != self.state['selected_shop_ids'])):
                raise OnlineError('批次日期、店铺或浏览器配置已变化', 'resume_mismatch')
            available = {shop['id']: shop for shop in self.registry['shops']}
            selected = self.state['selected_shop_ids']
            records = self.state.get('shops')
            if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
                    or set(selected) - available.keys() or not isinstance(records, list)
                    or len(records) != len(selected)):
                raise OnlineError('批次店铺清单损坏', 'checkpoint_invalid')
            for sid, saved in zip(selected, records):
                expected = available[sid]
                if (not isinstance(saved, dict) or
                        any(saved.get(key) != expected[key] for key in ('id', 'store', 'browser_config')) or
                        saved.get('run_dir') != str(self.run_dir / 'shops' / sid)):
                    raise OnlineError('批次店铺身份或输出目录与清单不符', 'resume_mismatch')
                if ('login_username' in saved and
                        saved['login_username'] != expected.get('login_username', '')):
                    raise OnlineError('批次店铺登录账号配置已变化', 'resume_mismatch')
                # Legacy batches had no account-label binding. It may be
                # supplied before first collection; OnlineRunner separately
                # rejects changes after an account context was committed.
                saved.setdefault('login_username', expected.get('login_username', ''))
                supplied = policy_for_store(tax_config, saved['store']) if tax_config is not None else None
                saved['tax_rate_policy'] = frozen_tax_rate_policy(
                    saved['store'], supplied_policy=supplied, saved=saved)
            self.node = node or self.state.get('node')
            self.node_modules = node_modules or self.state.get('node_modules')
            if plan_only and not self.state['plan_only']:
                raise OnlineError('恢复不能改变 plan-only 模式', 'resume_mismatch')
            if type(self.state.get('approve_applications', False)) is not bool:
                raise OnlineError('保存的批量同意策略无效', 'checkpoint_invalid')
            saved_batch_mode = self.state.get('approval_batch_mode', 'legacy_20')
            if not isinstance(saved_batch_mode, str) or saved_batch_mode not in APPROVAL_BATCH_MODES:
                raise OnlineError('保存的批量同意分批策略无效', 'checkpoint_invalid')
            if approval_batch_mode is not None and approval_batch_mode != saved_batch_mode:
                raise OnlineError('恢复时不能改变批量同意分批策略', 'resume_mismatch')
            self.approval_batch_mode = saved_batch_mode
        else:
            try:
                self.query_scope = resolve_scope(date=date, all_pending=all_pending)
            except ValueError as exc:
                raise OnlineError(f'新批次必须提供 --date 或 --all-pending: {exc}', 'configuration') from exc
            available = {shop['id']: shop for shop in self.registry['shops']}
            selected = shops if shops is not None else list(available)
            if not selected or len(set(selected)) != len(selected) or set(selected) - available.keys():
                raise OnlineError('--shops 必须是清单中不重复的店铺 id', 'configuration')
            policies = {sid: policy_for_store(tax_config, available[sid]['store']) for sid in selected}
            root = (output_root or Path(self.registry['output_root'])).resolve()
            self.run_dir = root / f'batch-{scope_label(self.query_scope)}-{datetime.now():%Y%m%d-%H%M%S-%f}'
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.state = {'version': 2, 'date': self.query_scope.get('date'),
                          **scope_fields(self.query_scope), 'status': 'created', 'created_at': utc_now(),
                          'registry_path': self.registry['path'],
                          'registry_identity_sha256': selected_identity(self.registry, selected),
                          'selected_shop_ids': selected, 'plan_only': plan_only,
                          'approve_applications': not plan_only,
                          'approval_batch_mode': self.approval_batch_mode,
                          'node': node, 'node_modules': node_modules,
                          'shops': [{**available[sid], 'status': 'pending', 'attempts': [],
                                     'tax_rate_policy': policies[sid],
                                     'run_dir': str(self.run_dir / 'shops' / sid)} for sid in selected]}
        self.lock = FileMutex(self.run_dir / '.batch.lock')

    def save(self):
        publish_json(self.run_dir / 'batch-state.json', self.state)
        rows = [{key: shop.get(key) for key in ('id', 'store', 'status', 'tax_rate_policy', 'ready_count', 'ready_amount',
                    'blocked_count', 'blocked_amount', 'excluded_count', 'excluded_amount',
                    'elapsed_seconds', 'error_code', 'error_site', 'error', 'recovery_action',
                    'common_template', 'tax_template', 'exceptions', 'run_manifest')}
                for shop in self.state['shops']]
        totals = {}
        completed = [shop for shop in self.state['shops'] if shop['status'] in SUCCESS]
        for key in ('ready_count', 'blocked_count', 'excluded_count'):
            totals[key] = sum(shop.get(key) or 0 for shop in completed)
        for key in ('ready_amount', 'blocked_amount', 'excluded_amount'):
            amounts = [shop.get(key) for shop in completed]
            totals[key] = None if any(item is None for item in amounts) else str(sum(map(Decimal, amounts), Decimal(0)))
        summary = {'status': self.state['status'], 'date': self.state['date'],
                   **scope_fields(self.query_scope),
                   'run_dir': str(self.run_dir), 'selected_shops': len(rows),
                   'finished_shops': len(completed), 'totals_of_finished_shops': totals, 'shops': rows}
        publish_json(self.run_dir / 'batch-summary.json', summary)
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
        publish(self.run_dir / 'batch-summary.csv', stream.getvalue().encode('utf-8-sig'))
        return summary

    @staticmethod
    def proof(run_dir: Path) -> list[dict]:
        state_path = run_dir / 'run-state.json'
        state = read_json(state_path)
        try:
            label = scope_label(scope_from_record(state))
        except ValueError as exc:
            raise OnlineError(f'成功店铺查询范围无效: {exc}', 'checkpoint_invalid') from exc
        paths = {state_path, run_dir / 'run.json'}
        if state.get('tax_rate_policy_file'):
            binding = state['tax_rate_policy_file']
            path = run_dir / 'tax-rate-policy.json'
            if (binding.get('path') != str(path.resolve()) or not path.is_file()
                    or file_sha256(path) != binding.get('sha256')):
                raise OnlineError('冻结税率策略文件缺失或变化', 'checkpoint_invalid')
            paths.add(path)
        for item in state.get('input_hashes', []):
            paths.add(Path(item['path']))
        for stage in state.get('stages', {}).values():
            for item in stage.get('outputs', []):
                paths.add(Path(item['path']))
        generated = Path(state['generated_dir'])
        paths.update(generated / name for name in ('run.json', 'exceptions.csv'))
        manifest = read_json(generated / 'run.json')
        common = generated / f'qianniu_common_{label}.xlsx'
        if common.is_file():
            paths.add(common)
        elif manifest.get('status') == 'no_applications' and manifest.get('empty_export'):
            empty = manifest['empty_export']
            evidence = Path(empty['path'])
            if (not evidence.is_file() or evidence.stat().st_size != 0 or
                    file_sha256(evidence) != empty['sha256']):
                raise OnlineError('无申请导出证据缺失或变化', 'checkpoint_invalid')
            paths.add(evidence)
        else:
            raise OnlineError('成功店铺缺少原始通用模板或已验证空导出证据', 'checkpoint_invalid')
        if manifest.get('status') == 'complete':
            paths.add(generated / f'qianniu_invoice_tax_template_{label}.xlsx')
        return [{'path': str(path), 'sha256': file_sha256(path)} for path in sorted(paths)]

    @staticmethod
    def verify_completed(shop: dict):
        if not shop.get('proof'):
            raise OnlineError('已完成店铺缺少文件校验记录', 'checkpoint_invalid')
        for item in shop['proof']:
            path = Path(item['path'])
            if not path.is_file() or file_sha256(path) != item['sha256']:
                raise OnlineError('已完成店铺文件发生变化，停止恢复', 'resume_mismatch')

    def run(self):
        try:
            self.lock.acquire()
        except FileMutexBusy as exc:
            raise OnlineError('此批次正被另一个作业执行', 'profile_locked') from exc
        try:
            # Validate all successful outputs before any new browser request.
            for shop in self.state['shops']:
                if shop['status'] in SUCCESS:
                    self.verify_completed(shop)
            self.state.update(status='running', updated_at=utc_now())
            self.save()
            for shop in self.state['shops']:
                if shop['status'] in SUCCESS:
                    continue
                child_dir = Path(shop['run_dir'])
                if child_dir.parent != self.run_dir / 'shops' or child_dir.name != shop['id']:
                    raise OnlineError('店铺输出目录不属于此批次', 'checkpoint_invalid')
                attempt = {'started_at': utc_now(), 'status': 'running'}
                attempt_started = time.perf_counter()
                shop['attempts'].append(attempt)
                shop['status'] = 'running'
                self.save()
                print(f"{shop['id']}: {shop['store']}", file=sys.stderr, flush=True)
                try:
                    kwargs = dict(date=self.state['date'], store=shop['store'], issuer=self.registry['issuer'],
                                  output_root=self.run_dir / 'shops', browser_config=Path(shop['browser_config']),
                                  jst_browser_config=Path(self.registry['jst_browser_config']),
                                  node=self.node, node_modules=self.node_modules, plan_only=self.state['plan_only'],
                                  connect_only=self.connect_only, query_scope=dict(self.query_scope),
                                  approve_applications=self.state.get('approve_applications', False),
                                  approval_batch_mode=self.approval_batch_mode,
                                  tax_rate_policy=dict(shop['tax_rate_policy']))
                    if self.query_scope['mode'] == 'all_pending':
                        kwargs['all_pending'] = True
                    if shop.get('login_username'):
                        kwargs['expected_account'] = shop['login_username']
                    if (child_dir / 'run-state.json').exists():
                        kwargs['resume'] = child_dir
                    else:
                        kwargs['run_dir'] = child_dir
                    result = self.runner_factory(**kwargs).run()
                    if result.get('status') not in SUCCESS:
                        raise OnlineError('单店任务未返回成功终态', 'failed')
                    generated = Path(result['generated_dir'])
                    manifest = read_json(generated / 'run.json')
                    if manifest.get('status') != result['status']:
                        raise OnlineError('单店回执终态不一致', 'checkpoint_invalid')
                    shop.update({key: result.get(key) for key in ('status', 'ready_count', 'ready_amount',
                                 'blocked_count', 'blocked_amount', 'excluded_count', 'excluded_amount')})
                    common = generated / f'qianniu_common_{scope_label(self.query_scope)}.xlsx'
                    shop.update(common_template=str(common) if common.is_file() else None,
                                tax_template=manifest.get('output'), exceptions=str(generated / 'exceptions.csv'),
                                run_manifest=str(generated / 'run.json'), proof=self.proof(child_dir))
                    shop.pop('error_code', None); shop.pop('error_site', None); shop.pop('error', None)
                    shop.pop('recovery_action', None)
                    attempt.update(status=shop['status'], finished_at=utc_now())
                except Exception as exc:
                    code, site = getattr(exc, 'code', 'failed'), getattr(exc, 'site', None)
                    shop.update(status='failed', error_code=code, error_site=site, error=str(exc))
                    shop['recovery_action'] = (
                        '订单查询被平台限流，整批已停止；稍后确认原浏览器无验证拦截，再恢复此批次' if site == 'qianniu' and code == 'rate_limited' else
                        '由主账号为此子账号分配发票列表查看权限后恢复此批次' if site == 'qianniu' and code == 'permission_required' else
                        '在共享票聚窗口完成人工登录后恢复此批次' if site == 'jst' and code in {'auth_required', 'login_required'} else
                        '在此店铺原浏览器窗口完成人工登录后恢复此批次' if site == 'qianniu' and code in {'auth_required', 'login_required'} else
                        '待正在使用此环境的任务结束后恢复此批次' if code == 'profile_locked' else
                        '根据错误修复原环境或配置后恢复此批次，不重复采集已完成店铺')
                    attempt.update(status='failed', error_code=code, error_site=site, finished_at=utc_now())
                    # A shared failure affects every remaining shop. Stop once;
                    # never issue the same bad login/query nine times.
                    if site != 'qianniu' or code not in SHOP_FAILURES:
                        self.state['status'] = 'stopped'
                finally:
                    shop['elapsed_seconds'] = attempt['elapsed_seconds'] = round(time.perf_counter() - attempt_started, 3)
                if self.state['status'] == 'stopped':
                    self.state['finished_at'] = utc_now()
                    return self.save()
                self.save()
            self.state['status'] = ('partial' if any(s['status'] == 'failed' for s in self.state['shops']) else 'complete')
            self.state['finished_at'] = utc_now()
            return self.save()
        except BaseException:
            self.state['status'] = 'interrupted'
            self.save()
            raise
        finally:
            self.lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--date')
    scope.add_argument('--all-pending', action='store_true', help='处理截至北京时间今日近两个月的全部待处理申请')
    parser.add_argument('--shops', nargs='+')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--node'); parser.add_argument('--node-modules')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--tax-rate-config', type=Path,
                        help='按店铺税率配置；默认读取 skill 根目录 tax-rates.json，整批开始前冻结各店策略')
    parser.add_argument('--notification-config', type=Path,
                        help='企微推送私有配置；默认读取 skill 根目录的 notifications.json，缺失时兼容店铺清单旁的旧配置')
    parser.add_argument('--no-notify', action='store_true', help='仅整理交付压缩包，不向企微发送')
    parser.add_argument('--connect-only', action='store_true',
                        help='仅连接已运行浏览器；默认按需启动本批次所需的原有环境')
    args = parser.parse_args(argv)
    try:
        kwargs = vars(args).copy()
        kwargs.pop('notification_config')
        kwargs.pop('no_notify')
        runner = BatchRunner(**kwargs)
        result = runner.run()
    except (OnlineError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'configuration')}: {exc}", file=sys.stderr)
        return 2
    output = dict(result)
    exit_code = 0 if result['status'] == 'complete' else 2
    if not runner.state['plan_only'] and result['status'] in {'complete', 'partial', 'stopped'}:
        from invoice_delivery import resolve_notification_config
        config_path = resolve_notification_config(
            args.notification_config, args.registry.resolve().parent / 'notifications.json')
        output['delivery'] = finalize_delivery(
            runner.run_dir, config_path,
            package_only=args.no_notify or result['status'] != 'complete')
        if output['delivery'].get('status') in {'failed', 'unknown', 'busy'} and exit_code == 0:
            exit_code = 3
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
