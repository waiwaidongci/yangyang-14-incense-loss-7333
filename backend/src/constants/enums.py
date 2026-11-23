from enum import StrEnum


class Role(StrEnum):
    VIEWER = 'viewer'
    ANALYST = 'analyst'
    SUPERVISOR = 'supervisor'


class RunStatus(StrEnum):
    QUEUED = 'queued'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'


class Decision(StrEnum):
    APPROVED = 'approved'
    RETURNED = 'returned'


class VoucherStatus(StrEnum):
    QUEUED = 'queued'          # 已登记，等待入账
    POSTED = 'posted'          # 双边已入账
    WAITING = 'waiting'        # 扣减侧余额不足，挂起等待后续入账后自动重试
    FAILED = 'failed'          # 重试超过上限仍未入账


class EntrySide(StrEnum):
    DEBIT = 'debit'            # 增加批次物料余额（补入/退料）
    CREDIT = 'credit'          # 扣减批次物料余额（挪出/领料更正）
