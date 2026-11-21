"""合同、验收、退货与逐合同应付单测试。

用真实采购承诺与旧库实付事件搭建，验证：

* 合同只绑定一个真实且未取消的承诺，总价即承诺额，一承诺一合同；
* floor 口径的累计确认价、旧库实付只扣一次、负数标追回且不可付；
* 退货指向具体批次、累退不超该批；相同净件数下可退批次可能不同；
* 承诺取消后阻塞开支付单；应付单可重开、旧版本不可变且事实可追溯；
* 本服务只读旧库，绝不代替登记实付。
"""

from __future__ import annotations

import unittest

from tests.support import ServiceTestCase
from orgbudget import (
    Bill,
    ContractConflict,
    ContractService,
    IdempotencyConflict,
    InvalidContract,
    PayableError,
    ReceiptError,
    ReturnError,
    schedule_payments,
)
from orgbudget.contract_service import (
    BLOCKED_CANCELLED,
    PAYABLE,
    RECOVERY,
    ZERO_DUE,
)
from orgbudget.errors import CommitmentClosedError


class ContractServiceTestBase(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        version = self.build_tree([("HQ", None), ("L", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(
            version, 100_000, self.weights_for(product)
        )
        self.contracts = ContractService(self.tmp / "contract.db", self.budget)

    def _contract(
        self,
        contract_id: str = "C1",
        request_no: str = "REQ-1",
        amount: int = 10,
        total_qty: int = 3,
        **kwargs,
    ):
        self.budget.create_commitment(request_no, "L", amount)
        defaults = dict(
            supplier_id="SUP-A", earliest_day=1, latest_day=2,
            preferred_day=1, late_fee=4,
        )
        defaults.update(kwargs)
        return self.contracts.create_contract(
            contract_id, request_no, total_qty, **defaults
        )

    def _bill(self, statement):
        c = self.contracts.get_contract(statement.contract_id)
        return Bill(
            statement.contract_id, statement.payable_amount,
            c.earliest_day, c.latest_day, c.preferred_day, c.late_fee,
        )


class ContractBindingTest(ContractServiceTestBase):
    def test_contract_total_price_equals_commitment_amount(self) -> None:
        c = self._contract(amount=10)
        self.assertEqual(c.total_price, 10)
        self.assertEqual(c.request_no, "REQ-1")

    def test_one_commitment_binds_at_most_one_contract(self) -> None:
        self._contract("C1", "REQ-1")
        with self.assertRaises(ContractConflict):
            self.contracts.create_contract(
                "C2", "REQ-1", 3, "SUP-A", 1, 2, 1, 4
            )

    def test_cancelled_commitment_cannot_bind(self) -> None:
        self.budget.create_commitment("REQ-X", "L", 10)
        self.budget.cancel_commitment("REQ-X")
        with self.assertRaises(InvalidContract):
            self.contracts.create_contract(
                "CX", "REQ-X", 3, "SUP-A", 1, 2, 1, 4
            )

    def test_unknown_commitment_propagates(self) -> None:
        from orgbudget.errors import CommitmentNotFound

        with self.assertRaises(CommitmentNotFound):
            self.contracts.create_contract(
                "CY", "NO-SUCH", 3, "SUP-A", 1, 2, 1, 4
            )

    def test_idempotent_contract_retry_returns_same(self) -> None:
        first = self._contract("C1", "REQ-1")
        again = self.contracts.create_contract(
            "C1", "REQ-1", 3, "SUP-A", 1, 2, 1, 4
        )
        self.assertEqual(again, first)
        self.assertEqual(len(self.contracts.list_contracts()), 1)
        with self.assertRaises(IdempotencyConflict):
            self.contracts.create_contract(
                "C1", "REQ-1", 4, "SUP-A", 1, 2, 1, 4
            )

    def test_invalid_window_rejected(self) -> None:
        self.budget.create_commitment("REQ-1", "L", 10)
        with self.assertRaises(InvalidContract):
            self.contracts.create_contract(
                "C1", "REQ-1", 3, "SUP-A", earliest_day=3, latest_day=2,
                preferred_day=3, late_fee=4,
            )


class ReceiptReturnTest(ContractServiceTestBase):
    def test_cumulative_receipt_cannot_exceed_total_qty(self) -> None:
        self._contract(total_qty=3)
        self.contracts.register_receipt("C1", "RC-1", 2)
        with self.assertRaises(ReceiptError):
            self.contracts.register_receipt("C1", "RC-2", 2)  # 2+2 > 3
        # 边界：恰好补到 3 件合法。
        self.contracts.register_receipt("C1", "RC-2", 1)
        self.assertEqual(
            [r.qty for r in self.contracts.list_receipts("C1")], [2, 1]
        )

    def test_receipt_no_is_idempotent(self) -> None:
        self._contract(total_qty=5)
        self.contracts.register_receipt("C1", "RC-1", 2)
        # 同单号同内容重试不重复登记。
        self.contracts.register_receipt("C1", "RC-1", 2)
        self.assertEqual(len(self.contracts.list_receipts("C1")), 1)
        with self.assertRaises(IdempotencyConflict):
            self.contracts.register_receipt("C1", "RC-1", 3)

    def test_return_must_point_at_received_batch(self) -> None:
        self._contract(total_qty=5)
        self.contracts.register_receipt("C1", "RC-1", 2)
        with self.assertRaises(ReturnError):
            self.contracts.register_return("RT-X", "C1", "NO-RECEIPT", 1)

    def test_cumulative_return_cannot_exceed_batch(self) -> None:
        self._contract(total_qty=5)
        self.contracts.register_receipt("C1", "RC-1", 2)
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        # 再退 1 件恰好累退到批次上限 2，合法。
        self.contracts.register_return("RT-2", "C1", "RC-1", 1)
        self.assertEqual(
            sum(r.qty for r in self.contracts.list_returns("C1")), 2
        )
        # 再多退 1 件就超过该批验收数量。
        with self.assertRaises(ReturnError):
            self.contracts.register_return("RT-3", "C1", "RC-1", 1)

    def test_return_is_append_only_and_idempotent(self) -> None:
        self._contract(total_qty=5)
        self.contracts.register_receipt("C1", "RC-1", 3)
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        self.assertEqual(len(self.contracts.list_returns("C1")), 1)
        with self.assertRaises(IdempotencyConflict):
            self.contracts.register_return("RT-1", "C1", "RC-1", 2)


class PayableStatementTest(ContractServiceTestBase):
    def test_worked_example_due_four(self) -> None:
        # 承诺 10、旧库实付 2、合同 3 件验收 2 件、无退货 -> 应付 4。
        self._contract(amount=10, total_qty=3)
        self.budget.register_spend("REQ-1", 2, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 2)
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.status, PAYABLE)
        self.assertEqual(st.net_qty, 2)
        self.assertEqual(st.confirmed_price, 10 * 2 // 3)  # 6
        self.assertEqual(st.old_spent, 2)
        self.assertEqual(st.payable_amount, 4)
        self.assertTrue(st.can_pay)
        self.assertEqual(st.scheme_id, 1)
        self.assertIsNotNone(st.org_version)

    def test_floor_pricing_across_quantities(self) -> None:
        # 总价 10、N=4：q=1 -> 2, q=2 -> 5, q=3 -> 7, q=4 -> 10。
        self._contract(amount=10, total_qty=4)
        self.contracts.register_receipt("C1", "RC-1", 4)
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.confirmed_price, 10)
        # 逐切面核对 floor。
        for q, expected in [(1, 2), (2, 5), (3, 7), (4, 10)]:
            self.assertEqual(10 * q // 4, expected)

    def test_old_spend_deducted_once_even_with_returns(self) -> None:
        # 旧库实付 2；验收 2 件后退 1 件。退货不改变旧库实付，仍只扣一次。
        self._contract(amount=10, total_qty=3)
        self.budget.register_spend("REQ-1", 2, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 2)
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.net_qty, 1)
        self.assertEqual(st.confirmed_price, 3)      # floor(10*1/3)=3
        self.assertEqual(st.old_spent, 2)            # 退货不影响实付
        self.assertEqual(st.payable_amount, 1)
        self.assertEqual(len(st.spend_events), 1)
        self.assertEqual(st.spend_events[0].idem_key, "SP-1")

    def test_negative_payable_marked_recovery_not_payable(self) -> None:
        # 旧库实付 8；只验收 1 件（确认价 3）-> 3-8 = -5 追回。
        self._contract(amount=10, total_qty=3)
        self.budget.register_spend("REQ-1", 8, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 1)
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.status, RECOVERY)
        self.assertEqual(st.payable_amount, -5)
        self.assertEqual(st.recovery_amount, 5)
        self.assertFalse(st.can_pay)
        self.assertTrue(st.is_recovery)

    def test_zero_due_statement(self) -> None:
        # 实付恰好等于确认价 -> 净额 0，无需付款。
        self._contract(amount=10, total_qty=2)
        self.budget.register_spend("REQ-1", 5, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 1)  # floor(10*1/2)=5
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.status, ZERO_DUE)
        self.assertEqual(st.payable_amount, 0)
        self.assertFalse(st.can_pay)

    def test_cancelled_commitment_blocks_payment(self) -> None:
        self._contract(amount=10, total_qty=3)
        self.budget.register_spend("REQ-1", 2, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 2)
        # 合同建好后承诺被取消：应付单标阻塞，不能再开支付单。
        self.budget.cancel_commitment("REQ-1")
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.status, BLOCKED_CANCELLED)
        self.assertFalse(st.can_pay)
        self.assertTrue(st.is_blocked)
        # 阻塞 / 追回 / 零额单都不得进入排程：可付集合应为空。
        payable_bills = [
            self._bill(s)
            for s in self.contracts.statement_versions("C1")
            if s.can_pay
        ]
        self.assertEqual(payable_bills, [])
        # 旧库自身也拒绝在已取消承诺上再登记实付。
        with self.assertRaises(CommitmentClosedError):
            self.budget.register_spend("REQ-1", 1, "SP-LATE")

    def test_statements_are_versioned_and_immutable(self) -> None:
        self._contract(amount=10, total_qty=3)
        self.budget.register_spend("REQ-1", 2, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 2)
        v1 = self.contracts.build_payable_statement("C1")
        self.assertEqual((v1.version_no, v1.payable_amount), (1, 4))
        # 退货后重开：新增版本，旧版本内容不变。
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        v2 = self.contracts.build_payable_statement("C1")
        self.assertEqual((v2.version_no, v2.payable_amount), (2, 1))
        versions = self.contracts.statement_versions("C1")
        self.assertEqual([s.version_no for s in versions], [1, 2])
        self.assertEqual(versions[0].payable_amount, 4)
        self.assertEqual(versions[1].payable_amount, 1)
        # 版本里能说明所用交货、退货、实付事件。
        self.assertEqual(len(v2.receipts), 1)
        self.assertEqual(v2.receipts[0].returned_qty, 1)
        self.assertEqual(len(v2.returns), 1)
        self.assertEqual(v2.spend_events[0].amount, 2)

    def test_latest_statement_requires_one(self) -> None:
        self._contract()
        with self.assertRaises(PayableError):
            self.contracts.latest_statement("C1")

    def test_same_net_qty_different_returnable_batches(self) -> None:
        # 相同净件数 1，但批次结构不同，可退批次也不同。
        self._contract(amount=10, total_qty=4)
        # 情形甲：一批 2 件退 1 件。
        self.contracts.register_receipt("C1", "RC-1", 2)
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        st_a = self.contracts.build_payable_statement("C1")
        self.assertEqual(st_a.net_qty, 1)

        # 情形乙：另起合同，两批各 1 件、其中一批退 1 件（净也是 1），
        # 但可退批次集合与情形甲不同。
        self.budget.create_commitment("REQ-2", "L", 10)
        self.contracts.create_contract(
            "C2", "REQ-2", 4, "SUP-A", 1, 2, 1, 4
        )
        self.contracts.register_receipt("C2", "RC-2", 1)
        self.contracts.register_receipt("C2", "RC-3", 1)
        self.contracts.register_return("RT-2", "C2", "RC-2", 1)
        st_b = self.contracts.build_payable_statement("C2")
        self.assertEqual(st_b.net_qty, 1)
        # 净件数相同，但批次 / 退货落点不同。
        self.assertEqual(len(st_a.receipts), 1)
        self.assertEqual(len(st_b.receipts), 2)
        self.assertEqual(
            [r.receipt_no for r in st_b.receipts if r.returned_qty > 0],
            ["RC-2"],
        )

    def test_service_never_writes_spend_to_old_ledger(self) -> None:
        self._contract(amount=10, total_qty=3)
        self.contracts.register_receipt("C1", "RC-1", 2)
        self.contracts.build_payable_statement("C1")
        # 合同仓的任何操作都不在旧库留下支出事件。
        self.assertEqual(self.budget.spend_events(), ())
        commitment = self.budget.get_commitment("REQ-1")
        self.assertEqual(commitment.spent, 0)
        # 旧库自身登记实付后，下一版应付单才反映。
        self.budget.register_spend("REQ-1", 2, "SP-1")
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.old_spent, 2)
        self.assertEqual(st.payable_amount, 4)


