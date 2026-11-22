"""器材库：封箱/直送登记、最小直送费求解、容量不可拆分、中站换车与不可行诊断。"""

from __future__ import annotations

import os
import tempfile
import unittest

from league.schedule_service import ScheduleService
from league.transport import TransportOffice
from league.shipping import (
    EquipmentDepot, ShippingRejected, MAX_BOXES, MAX_OFFERS,
)

from tests.fixtures import FIXTURE4

TEAMS = ["A", "B", "C", "D"]
TEAMS4 = TEAMS
REGISTRY = {"ab": 1, "cd": 1, "ac": 2, "bd": 2, "ad": 3, "bc": 3}
SLOTS = {1: {"s": 2}, 2: {"s": 2}, 3: {"s": 2}}
TIMETABLE = [
    {"round": 1, "slot": "s", "kickoff": 2, "dispersal": 3},
    {"round": 2, "slot": "s", "kickoff": 10, "dispersal": 11},
    {"round": 3, "slot": "s", "kickoff": 20, "dispersal": 21},
]
BOX_B_A = {"box_id": "bx1", "origin": "B", "destination": "A",
           "available_at": 0, "deadline": 2}
OFFER_B_A = {"offer_id": "o1", "origin": "B", "destination": "A",
             "depart": 0, "arrive": 1, "capacity": 1, "fee": 5}


class ShippingTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sdb = os.path.join(self.tmp.name, "sched.db")
        self.ddb = os.path.join(self.tmp.name, "depot.db")
        self.svc = ScheduleService(self.sdb)
        self.svc.initialize(TEAMS, REGISTRY, SLOTS, {t: [] for t in TEAMS})
        self.before = [dict(m) for m in FIXTURE4]
        self.after = [
            {**m, "home": m["away"], "away": m["home"]}
            if m["match_id"] in {"ab", "ac", "bc"} else dict(m)
            for m in self.before
        ]
        self.ed_before = self.svc.publish(self.before)["edition_id"]
        self.ed_after = self.svc.publish(self.after)["edition_id"]
        self.office = TransportOffice(self.svc)
        self.office.configure(
            team_bases={t: t for t in TEAMS},
            team_venues={t: t for t in TEAMS},
            travel={(a, b): 1 for a in TEAMS4 for b in TEAMS if a != b},
            timetable=TIMETABLE, lead_beats=1, segment_capacity=1,
        )
        self.plan_before = self.office.generate_plan(self.ed_before)
        self.plan_after = self.office.generate_plan(self.ed_after)
        self.pid = self.plan_before["plan_id"]

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def depot(self, name: str = "depot.db") -> EquipmentDepot:
        return EquipmentDepot(os.path.join(self.tmp.name, name), self.sdb)


