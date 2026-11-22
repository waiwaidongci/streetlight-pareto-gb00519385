import json
from datetime import datetime
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role, Source
from src.domain.demo import demo_rows
from src.domain.demo_batches import build_batches
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import Schedule
from src.models.source import Dataset
from src.services.datasets import save
from src.utils.security import hash_password


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        # 保留手工测算用的完整示例批次。
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed',
                             source=Source.ROSTER, batch_date='2026-11-20')
        # 定时日报用的多日双来源批次；固定到达时间，重复执行不重建。
        batch_ids = []
        for name, filename, rows, source, batch_date, received_at in build_batches():
            dataset, _ = save(db, name, filename, rows, 'seed', source=source, batch_date=batch_date)
            dataset.created_at = received_at
            batch_ids.append(dataset.id)
        # 每日 18:00（北京时间）发报的定时计划，首份报告日为锚点 11-14。
        if not db.scalar(select(Schedule).limit(1)):
            anchor_day = SPEC['schedule_anchor_day']
            params = {field['key']: field['default'] for field in SPEC['params']}
            params['district_scope'] = ''
            db.add(Schedule(
                name='路灯暗区日报', dataset_id=item.id, params=params,
                recipient=SPEC['schedule_recipient'],
                next_at=datetime(2026, 11, 14, 10, 0),  # 18:00 北京 = 10:00 UTC
                repeat_daily=True, anchor_report_day=anchor_day, report_offset=0, author='seed'))
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created, 'schedule_batches': len(batch_ids)}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
