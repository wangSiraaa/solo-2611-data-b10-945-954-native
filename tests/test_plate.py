"""印厂制版排位测试：三张题面工单、退单、固定栏、纯载荷消费与暴力核对。"""

from __future__ import annotations

import json
import random
import unittest
from itertools import permutations

from orgbudget.errors import PlateInputError
from orgbudget.flow_voucher import FlowBand, FlowVoucher, VoucherLine
from orgbudget.plate import (
    REJECTED_CONFLICT,
    REJECTED_OVER_BUDGET,
    SIGNED,
    PlateConstraints,
    SideConstraints,
    issue_work_order,
    issue_work_order_from_payload,
)


def _voucher(
    cells: dict[tuple[str, str], int],
    left: tuple[str, ...],
    right: tuple[str, ...],
    voucher_no: str = "V",
) -> FlowVoucher:
    lines: list[VoucherLine] = []
    reqs: dict[tuple[str, str], str] = {}
    for k, ((o, n), w) in enumerate(sorted(cells.items()), start=1):
        req = f"R{k:02d}"
        lines.append(VoucherLine(req, f"leaf{k}", w, o, n, ("HQ",), ("HQ",)))
        reqs[(o, n)] = req
    bands = tuple(
        FlowBand(o, n, w, (reqs[(o, n)],))
        for (o, n), w in sorted(cells.items())
    )
    return FlowVoucher(
        voucher_no, 1, 2, 1,
        tuple(sorted(left)), tuple(sorted(right)),
        tuple(lines), bands, sum(cells.values()), "t",
    )


# 题面样张：AX=2、AY=1、BX=1、BY=2。
SAMPLE_CELLS = {
    ("A", "X"): 2, ("A", "Y"): 1, ("B", "X"): 1, ("B", "Y"): 2,
}


class SampleWorkOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v1 = _voucher(SAMPLE_CELLS, ("A", "B"), ("X", "Y"), "V1")

    def test_job1_free_right_signs_with_one_dot(self) -> None:
        order = issue_work_order(
            self.v1,
            PlateConstraints(left=SideConstraints(before=(("A", "B"),)),
                             ink_dot_budget=3),
        )
        self.assertEqual(order.status, SIGNED)
        self.assertEqual(order.left_order, ("A", "B"))
        self.assertEqual(order.right_order, ("X", "Y"))
        self.assertEqual(order.ink_dots, 1)
        # 唯一相交带对：AY(1) × BX(1) = 1。
        crossed = [c for c in order.crossings if c.crossed]
        self.assertEqual(len(crossed), 1)
        self.assertEqual(
            {(c.band_a, c.band_b) for c in crossed},
            {(("A", "Y"), ("B", "X"))},
        )
        self.assertEqual(crossed[0].ink_dots, 1)
        self.assertTrue(order.is_signed)

    def test_job2_extra_commitment_rejects_over_budget(self) -> None:
        # ay 另开 4：AY 1 -> 5。
        cells = dict(SAMPLE_CELLS)
        cells[("A", "Y")] = 5
        v2 = _voucher(cells, ("A", "B"), ("X", "Y"), "V2")
        order = issue_work_order(
            v2,
            PlateConstraints(left=SideConstraints(before=(("A", "B"),)),
                             ink_dot_budget=3),
        )
        self.assertEqual(order.status, REJECTED_OVER_BUDGET)
        self.assertEqual(order.ink_dots, 4)
        self.assertEqual(order.right_order, ("Y", "X"))
        self.assertIn("超额 1", order.reason)
        crossed = [c for c in order.crossings if c.crossed]
        self.assertEqual({(c.band_a, c.band_b) for c in crossed},
                         {(("A", "X"), ("B", "Y"))})
        self.assertEqual(crossed[0].ink_dots, 4)

    def test_job3_forced_y_before_x_also_rejects_with_different_bands(self) -> None:
        # 同一张原凭单，仅约束右栏 Y 必须先于 X：最低 4、退单，
        # 且交带清单与自由排位（job1 的 AY×BX）不同。
        order = issue_work_order(
            self.v1,
            PlateConstraints(
                left=SideConstraints(before=(("A", "B"),)),
                right=SideConstraints(before=(("Y", "X"),)),
                ink_dot_budget=3,
            ),
        )
        self.assertEqual(order.status, REJECTED_OVER_BUDGET)
        self.assertEqual(order.ink_dots, 4)
        self.assertEqual(order.right_order, ("Y", "X"))
        crossed = [c for c in order.crossings if c.crossed]
        self.assertEqual({(c.band_a, c.band_b) for c in crossed},
                         {(("A", "X"), ("B", "Y"))})

    def test_contradictory_constraints_rejected(self) -> None:
        order = issue_work_order(
            self.v1,
            PlateConstraints(
                left=SideConstraints(before=(("A", "B"), ("B", "A"))),
                ink_dot_budget=3,
            ),
        )
        self.assertEqual(order.status, REJECTED_CONFLICT)
        self.assertEqual(order.conflict_pairs, (("A", "B"),))
        self.assertEqual(order.left_order, ())

    def test_fixed_first_column_supported(self) -> None:
        order = issue_work_order(
            self.v1,
            PlateConstraints(left=SideConstraints(fixed_first="B"),
                             ink_dot_budget=99),
        )
        self.assertEqual(order.left_order[0], "B")
        self.assertEqual(order.status, SIGNED)
        # 固定首位与另一约束冲突时按矛盾退单。
        bad = issue_work_order(
            self.v1,
            PlateConstraints(
                left=SideConstraints(before=(("A", "B"),), fixed_first="B"),
                ink_dot_budget=99,
            ),
        )
        self.assertEqual(bad.status, REJECTED_CONFLICT)

    def test_budget_zero_means_only_zero_dot_orders_sign(self) -> None:
        order = issue_work_order(self.v1, PlateConstraints(ink_dot_budget=0))
        # 样张任何排位至少 1 个隔墨点，预算 0 必退单。
        self.assertEqual(order.status, REJECTED_OVER_BUDGET)
        self.assertGreaterEqual(order.ink_dots, 1)


