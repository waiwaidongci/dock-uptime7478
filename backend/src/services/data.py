import csv
import hashlib
import io
import json
import math
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine
from src.models.tables import datasets

PROFILE = json.loads(Path(__file__).with_name('domain.json').read_text(encoding='utf-8'))


def convert(value, field):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        raise ValueError('不能为空')
    kind = field['type']
    if kind == 'str':
        return str(value).strip()
    if kind == 'select':
        if value not in field['choices']:
            raise ValueError('请选择有效选项')
        return value
    number = int(str(value)) if kind == 'int' else float(value)
    if not math.isfinite(number):
        raise ValueError('必须为有限数值')
    if number < field.get('min', -math.inf) or number > field.get('max', math.inf):
        raise ValueError('数值超出可接受范围')
    return number


def csv_records(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
    columns = PROFILE['columns']
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    required = {field['key'] for field in columns}
    if not fieldnames or len(fieldnames) != len(required) or set(fieldnames) != required:
        raise ValueError('CSV 表头须为 ' + '、'.join(field['key'] for field in columns))
    rows = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f'第 {reader.line_num} 行列数多于表头。')
            normalized = {}
            for field in columns:
                try:
                    normalized[field['key']] = convert(row[field['key']], field)
                except (ValueError, TypeError, OverflowError) as error:
                    raise ValueError(f"第 {reader.line_num} 行 {field['label']} 无效（{error}）。") from error
            rows.append(normalized)
            if len(rows) > 5000:
                raise ValueError('单份数据最多 5000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('CSV 需要至少一条数据。')
    return rows


def parse_options(values):
    fields = PROFILE['options']
    if set(values) - {field['key'] for field in fields}:
        raise ValueError('存在未定义的分析参数。')
    output = {}
    for field in fields:
        try:
            output[field['key']] = convert(values.get(field['key'], field['default']), field)
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"{field['label']} 无效（{error}）。") from error
    return output


def save_revision(name, records, owner):
    if not name.strip():
        raise ValueError('数据名称不能为空。')
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    now = time.time()
    existing = select(datasets.c.id).where(datasets.c.checksum == checksum)
    try:
        with engine.begin() as connection:
            found = connection.scalar(existing)
            if found is not None:
                return found, False
            key = connection.execute(insert(datasets).values(name=name.strip(), owner=owner, records=records,
                                                             checksum=checksum, created=now)).inserted_primary_key[0]
            add_event(connection, owner, 'import', key, now)
            return key, True
    except IntegrityError:
        with engine.connect() as connection:
            found = connection.scalar(existing)
            if found is None:
                raise
            return found, False


def amendment_records(text):
    """解析维修班补报的停机工单修订 CSV，撤销行的起止时间允许留空。"""
    from src.constants.enums import AmendmentAction
    from src.services.domain import STAMP

    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
    fields = PROFILE['amendment_columns']
    required = {field['key'] for field in fields}
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    if not fieldnames or len(fieldnames) != len(required) or set(fieldnames) != required:
        raise ValueError('CSV 表头须为 ' + '、'.join(field['key'] for field in fields))
    rows = []
    seen = set()
    try:
        for raw in reader:
            if None in raw:
                raise ValueError(f'第 {reader.line_num} 行列数多于表头。')
            line = reader.line_num
            values = {key: (raw.get(key) or '').strip() for key in required}
            if not values['dock']:
                raise ValueError(f'第 {line} 行月台编号不能为空。')
            if values['kind'] not in ('计划停机', '非计划停机'):
                raise ValueError(f'第 {line} 行停机类型须为计划停机或非计划停机（修订只接受停机工单）。')
            if not values['record_id']:
                raise ValueError(f'第 {line} 行原始停机单号不能为空。')
            try:
                revision_no = int(values['revision_no'])
            except ValueError as error:
                raise ValueError(f'第 {line} 行修订号须为正整数。') from error
            if revision_no < 1:
                raise ValueError(f'第 {line} 行修订号须为正整数。')
            action = values['action']
            if action not in (AmendmentAction.reschedule.value, AmendmentAction.cancel.value):
                raise ValueError(f'第 {line} 行修订动作须为改期或撤销。')
            start = end = ''
            if action == AmendmentAction.reschedule.value:
                if not STAMP.fullmatch(values['start']) or not STAMP.fullmatch(values['end']):
                    raise ValueError(f'第 {line} 行改期时间无效，请按 YYYY-MM-DD HH:MM:SS 填写。')
                start, end = values['start'], values['end']
                if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
                    raise ValueError(f'第 {line} 行改期结束时间须晚于开始时间。')
            elif values['start'] or values['end']:
                raise ValueError(f'第 {line} 行撤销动作不应填写起止时间。')
            identity = (values['dock'], values['kind'], values['record_id'], revision_no)
            if identity in seen:
                raise ValueError(f'第 {line} 行同月台、类型、单号、修订号重复，请核对修订流水。')
            seen.add(identity)
            rows.append({
                'dock': values['dock'], 'kind': values['kind'], 'record_id': values['record_id'],
                'revision_no': revision_no, 'action': action, 'start': start, 'end': end,
            })
            if len(rows) > 2000:
                raise ValueError('单次修订最多 2000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('修订 CSV 需要至少一条记录。')
    return rows
