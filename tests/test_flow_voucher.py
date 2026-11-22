"""迁编流转凭单测试：占额口径、唯一覆盖、栏头反链、守恒与不能交版。"""

from __future__ import annotations

import unittest

from orgbudget.errors import VoucherHandOffError, VoucherVerifyError
from orgbudget.flow_voucher import (
    FlowVoucher,
    OccupancyFact,
    build_voucher,
)
from tests.support import ServiceTestCase


def _fact(
    request_no: str,
    dept: str,
    amount: int,
    status: str = "OPEN",
    spent: int = 0,
) -> OccupancyFact:
    remaining = amount - spent if status == "OPEN" else 0
    return OccupancyFact(request_no, dept, amount, status, spent, remaining)


class VoucherBuildTest(ServiceTestCase):
    """题面样张树：HQ -> {A, B} -> {ax, ay, bx, by}（旧）；
    HQ -> {X, Y} -> 同四叶（新）。"""

    def _publish_versions(self) -> tuple[int, int]:
        edges = [
            ("HQ", None),
            ("A", "HQ"), ("B", "HQ"),
            ("X", "HQ"), ("Y", "HQ"),
            ("ax", "A"), ("ay", "A"), ("bx", "B"), ("by", "B"),
        ]
        v1 = self.build_tree(edges)
        # 新版：四叶分别挂到 X、Y、X、Y；A、B 成为无叶的旧栏头仍保留。
        self.org.move_subtree("ax", "X")
        self.org.move_subtree("ay", "Y")
        self.org.move_subtree("bx", "X")
        self.org.move_subtree("by", "Y")
        v2 = self.org.publish("迁编到 X/Y").version
        return v1, v2

    def test_sample_occupancy_and_bands(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        occupancy = (
            _fact("REQ-ax", "ax", 2),
            _fact("REQ-ay", "ay", 1),
            _fact("REQ-bx", "bx", 1),
            _fact("REQ-by", "by", 2),
        )
        voucher = build_voucher(
            "V-1", p1, p2, ("A", "B"), ("X", "Y"), occupancy,
            scheme_id=1, sealed_at="t",
        )
        self.assertEqual(voucher.total_width, 6)
        band_map = {(b.old_header, b.new_header): b.width for b in voucher.bands}
        self.assertEqual(band_map, {
            ("A", "X"): 2, ("A", "Y"): 1, ("B", "X"): 1, ("B", "Y"): 2,
        })
        # 两条祖先链都保留，且沿各自版本的树。
        line_ax = next(ln for ln in voucher.lines if ln.request_no == "REQ-ax")
        self.assertEqual(line_ax.old_ancestors, ("HQ", "A"))
        self.assertEqual(line_ax.new_ancestors, ("HQ", "X"))
        # 入出合计自验通过。
        voucher.verify()

    def test_occupancy_paid_plus_open_remaining(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        occupancy = (
            # 已付 7、仍开立未付 3 -> 占额 10。
            OccupancyFact("R1", "ax", 10, "OPEN", 7, 3),
            # 已付 4 后取消：只留已付，占额 4。
            OccupancyFact("R2", "ay", 9, "CANCELLED", 4, 0),
            # 未发生任何支出的 OPEN 承诺：占额全额。
            _fact("R3", "bx", 1),
            # 零占额（全取消未付）不进凭单。
            OccupancyFact("R4", "by", 5, "CANCELLED", 0, 0),
        )
        voucher = build_voucher(
            "V-2", p1, p2, ("A", "B"), ("X", "Y"), occupancy, 1, "t",
        )
        amounts = {ln.request_no: ln.amount for ln in voucher.lines}
        self.assertEqual(amounts, {"R1": 10, "R2": 4, "R3": 1})
        self.assertEqual(voucher.total_width, 15)
        self.assertNotIn("R4", amounts)

    def test_missing_coverage_blocks_handoff_with_request(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        # 新栏只给 X：ay、by（新版在 Y 下）无覆盖。
        with self.assertRaises(VoucherHandOffError) as ctx:
            build_voucher(
                "V-3", p1, p2, ("A", "B"), ("X",),
                (_fact("R1", "ax", 1), _fact("R2", "ay", 1)), 1, "t",
            )
        codes = {p[0] for p in ctx.exception.problems}
        reqs = {p[1] for p in ctx.exception.problems}
        self.assertIn("MISSING", codes)
        self.assertIn("R2", reqs)

    def test_multiple_coverage_blocks_handoff(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        # 旧栏给 A 和 HQ：HQ 是 A 的祖先，ax 同时被两者覆盖 -> 多归。
        with self.assertRaises(VoucherHandOffError) as ctx:
            build_voucher(
                "V-4", p1, p2, ("A", "HQ"), ("X", "Y"),
                (_fact("R1", "ax", 1),), 1, "t",
            )
        self.assertTrue(any(p[0] == "MULTIPLE" and p[1] == "R1"
                            for p in ctx.exception.problems))

    def test_antichain_headers_rejected(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        with self.assertRaises(VoucherHandOffError) as ctx:
            build_voucher(
                "V-5", p1, p2, ("A", "HQ"), ("X", "Y"),
                (_fact("R1", "ax", 1),), 1, "t",
            )
        self.assertTrue(any(p[0] == "HEADER_ANCESTRY" for p in ctx.exception.problems))

    def test_header_not_in_product_rejected(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        with self.assertRaises(VoucherHandOffError) as ctx:
            build_voucher(
                "V-6", p1, p2, ("A", "GHOST"), ("X", "Y"),
                (_fact("R1", "ax", 1),), 1, "t",
            )
        self.assertTrue(any(p[0] == "HEADER_MISSING" for p in ctx.exception.problems))

    def test_sealed_voucher_immune_to_later_facts(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        occupancy = (
            _fact("REQ-ax", "ax", 2), _fact("REQ-ay", "ay", 1),
            _fact("REQ-bx", "bx", 1), _fact("REQ-by", "by", 2),
        )
        voucher = build_voucher("V-7", p1, p2, ("A", "B"), ("X", "Y"),
                                occupancy, 1, "t")
        # 后来“付清 / 补订”只改变切面事实，封存凭单对象本身不可变。
        later = (
            OccupancyFact("REQ-ax", "ax", 2, "OPEN", 2, 0),  # ax 已付清
            _fact("REQ-ay", "ay", 1),
            _fact("REQ-bx", "bx", 1),
            _fact("REQ-by", "by", 2),
            _fact("REQ-new", "ax", 8),                       # 补订
        )
        voucher_again = build_voucher("V-8", p1, p2, ("A", "B"), ("X", "Y"),
                                      later, 1, "t")
        self.assertEqual(voucher.total_width, 6)
        self.assertEqual(voucher_again.total_width, 14)
        self.assertEqual(len(voucher.lines), 4)

    def test_roundtrip_payload_and_tamper_detection(self) -> None:
        v1, v2 = self._publish_versions()
        p1, p2 = self.org.get_product(v1), self.org.get_product(v2)
        voucher = build_voucher(
            "V-9", p1, p2, ("A", "B"), ("X", "Y"),
            (_fact("R1", "ax", 2), _fact("R2", "by", 2)), 1, "t",
        )
        payload = voucher.to_dict()
        restored = FlowVoucher.from_dict(payload)
        self.assertEqual(restored, voucher)
        # 篡改带宽总额立即被自验发现。
        payload["total_width"] += 1
        with self.assertRaises(VoucherVerifyError):
            FlowVoucher.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
