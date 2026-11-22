"""奖务判定与听审排庭：发证/待核/不颁三态、回避、合分庭与传卷优化。"""

from __future__ import annotations

import unittest

from league.playbyplay import build_product, HOME, AWAY
from league.awards import (
    decide_award, build_hearing_case, AwardError,
    ISSUE_NOW, PENDING_HEARING, NO_AWARD,
)
from league.tribunal import (
    Chair, schedule_hearings, earliest_finish_with_cap, SchedulingError,
)

ROSTER = {"P": HOME, "U": HOME, "Q": HOME, "V": AWAY, "W": AWAY}
CARDS = [
    {"card_no": 1, "options": {HOME: "P"}},
    {"card_no": 2, "options": {HOME: "U", AWAY: "V"}},
    {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
]


def product(**kw):
    return build_product("ab", 1, "A", "B", dict(ROSTER), 2, 1, CARDS,
                         volume_id="VOL-ab", custodian="管", **kw)


class TestAwardDecision(unittest.TestCase):
    def test_player_award_pending_with_two_witnesses(self):
        d = decide_award("aw-p", product(), "player")
        self.assertEqual(d.decision, PENDING_HEARING)
        self.assertEqual(d.recipients, ["P", "Q"])
        self.assertIsNone(d.recipient)
        self.assertEqual(len(d.witnesses), 2)
        self.assertEqual(d.hearing_case_id, "case-aw-p")

    def test_station_award_issues_when_all_collapse_to_K(self):
        d = decide_award("aw-k", product(), "station",
                         {"P": "K", "Q": "K", "U": "K"})
        self.assertEqual(d.decision, ISSUE_NOW)
        self.assertEqual(d.recipient, "K")
        self.assertEqual(d.witnesses, [])

    def test_no_registry_means_nobody_can_claim(self):
        d = decide_award("aw-none", product(), "station", {})
        self.assertEqual(d.decision, NO_AWARD)
        self.assertEqual(d.recipients, [None])

    def test_mix_claimable_and_unclaimed_is_pending(self):
        d = decide_award("aw-mix", product(), "station",
                         {"P": "K", "U": "K"})  # Q 未登记
        self.assertEqual(d.decision, PENDING_HEARING)
        self.assertEqual(d.recipients, ["K", None])

    def test_draw_no_award(self):
        draw = build_product("d", 1, "A", "B", dict(ROSTER), 1, 1,
                             [{"card_no": 1, "options": {HOME: "P"}},
                              {"card_no": 2, "options": {AWAY: "V"}}],
                             volume_id="v", custodian="c")
        d = decide_award("aw-d", draw, "player")
        self.assertEqual(d.decision, NO_AWARD)
        self.assertIn("平局", d.note)

    def test_cancelled_match_emits_no_voucher(self):
        canc = build_product("c", 1, "A", "B", dict(ROSTER), 0, 0, CARDS,
                             status="cancelled", volume_id="v", custodian="c")
        d = decide_award("aw-c", canc, "player")
        self.assertEqual(d.decision, NO_AWARD)
        self.assertIn("取消场", d.note)

    def test_uninterpretable_volume_no_award(self):
        bad = build_product("u", 1, "A", "B", dict(ROSTER), 2, 1, [
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {HOME: "U"}},
            {"card_no": 3, "options": {HOME: "Q"}},
        ], volume_id="v", custodian="c")
        d = decide_award("aw-u", bad, "player")
        self.assertEqual(d.decision, NO_AWARD)
        self.assertFalse(d.feasible)
        self.assertEqual(d.first_dead_card, "final")

    def test_fixed_card_two_issues_immediately_to_P(self):
        fixed = build_product("ab", 1, "A", "B", dict(ROSTER), 2, 1, [
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {HOME: "U"}},
            {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
        ], volume_id="VOL-ab", custodian="管")
        dp = decide_award("p", fixed, "player")
        ds = decide_award("s", fixed, "station",
                          {"P": "S", "Q": "T", "U": "S"})
        self.assertEqual((dp.decision, dp.recipient), (ISSUE_NOW, "P"))
        self.assertEqual((ds.decision, ds.recipient), (ISSUE_NOW, "S"))

    def test_register_Q_to_S_collapses_station_but_player_still_pending(self):
        ds = decide_award("s2", product(), "station",
                          {"P": "S", "Q": "S", "U": "S"})
        self.assertEqual((ds.decision, ds.recipient), (ISSUE_NOW, "S"))
        dp = decide_award("p2", product(), "player")
        self.assertEqual(dp.decision, PENDING_HEARING)


