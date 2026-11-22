from datetime import date, datetime
from sqlalchemy import func, select
from src.constants.enums import DeliveryState, Source, Status
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset
from src.services import scheduling
from src.services.execution import claim, execute

# 恢复时点：11-21 17:40 北京（09:40 UTC）。此时 20 日热线（18:30 北京到）仍未到，21 日热线已到。
RECOVERY = datetime(2026, 11, 21, 9, 40)
# 全部来源到齐时点：11-21 20:00 北京（12:00 UTC），晚于 20 日热线 18:30 到达。
ALL_SOURCES = datetime(2026, 11, 21, 12, 0)


def make_schedule(session_factory, *, next_at=None, anchor='2026-11-14', offset=0, enabled=True):
    params = {field['key']: field['default'] for field in SPEC['params']}
    params['district_scope'] = ''
    with session_factory.begin() as db:
        plan = Schedule(
            name='路灯暗区日报',
            dataset_id=db.scalar(select(Dataset.id).limit(1)),
            params=params, recipient='夜巡班组站内收件箱',
            next_at=next_at or datetime(2026, 11, 14, 10, 0),
            repeat_daily=True, anchor_report_day=anchor, report_offset=offset,
            enabled=enabled, author='seed')
        db.add(plan)
        db.flush()
        return plan.id


def recover_cycle(session_factory, stamp):
    """模拟一次完整 worker 恢复周期：补挂起 → 物化到期 → 重试发布 → 消费队列。"""
    promoted = scheduling.promote_waiting(stamp)
    created = scheduling.materialize_due(stamp)
    retried = scheduling.retry_pending(stamp)
    return promoted, created, retried


def settle(session_factory, stamp):
    """跑到稳定：晚到来源补挂后，物化才能越过原缺口继续，直到不再产生新运行。"""
    promoted, created, retried = [], [], []
    for _ in range(50):
        p, c, r = recover_cycle(session_factory, stamp)
        promoted, created, retried = promoted + p, created + c, retried + r
        if not p and not c:
            break
    return promoted, created, retried


def drain(session_factory, stamp):
    """反复跑恢复周期并消费队列，直到没有新运行、没有待算任务。"""
    processed = []
    for _ in range(100):
        settle(session_factory, stamp)
        lease = claim()
        if lease is None:
            break
        execute(lease)
        processed.append(lease[0])
    return processed


def test_pick_source_uses_last_received_before_cutoff(seeded):
    factory, ids = seeded
    with factory.begin() as db:
        cutoff = datetime(2026, 11, 17, 10, 0)  # 11-17 18:00 北京发报点
        roster = scheduling.choose_roster(db, cutoff)
        hotline = scheduling.choose_hotline(db, date(2026, 11, 17), cutoff)
    # 发报点前最后名册是 16 日导出（快照沿用）；17 日热线（18 日才到）尚不存在。
    assert roster.batch_date == '2026-11-16'
    assert hotline is None
    # 晚到首批到达后，按报告日点选到该批次，而不是其他日期的热线。
    later = datetime(2026, 11, 18, 2, 0)
    with factory.begin() as db:
        hotline = scheduling.choose_hotline(db, date(2026, 11, 17), later)
    assert hotline.batch_date == '2026-11-17'
    assert hotline.id == ids[(Source.HOTLINE, '2026-11-17')]


def test_backfill_stops_at_first_open_gap(seeded):
    factory, ids = seeded
    plan_id = make_schedule(factory)
    promoted, created, _ = settle(factory, RECOVERY)
    with factory.begin() as db:
        runs = db.scalars(select(Run).where(Run.schedule_id == plan_id)
                          .order_by(Run.report_day)).all()
    report_days = [r.report_day for r in runs]
    # 14..20 全部物化；17 日因热线晚到先挂起、同周期被晚到首批补挂；20 日热线未到仍是缺口。
    assert report_days == [f'2026-11-{d:02d}' for d in range(14, 21)]
    waiting = [r for r in runs if r.status == Status.WAITING]
    assert [r.report_day for r in waiting] == ['2026-11-20']
    assert waiting[0].roster_dataset_id is not None
    assert waiting[0].hotline_dataset_id is None
    assert '市政热线批次' in waiting[0].error
    # 17 日已被晚到首批补挂。
    assert '2026-11-17' in [str(day) for day in promoted] or \
           next(r for r in runs if r.report_day == '2026-11-17').status == Status.QUEUED
    day17 = next(r for r in runs if r.report_day == '2026-11-17')
    assert day17.hotline_dataset_id == ids[(Source.HOTLINE, '2026-11-17')]
    # 21 日日报尚未物化——连续交接被首个缺口 20 日截断。
    assert all(r.report_day != '2026-11-21' for r in runs)
    # 已物化运行绑定的来源不会因后续批次到达而更换。
    day14 = next(r for r in runs if r.report_day == '2026-11-14')
    assert day14.roster_dataset_id == ids[(Source.ROSTER, '2026-11-14')]
    assert day14.hotline_dataset_id == ids[(Source.HOTLINE, '2026-11-14')]


