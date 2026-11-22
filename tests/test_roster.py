"""封样名册：归票、入封约束、归层、冻结签发与旧册重开。"""

from __future__ import annotations

import sqlite3
import unittest

from tests.support import ServiceTestCase
from orgbudget.audit import audit_roster
from orgbudget.errors import (
    IncompleteRoster,
    InvalidTree,
    OrganizationVersionNotFound,
    SpendEventNotFound,
    TicketConflict,
    TicketNotFound,
)


class RosterTestCase(ServiceTestCase):
    """小册：根 HQ -> 分部 X、Y；X 下四个叶，Y 下两个叶。"""

    def build_book(self):
        edges = [("HQ", None), ("X", "HQ"), ("Y", "HQ")]
        edges += [(f"X{i}", "X") for i in range(1, 5)]
        edges += [(f"Y{i}", "Y") for i in range(1, 3)]
        version = self.build_tree(edges)
        product = self.org.get_product(version)
        self.budget.register_root_budget(
            version, 100_000, self.weights_for(product)
        )
        return version, product

    def pay(self, seq: int, dept: str, ticket: str, amount: int = 100) -> str:
        req = f"R{seq}"
        self.budget.create_commitment(req, dept, 1_000)
        self.budget.register_spend(req, amount, f"S{seq}")
        self.budget.register_ticket(ticket)
        self.budget.assign_spend(f"S{seq}", ticket)
        return f"S{seq}"

    def pay_four_x_two_y(self) -> int:
        version, _ = self.build_book()
        seq = 0
        for dept in ("X1", "X2", "X3", "X4", "Y1", "Y2"):
            seq += 1
            self.pay(seq, dept, f"T{seq}")
        return version


class TicketingTest(RosterTestCase):
    def test_only_actual_spend_can_be_sealed(self) -> None:
        version, _ = self.build_book()
        self.budget.create_commitment("R1", "X1", 500)
        self.budget.register_ticket("T1")
        # 未入账的承诺不能入封：幂等键不存在。
        with self.assertRaises(SpendEventNotFound):
            self.budget.assign_spend("NO-SUCH", "T1")
        # 登记但尚未入账的支出同样不能入封。
        with self.assertRaises(SpendEventNotFound):
            self.budget.assign_spend("S1", "T1")
        self.budget.register_spend("R1", 100, "S1")
        self.budget.assign_spend("S1", "T1")
        roster = self.budget.seal_roster(version)
        self.assertEqual(roster.envelope_count, 1)

    def test_incomplete_batch_refuses_roster(self) -> None:
        version, _ = self.build_book()
        self.budget.create_commitment("R1", "X1", 500)
        self.budget.register_spend("R1", 100, "S1")
        self.budget.register_ticket("T1")
        # 有一条已入账支出未归票，不能签发完整名册。
        with self.assertRaises(IncompleteRoster):
            self.budget.seal_roster(version)

    def test_assign_is_one_ticket_per_event_and_correction_uses_reassign(
        self,
    ) -> None:
        version, _ = self.build_book()
        self.budget.create_commitment("R1", "X1", 500)
        self.budget.register_spend("R1", 100, "S1")
        self.budget.register_ticket("T1")
        self.budget.register_ticket("T2")
        first = self.budget.assign_spend("S1", "T1")
        self.assertEqual(first.ticket_id, "T1")
        # 重复归到同一票：幂等。
        self.assertEqual(self.budget.assign_spend("S1", "T1").ticket_id, "T1")
        # 直接改挂到另一票被拒绝，必须显式勘误。
        with self.assertRaises(TicketConflict):
            self.budget.assign_spend("S1", "T2")
        self.assertEqual(
            self.budget.reassign_spend("S1", "T2", note="勘误").ticket_id, "T2"
        )
        self.assertEqual(self.budget.ticket_assignments("T2")[0].idem_key, "S1")
        with self.assertRaises(TicketNotFound):
            self.budget.assign_spend("S1", "NO-TICKET")

    def test_cancelled_commitment_spent_part_stays_in_roster(self) -> None:
        version, _ = self.build_book()
        self.budget.create_commitment("R1", "X1", 500)
        self.budget.register_spend("R1", 120, "S1")
        self.budget.cancel_commitment("R1")  # 已付部分不可撤销
        self.budget.register_ticket("T1")
        self.budget.assign_spend("S1", "T1")
        roster = self.budget.seal_roster(version)
        # 取消承诺后已实付的 120 仍在册。
        self.assertEqual(roster.payment_count, 1)
        self.assertEqual(roster.layers[0].envelopes[0].payments[0].amount, 120)


