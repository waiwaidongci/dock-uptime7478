"""月报重编批次：修订导入、受影响月份重算、批准后原子切换与版本化交付。"""
import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import AmendmentAction, ReissueState, State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import (
    amendments,
    datasets,
    deliveries,
    reissue_runs,
    reissues,
    report_versions,
    runs,
    schedules,
)
from src.services import amendments as amend_service
from src.services.data import PROFILE, amendment_records
from src.services.domain import compute

STALE_SECONDS = 300


def _base_dataset(connection):
    """重编基线：优先取最近一次重编快照，保证多批次修订持续叠加；否则取最新台账。"""
    snapshot = connection.execute(
        select(datasets).where(datasets.c.name.like('重编快照·%'))
        .order_by(datasets.c.id.desc()).limit(1)).mappings().one_or_none()
    if snapshot is not None:
        return snapshot
    row = connection.execute(select(datasets).order_by(datasets.c.id.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        raise HTTPException(409, '请先导入月台班表与停机台账，再登记工单修订。')
    return row


def _save_snapshot(connection, name, records, owner, now):
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    found = connection.execute(select(datasets.c.id).where(datasets.c.checksum == checksum)).scalar()
    if found is not None:
        return found
    return connection.execute(insert(datasets).values(
        name=name, checksum=checksum, records=records, owner=owner, created=now)).inserted_primary_key[0]


def import_amendments(text, actor):
    rows = amendment_records(text)
    now = time.time()
    with engine.begin() as connection:
        base = _base_dataset(connection)
        stored = [dict(row) for row in connection.execute(select(amendments)).mappings()]
        stored_at = {(r['dock'], r['kind'], r['record_id'], r['revision_no']): r for r in stored}
        effective = amend_service.effective_amendments(stored)
        accepted = []
        for row in rows:
            identity = (row['dock'], row['kind'], row['record_id'], row['revision_no'])
            latest = effective.get(identity[:3])
            if latest is not None and row['revision_no'] <= latest['revision_no']:
                if latest['action'] == AmendmentAction.cancel.value:
                    raise ValueError(f'单号 {row["record_id"]} 已在修订号 {latest["revision_no"]} 撤销，'
                                     '撤销为终态，旧单补送不能恢复。')
                raise ValueError(f'单号 {row["record_id"]} 已有更高修订号 {latest["revision_no"]}，'
                                 '乱序到达的旧修订不予采用。')
            if identity in stored_at:
                prior = stored_at[identity]
                if (prior['action'], prior['start'] or '', prior['end'] or '') == \
                        (row['action'], row['start'], row['end']):
                    continue
                raise ValueError(f'单号 {row["record_id"]} 的修订号 {row["revision_no"]} 已登记且内容不同，'
                                 '请使用更高修订号提交。')
            accepted.append(row)
        if not accepted:
            raise ValueError('修订均已登记，没有需要重编的新内容。')

        enriched = []
        stored_ids = []
        for row in accepted:
            old = next((item for item in base['records']
                        if (item['dock'], item['kind'], item['record_id']) ==
                        (row['dock'], row['kind'], row['record_id'])), None)
            enriched.append({**row, 'old_start': old['start'] if old else None,
                             'old_end': old['end'] if old else None})
            key = connection.execute(insert(amendments).values(
                dock=row['dock'], kind=row['kind'], record_id=row['record_id'],
                revision_no=row['revision_no'], action=row['action'],
                start=row['start'] or None, end=row['end'] or None,
                applied=1, owner=actor, created=now)).inserted_primary_key[0]
            stored_ids.append(key)
            add_event(connection, actor, 'amendment', key, now)

        months = amend_service.touched_months(enriched)
        if not months:
            raise ValueError('本批修订未影响任何月报月份，无需重编。')
        all_entries = [dict(item) for item in
                       connection.execute(select(amendments).where(amendments.c.applied == 1)).mappings()]
        snapshot_rows = amend_service.apply_amendments(base['records'], all_entries)
        target_pct = float(PROFILE['options'][1]['default'])
        snapshot_id = _save_snapshot(
            connection, f'重编快照·{time.strftime("%Y%m%d%H%M%S", time.localtime(now))}',
            snapshot_rows, actor, now)
        batch_id = connection.execute(insert(reissues).values(
            state=ReissueState.queued.value, months=months, amendment_ids=stored_ids,
            snapshot={'dataset_id': snapshot_id, 'base_dataset_id': base['id']},
            target_pct=target_pct, owner=actor, created=now)).inserted_primary_key[0]
        for month in months:
            connection.execute(insert(reissue_runs).values(
                reissue_id=batch_id, month=month, run_id=None, state=ReissueState.queued.value,
                version_no=0, diff=None, error=None))
        add_event(connection, actor, 'reissue', batch_id, now)
    return {'id': batch_id, 'months': months, 'amendments': stored_ids}


def claim_reissue():
    now = time.time()
    claimable = or_(reissues.c.state == ReissueState.queued.value,
                    and_(reissues.c.state == ReissueState.running.value,
                         reissues.c.started < now - STALE_SECONDS))
    with engine.begin() as connection:
        batch = connection.execute(select(reissues).where(claimable)
                                   .order_by(reissues.c.id).limit(1)).mappings().one_or_none()
        if batch is None:
            return None
        won = connection.execute(update(reissues).where(reissues.c.id == batch['id'], claimable)
                                 .values(state=ReissueState.running.value, started=now))
        if won.rowcount != 1:
            return None
        return dict(batch)


def _next_version(connection, month):
    current_max = connection.execute(select(report_versions.c.version_no)
                                     .where(report_versions.c.month == month)
                                     .order_by(report_versions.c.version_no.desc()).limit(1)).scalar() or 0
    return current_max + 1


def _current_version(connection, month):
    return connection.execute(select(report_versions).where(report_versions.c.month == month,
                                                            report_versions.c.current == 1))\
        .mappings().one_or_none()


def _recipient_history(connection, month):
    """月份历史收件组：已切换版本的快照 + 旧流程该月投递记录兜底。"""
    recipients = set()
    for row in connection.execute(select(report_versions.c.recipient_snapshot)
                                  .where(report_versions.c.month == month)).all():
        recipients.update(row[0] or [])
    month_run_ids = [row[0] for row in connection.execute(
        select(runs.c.id, runs.c.options).where(runs.c.state == State.completed.value)).all()
        if (row[1] or {}).get('month') == month]
    if month_run_ids:
        for row in connection.execute(select(deliveries.c.recipient)
                                      .where(deliveries.c.run_id.in_(month_run_ids))).all():
            recipients.add(row[0])
    return sorted(recipients)


def process_reissue(batch, write_artifact):
    batch_id = batch['id']
    now = time.time()
    with engine.begin() as connection:
        items = connection.execute(select(reissue_runs).where(reissue_runs.c.reissue_id == batch_id)
                                   .order_by(reissue_runs.c.month)).mappings().all()
        snapshot = fetch_one(connection, datasets, batch['snapshot']['dataset_id'])
        target_pct = batch['target_pct']
        errors = []
        for item in items:
            if item['state'] == ReissueState.completed.value and item['run_id']:
                continue
            month = item['month']
            try:
                report = compute(snapshot['records'], {'month': month, 'target_pct': target_pct})
                report['columns'] = list(dict.fromkeys(key for row in report['rows'] for key in row))
                report['metric_order'] = list(report['metrics'])
                run_id = connection.execute(insert(runs).values(
                    dataset_id=snapshot['id'], options={'month': month, 'target_pct': target_pct},
                    state=State.completed.value, owner=f'reissue-{batch_id}', created=now,
                    started=now, finished=now, result=report, error=None)).inserted_primary_key[0]
                write_artifact(run_id, now, report)
                baseline = fetch_one(connection, datasets, batch['snapshot']['base_dataset_id'])
                tracked = [(a['dock'], a['kind'], a['record_id']) for a in
                           connection.execute(select(amendments).where(
                               amendments.c.id.in_(batch['amendment_ids']))).mappings()]
                diff = amend_service.compare_records(baseline['records'], snapshot['records'],
                                                      month, tracked)
                version_no = _next_version(connection, month)
                connection.execute(update(reissue_runs).where(reissue_runs.c.id == item['id'])
                                   .values(run_id=run_id, state=ReissueState.completed.value,
                                           version_no=version_no, diff=diff, error=None))
            except ValueError as error:
                errors.append(f'{month}：{error}')
                connection.execute(update(reissue_runs).where(reissue_runs.c.id == item['id'])
                                   .values(state=ReissueState.failed.value, error=str(error)))
            except Exception:  # pragma: no cover - 防御性兜底
                errors.append(f'{month}：计算失败，请核对数据后重试。')
                connection.execute(update(reissue_runs).where(reissue_runs.c.id == item['id'])
                                   .values(state=ReissueState.failed.value,
                                           error='计算失败，请核对数据后重试。'))
        final_state = ReissueState.failed.value if errors else ReissueState.ready.value
        connection.execute(update(reissues).where(
            reissues.c.id == batch_id, reissues.c.state == ReissueState.running.value)
            .values(state=final_state, error='\n'.join(errors) or None, finished=time.time()))
        add_event(connection, 'worker', final_state, batch_id, time.time())


def list_reissues():
    with engine.connect() as connection:
        batches = [dict(row) for row in
                   connection.execute(select(reissues).order_by(reissues.c.id.desc()).limit(100)).mappings()]
        for batch in batches:
            batch['months_detail'] = [dict(row) for row in connection.execute(
                select(reissue_runs).where(reissue_runs.c.reissue_id == batch['id'])
                .order_by(reissue_runs.c.month)).mappings()]
        return batches


def get_reissue(key):
    with engine.connect() as connection:
        batch = fetch_one(connection, reissues, key)
        if batch is None:
            raise HTTPException(404, '重编批次不存在。')
        batch = dict(batch)
        batch['months_detail'] = [dict(row) for row in connection.execute(
            select(reissue_runs).where(reissue_runs.c.reissue_id == key)
            .order_by(reissue_runs.c.month)).mappings()]
        batch['amendments_detail'] = [dict(row) for row in connection.execute(
            select(amendments).where(amendments.c.id.in_(batch['amendment_ids']))
            .order_by(amendments.c.dock, amendments.c.kind, amendments.c.record_id,
                      amendments.c.revision_no)).mappings()]
        return batch


def retry_reissue(key, actor):
    now = time.time()
    with engine.begin() as connection:
        batch = fetch_one(connection, reissues, key)
        if batch is None:
            raise HTTPException(404, '重编批次不存在。')
        if batch['state'] != ReissueState.failed.value:
            raise HTTPException(409, '仅失败的重编批次可以重试；已完成的月份不会重复计算或发布。')
        connection.execute(update(reissues).where(reissues.c.id == key)
                           .values(state=ReissueState.queued.value, error=None, started=None))
        connection.execute(update(reissue_runs).where(reissue_runs.c.reissue_id == key,
                                                      reissue_runs.c.state == ReissueState.failed.value)
                           .values(state=ReissueState.queued.value, error=None))
        add_event(connection, actor, 'retry', key, now)
    return {'id': key, 'state': ReissueState.queued.value}


def review_reissue(key, body, actor):
    now = time.time()
    note = body.note.strip()
    if len(note) < 2:
        raise ValueError('请输入至少两个字符的复核意见。')
    with engine.begin() as connection:
        batch = fetch_one(connection, reissues, key)
        if batch is None:
            raise HTTPException(404, '重编批次不存在。')
        if body.decision == 'returned':
            if batch['decision'] is not None:
                raise HTTPException(409, '该批次已完成复核。')
            if batch['state'] not in (ReissueState.ready.value, ReissueState.failed.value):
                raise HTTPException(409, '重编批次完成后才能退回。')
            connection.execute(update(reissues).where(reissues.c.id == key)
                .values(decision='returned', state=ReissueState.returned.value,
                        reviewer=actor, review_note=note, reviewed=now))
            add_event(connection, actor, 'returned', key, now)
            return {'decision': 'returned'}
        if batch['decision'] is not None:
            raise HTTPException(409, '该批次已完成复核。')
        if batch['state'] != ReissueState.ready.value:
            raise HTTPException(409, '全部受影响月份重算成功后才能批准切换；失败月份请先重试。')
        items = connection.execute(select(reissue_runs).where(reissue_runs.c.reissue_id == key)
                                   .order_by(reissue_runs.c.month)).mappings().all()
        # 一次性原子切换：旧版本 current 置 0、新版本置 1，并向历史收件组补发（去重）。
        for item in items:
            month = item['month']
            recipients = _recipient_history(connection, month)
            previous = _current_version(connection, month)
            if previous is not None:
                connection.execute(update(report_versions)
                                   .where(report_versions.c.id == previous['id'])
                                   .values(current=0))
                connection.execute(update(schedules).where(schedules.c.run_id == previous['run_id'],
                                                           schedules.c.active == 1)
                                   .values(run_id=item['run_id']))
            connection.execute(insert(report_versions).values(
                month=month, version_no=item['version_no'], run_id=item['run_id'], reissue_id=key,
                recipient_snapshot=recipients, current=1, created=now))
            for recipient in recipients:
                try:
                    with connection.begin_nested():
                        delivery_id = connection.execute(insert(deliveries).values(
                            schedule_id=None, reissue_id=key, run_id=item['run_id'], month=month,
                            recipient=recipient, scheduled=now, delivered=now)).inserted_primary_key[0]
                        add_event(connection, 'worker', 'delivered', delivery_id, now)
                except IntegrityError:
                    # 同批次同月份同收件组只补发一份，重试/重复批准不重复发布。
                    pass
        connection.execute(update(reissues).where(reissues.c.id == key).values(
            state=ReissueState.approved.value, decision='approved',
            reviewer=actor, review_note=note, reviewed=now))
        add_event(connection, actor, 'approved', key, now)
    return {'decision': 'approved'}


def list_amendments():
    with engine.connect() as connection:
        entries = [dict(row) for row in
                   connection.execute(select(amendments).order_by(amendments.c.id.desc()).limit(500)).mappings()]
        effective = amend_service.effective_amendments(entries)
        for entry in entries:
            latest = effective[(entry['dock'], entry['kind'], entry['record_id'])]
            entry['is_effective'] = latest['revision_no'] == entry['revision_no']
            entry['terminal_cancel'] = (latest['action'] == AmendmentAction.cancel.value
                                        and latest['revision_no'] == entry['revision_no'])
        return entries


def month_versions(month):
    with engine.connect() as connection:
        versions = [dict(row) for row in connection.execute(
            select(report_versions).where(report_versions.c.month == month)
            .order_by(report_versions.c.version_no.desc())).mappings()]
        if not versions:
            raise HTTPException(404, '该月份尚无可下载的月报版本。')
        current = next((row for row in versions if row['current'] == 1), None)
        diff = None
        if current and current['reissue_id']:
            item = connection.execute(select(reissue_runs).where(
                reissue_runs.c.reissue_id == current['reissue_id'],
                reissue_runs.c.month == month)).mappings().one_or_none()
            diff = item['diff'] if item else None
        return {'month': month, 'current_version': current, 'versions': versions, 'diff': diff}


def version_report(version_id):
    with engine.connect() as connection:
        version = fetch_one(connection, report_versions, version_id)
        if version is None:
            raise HTTPException(404, '月报版本不存在。')
        run = fetch_one(connection, runs, version['run_id'])
        if run is None or not run['result']:
            raise HTTPException(409, '该版本的分析结果不可用。')
        return dict(version), run