class TestHearingCase(unittest.TestCase):
    def test_case_carries_volume_custodian_and_real_parties(self):
        d = decide_award("aw", product(), "player")
        hc = build_hearing_case(d, product())
        self.assertEqual(hc.volume_id, "VOL-ab")
        self.assertEqual(hc.custodian, "管")
        self.assertEqual(hc.possible_recipients, ["P", "Q"])
        self.assertEqual(hc.conflict_parties, ["P", "Q"])
        self.assertEqual(len(hc.witnesses), 2)

    def test_build_case_on_non_pending_rejected(self):
        d = decide_award("aw", product(), "station",
                         {"P": "K", "Q": "K", "U": "K"})
        with self.assertRaises(AwardError):
            build_hearing_case(d, product())

    def test_missing_custodian_rejected(self):
        no_cust = build_product("ab", 1, "A", "B", dict(ROSTER), 2, 1, CARDS)
        d = decide_award("aw", no_cust, "player")
        with self.assertRaises(AwardError):
            build_hearing_case(d, no_cust)


def _two_cases():
    prod = product()
    dp = decide_award("award-player", prod, "player")
    ds = decide_award("award-station", prod, "station",
                      {"P": "S", "Q": "T", "U": "S"})
    return build_hearing_case(dp, prod), build_hearing_case(ds, prod)


def _chairs():
    # 第一天甲须回避 S；第二天乙须回避 P；第三天丙无回避
    return [
        Chair("甲", (1,), frozenset({"S"})),
        Chair("乙", (2,), frozenset({"P"})),
        Chair("丙", (3,), frozenset()),
    ]


