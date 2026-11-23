"""领退料更正单的登记、双边入账与挂起重试（持久化 + 并发控制层）。

并发口径：
- 更正单号唯一，重复登记按原单号返回既有单，绝不重复扣款（幂等）。
- 入账前对涉及的两个批次账户按批次号排序加行锁，串行化同批次并发更正，
  扣减前在锁内复核余额，因此任何批次都不会被扣成负数。
- 余额不足的更正单整单置 waiting，不阻塞无关批次；后续入账补足后由本模块或 worker 自动重试。
- 批次账户首次被引用时，按观测台账里该批次的净物料费物化期初余额，之后只随分录变动。
"""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import EntrySide, VoucherStatus
from src.domain import ledger as domain
from src.models.base import utc_now
from src.models.workflow import (
    AuditEvent, CorrectionVoucher, LedgerEntry, MaterialAccount, Observation,
)
from src.services.errors import BusinessError

PROCESS_LIMIT = 50


def base_material_cents(db, batch_code: str) -> int:
    """观测台账中某批次的净物料费合计（整数分）。payload 为 JSON，逐行求和以兼容各方言。"""
    rows = db.scalars(select(Observation.payload))
    return sum(int(row['material_cents']) for row in rows if row.get('batch_code') == batch_code)


def entry_delta_cents(db, batch_code: str) -> int:
    """某批次已入账分录的净变动：补入为正、扣回为负。"""
    delta = 0
    for entry in db.scalars(select(LedgerEntry).where(LedgerEntry.batch_code == batch_code)):
        delta += entry.amount_cents if entry.side == EntrySide.DEBIT else -entry.amount_cents
    return delta


def ensure_account(db, batch_code: str) -> MaterialAccount:
    account = db.scalar(select(MaterialAccount).where(MaterialAccount.batch_code == batch_code)
                        .with_for_update())
    if account is None:
        account = MaterialAccount(batch_code=batch_code, balance_cents=0, version=0)
        db.add(account)
        db.flush()
        # 新插入的行在本事务内即受锁保护，重新取一次以统一进入加锁集合。
        account = db.scalar(select(MaterialAccount).where(MaterialAccount.batch_code == batch_code)
                            .with_for_update())
    return account


def _to_domain_voucher(row: CorrectionVoucher) -> domain.CorrectionVoucher:
    return domain.CorrectionVoucher(
        voucher_no=row.voucher_no, source_batch=row.source_batch, target_batch=row.target_batch,
        amount_cents=row.amount_cents, reason=row.reason, status=VoucherStatus(row.status),
        attempts=row.attempts, last_error=row.last_error, created_at=row.created_at,
        posted_at=row.posted_at)


def _lock_accounts(db, voucher: CorrectionVoucher) -> dict[str, MaterialAccount]:
    # 统一按批次号排序加锁，避免两张单互锁同一对账户时形成死锁。
    for batch in sorted({voucher.source_batch, voucher.target_batch}):
        ensure_account(db, batch)
    accounts = {row.batch_code: row for row in db.scalars(
        select(MaterialAccount)
        .where(MaterialAccount.batch_code.in_([voucher.source_batch, voucher.target_batch]))
        .order_by(MaterialAccount.batch_code).with_for_update())}
    # 可用余额以观测台账净物料费 + 已入账分录净变动为准，避免冻结的期初快照漏掉后续新增观测。
    for code, account in accounts.items():
        account.balance_cents = base_material_cents(db, code) + entry_delta_cents(db, code)
    return accounts


def _persist_posting(db, voucher: CorrectionVoucher, accounts: dict[str, MaterialAccount],
                     new_entries: list[domain.LedgerEntry]) -> None:
    for entry in new_entries:
        account = accounts[entry.batch_code]
        db.add(LedgerEntry(voucher_no=entry.voucher_no, batch_code=entry.batch_code, side=entry.side,
                           amount_cents=entry.amount_cents, balance_after=entry.balance_after,
                           created_at=entry.at))
        account.balance_cents = entry.balance_after
        account.version += 1
    if voucher.status == VoucherStatus.POSTED:
        voucher.posted_at = voucher.posted_at or utc_now()


