"""凭单封存仓测试：实账切面、同号幂等、封存不可变、篡改可发现。"""

from __future__ import annotations

import sqlite3
import unittest

from orgbudget.errors import VoucherConflict, VoucherVerifyError
from orgbudget.voucher_service import VoucherService
from tests.support import ServiceTestCase


class VoucherServiceTest(ServiceTestCase):
    def _setup_migration(self) -> tuple[int, int, int]:
        """建样张两版树、根预算并在旧版四叶开 2/1/1/2 承诺，返回版本与方案。"""
        self.build_tree([
            ("HQ", None),
            ("A", "HQ"), ("B", "HQ"),
            ("X", "HQ"), ("Y", "HQ"),
            ("ax", "A"), ("ay", "A"), ("bx", "B"), ("by", "B"),
        ])
        v1 = self.org.current_version
        p1 = self.org.get_product(v1)
        weights = self.weights_for(p1)
        scheme = self.budget.register_root_budget(v1, 100_000, weights)
        for req, leaf, amount in [
            ("REQ-ax", "ax", 2), ("REQ-ay", "ay", 1),
            ("REQ-bx", "bx", 1), ("REQ-by", "by", 2),
        ]:
            self.budget.create_commitment(req, leaf, amount)
        self.org.move_subtree("ax", "X")
        self.org.move_subtree("ay", "Y")
        self.org.move_subtree("bx", "X")
        self.org.move_subtree("by", "Y")
        v2 = self.org.publish("迁编 X/Y").version
        return v1, v2, scheme.scheme_id

    def test_seal_from_real_budget_snapshot(self) -> None:
        v1, v2, scheme_id = self._setup_migration()
        vault = VoucherService(self.tmp / "voucher.db", self.org)
        facts = self.budget.occupancy_snapshot(scheme_id)
        voucher = vault.seal_voucher(
            "VCH-1", v1, v2, ("A", "B"), ("X", "Y"), facts, scheme_id
        )
        self.assertEqual(voucher.total_width, 6)
        self.assertEqual(voucher.scheme_id, scheme_id)
        # 读回的封存凭单与封存时一致并通过自验。
        self.assertEqual(vault.get_voucher("VCH-1"), voucher)

    def test_seal_idempotent_same_input(self) -> None:
        v1, v2, scheme_id = self._setup_migration()
        vault = VoucherService(self.tmp / "voucher.db", self.org)
        facts = self.budget.occupancy_snapshot(scheme_id)
        first = vault.seal_voucher(
            "VCH-1", v1, v2, ("A", "B"), ("X", "Y"), facts, scheme_id
        )
        second = vault.seal_voucher(
            "VCH-1", v1, v2, ("A", "B"), ("X", "Y"),
            self.budget.occupancy_snapshot(scheme_id), scheme_id,
        )
        self.assertEqual(first, second)

    def test_seal_conflict_on_different_input(self) -> None:
        v1, v2, scheme_id = self._setup_migration()
        vault = VoucherService(self.tmp / "voucher.db", self.org)
        vault.seal_voucher(
            "VCH-1", v1, v2, ("A", "B"), ("X", "Y"),
            self.budget.occupancy_snapshot(scheme_id), scheme_id,
        )
        # ay 再开一笔真实承诺，切面改变后同号封存必须冲突，原凭单不动。
        self.budget.create_commitment("REQ-ay-2", "ay", 4)
        with self.assertRaises(VoucherConflict):
            vault.seal_voucher(
                "VCH-1", v1, v2, ("A", "B"), ("X", "Y"),
                self.budget.occupancy_snapshot(scheme_id), scheme_id,
            )
        sealed = vault.get_voucher("VCH-1")
        self.assertEqual(sealed.total_width, 6)

    def test_sealed_voucher_survives_later_payment_and_revision(self) -> None:
        v1, v2, scheme_id = self._setup_migration()
        vault = VoucherService(self.tmp / "voucher.db", self.org)
        before = vault.seal_voucher(
            "VCH-1", v1, v2, ("A", "B"), ("X", "Y"),
            self.budget.occupancy_snapshot(scheme_id), scheme_id,
        )
        # 封存后：付清一笔、再补订，并把预算仓换版到新组织产品。
        self.budget.register_spend("REQ-ax", 2, "SP-ax")
        self.budget.create_commitment("REQ-bx-2", "bx", 3)
        p2 = self.org.get_product(v2)
        self.budget.revise_budget(v2, 100_000, self.weights_for(p2))
        after = vault.get_voucher("VCH-1")
        self.assertEqual(after, before)
        self.assertEqual(after.total_width, 6)
        # 新切面确实变了，证明封存的是快照而非实时视图。
        current_occ = {
            f.request_no: f.occupancy
            for f in self.budget.occupancy_snapshot()
        }
        self.assertEqual(current_occ["REQ-ax"], 2)  # 已付清，仍占已付 2
        self.assertIn("REQ-bx-2", current_occ)     # 补订进了实时切面
        self.assertNotIn(
            "REQ-bx-2", {ln.request_no for ln in after.lines}
        )

    def test_tampered_payload_row_detected(self) -> None:
        v1, v2, scheme_id = self._setup_migration()
        vault = VoucherService(self.tmp / "voucher.db", self.org)
        vault.seal_voucher(
            "VCH-1", v1, v2, ("A", "B"), ("X", "Y"),
            self.budget.occupancy_snapshot(scheme_id), scheme_id,
        )
        with sqlite3.connect(vault.db_path) as con:
            row = con.execute(
                "SELECT payload_json FROM sealed_voucher WHERE voucher_no = 'VCH-1'"
            ).fetchone()
            import json as _json
            payload = _json.loads(row[0])
            payload["total_width"] += 5
            con.execute(
                "UPDATE sealed_voucher SET payload_json = ? WHERE voucher_no = 'VCH-1'",
                (_json.dumps(payload, ensure_ascii=False),),
            )
            con.commit()
        with self.assertRaises(VoucherVerifyError):
            vault.get_voucher("VCH-1")

    def test_handoff_failure_leaves_no_row(self) -> None:
        v1, v2, scheme_id = self._setup_migration()
        vault = VoucherService(self.tmp / "voucher.db", self.org)
        from orgbudget.errors import VoucherHandOffError

        with self.assertRaises(VoucherHandOffError):
            vault.seal_voucher(
                "VCH-BAD", v1, v2, ("A", "B"), ("X",),
                self.budget.occupancy_snapshot(scheme_id), scheme_id,
            )
        self.assertEqual(vault.voucher_numbers(), ())


if __name__ == "__main__":
    unittest.main()
