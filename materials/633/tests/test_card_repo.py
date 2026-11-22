"""逐球卡仓：版本化冻结、原子/幂等、关账重开与奖证快照持久化。"""

from __future__ import annotations

import os
import tempfile
import unittest

from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository
from league.card_repo import PlayByPlayRepository, CardBatchRejected
from league.awards import ISSUE_NOW, PENDING_HEARING, NO_AWARD
from league.playbyplay import HOME, AWAY

from tests.fixtures import (
    TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY, FIXTURE4,
)

ROSTER = {"P": HOME, "U": HOME, "Q": HOME, "V": AWAY, "W": AWAY}
CARDS = [
    {"card_no": 1, "options": {HOME: "P"}},
    {"card_no": 2, "options": {HOME: "U", AWAY: "V"}},
    {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
]


class CardRepoTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sdb = os.path.join(self.tmp.name, "sched.db")
        self.rdb = os.path.join(self.tmp.name, "stand.db")
        self.pdb = os.path.join(self.tmp.name, "cards.db")
        self.svc = ScheduleService(self.sdb)
        self.svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
        self.svc.publish(FIXTURE4)
        self.repo = StandingsRepository(self.rdb, self.sdb)
        # ab 经原库 played 2:1
        self.repo.apply_result_batch("init", [{
            "result_id": "r-ab", "match_id": "ab", "state": "played",
            "score_home": 2, "score_away": 1}])
        self.pb = PlayByPlayRepository(self.pdb, self.sdb)

    def tearDown(self):
        self.pb.close()
        self.repo.close()
        self.svc.close()
        self.tmp.cleanup()

    def _frozen(self):
        return self.repo.active_results()

    def _submit(self, request_id="cv1", cards=CARDS, **kw):
        return self.pb.submit_card_volume(
            request_id, "ab", 1, dict(ROSTER), cards,
            frozen_results=self._frozen(),
            volume_id="VOL-ab", custodian="管", **kw)


class TestVersioningAndIdempotency(CardRepoTestBase):
    def test_freeze_from_frozen_fact_score(self):
        rec = self._submit()
        self.assertEqual(rec["volume_version"], 1)
        prod = self.pb.load_product(1)
        self.assertEqual((prod.final_home, prod.final_away), (2, 1))
        self.assertEqual(prod.home_team, "A")  # 当版主队来自赛程库只读连接

    def test_correction_issues_new_version_old_retained(self):
        self._submit("v1")
        fixed = [
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {HOME: "U"}},
            {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
        ]
        rec2 = self._submit("v2", cards=fixed)
        self.assertEqual(rec2["volume_version"], 2)
        versions = self.pb.list_versions("ab", 1)
        self.assertEqual([v["volume_version"] for v in versions], [1, 2])
        # 老版卡2仍有客队选择，新版卡2只剩主队
        self.assertEqual(set(self.pb.load_product(1).cards[1].options),
                         {HOME, AWAY})
        self.assertEqual(set(self.pb.load_product(2).cards[1].options), {HOME})

    def test_idempotent_replay_and_conflicting_request_id(self):
        rec = self._submit("same")
        again = self._submit("same")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["seq"], rec["seq"])
        with self.assertRaises(CardBatchRejected):
            self.pb.submit_card_volume(
                "same", "ab", 1, dict(ROSTER),
                CARDS[:2], frozen_results=self._frozen(),
                volume_id="VOL-ab", custodian="管")

    def test_invalid_volume_leaves_no_trace(self):
        bad = [{"card_no": 1, "options": {HOME: "P"}},
               {"card_no": 2, "options": {HOME: "X"}}]  # 未知球员
        with self.assertRaises(CardBatchRejected):
            self._submit("bad", cards=bad)
        self.assertEqual(self.pb.request_log(), [])
        self.assertEqual(self.pb.list_versions("ab", 1), [])

    def test_cancelled_match_accepted_but_no_award(self):
        self.repo.apply_result_batch("cancel", [{
            "result_id": "r-cd", "match_id": "cd", "state": "cancelled"}])
        rec = self.pb.submit_card_volume(
            "cc", "cd", 1, dict(ROSTER), CARDS,
            frozen_results=self.repo.active_results(),
            status="cancelled", volume_id="VOL-cd", custodian="管2")
        prod = self.pb.load_product(rec["volume_version"])
        self.assertEqual(prod.status, "cancelled")
        snap = self.pb.freeze_interpretation("snap-c", rec["volume_version"])
        from league.awards import decide_award
        d = decide_award("no-voucher", prod, "player")
        self.assertEqual(d.decision, NO_AWARD)
        self.assertTrue(snap["feasible"] is False or d.decision == NO_AWARD)

    def test_explicit_final_score_overrides_fact(self):
        rec = self.pb.submit_card_volume(
            "explicit", "ab", 1, dict(ROSTER), CARDS,
            final_score=(2, 1), volume_id="VOL-ab", custodian="管")
        self.assertEqual(rec["volume_version"], 1)

    def test_missing_fact_and_no_explicit_score_rejected(self):
        with self.assertRaises(CardBatchRejected):
            self.pb.submit_card_volume(
                "nos", "bd", 1, dict(ROSTER), CARDS,
                frozen_results=self._frozen(),
                volume_id="VOL-bd", custodian="管")

    def test_thirty_one_cards_rejected(self):
        big = [{"card_no": i, "options": {HOME: "P", AWAY: "V"}}
               for i in range(1, 32)]
        roster = {**ROSTER}
        with self.assertRaises(CardBatchRejected) as ctx:
            self.pb.submit_card_volume(
                "too-many", "ab", 1, roster, big,
                final_score=(16, 15), volume_id="VOL-ab", custodian="管")
        self.assertTrue(any("30" in r for r in ctx.exception.reasons))
        self.assertEqual(self.pb.request_log(), [])

    def test_unknown_match_rejected_at_repo(self):
        with self.assertRaises(CardBatchRejected):
            self.pb.submit_card_volume(
                "ghost", "zz", 1, dict(ROSTER), CARDS,
                final_score=(2, 1), volume_id="VOL-zz", custodian="管")


