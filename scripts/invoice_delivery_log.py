"""Render a deterministic human-readable log from the safe delivery report.

The caller verifies business files and supplies grouped, sanitized exception
reasons. This module does not access files, browser state, credentials or clocks.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re


_FINISHED = {'complete', 'no_applications', 'all_excluded', 'all_blocked', 'plan_only'}
_STATUS = {
    'complete': '处理完成',
    'no_applications': '本次查询及筛选范围无待处理申请',
    'all_excluded': '负数申请按规则全部排除',
    'all_blocked': '全部申请暂缓，未生成税局模板',
    'plan_only': '仅完成计划，未生成税局模板',
    'failed': '执行失败',
    'not_executed': '未执行',
    'pending': '未执行',
    'partial': '部分完成',
    'stopped': '已停止',
    'interrupted': '已中断',
}


def _text(value, fallback='未确认'):
    if not isinstance(value, str):
        return fallback
    return re.sub(r'[\s\x00-\x1f\x7f]+', ' ', value).strip() or fallback


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _money(value):
    if value is None:
        return '未确认'
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + abs(value.adjusted()) + 4)
        return format(value.quantize(Decimal('0.01')), '.2f') + ' 元'


def _metric(row, count_key, amount_key=None):
    count = _count(row.get(count_key))
    value = f'{count} 笔' if count is not None else '数量未确认'
    if amount_key is not None:
        value += '，金额 ' + _money(_amount(row.get(amount_key)))
    return value


def _total(rows, key, amount=False):
    if not rows:
        return '未确认（无已完成店铺）'
    values = [(_amount if amount else _count)(row.get(key)) for row in rows]
    missing = sum(value is None for value in values)
    known = [value for value in values if value is not None]
    total = sum(known, Decimal('0') if amount else 0)
    formatted = _money(total) if amount else f'{total} 笔'
    if missing:
        detail = f'已知 {formatted}；' if known else ''
        return f'未确认（{detail}{missing} 家未确认）'
    return formatted


def _generated(row):
    count = _count(row.get('ready_count'))
    return row.get('status') == 'complete' and count is not None and count > 0


def _status(row):
    return _STATUS.get(row.get('status'), '未完成，结果未确认')


def _reason(row):
    status = row.get('status')
    if status == 'failed':
        return _text(row.get('failure_reason'), '任务执行失败，未完成资料核验，未生成最终税局模板')
    if status not in _FINISHED:
        return '本店未完成执行，数量与金额尚未确认'
    if status == 'complete':
        blocked = _count(row.get('blocked_count'))
        if blocked is None:
            return '暂缓数量尚未确认，请核对该店目录的 exceptions.csv'
        if blocked:
            return '部分申请暂缓，具体原因见下方明细及该店目录的 exceptions.csv'
        return '已通过校验的申请已写入税局模板；尚未提交实际开票'
    return _status(row)


def render_summary_log(report: dict) -> bytes:
    """Return UTF-8 BOM text without adding timestamps or private report fields."""
    rows = report.get('shops', []) if report.get('type') == 'batch' else [report]
    finished = [row for row in rows if row.get('status') in _FINISHED]
    failed = [row for row in rows if row.get('status') == 'failed']
    pending = [row for row in rows if row.get('status') not in _FINISHED | {'failed'}]
    generated = [row for row in rows if _generated(row)]
    not_generated = [row for row in rows if not _generated(row)]
    ready_label = '可生成（含仅计划结果）' if any(row.get('status') == 'plan_only' for row in finished) else '已生成税局模板'
    scope = report.get('query_scope') or {}
    if scope.get('mode') == 'date':
        scope_line = '申请日期：' + _text(scope.get('date'))
    else:
        scope_line = '查询范围：' + _text(scope.get('start_date')) + ' 至 ' + _text(scope.get('end_date'))
    lines = [
        '千牛平台开票汇总日志',
        '说明：本次仅生成税局模板，尚未提交实际开票；可生成数量不代表已开票数量。',
        scope_line,
        '开票倒计时：' + ('已开始' if scope.get('countdown') == 'started' else '按本任务范围'),
        ('批次状态：' if report.get('type') == 'batch' else '任务状态：') + _status(report),
        f'店铺执行：共 {len(rows)} 家；已完成 {len(finished)} 家；失败 {len(failed)} 家；未执行或未完成 {len(pending)} 家。',
        '',
        f'已生成税局模板的店铺（{len(generated)} 家）：',
    ]
    lines += ['  ' + _text(row.get('store')) for row in generated] or ['  无']
    lines += [f'未生成税局模板的店铺（{len(not_generated)} 家）：']
    lines += ['  ' + _text(row.get('store')) + '：' + _status(row) for row in not_generated] or ['  无']
    lines += [
        '',
        f'已完成店铺数据合计（{len(finished)} 家，不含失败或未执行店铺）：',
        '  入选申请：' + _total(finished, 'selected_count'),
        '  ' + ready_label + '：' + _total(finished, 'ready_count') + '，金额 ' + _total(finished, 'ready_amount', True),
        '  暂缓：' + _total(finished, 'blocked_count') + '，金额 ' + _total(finished, 'blocked_amount', True),
        '  负数排除：' + _total(finished, 'excluded_count') + '，金额 ' + _total(finished, 'excluded_amount', True),
        '',
        '逐店结果：',
    ]
    for index, row in enumerate(rows, 1):
        lines += [f'{index}. {_text(row.get("store"))}', '  状态：' + _status(row)]
        if _generated(row):
            lines.append(f'  税局模板：已生成，包含 {row["ready_count"]} 笔申请（未提交实际开票）')
        elif row.get('status') in {'no_applications', 'all_blocked', 'all_excluded', 'plan_only'}:
            lines.append('  税局模板：未生成')
        else:
            lines.append('  税局模板：结果未确认')
        # A partial/failed run may carry provisional counters. They must never
        # masquerade as completed financial results.
        metrics = row if row.get('status') in _FINISHED else {}
        lines += [
            '  入选申请：' + _metric(metrics, 'selected_count'),
            '  可生成：' + _metric(metrics, 'ready_count', 'ready_amount'),
            '  暂缓：' + _metric(metrics, 'blocked_count', 'blocked_amount'),
            '  负数排除：' + _metric(metrics, 'excluded_count', 'excluded_amount'),
            '  处理说明：' + _reason(row),
        ]
        reasons = row.get('exception_reasons') or []
        if reasons:
            lines.append('  未生成、暂缓或排除原因（每笔按原始完整原因组计数）：')
            for reason in reasons:
                count = _count(reason.get('count'))
                count_text = f'{count} 笔' if count is not None else '数量未确认'
                lines.append('    ' + _text(reason.get('reason')) + '：' + count_text
                             + '，金额 ' + _money(_amount(reason.get('amount'))))
            lines.append('  逐笔明细：该店目录的 exceptions.csv。')
        elif row.get('status') in {'all_blocked', 'all_excluded'} or (
                row.get('status') == 'complete' and _count(row.get('blocked_count')) not in (None, 0)):
            lines.append('  具体原因：请核对该店目录的 exceptions.csv。')
        lines.append('')
    return ('\r\n'.join(lines).rstrip() + '\r\n').encode('utf-8-sig')