class TestNarrativeCosts(ShippingTestBase):
    def test_before_edition_free_shuttle_carries_box_at_zero_cost(self):
        depot = self.depot()
        depot.register_boxes("boxes", [BOX_B_A])
        depot.register_offers("offers", [OFFER_B_A])
        sol = depot.solve("solve-before", self.ed_before)
        self.assertTrue(sol["feasible"])
        self.assertEqual(sol["total_direct_fee"], 0)
        self.assertEqual(sol["purchased_offers"], [])
        chain = sol["manifests"]["bx1"]
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["kind"], "shuttle")
        self.assertEqual((chain[0]["from"], chain[0]["to"],
                          chain[0]["depart"], chain[0]["arrive"]),
                         ("B", "A", 0, 1))
        depot.close()

    def test_flipped_edition_free_leg_leaves_A_so_minimum_cost_is_fee(self):
        depot = self.depot()
        depot.register_boxes("boxes", [BOX_B_A])
        depot.register_offers("offers", [OFFER_B_A])
        sol = depot.solve("solve-after", self.ed_after)
        self.assertEqual(sol["total_direct_fee"], 5)
        self.assertEqual([o["offer_id"] for o in sol["purchased_offers"]],
                         ["o1"])
        # 直送单标定的发到时刻/容量/费用原样进交接与采购单
        offer = sol["purchased_offers"][0]
        self.assertEqual((offer["depart"], offer["arrive"],
                          offer["capacity"], offer["fee"]), (0, 1, 1, 5))
        self.assertEqual(offer["box_ids"], ["bx1"])
        depot.close()

    def test_second_identical_box_forces_purchase_of_one_direct_offer(self):
        depot = self.depot()
        depot.register_boxes("boxes", [
            BOX_B_A,
            {"box_id": "bx2", "origin": "B", "destination": "A",
             "available_at": 0, "deadline": 2},
        ])
        depot.register_offers("offers", [OFFER_B_A])
        sol = depot.solve("solve-two", self.ed_before)
        self.assertEqual(sol["total_direct_fee"], 5)  # 不是 10：整单只买一次
        used = {"bx1": [c for c in sol["manifests"]["bx1"] if c["kind"] != "wait"],
                "bx2": [c for c in sol["manifests"]["bx2"] if c["kind"] != "wait"]}
        kinds = {bid: chain[0]["kind"] for bid, chain in used.items()}
        self.assertEqual(sorted(kinds.values()), ["direct", "shuttle"])
        # 两只箱走的是两个不同的真实运力，不能被当成同一只箱
        lids = {chain[0]["lid"] for chain in used.values()}
        self.assertEqual(len(lids), 2)
        depot.close()

    def test_mid_station_transfer_with_waiting_is_free(self):
        depot = self.depot()
        depot.register_boxes("boxes", [
            {"box_id": "tr", "origin": "B", "destination": "C",
             "available_at": 0, "deadline": 14},
        ])
        sol = depot.solve("solve-transfer", self.ed_before)
        self.assertEqual(sol["total_direct_fee"], 0)
        rides = [c for c in sol["manifests"]["tr"] if c["kind"] == "shuttle"]
        waits = [c for c in sol["manifests"]["tr"] if c["kind"] == "wait"]
        self.assertEqual([(r["from"], r["to"]) for r in rides],
                         [("B", "A"), ("A", "C")])
        # 前程 1 拍到 A，留站等到 8 再交下一班
        self.assertEqual(waits[0]["station"], "A")
        self.assertEqual((waits[0]["from_time"], waits[0]["to_time"]), (1, 8))
        depot.close()

    def test_two_simultaneous_boxes_are_not_one_box_even_with_transfer(self):
        # 同起讫同时限的两只箱都需中站换车，而每段容量 1：不能把两只箱算成一只
        depot = self.depot()
        depot.register_boxes("boxes", [
            {"box_id": "t1", "origin": "B", "destination": "C",
             "available_at": 0, "deadline": 14},
            {"box_id": "t2", "origin": "B", "destination": "C",
             "available_at": 0, "deadline": 14},
        ])
        with self.assertRaises(ShippingRejected) as ctx:
            depot.solve("solve-two-transfer", self.ed_before)
        self.assertEqual(ctx.exception.issues[0].code, "capacity_infeasible")
        bottleneck = ctx.exception.issues[0].location["bottlenecks"]
        self.assertTrue(any(set(b["mandatory_boxes"]) == {"t1", "t2"}
                            and b["capacity"] == 1 for b in bottleneck))
        # 不可行切面也留痕
        self.assertFalse(depot.list_solutions()[-1]["feasible"])
        depot.close()


