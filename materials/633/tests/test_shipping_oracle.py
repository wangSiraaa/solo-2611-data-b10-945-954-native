"""求解器独立对照：用最直白的笛卡尔积枚举复核最小直送费与容量语义。

不调用 :class:`league.shipping.EquipmentDepot` 的分支定界搜索，只复用其
单箱交接链枚举（纯图遍历，无优化），再对“每只箱选哪条链”做完整笛卡尔积，
逐组合核算容量与整单采购费用，取最小值作为独立基准。
"""

from __future__ import annotations

import itertools
import os
import random
import tempfile
import unittest

from league.schedule_service import ScheduleService
from league.transport import TransportOffice
from league.shipping import EquipmentDepot

TEAMS = ["A", "B", "C", "D"]
REGISTRY = {"ab": 1, "cd": 1, "ac": 2, "bd": 2, "ad": 3, "bc": 3}
SLOTS = {1: {"s": 2}, 2: {"s": 2}, 3: {"s": 2}}
FIXTURE = [
    {"match_id": "ab", "round": 1, "home": "A", "away": "B", "slot": "s"},
    {"match_id": "cd", "round": 1, "home": "C", "away": "D", "slot": "s"},
    {"match_id": "ac", "round": 2, "home": "C", "away": "A", "slot": "s"},
    {"match_id": "bd", "round": 2, "home": "B", "away": "D", "slot": "s"},
    {"match_id": "ad", "round": 3, "home": "D", "away": "A", "slot": "s"},
    {"match_id": "bc", "round": 3, "home": "B", "away": "C", "slot": "s"},
]
TIMETABLE = [
    {"round": 1, "slot": "s", "kickoff": 2, "dispersal": 3},
    {"round": 2, "slot": "s", "kickoff": 10, "dispersal": 11},
    {"round": 3, "slot": "s", "kickoff": 20, "dispersal": 21},
]


def brute_force_optimum(depot: EquipmentDepot, edition_id: int):
    """独立基准：笛卡尔积枚举全部交接链组合，返回 (min_cost, feasible)。"""
    boxes = depot.list_boxes()
    legs = depot._active_legs(edition_id, None)  # 复用只读取数，不复用搜索
    caps = {leg["lid"]: leg["capacity"] for leg in legs}
    fees = {leg["lid"]: leg["fee"] for leg in legs if leg["kind"] == "direct"}
    options = []
    for box in boxes:
        paths = depot._enumerate_itineraries(legs, box)
        if not paths:
            return None, False
        options.append(paths)

    best = None
    for combo in itertools.product(*options):
        loads: dict[str, int] = {}
        feasible = True
        for path in combo:
            for leg in path:
                loads[leg["lid"]] = loads.get(leg["lid"], 0) + 1
                if loads[leg["lid"]] > caps[leg["lid"]]:
                    feasible = False
                    break
            if not feasible:
                break
        if not feasible:
            continue
        cost = sum(fee for lid, fee in fees.items() if loads.get(lid, 0) > 0)
        best = cost if best is None else min(best, cost)
    return best, best is not None


class TestSolverOracleCrossCheck(unittest.TestCase):
    def _world(self):
        self.tmp = tempfile.TemporaryDirectory()
        sdb = os.path.join(self.tmp.name, "sched.db")
        svc = ScheduleService(sdb)
        svc.initialize(TEAMS, REGISTRY, SLOTS, {t: [] for t in TEAMS})
        ed = svc.publish(FIXTURE)["edition_id"]
        office = TransportOffice(svc)
        office.configure(
            team_bases={t: t for t in TEAMS},
            team_venues={t: t for t in TEAMS},
            travel={(a, b): 1 for a in TEAMS for b in TEAMS if a != b},
            timetable=TIMETABLE, lead_beats=1, segment_capacity=1,
        )
        office.generate_plan(ed)
        return svc, sdb, ed

    def test_random_instances_match_brute_force(self):
        svc, sdb, ed = self._world()
        rng = random.Random(633)
        stations = TEAMS
        try:
            for trial in range(25):
                ddb = os.path.join(self.tmp.name, f"depot{trial}.db")
                depot = EquipmentDepot(ddb, sdb)
                n_boxes = rng.randint(1, 4)
                boxes = []
                for i in range(n_boxes):
                    o, d = rng.sample(stations, 2)
                    avail = rng.choice([0, 1, 5])
                    boxes.append({
                        "box_id": f"b{i}", "origin": o, "destination": d,
                        "available_at": avail,
                        "deadline": max(avail + 1, rng.choice([2, 9, 14, 25])),
                    })
                n_offers = rng.randint(0, 4)
                offers = []
                for i in range(n_offers):
                    o, d = rng.sample(stations, 2)
                    dep = rng.choice([0, 2, 7])
                    offers.append({
                        "offer_id": f"o{i}", "origin": o, "destination": d,
                        "depart": dep, "arrive": dep + rng.choice([1, 2]),
                        "capacity": rng.choice([1, 2]),
                        "fee": rng.choice([1, 3, 5, 9]),
                    })
                depot.register_boxes(f"boxes-{trial}", boxes)
                if offers:
                    depot.register_offers(f"offers-{trial}", offers)

                bf_cost, bf_feasible = brute_force_optimum(depot, ed)
                try:
                    sol = depot.solve(f"solve-{trial}", ed)
                    self.assertTrue(bf_feasible)
                    self.assertEqual(sol["total_direct_fee"], bf_cost,
                                     f"trial {trial}: {boxes} / {offers}")
                    # 交接单必须逐箱如约：终点、时限、容量、正时长
                    self._assert_manifests_valid(sol, boxes)
                except Exception as exc:
                    if not bf_feasible:
                        self.assertIn(exc.issues[0].code,
                                      ("capacity_infeasible", "no_route",
                                       "cannot_meet_deadline",
                                       "no_valid_itinerary"))
                    else:
                        raise
                depot.close()
        finally:
            svc.close()
            self.tmp.cleanup()

    @staticmethod
    def _assert_manifests_valid(sol, boxes):
        by_id = {b["box_id"]: b for b in boxes}
        for bid, chain in sol["manifests"].items():
            box = by_id[bid]
            rides = [c for c in chain if c["kind"] in ("shuttle", "direct")]
            assert rides, bid
            assert rides[0]["from"] == box["origin"]
            assert rides[-1]["to"] == box["destination"]
            assert rides[0]["depart"] >= box["available_at"]
            assert rides[-1]["arrive"] <= box["deadline"]
            prev_arrival = box["available_at"]
            prev_station = box["origin"]
            for c in rides:
                assert c["depart"] >= prev_arrival  # 同刻接续合法
                assert c["arrive"] > c["depart"]    # 严格正时长
                assert c["from"] == prev_station
                prev_arrival, prev_station = c["arrive"], c["to"]


if __name__ == "__main__":
    unittest.main()
