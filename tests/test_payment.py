"""付款排程纯函数测试：整单付清、迟费、信用额度与无解判定。

覆盖题面三个数字情形（当天借 vs 延期、退回后费用 0、额度收紧只能次日），
以及两合同争同日余额、信用费不进次日现金、结尾余额不得为负等边界。
"""

from __future__ import annotations

import unittest

from orgbudget.errors import InfeasibleSchedule, InvalidContract
from orgbudget.payment import Bill, schedule_payments


def make_bill(
    bill_id: str,
    amount: int,
    *,
    earliest: int = 1,
    latest: int = 8,
    preferred: int = 1,
    late_fee: int = 0,
) -> Bill:
    return Bill(bill_id, amount, earliest, latest, preferred, late_fee)


class WorkedExampleTest(unittest.TestCase):
    """题面例子：承诺 10、旧库实付 2、合同 3 件验收 2 件 -> 应付 4。"""

    def bill(self) -> Bill:
        # 窗口第 1~2 天，首选第 1 天，迟一天费 4。
        return make_bill("A", 4, earliest=1, latest=2, preferred=1, late_fee=4)

    def test_borrow_today_cheaper_than_delay(self) -> None:
        # 持现金 1，次日开账前入 3，额度 3、费率 1。
        sched = schedule_payments(
            [self.bill()], horizon_days=2, initial_cash=1,
            deposits={2: 3}, credit_limit=3, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days, {"A": 1})
        # 当天借 3：信用费 3，比延期迟费 4 便宜。
        self.assertEqual(sched.total_fee, 3)
        self.assertEqual(sched.total_late_fee, 0)
        self.assertEqual(sched.total_credit_fee, 3)
        self.assertEqual(sched.closing_balances, (-3, 0))
        # 信用费独立存放，不进次日现金：次日开账仍是 -3，加存入 3 归零。
        self.assertEqual(sched.ledgers[1].opening_cash, -3)
        self.assertEqual(sched.ledgers[1].closing_balance, 0)

    def test_return_one_then_due_one_pays_free(self) -> None:
        # 固定出纳单：退回一件后应付 1，当天现金 1 即可付清，费用 0。
        bill = make_bill("A", 1, earliest=1, latest=2, preferred=1, late_fee=4)
        sched = schedule_payments(
            [bill], horizon_days=2, initial_cash=1,
            deposits={2: 3}, credit_limit=3, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days, {"A": 1})
        self.assertEqual(sched.total_fee, 0)
        self.assertEqual(sched.closing_balances[0], 0)

    def test_tight_credit_limit_forces_next_day(self) -> None:
        # 恢复原验收（应付 4），仅把信用额度改成 2：当天最多下探 -2，
        # 现金 1 凑不齐 4，只能次日付，迟费 4。
        sched = schedule_payments(
            [self.bill()], horizon_days=2, initial_cash=1,
            deposits={2: 3}, credit_limit=2, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days, {"A": 2})
        self.assertEqual(sched.total_fee, 4)
        self.assertEqual(sched.total_late_fee, 4)
        self.assertEqual(sched.total_credit_fee, 0)
        self.assertEqual(sched.closing_balances, (1, 0))


class LateFeeTest(unittest.TestCase):
    def test_late_fee_counts_days_after_preferred(self) -> None:
        bill = make_bill("A", 5, earliest=1, latest=4, preferred=1, late_fee=2)
        # 现金只够第 3 天付（第 1、2 天无入款，额度 0）。
        sched = schedule_payments(
            [bill], horizon_days=4, initial_cash=0,
            deposits={3: 5}, credit_limit=0, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days, {"A": 3})
        # 迟 (3-1)=2 天 × 2 = 4。
        self.assertEqual(sched.total_late_fee, 4)

    def test_paying_on_preferred_or_earlier_has_no_late_fee(self) -> None:
        bill = make_bill("A", 5, earliest=1, latest=4, preferred=3, late_fee=2)
        sched = schedule_payments(
            [bill], horizon_days=4, initial_cash=5,
            deposits={}, credit_limit=0, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days["A"], 3)
        self.assertEqual(sched.total_late_fee, 0)


