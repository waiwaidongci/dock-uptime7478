import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction


DEFAULT_MONTH = '2026-10'
DEFAULT_TARGET = 99.0
SHIFT = '班次'
PLANNED = '计划停机'
UNPLANNED = '非计划停机'
KINDS = (SHIFT, PLANNED, UNPLANNED)
STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}')
MONTH = re.compile(r'[0-9]{4}-[0-9]{2}')


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


@dataclass
class Totals:
    scheduled: int = 0
    planned: int = 0
    unplanned: int = 0
    overlap: int = 0
    outside: int = 0

    @property
    def available(self) -> int:
        return self.scheduled - self.planned - self.unplanned

    @property
    def rate(self) -> float | None:
        if self.scheduled:
            return round(100 * self.available / self.scheduled, 4)
        return None

    def record(self, seconds: int, active: dict[str, set[str]]) -> str:
        if not active[SHIFT]:
            self.outside += seconds
            return '班外停机'

        self.scheduled += seconds
        if active[UNPLANNED]:
            self.unplanned += seconds
            if active[PLANNED]:
                self.overlap += seconds
            return UNPLANNED
        if active[PLANNED]:
            self.planned += seconds
            return PLANNED
        return '可用'

    def add(self, other: 'Totals') -> None:
        self.scheduled += other.scheduled
        self.planned += other.planned
        self.unplanned += other.unplanned
        self.overlap += other.overlap
        self.outside += other.outside

    def below(self, target: Fraction) -> bool:
        return self.scheduled > 0 and 100 * self.available < self.scheduled * target


def _report_options(options: dict) -> tuple[str, Interval, Fraction]:
    month = options.get('month', DEFAULT_MONTH)
    if isinstance(month, str) and MONTH.fullmatch(month):
        try:
            start = datetime.fromisoformat(f'{month}-01')
            next_year, next_month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
            period = Interval(start, datetime(next_year, next_month, 1))
        except ValueError:
            pass
        else:
            value = options.get('target_pct', DEFAULT_TARGET)
            try:
                target = Fraction(str(value))
            except (ValueError, ZeroDivisionError) as error:
                raise ValueError('请将可用率目标填写为 0 至 100 之间的有限数值。') from error
            if not 0 <= target <= 100:
                raise ValueError('请将可用率目标填写为 0 至 100 之间的有限数值。')
            return month, period, target
    raise ValueError('请按 YYYY-MM 填写有效报表月份，例如 2026-10。')


def _parse_intervals(rows: list[dict], period: Interval) -> dict[str, dict[tuple[str, str], Interval]]:
    seen: dict[tuple[str, str, str], Interval] = {}
    by_dock: dict[str, dict[tuple[str, str], Interval]] = defaultdict(dict)
    for line, row in enumerate(rows, start=2):
        values = [row.get(key) for key in ('dock', 'kind', 'record_id', 'start', 'end')]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f'CSV 第 {line} 行字段缺失，请补齐月台、记录类型、记录编号、开始和结束时间。')
        dock, kind, record_id, start_text, end_text = (value.strip() for value in values)
        if kind not in KINDS:
            raise ValueError(f'CSV 第 {line} 行记录类型无效，请填写班次、计划停机或非计划停机。')
        try:
            if not STAMP.fullmatch(start_text) or not STAMP.fullmatch(end_text):
                raise ValueError
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
        except ValueError as error:
            raise ValueError(f'CSV 第 {line} 行时间无效，请按 YYYY-MM-DD HH:MM:SS 填写仓库当地时间。') from error
        if end <= start:
            raise ValueError(f'CSV 第 {line} 行请将结束时间改为晚于开始时间，跨夜记录请填写次日日期。')
        key = (dock, kind, record_id)
        interval = Interval(start, end)
        if key in seen:
            if interval != seen[key]:
                raise ValueError(f'CSV 第 {line} 行记录编号 {record_id} 对应多个区间，请核对起止时间或更正编号。')
            continue
        seen[key] = interval
        clipped = Interval(max(start, period.start), min(end, period.end))
        if clipped.start < clipped.end:
            by_dock[dock][kind, record_id] = clipped
    return by_dock