class TestScheduling(unittest.TestCase):
    def test_deadline_day_two_needs_two_summons_split(self):
        hc_p, hc_s = _two_cases()
        plan = schedule_hearings([hc_p, hc_s], _chairs(), deadline=2)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.summons, 2)
        self.assertEqual(plan.finished_day, 2)
        days = {(t.day, t.chair_id) for t in plan.tribunals}
        self.assertEqual(days, {(1, "甲"), (2, "乙")})
        for t in plan.tribunals:
            self.assertNotEqual(t.chair_id, t.custodian)

    def test_day_three_allows_single_joint_hearing_one_summons(self):
        hc_p, hc_s = _two_cases()
        plan = schedule_hearings([hc_p, hc_s], _chairs(), deadline=3)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.summons, 1)  # 合庭共传一次
        self.assertEqual(plan.finished_day, 3)
        self.assertEqual(len(plan.tribunals), 1)
        t = plan.tribunals[0]
        self.assertTrue(t.joint)
        self.assertEqual(t.chair_id, "丙")
        self.assertEqual(set(t.case_ids),
                         {"case-award-player", "case-award-station"})

    def test_one_summon_by_day_two_has_no_common_chair(self):
        hc_p, hc_s = _two_cases()
        plan = schedule_hearings([hc_p, hc_s], _chairs(), deadline=2,
                                 summon_cap=1)
        self.assertFalse(plan.feasible)
        self.assertIn("无满足", plan.reason)

    def test_minimize_summons_beats_earliness(self):
        # 即使第 1、2 天能分庭快速收完，只要第 3 天能合庭少传，就优先少传
        hc_p, hc_s = _two_cases()
        plan = schedule_hearings([hc_p, hc_s], _chairs(), deadline=3)
        self.assertEqual(plan.summons, 1)
        self.assertEqual(plan.finished_day, 3)

    def test_single_player_case_heard_by_jia_day_one(self):
        hc_p, _ = _two_cases()
        plan = schedule_hearings([hc_p], _chairs(), deadline=2)
        self.assertEqual(plan.summons, 1)
        self.assertEqual((plan.tribunals[0].day, plan.tribunals[0].chair_id),
                         (1, "甲"))

    def test_recusal_blocks_chair(self):
        # 甲回避 S，站奖案含 S => 即使甲第 1 天有空也不能主持站奖
        _, hc_s = _two_cases()
        only_jia = [Chair("甲", (1, 2, 3), frozenset({"S"}))]
        plan = schedule_hearings([hc_s], only_jia, deadline=3)
        self.assertFalse(plan.feasible)

    def test_chair_cannot_be_custodian(self):
        hc_p, _ = _two_cases()
        # 唯一主席就是保管人“管” => 主席与保管人须不同，无解
        bad = [Chair("管", (1, 2, 3), frozenset())]
        plan = schedule_hearings([hc_p], bad, deadline=3)
        self.assertFalse(plan.feasible)

    def test_custodian_one_appearance_per_day(self):
        # 强制分庭：没有一位主席能同时听两案（X 回避人奖收件者 P，Y 回避站奖 S），
        # 合庭无合格主席 => 只能分庭；同场保管人相同，两庭不得落在同一天。
        hc_p, hc_s = _two_cases()
        chairs = [Chair("X", (1, 2), frozenset({"P"})),
                  Chair("Y", (1, 2), frozenset({"S"}))]
        plan = schedule_hearings([hc_p, hc_s], chairs, deadline=2)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.summons, 2)
        self.assertEqual(len({t.day for t in plan.tribunals}), 2)
        # 每位保管人每天至多一次：同日同保管人不得出现两庭
        appearances = [(t.custodian, t.day) for t in plan.tribunals]
        self.assertEqual(len(appearances), len(set(appearances)))

    def test_earliest_finish_with_cap_scan(self):
        hc_p, hc_s = _two_cases()
        plan = earliest_finish_with_cap([hc_p, hc_s], _chairs(), summon_cap=1)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.finished_day, 3)
        self.assertEqual(plan.summons, 1)
        impossible = earliest_finish_with_cap([hc_p, hc_s], _chairs(),
                                              summon_cap=0)
        self.assertFalse(impossible.feasible)

    def test_batch_limits_validated(self):
        hc_p, _ = _two_cases()
        with self.assertRaises(SchedulingError):
            schedule_hearings([hc_p] * 7, _chairs(), deadline=3)
        with self.assertRaises(SchedulingError):
            schedule_hearings([hc_p], _chairs(), deadline=6)

    def test_different_matches_run_in_parallel_with_distinct_custodians(self):
        from league.playbyplay import PlayByPlayProduct
        prod = product()
        d1 = decide_award("a1", prod, "player")
        hc1 = build_hearing_case(d1, prod)
        # 另一场比赛、另一位保管人
        prod2 = PlayByPlayProduct.from_dict({**prod.to_dict(),
                                             "match_id": "cd",
                                             "volume_id": "VOL-cd",
                                             "custodian": "另一管"})
        d2 = decide_award("a2", prod2, "player")
        hc2 = build_hearing_case(d2, prod2)
        chairs = [Chair("X", (1,), frozenset()), Chair("Y", (1,), frozenset())]
        plan = schedule_hearings([hc1, hc2], chairs, deadline=1)
        self.assertTrue(plan.feasible)
        # 不同保管人可同日各开一庭，一天审完两场、传卷两次
        self.assertEqual(plan.finished_day, 1)
        self.assertEqual(plan.summons, 2)
        self.assertEqual({t.custodian for t in plan.tribunals},
                         {"管", "另一管"})


if __name__ == "__main__":
    unittest.main()
