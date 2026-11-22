from datetime import datetime
from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.constants.enums import Source
from src.utils.time import now

class Dataset(Base):
    __tablename__ = 'datasets'
    __table_args__ = (UniqueConstraint('name', 'checksum'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    row_count: Mapped[int]
    # 数据来源：GIS 灯组名册（roster）或市政热线工单批次（hotline）。
    source: Mapped[str] = mapped_column(String(20), default=Source.ROSTER, index=True)
    # 来源批次的业务归属日（热线批次为批次覆盖日；名册为台账导出口径日），用于按日报点选数据。
    batch_date: Mapped[str | None] = mapped_column(String(10), index=True)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