def test_late_first_batch_locked_and_never_swapped(seeded):
    factory, ids = seeded
    make_schedule(factory)
    settle(factory, RECOVERY)
    with factory.begin() as db:
        day17 = db.scalar(select(Run).where(Run.report_day == '2026-11-17'))
        # 17 日热线晚到，恢复时以晚到首批锁定。
        assert day17.hotline_dataset_id == ids[(Source.HOTLINE, '2026-11-17')]
        day18 = db.scalar(select(Run).where(Run.report_day == '2026-11-18'))
        # 18 日发报点名册快照停留在 16 日导出；18 日名册 19 日才到，但日报已锁定 16 日名册不换。
        assert day18.roster_dataset_id == ids[(Source.ROSTER, '2026-11-16')]
        assert day18.hotline_dataset_id == ids[(Source.HOTLINE, '2026-11-18')]
        day19 = db.scalar(select(Run).where(Run.report_day == '2026-11-19'))
        # 19 日日报采用发报前最新的 19 日名册。
        assert day19.roster_dataset_id == ids[(Source.ROSTER, '2026-11-19')]
    # 再跑到稳定是幂等的，不换批次、不重建运行。
    promoted, created, _ = settle(factory, RECOVERY)
    assert promoted == [] and created == []
    with factory.begin() as db:
        day18 = db.scalar(select(Run).where(Run.report_day == '2026-11-18'))
        assert day18.roster_dataset_id == ids[(Source.ROSTER, '2026-11-16')]


def test_backfill_runs_and_idempotent_delivery(seeded, tmp_path, monkeypatch):
    factory, ids = seeded
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
    from src.config import settings
    settings.ARTIFACTS = tmp_path
    plan_id = make_schedule(factory)
    settle(factory, RECOVERY)
    processed = drain(factory, RECOVERY)
    with factory.begin() as db:
        runs = db.scalars(select(Run).where(Run.schedule_id == plan_id)
                          .order_by(Run.report_day)).all()
        receipts = db.scalars(select(Delivery)).all()
    completed = [r for r in runs if r.status == Status.COMPLETE]
    # 14..19 共 6 份可完成；20 日挂起；每份完成运行恰好一份 delivered 回执。
    assert len(completed) == 6
    assert {r.report_day for r in completed} == {f'2026-11-{d:02d}' for d in range(14, 20)}
    assert len(receipts) == 6
    assert all(r.state == DeliveryState.DELIVERED for r in receipts)
    assert all(r.attempts == 1 for r in receipts)
    # 重放恢复与发布：已发的不重复。
    assert scheduling.retry_pending(RECOVERY) == []
    assert drain(factory, RECOVERY) == []
    with factory.begin() as db:
        assert db.scalar(select(func.count()).select_from(Delivery)) == 6
    # 合并批次分析：14 日有 2 张工单（柳汀大道、东门巷各一），导出文件已生成。
    day14 = next(r for r in runs if r.report_day == '2026-11-14')
    assert day14.export_file
    metric = day14.result['metrics'][1]
    assert metric['label'] == '纳入工单' and metric['value'] == 2


def test_gap_closure_unblocks_then_remaining_day_materializes(seeded):
    factory, ids = seeded
    plan_id = make_schedule(factory)
    settle(factory, RECOVERY)
    drain(factory, RECOVERY)
    # 20 日热线在 11-21 18:30 北京（10:30 UTC）到达；ALL_SOURCES 时所有来源齐备。
    promoted, created, _ = settle(factory, ALL_SOURCES)
    with factory.begin() as db:
        runs = db.scalars(select(Run).where(Run.schedule_id == plan_id)
                          .order_by(Run.report_day)).all()
    days = [r.report_day for r in runs]
    assert days == [f'2026-11-{d:02d}' for d in range(14, 22)]
    # 20 日被晚到首批补挂，21 日日报在缺口闭合后才物化。
    day20 = next(r for r in runs if r.report_day == '2026-11-20')
    assert day20.hotline_dataset_id == ids[(Source.HOTLINE, '2026-11-20')]
    day21 = next(r for r in runs if r.report_day == '2026-11-21')
    assert day21.status == Status.QUEUED
    drain(factory, ALL_SOURCES)
    with factory.begin() as db:
        all_runs = db.scalars(select(Run).where(Run.schedule_id == plan_id)).all()
        assert all(r.status == Status.COMPLETE for r in all_runs)
        assert db.scalar(select(func.count()).select_from(Delivery)) == 8


