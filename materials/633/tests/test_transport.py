"""客队班车运输办公室：凭单生成、真实主客、时刻倒推/顺推、重叠校验、版本修订与持久化。"""

from __future__ import annotations

import os
import tempfile
import unittest

from league.schedule_service import ScheduleService
from league.transport import (
    TransportOffice, TransportRejected, OUTBOUND, RETURN, RUN_CANCELLED,
)

from tests.fixtures import (
    TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY, FIXTURE4,
)

# 后轮窗口整体晚于前轮（更晚互不冲突的日期）
TIMETABLE = [
    {"round": 1, "slot": "s", "kickoff": 2, "dispersal": 3},
    {"round": 2, "slot": "s", "kickoff": 10, "dispersal": 11},
    {"round": 3, "slot": "s", "kickoff": 20, "dispersal": 21},
]


class TransportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sdb = os.path.join(self.tmp.name, "sched.db")
        self.svc = ScheduleService(self.sdb)
        self.svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
        self.ed_before = self.svc.publish(FIXTURE4)["edition_id"]
        flipped = [
            {**m, "home": m["away"], "away": m["home"]}
            if m["match_id"] in {"ab", "ac", "bc"} else dict(m)
            for m in FIXTURE4
        ]
        self.assertEqual(self.svc.check_candidate(flipped), [])
        self.ed_after = self.svc.publish(flipped)["edition_id"]
        self.office = TransportOffice(self.svc)
        self.office.configure(
            team_bases={t: t for t in TEAMS4},
            team_venues={t: t for t in TEAMS4},
            travel={(a, b): 1 for a in TEAMS4 for b in TEAMS4 if a != b},
            timetable=TIMETABLE, lead_beats=1, segment_capacity=1,
        )

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()


class TestPlanGeneration(TransportTestBase):
    def test_plan_uses_real_home_away_not_match_id_letters(self):
        plan = self.office.generate_plan(self.ed_before)
        runs = {r["match_id"]: r for r in plan["runs"]}
        # ab：edition 中真实主队 A、客队 B -> 客队是 B，不是按字母把 A 当客队
        ab = runs["ab"]
        self.assertEqual(ab["team"], "B")
        self.assertEqual(ab["venue"], "A")
        self.assertEqual(ab["base"], "B")
        legs = {l["direction"]: l for l in ab["legs"]}
        # 开赛前 1 拍抵达：kickoff=2 -> arrive=1；时长 1 倒推发车=0
        self.assertEqual((legs[OUTBOUND]["from"], legs[OUTBOUND]["to"],
                          legs[OUTBOUND]["depart"], legs[OUTBOUND]["arrive"]),
                         ("B", "A", 0, 1))
        # 散场 3 发车，顺推 +1 -> 4 抵达原基地
        self.assertEqual((legs[RETURN]["from"], legs[RETURN]["to"],
                          legs[RETURN]["depart"], legs[RETURN]["arrive"]),
                         ("A", "B", 3, 4))
        for leg in legs.values():
            self.assertEqual(leg["capacity"], 1)
            self.assertLess(leg["depart"], leg["arrive"])  # 车程严格正时长

    def test_flipped_edition_flips_round_one_free_leg(self):
        # 全季主客对换的新 edition：ab 的客队变成 A，班车从 A 基地发往 B 场站
        plan = self.office.generate_plan(self.ed_after)
        ab = next(r for r in plan["runs"] if r["match_id"] == "ab")
        self.assertEqual(ab["team"], "A")
        out = next(l for l in ab["legs"] if l["direction"] == OUTBOUND)
        self.assertEqual((out["from"], out["to"]), ("A", "B"))

    def test_full_season_runs_built_for_every_published_match(self):
        plan = self.office.generate_plan(self.ed_before)
        self.assertEqual(len(plan["runs"]), len(FIXTURE4))
        self.assertEqual({r["match_id"] for r in plan["runs"]},
                         {m["match_id"] for m in FIXTURE4})
        for r in plan["runs"]:
            self.assertEqual(len(r["legs"]), 2)

    def test_venue_can_differ_from_team_base(self):
        # 另起一库：A 的基地与 A 主场场站不同，时刻必须按场站时刻表+有向时长派生
        with tempfile.TemporaryDirectory() as d:
            svc = ScheduleService(os.path.join(d, "s.db"))
            svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
            ed = svc.publish(FIXTURE4)["edition_id"]
            office = TransportOffice(svc)
            stations = TEAMS4 + ["V-A"]
            office.configure(
                team_bases={t: t for t in TEAMS4},
                team_venues={**{t: t for t in TEAMS4}, "A": "V-A"},
                travel={(a, b): 2 for a in stations for b in stations if a != b},
                timetable=TIMETABLE, lead_beats=1, segment_capacity=1,
            )
            plan = office.generate_plan(ed)
            ab = next(r for r in plan["runs"] if r["match_id"] == "ab")
            out = next(l for l in ab["legs"] if l["direction"] == OUTBOUND)
            back = next(l for l in ab["legs"] if l["direction"] == RETURN)
            self.assertEqual(ab["venue"], "V-A")
            self.assertEqual((out["from"], out["to"], out["depart"],
                              out["arrive"]), ("B", "V-A", -1, 1))
            self.assertEqual((back["from"], back["to"], back["depart"],
                              back["arrive"]), ("V-A", "B", 3, 5))
            svc.close()

    def test_station_vouchers_queryable_without_any_shipping_order(self):
        self.office.generate_plan(self.ed_before)
        # 器材库尚未存在/无任何托运单，车站凭单照样可查
        vouch = self.office.vouchers_for_station(self.ed_before, "A")
        self.assertEqual({r["run_id"] for r in vouch["runs"]},
                         {"R-ab-B", "R-ac-A", "R-ad-A"})
        with self.assertRaises(KeyError):
            self.office.vouchers_for_station(self.ed_before, "GHOST")

    def test_second_generation_rejected_revisions_only(self):
        self.office.generate_plan(self.ed_before)
        with self.assertRaises(TransportRejected) as ctx:
            self.office.generate_plan(self.ed_before)
        self.assertEqual(ctx.exception.issues[0].code, "plan_exists")

    def test_unknown_edition_rejected(self):
        with self.assertRaises(TransportRejected) as ctx:
            self.office.generate_plan(99)
        self.assertEqual(ctx.exception.issues[0].code, "edition_unknown")