class LayeringTest(RosterTestCase):
    def test_branches_and_joint_layer(self) -> None:
        version, product = self.build_book()
        # 同票跨 X、Y 两个直接分部 => 联合层。
        self.budget.create_commitment("RA", "X1", 400)
        self.budget.register_spend("RA", 100, "SA")
        self.budget.create_commitment("RB", "Y1", 400)
        self.budget.register_spend("RB", 100, "SB")
        self.budget.register_ticket("TJ")
        self.budget.assign_spend("SA", "TJ")
        self.budget.assign_spend("SB", "TJ")
        # 单独 X 票。
        self.pay(3, "X2", "TX")
        roster = self.budget.seal_roster(version)
        self.assertEqual(dict(roster.layer_sizes()), {"X": 1, "联合": 1})
        joint = roster.layer("联合")
        self.assertEqual(joint.envelopes[0].ticket_id, "TJ")
        self.assertEqual(joint.envelopes[0].payment_count, 2)
        self.assertIn("跨分部", joint.envelopes[0].basis)
        # 联合层排在各分部层之后。
        self.assertEqual(roster.layer_names, ("X", "联合"))

    def test_same_ticket_only_one_envelope_even_multi_payment(self) -> None:
        version, _ = self.build_book()
        # 一张纸票被两笔支付共同使用：封数=1，内有两笔实付。
        self.budget.create_commitment("R1", "X1", 1_000)
        self.budget.register_spend("R1", 200, "S1a")
        self.budget.register_spend("R1", 300, "S1b")
        self.budget.register_ticket("T1")
        self.budget.assign_spend("S1a", "T1")
        self.budget.assign_spend("S1b", "T1")
        roster = self.budget.seal_roster(version)
        self.assertEqual(roster.envelope_count, 1)
        self.assertEqual(roster.payment_count, 2)
        self.assertEqual(roster.layers[0].envelopes[0].payment_count, 2)


class RosterFreezeTest(RosterTestCase):
    def test_small_book_reseals_after_move_and_old_roster_reopens(self) -> None:
        version = self.pay_four_x_two_y()
        old = self.budget.seal_roster(version)
        self.assertEqual(dict(old.layer_sizes()), {"X": 4, "Y": 2})

        # 只把 X1 这一个叶移到 Y 下并发布组织新版；实付原件仍是六封。
        self.org.move_subtree("X1", "Y")
        v2 = self.org.publish("move X1 under Y").version
        new = self.budget.seal_roster(v2)
        self.assertEqual(new.payment_count, 6)
        self.assertEqual(new.envelope_count, 6)
        # 归层按新组织版本重算：X 剩 3，Y 得 3。
        self.assertEqual(dict(new.layer_sizes()), {"X": 3, "Y": 3})

        # 旧名册原样可重开，不随迁移变化。
        reopened = self.budget.get_roster(old.roster_id)
        self.assertEqual(dict(reopened.layer_sizes()), {"X": 4, "Y": 2})
        self.assertEqual(reopened.cut_digest, old.cut_digest)

    def test_correction_produces_new_roster_old_still_reopens(self) -> None:
        version = self.pay_four_x_two_y()
        first = self.budget.seal_roster(version)
        # 勘误：把 S1 从 T1 改挂到 T2，T1 空缸后不再有票，T2 收两笔。
        self.budget.reassign_spend("S1", "T2")
        second = self.budget.seal_roster(version)
        self.assertEqual(second.envelope_count, 5)  # 6 票减 1 空票
        # 支付集合未变，故摘要不变；变的是封的归组（T2 收两笔）。
        self.assertEqual(second.cut_digest, first.cut_digest)
        self.assertFalse(
            any(env.ticket_id == "T1" for env in second.envelopes())
        )
        t2 = next(env for env in second.envelopes() if env.ticket_id == "T2")
        self.assertEqual(t2.payment_count, 2)
        # 旧名册重开仍是 6 封、原归组。
        old = self.budget.get_roster(first.roster_id)
        self.assertEqual(old.envelope_count, 6)
        self.assertEqual(old.cut_digest, first.cut_digest)

    def test_roster_binds_published_org_version_and_rejects_unknown_dept(
        self,
    ) -> None:
        self.pay_four_x_two_y()
        with self.assertRaises(OrganizationVersionNotFound):
            self.budget.seal_roster(99)  # 未发布版本

    def test_roster_requires_actual_spend(self) -> None:
        _, _ = self.build_book()
        with self.assertRaises(IncompleteRoster):
            self.budget.seal_roster(1)


