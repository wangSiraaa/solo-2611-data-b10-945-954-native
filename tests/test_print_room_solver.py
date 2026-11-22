"""印务室纯求解器测试：最低备印费、预案见证、互斥与一人一轮一次、
全备不可行诊断。"""

from __future__ import annotations

import unittest

from orgbudget.auth import HANDLER, REVIEWER
from orgbudget.errors import StampFeeMissing
from orgbudget.print_room import (
    AbsencePlan,
    DocCandidate,
    PrintRound,
    Witness,
    plan_prep,
)

ALL = AbsencePlan("ALL")
BASE_FEES = {
    ("A", HANDLER): 1,
    ("B", HANDLER): 1,
    ("B", REVIEWER): 1,
    ("C", REVIEWER): 5,
}
DUTY = ("A", "B", "C")


def sheet(
    sheet_id: str,
    handlers: tuple[str, ...] = ("A",),
    reviewers: tuple[str, ...] = ("B", "C"),
) -> DocCandidate:
    return DocCandidate(sheet_id, f"REQ-{sheet_id}", handlers, reviewers)


class WorkedNumbersTest(unittest.TestCase):
    """题面给定的五个费用数值逐一复算。"""

    def test_case1_baseline_fee_2(self) -> None:
        pr = PrintRound("R", (sheet("S1"),), dict(BASE_FEES), frozenset(), DUTY)
        result = plan_prep(pr, (ALL,))
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_fee, 2)
        self.assertEqual(
            result.per_plan["ALL"], (Witness("S1", "A", "B"),)
        )

    def test_case2_moved_handler_b_cannot_self_review_fee_6(self) -> None:
        # D 迁 Y 后经办只剩 B；B 不能自核，复核只能 C -> 1 + 5 = 6。
        pr = PrintRound(
            "R",
            (sheet("S1", handlers=("B",), reviewers=("B", "C")),),
            dict(BASE_FEES),
            frozenset(),
            DUTY,
        )
        result = plan_prep(pr, (ALL,))
        self.assertEqual(result.total_fee, 6)
        self.assertEqual(result.per_plan["ALL"], (Witness("S1", "B", "C"),))

    def test_case3_b_review_fee_raised_to_3_still_4(self) -> None:
        fees = dict(BASE_FEES)
        fees[("B", REVIEWER)] = 3
        pr = PrintRound("R", (sheet("S1"),), fees, frozenset(), DUTY)
        result = plan_prep(pr, (ALL,))
        self.assertEqual(result.total_fee, 4)
        self.assertEqual(result.per_plan["ALL"], (Witness("S1", "A", "B"),))

    def test_case4_all_present_plus_b_absent_fee_6(self) -> None:
        pr = PrintRound("R", (sheet("S1"),), dict(BASE_FEES), frozenset(), DUTY)
        plans = (ALL, AbsencePlan("NO-B", frozenset({"B"})))
        result = plan_prep(pr, plans)
        self.assertEqual(result.total_fee, 6)
        # 只能备 A 经办 + C 复核；全员预案下也被重新指派为 A/C。
        self.assertEqual(
            set(result.prepared), {("A", HANDLER), ("C", REVIEWER)}
        )
        for plan_id in ("ALL", "NO-B"):
            self.assertEqual(
                result.per_plan[plan_id], (Witness("S1", "A", "C"),)
            )

    def test_case5_add_c_absent_requires_both_review_stamps_fee_7(self) -> None:
        pr = PrintRound("R", (sheet("S1"),), dict(BASE_FEES), frozenset(), DUTY)
        plans = (
            ALL,
            AbsencePlan("NO-B", frozenset({"B"})),
            AbsencePlan("NO-C", frozenset({"C"})),
        )
        result = plan_prep(pr, plans)
        self.assertEqual(result.total_fee, 7)
        self.assertEqual(
            set(result.prepared),
            {("A", HANDLER), ("B", REVIEWER), ("C", REVIEWER)},
        )
        self.assertEqual(result.per_plan["NO-B"], (Witness("S1", "A", "C"),))
        self.assertEqual(result.per_plan["NO-C"], (Witness("S1", "A", "B"),))


