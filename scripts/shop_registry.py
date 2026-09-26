"""Private shop registry shared by batch jobs and browser setup."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from playwright_controller import load_browser_config, _configured_debug_port
from run_online import OnlineError, stable_sha256


def resolved(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_registry(path: Path, *, require_issuer: bool = True) -> dict:
    path = path.resolve()
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        raise OnlineError('店铺清单 schema_version 必须为 1', 'configuration')
    issuer = str(raw.get('issuer') or '').strip()
    if require_issuer and not issuer:
        raise OnlineError('店铺清单缺少已核对的票聚 issuer', 'configuration')
    shops = raw.get('shops')
    if not isinstance(shops, list) or not shops:
        raise OnlineError('店铺清单 shops 必须是非空数组', 'configuration')
    if not raw.get('jst_browser_config'):
        raise OnlineError('店铺清单缺少共享票聚配置', 'configuration')
    shared_path = resolved(path.parent, raw['jst_browser_config'])
    shared, _ = load_browser_config(shared_path)
    if set(shared['browser_sessions']) != {'goods'}:
        raise OnlineError('共享票聚环境只能配置 goods 页面', 'configuration')
    identities = []
    ids, stores, roots, downloads, ports = set(), set(), [], set(), set()

    def register(config):
        root = os.path.normcase(str(Path(config['user_data_dir']).resolve()))
        download = os.path.normcase(str(Path(config['download_dir']).resolve()))
        port = _configured_debug_port(config)
        if any(root == other or Path(root) in Path(other).parents or Path(other) in Path(root).parents
               for other in roots):
            raise OnlineError('不同浏览器必须使用互不嵌套的独立用户数据目录', 'configuration')
        if download in downloads or port in ports:
            raise OnlineError('不同浏览器必须使用独立下载目录和调试端口', 'configuration')
        roots.append(root); downloads.add(download); ports.add(port)
        # Contains local endpoint configuration only as a digest in job reports.
        return stable_sha256(config)

    shared_digest = register(shared)
    normalized = []
    for shop in shops:
        if not isinstance(shop, dict):
            raise OnlineError('每个店铺必须是对象', 'configuration')
        sid, store = str(shop.get('id') or ''), str(shop.get('store') or '').strip()
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', sid) or sid in {'shared', 'piaoju'}:
            raise OnlineError('店铺 id 必须为安全的字母数字标识，且不能为 shared/piaoju', 'configuration')
        if not store or sid in ids or store in stores or not shop.get('browser_config'):
            raise OnlineError('店铺名称/id 必须唯一，并提供 browser_config', 'configuration')
        if shop.get('issuer') and shop['issuer'] != issuer:
            raise OnlineError('此批次只支持同一个共享票聚主体', 'configuration')
        ids.add(sid); stores.add(store)
        config_path = resolved(path.parent, shop['browser_config'])
        config, _ = load_browser_config(config_path)
        if set(config['browser_sessions']) != {'invoice', 'orders'}:
            raise OnlineError('店铺环境只能配置 invoice 和 orders 页面', 'configuration')
        digest = register(config)
        normalized.append({'id': sid, 'store': store, 'browser_config': str(config_path)})
        identities.append({'id': sid, 'store': store, 'browser_config': str(config_path),
                           'browser_identity_sha256': digest})
    identity = {'issuer': issuer, 'jst_browser_config': str(shared_path),
                'shared_identity_sha256': shared_digest, 'shops': identities}
    return {'path': str(path), 'issuer': issuer, 'shops': normalized,
            'jst_browser_config': str(shared_path),
            'output_root': str(resolved(path.parent, raw.get('output_root') or 'outputs')),
            'identity_sha256': stable_sha256(identity)}
