"""领退料更正单双边入账的离线复验。

运行（在 backend 目录下，标准库即可，无需第三方依赖；-I 忽略环境变量，路径在下方自举）：
    python3 -I tests/test_ledger_engine.py
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.constants.enums import EntrySide, VoucherStatus  # noqa: E402
from src.domain import ledger as L  # noqa: E402


def at(hour):
    return datetime(2026, 11, 23, hour, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)


class ValidationTests(unittest.TestCase):
    def test_accepts_well_formed_voucher(self):
        clean = L.validate_voucher({'voucher_no': 'GZ-LT-0001', 'source_batch': 'GZ-2611-01',
                                    'target_batch': 'YX-2611-02', 'amount_cents': 500, 'reason': '错记调整'})
        self.assertEqual(clean['amount_cents'], 500)

    def test_rejects_same_source_and_target(self):
        with self.assertRaisesRegex(ValueError, '同一批次'):
            L.validate_voucher({'voucher_no': 'V1', 'source_batch': 'A', 'target_batch': 'A',
                                'amount_cents': 10})

    def test_rejects_non_positive_or_non_integer_amount(self):
        base = {'voucher_no': 'V1', 'source_batch': 'A', 'target_batch': 'B'}
        for bad in (0, -5, 1.5, '100', True, None):
            with self.assertRaises(ValueError):
                L.validate_voucher({**base, 'amount_cents': bad})

    def test_rejects_blank_code_and_no(self):
        with self.assertRaises(ValueError):
            L.validate_voucher({'voucher_no': ' ', 'source_batch': 'A', 'target_batch': 'B', 'amount_cents': 1})
        with self.assertRaises(ValueError):
            L.validate_voucher({'voucher_no': 'V', 'source_batch': ' ', 'target_batch': 'B', 'amount_cents': 1})


class PostingTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {'A': L.MaterialAccount('A', 1000), 'B': L.MaterialAccount('B', 200)}
        self.entries = []

    def test_double_entry_moves_amount_and_conserves_total(self):
        v = L.CorrectionVoucher('V1', 'A', 'B', 300, created_at=at(1))
        _, new_entries = L.post_voucher(v, self.accounts, self.entries, at=at(2))
        self.assertEqual(v.status, VoucherStatus.POSTED)
        self.assertEqual(self.accounts['A'].balance_cents, 700)
        self.assertEqual(self.accounts['B'].balance_cents, 500)
        self.assertEqual(len(new_entries), 2)
        credit, debit = new_entries
        self.assertEqual(credit.side, EntrySide.CREDIT)
        self.assertEqual(debit.side, EntrySide.DEBIT)
        self.assertEqual(credit.balance_after, 700)
        self.assertEqual(debit.balance_after, 500)
        rec = L.reconcile(self.accounts, self.entries)
        self.assertEqual(rec['difference_cents'], 0)
        self.assertEqual(rec['account_total_cents'], 1200)  # 总额守恒到分

    def test_balance_shortfall_waives_without_touching_any_account(self):
        v = L.CorrectionVoucher('V2', 'A', 'B', 5000, created_at=at(1))
        _, new_entries = L.post_voucher(v, self.accounts, self.entries, at=at(2))
        self.assertEqual(v.status, VoucherStatus.WAITING)
        self.assertEqual(v.attempts, 1)
        self.assertEqual(new_entries, [])
        self.assertEqual(self.accounts['A'].balance_cents, 1000)  # 未被扣成负数
        self.assertEqual(self.accounts['B'].balance_cents, 200)
        self.assertIn('不足扣减', v.last_error)
        self.assertEqual(L.reconcile(self.accounts, self.entries)['entry_count'], 0)

    def test_never_goes_negative_when_balance_is_zero_or_missing(self):
        v = L.CorrectionVoucher('V3', 'GHOST', 'A', 10, created_at=at(1))
        L.post_voucher(v, self.accounts, self.entries, at=at(2))
        self.assertEqual(v.status, VoucherStatus.WAITING)
        self.assertNotIn('GHOST', self.accounts)  # 余额不足时不创建空账户

    def test_posted_voucher_is_idempotent(self):
        v = L.CorrectionVoucher('V4', 'A', 'B', 100, created_at=at(1))
        L.post_voucher(v, self.accounts, self.entries, at=at(2))
        _, again = L.post_voucher(v, self.accounts, self.entries, at=at(3))
        self.assertEqual(again, [])                          # 中断后按原单号重试不重复扣款
        self.assertEqual(self.accounts['A'].balance_cents, 900)
        self.assertEqual(len(self.entries), 2)

    def test_retry_unlocks_after_balance_is_funded(self):
        # A 只有 1000，先挂起一张 1500 的单。
        waiting = L.CorrectionVoucher('W1', 'A', 'B', 1500, created_at=at(1))
        L.post_voucher(waiting, self.accounts, self.entries, at=at(2))
        self.assertEqual(waiting.status, VoucherStatus.WAITING)
        # 另一张无关单（C -> B 中 B 是补入侧）先照常入账，给 A 无关的链路不受影响。
        self.accounts['C'] = L.MaterialAccount('C', 2000)
        ok = L.CorrectionVoucher('W2', 'C', 'B', 150, created_at=at(2))
        L.post_voucher(ok, self.accounts, self.entries, at=at(3))
        self.assertEqual(ok.status, VoucherStatus.POSTED)
        # 后续入账把 A 补足（模拟 A 作为补入侧收到一笔）。
        fund = L.CorrectionVoucher('W3', 'C', 'A', 600, created_at=at(3))
        L.post_voucher(fund, self.accounts, self.entries, at=at(4))
        moved = L.retry_waiting([waiting], self.accounts, self.entries, at=at(5))
        self.assertEqual([v.voucher_no for v in moved], ['W1'])
        self.assertEqual(waiting.status, VoucherStatus.POSTED)
        self.assertEqual(self.accounts['A'].balance_cents, 1000 - 1500 + 600)
        self.assertEqual(L.reconcile(self.accounts, self.entries)['difference_cents'], 0)

    def test_unrelated_batches_post_while_another_is_blocked(self):
        blocked = L.CorrectionVoucher('B1', 'A', 'B', 9999, created_at=at(1))
        L.post_voucher(blocked, self.accounts, self.entries, at=at(2))
        self.accounts['D'] = L.MaterialAccount('D', 80)
        fine = L.CorrectionVoucher('B2', 'D', 'B', 80, created_at=at(2))
        L.post_voucher(fine, self.accounts, self.entries, at=at(3))
        self.assertEqual(blocked.status, VoucherStatus.WAITING)
        self.assertEqual(fine.status, VoucherStatus.POSTED)

    def test_retry_order_follows_registration_sequence(self):
        self.accounts['X'] = L.MaterialAccount('X', 10)
        first = L.CorrectionVoucher('R1', 'X', 'B', 50, created_at=at(1))
        second = L.CorrectionVoucher('R2', 'X', 'B', 50, created_at=at(2))
        L.post_voucher(first, self.accounts, self.entries, at=at(3))
        L.post_voucher(second, self.accounts, self.entries, at=at(4))
        self.assertEqual(first.status, VoucherStatus.WAITING)
        self.assertEqual(second.status, VoucherStatus.WAITING)
        # 给 X 补 100，重试应按登记顺序先解 R1 再解 R2。
        self.accounts['Y'] = L.MaterialAccount('Y', 200)
        L.post_voucher(L.CorrectionVoucher('R0', 'Y', 'X', 100, created_at=at(5)),
                       self.accounts, self.entries, at=at(6))
        moved = L.retry_waiting([second, first], self.accounts, self.entries, at=at(7))
        self.assertEqual([v.voucher_no for v in moved], ['R1', 'R2'])
        self.assertEqual(self.accounts['X'].balance_cents, 10 + 100 - 50 - 50)

    def test_attempts_cap_marks_failed(self):
        v = L.CorrectionVoucher('F1', 'A', 'B', 9999, created_at=at(1))
        for _ in range(L.MAX_ATTEMPTS - 1):
            L.post_voucher(v, self.accounts, self.entries, at=at(2))
            self.assertEqual(v.status, VoucherStatus.WAITING)
        L.post_voucher(v, self.accounts, self.entries, at=at(3))
        self.assertEqual(v.status, VoucherStatus.FAILED)
        self.assertEqual(v.attempts, L.MAX_ATTEMPTS)


class ReconcileTests(unittest.TestCase):
    def test_credit_and_debit_totals_match_to_the_cent(self):
        accounts = {'A': L.MaterialAccount('A', 5000), 'B': L.MaterialAccount('B', 0),
                    'C': L.MaterialAccount('C', 3000)}
        entries = []
        for i, (src, dst, amt) in enumerate((('A', 'B', 123), ('C', 'A', 457), ('B', 'C', 89))):
            L.post_voucher(L.CorrectionVoucher(f'N{i}', src, dst, amt, created_at=at(i)),
                           accounts, entries, at=at(i + 1))
        rec = L.reconcile(accounts, entries)
        self.assertEqual(rec['credit_total_cents'], rec['debit_total_cents'])
        self.assertEqual(rec['difference_cents'], 0)
        self.assertEqual(rec['entry_count'], 6)
        self.assertEqual(rec['account_total_cents'], 8000)


if __name__ == '__main__':
    unittest.main(verbosity=2)
