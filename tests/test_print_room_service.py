"""印务室正式登记服务测试：备印落轮、实际到岗签单、一人一轮一次、
回执幂等复读与冲突拒绝。"""

from __future__ import annotations

import unittest

from tests.support import ServiceTestCase
from orgbudget.auth import HANDLER, REVIEWER
from orgbudget.auth_service import AuthService
from orgbudget.countersign_service import CountersignService, PrintRoomService
from orgbudget.errors import (
    IncompatibleAssignees,
    PersonOccupied,
    PersonOffDuty,
    PrintRoomError,
    ReceiptConflict,
    StampNotPrepared,
)
from orgbudget.print_room import (
    AbsencePlan,
    DocCandidate,
    PrintRound,
    Witness,
    plan_prep,
)

ALL = AbsencePlan("ALL")


class PrintRoomRegistrationTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.auth = AuthService(self.tmp / "auth.db")
        self.cs = CountersignService(self.tmp / "cs.db", self.budget, self.auth)
        self.room = PrintRoomService(self.tmp / "cs.db")
        version = self.build_tree(
            [
                ("ROOT", None),
                ("X", "ROOT"),
                ("Y", "ROOT"),
                ("XE", "X"),
                ("YE", "Y"),
                ("D", "X"),
            ],
            root="ROOT",
        )
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 1_000, self.weights_for(product))
        self.budget.create_commitment("REQ-1", "D", 100)
        self.budget.create_commitment("REQ-2", "D", 100)
        # 四个人：经办 A/B，复核 B/C/D，两单四个印位才能互不抢占。
        self.auth.grant("X", HANDLER, "A")
        self.auth.grant("X", HANDLER, "B")
        self.auth.grant("ROOT", REVIEWER, "B")
        self.auth.grant("ROOT", REVIEWER, "C")
        self.auth.grant("ROOT", REVIEWER, "D")
        self.auth.publish_edition()
        self.fees = {
            ("A", HANDLER): 1,
            ("B", HANDLER): 1,
            ("B", REVIEWER): 1,
            ("C", REVIEWER): 5,
            ("D", REVIEWER): 2,
        }
        self.duty = ("A", "B", "C", "D")
        self.l1 = self.cs.inquiry("L1", "REQ-1", 1)
        self.l2 = self.cs.inquiry("L2", "REQ-2", 1)

    def _open(
        self,
        round_id: str,
        plans: tuple[AbsencePlan, ...],
        *,
        sheets: tuple[DocCandidate, ...] | None = None,
        incompat=frozenset(),
    ):
        pr = PrintRound(
            round_id,
            sheets or (self.l1.candidate("S1"), self.l2.candidate("S2")),
            dict(self.fees),
            frozenset(incompat),
            self.duty,
        )
        result = plan_prep(pr, plans)
        self.room.open_round(round_id, pr, plans, result)
        return pr, result

    def test_register_full_set_and_replay_is_idempotent(self) -> None:
        _, prep = self._open("R1", (ALL,))
        witnesses = prep.per_plan["ALL"]
        nos = {w.sheet_id: f"RC-{w.sheet_id}" for w in witnesses}
        receipts = self.room.register_receipts("R1", frozenset(), witnesses, nos)
        self.assertEqual(len(receipts), 2)
        people = [p for w in witnesses for p in (w.handler, w.reviewer)]
        self.assertEqual(len(people), len(set(people)))  # 一人一轮一次
        # 同内容整批重放：幂等复读，不二次占印。
        again = self.room.register_receipts("R1", frozenset(), witnesses, nos)
        self.assertEqual(again, receipts)
        self.assertEqual(len(self.room.receipts("R1")), 2)

    def test_replay_with_different_receipt_number_conflicts(self) -> None:
        _, prep = self._open("R1", (ALL,))
        witnesses = prep.per_plan["ALL"]
        nos = {w.sheet_id: f"RC-{w.sheet_id}" for w in witnesses}
        self.room.register_receipts("R1", frozenset(), witnesses, nos)
        with self.assertRaises(ReceiptConflict):
            self.room.register_receipts(
                "R1", frozenset(), witnesses, {s: f"OTHER-{s}" for s in nos}
            )

    def test_off_duty_and_unprepared_stamps_rejected(self) -> None:
        # 单轮三预案（对应题面费用 7 的备印：A 经办 + B/C 两枚复核）。
        plans = (
            ALL,
            AbsencePlan("NO-B", frozenset({"B"})),
            AbsencePlan("NO-C", frozenset({"C"})),
        )
        s1 = DocCandidate("S1", "REQ-1", ("A",), ("B", "C"))
        _, prep = self._open("R2", plans, sheets=(s1,))
        self.assertEqual(prep.total_fee, 7)
        self.assertEqual(
            set(prep.prepared),
            {("A", HANDLER), ("B", REVIEWER), ("C", REVIEWER)},
        )
        # 实际 B 缺席，却指派 B 盖复核 -> 拒绝。
        with self.assertRaises(PersonOffDuty):
            self.room.register_receipts(
                "R2", frozenset({"B"}),
                (Witness("S1", "A", "B"),), {"S1": "RC-1"},
            )
        # 全员到岗但指派未备的 B 经办 -> 不能再借新章。
        with self.assertRaises(StampNotPrepared):
            self.room.register_receipts(
                "R2", frozenset(),
                (Witness("S1", "B", "C"),), {"S1": "RC-2"},
            )
        self.assertEqual(self.room.receipts("R2"), ())

    def test_one_person_one_stamp_per_round_enforced(self) -> None:
        self._open("R3", (ALL,))
        bad = (Witness("S1", "A", "C"), Witness("S2", "A", "D"))
        with self.assertRaises(PersonOccupied):
            self.room.register_receipts(
                "R3", frozenset(), bad,
                {w.sheet_id: f"RC-{w.sheet_id}" for w in bad},
            )
        self.assertEqual(self.room.receipts("R3"), ())

    def test_incompatible_pair_rejected_at_registration(self) -> None:
        # A/B 本轮互斥。S2（E 经办、仅 B 复核）强制备下 B 复核印；
        # 可行指派为 S1 A/C、S2 E/B。正式登记时若在 S1 违规配 A/B，
        # 两枚印都已备，报错点落在互斥校验而不是未备印。
        fees = dict(self.fees)
        fees[("E", HANDLER)] = 1
        duty = ("A", "B", "C", "E")
        pr = PrintRound(
            "R4",
            (
                DocCandidate("S1", "REQ-1", ("A",), ("B", "C")),
                DocCandidate("S2", "REQ-2", ("E",), ("B",)),
            ),
            fees,
            frozenset({("A", "B")}),
            duty,
        )
        result = plan_prep(pr, (ALL,))
        self.assertTrue(result.feasible)
        self.room.open_round("R4", pr, (ALL,), result)
        self.assertIn(("B", REVIEWER), set(result.prepared))
        bad = (Witness("S1", "A", "B"), Witness("S2", "E", "B"))
        with self.assertRaises(IncompatibleAssignees):
            self.room.register_receipts(
                "R4", frozenset(), bad,
                {w.sheet_id: f"RC-{w.sheet_id}" for w in bad},
            )
        self.assertEqual(self.room.receipts("R4"), ())

    def test_infeasible_plan_cannot_open_round(self) -> None:
        # 两张单都只有 A 一个经办人：全备也排不开（一人一轮一次）。
        pr = PrintRound(
            "RBAD",
            (
                DocCandidate("S1", "REQ-1", ("A",), ("B", "C")),
                DocCandidate("S2", "REQ-2", ("A",), ("B", "C")),
            ),
            {("A", HANDLER): 1, ("B", REVIEWER): 1, ("C", REVIEWER): 5},
            frozenset(),
            self.duty,
        )
        result = plan_prep(pr, (ALL,))
        self.assertFalse(result.feasible)
        with self.assertRaises(PrintRoomError):
            self.room.open_round("RBAD", pr, (ALL,), result)


if __name__ == "__main__":
    unittest.main()
