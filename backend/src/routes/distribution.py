from datetime import UTC, date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import Session as SessionFactory, connection
from src.constants.enums import Role, Status
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset
from src.schemas.requests import ScheduleRequest
from src.services import scheduling
from src.services.audit import record
from src.services.validation import normalize_params
from src.utils import views
from src.utils.db import row_locked
from src.utils.time import now
from src.utils.wakeup import wake

router = APIRouter()


@router.get('/schedules')
def schedules(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.schedule(item) for item in db.scalars(select(Schedule).order_by(Schedule.id.desc()).limit(100))]


@router.post('/schedules', status_code=201)
def add(body: ScheduleRequest, db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    if body.first_at.utcoffset() is None:
        raise ValueError('首次运行时间需要明确时区')
    first = body.first_at.astimezone(UTC).replace(tzinfo=None)
    stamp = now()
    if first < stamp - timedelta(seconds=30) or first > stamp + timedelta(days=366):
        raise ValueError('请选择从现在起一年内的首次运行时间')
    anchor = body.anchor_report_day
    if anchor is None:
        anchor = (stamp + timedelta(hours=8)).date()
    item = Schedule(dataset_id=body.dataset_id, params=normalize_params(body.params), name=body.name,
                    recipient=body.recipient, next_at=first, repeat_daily=body.repeat_daily,
                    anchor_report_day=anchor.isoformat(), report_offset=0, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'schedule.created', item.id, recipient=item.recipient,
           anchor_report_day=item.anchor_report_day)
    db.commit()
    wake()
    return views.schedule(item)


@router.post('/schedules/recover', status_code=202)
def recover(actor: User = Depends(allow(Role.ADMIN))):
    """漏发恢复入口：补齐挂起日报、物化到期计划并重试中断的发布。"""
    promoted = scheduling.promote_waiting()
    materialized = scheduling.materialize_due()
    retried = scheduling.retry_pending()
    with SessionFactory.begin() as db:
        record(db, actor.username, 'schedule.recovered', 'schedules',
               promoted=promoted, materialized=materialized, retried=retried)
    wake()
    return {'promoted': promoted, 'materialized': materialized, 'retried': retried}


@router.post('/schedules/{schedule_id}/disable')
def disable(schedule_id: int, db: Session = Depends(connection), actor: User = Depends(allow(Role.ADMIN))):
    item = db.get(Schedule, schedule_id, with_for_update=True) if row_locked(db) else db.get(Schedule, schedule_id)
    if not item:
        raise HTTPException(404, '找不到该分发计划')
    if item.enabled:
        item.enabled = False
        record(db, actor.username, 'schedule.disabled', item.id)
        db.commit()
    return views.schedule(item)


@router.get('/deliveries')
def outbox(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.delivery(item) for item in db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(100))]


def _source_view(db, dataset_id):
    if not dataset_id:
        return None
    item = db.get(Dataset, dataset_id)
    return {'dataset_id': item.id, 'name': item.name, 'filename': item.filename,
            'source': item.source, 'batch_date': item.batch_date,
            'received_at': views.dataset(item)['created_at']} if item else None


@router.get('/delivery-checklist')
def checklist(db: Session = Depends(connection), actor: User = Depends(current)):
    """交付清单：逐日列出待补来源、所用批次、发布状态与日报下载。"""
    runs = db.scalars(
        select(Run).where(Run.schedule_id.is_not(None))
        .order_by(Run.report_day.asc(), Run.id.asc()).limit(200)
    ).all()
    receipts = {}
    if runs:
        receipts = {item.run_id: views.delivery(item)
                    for item in db.scalars(select(Delivery).where(Delivery.run_id.in_([r.id for r in runs])))}
    items = []
    for run in runs:
        missing = []
        if run.roster_dataset_id is None:
            missing.append('roster')
        if run.hotline_dataset_id is None:
            missing.append('hotline')
        items.append({
            'run_id': run.id,
            'report_day': run.report_day,
            'scheduled_at': views.run(run)['scheduled_at'],
            'status': run.status,
            'missing_sources': missing,
            'roster': _source_view(db, run.roster_dataset_id),
            'hotline': _source_view(db, run.hotline_dataset_id),
            'delivery': receipts.get(run.id),
            'export_path': f'/runs/{run.id}/export' if run.export_file else None,
            'result_path': f'/results/{run.id}',
            'error': run.error,
        })
    # 连续交接日期只能走到首个缺口前：计算每个计划当前的交接边界。
    boundary = None
    for item in items:
        if item['missing_sources'] or item['status'] == Status.WAITING:
            boundary = item['report_day']
            break
    return {'items': items, 'handoff_boundary': boundary,
            'source_labels': {entry['key']: entry['label'] for entry in SPEC['sources']}}