class RosterAuditTest(RosterTestCase):
    def test_audit_recomputes_and_detects_tamper(self) -> None:
        version = self.pay_four_x_two_y()
        roster = self.budget.seal_roster(version)
        result = audit_roster(self.budget.db_path, self.org.db_path, roster.roster_id)
        self.assertEqual(result.spend_count, 6)
        self.assertEqual(result.envelope_count, 6)
        self.assertEqual(dict(result.layer_sizes), {"X": 4, "Y": 2})

        # 篡改名册支付的部门：归层重算立刻发现。
        with sqlite3.connect(self.budget.db_path) as con:
            con.execute(
                "UPDATE roster_payment SET dept_id = 'Y1' "
                "WHERE roster_id = ? AND idem_key = 'S1'",
                (roster.roster_id,),
            )
            con.commit()
        with self.assertRaises(InvalidTree):
            audit_roster(self.budget.db_path, self.org.db_path, roster.roster_id)

    def test_audit_detects_missing_payment(self) -> None:
        version = self.pay_four_x_two_y()
        roster = self.budget.seal_roster(version)
        with sqlite3.connect(self.budget.db_path) as con:
            con.execute(
                "DELETE FROM roster_payment WHERE roster_id = ? AND idem_key = 'S1'",
                (roster.roster_id,),
            )
            con.commit()
        with self.assertRaises(InvalidTree):
            audit_roster(self.budget.db_path, self.org.db_path, roster.roster_id)