class TestTimingRules(ShippingTestBase):
    def test_same_time_arrival_allows_connection(self):
        # 同刻到达允许接续：免费班车 R-ab-B 去程 1 拍到 A，直送 e2 在 1 刻
        # 从 A 发车——到达与发车同一拍，允许交接。最优链只买 e2（fee 3），
        # 不必再买 e1（fee 2），求解器必须识别出这条更省的同刻接续。
        depot = self.depot()
        depot.register_boxes("boxes", [
            {"box_id": "eq", "origin": "B", "destination": "C",
             "available_at": 0, "deadline": 4},
        ])
        depot.register_offers("offers", [
            {"offer_id": "e1", "origin": "B", "destination": "A",
             "depart": 0, "arrive": 1, "capacity": 1, "fee": 2},
            {"offer_id": "e2", "origin": "A", "destination": "C",
             "depart": 1, "arrive": 2, "capacity": 1, "fee": 3},
        ])
        sol = depot.solve("solve-equal", self.ed_before)
        chain = [c for c in sol["manifests"]["eq"] if c["kind"] != "wait"]
        self.assertEqual([c["lid"] for c in chain],
                         [f"{self.pid}:R-ab-B:out", "offer:e2"])
        self.assertEqual(sol["total_direct_fee"], 3)
        self.assertEqual([o["offer_id"] for o in sol["purchased_offers"]],
                         ["e2"])
        depot.close()

    def test_equal_time_connection_between_two_direct_offers(self):
        # 没有免费班车可借时，两张直送单之间同样允许同刻接续（1 到 1 发）。
        # deadline=3 排除 4 拍才到 B 的免费回程，唯一可行链为 f1+f2。
        depot = self.depot()
        depot.register_boxes("boxes", [
            {"box_id": "eq2", "origin": "D", "destination": "B",
             "available_at": 0, "deadline": 3},
        ])
        depot.register_offers("offers", [
            {"offer_id": "f1", "origin": "D", "destination": "A",
             "depart": 0, "arrive": 1, "capacity": 1, "fee": 2},
            {"offer_id": "f2", "origin": "A", "destination": "B",
             "depart": 1, "arrive": 2, "capacity": 1, "fee": 3},
        ])
        sol = depot.solve("solve-equal-direct", self.ed_before)
        chain = [c for c in sol["manifests"]["eq2"] if c["kind"] != "wait"]
        self.assertEqual([c["lid"] for c in chain],
                         ["offer:f1", "offer:f2"])
        depot.close()

    def test_strictly_positive_travel_enforced_on_offers(self):
        depot = self.depot()
        with self.assertRaises(ShippingRejected) as ctx:
            depot.register_offers("bad", [
                {"offer_id": "z", "origin": "B", "destination": "A",
                 "depart": 1, "arrive": 1, "capacity": 1, "fee": 5},
            ])
        self.assertEqual(ctx.exception.issues[0].code, "non_positive_travel")
        depot.close()

    def test_deadline_endpoint_inclusive(self):
        # 最晚入站时刻含端点：1 拍到 A，deadline=1 仍守约（题面 2 拍前含端点同理）
        depot = self.depot()
        depot.register_boxes("boxes", [
            {"box_id": "d1", "origin": "B", "destination": "A",
             "available_at": 0, "deadline": 1},
        ])
        sol = depot.solve("solve-deadline", self.ed_before)
        self.assertTrue(sol["feasible"])
        depot.close()

    def test_missed_deadline_reported_with_earliest_arrival(self):
        depot = self.depot()
        depot.register_boxes("boxes", [
            {"box_id": "late", "origin": "B", "destination": "A",
             "available_at": 0, "deadline": 0},
        ])
        with self.assertRaises(ShippingRejected) as ctx:
            depot.solve("solve-late", self.ed_before)
        issue = ctx.exception.issues[0]
        self.assertEqual(issue.code, "cannot_meet_deadline")
        self.assertEqual(issue.location["earliest_arrival"], 1)
        depot.close()

    def test_no_fictional_transfer_when_no_real_route(self):
        # 目的地 E 是运输配置内的真实车站，但没有任何车段/直送单通往 E：
        # 必须报 no_route，而不是虚构一个免费转站把箱送过去。
        depot = self.depot()
        self.svc.conn.execute(
            "INSERT INTO transport_station(station_id, ord) VALUES('E', 99)")
        self.svc.conn.commit()
        depot.register_boxes("boxes-noroute", [
            {"box_id": "lost", "origin": "B", "destination": "E",
             "available_at": 0, "deadline": 30},
        ])
        with self.assertRaises(ShippingRejected) as ctx:
            depot.solve("solve-noroute", self.ed_before)
        self.assertEqual(ctx.exception.issues[0].code, "no_route")
        depot.close()


