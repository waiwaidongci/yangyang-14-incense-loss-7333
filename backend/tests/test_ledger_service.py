"""服务层事务流程（行锁、幂等登记、挂起补足后自动重试、对账）的内存复验。

不依赖 SQLAlchemy：用一个带行锁语义的内存仓储驱动与生产同构的入账流程，
覆盖“并发更正不扣成负数、余额不足挂起、补足后自动重试、原单号重试不重复扣款、无关批次照常入账”。
运行：python3 -I tests/test_ledger_service.py
"""
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.constants.enums import EntrySide, VoucherStatus  # noqa: E402
from src.domain import ledger as domain  # noqa: E402


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Account:
    def __init__(self, batch_code, opening):
        self.batch_code = batch_code
        self.balance_cents = opening
        self.version = 0


class Voucher:
    _ids = 0

    def __init__(self, clean, actor):
        Voucher._ids += 1
        self.id = Voucher._ids
        self.voucher_no = clean['voucher_no']
        self.source_batch = clean['source_batch']
        self.target_batch = clean['target_batch']
        self.amount_cents = clean['amount_cents']
        self.reason = clean['reason']
        self.status = VoucherStatus.QUEUED
        self.attempts = 0
        self.last_error = None
        self.created_by = actor
        self.created_at = utc_now()
        self.posted_at = None


class Entry:
    def __init__(self, domain_entry):
        self.voucher_no = domain_entry.voucher_no
        self.batch_code = domain_entry.batch_code
        self.side = domain_entry.side.value
        self.amount_cents = domain_entry.amount_cents
        self.balance_after = domain_entry.balance_after


class LedgerRepository:
    """内存仓储，用一把全局锁 + 批次行锁集合模拟 SELECT ... FOR UPDATE 的串行化。"""

    def __init__(self, opening_balances):
        self._lock = threading.RLock()
        self._accounts = {code: Account(code, bal) for code, bal in opening_balances.items()}
        self._vouchers: dict[str, Voucher] = {}
        self._entries: list[Entry] = []
        self._row_locks: set[str] = set()

    def lock_accounts(self, *batch_codes):
        # 与生产一致：按批次号排序加锁，避免同对账户互锁导致死锁。
        for code in sorted(set(batch_codes)):
            self._row_locks.add(code)

    def unlock_all(self):
        self._row_locks.clear()

    def domain_accounts(self, *batch_codes):
        return {code: domain.MaterialAccount(code, self._accounts[code].balance_cents,
                                             self._accounts[code].version)
                for code in batch_codes}

    def apply(self, voucher: Voucher, new_entries):
        for entry in new_entries:
            account = self._accounts[entry.batch_code]
            account.balance_cents = entry.balance_after
            account.version += 1
            self._entries.append(Entry(entry))
        if voucher.status == VoucherStatus.POSTED:
            voucher.posted_at = utc_now()


