"""把领退料明细账叠加到观测台账，得到某一时点（快照）各批次的有效物料费。

口径：
- 已排队的分析按发起时的账算：只纳入该分析快照时点之前已入账（posted）的更正单。
- 新分析用当前已入账的账；尚未入账（queued/waiting）的更正单不计入任何分析。
- 旧结果永久保留当时所用凭证清单与重算后的金额，可随时回查当时凭证。
"""
from sqlalchemy import select

from src.constants.enums import EntrySide, VoucherStatus
from src.domain import ledger as domain
from src.domain.projection import apply_entries
from src.models.workflow import CorrectionVoucher, LedgerEntry, Observation


def posted_entries(db, *, max_voucher_id: int | None = None) -> list[domain.LedgerEntry]:
    """取已入账更正单的分录；max_voucher_id 限定为某分析快照时点之前的凭证。"""
    voucher_query = select(CorrectionVoucher).where(CorrectionVoucher.status == VoucherStatus.POSTED)
    if max_voucher_id is not None:
        voucher_query = voucher_query.where(CorrectionVoucher.id <= max_voucher_id)
    numbers = [voucher.voucher_no for voucher in db.scalars(voucher_query)]
    if not numbers:
        return []
    records = db.scalars(select(LedgerEntry).where(LedgerEntry.voucher_no.in_(numbers))
                         .order_by(LedgerEntry.id))
    return [domain.LedgerEntry(row.voucher_no, row.batch_code, EntrySide(row.side),
                               row.amount_cents, row.balance_after, row.created_at)
            for row in records]


def project_for_run(db, observation_ids: list[int], max_voucher_id: int | None) -> list[dict]:
    """按分析固化的观测清单和凭证时点，重算当时各批次有效物料费。"""
    rows = db.scalars(select(Observation.payload).where(Observation.id.in_(observation_ids))
                      .order_by(Observation.id))
    return apply_entries(list(rows), posted_entries(db, max_voucher_id=max_voucher_id))
