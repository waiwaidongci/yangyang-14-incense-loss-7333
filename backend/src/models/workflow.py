from datetime import datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.constants.enums import RunStatus, VoucherStatus
from src.models.base import Base, utc_now


class Observation(Base):
    __tablename__ = 'observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(96), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class MaterialAccount(Base):
    """批次物料费账户。余额以整数分保存，version 为乐观锁计数。"""
    __tablename__ = 'material_accounts'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_code: Mapped[str] = mapped_column(String(40), unique=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class CorrectionVoucher(Base):
    """领退料更正单：把一笔物料费从 source 批次挪到 target 批次。"""
    __tablename__ = 'correction_vouchers'
    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_no: Mapped[str] = mapped_column(String(40), unique=True)
    source_batch: Mapped[str] = mapped_column(String(40), index=True)
    target_batch: Mapped[str] = mapped_column(String(40), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(200), default='')
    status: Mapped[str] = mapped_column(String(16), default=VoucherStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LedgerEntry(Base):
    """物料明细账分录。一张已入账更正单恒产生借、贷各一条，金额相等。"""
    __tablename__ = 'ledger_entries'
    __table_args__ = (
        UniqueConstraint('voucher_no', 'batch_code', 'side', name='uq_entry_voucher_batch_side'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_no: Mapped[str] = mapped_column(String(40), index=True)
    batch_code: Mapped[str] = mapped_column(String(40), index=True)
    side: Mapped[str] = mapped_column(String(8))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    balance_after: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AnalysisRun(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON)
    last_observation_id: Mapped[int] = mapped_column(Integer)
    sample_size: Mapped[int] = mapped_column(Integer)
    observation_ids: Mapped[list] = mapped_column(JSON)
    # 发起分析时已入账的更正单上界与其单号清单，固化“当时的账”，保证旧结果可复算、可回查凭证。
    last_voucher_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voucher_nos: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED, index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), unique=True)
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Schedule(Base):
    __tablename__ = 'schedules'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    recipient: Mapped[str] = mapped_column(String(100))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    next_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base):
    __tablename__ = 'deliveries'
    __table_args__ = (UniqueConstraint('schedule_id', 'scheduled_at'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    filename: Mapped[str] = mapped_column(String(120))
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