def _row(level: str, dock: str, category: str, interval: Interval, totals: Totals, sources: str = '') -> dict:
    return {
        '层级': level,
        '月台': dock,
        '归类或复核状态': category,
        '开始（含）': interval.start.isoformat(sep=' ', timespec='seconds'),
        '结束（不含）': interval.end.isoformat(sep=' ', timespec='seconds'),
        '排班秒': totals.scheduled,
        '计划停机秒': totals.planned,
        '非计划停机秒': totals.unplanned,
        '班内重叠秒': totals.overlap,
        '班外停机秒': totals.outside,
        '可用秒': totals.available,
        '可用率（%）': totals.rate,
        '来源编号': sources,
    }


def _timeline(dock: str, intervals: dict[tuple[str, str], Interval]) -> tuple[Totals, list[dict]]:
    changes = defaultdict(list)
    for (kind, record_id), interval in intervals.items():
        changes[interval.start].append((kind, record_id, True))
        changes[interval.end].append((kind, record_id, False))

    active: dict[str, set[str]] = {kind: set() for kind in KINDS}
    totals = Totals()
    detail = []
    boundaries = sorted(changes)
    for start, end in zip(boundaries, boundaries[1:]):
        for kind, record_id, begins in changes[start]:
            if begins:
                active[kind].add(record_id)
            else:
                active[kind].remove(record_id)
        if not any(active.values()):
            continue
        duration = end - start
        seconds = duration.days * 86400 + duration.seconds
        segment = Totals()
        category = segment.record(seconds, active)
        totals.add(segment)
        sources = json.dumps({kind: sorted(ids) for kind, ids in active.items() if ids}, ensure_ascii=False)
        detail.append(_row('区间明细', dock, category, Interval(start, end), segment, sources))
    return totals, detail


def compute(rows: list[dict], options: dict) -> dict:
    month, period, target = _report_options(options)
    if not rows:
        raise ValueError('请导入至少一条月台排班记录，有停机时请同时导入停机台账。')
    intervals = _parse_intervals(rows, period)
    monthly = Totals()
    dock_rows = []
    detail_rows = []
    chart_rates = []
    below_target = 0
    unscheduled = 0
    for dock in sorted(intervals):
        totals, detail = _timeline(dock, intervals[dock])
        monthly.add(totals)
        if totals.scheduled == 0:
            unscheduled += 1
            status = '本月无排班'
        elif totals.below(target):
            below_target += 1
            status = '低于目标'
        else:
            status = '达标'
        dock_rows.append(_row('月台小计', dock, status, period, totals))
        detail_rows.extend(detail)
        if totals.scheduled:
            chart_rates.append((Fraction(totals.available, totals.scheduled), dock, totals.rate))

    if monthly.scheduled == 0:
        raise ValueError('所选月份没有可用于计算的排班，请补齐覆盖该月的班次或改选月份。')
    headline = f'{month} 月台排班加权可用率 {monthly.rate:.4f}%，{below_target} 个排班月台低于目标'
    if unscheduled:
        headline += f'，{unscheduled} 个月台仅有班外停机'
    monthly_row = _row('全仓汇总', '全仓', '低于目标' if monthly.below(target) else '达标', period, monthly)
    return {
        'headline': headline,
        'metrics': {
            '排班加权可用率（%）': monthly.rate,
            '排班秒': monthly.scheduled,
            '计划停机秒': monthly.planned,
            '非计划停机秒': monthly.unplanned,
            '班内重叠秒': monthly.overlap,
            '班外停机秒': monthly.outside,
            '可用秒': monthly.available,
            '排班月台数': len(dock_rows) - unscheduled,
            '低于目标的月台数': below_target,
            '仅有班外停机的月台数': unscheduled,
        },
        'rows': [monthly_row, *dock_rows, *detail_rows],
        'notes': [
            '时间使用仓库本地时间（UTC+08:00），区间含起点、不含终点，跨月记录按自然月裁切。',
            '同月台重叠班次先取并集。计划与非计划停机各自取并集，再与排班求交。班内重叠秒已计入非计划停机秒。',
            '班外停机按同月台两类停机的并集统计。无排班月台的可用率留空，班外秒数保留在明细和全仓统计中。',
            '全仓可用率 = 各月台可用秒合计 ÷ 各月台排班秒合计，计划和非计划停机均扣减可用秒。',
            f'本期目标为 {float(target):g}%，按舍入前的可用率判断达标。明细按来源边界分段，可在导出的 CSV 中按月台和记录编号筛查原始台账。',
        ],
        'chart': {
            'label': '排班月台可用率（由低到高）',
            'unit': '%',
            'points': [{'label': dock, 'value': rate} for _, dock, rate in sorted(chart_rates)],
        },
        'can_publish': True,
    }