class TestSnapshotsAndReopen(CardRepoTestBase):
    def test_product_snapshot_immutable_and_reopenable(self):
        rec = self._submit("v1")
        snap1 = self.pb.freeze_interpretation("snap1", rec["volume_version"])
        sid = snap1["snapshot_id"]
        # 订正出新版
        fixed = [
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {HOME: "U"}},
            {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
        ]
        rec2 = self._submit("v2", cards=fixed)
        self.pb.freeze_interpretation("snap2", rec2["volume_version"])
        # 旧快照不被改写：仍是双解释
        old = self.pb.get_product_snapshot(sid)
        self.assertEqual(old["report"]["winning_cards"], [1, 3])

        # 关闭重开：版本、流水、快照全部可复算
        self.pb.close()
        pb2 = PlayByPlayRepository(self.pdb, self.sdb)
        reloaded = pb2.get_product_snapshot(sid)
        self.assertEqual(reloaded["report"]["winning_cards"], [1, 3])
        self.assertEqual(len(pb2.list_versions("ab", 1)), 2)
        self.assertEqual([r["request_id"] for r in pb2.request_log()],
                         ["v1", "v2"])
        pb2.close()

    def test_award_snapshot_persists_issue_and_pending(self):
        rec = self._submit("v1")
        sid = self.pb.freeze_interpretation("snap", rec["volume_version"])["snapshot_id"]
        # 培训站 K 收敛 => 立即发证
        d_issue = self.pb.register_award(
            "aw-k", sid, "station", {"P": "K", "Q": "K", "U": "K"})
        self.assertEqual(d_issue.decision, ISSUE_NOW)
        # 球员奖待核
        d_pending = self.pb.register_award("aw-p", sid, "player")
        self.assertEqual(d_pending.decision, PENDING_HEARING)
        # 重开后奖证原样可读
        self.pb.close()
        pb2 = PlayByPlayRepository(self.pdb, self.sdb)
        self.assertEqual(pb2.get_award("aw-k")["recipient"], "K")
        self.assertEqual(pb2.get_award("aw-p")["decision"], "pending")
        # 奖证不可重复签发
        with self.assertRaises(CardBatchRejected):
            pb2.register_award("aw-k", sid, "station", {})
        pb2.close()

    def test_closed_ledger_re_decision_with_different_registry(self):
        # 同一份冻结产品，关闭成绩库后按不同奖务登记给出不同结论
        rec = self._submit("v1")
        sid = self.pb.freeze_interpretation("snap", rec["volume_version"])["snapshot_id"]
        self.pb.close()
        self.repo.close()  # 关闭成绩/事实库
        pb2 = PlayByPlayRepository(self.pdb, self.sdb)
        snap = pb2.get_product_snapshot(sid)
        from league.playbyplay import PlayByPlayProduct
        from league.awards import decide_award
        prod = PlayByPlayProduct.from_dict(snap["product"])
        # 登记一：P、Q 同站 K => 发证 K
        d1 = decide_award("r1", prod, "station",
                          {"P": "K", "Q": "K", "U": "K"})
        # 登记二：P->S、Q->T => 待核
        d2 = decide_award("r2", prod, "station",
                          {"P": "S", "Q": "T", "U": "S"})
        self.assertEqual((d1.decision, d1.recipient), (ISSUE_NOW, "K"))
        self.assertEqual(d2.decision, PENDING_HEARING)
        pb2.close()


if __name__ == "__main__":
    unittest.main()
