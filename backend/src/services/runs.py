import json
import logging

from sqlalchemy import select

from src.config.project import SPEC
from src.constants.enums import Decision, Role, RunStatus, VoucherStatus
from src.domain.analysis import analyze
from src.models.base import utc_now
from src.models.workflow import AnalysisRun, AuditEvent, CorrectionVoucher, Observation, Review
from src.services.errors import BusinessError
from src.services.projection import project_for_run
from src.services.validation import validated

logger = logging.getLogger(__name__)


def create_run(db, request, actor):
    parameters = validated(SPEC['parameters'], request.parameters, defaults=True)
    sample_ids = list(db.scalars(select(Observation.id).order_by(Observation.id)))
    if not sample_ids:
        raise BusinessError('请先导入观测记录')
    # 固化发起时点的账：只认此刻已入账的更正单，排队/挂起单不进分析。
    posted = list(db.scalars(
        select(CorrectionVoucher).where(CorrectionVoucher.status == VoucherStatus.POSTED)
        .order_by(CorrectionVoucher.id)))
    last_voucher_id = posted[-1].id if posted else None
    voucher_nos = [voucher.voucher_no for voucher in posted]
    item = AnalysisRun(title=request.title, owner=actor, parameters=parameters, observation_ids=sample_ids,
                       last_observation_id=sample_ids[-1], sample_size=len(sample_ids),
                       last_voucher_id=last_voucher_id, voucher_nos=voucher_nos)
    db.add(item)
    db.flush()
    db.add(AuditEvent(actor=actor, action='run.created',
                      detail=f'分析 #{item.id}，固化 {item.sample_size} 条观测、{len(voucher_nos)} 张已入账更正单'))
    return item


def visible_runs(db, user, run_id=None):
    query = select(AnalysisRun, Review).outerjoin(Review, Review.run_id == AnalysisRun.id)
    if user['role'] == Role.VIEWER:
        query = query.where(Review.decision == Decision.APPROVED)
    if run_id is not None:
        pair = db.execute(query.where(AnalysisRun.id == run_id)).first()
        if pair is None:
            raise BusinessError('没有可查看的分析结果', 404)
        return pair
    return db.execute(query.order_by(AnalysisRun.id.desc()).limit(100)).all()


def submit_review(db, run_id, request, actor):
    row = db.get(AnalysisRun, run_id, with_for_update=True)
    if row is None:
        raise BusinessError('分析不存在', 404)
    if row.status != RunStatus.SUCCEEDED:
        raise BusinessError('分析算完后才能复核', 409)
    if db.scalar(select(Review).where(Review.run_id == run_id)):
        raise BusinessError('这份分析已完成复核。请发起新分析并重新复核。', 409)
    if row.owner == actor:
        raise BusinessError('分析须由另一位负责人复核', 409)
    review = Review(run_id=run_id, decision=request.decision, note=request.note, reviewer=actor)
    db.add(review)
    db.add(AuditEvent(actor=actor, action=f'review.{request.decision}', detail=f'分析 #{run_id}'))
    db.flush()
    return review


def run_pending(db):
    item = db.scalar(select(AnalysisRun).where(AnalysisRun.status == RunStatus.QUEUED)
                     .order_by(AnalysisRun.id).limit(1).with_for_update(skip_locked=True))
    if item is None:
        return False
    try:
        # 用发起时固化的观测清单与凭证时点重算，后续入账不影响这份分析。
        sample = project_for_run(db, item.observation_ids, item.last_voucher_id)
        result = analyze(sample, item.parameters)
        if not {'headline', 'summary', 'metrics', 'columns', 'rows'} <= set(result):
            raise RuntimeError('invalid result schema')
        result['ledger_snapshot'] = {
            'last_voucher_id': item.last_voucher_id,
            'voucher_nos': list(item.voucher_nos or []),
            'basis': '按分析发起时已入账的领退料更正单重算物料费，未入账单据不计入',
        }
        json.dumps(result, allow_nan=False, ensure_ascii=False)
        item.result = result
        item.status = RunStatus.SUCCEEDED
    except ValueError as exc:
        item.error = str(exc)[:600] or '本次观测数据不足以完成计算'
        item.status = RunStatus.FAILED
    except Exception:
        logger.exception('Analysis %s raised unexpectedly', item.id)
        item.error = '分析计算失败，请检查记录和参数后重试'
        item.status = RunStatus.FAILED
    item.finished_at = utc_now()
    return True
