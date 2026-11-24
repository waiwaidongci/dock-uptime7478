"""停机工单修订与月报重编的端到端集成测试（SQLite 内存库）。"""
import csv
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

ARTIFACT_DIR = tempfile.mkdtemp(prefix='artifacts-')
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['ARTIFACTS_DIR'] = ARTIFACT_DIR
os.environ['REDIS_URL'] = 'redis://localhost:6379/0'
os.environ['SEED_DEMO'] = 'false'

from sqlalchemy import select  # noqa: E402

from src.models.database import engine  # noqa: E402
from src.models.tables import (  # noqa: E402
    amendments, deliveries, report_versions, runs,
)
from src.services import amendments as amend_service  # noqa: E402
from src.services.data import PROFILE, amendment_records, save_revision  # noqa: E402
from src.services.domain import sample_amendments, sample_rows  # noqa: E402
from src.services.reissues import (  # noqa: E402
    claim_reissue, get_reissue, import_amendments, list_amendments, month_versions,
    process_reissue, retry_reissue, review_reissue, version_report,
)
from src.constants.enums import ReissueState  # noqa: E402
from src.schemas.requests import ReissueReview  # noqa: E402
from src.worker import write_artifact  # noqa: E402

failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  PASS {name}')
    else:
        failures.append((name, detail))
        print(f'  FAIL {name} {detail}')


def amendment_csv(rows):
    keys = [f['key'] for f in PROFILE['amendment_columns']]
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, '') for key in keys})
    return stream.getvalue()


def wait_ready(batch_id, expect_failed=False):
    for _ in range(10):
        batch = get_reissue(batch_id)
        if batch['state'] in (ReissueState.ready.value, ReissueState.failed.value):
            return batch
        claim = claim_reissue()
        if claim:
            process_reissue(claim, write_artifact)
        time.sleep(0.05)
    raise AssertionError('重编批次未在预期时间内结束')


# ---------- 1. 基础台账 ----------
base_rows = [{f['key']: row[f['key']] for f in PROFILE['columns']} for row in sample_rows()]
with engine.begin() as conn:
    from src.models.tables import metadata
    metadata.create_all(conn)
    base_id = save_revision('demo-base', base_rows, 'seed')[0]
print(f'基础台账 dataset={base_id}, {len(base_rows)} 行')

# ---------- 2. 修订解析与领域规则 ----------
parsed = amendment_records(amendment_csv(sample_amendments()))
check('修订 CSV 解析 4 行', len(parsed) == 4, str(parsed))
effective = amend_service.effective_amendments(parsed)
u1019 = effective[('D02', '非计划停机', 'U-1019-01')]
check('乱序以最高修订号为准', u1019['revision_no'] == 2 and u1019['start'].startswith('2026-11-02'))
u1014_03 = effective[('D01', '非计划停机', 'U-1014-03')]
check('撤销为终态（同号补送不恢复）', u1014_03['action'] == '撤销' and u1014_03['revision_no'] == 1)
new_rows = amend_service.apply_amendments(base_rows, parsed)
u1014_rows = [r for r in new_rows if r['record_id'] == 'U-1014-03']
check('撤销工单从有效记录剔除', not u1014_rows)
u1019_rows = [r for r in new_rows if r['record_id'] == 'U-1019-01']
check('改期工单替换区间', len(u1019_rows) == 1 and u1019_rows[0]['start'].startswith('2026-11-02'))
u1031_rows = [r for r in new_rows if r['record_id'] == 'U-1031-01']
check('跨月改期整体移入 11 月', u1031_rows[0]['start'].startswith('2026-11-01'))
touched = amend_service.touched_months([
    {**r, 'old_start': next((x['start'] for x in base_rows if (x['dock'], x['kind'], x['record_id']) ==
     (r['dock'], r['kind'], r['record_id'])), None),
     'old_end': next((x['end'] for x in base_rows if (x['dock'], x['kind'], x['record_id']) ==
     (r['dock'], r['kind'], r['record_id'])), None)} for r in parsed])
check('受影响月份含旧月 2026-10 和新月 2026-11', touched == ['2026-10', '2026-11'], str(touched))

# ---------- 3. 导入修订 → 重编批次 ----------
result = import_amendments(amendment_csv(sample_amendments()), 'analyst')
batch_id = result['id']
check('批次受影响月份为 10/11 两月', result['months'] == ['2026-10', '2026-11'], str(result['months']))
with engine.connect() as conn:
    check('修订流水落库 4 行', conn.execute(select(amendments)).all().__len__() == 4)

# 重复导入完全相同内容 → 拒绝（已登记或低修订号规则）
try:
    import_amendments(amendment_csv(sample_amendments()), 'analyst')
    check('重复修订被拒绝', False)
except ValueError as error:
    check('重复修订被拒绝', '没有需要重编' in str(error) or '更高修订号' in str(error) or '终态' in str(error),
          str(error))

