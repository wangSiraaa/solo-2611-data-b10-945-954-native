"""器材库：封箱托运、付费直送采购与最小费用求解（独立 SQLite，只读赛程库）。

事实模型
========
* 托运方一次交一批**密封箱**：箱号、起点、可取时刻、目的地、最晚入站时刻。
  箱不可拆；箱可以在车站**留站等待**（免费、不产生任何运输关系），但交下一班
  之前前程必须真实到达——同刻到达允许接续；所有车程严格正时长。
* 可乘关系只有两类真实来源：赛程库中当前班车方案里**未退运**班次的车段，
  以及本库登记后被方案**采购**的付费直送单。不存在任何虚构的免费转站。
* 付费直送单标定起讫站、发到时刻、容量与整单费用：整单只买一次，一次采购后
  可按容量带多只实物箱；不采购则不能用。
* 求解要求全部箱如约到站，最小化直送采购总费用，并为每只箱输出完整交接单
  （含留站等待与逐段交接）；不能全部兑现时给出可定位原因，不伪造方案。
* 单题规模上限：至多 6 只箱、12 班直送单，超出在登记环节拒绝。

与 :mod:`league.transport` 的关系：器材库以只读 URI 打开赛程库读取班车凭单，
自身的箱/直送单/方案落在独立 SQLite 文件，两个仓写入边界互不污染。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .schedule_service import Issue

MAX_BOXES = 6
MAX_OFFERS = 12


class ShippingRejected(ValueError):
    """托运登记或求解被拒绝；携带可定位原因，整批无副作用。"""

    def __init__(self, issues: Sequence[Issue]):
        self.issues = list(issues)
        super().__init__(f"shipping rejected with {len(self.issues)} issue(s)")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _content_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class EquipmentDepot:
    """器材库：独立 SQLite + 只读赛程库连接。"""

    def __init__(self, depot_db_path: str, schedule_db_path: str):
        self.depot_db_path = depot_db_path
        first_time = not os.path.exists(depot_db_path)
        self.conn = sqlite3.connect(depot_db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        uri = "file:" + os.path.abspath(schedule_db_path) + "?mode=ro"
        self.sched = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.sched.row_factory = sqlite3.Row
        if first_time:
            self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE ship_request_log (
                request_id   TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                seq          INTEGER NOT NULL,
                applied_at   TEXT NOT NULL
            );
            CREATE TABLE box (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                box_id       TEXT NOT NULL UNIQUE,
                origin       TEXT NOT NULL,
                destination  TEXT NOT NULL,
                available_at INTEGER NOT NULL,
                deadline     INTEGER NOT NULL,
                request_id   TEXT NOT NULL,
                seq          INTEGER NOT NULL
            );
            CREATE TABLE direct_offer (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id     TEXT NOT NULL UNIQUE,
                origin       TEXT NOT NULL,
                destination  TEXT NOT NULL,
                depart       INTEGER NOT NULL,
                arrive       INTEGER NOT NULL,
                capacity     INTEGER NOT NULL,
                fee          INTEGER NOT NULL,
                request_id   TEXT NOT NULL,
                seq          INTEGER NOT NULL
            );
            CREATE TABLE ship_solution (
                solution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  TEXT NOT NULL,
                edition_id  INTEGER NOT NULL,
                plan_id     INTEGER NOT NULL,
                version     INTEGER NOT NULL,
                feasible    INTEGER NOT NULL,
                payload     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------ 幂等/序号
    def _check_idempotent(self, request_id: str, kind: str,
                          payload: Any) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT kind, content_hash, seq FROM ship_request_log WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        digest = _content_hash(payload)
        if row["kind"] != kind or row["content_hash"] != digest:
            raise ShippingRejected([Issue("request_id_conflict",
                                          {"request_id": request_id,
                                           "stored_kind": row["kind"]},
                                          "同 request_id 已以不同内容/类型接收，同号异文拒绝")])
        return {"request_id": request_id, "kind": kind,
                "seq": row["seq"], "replayed": True}

    def _next_seq(self) -> int:
        return int(self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM ship_request_log").fetchone()["s"])

    def _transport_stations(self) -> set[str]:
        if self.sched.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='transport_station'").fetchone() is None:
            raise ShippingRejected([Issue("transport_not_configured", {},
                                          "赛程库尚未建立运输办公室，无车站/班车事实")])
        return {r["station_id"] for r in
                self.sched.execute("SELECT station_id FROM transport_station")}

    # ------------------------------------------------------------ 封箱登记
    def register_boxes(self, request_id: str,
                       boxes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """原子登记一批密封箱（同号同文复读、同号异文拒绝）。"""
        payload = [dict(b) for b in boxes]
        replay = self._check_idempotent(request_id, "boxes", payload)
        if replay is not None:
            return replay
        if self.conn.execute("SELECT 1 FROM box").fetchone() is not None:
            raise ShippingRejected([Issue("boxes_already_registered", {},
                                          "封箱集合已登记，本题单仓只接受一批托运箱")])

        issues: list[Issue] = []
        if not isinstance(boxes, Sequence) or isinstance(boxes, (str, bytes)):
            raise ShippingRejected([Issue("bad_boxes_shape", {}, "封箱必须是字典序列")])
        if len(boxes) == 0:
            issues.append(Issue("empty_boxes", {}, "至少登记一只箱"))
        if len(boxes) > MAX_BOXES:
            issues.append(Issue("too_many_boxes", {"count": len(boxes), "limit": MAX_BOXES},
                                f"至多登记 {MAX_BOXES} 只箱"))
        stations = self._transport_stations()
        if not stations:
            issues.append(Issue("transport_not_configured", {},
                                "赛程库尚未配置运输车站，无法登记封箱"))
        seen: set[str] = set()
        for i, b in enumerate(boxes):
            bid = b.get("box_id")
            if not isinstance(bid, str) or not bid:
                issues.append(Issue("bad_box_id", {"index": i}, "箱号必须是非空字符串"))
            elif bid in seen:
                issues.append(Issue("duplicate_box_id", {"index": i, "box_id": bid},
                                    "批内箱号重复"))
            else:
                seen.add(bid)
            origin, dest = b.get("origin"), b.get("destination")
            avail, deadline = b.get("available_at"), b.get("deadline")
            for name, val in (("origin", origin), ("destination", dest)):
                if val not in stations:
                    issues.append(Issue("unknown_station",
                                        {"index": i, "box_id": bid, "field": name,
                                         "station": val}, "引用了运输配置外的车站"))
            if origin == dest and origin in stations:
                issues.append(Issue("zero_leg_box", {"index": i, "box_id": bid},
                                    "起讫站相同的箱无需托运"))
            for name, val in (("available_at", avail), ("deadline", deadline)):
                if not isinstance(val, int) or isinstance(val, bool):
                    issues.append(Issue("bad_time", {"index": i, "box_id": bid,
                                                     "field": name, "value": val},
                                        "时刻必须为整数"))
            if isinstance(avail, int) and isinstance(deadline, int) and not isinstance(
                    avail, bool) and not isinstance(deadline, bool) and deadline < avail:
                issues.append(Issue("deadline_before_available",
                                    {"index": i, "box_id": bid},
                                    "最晚入站时刻早于可取时刻"))
        if issues:
            raise ShippingRejected(issues)

        seq = self._next_seq()
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO ship_request_log(request_id, kind, content_hash, seq, applied_at)"
                " VALUES(?, 'boxes', ?, ?, ?)",
                (request_id, _content_hash(payload), seq,
                 datetime.now(timezone.utc).isoformat()),
            )
            cur.executemany(
                "INSERT INTO box(box_id, origin, destination, available_at, deadline,"
                " request_id, seq) VALUES(?, ?, ?, ?, ?, ?, ?)",
                [(b["box_id"], b["origin"], b["destination"], b["available_at"],
                  b["deadline"], request_id, seq) for b in boxes],
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return {"request_id": request_id, "kind": "boxes", "seq": seq,
                "replayed": False, "boxes": len(boxes)}

    # ------------------------------------------------------------ 直送单登记
    def register_offers(self, request_id: str,
                        offers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """原子登记一批可采购的付费直送单（整单费用、容量固定）。"""
        payload = [dict(o) for o in offers]
        replay = self._check_idempotent(request_id, "offers", payload)
        if replay is not None:
            return replay
        if self.conn.execute("SELECT 1 FROM direct_offer").fetchone() is not None:
            raise ShippingRejected([Issue("offers_already_registered", {},
                                          "直送单集合已登记，本仓只接受一批")])

        issues: list[Issue] = []
        if not isinstance(offers, Sequence) or isinstance(offers, (str, bytes)):
            raise ShippingRejected([Issue("bad_offers_shape", {}, "直送单必须是字典序列")])
        if len(offers) > MAX_OFFERS:
            issues.append(Issue("too_many_offers",
                                {"count": len(offers), "limit": MAX_OFFERS},
                                f"至多登记 {MAX_OFFERS} 班付费直送"))
        stations = self._transport_stations()
        if not stations:
            issues.append(Issue("transport_not_configured", {},
                                "赛程库尚未配置运输车站，无法登记直送单"))
        seen: set[str] = set()
        for i, o in enumerate(offers):
            oid = o.get("offer_id")
            if not isinstance(oid, str) or not oid:
                issues.append(Issue("bad_offer_id", {"index": i}, "直送单号必须是非空字符串"))
            elif oid in seen:
                issues.append(Issue("duplicate_offer_id", {"index": i, "offer_id": oid},
                                    "批内直送单号重复"))
            else:
                seen.add(oid)
            for name in ("origin", "destination"):
                if o.get(name) not in stations:
                    issues.append(Issue("unknown_station",
                                        {"index": i, "offer_id": oid, "field": name,
                                         "station": o.get(name)},
                                        "引用了运输配置外的车站"))
            dep, arr = o.get("depart"), o.get("arrive")
            cap, fee = o.get("capacity"), o.get("fee")
            for name, val in (("depart", dep), ("arrive", arr),
                              ("capacity", cap), ("fee", fee)):
                if not isinstance(val, int) or isinstance(val, bool):
                    issues.append(Issue("bad_offer_field",
                                        {"index": i, "offer_id": oid,
                                         "field": name, "value": val},
                                        "该字段必须为整数"))
            if isinstance(dep, int) and isinstance(arr, int) and not isinstance(
                    dep, bool) and not isinstance(arr, bool) and arr <= dep:
                issues.append(Issue("non_positive_travel",
                                    {"index": i, "offer_id": oid},
                                    "直送车程必须严格正时长（arrive > depart）"))
            if isinstance(cap, int) and not isinstance(cap, bool) and cap <= 0:
                issues.append(Issue("bad_offer_capacity",
                                    {"index": i, "offer_id": oid}, "容量必须为正整数"))
            if isinstance(fee, int) and not isinstance(fee, bool) and fee < 0:
                issues.append(Issue("bad_offer_fee", {"index": i, "offer_id": oid},
                                    "整单费用必须为非负整数"))
        if issues:
            raise ShippingRejected(issues)

        seq = self._next_seq()
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO ship_request_log(request_id, kind, content_hash, seq, applied_at)"
                " VALUES(?, 'offers', ?, ?, ?)",
                (request_id, _content_hash(payload), seq,
                 datetime.now(timezone.utc).isoformat()),
            )
            cur.executemany(
                "INSERT INTO direct_offer(offer_id, origin, destination, depart, arrive,"
                " capacity, fee, request_id, seq) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(o["offer_id"], o["origin"], o["destination"], o["depart"], o["arrive"],
                  o["capacity"], o["fee"], request_id, seq) for o in offers],
            )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return {"request_id": request_id, "kind": "offers", "seq": seq,
                "replayed": False, "offers": len(offers)}

    # ------------------------------------------------------------ 读取
    def list_boxes(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT box_id, origin, destination, available_at, deadline FROM box"
            " ORDER BY id")]

    def list_offers(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT offer_id, origin, destination, depart, arrive, capacity, fee"
            " FROM direct_offer ORDER BY id")]

    def _active_legs(self, edition_id: int, version: int | None) -> list[dict[str, Any]]:
        self._transport_stations()  # 运输表不存在时给干净的业务拒绝而非底层报错
        if version is None:
            plan = self.sched.execute(
                "SELECT plan_id, version FROM transport_plan WHERE edition_id = ?"
                " ORDER BY version DESC LIMIT 1", (edition_id,)).fetchone()
        else:
            plan = self.sched.execute(
                "SELECT plan_id, version FROM transport_plan"
                " WHERE edition_id = ? AND version = ?",
                (edition_id, version)).fetchone()
        if plan is None:
            raise ShippingRejected([Issue("plan_missing", {"edition_id": edition_id},
                                          "该 edition 没有班车方案，无可乘车段")])
        plan_id, ver = plan["plan_id"], plan["version"]
        legs = []
        for r in self.sched.execute(
                "SELECT l.run_id, l.direction, l.from_station, l.to_station,"
                " l.depart, l.arrive, l.capacity, u.pickup_platform, u.match_id, u.team_id"
                " FROM transport_leg l JOIN transport_run u"
                " ON l.plan_id = u.plan_id AND l.run_id = u.run_id"
                " WHERE l.plan_id = ? AND u.status = 'active'"
                " ORDER BY l.depart, l.run_id, l.direction", (plan_id,)):
            legs.append({"kind": "shuttle",
                         "lid": f"{plan_id}:{r['run_id']}:{r['direction']}",
                         "run_id": r["run_id"], "direction": r["direction"],
                         "match_id": r["match_id"], "team": r["team_id"],
                         "from": r["from_station"], "to": r["to_station"],
                         "depart": r["depart"], "arrive": r["arrive"],
                         "capacity": r["capacity"],
                         "pickup_platform": r["pickup_platform"]})
        for r in self.conn.execute(
                "SELECT offer_id, origin, destination, depart, arrive, capacity, fee"
                " FROM direct_offer ORDER BY depart, offer_id"):
            legs.append({"kind": "direct", "lid": f"offer:{r['offer_id']}",
                         "offer_id": r["offer_id"],
                         "from": r["origin"], "to": r["destination"],
                         "depart": r["depart"], "arrive": r["arrive"],
                         "capacity": r["capacity"], "fee": r["fee"]})
        return legs

    # ------------------------------------------------------------ 求解
    def _earliest_arrivals(self, legs: list[dict[str, Any]],
                           origin: str, start_time: int) -> dict[str, int]:
        """忽略容量的最早可达时刻：按发车时刻扫描（正时长保证时间拓扑序）。"""
        earliest = {origin: start_time}
        for leg in sorted(legs, key=lambda x: (x["depart"], x["lid"])):
            ready = earliest.get(leg["from"])
            if ready is not None and ready <= leg["depart"]:
                got = leg["arrive"]
                if leg["to"] not in earliest or got < earliest[leg["to"]]:
                    earliest[leg["to"]] = got
        return earliest

    def _enumerate_itineraries(self, legs: list[dict[str, Any]],
                               box: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
        """枚举一只箱的全部可行交接链；留站等待隐含在相邻车段的时刻间隔里。"""
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for leg in legs:
            adjacency.setdefault(leg["from"], []).append(leg)
        for sts in adjacency.values():
            sts.sort(key=lambda x: (x["depart"], x["lid"]))

        results: list[list[dict[str, Any]]] = []
        deadline = box["deadline"]

        def walk(station: str, now: int, path: list[dict[str, Any]]) -> None:
            if station == box["destination"]:
                results.append(list(path))
                return  # 已如约到站；正时长下离开再返回不会更优
            for leg in adjacency.get(station, ()):
                if leg["depart"] < now or leg["arrive"] > deadline:
                    continue
                path.append(leg)
                walk(leg["to"], leg["arrive"], path)
                path.pop()

        walk(box["origin"], box["available_at"], [])
        # 稳定、可复现的顺序：先直送费低、段数少、字典序小
        results.sort(key=lambda p: (
            sum(x.get("fee", 0) for x in p),
            len(p),
            tuple(x["lid"] for x in p),
        ))
        return results

    @staticmethod
    def _manifest(box: Mapping[str, Any],
                  itinerary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把交接链展开为完整交接单，显式补上留站等待段。"""
        chain: list[dict[str, Any]] = []
        now = box["available_at"]
        station = box["origin"]
        for leg in itinerary:
            if leg["depart"] > now:
                chain.append({"kind": "wait", "station": station,
                              "from_time": now, "to_time": leg["depart"]})
            entry = {"kind": leg["kind"], "lid": leg["lid"],
                     "from": leg["from"], "to": leg["to"],
                     "depart": leg["depart"], "arrive": leg["arrive"]}
            if leg["kind"] == "shuttle":
                entry.update({"run_id": leg["run_id"],
                              "pickup_platform": leg["pickup_platform"]})
            else:
                entry["offer_id"] = leg["offer_id"]
            chain.append(entry)
            now, station = leg["arrive"], leg["to"]
        return chain

    def solve(self, request_id: str, edition_id: int,
              version: int | None = None) -> dict[str, Any]:
        """求全部箱如约到站的最小直送采购费用方案；不可行时返回可定位原因。"""
        boxes = self.list_boxes()
        offers = self.list_offers()
        legs = self._active_legs(edition_id, version)
        plan_row = self.sched.execute(
            "SELECT plan_id, version FROM transport_plan WHERE edition_id = ?"
            " ORDER BY version DESC LIMIT 1", (edition_id,)).fetchone() \
            if version is None else self.sched.execute(
            "SELECT plan_id, version FROM transport_plan"
            " WHERE edition_id = ? AND version = ?",
            (edition_id, version)).fetchone()
        fingerprint = _canonical({
            "edition_id": edition_id, "version": plan_row["version"],
            "boxes": boxes, "offers": offers,
            "legs": [{k: v for k, v in leg.items() if k != "fee"} for leg in legs],
        })
        fingerprint_hash = _content_hash(fingerprint)
        replay = self._check_idempotent(request_id, "solve", fingerprint)
        if replay is not None:
            row = self.conn.execute(
                "SELECT solution_id, payload FROM ship_solution WHERE request_id = ?",
                (request_id,)).fetchone()
            payload = json.loads(row["payload"])
            payload["solution_id"] = row["solution_id"]
            payload["replayed"] = True
            return payload

        if len(boxes) == 0:
            raise ShippingRejected([Issue("no_boxes", {}, "尚未登记任何封箱，无可求解对象")])

        # 与接收批一致：先占 request_id 流水，再计算；同号异文已在上面被拦。
        seq = self._next_seq()
        self.conn.execute(
            "INSERT INTO ship_request_log(request_id, kind, content_hash, seq, applied_at)"
            " VALUES(?, 'solve', ?, ?, ?)",
            (request_id, fingerprint_hash, seq,
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

        # 第一层诊断：单箱无视容量是否根本无法如约
        options: dict[str, list[list[dict[str, Any]]]] = {}
        for box in boxes:
            earliest = self._earliest_arrivals(legs, box["origin"], box["available_at"])
            if box["destination"] not in earliest:
                raise self._persist_infeasible(
                    request_id, edition_id, plan_row, boxes, offers,
                    Issue("no_route", {"box_id": box["box_id"],
                                       "origin": box["origin"],
                                       "destination": box["destination"]},
                          "只凭真实班车与已登记直送单无法到达目的地（无虚构转站）"))
            if earliest[box["destination"]] > box["deadline"]:
                raise self._persist_infeasible(
                    request_id, edition_id, plan_row, boxes, offers,
                    Issue("cannot_meet_deadline",
                          {"box_id": box["box_id"], "deadline": box["deadline"],
                           "earliest_arrival": earliest[box["destination"]]},
                          "最早可达时刻仍晚于最晚入站时刻"))
            paths = self._enumerate_itineraries(legs, box)
            if not paths:
                raise self._persist_infeasible(
                    request_id, edition_id, plan_row, boxes, offers,
                    Issue("no_valid_itinerary", {"box_id": box["box_id"]},
                          "不存在满足可取时刻与最晚入站时刻的交接链"))
            options[box["box_id"]] = paths

        # MRV：可选交接链最少的箱先分配。单题至多 6 箱，分支定界即可完整求解，
        # 不做可能掩盖“实物箱各占容量”语义的聚合。
        order = sorted(boxes, key=lambda b: (len(options[b["box_id"]]), b["box_id"]))
        caps = {leg["lid"]: leg["capacity"] for leg in legs}
        fees = {leg["lid"]: leg["fee"] for leg in legs if leg["kind"] == "direct"}
        loads: dict[str, int] = {}
        best_cost: int | None = None
        best_assign: dict[str, list[dict[str, Any]]] | None = None
        work: dict[str, list[dict[str, Any]]] = {}

        def dfs(idx: int, spent: int) -> None:
            nonlocal best_cost, best_assign
            if best_cost is not None and spent >= best_cost:
                return  # 直送费非负，更深层不可能更省
            if idx == len(order):
                best_cost = spent
                best_assign = dict(work)
                return
            box = order[idx]
            for path in options[box["box_id"]]:
                # 实物箱各占一个箱位：两个同时可用的箱不能当成同一箱
                if any(loads.get(leg["lid"], 0) >= caps[leg["lid"]] for leg in path):
                    continue
                # 整单只买一次：费用只在首只占用该直送单的箱上计入
                added = sum(fees[leg["lid"]] for leg in path
                            if leg["kind"] == "direct"
                            and loads.get(leg["lid"], 0) == 0)
                if best_cost is not None and spent + added >= best_cost:
                    continue
                for leg in path:
                    loads[leg["lid"]] = loads.get(leg["lid"], 0) + 1
                work[box["box_id"]] = path
                dfs(idx + 1, spent + added)
                del work[box["box_id"]]
                for leg in path:
                    loads[leg["lid"]] -= 1

        dfs(0, 0)
        if best_assign is None:
            # 第二层诊断：找出每个箱都必经的瓶颈车段/直送单
            bottlenecks = self._bottlenecks(order, options, caps)
            raise self._persist_infeasible(
                request_id, edition_id, plan_row, boxes, offers,
                Issue("capacity_infeasible",
                      {"bottlenecks": bottlenecks},
                      "全部交接链都受限于实物箱容量，无法把所有箱如约送到"))
        chosen = best_assign
        min_cost = best_cost

        purchased: dict[str, dict[str, Any]] = {}
        manifests = {}
        for box in boxes:
            path = chosen[box["box_id"]]
            manifests[box["box_id"]] = self._manifest(box, path)
            for leg in path:
                if leg["kind"] == "direct":
                    purchased.setdefault(leg["offer_id"], {
                        "offer_id": leg["offer_id"], "from": leg["from"],
                        "to": leg["to"], "depart": leg["depart"],
                        "arrive": leg["arrive"], "capacity": leg["capacity"],
                        "fee": leg["fee"], "box_ids": []})
        for box in boxes:
            for leg in chosen[box["box_id"]]:
                if leg["kind"] == "direct":
                    purchased[leg["offer_id"]]["box_ids"].append(box["box_id"])
        actual_cost = sum(p["fee"] for p in purchased.values())
        assert actual_cost == min_cost

        payload = {
            "request_id": request_id, "feasible": True,
            "edition_id": edition_id, "plan_id": plan_row["plan_id"],
            "version": plan_row["version"],
            "total_direct_fee": min_cost,
            "purchased_offers": [purchased[k] for k in sorted(purchased)],
            "manifests": {b["box_id"]: manifests[b["box_id"]]
                          for b in sorted(boxes, key=lambda x: x["box_id"])},
        }
        self._persist(request_id, edition_id, plan_row, payload, feasible=True)
        return payload

    def _bottlenecks(self, order: list[dict[str, Any]],
                     options: dict[str, list[list[dict[str, Any]]]],
                     caps: dict[str, int]) -> list[dict[str, Any]]:
        report = []
        mandatory: dict[str, set[str]] = {}
        for box in order:
            paths = options[box["box_id"]]
            common = {leg["lid"] for leg in paths[0]}
            for path in paths[1:]:
                common &= {leg["lid"] for leg in path}
            for lid in common:
                mandatory.setdefault(lid, set()).add(box["box_id"])
        for lid, box_ids in sorted(mandatory.items()):
            if len(box_ids) > caps[lid]:
                report.append({"lid": lid, "capacity": caps[lid],
                               "mandatory_boxes": sorted(box_ids)})
        return report

    def _persist_infeasible(self, request_id: str, edition_id: int,
                           plan_row: sqlite3.Row | None,
                           boxes: list[dict[str, Any]],
                           offers: list[dict[str, Any]],
                           issue: Issue) -> "ShippingRejected":
        payload = {"request_id": request_id, "feasible": False,
                   "edition_id": edition_id,
                   "plan_id": plan_row["plan_id"] if plan_row else None,
                   "version": plan_row["version"] if plan_row else None,
                   "reason": issue.to_dict(),
                   "box_ids": [b["box_id"] for b in boxes],
                   "offer_ids": [o["offer_id"] for o in offers]}
        if plan_row is not None:
            self._persist(request_id, edition_id, plan_row, payload, feasible=False)
        return ShippingRejected([issue])

    def _persist(self, request_id: str, edition_id: int,
                 plan_row: sqlite3.Row, payload: dict[str, Any],
                 feasible: bool) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ship_solution(request_id, edition_id, plan_id, version,"
            " feasible, payload, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (request_id, edition_id, plan_row["plan_id"], plan_row["version"],
             1 if feasible else 0, json.dumps(payload, ensure_ascii=False, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        payload["solution_id"] = cur.lastrowid

    def get_solution(self, solution_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload FROM ship_solution WHERE solution_id = ?",
            (solution_id,)).fetchone()
        if row is None:
            raise KeyError(f"solution {solution_id} 不存在")
        return json.loads(row["payload"])

    def list_solutions(self) -> list[dict[str, Any]]:
        return [{"solution_id": r["solution_id"], "request_id": r["request_id"],
                 "edition_id": r["edition_id"], "plan_id": r["plan_id"],
                 "version": r["version"], "feasible": bool(r["feasible"]),
                 "created_at": r["created_at"]}
                for r in self.conn.execute(
                    "SELECT solution_id, request_id, edition_id, plan_id, version,"
                    " feasible, created_at FROM ship_solution ORDER BY solution_id")]

    def close(self) -> None:
        self.conn.close()
        self.sched.close()

    def __enter__(self) -> "EquipmentDepot":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