class LedgerService:
    """与 src.services.ledger 同构的事务脚本，只是持久化目标换成内存仓储。"""

    def __init__(self, repo: LedgerRepository):
        self.repo = repo

    def register(self, payload, actor):
        clean = domain.validate_voucher(payload)
        with self.repo._lock:
            existing = self.repo._vouchers.get(clean['voucher_no'])
            if existing is not None:
                same = (existing.source_batch == clean['source_batch']
                        and existing.target_batch == clean['target_batch']
                        and existing.amount_cents == clean['amount_cents'])
                if not same:
                    raise ValueError('单号已存在且内容不同，不能覆盖原单')
                return existing, False
            voucher = Voucher(clean, actor)
            self.repo._vouchers[voucher.voucher_no] = voucher
            self._attempt(voucher)
            return voucher, True

    def _attempt(self, voucher: Voucher):
        self.repo.lock_accounts(voucher.source_batch, voucher.target_batch)
        try:
            # 锁内复核余额，确保并发更正不会把账户扣成负数。
            accounts = self.repo.domain_accounts(voucher.source_batch, voucher.target_batch)
            domain_voucher = domain.CorrectionVoucher(
                voucher.voucher_no, voucher.source_batch, voucher.target_batch,
                voucher.amount_cents, voucher.reason, voucher.status, voucher.attempts,
                voucher.last_error, voucher.created_at, voucher.posted_at)
            _, new_entries = domain.post_voucher(domain_voucher, accounts, [])
            voucher.attempts = domain_voucher.attempts
            voucher.status = domain_voucher.status
            voucher.last_error = domain_voucher.last_error
            voucher.posted_at = domain_voucher.posted_at
            self.repo.apply(voucher, new_entries)
        finally:
            self.repo.unlock_all()

    def process_due(self, limit=50):
        acted = 0
        budget = limit
        with self.repo._lock:
            while budget > 0:
                tried = set()
                posted_this_pass = False
                progressed_this_pass = False
                while budget > 0:
                    waiting = sorted(
                        [v for v in self.repo._vouchers.values()
                         if v.status in (VoucherStatus.QUEUED, VoucherStatus.WAITING) and v.id not in tried],
                        key=lambda v: (v.created_at, v.id))
                    if not waiting:
                        break
                    voucher = waiting[0]
                    tried.add(voucher.id)
                    budget -= 1
                    before = (voucher.status, voucher.attempts)
                    self._attempt(voucher)
                    if (voucher.status, voucher.attempts) != before:
                        acted += 1
                        progressed_this_pass = True
                    if voucher.status == VoucherStatus.POSTED:
                        posted_this_pass = True
                if not progressed_this_pass or not posted_this_pass:
                    break
        return acted

    def reconcile(self):
        entries = self.repo._entries
        credit = sum(e.amount_cents for e in entries if e.side == EntrySide.CREDIT.value)
        debit = sum(e.amount_cents for e in entries if e.side == EntrySide.DEBIT.value)
        return {
            'entry_count': len(entries), 'credit_total_cents': credit,
            'debit_total_cents': debit, 'difference_cents': debit - credit,
            'account_total_cents': sum(a.balance_cents for a in self.repo._accounts.values()),
            'waiting_count': sum(1 for v in self.repo._vouchers.values()
                                 if v.status == VoucherStatus.WAITING),
        }

    def balance(self, code):
        return self.repo._accounts[code].balance_cents