# 终态撤销后补送低/同号改期 → 拒绝
try:
    import_amendments(amendment_csv([
        {'dock': 'D01', 'kind': '非计划停机', 'record_id': 'U-1014-03', 'revision_no': 1,
         'action': '改期', 'start': '2026-10-14 22:05:00', 'end': '2026-10-14 22:35:00'}]), 'analyst')
    check('撤销后旧单补送被拒绝', False)
except ValueError as error:
    check('撤销后旧单补送被拒绝', '终态' in str(error), str(error))

# 乱序旧修订号 → 拒绝
try:
    import_amendments(amendment_csv([
        {'dock': 'D02', 'kind': '非计划停机', 'record_id': 'U-1019-01', 'revision_no': 1,
         'action': '改期', 'start': '2026-10-19 11:00:00', 'end': '2026-10-19 14:00:00'}]), 'analyst')
    check('乱序低修订号被拒绝', False)
except ValueError as error:
    check('乱序低修订号被拒绝', '更高修订号' in str(error), str(error))

# ---------- 4. worker 处理重编 ----------
batch = wait_ready(batch_id)
check('批次进入待复核', batch['state'] == ReissueState.ready.value, batch['state'])
check('两个月都重算成功', all(i['state'] == ReissueState.completed.value for i in batch['months_detail']))
check('分配了新版本号', sorted(i['version_no'] for i in batch['months_detail']) == [1, 1])
with engine.connect() as conn:
    new_run_count = conn.execute(select(runs).where(runs.c.owner.like('reissue-%'))).all()
    check('生成两条重编分析任务', len(new_run_count) == 2, str(len(new_run_count)))
    check('尚无生效版本（未批准）', conn.execute(select(report_versions)).all() == [])

# ---------- 5. 退回与重复复核拦截 ----------
review_reissue(batch_id, ReissueReview(decision='returned', note='数据再核'), 'reviewer')
batch = get_reissue(batch_id)
check('退回后状态为 returned', batch['state'] == ReissueState.returned.value)
try:
    review_reissue(batch_id, ReissueReview(decision='approved', note='ok'), 'reviewer')
    check('已复核批次不能再次操作', False)
except Exception as error:
    check('已复核批次不能再次操作', '已完成复核' in str(error), str(error))

# ---------- 6. 第二批：模拟重编失败 + 重试不重复发布 ----------
failing_csv = [
    {'dock': 'D01', 'kind': '计划停机', 'record_id': 'P-1014-01', 'revision_no': 1,
     'action': '改期', 'start': '2026-10-14 15:00:00', 'end': '2026-10-14 15:30:00'},
]
second = import_amendments(amendment_csv(failing_csv), 'analyst')
second_id = second['id']
check('第二批只影响 10 月', second['months'] == ['2026-10'], str(second['months']))

# 让 compute 对 2026-10 抛错一次
import src.services.reissues as reissue_mod  # noqa: E402
real_compute = reissue_mod.compute
calls = {'n': 0}


def broken_compute(rows, options):
    calls['n'] += 1
    if options['month'] == '2026-10' and calls['n'] == 1:
        raise ValueError('模拟重编失败')
    return real_compute(rows, options)


reissue_mod.compute = broken_compute
batch2 = wait_ready(second_id)
check('失败批次保留 failed 状态', batch2['state'] == ReissueState.failed.value, batch2['state'])
check('失败月份记录错误信息', bool(batch2['months_detail'][0]['error']))
with engine.connect() as conn:
    check('失败时不生成任何版本', conn.execute(select(report_versions)).all() == [])

# 重试：只补失败月份
retry_reissue(second_id, 'analyst')
batch2 = wait_ready(second_id)
check('重试后进入待复核', batch2['state'] == ReissueState.ready.value)
check('重试只调用一次 compute（不重复成功月份）', calls['n'] == 2, str(calls))
reissue_mod.compute = real_compute

# ---------- 7. 批准第二批：原子切换 + 补发 + 旧版保留 ----------
# 先让第一批重新可批准：直接重置其复核状态（模拟复核员重新发起新批次的常规路径）
# 这里改为批准第二批（10 月首版），验证首版切换
review_reissue(second_id, ReissueReview(decision='approved', note='10 月首版切换'), 'reviewer')
with engine.connect() as conn:
    versions = conn.execute(select(report_versions).order_by(report_versions.c.id)).mappings().all()
    check('10 月生成 v1 当前版本', len(versions) == 1 and versions[0]['version_no'] == 1
          and versions[0]['current'] == 1 and versions[0]['month'] == '2026-10')
    check('首版无历史收件组则无补发', conn.execute(select(deliveries).where(
        deliveries.c.reissue_id.isnot(None))).all() == [])