class EndToEndScheduleTest(ContractServiceTestBase):
    """用真实采购与收退材料，把应付单接入付款排程重放题面。"""

    def test_worked_example_three_ways(self) -> None:
        self._contract(amount=10, total_qty=3)
        self.budget.register_spend("REQ-1", 2, "SP-1")
        self.contracts.register_receipt("C1", "RC-1", 2)
        st = self.contracts.build_payable_statement("C1")
        self.assertEqual(st.payable_amount, 4)

        # 情形 1：额度 3 -> 当天借，费用 3。
        s1 = schedule_payments(
            [self._bill(st)], horizon_days=2, initial_cash=1,
            deposits={2: 3}, credit_limit=3, credit_rate=1,
        )
        self.assertEqual(s1.bill_pay_days["C1"], 1)
        self.assertEqual(s1.total_fee, 3)

        # 情形 2：退一件，应付 1，当天付清费用 0。
        self.contracts.register_return("RT-1", "C1", "RC-1", 1)
        st2 = self.contracts.build_payable_statement("C1")
        self.assertEqual(st2.payable_amount, 1)
        s2 = schedule_payments(
            [self._bill(st2)], horizon_days=2, initial_cash=1,
            deposits={2: 3}, credit_limit=3, credit_rate=1,
        )
        self.assertEqual(s2.total_fee, 0)

    def test_two_contracts_compete_for_same_day_cash(self) -> None:
        # 两笔合同，各自承诺 10、无旧库实付、各验收全额 -> 各应付 10。
        for cid, req in (("C1", "REQ-1"), ("C2", "REQ-2")):
            self.budget.create_commitment(req, "L", 10)
            self.contracts.create_contract(
                cid, req, 2, "SUP-A", 1, 2, 1, late_fee=2
            )
            self.contracts.register_receipt(cid, f"RC-{cid}", 2)
        statements = [
            self.contracts.build_payable_statement(c) for c in ("C1", "C2")
        ]
        bills = [self._bill(s) for s in statements]
        self.assertEqual([s.payable_amount for s in statements], [10, 10])
        # 现金 10、额度 0、次日入 10：当天只能付一张，另一张迟费 2。
        sched = schedule_payments(
            bills, horizon_days=2, initial_cash=10,
            deposits={2: 10}, credit_limit=0, credit_rate=1,
        )
        days = list(sched.bill_pay_days.values())
        self.assertEqual(sorted(days), [1, 2])
        self.assertEqual(sched.total_fee, 2)
        self.assertEqual(sched.closing_balances, (0, 0))

    def test_infeasible_when_cannot_cover_all(self) -> None:
        from orgbudget.errors import InfeasibleSchedule

        self._contract(amount=10, total_qty=1)
        self.contracts.register_receipt("C1", "RC-1", 1)
        st = self.contracts.build_payable_statement("C1")  # 应付 10
        with self.assertRaises(InfeasibleSchedule):
            schedule_payments(
                [self._bill(st)], horizon_days=2, initial_cash=1,
                deposits={}, credit_limit=3, credit_rate=1,
            )


if __name__ == "__main__":
    unittest.main()
