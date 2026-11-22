import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', '/tmp/streetlight-test-artifacts')


@pytest.fixture()
def db_session(monkeypatch):
    # 每个测试用独立的内存 SQLite，并把服务层使用的 Session 指过去。
    from src.config import database as db_module
    engine = create_engine('sqlite:///:memory:',
                           connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    TestingSession = sessionmaker(engine, expire_on_commit=False)
    db_module.engine = engine
    db_module.Session = TestingSession
    # 服务层以 `from src.config.database import Session` 提前绑定，需同步替换。
    from src.services import scheduling, execution
    from src.routes import distribution
    scheduling.Session = TestingSession
    execution.Session = TestingSession
    distribution.SessionFactory = TestingSession
    from src.config.database import Base
    from src.models import identity, jobs, source  # noqa: F401
    Base.metadata.create_all(engine)

    # Redis 在本地测试环境不存在，打桩唤醒函数。
    from src.utils import wakeup
    monkeypatch.setattr(wakeup, 'wake', lambda: None)
    monkeypatch.setattr(wakeup, 'wait', lambda: None)

    # 不保留长连接会话：所有操作都通过工厂短事务串行使用 StaticPool 的单连接。
    yield TestingSession
    engine.dispose()


@pytest.fixture()
def seeded(db_session):
    from src.constants.enums import Role
    from src.domain.demo_batches import build_batches
    from src.models.identity import User
    from src.models.source import Dataset
    from src.services.datasets import save
    from src.utils.security import hash_password
    session_factory = db_session
    with session_factory.begin() as db:
        for role in Role:
            db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-pw')))
        ids = {}
        for name, filename, rows, source, batch_date, received_at in build_batches():
            dataset, _ = save(db, name, filename, rows, 'seed', source=source, batch_date=batch_date)
            dataset.created_at = received_at
            ids[(source, batch_date)] = dataset.id
    return session_factory, ids


def utc(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc).replace(tzinfo=None)
