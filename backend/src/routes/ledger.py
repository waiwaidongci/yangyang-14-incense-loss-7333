from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from src.constants.enums import Role
from src.controllers.views import account_view, entry_view, voucher_view
from src.domain import ledger as domain
from src.models.base import Session
from src.models.workflow import CorrectionVoucher, LedgerEntry, MaterialAccount
from src.services.auth import current_user, roles
from src.services.broker import wake_worker
from src.services.errors import BusinessError
from src.services.ledger import ledger_reconcile, process_due_vouchers, register_voucher

router = APIRouter(prefix='/api/ledger')
editor = roles(Role.ANALYST, Role.SUPERVISOR)


@router.post('/vouchers', status_code=201)
def create_voucher(payload: dict, user=Depends(editor)):
    """登记领退料更正单并立即尝试双边入账；余额不足自动挂起，不重复扣款。"""
    try:
        domain.validate_voucher(payload)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc
    with Session.begin() as db:
        voucher, created = register_voucher(db, payload, user['sub'])
        view = voucher_view(voucher)
        view['created'] = created
    wake_worker()
    return view


@router.get('/vouchers')
def list_vouchers(status: str | None = None, offset: int = Query(0, ge=0),
                  limit: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    with Session() as db:
        query = select(CorrectionVoucher)
        if status is not None:
            query = query.where(CorrectionVoucher.status == status)
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = db.scalars(query.order_by(CorrectionVoucher.id.desc()).offset(offset).limit(limit))
        return {'total': total, 'offset': offset, 'items': [voucher_view(row) for row in rows]}


@router.get('/vouchers/{voucher_no}')
def voucher_detail(voucher_no: str, user=Depends(current_user)):
    with Session() as db:
        voucher = db.scalar(select(CorrectionVoucher).where(CorrectionVoucher.voucher_no == voucher_no))
        if voucher is None:
            raise BusinessError('更正单不存在', 404)
        entries = db.scalars(select(LedgerEntry).where(LedgerEntry.voucher_no == voucher_no)
                             .order_by(LedgerEntry.id))
        return {'voucher': voucher_view(voucher), 'entries': [entry_view(e) for e in entries]}


@router.post('/vouchers/retry')
def retry_all(user=Depends(editor)):
    """手动触发一轮挂起单重试；worker 也会在每轮自动调用。"""
    with Session.begin() as db:
        acted = process_due_vouchers(db)
    wake_worker()
    return {'processed': acted}


@router.get('/accounts')
def list_accounts(user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(MaterialAccount).order_by(MaterialAccount.batch_code))
        return [account_view(row) for row in rows]


@router.get('/accounts/{batch_code}')
def account_detail(batch_code: str, user=Depends(current_user)):
    with Session() as db:
        account = db.scalar(select(MaterialAccount).where(MaterialAccount.batch_code == batch_code))
        if account is None:
            raise BusinessError('该批次暂无物料账户', 404)
        entries = db.scalars(select(LedgerEntry).where(LedgerEntry.batch_code == batch_code)
                             .order_by(LedgerEntry.id))
        return {'account': account_view(account), 'entries': [entry_view(e) for e in entries]}


@router.get('/entries')
def list_entries(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                 user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(LedgerEntry).order_by(LedgerEntry.id.desc()).offset(offset).limit(limit))
        return {'items': [entry_view(row) for row in rows]}


@router.get('/reconcile')
def reconcile(user=Depends(current_user)):
    """物料费总额对账，借/贷差额精确到分须为 0。"""
    with Session() as db:
        return ledger_reconcile(db)