# ---------- 8. 第三批：制造 10 月 v2，验证旧版可下载、补发去重 ----------
third_csv = [
    {'dock': 'D01', 'kind': '非计划停机', 'record_id': 'U-1014-01', 'revision_no': 1,
     'action': '改期', 'start': '2026-10-14 13:50:00', 'end': '2026-10-14 14:05:00'},
    # D02 无重叠的 25 分钟非计划停机改到班外，月份班内非计划秒应下降
    {'dock': 'D02', 'kind': '非计划停机', 'record_id': 'U-1016-01', 'revision_no': 1,
     'action': '改期', 'start': '2026-10-16 05:30:00', 'end': '2026-10-16 05:55:00'},
]
third = import_amendments(amendment_csv(third_csv), 'analyst')
third_id = third['id']
batch3 = wait_ready(third_id)
check('第三批待复核且带工单秒数差异', batch3['state'] == ReissueState.ready.value
      and bool(batch3['months_detail'][0]['diff']))
diff = batch3['months_detail'][0]['diff']
u101401_change = next(c for c in diff['changes'] if c['record_id'] == 'U-1014-01')
check('工单班内秒数按修订缩短', u101401_change['delta_in_shift_seconds'] < 0,
      json.dumps(u101401_change, ensure_ascii=False))
check('工单级停机秒数发生变化（并集口径下月份合计可能不变）',
      any(c['delta_in_shift_seconds'] != 0 or c['before_total_seconds'] != c['after_total_seconds']
          for c in diff['changes']))
check('月份级非计划停机秒发生变化',
      diff['monthly_before']['unplanned_seconds'] != diff['monthly_after']['unplanned_seconds']
      or diff['monthly_before']['outside_seconds'] != diff['monthly_after']['outside_seconds'])

# 模拟旧版已投递给两个收件组（直接插 deliveries，等价于历史计划投递）
v1_run = versions[0]['run_id']
with engine.begin() as conn:
    now = time.time()
    for recipient in ('仓配运营月报组', '仓配运营月报组', '冷链质控组'):
        conn.execute(deliveries.insert().values(schedule_id=None, reissue_id=None, run_id=v1_run,
                     month='2026-10', recipient=recipient, scheduled=now, delivered=now))

review_reissue(third_id, ReissueReview(decision='approved', note='10 月 v2 切换'), 'reviewer')
with engine.connect() as conn:
    rows = conn.execute(select(report_versions).where(report_versions.c.month == '2026-10')
                        .order_by(report_versions.c.version_no)).mappings().all()
    check('10 月存在 v1/v2 两版', [r['version_no'] for r in rows] == [1, 2])
    check('v1 置为历史、v2 当前生效', rows[0]['current'] == 0 and rows[1]['current'] == 1)
    check('v2 快照含历史收件组', set(rows[1]['recipient_snapshot']) == {'仓配运营月报组', '冷链质控组'})
    redeliveries = conn.execute(select(deliveries).where(deliveries.c.reissue_id == third_id)).mappings().all()
    check('按历史收件组补发且去重（2 份）', len(redeliveries) == 2
          and {r['recipient'] for r in redeliveries} == {'仓配运营月报组', '冷链质控组'},
          str(len(redeliveries)))

# 重复批准尝试不重复发布
try:
    review_reissue(third_id, ReissueReview(decision='approved', note='again'), 'reviewer')
    check('重复批准被拦截', False)
except Exception as error:
    check('重复批准被拦截', '已完成复核' in str(error), str(error))
with engine.connect() as conn:
    redeliveries = conn.execute(select(deliveries).where(deliveries.c.reissue_id == third_id)).all()
    check('补发份数不增加', len(redeliveries) == 2)

# ---------- 9. 旧版仍可下载 ----------
v1_id = rows[0]['id']
version, run = version_report(v1_id)
check('旧版元数据可查', version['version_no'] == 1 and version['current'] == 0)
path = os.path.join(ARTIFACT_DIR, f"run-{run['id']}-{run['started'].hex()}.csv")
check('旧版 CSV artifact 存在', os.path.exists(path), path)

payload = month_versions('2026-10')
check('月份版本接口返回当前版与差异', payload['current_version']['version_no'] == 2 and bool(payload['diff']))

# ---------- 10. 修订流水标记 ----------
listing = list_amendments()
check('流水含生效/覆盖标记', all('is_effective' in row for row in listing))
check('被覆盖的低修订号标记为非生效',
      next(r for r in listing if r['record_id'] == 'U-1019-01' and r['revision_no'] == 1)['is_effective'] is False)
check('终态撤销标记',
      next(r for r in listing if r['record_id'] == 'U-1014-03')['terminal_cancel'] is True)

print()
if failures:
    print(f'{len(failures)} 项失败：')
    for name, detail in failures:
        print(' -', name, detail)
    sys.exit(1)
print('全部集成测试通过。')