class RosterSamplingIntegrationTest(RosterTestCase):
    """名册 + 求解器端到端：题面小册的三个数量与费用。"""

    def _seal(self, version):
        return self.budget.seal_roster(version)

    def test_plan_from_roster_and_after_move_and_p_change(self) -> None:
        from fractions import Fraction

        from orgbudget.sampling import plan_for_roster

        version = self.pay_four_x_two_y()
        roster = self._seal(version)
        plan = plan_for_roster(roster, {"X": 1, "Y": 2}, 1, Fraction(1, 2))
        self.assertEqual((plan.quotas, plan.total_cost), ((2, 1), 4))

        # 只把一片叶 X1 移到 Y，再发布组织新版并另作名册。
        self.org.move_subtree("X1", "Y")
        v2 = self.org.publish("move X1 under Y").version
        roster2 = self._seal(v2)
        # 实付原件仍是六封。
        self.assertEqual(roster2.envelope_count, 6)
        plan2 = plan_for_roster(roster2, {"X": 1, "Y": 2}, 1, Fraction(1, 2))
        self.assertEqual((plan2.quotas, plan2.total_cost), ((2, 2), 6))

        # 沿原组织仅把 p 改为 2/3，须 3+2 封、费用 7。
        plan3 = plan_for_roster(roster, {"X": 1, "Y": 2}, 1, Fraction(2, 3))
        self.assertEqual((plan3.quotas, plan3.total_cost), ((3, 2), 7))

        # 旧名册仍可重开，且能独立复点。
        old = self.budget.get_roster(roster.roster_id)
        self.assertEqual(dict(old.layer_sizes()), {"X": 4, "Y": 2})
        recheck = audit_roster(
            self.budget.db_path, self.org.db_path, roster.roster_id,
            costs={"X": 1, "Y": 2}, problem_count=1, probability=Fraction(1, 2),
        )
        self.assertEqual(dict(recheck.layer_sizes), {"X": 4, "Y": 2})

    def test_joint_layer_counts_and_zero_or_full_quota(self) -> None:
        from fractions import Fraction


        from orgbudget.sampling import plan_for_roster

        version, _ = self.build_book()
        # 一张跨部门票（联合层）+ 一张 X 票。
        self.budget.create_commitment("RA", "X1", 400)
        self.budget.register_spend("RA", 100, "SA")
        self.budget.create_commitment("RB", "Y1", 400)
        self.budget.register_spend("RB", 100, "SB")
        self.budget.register_ticket("TJ")
        self.budget.assign_spend("SA", "TJ")
        self.budget.assign_spend("SB", "TJ")
        self.pay(3, "X2", "TX")
        roster = self.budget.seal_roster(version)
        self.assertEqual(dict(roster.layer_sizes()), {"X": 1, "联合": 1})
        # 联合层同样参与求解：k=1 时两层各开满（各 1 封）即必查中。
        plan = plan_for_roster(
            roster, {"X": 1, "联合": 1}, 1, Fraction(1)
        )
        self.assertEqual(dict(zip(roster.layer_names, plan.quotas)),
                         {"X": 1, "联合": 1})
        self.assertEqual(plan.guarantee, Fraction(1))

    def test_k_greater_than_one_over_real_joint_roster(self) -> None:
        from fractions import Fraction

        from orgbudget.sampling import plan_for_roster, worst_case_miss

        version, _ = self.build_book()
        # 4 封 X、1 封联合（跨 X/Y）、1 封 Y：层规模 X4/ Y1/ 联合1。
        for seq, dept in enumerate(("X1", "X2", "X3", "X4"), start=1):
            self.pay(seq, dept, f"TX{seq}")
        self.pay(5, "Y1", "TY1")
        self.budget.create_commitment("RJ1", "X2", 400)
        self.budget.register_spend("RJ1", 100, "SJ1")
        self.budget.create_commitment("RJ2", "Y2", 400)
        self.budget.register_spend("RJ2", 100, "SJ2")
        self.budget.register_ticket("TJ")
        self.budget.assign_spend("SJ1", "TJ")
        self.budget.assign_spend("SJ2", "TJ")
        roster = self.budget.seal_roster(version)
        self.assertEqual(dict(roster.layer_sizes()), {"X": 4, "Y": 1, "联合": 1})

        # k=2：两个问题封可能全都藏进未开的空位。要保证必查中，未开空位
        # 必须少于 k（否则两个问题封可全落在未开部分）。总 6 封、k=2，
        # 即至少要开 5 封；最省是开满成本各为 1 的 5 封，费用 5。
        # 注意不能把各层“发现一封”的概率简单相加：两层各开 1 封时
        # X 层仍有 3 封未开，问题封可两颗都藏在 X 里。
        miss, dist = worst_case_miss([4, 1, 1], [1, 1, 1], 2)
        self.assertEqual(dist, (2, 0, 0))       # 最不利：两问题封都藏 X
        self.assertEqual(miss, Fraction(1, 2))  # X 开 1 封，漏检 C(2,1)/C(4,1)
        plan = plan_for_roster(
            roster, {"X": 1, "Y": 1, "联合": 1}, 2, Fraction(1)
        )
        self.assertEqual(sum(plan.quotas), 5)
        self.assertEqual(plan.total_cost, 5)
        self.assertEqual(plan.guarantee, Fraction(1))


if __name__ == "__main__":
    unittest.main()