class TestRegistrationAndIdempotency(ShippingTestBase):
    def test_box_and_offer_limits(self):
        depot = self.depot()
        too_many_boxes = [
            {"box_id": f"b{i}", "origin": "B", "destination": "A",
             "available_at": 0, "deadline": 2} for i in range(MAX_BOXES + 1)]
        with self.assertRaises(ShippingRejected) as ctx:
            depot.register_boxes("too-many", too_many_boxes)
        self.assertEqual(ctx.exception.issues[0].code, "too_many_boxes")
        too_many_offers = [
            {"offer_id": f"o{i}", "origin": "B", "destination": "A",
             "depart": 0, "arrive": 1, "capacity": 1, "fee": 1}
            for i in range(MAX_OFFERS + 1)]
        with self.assertRaises(ShippingRejected) as ctx2:
            depot.register_offers("too-many-o", too_many_offers)
        self.assertEqual(ctx2.exception.issues[0].code, "too_many_offers")
        depot.close()

    def test_replayed_request_returns_same_receipt_conflicting_rejected(self):
        depot = self.depot()
        r1 = depot.register_boxes("boxes", [BOX_B_A])
        r2 = depot.register_boxes("boxes", [BOX_B_A])
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["seq"], r2["seq"])
        with self.assertRaises(ShippingRejected):
            depot.register_boxes("boxes", [
                {"box_id": "other", "origin": "B", "destination": "A",
                 "available_at": 0, "deadline": 2}])
        depot.close()

    def test_solve_replay_returns_same_solution_and_persists(self):
        depot = self.depot()
        depot.register_boxes("boxes", [BOX_B_A])
        depot.register_offers("offers", [OFFER_B_A])
        s1 = depot.solve("sv", self.ed_after)
        s2 = depot.solve("sv", self.ed_after)
        self.assertEqual(s1["solution_id"], s2["solution_id"])
        self.assertEqual(s2["total_direct_fee"], 5)
        stored = depot.get_solution(s1["solution_id"])
        self.assertEqual(stored["manifests"]["bx1"][0]["offer_id"], "o1")
        depot.close()

    def test_unknown_station_and_bad_times_rejected(self):
        depot = self.depot()
        with self.assertRaises(ShippingRejected) as ctx:
            depot.register_boxes("bad", [
                {"box_id": "x", "origin": "Z", "destination": "A",
                 "available_at": 0, "deadline": 2},
            ])
        codes = {i.code for i in ctx.exception.issues}
        self.assertIn("unknown_station", codes)
        with self.assertRaises(ShippingRejected) as ctx2:
            depot.register_boxes("bad2", [
                {"box_id": "y", "origin": "B", "destination": "A",
                 "available_at": 5, "deadline": 2},
            ])
        self.assertEqual(ctx2.exception.issues[0].code,
                         "deadline_before_available")
        depot.close()


class TestPlanVersionCoupling(ShippingTestBase):
    def test_cancelled_run_disappears_from_shipping_network(self):
        depot = self.depot()
        depot.register_boxes("boxes", [
            BOX_B_A,
            {"box_id": "bx2", "origin": "B", "destination": "A",
             "available_at": 0, "deadline": 2},
        ])
        depot.register_offers("offers", [OFFER_B_A])
        self.assertEqual(depot.solve("v1", self.ed_before)["total_direct_fee"], 5)
        self.office.cancel_run(self.ed_before, "R-ab-B")
        with self.assertRaises(ShippingRejected) as ctx:
            depot.solve("v2", self.ed_before)
        self.assertEqual(ctx.exception.issues[0].code, "capacity_infeasible")
        # 指定旧版本求解仍能乘到未退运的车
        old = depot.solve("v-old", self.ed_before, version=1)
        self.assertEqual(old["version"], 1)
        self.assertEqual(old["total_direct_fee"], 5)
        depot.close()

    def test_reopen_depot_reads_latest_plan(self):
        depot = self.depot()
        depot.register_boxes("boxes", [BOX_B_A])
        depot.register_offers("offers", [OFFER_B_A])
        depot.close()
        depot2 = self.depot("depot.db")
        sol = depot2.solve("reopen", self.ed_before)
        self.assertEqual(sol["total_direct_fee"], 0)
        depot2.close()

    def test_depot_against_schedule_without_transport_office(self):
        bare_db = os.path.join(self.tmp.name, "bare.db")
        bare = ScheduleService(bare_db)
        bare.initialize(TEAMS, REGISTRY, SLOTS, {t: [] for t in TEAMS})
        bare.publish(self.before)
        depot = EquipmentDepot(os.path.join(self.tmp.name, "bare-depot.db"), bare_db)
        with self.assertRaises(ShippingRejected) as ctx:
            depot.register_boxes("b", [BOX_B_A])
        self.assertEqual(ctx.exception.issues[0].code,
                         "transport_not_configured")
        depot.close()
        bare.close()


