"""停机工单修订领域逻辑。

修订按（月台 dock、停机类型 kind、单号 record_id）识别，乱序到达时以最高修订号为准；
「撤销」为终态，撤销后旧单补送（更低或相同修订号）不能恢复。
"""
import json
from collections import defaultdict
from datetime import datetime

from src.constants.enums import AmendmentAction
from src.services.domain import (
    PLANNED,
    Interval,
    Totals,
    _parse_intervals,
    _timeline,
)

DOWNTIME_KINDS = (PLANNED, '非计划停机')


def parse_stamp(text: str, label: str) -> datetime:
    from src.services.domain import STAMP

    if not isinstance(text, str) or not STAMP.fullmatch(text.strip()):
        raise ValueError(f'{label}时间无效，请按 YYYY-MM-DD HH:MM:SS 填写仓库当地时间。')
    return datetime.fromisoformat(text.strip())


def months_between(start: datetime, end: datetime) -> list[str]:
    """半开区间触及的全部自然月（YYYY-MM），含跨月区间两端。"""
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f'{year:04d}-{month:02d}')
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def effective_amendments(entries: list[dict]) -> dict[tuple[str, str, str], dict]:
    """从修订流水里取每个业务键的最高修订号记录（撤销同样可以是最高号）。"""
    chosen: dict[tuple[str, str, str], dict] = {}
    for entry in entries:
        key = (entry['dock'], entry['kind'], entry['record_id'])
        current = chosen.get(key)
        if current is None or entry['revision_no'] > current['revision_no']:
            chosen[key] = entry
    return chosen


def apply_amendments(rows: list[dict], entries: list[dict]) -> list[dict]:
    """把当前生效修订叠加到原始台账，返回规范化后的有效记录列表。

    改期：替换同键原始区间；撤销：剔除同键原始记录。修订指向台账中尚不存在的单号时，
    改期记录会作为新区间进入，撤销则无记录可删。
    """
    chosen = effective_amendments(entries)
    result: list[dict] = []
    replaced = set()
    for row in rows:
        key = (row['dock'], row['kind'], row['record_id'])
        entry = chosen.get(key)
        if entry is None:
            result.append(dict(row))
            continue
        if entry['action'] == AmendmentAction.cancel.value:
            continue
        result.append({
            'dock': row['dock'], 'kind': row['kind'], 'record_id': row['record_id'],
            'start': entry['start'], 'end': entry['end'],
        })
        replaced.add(key)
    for key, entry in chosen.items():
        if key in replaced or entry['action'] == AmendmentAction.cancel.value:
            continue
        result.append({
            'dock': key[0], 'kind': key[1], 'record_id': key[2],
            'start': entry['start'], 'end': entry['end'],
        })
    return result


def touched_months(entries: list[dict]) -> list[str]:
    """一批修订在「生效前后」可能改变的月份：旧区间月份 + 新区间月份。

    撤销记录没有新时间，只回退到其原始区间所在月；改期移出旧月时旧月同样要重算。
    """
    months: set[str] = set()
    for entry in entries:
        if entry['action'] == AmendmentAction.reschedule.value:
            start = parse_stamp(entry['start'], '改期开始')
            end = parse_stamp(entry['end'], '改期结束')
            if end <= start:
                raise ValueError(f'单号 {entry["record_id"]} 的改期结束时间须晚于开始时间。')
            months.update(months_between(start, end))
        for old_text in (entry.get('old_start'), entry.get('old_end')):
            if old_text:
                months.add(old_text[:7])
    return sorted(months)


def downtime_by_record(rows: list[dict], month: str) -> tuple[dict[tuple, dict], Totals]:
    """按月裁切后，逐（月台、停机类型、单号）统计停机秒数。

    in_shift_seconds 为该工单处于活跃状态的班内秒数（计划/非计划重叠归非计划口径由
    时间线分段保证，重叠时段每个涉及工单各自记一份，用于追溯）；
    total_seconds 为该工单在月内活跃的全部秒数（含班外）。
    """
    period_start = datetime.fromisoformat(f'{month}-01')
    next_year, next_month = (period_start.year + 1, 1) if period_start.month == 12 \
        else (period_start.year, period_start.month + 1)
    period = Interval(period_start, datetime(next_year, next_month, 1))
    intervals = _parse_intervals(rows, period)
    summary: dict[tuple[str, str, str], dict] = {}
    monthly = Totals()
    for dock, dock_intervals in intervals.items():
        totals, detail = _timeline(dock, dock_intervals)
        monthly.add(totals)
        per_key = defaultdict(lambda: {'total_seconds': 0, 'in_shift_seconds': 0})
        for segment in detail:
            active = json.loads(segment['来源编号'])
            if not active:
                continue
            in_shift = segment['排班秒'] > 0
            segment_seconds = segment['排班秒'] + segment['班外停机秒']
            for kind in DOWNTIME_KINDS:
                for record_id in active.get(kind, []):
                    bucket = per_key[(dock, kind, record_id)]
                    bucket['total_seconds'] += segment_seconds
                    if in_shift:
                        bucket['in_shift_seconds'] += segment['排班秒']
        for key, bucket in per_key.items():
            summary[key] = bucket
    return summary, monthly


def compare_records(old_rows: list[dict], new_rows: list[dict], month: str,
                    tracked_keys: list[tuple[str, str, str]]) -> dict:
    """仓配核对用：按工单比较修订前后停机秒数，并附月份级合计。"""
    old_summary, old_monthly = downtime_by_record(old_rows, month)
    new_summary, new_monthly = downtime_by_record(new_rows, month)
    changes = []
    empty_bucket = {'total_seconds': 0, 'in_shift_seconds': 0}
    for key in sorted(set(tracked_keys) | set(old_summary) | set(new_summary)):
        before = old_summary.get(key, dict(empty_bucket))
        after = new_summary.get(key, dict(empty_bucket))
        if before == after:
            status = '无变化'
        elif after['in_shift_seconds'] == 0 and after['total_seconds'] == 0:
            status = '撤销/移出本月'
        elif before['in_shift_seconds'] == 0 and before['total_seconds'] == 0:
            status = '新入本月'
        else:
            status = '秒数变化'
        changes.append({
            'dock': key[0], 'kind': key[1], 'record_id': key[2], 'status': status,
            'before_in_shift_seconds': before['in_shift_seconds'],
            'after_in_shift_seconds': after['in_shift_seconds'],
            'delta_in_shift_seconds': after['in_shift_seconds'] - before['in_shift_seconds'],
            'before_total_seconds': before['total_seconds'],
            'after_total_seconds': after['total_seconds'],
        })
    return {
        'month': month,
        'changes': changes,
        'monthly_before': _monthly_metrics(old_monthly),
        'monthly_after': _monthly_metrics(new_monthly),
    }


def _monthly_metrics(totals: Totals) -> dict:
    return {
        'scheduled_seconds': totals.scheduled,
        'planned_seconds': totals.planned,
        'unplanned_seconds': totals.unplanned,
        'outside_seconds': totals.outside,
        'available_seconds': totals.available,
        'rate': totals.rate,
    }