class ServiceFlowTests(unittest.TestCase):
    def setUp(self):
        # 期初：A=1000，B=200，C=5000。
        self.service = LedgerService(LedgerRepository({'A': 1000, 'B': 200, 'C': 5000}))

    def test_register_posts_double_entry_and_conserves_total(self):
        voucher, created = self.service.register(
            {'voucher_no': 'GZ-LT-0001', 'source_batch': 'A', 'target_batch': 'B',
             'amount_cents': 300, 'reason': '错记调整'}, 'analyst@demo.local')
        self.assertTrue(created)
        self.assertEqual(voucher.status, VoucherStatus.POSTED)
        self.assertEqual(self.service.balance('A'), 700)
        self.assertEqual(self.service.balance('B'), 500)
        rec = self.service.reconcile()
        self.assertEqual(rec['difference_cents'], 0)
        self.assertEqual(rec['entry_count'], 2)
        self.assertEqual(rec['account_total_cents'], 6200)  # 1000+200+5000 守恒

    def test_duplicate_voucher_no_never_double_charges(self):
        payload = {'voucher_no': 'GZ-LT-0002', 'source_batch': 'A', 'target_batch': 'B',
                   'amount_cents': 100}
        first, created1 = self.service.register(payload, 'analyst@demo.local')
        second, created2 = self.service.register(payload, 'analyst@demo.local')  # 中断后按原单号重试
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertIs(first, second)
        self.assertEqual(self.service.balance('A'), 900)
        self.assertEqual(self.service.reconcile()['entry_count'], 2)

    def test_duplicate_no_with_conflicting_content_is_rejected(self):
        self.service.register({'voucher_no': 'V', 'source_batch': 'A', 'target_batch': 'B',
                               'amount_cents': 100}, 'analyst@demo.local')
        with self.assertRaisesRegex(ValueError, '不能覆盖'):
            self.service.register({'voucher_no': 'V', 'source_batch': 'A', 'target_batch': 'C',
                                   'amount_cents': 100}, 'analyst@demo.local')

    def test_shortfall_waits_never_negative_unrelated_still_posts(self):
        blocked, _ = self.service.register(
            {'voucher_no': 'W1', 'source_batch': 'A', 'target_batch': 'B', 'amount_cents': 1500},
            'analyst@demo.local')
        self.assertEqual(blocked.status, VoucherStatus.WAITING)
        self.assertEqual(self.service.balance('A'), 1000)       # 未扣成负数
        # 无关批次 C 的更正照常入账。
        fine, _ = self.service.register(
            {'voucher_no': 'W2', 'source_batch': 'C', 'target_batch': 'B', 'amount_cents': 500},
            'analyst@demo.local')
        self.assertEqual(fine.status, VoucherStatus.POSTED)
        self.assertEqual(self.service.balance('C'), 4500)
        self.assertEqual(self.service.reconcile()['waiting_count'], 1)

    def test_head_of_line_blocking_does_not_stall_unrelated_vouchers(self):
        # 队首张单（A 余额不足）挂起；同轮处理时排在后面的无关单仍须照常入账。
        head, _ = self.service.register(
            {'voucher_no': 'H1', 'source_batch': 'A', 'target_batch': 'B', 'amount_cents': 9000},
            'analyst@demo.local')
        self.assertEqual(head.status, VoucherStatus.WAITING)
        # 直接以挂起态补两张已登记单（绕过 register 的即时入账，模拟排队队列）。
        tail1 = Voucher(domain.validate_voucher(
            {'voucher_no': 'H2', 'source_batch': 'C', 'target_batch': 'B', 'amount_cents': 400,
             'reason': ''}), 'analyst@demo.local')
        tail1.status = VoucherStatus.QUEUED
        tail2 = Voucher(domain.validate_voucher(
            {'voucher_no': 'H3', 'source_batch': 'C', 'target_batch': 'A', 'amount_cents': 700,
             'reason': ''}), 'analyst@demo.local')
        tail2.status = VoucherStatus.QUEUED
        self.service.repo._vouchers[tail1.voucher_no] = tail1
        self.service.repo._vouchers[tail2.voucher_no] = tail2
        acted = self.service.process_due()
        self.assertEqual(head.status, VoucherStatus.WAITING)   # 队首仍挂起
        self.assertEqual(tail1.status, VoucherStatus.POSTED)   # 无关单不被阻塞
        self.assertEqual(tail2.status, VoucherStatus.POSTED)
        self.assertGreaterEqual(acted, 2)
        self.assertGreaterEqual(self.service.balance('A'), 0)  # A 收到 700 后仍未被超额扣减

    def test_funding_unblocks_waiting_voucher_automatically(self):
        blocked, _ = self.service.register(
            {'voucher_no': 'W3', 'source_batch': 'A', 'target_batch': 'B', 'amount_cents': 1500},
            'analyst@demo.local')
        self.assertEqual(blocked.status, VoucherStatus.WAITING)
        # 后续入账把 A 补足（A 作为补入侧收到 800）。
        self.service.register({'voucher_no': 'F1', 'source_batch': 'C', 'target_batch': 'A',
                               'amount_cents': 800}, 'analyst@demo.local')
        acted = self.service.process_due()
        self.assertGreaterEqual(acted, 1)
        self.assertEqual(blocked.status, VoucherStatus.POSTED)
        self.assertEqual(self.service.balance('A'), 1000 + 800 - 1500)
        self.assertEqual(self.service.reconcile()['difference_cents'], 0)

    def test_concurrent_corrections_on_same_batch_serialize_without_negative(self):
        results = []

        def move(no, amount):
            voucher, _ = self.service.register(
                {'voucher_no': no, 'source_batch': 'A', 'target_batch': 'B', 'amount_cents': amount},
                'analyst@demo.local')
            results.append((no, voucher.status))

        # 三张单合计 1600 > A 的 1000：恰好一张应挂起，其余入账，A 余额绝不小于 0。
        threads = [threading.Thread(target=move, args=(f'C{i}', amount))
                   for i, amount in enumerate((600, 300, 700))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = {no: status for no, status in results}
        self.assertEqual(sorted(statuses.values()).count(VoucherStatus.POSTED), 2)
        self.assertEqual(sorted(statuses.values()).count(VoucherStatus.WAITING), 1)
        self.assertGreaterEqual(self.service.balance('A'), 0)
        # 挂起单在补足后经自动重试入账。
        self.service.register({'voucher_no': 'FUND', 'source_batch': 'C', 'target_batch': 'A',
                               'amount_cents': 1000}, 'analyst@demo.local')
        self.service.process_due()
        self.assertEqual(self.service.balance('A'), 1000 + 1000 - 1600)
        self.assertTrue(all(v.status == VoucherStatus.POSTED
                            for v in self.repo_vouchers().values()))
        self.assertEqual(self.service.reconcile()['difference_cents'], 0)

    def repo_vouchers(self):
        return self.service.repo._vouchers


if __name__ == '__main__':
    unittest.main(verbosity=2)
