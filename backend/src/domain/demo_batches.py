"""定时日报演示用的多日双来源批次。

时间线（北京时间，UTC 存储减 8 小时）：
- 灯组名册按晚导出口径陆续到达；热线批次每日一份，其中 17、18 两晚漏发后晚到。
- worker 在 11-15 至 11-20 期间停机，11-21 晚恢复，触发逐日补跑。
- 18 日名册与 17 日热线为晚到首批；20 日热线在恢复时点仍未到，构成首个未闭合缺口。
"""

from datetime import datetime, timedelta, timezone

from src.constants.enums import Source

CST = timezone(timedelta(hours=8))

# (片区, 点位, 灯具数, 有报修的日期)
_SITES = (
    ("滨河片区", "柳汀大道北段", 32, (14, 15, 16, 17, 18, 19, 20)),
    ("老城片区", "东门巷", 5, (14, 16, 17, 19, 20)),
    ("滨河片区", "汀桥路公交站段", 24, (15, 17, 18, 20)),
    ("滨河片区", "长堤辅道", 3, (16, 20)),
    ("老城片区", "西仓里路口", 2, (19,)),
    ("滨河片区", "渡口支路", 9, ()),
    ("滨河片区", "桐浦堤下通道", 6, ()),
    ("老城片区", "埠头街南段", 14, ()),
    ("老城片区", "石平码头广场路", 11, ()),
    ("老城片区", "钟楼西街", 17, ()),
)

# 每日热线批次的工单行（按报告日过滤后只命中当日工单）。
_INCIDENTS_BY_DAY = {
    day: [
        (number, f"LT-2611{day}-{number:02d}-{sequence:02d}")
        for number, (_, _, _, days) in enumerate(_SITES, start=1)
        for sequence, d in enumerate([d for d in days if d == day], start=1)
    ]
    for day in range(14, 21)
}


def _received(day, hour=17, minute=30):
    """批次到达时间（北京时间）转 UTC naive，与 utils.time.now 口径一致。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=CST).astimezone(timezone.utc).replace(tzinfo=None)


def roster_rows():
    rows = []
    for number, (district, site, fixtures, _days) in enumerate(_SITES, start=1):
        rows.append({
            "record_type": "灯组台账", "site_id": f"LH-{number:02d}", "district": district,
            "site_name": site, "fixture_count": fixtures, "incident_id": None, "report_date": None,
        })
    return rows


def hotline_rows(day):
    rows = []
    for number, incident_id in _INCIDENTS_BY_DAY.get(day, []):
        rows.append({
            "record_type": "暗区工单", "site_id": f"LH-{number:02d}", "district": None,
            "site_name": None, "fixture_count": None,
            "incident_id": incident_id, "report_date": f"2026-11-{day:02d}",
        })
    return rows


# name, filename, source, batch_date, received_at(UTC naive)
BATCH_PLAN = [
    ("GIS 灯组名册 · 11-14 导出", "roster-20261114.csv", Source.ROSTER, "2026-11-14", _received(14, 16, 30)),
    ("热线批次 · 11-14", "hotline-20261114.csv", Source.HOTLINE, "2026-11-14", _received(14, 17, 20)),
    ("热线批次 · 11-15", "hotline-20261115.csv", Source.HOTLINE, "2026-11-15", _received(15, 17, 20)),
    ("GIS 灯组名册 · 11-16 导出", "roster-20261116.csv", Source.ROSTER, "2026-11-16", _received(16, 16, 40)),
    ("热线批次 · 11-16", "hotline-20261116.csv", Source.HOTLINE, "2026-11-16", _received(16, 17, 20)),
    # 17 日热线晚到：18 日 09:10（北京）才进系统，晚于 17 日 18:00 发报点，补跑时作为晚到首批被锁定。
    ("热线批次 · 11-17（晚到）", "hotline-20261117-late.csv", Source.HOTLINE, "2026-11-17", _received(18, 9, 10)),
    # 18 日名册晚到：19 日上午补导（18 日发报点名册快照停留在 16 日导出）。
    ("GIS 灯组名册 · 11-18 导出（晚到）", "roster-20261118-late.csv", Source.ROSTER, "2026-11-18", _received(19, 9, 30)),
    ("热线批次 · 11-18", "hotline-20261118.csv", Source.HOTLINE, "2026-11-18", _received(18, 17, 20)),
    ("GIS 灯组名册 · 11-19 导出", "roster-20261119.csv", Source.ROSTER, "2026-11-19", _received(19, 16, 30)),
    ("热线批次 · 11-19", "hotline-20261119.csv", Source.HOTLINE, "2026-11-19", _received(19, 17, 20)),
    ("GIS 灯组名册 · 11-20 导出", "roster-20261120.csv", Source.ROSTER, "2026-11-20", _received(20, 16, 30)),
    # 20 日热线在恢复时点（11-21 17:40）仍未到，形成首个未闭合缺口。
    ("热线批次 · 11-20", "hotline-20261120.csv", Source.HOTLINE, "2026-11-20", _received(21, 18, 30)),
    ("GIS 灯组名册 · 11-21 导出", "roster-20261121.csv", Source.ROSTER, "2026-11-21", _received(21, 16, 30)),
    ("热线批次 · 11-21", "hotline-20261121.csv", Source.HOTLINE, "2026-11-21", _received(21, 17, 10)),
]


def build_batches():
    """返回 (name, filename, rows, source, batch_date, received_at) 列表。"""
    batches = []
    for name, filename, source, batch_date, received_at in BATCH_PLAN:
        day = int(batch_date[-2:])
        rows = roster_rows() if source == Source.ROSTER else hotline_rows(day)
        batches.append((name, filename, rows, source, batch_date, received_at))
    return batches


# 演示恢复时点：11-21 17:40 北京时间（20 日热线 18:30 才到，故仍是缺口）。
RECOVERY_STAMP = datetime(2026, 11, 21, 9, 40, tzinfo=timezone.utc).replace(tzinfo=None)
# 全部来源到齐时点：11-21 19:00 北京时间。
ALL_SOURCES_STAMP = datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc).replace(tzinfo=None)