class TwoBillsTest(unittest.TestCase):
    """两合同争同日余额：DP 必须在单付 / 合付之间权衡。"""

    def test_two_bills_split_across_days(self) -> None:
        # 两张各 5 的单，首选第 1 天，迟费每天 1；初始现金 5、额度 0，
        # 第 2 天入 5。当天只能付一张，另一张次日付，恰一张产生迟费 1。
        bills = [
            make_bill("X", 5, earliest=1, latest=2, preferred=1, late_fee=1),
            make_bill("Y", 5, earliest=1, latest=2, preferred=1, late_fee=1),
        ]
        sched = schedule_payments(
            bills, horizon_days=2, initial_cash=5,
            deposits={2: 5}, credit_limit=0, credit_rate=1,
        )
        days = sched.bill_pay_days
        self.assertEqual(sorted(days), ["X", "Y"])
        self.assertEqual(sorted(days.values()), [1, 2])
        self.assertEqual(sched.total_late_fee, 1)
        self.assertEqual(sched.total_credit_fee, 0)
        self.assertEqual(sched.closing_balances, (0, 0))

    def test_two_bills_same_day_uses_credit_when_cheaper(self) -> None:
        # 两单各 5、首选当天、迟费各 10（很重）；现金 6、额度 4、费率 1。
        # 当天合付 10：余额 -4，信用费 4；拆开则一张迟费 10，故合付更省。
        bills = [
            make_bill("X", 5, earliest=1, latest=2, preferred=1, late_fee=10),
            make_bill("Y", 5, earliest=1, latest=2, preferred=1, late_fee=10),
        ]
        sched = schedule_payments(
            bills, horizon_days=2, initial_cash=6,
            deposits={2: 100}, credit_limit=4, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days, {"X": 1, "Y": 1})
        self.assertEqual(sched.total_credit_fee, 4)
        self.assertEqual(sched.total_late_fee, 0)
        self.assertEqual(sched.total_fee, 4)

    def test_windows_force_which_bill_pays_first(self) -> None:
        # X 只能第 1 天付，Y 窗口 1~2；现金 5 只够一张，额度 0。
        # X 窗口锁死第 1 天，因此 X 当天付、Y 次日付。
        bills = [
            make_bill("X", 5, earliest=1, latest=1, preferred=1, late_fee=1),
            make_bill("Y", 5, earliest=1, latest=2, preferred=1, late_fee=1),
        ]
        sched = schedule_payments(
            bills, horizon_days=2, initial_cash=5,
            deposits={2: 5}, credit_limit=0, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days["X"], 1)
        self.assertEqual(sched.bill_pay_days["Y"], 2)


class InfeasibilityTest(unittest.TestCase):
    def test_insufficient_cash_even_with_credit(self) -> None:
        # 应付 10，现金 1、额度 3（最多下探 -2 净付能力）、无入款，无解。
        bill = make_bill("A", 10, earliest=1, latest=2, preferred=1, late_fee=1)
        with self.assertRaises(InfeasibleSchedule):
            schedule_payments(
                [bill], horizon_days=2, initial_cash=1,
                deposits={}, credit_limit=3, credit_rate=1,
            )

    def test_window_outside_horizon_rejected(self) -> None:
        bill = make_bill("A", 5, earliest=9, latest=10, preferred=9)
        with self.assertRaises(InvalidContract):
            schedule_payments(
                [bill], horizon_days=8, initial_cash=100,
                deposits={}, credit_limit=0, credit_rate=1,
            )

    def test_negative_closing_balance_on_last_day_forbidden(self) -> None:
        # 唯一能付的日子是最后一天，但付完结尾为负（额度不允许跨末日）。
        bill = make_bill("A", 8, earliest=2, latest=2, preferred=2)
        with self.assertRaises(InfeasibleSchedule):
            schedule_payments(
                [bill], horizon_days=2, initial_cash=5,
                deposits={}, credit_limit=10, credit_rate=1,
            )

    def test_credit_allowed_intraday_but_settled_by_end(self) -> None:
        # 第 1 天借信用付，第 2 天入款把结尾拉回非负：合法。
        bill = make_bill("A", 6, earliest=1, latest=2, preferred=1, late_fee=100)
        sched = schedule_payments(
            [bill], horizon_days=2, initial_cash=2,
            deposits={2: 4}, credit_limit=4, credit_rate=1,
        )
        self.assertEqual(sched.bill_pay_days["A"], 1)
        self.assertEqual(sched.ledgers[0].closing_balance, -4)
        self.assertEqual(sched.ledgers[1].closing_balance, 0)
        self.assertEqual(sched.total_credit_fee, 4)