def test_delivery_interrupt_retry_publishes_only_missing(seeded, monkeypatch, tmp_path):
    factory, ids = seeded
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
    from src.config import settings
    settings.ARTIFACTS = tmp_path
    plan_id = make_schedule(factory)
    settle(factory, RECOVERY)
    # 先只跑一份；它正常发布后，模拟发布在终态落库前中断：把回执置回 pending。
    lease = claim()
    execute(lease)
    with factory.begin() as db:
        first_run = db.get(Run, lease[0])
        receipt = db.scalar(select(Delivery).where(Delivery.run_id == first_run.id))
        assert receipt is not None
        receipt.state = DeliveryState.PENDING
        receipt.attempts = 1
        pending_run_id = first_run.id
    # retry 把 pending 推进到 delivered（同一回执，不新建）；已 delivered 的不受影响。
    retried = scheduling.retry_pending(RECOVERY)
    assert retried == [pending_run_id]
    with factory.begin() as db:
        receipt = db.scalar(select(Delivery).where(Delivery.run_id == pending_run_id))
        assert receipt.state == DeliveryState.DELIVERED
        assert receipt.attempts == 2
    # 继续跑完剩余日报。
    drain(factory, RECOVERY)
    with factory.begin() as db:
        receipts = db.scalars(select(Delivery)).all()
        # 首单只有一份回执（重试未新建），其余各一份。
        assert len(receipts) == 6
        assert all(r.state == DeliveryState.DELIVERED for r in receipts)


def test_retry_creates_receipt_for_completed_run_without_one(seeded, monkeypatch, tmp_path):
    factory, ids = seeded
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
    from src.config import settings
    settings.ARTIFACTS = tmp_path
    make_schedule(factory)
    settle(factory, RECOVERY)
    lease = claim()
    execute(lease)
    # 模拟发布前彻底崩溃：删掉回执，只保留已完成运行。
    with factory.begin() as db:
        run_id = lease[0]
        existing = db.scalar(select(Delivery).where(Delivery.run_id == run_id))
        db.delete(existing)
    retried = scheduling.retry_pending(RECOVERY)
    assert retried == [run_id]
    with factory.begin() as db:
        receipt = db.scalar(select(Delivery).where(Delivery.run_id == run_id))
        assert receipt.state == DeliveryState.DELIVERED
        assert receipt.attempts == 1


def test_concurrent_materialize_does_not_duplicate_runs(seeded):
    factory, ids = seeded
    plan_id = make_schedule(factory)
    # 取首个发报点之后、第二个发报点之前的时点，只应物化 14 日一份。
    stamp = datetime(2026, 11, 14, 11, 0)
    scheduling.materialize_due(stamp)
    # 模拟第二个 worker 并发进入：计划已推进，无到期内容，不产生重复运行。
    again = scheduling.materialize_due(stamp)
    with factory.begin() as db:
        count = db.scalar(select(func.count()).select_from(Run)
                          .where(Run.schedule_id == plan_id))
    assert count == 1
    assert again == []


def test_handoff_boundary_and_checklist(seeded):
    from src.routes.distribution import checklist
    factory, ids = seeded
    make_schedule(factory)
    settle(factory, RECOVERY)
    drain(factory, RECOVERY)
    with factory.begin() as db:
        payload = checklist(db=db)
    assert payload['handoff_boundary'] == '2026-11-20'
    waiting_item = next(item for item in payload['items'] if item['report_day'] == '2026-11-20')
    assert waiting_item['missing_sources'] == ['hotline']
    assert waiting_item['roster']['batch_date'] == '2026-11-20'
    assert waiting_item['hotline'] is None
    day14 = next(item for item in payload['items'] if item['report_day'] == '2026-11-14')
    assert day14['delivery']['state'] == 'delivered'
    assert day14['export_path'] == f'/runs/{day14["run_id"]}/export'
    assert set(payload['source_labels']) == {'roster', 'hotline'}


def test_late_roster_first_batch_fills_waiting_run(seeded):
    """名册在发报点前缺失时，挂起日报由晚到首批名册补挂并锁定。"""
    from src.constants.enums import Source as Src
    from src.models.source import Dataset
    factory, ids = seeded
    plan_id = make_schedule(factory, next_at=datetime(2026, 11, 14, 10, 0))
    # 人为删掉 14、16 日名册，使 14 日发报点前没有任何名册。
    with factory.begin() as db:
        for ds_id in [ids[(Src.ROSTER, '2026-11-14')], ids[(Src.ROSTER, '2026-11-16')]]:
            db.delete(db.get(Dataset, ds_id))
    stamp = datetime(2026, 11, 14, 11, 0)
    scheduling.materialize_due(stamp)
    with factory.begin() as db:
        run = db.scalar(select(Run).where(Run.report_day == '2026-11-14'))
        assert run.status == Status.WAITING
        assert run.roster_dataset_id is None
    # 恢复时点（晚于 18 日名册到达）：以最早的晚到名册（18 日导出）补挂。
    promoted = scheduling.promote_waiting(RECOVERY)
    assert promoted == [run.id]
    with factory.begin() as db:
        run = db.scalar(select(Run).where(Run.report_day == '2026-11-14'))
        assert run.status == Status.QUEUED
        assert run.roster_dataset_id == ids[(Src.ROSTER, '2026-11-18')]
        assert run.hotline_dataset_id == ids[(Src.HOTLINE, '2026-11-14')]
    # 后续再晋升不换名册。
    assert scheduling.promote_waiting(RECOVERY) == []
