import csv
import io
import json

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import Role
from src.models.database import engine
from src.models.tables import datasets, reissues, users
from src.services.data import PROFILE, convert, save_revision
from src.services.domain import sample_amendments, sample_rows
from src.services.reissues import import_amendments
from src.utils.security import protect


def _amendment_csv(rows):
    keys = [field['key'] for field in PROFILE['amendment_columns']]
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, '') for key in keys})
    return stream.getvalue()


def seed():
    for role in Role:
        try:
            with engine.begin() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    connection.execute(insert(users).values(username=role.value, role=role.value,
                                                            password=protect(f'{role.value}-demo-24')))
        except IntegrityError:
            with engine.connect() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    raise
    records = [{field['key']: convert(row[field['key']], field) for field in PROFILE['columns']} for row in sample_rows()]
    revision, created = save_revision(PROFILE['filename'], records, 'seed')
    reissue_id = None
    try:
        reissue_id = import_amendments(_amendment_csv(sample_amendments()), 'seed')['id']
    except (ValueError, IntegrityError):
        # 修订已登记或无新内容时跳过，worker 会继续处理已入队的重编批次。
        pass
    with engine.connect() as connection:
        counts = {table.name: connection.scalar(select(func.count()).select_from(table))
                  for table in (users, datasets, reissues)}
    return {'revision': revision, 'created': created, 'reissue': reissue_id, **counts, 'rows': len(records)}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