class SolverRulesTest(unittest.TestCase):
    def test_incompatible_pair_forces_other_combo(self) -> None:
        # A 与 B 本轮不可配对：基线 A/B 组合作废，只能 A/C，费用 6。
        pr = PrintRound(
            "R",
            (sheet("S1"),),
            dict(BASE_FEES),
            frozenset({("A", "B")}),
            DUTY,
        )
        result = plan_prep(pr, (ALL,))
        self.assertEqual(result.total_fee, 6)
        self.assertEqual(result.per_plan["ALL"], (Witness("S1", "A", "C"),))

    def test_two_sheets_contending_one_handler(self) -> None:
        """两单抢同一人：只有一个经办人时全备也盖不齐两单。"""
        pr = PrintRound(
            "R",
            (sheet("S1", handlers=("A",)), sheet("S2", handlers=("A",))),
            dict(BASE_FEES),
            frozenset(),
            DUTY,
        )
        result = plan_prep(pr, (ALL,))
        self.assertFalse(result.feasible)
        self.assertEqual(len(result.failures), 1)
        blocked = {b.sheet_id: b.reason for b in result.failures[0].blocked}
        # 结构上每单都能配对，堵点是一人一轮一次的全局占用。
        self.assertIn("一人一轮只能动一次印", next(iter(blocked.values())))

    def test_two_sheets_two_handlers_feasible_with_global_occupancy(self) -> None:
        # 四个候选人位：经办 A/B，复核 C/D，两单才能互不抢占。
        pr = PrintRound(
            "R",
            (
                sheet("S1", handlers=("A", "B"), reviewers=("C", "D")),
                sheet("S2", handlers=("A", "B"), reviewers=("C", "D")),
            ),
            {
                ("A", HANDLER): 1,
                ("B", HANDLER): 1,
                ("C", REVIEWER): 5,
                ("D", REVIEWER): 2,
            },
            frozenset(),
            ("A", "B", "C", "D"),
        )
        result = plan_prep(pr, (ALL,))
        self.assertTrue(result.feasible)
        witnesses = result.per_plan["ALL"]
        people_used = [p for w in witnesses for p in (w.handler, w.reviewer)]
        # 四个人位互不相同：一人一轮一次。
        self.assertEqual(len(people_used), len(set(people_used)))

    def test_absent_person_stamp_unusable_even_if_prepared(self) -> None:
        # 复核候选 C/B；C 缺席预案下求解器必须把 B 复核纳入备印。
        pr = PrintRound(
            "R",
            (sheet("S1", handlers=("A",), reviewers=("B", "C")),),
            {
                ("A", HANDLER): 1,
                ("C", REVIEWER): 5,
                ("B", REVIEWER): 1,
            },
            frozenset(),
            DUTY,
        )
        result = plan_prep(pr, (AbsencePlan("NO-C", frozenset({"C"})),))
        self.assertTrue(result.feasible)
        self.assertEqual(
            set(result.prepared), {("A", HANDLER), ("B", REVIEWER)}
        )
        self.assertEqual(
            result.per_plan["NO-C"], (Witness("S1", "A", "B"),)
        )

    def test_missing_fee_rejected_before_cabinet(self) -> None:
        pr = PrintRound(
            "R",
            (sheet("S1"),),
            {("A", HANDLER): 1, ("B", REVIEWER): 1},  # 缺 C 复核印金
            frozenset(),
            DUTY,
        )
        with self.assertRaises(StampFeeMissing):
            plan_prep(pr, (ALL,))

    def test_structural_block_no_reviewer(self) -> None:
        pr = PrintRound(
            "R",
            (sheet("S1", handlers=("A",), reviewers=("C",)),),
            {("A", HANDLER): 1, ("C", REVIEWER): 5},
            frozenset(),
            DUTY,
        )
        result = plan_prep(pr, (AbsencePlan("NO-C", frozenset({"C"})),))
        self.assertFalse(result.feasible)
        blocked = result.failures[0].blocked
        self.assertEqual(blocked[0].sheet_id, "S1")
        self.assertIn("复核", blocked[0].reason)

    def test_fee_charged_once_per_pair_regardless_of_sheets(self) -> None:
        # 同一对“人、角色”是两张单的共同候选，备印费只按对收一次。
        pr = PrintRound(
            "R",
            (
                sheet("S1", handlers=("A", "B"), reviewers=("C", "D")),
                sheet("S2", handlers=("A", "B"), reviewers=("C", "D")),
            ),
            {
                ("A", HANDLER): 1,
                ("B", HANDLER): 1,
                ("C", REVIEWER): 5,
                ("D", REVIEWER): 2,
            },
            frozenset(),
            ("A", "B", "C", "D"),
        )
        result = plan_prep(pr, (ALL,))
        self.assertTrue(result.feasible)
        # 四对印各收一次：1 + 1 + 5 + 2 = 9，绝不因候选出现在两张单而翻倍。
        self.assertEqual(result.total_fee, 9)
        self.assertEqual(
            set(result.prepared),
            {("A", HANDLER), ("B", HANDLER), ("C", REVIEWER), ("D", REVIEWER)},
        )


if __name__ == "__main__":
    unittest.main()