class TestCapacityAndFixedCharge(unittest.TestCase):
    def test_capacity_two_shuttle_and_multi_box_offer_single_fee(self):
        with tempfile.TemporaryDirectory() as d:
            sdb = os.path.join(d, "s.db")
            svc = ScheduleService(sdb)
            svc.initialize(TEAMS, REGISTRY, SLOTS, {t: [] for t in TEAMS})
            ed = svc.publish(FIXTURE4)["edition_id"]
            office = TransportOffice(svc)
            office.configure(
                team_bases={t: t for t in TEAMS},
                team_venues={t: t for t in TEAMS},
                travel={(a, b): 1 for a in TEAMS for b in TEAMS if a != b},
                timetable=TIMETABLE, lead_beats=1, segment_capacity=2,
            )
            office.generate_plan(ed)
            depot = EquipmentDepot(os.path.join(d, "dep.db"), sdb)
            # 三只箱同起讫同时限：班车段容量 2 可免费带两只，第三只走直送
            depot.register_boxes("boxes", [
                {"box_id": f"b{i}", "origin": "B", "destination": "A",
                 "available_at": 0, "deadline": 2} for i in range(3)])
            # 容量 3 的直送单：一次采购可带多箱，整单只收一次费
            depot.register_offers("offers", [
                {"offer_id": "big", "origin": "B", "destination": "A",
                 "depart": 0, "arrive": 1, "capacity": 3, "fee": 7}])
            sol = depot.solve("sv", ed)
            self.assertEqual(sol["total_direct_fee"], 7)
            offer = sol["purchased_offers"][0]
            self.assertEqual(len(offer["box_ids"]), 1)  # 只需 1 只走直送
            free = [bid for bid, chain in sol["manifests"].items()
                    if all(c["kind"] == "shuttle" for c in chain)]
            self.assertEqual(len(free), 2)  # 容量 2 的班车段免费带两只
            depot.close()
            svc.close()

    def test_offer_capacity_three_carries_three_boxes_for_one_fee(self):
        with tempfile.TemporaryDirectory() as d:
            sdb = os.path.join(d, "s.db")
            svc = ScheduleService(sdb)
            svc.initialize(TEAMS, REGISTRY, SLOTS, {t: [] for t in TEAMS})
            ed = svc.publish(FIXTURE4)["edition_id"]
            office = TransportOffice(svc)
            office.configure(
                team_bases={t: t for t in TEAMS},
                team_venues={t: t for t in TEAMS},
                travel={(a, b): 1 for a in TEAMS for b in TEAMS if a != b},
                timetable=TIMETABLE, lead_beats=1, segment_capacity=1,
            )
            office.generate_plan(ed)
            office.cancel_run(ed, "R-ab-B")  # 撤掉唯一免费 B->A 车
            depot = EquipmentDepot(os.path.join(d, "dep.db"), sdb)
            depot.register_boxes("boxes", [
                {"box_id": f"b{i}", "origin": "B", "destination": "A",
                 "available_at": 0, "deadline": 2} for i in range(3)])
            depot.register_offers("offers", [
                {"offer_id": "big", "origin": "B", "destination": "A",
                 "depart": 0, "arrive": 1, "capacity": 3, "fee": 7}])
            sol = depot.solve("sv", ed)
            self.assertEqual(sol["total_direct_fee"], 7)  # 三箱仍只买一次
            self.assertEqual(
                sol["purchased_offers"][0]["box_ids"],
                ["b0", "b1", "b2"])
            depot.close()
            svc.close()


if __name__ == "__main__":
    unittest.main()
