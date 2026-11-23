"""更正单叠加到观测台账后重算拆因的离线复验（标准库即可）。

运行：python3 -I tests/test_projection_analysis.py
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.constants.enums import EntrySide  # noqa: E402
from src.domain import ledger as L  # noqa: E402
from src.domain.analysis import analyze  # noqa: E402
from src.domain.projection import apply_entries  # noqa: E402


def at(hour):
    return datetime(2026, 11, 23, hour, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)


def row(code, team, material, weights='干燥开裂=1,断条=1', minutes=60, rate=6000):
    return {'batch_code': code, 'team': team, 'rework_date': '2026-11-16',
            'labor_minutes': minutes, 'hourly_labor_cents': rate,
            'material_cents': material, 'cause_weights': weights}


class ProjectionTests(unittest.TestCase):
    def test_entries_move_material_between_batches_but_keep_total(self):
        rows = [row('GZ-2611-01', '干燥乙班', 1000), row('YX-2611-01', '压线甲班', 400)]
        accounts = {'GZ-2611-01': L.MaterialAccount('GZ-2611-01', 1000),
                    'YX-2611-01': L.MaterialAccount('YX-2611-01', 400)}
        entries = []
        L.post_voucher(L.CorrectionVoucher('GZ-LT-1', 'GZ-2611-01', 'YX-2611-01', 300,
                                           created_at=at(1)), accounts, entries, at=at(2))
        projected = apply_entries(rows, entries)
        by_code = {r['batch_code']: r for r in projected}
        self.assertEqual(by_code['GZ-2611-01']['material_cents'], 700)   # 扣回 300
        self.assertEqual(by_code['YX-2611-01']['material_cents'], 700)   # 补入 300
        self.assertEqual(sum(r['material_cents'] for r in projected), 1400)  # 总额不变

    def test_analysis_recomputes_on_projected_ledger_and_reconciles_to_cent(self):
        rows = [row('GZ-2611-01', '干燥乙班', 10001), row('GZ-2611-02', '干燥乙班', 8000)]
        accounts = {r['batch_code']: L.MaterialAccount(r['batch_code'], r['material_cents']) for r in rows}
        entries = []
        # 把 2500 分从 01 批挪到 02 批。
        L.post_voucher(L.CorrectionVoucher('GZ-LT-9', 'GZ-2611-01', 'GZ-2611-02', 2500,
                                           created_at=at(1)), accounts, entries, at=at(2))
        result = analyze(apply_entries(rows, entries), {'priority_coverage_pct': 80})
        metrics = {m['label']: m['value'] for m in result['metrics']}
        # 物料总额守恒：账面与分摊仍精确对齐，差额为 0 分。
        self.assertEqual(metrics['对账差额（分摊减账面）'], 0)
        self.assertEqual(metrics['账面损失'], metrics['分摊损失'])
        # 账面行反映更正后的物料费。
        book = {r['batch_code']: r['material_cents'] for r in result['rows'] if r['kind'] == '批次账面'}
        self.assertEqual(book['GZ-2611-01'], 7501)
        self.assertEqual(book['GZ-2611-02'], 10500)

    def test_queued_correction_is_excluded_from_snapshot(self):
        rows = [row('GZ-2611-01', '干燥乙班', 1000)]
        # 未入账（queued/waiting）的更正单不产生分录，投影后金额保持台账原值。
        projected = apply_entries(rows, [])
        self.assertEqual(projected[0]['material_cents'], 1000)
        self.assertEqual(projected[0]['ledger_adjustment_cents'], 0)

    def test_batch_delta_is_applied_once_even_with_multiple_rows_for_batch(self):
        # 同一批次若有多条观测行，批次级净变动只叠加一次，不能每行都加一遍。
        rows = [row('GZ-2611-01', '干燥乙班', 1000), row('GZ-2611-01', '干燥乙班', 500)]
        entries = [L.LedgerEntry('GZ-LT-1', 'GZ-2611-01', EntrySide.DEBIT, 300, 0, at(1))]
        projected = apply_entries(rows, entries)
        self.assertEqual([r['material_cents'] for r in projected], [1300, 500])
        self.assertEqual([r['ledger_adjustment_cents'] for r in projected], [300, 0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
