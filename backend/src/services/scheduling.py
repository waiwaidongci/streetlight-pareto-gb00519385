"""定时日报的来源点选、逐日补跑与幂等发布。

口径：
- 每个报告日需要两类来源：GIS 灯组名册（roster）与当天市政热线批次（hotline）。
- 取原定发报时点之前最后收到的来源批次；缺失时取该来源晚到的首批数据，选定后锁定不换。
- 漏发恢复后逐日补跑；连续交接日期只走到首个缺口前，后面的日报可先行。
- 发布与定时/补跑并发时每个运行只产生一份回执；中断恢复后已发不重发、未发补发。
"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from src.config.database import Session
from src.constants.enums import DeliveryState, Source, Status
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset
from src.services.audit import record
from src.utils.db import row_locked
from src.utils.time import now

log = logging.getLogger(__name__)

# 单份日报补跑时一次最多物化的天数，避免长时间停机后单轮事务过大。
MAX_CATCH_UP_PER_CYCLE = 30


def _locked(query, db):
    # PostgreSQL 用行锁串行化并发 worker；SQLite 等本地测试方言不支持 SKIP LOCKED。
    return query.with_for_update(skip_locked=True) if row_locked(db) else query


def report_day_params(report_day: date) -> dict:
    """日报固定按报告日当日 00:00–23:59 的报修窗口测算。"""
    day = report_day.isoformat()
    return {'start_date': day, 'end_date': day}


def pick_source(db, source: str, cutoff: datetime):
    """发报时点（含）之前最后收到的该来源批次；没有则 None，等待晚到首批。"""
    return db.scalar(
        select(Dataset)
        .where(Dataset.source == source, Dataset.created_at <= cutoff)
        .order_by(Dataset.created_at.desc(), Dataset.id.desc())
        .limit(1)
    )


def select_for_day(db, source: str, report_day: date, cutoff: datetime):
    """按报告日点选来源批次。

    取报告日当天、且在当前时点（含）前已经收到的批次里最早到达的一份：
    - 发报点前到达 → 即原定的最后一批；
    - 发报点后才到 → 即晚到首批，选定后锁定；
    - 当天批次尚未到达 → 返回 None，由调用方挂起等待，不用别日批次顶替。
    """
    day = report_day.isoformat()
    return db.scalar(
        select(Dataset).where(
            Dataset.source == source, Dataset.batch_date == day, Dataset.created_at <= cutoff)
        .order_by(Dataset.created_at.asc(), Dataset.id.asc()).limit(1))


def choose_roster(db, cutoff: datetime):
    """名册为台账快照：严格取发报时点（含）前最后收到的一份；没有则返回 None 等待晚到首批。"""
    return pick_source(db, Source.ROSTER, cutoff)


def choose_hotline(db, report_day: date, cutoff: datetime):
    """热线按报告日点选当天批次；当天未到则等当天的晚到首批，不用别日批次顶替。"""
    return select_for_day(db, Source.HOTLINE, report_day, cutoff)


def choose_late_roster(db, due: datetime, stamp: datetime):
    """发报点前没有名册时，取晚到首批：due 之后、当前时点（含）前最早收到的名册。"""
    return db.scalar(
        select(Dataset).where(Dataset.source == Source.ROSTER,
                              Dataset.created_at > due, Dataset.created_at <= stamp)
        .order_by(Dataset.created_at.asc(), Dataset.id.asc()).limit(1))


def _materialize_one(db, plan: Schedule, due: datetime, report_day: date):
    """物化一份日报运行；来源不齐时挂起等待，连续交接在此处截断。"""
    roster = choose_roster(db, due)
    hotline = choose_hotline(db, report_day, due)
    waiting = roster is None or hotline is None
    params = {**plan.params, **report_day_params(report_day)}
    run = Run(
        schedule_id=plan.id,
        scheduled_at=due,
        report_day=report_day.isoformat(),
        name=f'{plan.name} · {report_day.isoformat()}',
        params=params,
        author=plan.author,
        roster_dataset_id=roster.id if roster else None,
        hotline_dataset_id=hotline.id if hotline else None,
        dataset_id=(roster or hotline).id if (roster or hotline) else plan.dataset_id,
        status=Status.WAITING if waiting else Status.QUEUED,
    )
    db.add(run)
    db.flush()
    if waiting:
        missing = [item['key'] for item in SPEC['sources']
                   if (item['key'] == Source.ROSTER and roster is None)
                   or (item['key'] == Source.HOTLINE and hotline is None)]
        labels = {item['key']: item['label'] for item in SPEC['sources']}
        run.error = '等待来源补齐：' + '、'.join(labels[key] for key in missing)
        record(db, 'worker', 'run.waiting', run.id, report_day=run.report_day, missing=missing)
    else:
        record(db, 'worker', 'run.materialized', run.id, report_day=run.report_day,
               roster_dataset_id=roster.id, hotline_dataset_id=hotline.id)
    return run, waiting


def materialize_due(stamp: datetime | None = None):
    """把到期的定时计划逐日物化；连续日报在首个缺口处停下，后续计划的日报不受影响。"""
    if not SPEC.get('scheduled'):
        return []
    stamp = stamp or now()
    created = []
    with Session.begin() as db:
        plan_query = _locked(
            select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
            .order_by(Schedule.next_at).limit(20), db)
        plans = db.scalars(plan_query)
        for plan in plans:
            if not plan.repeat_daily or not plan.anchor_report_day:
                plan.enabled = False
                continue
            anchor = date.fromisoformat(plan.anchor_report_day)
            slot = plan.next_at
            produced = 0
            while slot <= stamp and produced < MAX_CATCH_UP_PER_CYCLE:
                report_day = anchor + timedelta(days=plan.report_offset)
                # 计划已被行锁串行化；再按唯一键查重，保证并发/重入下同一时点不重复建单。
                exists = db.scalar(
                    select(Run.id).where(Run.schedule_id == plan.id, Run.scheduled_at == slot).limit(1))
                if exists is not None:
                    plan.report_offset += 1
                    plan.next_at = slot + timedelta(days=1)
                    slot = plan.next_at
                    produced += 1
                    continue
                run, waiting = _materialize_one(db, plan, slot, report_day)
                created.append(run.id)
                plan.report_offset += 1
                plan.next_at = slot + timedelta(days=1)
                slot = plan.next_at
                produced += 1
                if waiting:
                    # 连续交接日期只走到首个缺口前；后面的日报等缺口补齐后再推进。
                    break
    return created


def promote_waiting(stamp: datetime | None = None):
    """来源晚到后按时间顺序补挂起日报；只处理每个计划最旧的一份，保持首缺口顺序。"""
    if not SPEC.get('scheduled'):
        return []
    stamp = stamp or now()
    promoted = []
    with Session.begin() as db:
        waiting_query = _locked(
            select(Run).where(Run.status == Status.WAITING)
            .order_by(Run.scheduled_at, Run.id).limit(50), db)
        waiting_runs = db.scalars(waiting_query).all()
        seen_schedules = set()
        for run in waiting_runs:
            if run.schedule_id in seen_schedules:
                continue
            seen_schedules.add(run.schedule_id)
            due = run.scheduled_at
            report_day = date.fromisoformat(run.report_day)
            # 已锁定的批次保持不换；缺失的来源按恢复时点点选晚到首批。
            roster = (db.get(Dataset, run.roster_dataset_id) if run.roster_dataset_id
                      else choose_late_roster(db, due, stamp))
            hotline = (db.get(Dataset, run.hotline_dataset_id) if run.hotline_dataset_id
                       else choose_hotline(db, report_day, stamp))
            if roster is None or hotline is None:
                continue  # 仍是首个缺口，保持等待，后续日报不越过它。
            run.roster_dataset_id, run.hotline_dataset_id = roster.id, hotline.id
            run.dataset_id = roster.id
            run.status = Status.QUEUED
            run.error = None
            promoted.append(run.id)
            record(db, 'worker', 'run.promoted', run.id, report_day=run.report_day,
                   roster_dataset_id=roster.id, hotline_dataset_id=hotline.id)
    return promoted


def combined_rows(db, run: Run) -> list[dict]:
    """合并日报锁定的名册与热线批次；手工运行沿用单一数据集。"""
    if not run.schedule_id or not run.roster_dataset_id:
        return db.get(Dataset, run.dataset_id).rows
    roster = db.get(Dataset, run.roster_dataset_id)
    hotline = db.get(Dataset, run.hotline_dataset_id)
    return [*roster.rows, *hotline.rows]


def publish(db, run: Run, stamp: datetime | None = None):
    """幂等发布：每个运行最多一份回执，中断恢复后已发不重发、未发补发。"""
    stamp = stamp or now()
    receipt = db.scalar(_locked(select(Delivery).where(Delivery.run_id == run.id), db))
    ready = not run.error and (run.result or {}).get('ready', True)
    if receipt is not None and receipt.state in (DeliveryState.DELIVERED, DeliveryState.BLOCKED):
        return receipt
    detail = '已保存 CSV 到站内收件箱' if ready else (run.error or (run.result or {}).get('headline', '发布未就绪'))
    if receipt is None:
        plan = db.get(Schedule, run.schedule_id)
        receipt = Delivery(run_id=run.id, recipient=plan.recipient if plan else '站内收件箱',
                           state=DeliveryState.PENDING, detail='等待发布')
        db.add(receipt)
    receipt.attempts = (receipt.attempts or 0) + 1
    receipt.state = DeliveryState.DELIVERED if ready else DeliveryState.BLOCKED
    receipt.detail = detail[:500]
    receipt.updated_at = stamp
    record(db, 'worker', f'delivery.{receipt.state}', run.id,
           recipient=receipt.recipient, attempt=receipt.attempts)
    return receipt


def retry_pending(stamp: datetime | None = None):
    """发布中断恢复：未到终态的回执重新发布；完成却没有回执的运行补建。已投递不重复。"""
    if not SPEC.get('scheduled'):
        return []
    stamp = stamp or now()
    retried = []
    with Session.begin() as db:
        pending = select(Delivery).where(
            Delivery.state.in_((DeliveryState.PENDING, DeliveryState.RETRYING))
        ).order_by(Delivery.id).limit(50)
        receipts = db.scalars(_locked(pending, db)).all()
        for receipt in receipts:
            run = db.get(Run, receipt.run_id)
            if run is None or run.status != Status.COMPLETE:
                continue
            receipt.state = DeliveryState.RETRYING
            receipt.updated_at = stamp
            publish(db, run, stamp)
            retried.append(run.id)
        # 计算已完成但发布前崩溃、连回执都没留下的运行：补建且不影响已发运行。
        orphan_runs = db.scalars(
            select(Run).where(Run.schedule_id.is_not(None), Run.status == Status.COMPLETE,
                              ~Run.id.in_(select(Delivery.run_id))).order_by(Run.id).limit(50)
        ).all()
        for run in orphan_runs:
            publish(db, run, stamp)
            retried.append(run.id)
    return retried