class TestOverlapAndConfigValidation(unittest.TestCase):
    def _make(self, timetable, travel=None):
        self.tmp = tempfile.TemporaryDirectory()
        svc = ScheduleService(os.path.join(self.tmp.name, "s.db"))
        svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
        ed = svc.publish(FIXTURE4)["edition_id"]
        office = TransportOffice(svc)
        travel = travel or {(a, b): 1 for a in TEAMS4 for b in TEAMS4 if a != b}
        return svc, office, ed

    def test_same_team_overlapping_segments_reject_entire_plan(self):
        # 轮次窗口本身不交错（通过配置校验），但 A 队 r2 回程 [6,7) 与其
        # r3 去程 [6,7) 严格重叠 -> 生成环节整版拒绝，无任何凭单落库。
        timetable = [
            {"round": 1, "slot": "s", "kickoff": 2, "dispersal": 3},
            {"round": 2, "slot": "s", "kickoff": 5, "dispersal": 6},
            {"round": 3, "slot": "s", "kickoff": 7, "dispersal": 8},
        ]
        svc, office, ed = self._make(timetable)
        try:
            office.configure(
                team_bases={t: t for t in TEAMS4},
                team_venues={t: t for t in TEAMS4},
                travel={(a, b): 1 for a in TEAMS4 for b in TEAMS4 if a != b},
                timetable=timetable,
            )
            with self.assertRaises(TransportRejected) as ctx:
                office.generate_plan(ed)
            self.assertEqual(ctx.exception.issues[0].code, "segment_overlap")
            self.assertEqual(office.list_plans(), [])
        finally:
            svc.close()
            self.tmp.cleanup()

    def test_endpoint_touch_is_not_overlap(self):
        # 同刻到达允许接续：前一段 arrive == 下一段 depart 合法（端点相接）
        timetable = [
            {"round": 1, "slot": "s", "kickoff": 2, "dispersal": 3},
            {"round": 2, "slot": "s", "kickoff": 4, "dispersal": 5},
            {"round": 3, "slot": "s", "kickoff": 6, "dispersal": 7},
        ]
        svc, office, ed = self._make(timetable)
        try:
            office.configure(
                team_bases={t: t for t in TEAMS4},
                team_venues={t: t for t in TEAMS4},
                travel={(a, b): 1 for a in TEAMS4 for b in TEAMS4 if a != b},
                timetable=timetable,
            )
            plan = office.generate_plan(ed)  # 不抛重叠
            self.assertEqual(len(plan["runs"]), 6)
        finally:
            svc.close()
            self.tmp.cleanup()

    def test_missing_travel_duration_rejected(self):
        svc, office, ed = self._make(TIMETABLE)
        try:
            with self.assertRaises(TransportRejected) as ctx:
                office.configure(
                    team_bases={t: t for t in TEAMS4},
                    team_venues={t: t for t in TEAMS4},
                    travel={("A", "B"): 1},  # 几乎全部有向站对缺失
                    timetable=TIMETABLE,
                )
            codes = {i.code for i in ctx.exception.issues}
            self.assertIn("travel_missing", codes)
        finally:
            svc.close()
            self.tmp.cleanup()

    def test_zero_duration_rejected(self):
        svc, office, ed = self._make(TIMETABLE)
        try:
            travel = {(a, b): 1 for a in TEAMS4 for b in TEAMS4 if a != b}
            travel[("A", "B")] = 0
            with self.assertRaises(TransportRejected) as ctx:
                office.configure(
                    team_bases={t: t for t in TEAMS4},
                    team_venues={t: t for t in TEAMS4},
                    travel=travel, timetable=TIMETABLE,
                )
            self.assertIn("bad_travel_duration",
                          {i.code for i in ctx.exception.issues})
        finally:
            svc.close()
            self.tmp.cleanup()


