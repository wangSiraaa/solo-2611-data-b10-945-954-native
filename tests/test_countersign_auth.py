"""授权本与询证冻结测试：叶→根最近一条、下级禁令覆盖、换版前后取值、
拒绝理由与旧名单复读。"""

from __future__ import annotations

import unittest

from tests.support import ServiceTestCase
from orgbudget.auth import DENY, HANDLER, REVIEWER, resolve_role
from orgbudget.auth_service import AuthService
from orgbudget.countersign_service import CountersignService
from orgbudget.errors import (
    AuthEditionNotFound,
    CommitmentNotOpen,
    InvalidPaymentAmount,
    InsufficientCommitmentRemaining,
    NoSignerAvailable,
)


class CountersignTestCase(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.auth = AuthService(self.tmp / "auth.db")
        self.cs = CountersignService(self.tmp / "cs.db", self.budget, self.auth)

    def _tree_and_commitment(self, d_parent: str = "X", amount: int = 100) -> int:
        version = self.build_tree(
            [
                ("ROOT", None),
                ("X", "ROOT"),
                ("Y", "ROOT"),
                ("XE", "X"),
                ("YE", "Y"),
                ("D", d_parent),
            ],
            root="ROOT",
        )
        product = self.org.get_product(version)
        weights = self.weights_for(product)
        self.budget.register_root_budget(version, 1_000, weights)
        self.budget.create_commitment("REQ-1", "D", amount)
        return version

    def _baseline_grants(self) -> None:
        self.auth.grant("X", HANDLER, "A")
        self.auth.grant("Y", HANDLER, "B")
        self.auth.grant("ROOT", REVIEWER, "B")
        self.auth.grant("ROOT", REVIEWER, "C")
        self.auth.publish_edition("baseline")


class AuthResolutionTest(CountersignTestCase):
    def test_nearest_entry_wins_leaf_to_root(self) -> None:
        self._tree_and_commitment()
        # 根先授予 A 复核，X 再禁止：D 在 X 下，最近一条是 X 的禁令。
        self.auth.grant("ROOT", REVIEWER, "A")
        self.auth.deny("X", REVIEWER, "A")
        self.auth.publish_edition()
        book = self.auth.require_latest_book()
        product = self.org.get_product(1)
        authorities = resolve_role(book, REVIEWER, "D", product.ancestors("D"))
        only = {a.person_id: a for a in authorities}["A"]
        self.assertFalse(only.granted)
        self.assertEqual(only.denied_by, "X")
        self.assertIsNone(only.layer)

    def test_lower_deny_overrides_higher_grant_and_recovers(self) -> None:
        self._tree_and_commitment()
        self._baseline_grants()
        # 下级 X 禁止 A 经办：沿 D(X)->ROOT 最近一条命中 X 禁令。
        self.auth.deny("X", HANDLER, "A")
        self.auth.publish_edition("deny-a")
        with self.assertRaises(NoSignerAvailable):
            self.cs.inquiry("L-DENY", "REQ-1", 1)
        # 禁令撤销（同位置改回授予）后恢复可签。
        self.auth.grant("X", HANDLER, "A")
        self.auth.publish_edition("restore-a")
        lst = self.cs.inquiry("L-OK", "REQ-1", 1)
        self.assertEqual(lst.handlers, ("A",))
        explanation = next(e for e in lst.explanations if e.person_id == "A")
        self.assertTrue(explanation.granted)
        self.assertEqual(explanation.layer, "X")

    def test_no_entry_means_no_authority(self) -> None:
        self._tree_and_commitment()
        self.auth.grant("ROOT", REVIEWER, "C")
        self.auth.publish_edition()
        with self.assertRaises(NoSignerAvailable):
            self.cs.inquiry("L-NO-HANDLER", "REQ-1", 1)

    def test_book_not_published_everyone_unauthorized(self) -> None:
        self._tree_and_commitment()
        with self.assertRaises(AuthEditionNotFound):
            self.cs.inquiry("L-NO-BOOK", "REQ-1", 1)

    def test_staging_upsert_keeps_one_entry_per_slot(self) -> None:
        self._tree_and_commitment()
        self.auth.grant("X", HANDLER, "A")
        self.auth.deny("X", HANDLER, "A")
        entries = self.auth.staging_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].effect, DENY)


