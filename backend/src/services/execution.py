import logging
from datetime import timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze
from src.models.jobs import Run
from src.services import scheduling
from src.services.audit import record
from src.utils.db import row_locked
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def _lock(query, db):
    return query.with_for_update(skip_locked=True) if row_locked(db) else query


def claim(stamp=None):
    stamp = stamp or now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(_lock(query.order_by(Run.id).limit(1), db))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        records = scheduling.combined_rows(db, job)
        return job.id, job.attempt, records, job.params


def execute(lease):
    job_id, attempt, rows, params = lease
    result = export = failure = None
    try:
        result = analyze(rows, params)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True) if row_locked(db) else db.get(Run, job_id)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if job.schedule_id and not failure:
            # 定时日报在计算完成后发布；publish 自身幂等，并发/重试只保留一份回执。
            scheduling.publish(db, job)
