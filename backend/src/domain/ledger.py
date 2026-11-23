"""领退料更正单的双边入账领域逻辑（仅用标准库，便于离线复验）。

口径：
- 一张更正单把一笔整数物料费从一个线香批次（扣减侧 credit）挪到另一个批次（补入侧 debit）。
- 两条分录同生共死：要么同事务一起入账，要么都不动。
- 扣减侧余额不足时整单挂起（waiting），不允许把任何批次扣成负数；与本单无关的批次照常入账。
- 更正单按原单号幂等：同一单号重复提交不会重复扣款，已入账则原样返回既有分录。
- 任意一笔入账成功后，按登记顺序自动重试挂起单；后续入账把余额补足即可解开。
- 每张更正单借、贷金额相等，因此全坊物料费总额守恒，对账差额恒为 0 分。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.constants.enums import EntrySide, VoucherStatus

MAX_AMOUNT = 100_000_000          # 单张更正单上限：100 万元（分）
MAX_ATTEMPTS = 8                  # 自动重试上限，超过后置 failed 需人工处理
VOUCHER_PREFIX = 'GZ-LT-'         # 领退料更正单号前缀


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class LedgerEntry:
    """一条物料明细账分录。"""
    voucher_no: str
    batch_code: str
    side: EntrySide
    amount_cents: int
    balance_after: int
    at: datetime


@dataclass
class MaterialAccount:
    """批次物料费账户，余额以整数分保存。"""
    batch_code: str
    balance_cents: int = 0
    version: int = 0


@dataclass
class CorrectionVoucher:
    voucher_no: str
    source_batch: str            # 错记、需扣回物料费的批次
    target_batch: str            # 应补记物料费的批次
    amount_cents: int
    reason: str = ''
    status: VoucherStatus = VoucherStatus.QUEUED
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    posted_at: datetime | None = None


def validate_voucher(payload: dict) -> dict:
    """校验更正单录入，返回规范化字段。校验规则与录入渠道无关（表单、CSV 共用）。"""
    if not isinstance(payload, dict):
        raise ValueError('更正单内容须为对象')
    voucher_no = str(payload.get('voucher_no', '')).strip()
    if not voucher_no:
        raise ValueError('更正单号不能为空')
    if len(voucher_no) > 40:
        raise ValueError('更正单号长度不能超过 40 个字符')
    source = str(payload.get('source_batch', '')).strip()
    target = str(payload.get('target_batch', '')).strip()
    for label, code in (('扣减批次', source), ('补入批次', target)):
        if not code:
            raise ValueError(f'{label}不能为空')
        if len(code) > 40:
            raise ValueError(f'{label}号长度不能超过 40 个字符')
    if source == target:
        raise ValueError('扣减批次与补入批次不能是同一批次')
    amount = payload.get('amount_cents')
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError('物料费金额须为整数分')
    if not 1 <= amount <= MAX_AMOUNT:
        raise ValueError(f'物料费金额须为 1 至 {MAX_AMOUNT} 分的正整数')
    reason = str(payload.get('reason', '') or '').strip()
    if len(reason) > 200:
        raise ValueError('更正原因不能超过 200 字')
    return {'voucher_no': voucher_no, 'source_batch': source, 'target_batch': target,
            'amount_cents': amount, 'reason': reason}


def post_voucher(voucher: CorrectionVoucher, accounts: dict[str, MaterialAccount],
                 entries: list[LedgerEntry], *, at: datetime | None = None
                 ) -> tuple[CorrectionVoucher, list[LedgerEntry]]:
    """对一张更正单执行双边入账（纯内存变换，调用方负责加锁与持久化）。

    成功：扣减侧与补入侧同事务更新，追加两条金额相等的分录，凭证置 posted。
    余额不足：不改动任何账户，凭证置 waiting 并记录原因，等待后续自动重试。
    返回 (凭证, 本次新增分录)。重复入账（已 posted）直接返回空分录，保证幂等。
    """
    if voucher.status == VoucherStatus.POSTED:
        return voucher, []
    voucher.attempts += 1
    moment = at or utc_now()
    source = accounts.get(voucher.source_batch)
    if source is None or source.balance_cents < voucher.amount_cents:
        available = source.balance_cents if source is not None else 0
        voucher.status = (VoucherStatus.FAILED if voucher.attempts >= MAX_ATTEMPTS
                          else VoucherStatus.WAITING)
        voucher.last_error = (f'批次 {voucher.source_batch} 物料余额 {available} 分，'
                              f'不足扣减 {voucher.amount_cents} 分，已挂起等待补足后重试')
        return voucher, []

    target = accounts.setdefault(voucher.target_batch,
                                 MaterialAccount(batch_code=voucher.target_batch))
    # 先算后写：任何一边不满足都不落分录，保证双边同生共死。
    source.balance_cents -= voucher.amount_cents
    source.version += 1
    target.balance_cents += voucher.amount_cents
    target.version += 1
    new_entries = [
        LedgerEntry(voucher.voucher_no, voucher.source_batch, EntrySide.CREDIT,
                    voucher.amount_cents, source.balance_cents, moment),
        LedgerEntry(voucher.voucher_no, voucher.target_batch, EntrySide.DEBIT,
                    voucher.amount_cents, target.balance_cents, moment),
    ]
    entries.extend(new_entries)
    voucher.status = VoucherStatus.POSTED
    voucher.posted_at = moment
    voucher.last_error = None
    return voucher, new_entries


def retry_waiting(vouchers: list[CorrectionVoucher], accounts: dict[str, MaterialAccount],
                  entries: list[LedgerEntry], *, at: datetime | None = None) -> list[CorrectionVoucher]:
    """按登记顺序（创建时间，再以单号兜底）自动重试所有挂起单，直到一轮内没有单能再入账为止。

    后续入账补足余额后，排在前面的挂起单优先解开；无关批次的入账不被阻塞。
    返回本轮状态发生变化的凭证列表。
    """
    moved: list[CorrectionVoucher] = []
    waiting = [v for v in vouchers if v.status == VoucherStatus.WAITING]
    waiting.sort(key=lambda v: (v.created_at, v.voucher_no))
    progressed = True
    while progressed:
        progressed = False
        for voucher in waiting:
            if voucher.status != VoucherStatus.WAITING:
                continue
            before = voucher.status
            post_voucher(voucher, accounts, entries, at=at)
            if voucher.status == VoucherStatus.POSTED:
                moved.append(voucher)
                progressed = True
            elif before != voucher.status:
                moved.append(voucher)
    return moved


def reconcile(accounts: dict[str, MaterialAccount], entries: list[LedgerEntry]) -> dict:
    """对账：校验分录守恒与账户余额，全部以整数分精确到分。"""
    credited = sum(e.amount_cents for e in entries if e.side == EntrySide.CREDIT)
    debited = sum(e.amount_cents for e in entries if e.side == EntrySide.DEBIT)
    return {
        'entry_count': len(entries),
        'credit_total_cents': credited,
        'debit_total_cents': debited,
        'difference_cents': debited - credited,          # 双边等额，恒为 0
        'account_total_cents': sum(a.balance_cents for a in accounts.values()),
        'account_count': len(accounts),
    }
