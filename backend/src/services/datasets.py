import csv
import hashlib
import io
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.constants.enums import Source
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.source import Dataset
from src.utils.db import row_locked


def checksum(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def save(db, name, filename, rows, actor, source=Source.ROSTER, batch_date=None):
    signature = checksum(rows)
    lookup = select(Dataset).where(Dataset.name == name, Dataset.checksum == signature)
    existing = db.scalar(lookup)
    if existing:
        return existing, False
    dataset = Dataset(name=name, filename=filename, checksum=signature, rows=rows,
                      row_count=len(rows), source=source, batch_date=batch_date, author=actor)
    if row_locked(db):
        try:
            with db.begin_nested():
                db.add(dataset)
                db.flush()
        except IntegrityError:
            existing = db.scalar(lookup.with_for_update())
            if existing:
                return existing, False
            raise
    else:
        # SQLite 等测试方言不使用 SAVEPOINT，依赖外层事务与唯一约束直接去重。
        db.add(dataset)
        db.flush()
    return dataset, True


def template():
    fields = [field['key'] for field in SPEC['fields']]
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    # The complete representative dataset also works as a reproducible import fixture.
    writer.writerows(demo_rows())
    return '﻿' + buffer.getvalue()