def sample_rows() -> list[dict]:
    rows = []

    def add(dock: str, kind: str, record_id: str, start: datetime, end: datetime) -> None:
        rows.append({'dock': dock, 'kind': kind, 'record_id': record_id,
                     'start': start.isoformat(sep=' '), 'end': end.isoformat(sep=' ')})

    for day_number in range(1, 32):
        day = date(2026, 10, day_number)
        if day.weekday() >= 5:
            continue
        for dock, suffix, start, end in (
            ('D01', 'A', time(6), time(14)),
            ('D01', 'B', time(13, 45), time(22)),
            ('D02', 'A', time(7), time(15)),
            ('D03', 'A', time(12), time(20)),
        ):
            add(dock, SHIFT, f'S-{day:%m%d}-{dock}-{suffix}', datetime.combine(day, start), datetime.combine(day, end))

    for dock, kind, record_id, start, end in (
        ('D03', SHIFT, 'S-0930-D03-X', '2026-09-30 22:00:00', '2026-10-01 04:00:00'),
        ('D03', SHIFT, 'S-1031-D03-X', '2026-10-31 22:00:00', '2026-11-01 02:00:00'),
        ('D03', PLANNED, 'P-0930-01', '2026-09-30 23:50:00', '2026-10-01 00:20:00'),
        ('D01', PLANNED, 'P-1014-01', '2026-10-14 13:30:00', '2026-10-14 14:30:00'),
        ('D01', UNPLANNED, 'U-1014-01', '2026-10-14 13:50:00', '2026-10-14 14:10:00'),
        ('D01', UNPLANNED, 'U-1014-02', '2026-10-14 13:55:00', '2026-10-14 14:25:00'),
        ('D01', UNPLANNED, 'U-1014-03', '2026-10-14 22:00:00', '2026-10-14 22:40:00'),
        ('D02', UNPLANNED, 'U-1016-01', '2026-10-16 06:50:00', '2026-10-16 07:15:00'),
        ('D02', UNPLANNED, 'U-1019-01', '2026-10-19 10:00:00', '2026-10-19 13:00:00'),
        ('D02', PLANNED, 'P-1019-01', '2026-10-19 10:30:00', '2026-10-19 11:00:00'),
        ('D03', UNPLANNED, 'U-1031-01', '2026-10-31 23:45:00', '2026-11-01 00:15:00'),
    ):
        add(dock, kind, record_id, datetime.fromisoformat(start), datetime.fromisoformat(end))
    return rows


def sample_amendments() -> list[dict]:
    """月报寄出后维修班补报的修订样例，覆盖改期、撤销、跨月与乱序规则。

    撤销终态与「旧单补送不能恢复」的演示见修订页的二次导入提示：
    对已撤销的 U-1014-03 再提交同号或更低号的改期会被系统拒收。
    """
    return [
        # D02 的 3 小时非计划停机改到 11 月 2 日：10 月与 11 月都受影响
        {'dock': 'D02', 'kind': UNPLANNED, 'record_id': 'U-1019-01', 'revision_no': 2,
         'action': '改期', 'start': '2026-11-02 09:30:00', 'end': '2026-11-02 12:30:00'},
        # 同单号修订号 1 乱序晚到，低于修订号 2，应被忽略
        {'dock': 'D02', 'kind': UNPLANNED, 'record_id': 'U-1019-01', 'revision_no': 1,
         'action': '改期', 'start': '2026-10-19 11:00:00', 'end': '2026-10-19 14:00:00'},
        # D01 班外停机撤销，后续若补送同号改期将被拒收（撤销为终态）
        {'dock': 'D01', 'kind': UNPLANNED, 'record_id': 'U-1014-03', 'revision_no': 1,
         'action': '撤销', 'start': '', 'end': ''},
        # D03 跨月非计划停机改期，整体移入 11 月，10 月须重算扣减
        {'dock': 'D03', 'kind': UNPLANNED, 'record_id': 'U-1031-01', 'revision_no': 3,
         'action': '改期', 'start': '2026-11-01 09:00:00', 'end': '2026-11-01 09:40:00'},
    ]
