"""Initialize isolated shop profiles and explicitly start selected browsers."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
from pathlib import Path

from playwright_controller import PlaywrightBrowserController, BrowserControllerError, load_browser_config
from run_online import OnlineError, atomic_json
from shop_registry import load_registry

URLS = {
    'invoice': 'https://myseller.taobao.com/home.htm/merchant-invoice/',
    'orders': 'https://myseller.taobao.com/home.htm/trade-platform/tp/sold',
    'goods': 'https://fp.erp321.com/setting/goodsManage',
}


def available_ports(count: int):
    result = []
    for port in range(9400, 9900):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                continue
        result.append(port)
        if len(result) == count:
            return result
    raise OnlineError('没有足够可用的本机调试端口', 'configuration')


def initialize(root: Path, shops_file: Path, issuer: str | None):
    root = root.resolve()
    shops = json.loads(shops_file.read_text(encoding='utf-8-sig'))
    if not isinstance(shops, list) or not shops:
        raise OnlineError('shops-file 必须是店铺对象数组', 'configuration')
    clean, ids, stores = [], set(), set()
    for shop in shops:
        if not isinstance(shop, dict):
            raise OnlineError('每项需要 id、store，可选 login_username', 'configuration')
        sid, store = str(shop.get('id') or ''), str(shop.get('store') or '').strip()
        if (not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', sid) or sid in {'shared', 'piaoju'}
                or not store or sid in ids or store in stores):
            raise OnlineError('店铺 id/名称无效或重复', 'configuration')
        if any('pass' in str(key).lower() or '密码' in str(key) for key in shop):
            raise OnlineError('初始化清单不要包含密码，浏览器负责保存登录态', 'configuration')
        ids.add(sid); stores.add(store)
        clean.append({'id': sid, 'store': store,
                      **({'login_username': str(shop['login_username'])} if shop.get('login_username') else {})})
    registry_path = root / 'shops.json'
    if registry_path.exists():
        existing = load_registry(registry_path, require_issuer=False)
        if ([(s['id'], s['store']) for s in existing['shops']] != [(s['id'], s['store']) for s in clean]
                or (issuer is not None and existing['issuer'] != issuer)):
            raise OnlineError('已有清单不同，拒绝覆盖浏览器环境', 'configuration')
        return {'status': 'already_initialized', 'registry': str(registry_path), 'shops': len(clean)}
    # A fresh initialization never reuses an occupied directory or profile.
    for name in ('config', 'profiles', 'downloads'):
        if (root / name).exists() and any((root / name).iterdir()):
            raise OnlineError('目标已有浏览器文件且无完整清单，保留现场并停止', 'configuration')
    ports = iter(available_ports(len(clean) + 1))
    for sid, roles in [('piaoju', ['goods'])] + [(s['id'], ['invoice', 'orders']) for s in clean]:
        (root / 'profiles' / sid).mkdir(parents=True, exist_ok=True)
        (root / 'downloads' / sid).mkdir(parents=True, exist_ok=True)
        atomic_json(root / 'config' / f'{sid}.json', {
            'schema_version': 1, 'browser': 'Edge', 'user_data_dir': f'../profiles/{sid}',
            'profile_directory': 'Default', 'remote_debugging_port': next(ports),
            'download_dir': f'../downloads/{sid}',
            'browser_sessions': {role: {'url': URLS[role]} for role in roles},
        })
    (root / 'outputs').mkdir(exist_ok=True)
    atomic_json(registry_path, {
        'schema_version': 1, 'issuer': issuer or '', 'jst_browser_config': 'config/piaoju.json',
        'output_root': 'outputs',
        'shops': [{**shop, 'browser_config': f"config/{shop['id']}.json"} for shop in clean],
    })
    load_registry(registry_path, require_issuer=False)
    return {'status': 'initialized', 'registry': str(registry_path), 'shops': len(clean),
            'browser_environments': len(clean) + 1}


async def manage(registry_path: Path, command: str, targets: list[str] | None,
                 all_browsers: bool, timeout: int):
    registry = load_registry(registry_path, require_issuer=False)
    choices = {'piaoju': {'name': '共享票聚', 'config': registry['jst_browser_config']},
               **{s['id']: {'name': s['store'], 'config': s['browser_config']} for s in registry['shops']}}
    selected = list(choices) if all_browsers else targets
    if not selected or len(set(selected)) != len(selected) or set(selected) - choices.keys():
        raise OnlineError('提供 --all 或有效的 --targets（含 piaoju 或店铺 id）', 'configuration')
    results = []
    for target in selected:
        choice = choices[target]
        config, _ = load_browser_config(choice['config'])
        controller = PlaywrightBrowserController(config, timeout_ms=timeout)
        print(f"{command}: {target} {choice['name']}", file=sys.stderr, flush=True)
        try:
            if command == 'start':
                await controller.start(open_missing=True)
            else:
                await controller.connect()
            result = {'id': target, 'name': choice['name'], 'status': 'connected',
                      'authentication': 'not_checked', 'roles': list(controller.registrations)}
        except BrowserControllerError as exc:
            result = {'id': target, 'name': choice['name'], 'status': 'needs_attention',
                      'error_code': exc.code, 'message': str(exc)}
        finally:
            try:
                await controller.close()
            except Exception:
                result['status'] = 'needs_attention'
                result['cleanup_error'] = '控制连接未正常释放，请先检查该环境'
        results.append(result)
    return {'status': 'ready' if all(r['status'] == 'connected' for r in results) else 'needs_attention',
            'authentication': 'not_checked', 'browsers': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--root', required=True, type=Path)
    init.add_argument('--shops-file', required=True, type=Path)
    init.add_argument('--issuer')
    for command in ('start', 'status'):
        sub = commands.add_parser(command)
        sub.add_argument('--registry', required=True, type=Path)
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument('--all', action='store_true')
        group.add_argument('--targets', nargs='+')
        sub.add_argument('--timeout', type=int, default=15000)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = initialize(args.root, args.shops_file, args.issuer)
        else:
            result = asyncio.run(manage(args.registry, args.command, args.targets, args.all, args.timeout))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result['status'] == 'needs_attention' else 0
    except (OnlineError, BrowserControllerError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'configuration')}: {exc}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
