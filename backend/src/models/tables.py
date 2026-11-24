from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer), Column('reissue_id', Integer),
                   Column('run_id', Integer, nullable=False), Column('month', String(7)),
                   Column('recipient', String(100), nullable=False),
                   Column('scheduled', Double, nullable=False), Column('delivered', Double, nullable=False),
                   UniqueConstraint('schedule_id', 'scheduled'),
                   UniqueConstraint('reissue_id', 'month', 'recipient'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))
# 维修班月报寄出后的停机工单修订流水；同一（月台、停机类型、单号）以最高 revision_no 为准
amendments = Table('downtime_amendments', metadata, Column('id', Integer, primary_key=True),
                   Column('dock', String(40), nullable=False), Column('kind', String(20), nullable=False),
                   Column('record_id', String(80), nullable=False), Column('revision_no', Integer, nullable=False),
                   Column('action', String(16), nullable=False), Column('start', String(32)),
                   Column('end', String(32)), Column('applied', Integer, nullable=False),
                   Column('owner', String(40), nullable=False), Column('created', Double, nullable=False),
                   UniqueConstraint('dock', 'kind', 'record_id', 'revision_no'))
# 一次导入触发一次重编批次，覆盖全部受影响月份
reissues = Table('report_reissues', metadata, Column('id', Integer, primary_key=True),
                 Column('state', String(16), nullable=False, index=True),
                 Column('months', JSON, nullable=False), Column('amendment_ids', JSON, nullable=False),
                 Column('snapshot', JSON, nullable=False), Column('target_pct', Double, nullable=False),
                 Column('error', Text), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
                 Column('decision', String(16)), Column('reviewer', String(40)),
                 Column('review_note', Text), Column('reviewed', Double))
reissue_runs = Table('reissue_runs', metadata, Column('id', Integer, primary_key=True),
                     Column('reissue_id', Integer, nullable=False, index=True), Column('month', String(7), nullable=False),
                     Column('run_id', Integer), Column('state', String(16), nullable=False),
                     Column('version_no', Integer, nullable=False), Column('error', Text),
                     Column('diff', JSON), UniqueConstraint('reissue_id', 'month'))
# 月份当前生效的月报版本；历史版本行永久保留，已寄出的旧版仍可下载
report_versions = Table('report_versions', metadata, Column('id', Integer, primary_key=True),
                        Column('month', String(7), nullable=False), Column('version_no', Integer, nullable=False),
                        Column('run_id', Integer, nullable=False), Column('reissue_id', Integer),
                        Column('recipient_snapshot', JSON, nullable=False), Column('current', Integer, nullable=False),
                        Column('created', Double, nullable=False), UniqueConstraint('month', 'version_no'))