class TestRevisionsAndPersistence(TransportTestBase):
    def test_cancel_keeps_old_version_and_marks_new(self):
        v1 = self.office.generate_plan(self.ed_before)
        self.assertEqual(v1["version"], 1)
        v2 = self.office.cancel_run(self.ed_before, "R-ab-B")
        self.assertEqual(v2["version"], 2)
        cancelled = next(r for r in v2["runs"] if r["run_id"] == "R-ab-B")
        self.assertEqual(cancelled["status"], RUN_CANCELLED)
        # 旧版凭单原封不动
        old = self.office.get_plan(self.ed_before, version=1)
        self.assertEqual(next(r for r in old["runs"]
                              if r["run_id"] == "R-ab-B")["status"], "active")
        # 旧赛程 edition 的方案不受影响
        other = self.office.generate_plan(self.ed_after)
        self.assertTrue(all(r["status"] == "active" for r in other["runs"]))

    def test_cancel_unknown_run_rejected_without_side_effect(self):
        self.office.generate_plan(self.ed_before)
        with self.assertRaises(TransportRejected) as ctx:
            self.office.cancel_run(self.ed_before, "R-nope")
        self.assertEqual(ctx.exception.issues[0].code, "run_unknown")
        self.assertEqual(self.office.get_plan(self.ed_before)["version"], 1)

    def test_revise_platform_freezes_times_and_stations(self):
        self.office.generate_plan(self.ed_before)
        v2 = self.office.revise_platform(self.ed_before, "R-ab-B", "P9")
        run = next(r for r in v2["runs"] if r["run_id"] == "R-ab-B")
        self.assertEqual(run["pickup_platform"], "P9")
        before = {(l["direction"], l["from"], l["to"], l["depart"],
                   l["arrive"], l["capacity"]) for l in
                  next(r for r in self.office.get_plan(
                      self.ed_before, 1)["runs"]
                       if r["run_id"] == "R-ab-B")["legs"]}
        after = {(l["direction"], l["from"], l["to"], l["depart"],
                  l["arrive"], l["capacity"]) for l in run["legs"]}
        self.assertEqual(before, after)

    def test_platform_revision_rejected_on_cancelled_run(self):
        self.office.generate_plan(self.ed_before)
        self.office.cancel_run(self.ed_before, "R-ab-B")
        with self.assertRaises(TransportRejected) as ctx:
            self.office.revise_platform(self.ed_before, "R-ab-B", "P9")
        self.assertEqual(ctx.exception.issues[0].code, "run_cancelled")

    def test_reopen_persistence(self):
        self.office.generate_plan(self.ed_before)
        self.office.cancel_run(self.ed_before, "R-ab-B")
        self.svc.close()
        svc2 = ScheduleService(self.sdb)
        office2 = TransportOffice(svc2)
        plans = office2.list_plans()
        self.assertEqual([(p["edition_id"], p["version"]) for p in plans],
                         [(self.ed_before, 1), (self.ed_before, 2)])
        latest = office2.get_plan(self.ed_before)
        self.assertEqual(next(r for r in latest["runs"]
                              if r["run_id"] == "R-ab-B")["status"],
                         RUN_CANCELLED)
        # 重开后仍可继续修订产生 v3
        v3 = office2.revise_platform(self.ed_before, "R-cd-D", "P3")
        self.assertEqual(v3["version"], 3)
        svc2.close()


if __name__ == "__main__":
    unittest.main()
