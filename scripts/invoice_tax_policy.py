"""Resolve per-store tax rates without altering collected business evidence."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / 'tax-rates.json'


def normalize_policy(value):
    """Validate a resolved policy. Missing legacy policies use Piaoju."""
    if value is None:
        return {'source': 'piaoju'}
    if not isinstance(value, dict):
        raise ValueError('税率规则必须是对象')
    source = value.get('source')
    if source == 'piaoju' and set(value) == {'source'}:
        return {'source': 'piaoju'}
    if source != 'fixed' or set(value) != {'source', 'rate'}:
        raise ValueError('税率规则仅支持 source=piaoju，或 source=fixed 并提供 rate')
    raw = value['rate']
    if (type(raw) not in {str, int, float}
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', str(raw))):
        raise ValueError('固定税率须为 0 到 1 的小数，例如 0.1 表示 10%，不能填写 10%')
    rate = Decimal(str(raw))
    if not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1):
        raise ValueError('固定税率须为 0 到 1 的有限小数')
    text = format(rate, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return {'source': 'fixed', 'rate': text}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('税率配置包含重复字段或重复店铺')
        result[key] = value
    return result


def load_tax_rate_config(config_path=None):
    """Read one config snapshot; only an absent implicit config is legacy."""
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG
    if config_path is None and not path.exists():
        return {'schema_version': 1, 'default': {'source': 'piaoju'}, 'stores': {}}
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise ValueError('税率配置无法读取或 JSON 无效，请检查 tax-rates.json') from exc
    if (not isinstance(config, dict) or set(config) != {'schema_version', 'default', 'stores'}
            or type(config.get('schema_version')) is not int or config['schema_version'] != 1
            or not isinstance(config.get('default'), dict) or not isinstance(config.get('stores'), dict)):
        raise ValueError('税率配置须包含 schema_version=1、default 规则及 stores 对象')
    default = normalize_policy(config['default'])
    stores = {}
    for name, rule in config['stores'].items():
        if not isinstance(name, str) or not name.strip() or name != name.strip() or not isinstance(rule, dict):
            raise ValueError('税率配置的店铺名须为无首尾空格的完整名称，店铺规则须为对象')
        stores[name] = normalize_policy(rule)
    return {'schema_version': 1, 'default': default, 'stores': stores}


def policy_for_store(config, store):
    if not isinstance(store, str) or not store.strip():
        raise ValueError('选择税率规则需要完整店铺名')
    return dict(config['stores'].get(store.strip(), config['default']))


def resolve_tax_rate_policy(store, config_path=None):
    return policy_for_store(load_tax_rate_config(config_path), store)