class CreditFeeSemanticsTest(unittest.TestCase):
    def test_credit_fee_not_rolled_into_next_cash(self) -> None:
        bill = make_bill("A", 4, earliest=1, latest=2, preferred=1, late_fee=4)
        sched = schedule_payments(
            [bill], horizon_days=2, initial_cash=1,
            deposits={2: 3}, credit_limit=3, credit_rate=1,
        )
        # 第 1 天信用费 3 独立存放；第 2 天开账现金不被扣这 3。
        self.assertEqual(sched.ledgers[1].opening_cash, -3)
        self.assertEqual(sched.ledgers[1].deposit, 3)
        self.assertEqual(sched.ledgers[1].closing_balance, 0)

    def test_zero_rate_means_free_credit(self) -> None:
        # 费率 0：信用免费；用重迟费逼其第 1 天借信用付，第 2 天入款补回。
        bill = make_bill("A", 10, earliest=1, latest=2, preferred=1, late_fee=100)
        sched = schedule_payments(
            [bill], horizon_days=2, initial_cash=0,
            deposits={2: 10}, credit_limit=10, credit_rate=0,
        )
        self.assertEqual(sched.bill_pay_days["A"], 1)
        self.assertEqual(sched.total_credit_fee, 0)
        self.assertEqual(sched.ledgers[0].closing_balance, -10)
        self.assertEqual(sched.ledgers[1].closing_balance, 0)

    def test_input_validation(self) -> None:
        good = make_bill("A", 5)
        with self.assertRaises(InvalidContract):
            schedule_payments(
                [good], horizon_days=0, initial_cash=5, deposits={},
                credit_limit=0, credit_rate=1,
            )
        with self.assertRaises(InvalidContract):
            schedule_payments(
                [good], horizon_days=8, initial_cash=-1, deposits={},
                credit_limit=0, credit_rate=1,
            )
        with self.assertRaises(InvalidContract):
            schedule_payments(
                [good, good], horizon_days=8, initial_cash=100, deposits={},
                credit_limit=0, credit_rate=1,
            )


def _brute_force(bills, horizon, initial_cash, deposits, credit_limit, credit_rate):
    """独立实现：枚举每张单的付款日（含“不付”），返回最小费用或 None。"""
    import itertools

    choices = []
    for b in bills:
        days = list(range(b.earliest_day, b.latest_day + 1))
        choices.append(days)  # 每张单必须在窗口内付，无“不付”选项

    best = None
    for combo in itertools.product(*choices):
        cash = initial_cash
        total = 0
        feasible = True
        for day in range(1, horizon + 1):
            cash += deposits.get(day, 0)
            paid = 0
            late = 0
            for b, pay_day in zip(bills, combo):
                if pay_day == day:
                    paid += b.amount
                    late += b.late_fee_on(day)
            closing = cash - paid
            floor = 0 if day == horizon else -credit_limit
            if closing < floor:
                feasible = False
                break
            total += late + max(0, -closing) * credit_rate
            cash = closing  # 信用费不进次日现金
        if feasible and (best is None or total < best):
            best = total
    return best


class BruteForceCrossCheckTest(unittest.TestCase):
    """随机小规模问题：DP 最优值必须与暴力枚举完全一致，无解也要吻合。"""

    def test_dp_matches_brute_force(self) -> None:
        import random

        rng = random.Random(20261121)
        for _ in range(300):
            n = rng.randint(1, 4)
            horizon = rng.randint(2, 5)
            bills = []
            for i in range(n):
                e = rng.randint(1, horizon)
                l = rng.randint(e, horizon)
                p = rng.randint(e, l)
                bills.append(
                    Bill(
                        f"B{i}",
                        rng.randint(1, 8),
                        e, l, p,
                        rng.randint(0, 4),
                    )
                )
            initial_cash = rng.randint(0, 10)
            deposits = {
                d: rng.randint(0, 8)
                for d in range(1, horizon + 1) if rng.random() < 0.5
            }
            credit_limit = rng.randint(0, 10)
            credit_rate = rng.randint(0, 3)

            expected = _brute_force(
                bills, horizon, initial_cash, deposits,
                credit_limit, credit_rate,
            )
            if expected is None:
                with self.assertRaises(InfeasibleSchedule):
                    schedule_payments(
                        bills, horizon_days=horizon, initial_cash=initial_cash,
                        deposits=deposits, credit_limit=credit_limit,
                        credit_rate=credit_rate,
                    )
            else:
                sched = schedule_payments(
                    bills, horizon_days=horizon, initial_cash=initial_cash,
                    deposits=deposits, credit_limit=credit_limit,
                    credit_rate=credit_rate,
                )
                self.assertEqual(
                    sched.total_fee, expected,
                    msg=f"bills={bills} cash={initial_cash} dep={deposits} "
                        f"limit={credit_limit} rate={credit_rate}",
                )
                # 每张单都恰好付一次，且落在窗口内。
                self.assertEqual(len(sched.bill_pay_days), n)
                for b in bills:
                    d = sched.bill_pay_days[b.bill_id]
                    self.assertTrue(b.earliest_day <= d <= b.latest_day)


if __name__ == "__main__":
    unittest.main()
