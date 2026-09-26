"""Derive invoice page roles in memory from a workbench homepage config."""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlsplit


INVOICE_URL = 'https://myseller.taobao.com/home.htm/merchant-invoice/'
ORDERS_URL = 'https://myseller.taobao.com/home.htm/trade-platform/tp/sold'
ALLOWED_ROLES = {'home', 'invoice', 'orders', 'goods'}


def with_invoice_pages(config: dict) -> dict:
    """Keep browser identity and explicit business options; never edit input.

    A homepage marks a Qianniu environment owned by the browser workbench.
    Only that layout gains invoice/order defaults. Legacy business layouts
    remain identical, including incomplete layouts rejected by their caller.
    """
    if not isinstance(config, dict):
        raise ValueError('浏览器配置必须是对象')
    sessions = config.get('browser_sessions')
    if not isinstance(sessions, dict) or not sessions:
        raise ValueError('browser_sessions 必须是非空页面角色对象')
    if set(sessions) - ALLOWED_ROLES:
        raise ValueError('browser_sessions 包含未知页面角色')
    result = deepcopy(config)
    if 'home' not in sessions:
        return result
    home = sessions['home']
    url = home.get('url') if isinstance(home, dict) else home
    if not isinstance(url, str):
        raise ValueError('home 必须提供千牛主页 URL')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or (parsed.hostname or '').lower() != 'myseller.taobao.com':
        raise ValueError('home 必须是千牛商家主页 URL')
    effective = result['browser_sessions']
    effective.pop('home')
    effective.setdefault('invoice', {'url': INVOICE_URL})
    effective.setdefault('orders', {'url': ORDERS_URL})
    return result
