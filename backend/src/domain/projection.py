"""把领退料明细账叠加到观测行的纯函数（仅标准库，可离线复验）。"""
from src.constants.enums import EntrySide
from src.domain import ledger as domain


def apply_entries(rows: list[dict], entries: list[domain.LedgerEntry]) -> list[dict]:
    """在观测行的 material_cents 上叠加借/贷分录，返回新行列表（不改入参）。

    debit（补入侧）增加批次物料费，credit（扣减侧）减少；更正单按批次记账，
    因此每个批次的净变动只叠加一次（同一批次若有多条观测行，变动落在该批第一条并在其余行置 0）。
    未入账更正单不产生分录，因此不影响结果。
    """
    delta: dict[str, int] = {}
    for entry in entries:
        sign = 1 if entry.side == EntrySide.DEBIT else -1
        delta[entry.batch_code] = delta.get(entry.batch_code, 0) + sign * entry.amount_cents
    projected = []
    seen_batches: set[str] = set()
    for row in rows:
        code = row['batch_code']
        adjusted = dict(row)
        batch_delta = delta.get(code, 0) if code not in seen_batches else 0
        seen_batches.add(code)
        adjusted['material_cents'] = int(row['material_cents']) + batch_delta
        if adjusted['material_cents'] < 0:
            # 理论上不会发生：credit 仅在余额充足时入账；保留防线避免污染分摊算法。
            raise ValueError(f'批次 {code} 叠加更正单后物料费为负，请核对台账')
        adjusted['ledger_adjustment_cents'] = batch_delta
        projected.append(adjusted)
    return projected
