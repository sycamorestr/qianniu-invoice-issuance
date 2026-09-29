"""Explicit invoice query scope, with compatibility for dated checkpoints."""
from calendar import monthrange
from datetime import date as calendar_date, datetime, timedelta, timezone


def _two_months_before(day):
    month_index = day.year * 12 + day.month - 3
    year, month0 = divmod(month_index, 12)
    return calendar_date(year, month0 + 1, min(day.day, monthrange(year, month0 + 1)[1]))


def make_scope(date=None, all_pending=False, *, today=None, countdown=None):
    """Construct a scope; omitted countdown preserves a historical scope."""
    if countdown not in (None, 'started'):
        raise ValueError('开票倒计时筛选仅支持 started')
    filters = {'countdown': countdown} if countdown is not None else {}
    if all_pending:
        if date is not None:
            raise ValueError('--date 与 --all-pending 不能同时使用')
        end = today or datetime.now(timezone(timedelta(hours=8))).date()
        return {'mode': 'all_pending', 'start_date': _two_months_before(end).isoformat(),
                'end_date': end.isoformat(), **filters}
    if not isinstance(date, str) or not date:
        raise ValueError('必须提供 --date 或 --all-pending')
    if calendar_date.fromisoformat(date).isoformat() != date:
        raise ValueError('--date 必须是 YYYY-MM-DD')
    return {'mode': 'date', 'date': date, **filters}


def scope_from_record(record):
    scope = record.get('query_scope')
    if scope is None:
        return make_scope(record.get('date'))
    if not isinstance(scope, dict):
        raise ValueError('query_scope 必须是对象')
    if 'countdown' in scope and scope['countdown'] != 'started':
        raise ValueError('开票倒计时筛选仅支持 started')
    countdown = scope.get('countdown')
    if scope.get('mode') == 'all_pending':
        if record.get('date') is not None:
            raise ValueError('全部待处理模式的 date 必须为空')
        try:
            end = calendar_date.fromisoformat(scope['end_date'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('全部待处理缺少有效截止日期') from exc
        result = make_scope(all_pending=True, today=end, countdown=countdown)
        if scope != result:
            raise ValueError('全部待处理范围必须是截止日期及其前两个月')
        return result
    result = make_scope(record.get('date'), countdown=countdown)
    if scope != result:
        raise ValueError('query_scope 与 date 不一致')
    return result


def resolve_scope(date=None, all_pending=False, saved=None):
    """New runs select started countdowns; resumes inherit the exact old filter."""
    previous = scope_from_record(saved) if saved is not None else None
    if date is None and not all_pending and previous is not None:
        return previous
    if date is None and all_pending and previous is not None and previous['mode'] == 'all_pending':
        return previous
    scope = make_scope(date, all_pending,
                       countdown=previous.get('countdown') if previous is not None else 'started')
    if previous is not None and scope != previous:
        raise ValueError('查询范围与原任务不一致，不能在恢复时切换日期模式')
    return scope


def scope_label(scope):
    return 'all-pending' if scope['mode'] == 'all_pending' else scope['date']


def scope_fields(scope):
    # Keep legacy unfiltered dated request shapes and checkpoint hashes unchanged.
    return {'query_scope': dict(scope)} if scope['mode'] == 'all_pending' or 'countdown' in scope else {}


def scope_date_range(scope):
    if scope['mode'] == 'all_pending':
        return {'start': scope['start_date'], 'end': scope['end_date']}
    return {'start': scope['date'], 'end': scope['date']}