class InquiryFreezeTest(CountersignTestCase):
    def test_frozen_list_keeps_old_org_after_migration(self) -> None:
        v1 = self._tree_and_commitment(d_parent="X")
        self._baseline_grants()
        before = self.cs.inquiry("L-BEFORE", "REQ-1", 1)
        self.assertEqual(before.org_version, v1)
        self.assertEqual(before.handlers, ("A",))

        # 行政改树但预算换版未获准：生效方案仍绑旧版，询证仍走旧树旧印。
        self.org.move_subtree("D", "Y")
        self.org.publish("moved-draft")
        still = self.cs.inquiry("L-STILL-OLD", "REQ-1", 1)
        self.assertEqual(still.org_version, v1)
        self.assertEqual(still.handlers, ("A",))

        # 足额新预算换版成功：承诺仍绑 D，D 现在在 Y 下 -> 经办只剩 B。
        v3 = self.org.current_version
        product = self.org.get_product(v3)
        self.budget.revise_budget(v3, 1_000, self.weights_for(product))
        after = self.cs.inquiry("L-AFTER", "REQ-1", 1)
        self.assertEqual(after.org_version, v3)
        self.assertEqual(after.handlers, ("B",))
        self.assertEqual(after.commitment_dept, "D")
        # 旧名单复读内容不变，不受新版授权 / 组织影响。
        replay = self.cs.get_list("L-BEFORE")
        self.assertEqual(replay.handlers, ("A",))
        self.assertEqual(replay.org_version, v1)

    def test_later_auth_change_does_not_mutate_issued_list(self) -> None:
        self._tree_and_commitment()
        self._baseline_grants()
        lst = self.cs.inquiry("L1", "REQ-1", 1)
        self.assertEqual(set(lst.reviewers), {"B", "C"})
        self.auth.deny("ROOT", REVIEWER, "B")
        self.auth.publish_edition("kill-b")
        replay = self.cs.get_list("L1")
        self.assertEqual(set(replay.reviewers), {"B", "C"})

    def test_amount_validation_and_commitment_facts(self) -> None:
        self._tree_and_commitment(amount=10)
        self._baseline_grants()
        with self.assertRaises(InvalidPaymentAmount):
            self.cs.inquiry("L-ZERO", "REQ-1", 0)
        with self.assertRaises(InsufficientCommitmentRemaining):
            self.cs.inquiry("L-OVER", "REQ-1", 11)
        self.budget.register_spend("REQ-1", 4, "SP-1")
        with self.assertRaises(InsufficientCommitmentRemaining):
            self.cs.inquiry("L-REMAIN", "REQ-1", 7)  # 真实剩余仅 6
        ok = self.cs.inquiry("L-OK", "REQ-1", 6)
        self.assertEqual(ok.commitment_remaining, 6)

    def test_cancelled_commitment_rejected_without_blank_credential(self) -> None:
        self._tree_and_commitment()
        self._baseline_grants()
        self.budget.cancel_commitment("REQ-1")
        with self.assertRaises(CommitmentNotOpen):
            self.cs.inquiry("L-CANCEL", "REQ-1", 1)
        self.assertEqual(self.cs.lists(), ())

    def test_explanation_records_inherited_and_denied_layers(self) -> None:
        self._tree_and_commitment()
        self.auth.grant("X", HANDLER, "A")
        self.auth.grant("X", HANDLER, "B")  # B 的经办授在 D 路径上的 X
        self.auth.grant("ROOT", REVIEWER, "B")
        self.auth.grant("ROOT", REVIEWER, "C")
        self.auth.deny("X", REVIEWER, "C")  # 下级禁令盖过根授予
        self.auth.publish_edition()
        lst = self.cs.inquiry("L1", "REQ-1", 1)
        indexed = {(e.person_id, e.role): e for e in lst.explanations}
        self.assertEqual(indexed[("A", HANDLER)].layer, "X")
        self.assertEqual(indexed[("B", HANDLER)].layer, "X")
        self.assertEqual(indexed[("B", REVIEWER)].layer, "ROOT")
        c_review = indexed[("C", REVIEWER)]
        self.assertFalse(c_review.granted)
        self.assertEqual(c_review.denied_by, "X")
        self.assertNotIn("C", lst.reviewers)


if __name__ == "__main__":
    unittest.main()
