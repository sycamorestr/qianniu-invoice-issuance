"""Sequential, resumable shop jobs using one shared Piaoju browser."""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import uuid
from datetime import date as calendar_date, datetime
from decimal import Decimal
from pathlib import Path

from browser_lock import FileMutex, FileMutexBusy
from run_online import OnlineRunner, OnlineError, replace_checkpoint, file_sha256, read_json, utc_now
from shop_registry import load_registry

SUCCESS = {'complete', 'no_applications', 'all_excluded', 'all_blocked', 'plan_only'}
SHOP_FAILURES = {'auth_required', 'login_required', 'context_changed', 'context_mismatch',
                 'context_missing', 'page_missing', 'browser_disconnected', 'profile_locked'}


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
                 plan_only: bool = False, runner_factory=OnlineRunner):
        self.registry = load_registry(registry)
        self.node, self.node_modules = node, node_modules
        self.runner_factory = runner_factory
        if resume:
            self.run_dir = resume.resolve()
            self.state = read_json(self.run_dir / 'batch-state.json')
            if (self.state.get('version') != 1 or
                    self.state['registry_identity_sha256'] != self.registry['identity_sha256'] or
                    (date and date != self.state['date']) or
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
            self.node = node or self.state.get('node')
            self.node_modules = node_modules or self.state.get('node_modules')
            if plan_only and not self.state['plan_only']:
                raise OnlineError('恢复不能改变 plan-only 模式', 'resume_mismatch')
        else:
            if not date:
                raise OnlineError('新批次必须提供 --date', 'configuration')
            calendar_date.fromisoformat(date)
            available = {shop['id']: shop for shop in self.registry['shops']}
            selected = shops if shops is not None else list(available)
            if not selected or len(set(selected)) != len(selected) or set(selected) - available.keys():
                raise OnlineError('--shops 必须是清单中不重复的店铺 id', 'configuration')
            root = (output_root or Path(self.registry['output_root'])).resolve()
            self.run_dir = root / f'batch-{date}-{datetime.now():%Y%m%d-%H%M%S-%f}'
            self.run_dir.mkdir(parents=True, exist_ok=False)
            self.state = {'version': 1, 'date': date, 'status': 'created', 'created_at': utc_now(),
                          'registry_path': self.registry['path'],
                          'registry_identity_sha256': self.registry['identity_sha256'],
                          'selected_shop_ids': selected, 'plan_only': plan_only,
                          'node': node, 'node_modules': node_modules,
                          'shops': [{**available[sid], 'status': 'pending', 'attempts': [],
                                     'run_dir': str(self.run_dir / 'shops' / sid)} for sid in selected]}
        self.lock = FileMutex(self.run_dir / '.batch.lock')

    def save(self):
        publish_json(self.run_dir / 'batch-state.json', self.state)
        rows = [{key: shop.get(key) for key in ('id', 'store', 'status', 'ready_count', 'ready_amount',
                    'blocked_count', 'blocked_amount', 'excluded_count', 'error_code', 'error_site',
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
        paths = {state_path, run_dir / 'run.json'}
        for item in state.get('input_hashes', []):
            paths.add(Path(item['path']))
        for stage in state.get('stages', {}).values():
            for item in stage.get('outputs', []):
                paths.add(Path(item['path']))
        generated = Path(state['generated_dir'])
        paths.update(generated / name for name in ('run.json', 'exceptions.csv'))
        paths.add(generated / ('qianniu_common_' + state['date'] + '.xlsx'))
        if read_json(generated / 'run.json').get('status') == 'complete':
            paths.add(generated / ('qianniu_invoice_tax_template_' + state['date'] + '.xlsx'))
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
                shop['attempts'].append(attempt)
                shop['status'] = 'running'
                self.save()
                print(f"{shop['id']}: {shop['store']}", file=sys.stderr, flush=True)
                try:
                    kwargs = dict(date=self.state['date'], store=shop['store'], issuer=self.registry['issuer'],
                                  output_root=self.run_dir / 'shops', browser_config=Path(shop['browser_config']),
                                  jst_browser_config=Path(self.registry['jst_browser_config']),
                                  node=self.node, node_modules=self.node_modules, plan_only=self.state['plan_only'])
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
                    shop.update(common_template=str(generated / f"qianniu_common_{self.state['date']}.xlsx"),
                                tax_template=manifest.get('output'), exceptions=str(generated / 'exceptions.csv'),
                                run_manifest=str(generated / 'run.json'), proof=self.proof(child_dir))
                    shop.pop('error_code', None); shop.pop('error_site', None); shop.pop('error', None)
                    attempt.update(status=shop['status'], finished_at=utc_now())
                except Exception as exc:
                    code, site = getattr(exc, 'code', 'failed'), getattr(exc, 'site', None)
                    shop.update(status='failed', error_code=code, error_site=site, error=str(exc))
                    attempt.update(status='failed', error_code=code, error_site=site, finished_at=utc_now())
                    # A shared failure affects every remaining shop. Stop once;
                    # never issue the same bad login/query nine times.
                    if site != 'qianniu' or code not in SHOP_FAILURES:
                        self.state['status'] = 'stopped'
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
    parser.add_argument('--date')
    parser.add_argument('--shops', nargs='+')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--node'); parser.add_argument('--node-modules')
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = BatchRunner(**vars(args)).run()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'complete' else 2
    except (OnlineError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'configuration')}: {exc}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