class PayloadOnlyConsumerTest(unittest.TestCase):
    """消费端只收凭单 JSON 与自己的约束，不接触任何服务/数据库。"""

    def test_issue_from_json_payload(self) -> None:
        voucher = _voucher(SAMPLE_CELLS, ("A", "B"), ("X", "Y"))
        payload = json.loads(json.dumps(voucher.to_dict(), ensure_ascii=False))
        order = issue_work_order_from_payload(
            payload,
            PlateConstraints(left=SideConstraints(before=(("A", "B"),)),
                             ink_dot_budget=3),
        )
        self.assertEqual(order.status, SIGNED)
        self.assertEqual(order.ink_dots, 1)
        # 工单自身也可序列化为纯 JSON 交接。
        decoded = json.loads(order.to_json())
        self.assertEqual(decoded["left_order"], ["A", "B"])

    def test_unknown_header_in_constraint_rejected(self) -> None:
        voucher = _voucher(SAMPLE_CELLS, ("A", "B"), ("X", "Y"))
        with self.assertRaises(PlateInputError):
            issue_work_order(
                voucher,
                PlateConstraints(left=SideConstraints(before=(("A", "Z"),))),
            )

    def test_columns_not_split_or_dropped(self) -> None:
        voucher = _voucher(SAMPLE_CELLS, ("A", "B"), ("X", "Y"))
        order = issue_work_order(voucher, PlateConstraints(ink_dot_budget=99))
        self.assertEqual(sorted(order.left_order), sorted(voucher.old_headers))
        self.assertEqual(sorted(order.right_order), sorted(voucher.new_headers))


class BruteForceCrossCheck(unittest.TestCase):
    """随机小例：子集/枚举求解与全排列暴力最优逐例一致。"""

    @staticmethod
    def _brute(cells, lhs, rhs, lc, rc):
        def feasible(headers, pairs):
            return [
                p for p in permutations(sorted(headers))
                if all(p.index(u) < p.index(v) for u, v in pairs)
            ]

        def cost(lo, ro):
            lp = {h: i for i, h in enumerate(lo)}
            rp = {h: i for i, h in enumerate(ro)}
            keys = list(cells)
            total = 0
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    a, b = keys[i], keys[j]
                    if a[0] == b[0] or a[1] == b[1]:
                        continue
                    if (lp[a[0]] < lp[b[0]]) != (rp[a[1]] < rp[b[1]]):
                        total += cells[a] * cells[b]
            return total

        fl = feasible(lhs, lc)
        fr = feasible(rhs, rc)
        if not fl or not fr:
            return None
        return min(cost(lo, ro) for lo in fl for ro in fr)

    def test_matches_brute_force(self) -> None:
        rng = random.Random(20261122)
        checked = 0
        for _ in range(300):
            nl, nr = rng.randint(2, 4), rng.randint(2, 4)
            lhs = [f"L{i}" for i in range(nl)]
            rhs = [f"R{j}" for j in range(nr)]
            cells = {
                (o, n): rng.randint(1, 9)
                for o in lhs for n in rhs
                if rng.random() < 0.6
            }
            if len(cells) < 2:
                continue
            lc = tuple((lhs[i], lhs[j]) for i in range(nl)
                       for j in range(i + 1, nl) if rng.random() < 0.35)
            rc = tuple((rhs[i], rhs[j]) for i in range(nr)
                       for j in range(i + 1, nr) if rng.random() < 0.35)
            voucher = _voucher(cells, tuple(lhs), tuple(rhs))
            order = issue_work_order(
                voucher,
                PlateConstraints(
                    left=SideConstraints(before=lc),
                    right=SideConstraints(before=rc),
                    ink_dot_budget=0,
                ),
            )
            brute = self._brute(cells, lhs, rhs, lc, rc)
            if brute is None:
                self.assertEqual(order.status, REJECTED_CONFLICT)
                continue
            checked += 1
            self.assertEqual(order.ink_dots, brute)
            self.assertEqual(sum(c.ink_dots for c in order.crossings), brute)
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
