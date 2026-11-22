"""客队班车运输办公室（写入赛程库，独立前缀表）。

职责边界
========
* 赛务（联赛办公室）侧：按**已发布 edition 的真实主客与 slot** 为每场比赛生成
  客队班车凭单，主队取自 ``edition_match.home``，绝不解析 ``match_id`` 里的字母。
* 每队一处基地（base），每个主队有一个比赛场站（venue），两者允许不同；
  有向站对行车时长由配置固定，且严格为正。
* 场站时刻表按 ``(round, slot)`` 给出开赛/散场时刻；去程按
  ``开赛 - lead_beats`` 抵达、再按时长倒推发车，回程散场发车、顺推抵达。
* 同一客队的去/回程车段与其他场次车段不得严格重叠（端点相接允许，对应
  “同刻到达允许接续”），时长必须齐全，否则整版拒绝、不产生任何凭单。
* 每个 edition 的班车方案整体版本化：首版生成后只允许“退运指定班次”或
  “修改接货站台”这类整版修订，旧版凭单（真实可乘的发到站、时刻、可带箱数）
  永久保留；旧赛程 edition 的方案互不影响。
* 车站查询不依赖任何托运单：即使器材库一只箱也没交，凭单照样可查。

器材库（:mod:`league.shipping`）以只读 URI 连接本库读取凭单，不能反向写入。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .schedule_service import Issue

OUTBOUND = "out"
RETURN = "back"
RUN_ACTIVE = "active"
RUN_CANCELLED = "cancelled"
DEFAULT_PLATFORM = "P1"


class TransportRejected(ValueError):
    """运输配置或班车方案未通过校验，携带全部可定位问题，无任何写入副作用。"""

    def __init__(self, issues: Sequence[Issue]):
        self.issues = list(issues)
        super().__init__(f"transport rejected with {len(self.issues)} issue(s)")


@dataclass(frozen=True)
class _Leg:
    run_id: str
    direction: str
    from_station: str
    to_station: str
    depart: int
    arrive: int
    capacity: int


class TransportOffice:
    """在赛程 SQLite 文件内维护运输配置与版本化班车凭单。"""

    def __init__(self, schedule_service: "ScheduleService"):  # noqa: F821
        self.svc = schedule_service
        self.conn = schedule_service.conn
        self._ensure_schema()

    # ------------------------------------------------------------------ 结构
    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS transport_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_station (
                station_id TEXT PRIMARY KEY,
                ord        INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_team_base (
                team_id    TEXT PRIMARY KEY,
                station_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_team_venue (
                team_id    TEXT PRIMARY KEY,
                station_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_travel (
                from_station TEXT NOT NULL,
                to_station   TEXT NOT NULL,
                duration     INTEGER NOT NULL,
                PRIMARY KEY (from_station, to_station)
            );
            CREATE TABLE IF NOT EXISTS transport_timetable (
                round_no  INTEGER NOT NULL,
                slot      TEXT NOT NULL,
                kickoff   INTEGER NOT NULL,
                dispersal INTEGER NOT NULL,
                PRIMARY KEY (round_no, slot)
            );
            CREATE TABLE IF NOT EXISTS transport_param (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transport_plan (
                plan_id    INTEGER PRIMARY KEY,
                edition_id INTEGER NOT NULL,
                version    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (edition_id, version)
            );
            CREATE TABLE IF NOT EXISTS transport_run (
                run_id          TEXT NOT NULL,
                plan_id         INTEGER NOT NULL REFERENCES transport_plan(plan_id),
                edition_id      INTEGER NOT NULL,
                version         INTEGER NOT NULL,
                match_id        TEXT NOT NULL,
                round_no        INTEGER NOT NULL,
                slot            TEXT NOT NULL,
                team_id         TEXT NOT NULL,
                venue_station   TEXT NOT NULL,
                base_station    TEXT NOT NULL,
                status          TEXT NOT NULL CHECK(status IN ('active', 'cancelled')),
                pickup_platform TEXT NOT NULL,
                PRIMARY KEY (plan_id, run_id)
            );
            CREATE TABLE IF NOT EXISTS transport_leg (
                plan_id      INTEGER NOT NULL,
                run_id       TEXT NOT NULL,
                direction    TEXT NOT NULL CHECK(direction IN ('out', 'back')),
                from_station TEXT NOT NULL,
                to_station   TEXT NOT NULL,
                depart       INTEGER NOT NULL,
                arrive       INTEGER NOT NULL,
                capacity     INTEGER NOT NULL,
                PRIMARY KEY (plan_id, run_id, direction)
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------ 配置
    def is_configured(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM transport_meta WHERE key='configured'"
        ).fetchone() is not None

    def configure(
        self,
        team_bases: Mapping[str, str],
        team_venues: Mapping[str, str],
        travel: Mapping[tuple[str, str], int],
        timetable: Sequence[Mapping[str, Any]],
        lead_beats: int = 1,
        segment_capacity: int = 1,
    ) -> None:
        """固定基地/场站/有向时长/时刻表等运输事实，仅允许配置一次。

        参数
        ----
        team_bases:
            ``球队 -> 基地站``，每队一处。
        team_venues:
            ``主队 -> 比赛场站``，可与该队基地不同。
        travel:
            ``(起点, 终点) -> 行车时长``；全部用到的有向站对都必须给正整数时长。
        timetable:
            ``[{round, slot, kickoff, dispersal}]``，按轮次/slot 给开赛与散场时刻；
            后轮整体窗口必须晚于前轮（更晚且互不冲突的日期）。
        lead_beats:
            去程在开赛前多少拍抵达（正整数）。
        segment_capacity:
            每班车每段可载的实物箱数（正整数，版本修订不改写该容量）。
        """
        if self.is_configured():
            raise ValueError("运输配置已固定，不允许重新配置")

        cfg = self.svc.get_config()
        teams = set(cfg["teams"])
        round_slots = cfg["round_slots"]
        issues: list[Issue] = []

        if not isinstance(lead_beats, int) or isinstance(lead_beats, bool) or lead_beats <= 0:
            issues.append(Issue("bad_lead_beats", {"lead_beats": lead_beats},
                                "开赛前抵达提前量必须为正整数"))
        if (not isinstance(segment_capacity, int) or isinstance(segment_capacity, bool)
                or segment_capacity <= 0):
            issues.append(Issue("bad_segment_capacity", {"capacity": segment_capacity},
                                "每段可带箱数必须为正整数"))

        bases: dict[str, str] = {}
        venues: dict[str, str] = {}
        for team in teams:
            base = team_bases.get(team)
            venue = team_venues.get(team)
            if base is None:
                issues.append(Issue("team_base_missing", {"team": team}, "该队缺少基地声明"))
            else:
                bases[team] = base
            if venue is None:
                issues.append(Issue("team_venue_missing", {"team": team}, "该队缺少比赛场站声明"))
            else:
                venues[team] = venue
        for team in tuple(team_bases):
            if team not in teams:
                issues.append(Issue("unknown_team_base", {"team": team},
                                    "基地声明引用了未知球队"))
        for team in tuple(team_venues):
            if team not in teams:
                issues.append(Issue("unknown_team_venue", {"team": team},
                                    "场站声明引用了未知球队"))

        stations = sorted(set(bases.values()) | set(venues.values()))
        durations: dict[tuple[str, str], int] = {}
        for (a, b), dur in travel.items():
            if a not in stations or b not in stations:
                issues.append(Issue("travel_unknown_station",
                                    {"from": a, "to": b},
                                    "行车时长引用了未使用的车站"))
                continue
            if not isinstance(dur, int) or isinstance(dur, bool) or dur <= 0:
                issues.append(Issue("bad_travel_duration",
                                    {"from": a, "to": b, "duration": dur},
                                    "行车时长必须为严格正整数"))
                continue
            durations[(a, b)] = dur
        for a in stations:
            for b in stations:
                if a != b and (a, b) not in durations:
                    issues.append(Issue("travel_missing", {"from": a, "to": b},
                                        "缺少该有向站对的行车时长"))

        tt: dict[tuple[int, str], tuple[int, int]] = {}
        for i, row in enumerate(timetable):
            rnd, slot = row.get("round"), row.get("slot")
            kick, disp = row.get("kickoff"), row.get("dispersal")
            if not isinstance(rnd, int) or rnd not in round_slots:
                issues.append(Issue("timetable_bad_round", {"index": i, "round": rnd},
                                    "时刻表轮次不在赛程骨架内"))
                continue
            if slot not in round_slots[rnd]:
                issues.append(Issue("timetable_bad_slot",
                                    {"index": i, "round": rnd, "slot": slot},
                                    "时刻表 slot 未在该轮声明"))
                continue
            if not (isinstance(kick, int) and isinstance(disp, int)) or isinstance(kick, bool) \
                    or isinstance(disp, bool) or kick >= disp:
                issues.append(Issue("bad_kickoff_window",
                                    {"index": i, "round": rnd, "slot": slot,
                                     "kickoff": kick, "dispersal": disp},
                                    "开赛/散场必须为整数且开赛早于散场"))
                continue
            if (rnd, slot) in tt:
                issues.append(Issue("timetable_duplicate",
                                    {"index": i, "round": rnd, "slot": slot},
                                    "同一 (round, slot) 时刻表重复"))
                continue
            tt[(rnd, slot)] = (kick, disp)
        for rnd, slots in round_slots.items():
            for slot in slots:
                if (rnd, slot) not in tt:
                    issues.append(Issue("timetable_missing",
                                        {"round": rnd, "slot": slot},
                                        "该 (round, slot) 缺少开赛/散场时刻"))
        # 后轮安排在更晚互不冲突的日期：整轮窗口严格晚于上一轮
        windows: dict[int, tuple[int, int]] = {}
        for (rnd, _slot), (kick, disp) in tt.items():
            lo, hi = windows.get(rnd, (kick, disp))
            windows[rnd] = (min(lo, kick), max(hi, disp))
        for rnd in sorted(windows):
            if rnd - 1 in windows and windows[rnd][0] <= windows[rnd - 1][1]:
                issues.append(Issue("rounds_interleave",
                                    {"round": rnd,
                                     "window": windows[rnd],
                                     "previous_window": windows[rnd - 1]},
                                    "后轮窗口必须整体晚于前轮"))

        if issues:
            raise TransportRejected(issues)

        cur = self.conn.cursor()
        cur.executemany(
            "INSERT INTO transport_station(station_id, ord) VALUES(?, ?)",
            [(s, i) for i, s in enumerate(stations)],
        )
        cur.executemany(
            "INSERT INTO transport_team_base(team_id, station_id) VALUES(?, ?)",
            list(bases.items()),
        )
        cur.executemany(
            "INSERT INTO transport_team_venue(team_id, station_id) VALUES(?, ?)",
            list(venues.items()),
        )
        cur.executemany(
            "INSERT INTO transport_travel(from_station, to_station, duration) VALUES(?, ?, ?)",
            [(a, b, d) for (a, b), d in sorted(durations.items())],
        )
        cur.executemany(
            "INSERT INTO transport_timetable(round_no, slot, kickoff, dispersal)"
            " VALUES(?, ?, ?, ?)",
            [(rnd, slot, kick, disp)
             for (rnd, slot), (kick, disp) in sorted(tt.items(),
                                                      key=lambda x: (x[0][0], str(x[0][1])))],
        )
        cur.execute("INSERT INTO transport_param(key, value) VALUES('lead_beats', ?)",
                    (str(lead_beats),))
        cur.execute("INSERT INTO transport_param(key, value) VALUES('segment_capacity', ?)",
                    (str(segment_capacity),))
        cur.execute("INSERT INTO transport_meta(key, value) VALUES('configured', '1')")
        self.conn.commit()

    def get_transport_config(self) -> dict[str, Any]:
        if not self.is_configured():
            raise ValueError("运输办公室尚未配置")
        stations = [r["station_id"] for r in
                    self.conn.execute("SELECT station_id FROM transport_station ORDER BY ord")]
        bases = {r["team_id"]: r["station_id"] for r in
                 self.conn.execute("SELECT team_id, station_id FROM transport_team_base")}
        venues = {r["team_id"]: r["station_id"] for r in
                  self.conn.execute("SELECT team_id, station_id FROM transport_team_venue")}
        travel = {(r["from_station"], r["to_station"]): r["duration"] for r in
                  self.conn.execute(
                      "SELECT from_station, to_station, duration FROM transport_travel")}
        timetable = [{"round": r["round_no"], "slot": r["slot"],
                      "kickoff": r["kickoff"], "dispersal": r["dispersal"]}
                     for r in self.conn.execute(
                         "SELECT round_no, slot, kickoff, dispersal"
                         " FROM transport_timetable ORDER BY round_no, slot")]
        params = {r["key"]: int(r["value"]) for r in
                  self.conn.execute("SELECT key, value FROM transport_param")}
        return {"stations": stations, "team_bases": bases, "team_venues": venues,
                "travel": travel, "timetable": timetable,
                "lead_beats": params["lead_beats"],
                "segment_capacity": params["segment_capacity"]}

    # ------------------------------------------------------------------ 方案生成
    def _require_configured(self) -> None:
        if not self.is_configured():
            raise TransportRejected([Issue("transport_not_configured", {},
                                           "运输办公室尚未配置基地/场站/时刻表")])

    def _latest_plan_row(self, edition_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT plan_id, edition_id, version FROM transport_plan"
            " WHERE edition_id = ? ORDER BY version DESC LIMIT 1",
            (edition_id,),
        ).fetchone()

    def generate_plan(self, edition_id: int) -> dict[str, Any]:
        """按已发布 edition 的真实主客/slot 生成该 edition 的首版班车方案。"""
        self._require_configured()
        issues: list[Issue] = []
        if self.conn.execute("SELECT 1 FROM edition WHERE edition_id = ?",
                             (edition_id,)).fetchone() is None:
            raise TransportRejected([Issue("edition_unknown", {"edition_id": edition_id},
                                           "该赛程 edition 不存在，无法生成班车")])
        if self._latest_plan_row(edition_id) is not None:
            raise TransportRejected([Issue("plan_exists", {"edition_id": edition_id},
                                           "该 edition 已有班车方案，修订请走退运/改站台")])

        tcfg = self.get_transport_config()
        bases, venues = tcfg["team_bases"], tcfg["team_venues"]
        travel, timetable = tcfg["travel"], tcfg["timetable"]
        lead, capacity = tcfg["lead_beats"], tcfg["segment_capacity"]
        tt = {(row["round"], row["slot"]): (row["kickoff"], row["dispersal"])
              for row in timetable}

        matches = [dict(r) for r in self.conn.execute(
            "SELECT match_id, round_no AS round, home, away, slot"
            " FROM edition_match WHERE edition_id = ? ORDER BY round_no, match_id",
            (edition_id,))]

        # 先逐场派生两段车段，缺时长/缺时刻表独立报错，不级联噪音
        built: list[dict[str, Any]] = []
        for m in matches:
            mid, rnd, slot = m["match_id"], m["round"], m["slot"]
            home, away = m["home"], m["away"]  # 真实主客，绝不从 match_id 字母推断
            venue, base = venues.get(home), bases.get(away)
            if venue is None or base is None:
                issues.append(Issue("team_config_missing",
                                    {"match_id": mid, "home": home, "away": away},
                                    "主队场站或客队基地缺失"))
                continue
            win = tt.get((rnd, slot))
            if win is None:
                issues.append(Issue("timetable_missing",
                                    {"match_id": mid, "round": rnd, "slot": slot},
                                    "该比赛的 (round, slot) 没有时刻表"))
                continue
            kickoff, dispersal = win
            if (base, venue) not in travel or (venue, base) not in travel:
                issues.append(Issue("travel_missing",
                                    {"match_id": mid, "legs": [(base, venue), (venue, base)]},
                                    "去程或回程缺少有向行车时长"))
                continue
            run_id = f"R-{mid}-{away}"
            out_arrive = kickoff - lead
            out = _Leg(run_id, OUTBOUND, base, venue,
                       out_arrive - travel[(base, venue)], out_arrive, capacity)
            back = _Leg(run_id, RETURN, venue, base,
                        dispersal, dispersal + travel[(venue, base)], capacity)
            built.append({"run_id": run_id, "match_id": mid, "round": rnd, "slot": slot,
                          "team": away, "venue": venue, "base": base,
                          "legs": (out, back)})

        # 同队车段不得严格重叠（端点相接合法）
        by_team: dict[str, list[_Leg]] = {}
        for item in built:
            by_team.setdefault(item["team"], []).extend(item["legs"])
        for team, legs in by_team.items():
            legs = sorted(legs, key=lambda x: (x.depart, x.arrive, x.run_id, x.direction))
            for i in range(len(legs)):
                for j in range(i + 1, len(legs)):
                    a, b = legs[i], legs[j]
                    if b.depart >= a.arrive:
                        break  # 已按发车排序，后面只会更晚
                    if a.depart < b.arrive and b.depart < a.arrive:
                        issues.append(Issue("segment_overlap",
                                            {"team": team,
                                             "a": {"run": a.run_id, "direction": a.direction,
                                                   "interval": [a.depart, a.arrive]},
                                             "b": {"run": b.run_id, "direction": b.direction,
                                                   "interval": [b.depart, b.arrive]}},
                                            "同队车段严格重叠，无法同时乘用"))
        if issues:
            raise TransportRejected(issues)

        cur = self.conn.cursor()
        try:
            plan_id = (cur.execute("SELECT COALESCE(MAX(plan_id), 0) + 1 FROM transport_plan")
                       .fetchone()[0])
            cur.execute(
                "INSERT INTO transport_plan(plan_id, edition_id, version, created_at)"
                " VALUES(?, ?, 1, ?)",
                (plan_id, edition_id, datetime.now(timezone.utc).isoformat()),
            )
            for item in built:
                cur.execute(
                    "INSERT INTO transport_run(run_id, plan_id, edition_id, version, match_id,"
                    " round_no, slot, team_id, venue_station, base_station, status,"
                    " pickup_platform) VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item["run_id"], plan_id, edition_id, item["match_id"], item["round"],
                     item["slot"], item["team"], item["venue"], item["base"],
                     RUN_ACTIVE, DEFAULT_PLATFORM),
                )
                for leg in item["legs"]:
                    cur.execute(
                        "INSERT INTO transport_leg(plan_id, run_id, direction, from_station,"
                        " to_station, depart, arrive, capacity) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (plan_id, leg.run_id, leg.direction, leg.from_station,
                         leg.to_station, leg.depart, leg.arrive, leg.capacity),
                    )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return self.get_plan(edition_id)

    # ------------------------------------------------------------------ 版本修订
    def _revise(self, edition_id: int, run_id: str, mutate_run) -> dict[str, Any]:
        self._require_configured()
        head = self._latest_plan_row(edition_id)
        if head is None:
            raise TransportRejected([Issue("plan_missing", {"edition_id": edition_id},
                                           "该 edition 尚未生成班车方案")])
        old_runs = [dict(r) for r in self.conn.execute(
            "SELECT run_id, match_id, round_no, slot, team_id, venue_station, base_station,"
            " status, pickup_platform FROM transport_run WHERE plan_id = ? ORDER BY run_id",
            (head["plan_id"],))]
        target = next((r for r in old_runs if r["run_id"] == run_id), None)
        if target is None:
            raise TransportRejected([Issue("run_unknown",
                                           {"edition_id": edition_id, "run_id": run_id},
                                           "班车方案中没有该班次")])
        mutate_run(target)  # 抛 TransportRejected 即整版无副作用

        cur = self.conn.cursor()
        try:
            new_plan_id = (cur.execute(
                "SELECT COALESCE(MAX(plan_id), 0) + 1 FROM transport_plan").fetchone()[0])
            new_version = head["version"] + 1
            cur.execute(
                "INSERT INTO transport_plan(plan_id, edition_id, version, created_at)"
                " VALUES(?, ?, ?, ?)",
                (new_plan_id, edition_id, new_version,
                 datetime.now(timezone.utc).isoformat()),
            )
            for r in old_runs:
                cur.execute(
                    "INSERT INTO transport_run(run_id, plan_id, edition_id, version, match_id,"
                    " round_no, slot, team_id, venue_station, base_station, status,"
                    " pickup_platform) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["run_id"], new_plan_id, edition_id, new_version, r["match_id"],
                     r["round_no"], r["slot"], r["team_id"], r["venue_station"],
                     r["base_station"], r["status"], r["pickup_platform"]),
                )
                for leg in self.conn.execute(
                        "SELECT direction, from_station, to_station, depart, arrive, capacity"
                        " FROM transport_leg WHERE plan_id = ? AND run_id = ?",
                        (head["plan_id"], r["run_id"])):
                    cur.execute(
                        "INSERT INTO transport_leg(plan_id, run_id, direction, from_station,"
                        " to_station, depart, arrive, capacity) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (new_plan_id, r["run_id"], leg["direction"], leg["from_station"],
                         leg["to_station"], leg["depart"], leg["arrive"], leg["capacity"]),
                    )
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return self.get_plan(edition_id, new_version)

    def cancel_run(self, edition_id: int, run_id: str) -> dict[str, Any]:
        """退运指定班次：整版复制后把该班次标记为 cancelled，旧版凭单保留。"""

        def mutate(target: dict[str, Any]) -> None:
            if target["status"] == RUN_CANCELLED:
                raise TransportRejected([Issue("run_already_cancelled",
                                               {"edition_id": edition_id, "run_id": run_id},
                                               "该班次在当前版本已退运")])
            target["status"] = RUN_CANCELLED

        return self._revise(edition_id, run_id, mutate)

    def revise_platform(self, edition_id: int, run_id: str,
                        pickup_platform: str) -> dict[str, Any]:
        """修改指定班次的接货站台：发到站/时刻/容量全部冻结，仅站台变化。"""
        if not isinstance(pickup_platform, str) or not pickup_platform.strip():
            raise TransportRejected([Issue("bad_platform",
                                           {"run_id": run_id, "platform": pickup_platform},
                                           "接货站台必须是非空字符串")])

        def mutate(target: dict[str, Any]) -> None:
            if target["status"] == RUN_CANCELLED:
                raise TransportRejected([Issue("run_cancelled",
                                               {"edition_id": edition_id, "run_id": run_id},
                                               "已退运班次不能修改站台")])
            target["pickup_platform"] = pickup_platform

        return self._revise(edition_id, run_id, mutate)

    # ------------------------------------------------------------------ 查询
    def _resolve_plan(self, edition_id: int, version: int | None) -> sqlite3.Row:
        if version is None:
            row = self._latest_plan_row(edition_id)
        else:
            row = self.conn.execute(
                "SELECT plan_id, edition_id, version FROM transport_plan"
                " WHERE edition_id = ? AND version = ?",
                (edition_id, version),
            ).fetchone()
        if row is None:
            what = "班车方案" if version is None else f"班车方案版本 {version}"
            raise KeyError(f"edition {edition_id} 的{what}不存在")
        return row

    def _plan_payload(self, plan_row: sqlite3.Row) -> dict[str, Any]:
        plan_id = plan_row["plan_id"]
        runs = []
        for r in self.conn.execute(
                "SELECT run_id, match_id, round_no, slot, team_id, venue_station,"
                " base_station, status, pickup_platform FROM transport_run"
                " WHERE plan_id = ? ORDER BY round_no, match_id, run_id", (plan_id,)):
            legs = [{
                "direction": x["direction"], "from": x["from_station"],
                "to": x["to_station"], "depart": x["depart"],
                "arrive": x["arrive"], "capacity": x["capacity"],
            } for x in self.conn.execute(
                "SELECT direction, from_station, to_station, depart, arrive, capacity"
                " FROM transport_leg WHERE plan_id = ? AND run_id = ?"
                " ORDER BY direction", (plan_id, r["run_id"]))]
            runs.append({
                "run_id": r["run_id"], "match_id": r["match_id"], "round": r["round_no"],
                "slot": r["slot"], "team": r["team_id"], "venue": r["venue_station"],
                "base": r["base_station"], "status": r["status"],
                "pickup_platform": r["pickup_platform"], "legs": legs,
            })
        return {"plan_id": plan_id, "edition_id": plan_row["edition_id"],
                "version": plan_row["version"], "runs": runs}

    def get_plan(self, edition_id: int, version: int | None = None) -> dict[str, Any]:
        """取某 edition 的班车方案（默认最新版），含每段真实发到站、时刻、容量。"""
        return self._plan_payload(self._resolve_plan(edition_id, version))

    def list_plans(self) -> list[dict[str, Any]]:
        return [{"plan_id": r["plan_id"], "edition_id": r["edition_id"],
                 "version": r["version"], "created_at": r["created_at"]}
                for r in self.conn.execute(
                    "SELECT plan_id, edition_id, version, created_at"
                    " FROM transport_plan ORDER BY plan_id")]

    def vouchers_for_station(self, edition_id: int, station: str,
                             version: int | None = None) -> dict[str, Any]:
        """车站视角凭单查询：任何经过该站（基地或场站）的班次都可查，与托运单无关。"""
        plan = self.get_plan(edition_id, version)
        if station not in {r["station_id"] for r in
                           self.conn.execute("SELECT station_id FROM transport_station")}:
            raise KeyError(f"未知车站 {station}")
        runs = [r for r in plan["runs"]
                if station in (r["base"], r["venue"])]
        return {"edition_id": plan["edition_id"], "version": plan["version"],
                "plan_id": plan["plan_id"], "station": station, "runs": runs}

    def active_legs(self, edition_id: int, version: int | None = None) -> list[dict[str, Any]]:
        """供器材库只读使用：当前版本未退运班次的全部可乘车段。"""
        plan = self.get_plan(edition_id, version)
        out: list[dict[str, Any]] = []
        for run in plan["runs"]:
            if run["status"] != RUN_ACTIVE:
                continue
            for leg in run["legs"]:
                out.append({"kind": "shuttle", "lid": f"{plan['plan_id']}:{run['run_id']}:"
                                                       f"{leg['direction']}",
                            "run_id": run["run_id"], "direction": leg["direction"],
                            "match_id": run["match_id"], "team": run["team"],
                            "from": leg["from_station"], "to": leg["to_station"],
                            "depart": leg["depart"], "arrive": leg["arrive"],
                            "capacity": leg["capacity"],
                            "pickup_platform": run["pickup_platform"]})
        return out