def attempt_post(db, voucher: CorrectionVoucher, *, audit_waiting: bool = True) -> CorrectionVoucher:
    """对单张更正单做一次入账尝试，调用方须已开启事务。"""
    locked = db.get(CorrectionVoucher, voucher.id, with_for_update=True)
    if locked is None:
        raise BusinessError('更正单不存在', 404)
    if locked.status == VoucherStatus.POSTED:
        return locked  # 幂等：已入账不再产生任何分录。
    accounts = _lock_accounts(db, locked)
    domain_accounts = {code: domain.MaterialAccount(code, row.balance_cents, row.version)
                       for code, row in accounts.items()}
    domain_voucher = _to_domain_voucher(locked)
    _, new_entries = domain.post_voucher(domain_voucher, domain_accounts, [])
    locked.attempts = domain_voucher.attempts
    locked.status = domain_voucher.status
    locked.last_error = domain_voucher.last_error
    _persist_posting(db, locked, accounts, new_entries)
    if domain_voucher.status == VoucherStatus.POSTED:
        db.add(AuditEvent(actor='worker', action='voucher.posted',
                          detail=f'更正单 {locked.voucher_no}：{locked.source_batch} → {locked.target_batch} '
                                 f'{locked.amount_cents} 分'))
    elif audit_waiting:
        db.add(AuditEvent(actor='worker', action='voucher.waiting',
                          detail=f'更正单 {locked.voucher_no}：{locked.last_error}'))
    db.flush()
    return locked


def process_due_vouchers(db, limit: int = PROCESS_LIMIT) -> int:
    """按登记顺序处理待入账/挂起单，一轮内多趟扫描直到没有可再入账的单。

    - 每趟跳过本趟已试且仍挂起的单，因此队首余额不足不会阻塞无关批次，它们照常入账。
    - 一趟中若有单入账（可能补足后续批次余额），清空趟内标记再扫一趟，把被解开的挂起单同轮入账。
    返回本轮有动作（入账或重试计数变化）的单据数，总尝试次数受 limit 约束。
    """
    acted = 0
    budget = limit
    while budget > 0:
        tried: set[int] = set()
        posted_this_pass = False
        progressed_this_pass = False
        while budget > 0:
            query = (select(CorrectionVoucher)
                     .where(CorrectionVoucher.status.in_([VoucherStatus.QUEUED, VoucherStatus.WAITING])))
            if tried:
                query = query.where(CorrectionVoucher.id.notin_(tried))
            voucher = db.scalar(
                query.order_by(CorrectionVoucher.created_at, CorrectionVoucher.id)
                .limit(1).with_for_update(skip_locked=True))
            if voucher is None:
                break
            tried.add(voucher.id)
            budget -= 1
            before = (voucher.status, voucher.attempts)
            # 自动重试仍余额不足时不重复刷审计，避免日志噪声；入账成功才记 posted。
            attempt_post(db, voucher, audit_waiting=False)
            if (voucher.status, voucher.attempts) != before:
                acted += 1
                progressed_this_pass = True
            if voucher.status == VoucherStatus.POSTED:
                posted_this_pass = True
        # 本趟没有任何进展，或没有入账能再补足余额时结束；否则再扫一趟处理链锁解开的单。
        if not progressed_this_pass or not posted_this_pass:
            break
    return acted


def register_voucher(db, payload: dict, actor: str) -> tuple[CorrectionVoucher, bool]:
    """登记一张领退料更正单并立即尝试入账。返回 (单据, 是否新建)。

    同一 voucher_no 重复提交：内容一致时原样返回既有单（不重复扣款）；内容冲突时拒绝。
    """
    clean = domain.validate_voucher(payload)
    existing = db.scalar(select(CorrectionVoucher).where(
        CorrectionVoucher.voucher_no == clean['voucher_no']).with_for_update())
    if existing is not None:
        same = (existing.source_batch == clean['source_batch']
                and existing.target_batch == clean['target_batch']
                and existing.amount_cents == clean['amount_cents'])
        if not same:
            raise BusinessError(f'更正单号 {clean["voucher_no"]} 已存在且内容不同，不能覆盖原单', 409)
        return existing, False
    voucher = CorrectionVoucher(status=VoucherStatus.QUEUED, created_by=actor, **clean)
    db.add(voucher)
    db.add(AuditEvent(actor=actor, action='voucher.registered',
                      detail=f'更正单 {voucher.voucher_no} 登记 {voucher.amount_cents} 分'))
    try:
        db.flush()
    except IntegrityError as exc:
        raise BusinessError('同一更正单号刚刚被提交，请刷新后查询原单', 409) from exc
    attempt_post(db, voucher)
    return voucher, True


def ledger_reconcile(db) -> dict:
    """物料账对账：借/贷分录各自合计，差额须为 0 分；并回算账户余额合计。"""
    credit = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
        LedgerEntry.side == EntrySide.CREDIT))
    debit = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
        LedgerEntry.side == EntrySide.DEBIT))
    account_total = db.scalar(select(func.coalesce(func.sum(MaterialAccount.balance_cents), 0)))
    return {
        'entry_count': db.scalar(select(func.count(LedgerEntry.id))),
        'credit_total_cents': int(credit),
        'debit_total_cents': int(debit),
        'difference_cents': int(debit) - int(credit),
        'account_total_cents': int(account_total),
        'account_count': db.scalar(select(func.count(MaterialAccount.id))),
        'waiting_count': db.scalar(select(func.count(CorrectionVoucher.id)).where(
            CorrectionVoucher.status == VoucherStatus.WAITING)),
    }
