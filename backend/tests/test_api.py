import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

_ARTIFACT = tempfile.mkdtemp(prefix='streetlight-api-')
_DB = Path(tempfile.gettempdir()) / 'streetlight-api-test.db'
if _DB.exists():
    _DB.unlink()
os.environ['DATABASE_URL'] = f'sqlite:///{_DB}'
os.environ['JWT_SECRET'] = 'api-test-secret'
os.environ['ARTIFACT_DIR'] = _ARTIFACT
os.environ['SEED_DEMO'] = 'true'


@pytest.fixture(scope='module')
def client():
    from fastapi.testclient import TestClient
    from src.main import app
    from src.utils import wakeup
    wakeup.wake = lambda: None
    with TestClient(app) as test_client:
        yield test_client


def _token(client, username, password):
    resp = client.post('/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return resp.json()['token']


def test_spec_exposes_scheduled_sources(client):
    token = _token(client, 'viewer', 'viewer-demo-2611')
    spec = client.get('/spec', headers={'Authorization': f'Bearer {token}'}).json()
    assert spec['scheduled'] is True
    assert {s['key'] for s in spec['sources']} == {'roster', 'hotline'}


def test_import_hotline_batch_and_checklist_shape(client):
    admin = _token(client, 'admin', 'admin-demo-2611')
    header = {'Authorization': f'Bearer {admin}'}
    csv_text = (
        'record_type,site_id,district,site_name,fixture_count,incident_id,report_date\n'
        '暗区工单,LH-01,,,,LT-API-1,2026-11-14\n')
    resp = client.post('/datasets', headers=header, json={
        'name': 'API 热线批次', 'filename': 'api-hotline.csv',
        'csv_text': csv_text, 'source': 'hotline', 'batch_date': '2026-11-14'})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['source'] == 'hotline'
    checklist = client.get('/delivery-checklist', headers=header).json()
    assert 'handoff_boundary' in checklist and 'items' in checklist
    assert set(checklist['source_labels']) == {'roster', 'hotline'}


def test_recover_permissions_and_idempotent_endpoint(client):
    viewer = _token(client, 'viewer', 'viewer-demo-2611')
    # 只读角色不能触发恢复。
    denied = client.post('/schedules/recover', headers={'Authorization': f'Bearer {viewer}'})
    assert denied.status_code == 403
    admin = _token(client, 'admin', 'admin-demo-2611')
    ok = client.post('/schedules/recover', headers={'Authorization': f'Bearer {admin}'})
    assert ok.status_code == 202
    payload = ok.json()
    assert set(payload) == {'promoted', 'materialized', 'retried'}


def test_worker_backfill_then_checklist_and_export(client):
    from src.services import scheduling
    from src.services.execution import claim, execute
    # 固定恢复时点驱动 worker，避免依赖墙钟。
    stamp = datetime(2026, 11, 21, 9, 40)
    for _ in range(20):
        scheduling.promote_waiting(stamp)
        scheduling.materialize_due(stamp)
        scheduling.retry_pending(stamp)
        lease = claim()
        if lease is None:
            break
        execute(lease)
    admin = _token(client, 'admin', 'admin-demo-2611')
    header = {'Authorization': f'Bearer {admin}'}
    checklist = client.get('/delivery-checklist', headers=header).json()
    # 首个未闭合缺口为 11-20（热线未到），连续交接在此截断。
    assert checklist['handoff_boundary'] == '2026-11-20'
    delivered = [item for item in checklist['items'] if item['delivery']
                 and item['delivery']['state'] == 'delivered']
    assert delivered, '至少有一份日报已发布'
    # 每份已发布日报都能下载，且运行与回执一一对应。
    for item in delivered:
        report = client.get(item['export_path'], headers=header)
        assert report.status_code == 200
        assert 'text/csv' in report.headers['content-type']
    run_ids = [item['run_id'] for item in checklist['items'] if item['delivery']]
    assert len(run_ids) == len(set(run_ids))
