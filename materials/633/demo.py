"""端到端演示：用 materials/633/case.json 生产两个真实赛程 edition，
固定同一批比分事实，分别按两个 edition 计算排名，并显式制造“断链消费者”
对照：after 请求已指定新 edition，消费者内部却冻结旧双方投影。

运行：python3 demo.py
产物：./run_data/ 下两个真实 SQLite（可关闭重开复算）与控制台证据。
"""

from __future__ import annotations

import copy
import json
import os
import shutil

from league.ranking import compute_standings
from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository
from league.transport import TransportOffice
from league.shipping import EquipmentDepot

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, "run_data")


def load_case() -> dict:
    with open(os.path.join(HERE, "case.json"), encoding="utf-8") as f:
        return json.load(f)


def build_editions(svc: ScheduleService, case: dict) -> tuple[list[dict], list[dict]]:
    before = case["before_fixture"]
    flipped_ids = set(case["flipped_match_ids"])
    after = copy.deepcopy(before)
    for row in after:
        if row["match_id"] in flipped_ids:
            row["home"], row["away"] = row["away"], row["home"]

    assert svc.check_candidate(before) == []
    assert svc.check_candidate(after) == []
    ed_before = svc.publish(before)
    ed_after = svc.publish(after)
    return before, after, ed_before["edition_id"], ed_after["edition_id"]


def initial_result_batch(case: dict) -> list[dict]:
    return [
        {
            "result_id": f"r-{row['match_id']}",
            "match_id": row["match_id"],
            "state": "played",
            "score_home": row["home_goals"],
            "score_away": row["away_goals"],
        }
        for row in case["scores"]
    ]


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR)
    os.makedirs(RUN_DIR)
    case = load_case()

    # ------------------------------------------------------------ 赛程生产
    banner("1. 赛程服务：初始化固定骨架并独立预检/发布两个 edition")
    sched_db = os.path.join(RUN_DIR, "schedule.db")
    svc = ScheduleService(sched_db)
    registry = {m["match_id"]: m["round"] for m in case["before_fixture"]}
    round_slots = {
        r: {"s": case["slot_capacity"]}
        for r in sorted(set(registry.values()))
    }
    svc.initialize(case["clubs"], registry, round_slots,
                   {t: list(case["blackouts"]) for t in case["clubs"]})
    before, after, ed_before, ed_after = build_editions(svc, case)
    print(f"已发布 edition：before={ed_before}，after={ed_after}")
    for mid in case["flipped_match_ids"]:
        b = svc.locate_match(ed_before, mid)
        a = svc.locate_match(ed_after, mid)
        print(f"  match {mid:>2}: before {b['home']}(主)-{b['away']}(客)"
              f"  ->  after {a['home']}(主)-{a['away']}(客)")

    # ------------------------------------------------------------ 事实接收
    banner("2. 排名仓：原子接收固定比分批（request_id=initial-results）")
    repo_db = os.path.join(RUN_DIR, "standings.db")
    repo = StandingsRepository(repo_db, sched_db)
    entries = initial_result_batch(case)
    receipt = repo.apply_result_batch("initial-results", entries)
    print("接收回执：", receipt)
    print("同号同文重试复读：",
          repo.apply_result_batch("initial-results", entries)["replayed"])
    rounds = (1, case["round_through"])

    # ------------------------------------------------------------ 两个真实 edition
    banner("3. 固定事实切面，分别明确请求两个真实 edition")
    snap_before = repo.standings("rank-before", ed_before, rounds,
                                 promotion_places=1, relegation_places=1)
    snap_after = repo.standings("rank-after", ed_after, rounds,
                                promotion_places=1, relegation_places=1)

    def show(tag: str, snap: dict) -> None:
        print(f"\n[{tag}] snapshot_id={snap['snapshot_id']}"
              f" edition={snap['edition_id']} seq={snap['seq_floor']}"
              f" 轮次={snap['rounds']}")
        for r in snap["ranked"]:
            print(f"  #{r['position']} {r['team']}  总积分={r['total_points']:>2}"
                  f" (比赛分 {r['match_points']} - 判罚 {r['penalty_points']})"
                  f"  赛 {r['played']}  净胜 {r['goal_diff']:>2}  进球 {r['goals_for']}")
        print("  升级：", snap["promotion"])
        print("  降级：", snap["relegation"])

    show("BEFORE 真实投影", snap_before)
    show("AFTER 真实投影", snap_after)

    # 固定 match_id 的主客归属真实改变：以 ab/ac/bc 三场为例核对积分落点
    banner("4. 固定 match_id 的比分归属随 edition 真实改变（逐场证据）")
    results = repo.active_results()
    for mid in case["flipped_match_ids"]:
        fx_b = svc.locate_match(ed_before, mid)
        fx_a = svc.locate_match(ed_after, mid)
        r = results[mid]
        sh, sa = r["score_home"], r["score_away"]
        print(f"  {mid}: 事实槽位比分 主{sh}:客{sa}")
        print(f"    before: {fx_b['home']} 取 {sh}，{fx_b['away']} 取 {sa}")
        print(f"    after : {fx_a['home']} 取 {sh}，{fx_a['away']} 取 {sa}")

    # ------------------------------------------------------------ 断链消费者
    banner("5. 断链对照：after 请求指定新 edition，消费者却冻结旧双方投影")
    # 正确做法：直接用 repo.standings(..., ed_after, ...)，即 snap_after。
    # 断链做法：消费者内部缓存了 before 的 fixture，却用 after 的请求身份计算。
    stale = compute_standings(
        teams=case["clubs"],
        fixtures=before,                       # 冻结的旧双方投影
        results=results,
        penalties=repo.active_penalties(),
        rounds=rounds,
        promotion_places=1,
        relegation_places=1,
    )
    correct_order = snap_after["order"]
    stale_order = stale["order"]
    print("请求身份：request_id=rank-after，显式 edition_id =", ed_after)
    print("  正确 after 名次：", correct_order)
    print("  断链投影名次  ：", stale_order)
    diffs = []
    for t in case["clubs"]:
        good = next(x for x in snap_after["ranked"] if x["team"] == t)
        bad = next(x for x in stale["ranked"] if x["team"] == t)
        if good["total_points"] != bad["total_points"] or \
                good["goals_for"] != bad["goals_for"]:
            diffs.append((t, good["total_points"], bad["total_points"],
                          good["goals_for"], bad["goals_for"]))
    print("  具体积分/进球错误（球队, 正确总积分, 断链总积分, 正确进球, 断链进球）：")
    for d in diffs:
        print("   ", d)
    if snap_after["promotion"]["teams"] != stale["promotion"]["teams"]:
        print("  资格错误：正确升级", snap_after["promotion"]["teams"],
              "，断链升级", stale["promotion"]["teams"])
    if snap_after["relegation"]["teams"] != stale["relegation"]["teams"]:
        print("  资格错误：正确降级", snap_after["relegation"]["teams"],
              "，断链降级", stale["relegation"]["teams"])

    # 对照：明确请求旧 edition 仍然合法，且与断链投影数值恰好一致
    banner("6. 控制对照：显式请求旧 edition 合法；after 与断链请求身份相同")
    print("  rank-before 显式 edition_id =", snap_before["edition_id"],
          "名次", snap_before["order"])
    print("  断链投影名次                 ", stale_order,
          "（数值等于 before，但请求身份是 after -> 语义非法）")
    assert stale_order == snap_before["order"]
    assert snap_before["edition_id"] == ed_before
    assert snap_after["edition_id"] == ed_after
    print("  结论：旧 edition 请求本身合法；错误只发生在 after 身份 + 旧投影的错配。")
    print("  未改动任何最终名次：after 正确名次保持", correct_order)

    # ------------------------------------------------------------ 客队班车凭单
    banner("7. 运输办公室：按两个真实 edition 生成版本化客队班车凭单")
    office = TransportOffice(svc)
    timetable = [
        {"round": 1, "slot": "s", "kickoff": 2, "dispersal": 3},
        {"round": 2, "slot": "s", "kickoff": 10, "dispersal": 11},
        {"round": 3, "slot": "s", "kickoff": 20, "dispersal": 21},
    ]
    travel = {(a, b): 1 for a in case["clubs"] for b in case["clubs"] if a != b}
    office.configure(
        team_bases={t: t for t in case["clubs"]},       # 每队一处基地
        team_venues={t: t for t in case["clubs"]},      # 主场场站（可与基地不同）
        travel=travel, timetable=timetable,
        lead_beats=1, segment_capacity=1,               # 开赛前 1 拍到、每段一箱
    )
    plan_b = office.generate_plan(ed_before)
    plan_a = office.generate_plan(ed_after)
    for tag, plan in (("before", plan_b), ("after", plan_a)):
        ab = next(r for r in plan["runs"] if r["match_id"] == "ab")
        out = next(l for l in ab["legs"] if l["direction"] == "out")
        print(f"  [{tag}] ab 客队={ab['team']}：去程 {out['from']} {out['depart']}"
              f" 拍发 -> {out['to']} {out['arrive']} 拍到，每段可带 "
              f"{out['capacity']} 箱")
    print("  车站 A 在无任何托运单时可查凭单：",
          [r["run_id"] for r in office.vouchers_for_station(ed_before, "A")["runs"]])

    # ------------------------------------------------------------ 器材托运求解
    banner("8. 器材库：封箱托运，最小化付费直送采购费并交完整交接单")
    depot_db = os.path.join(RUN_DIR, "depot.db")
    depot = EquipmentDepot(depot_db, sched_db)
    box = [{"box_id": "bx1", "origin": "B", "destination": "A",
            "available_at": 0, "deadline": 2}]
    offer = [{"offer_id": "o1", "origin": "B", "destination": "A",
              "depart": 0, "arrive": 1, "capacity": 1, "fee": 5}]
    print(depot.register_boxes("boxes-1", box))
    print(depot.register_offers("offers-1", offer))

    def show_solution(tag: str, sol: dict) -> None:
        print(f"\n[{tag}] 直送采购总费用={sol['total_direct_fee']}，"
              f"采购单={[o['offer_id'] for o in sol['purchased_offers']]}")
        for bid, chain in sol["manifests"].items():
            for c in chain:
                if c["kind"] == "wait":
                    print(f"  {bid}: 留站 {c['station']} "
                          f"{c['from_time']}->{c['to_time']} 拍")
                else:
                    print(f"  {bid}: {c['kind']} {c['from']} {c['depart']} -> "
                          f"{c['to']} {c['arrive']}（{c['lid']}）")

    show_solution("before：比赛车 B→A 免费捎带",
                  depot.solve("solve-before", ed_before))
    show_solution("after：免费车改从 A 发，最省 5",
                  depot.solve("solve-after", ed_after))

    # 保持原班车，再交一只同起讫同时限的箱：容量 1 迫使另买一单
    depot2 = EquipmentDepot(os.path.join(RUN_DIR, "depot2.db"), sched_db)
    depot2.register_boxes("boxes-2", box + [
        {"box_id": "bx2", "origin": "B", "destination": "A",
         "available_at": 0, "deadline": 2}])
    depot2.register_offers("offers-2", offer)
    show_solution("两箱同限：一只免费、一只付费 5",
                  depot2.solve("solve-two", ed_before))

    # 中站换车：B->A 免费车 + A 留站 + A->C 客队车，零直送费
    depot3 = EquipmentDepot(os.path.join(RUN_DIR, "depot3.db"), sched_db)
    depot3.register_boxes("boxes-3", [
        {"box_id": "tr", "origin": "B", "destination": "C",
         "available_at": 0, "deadline": 14}])
    show_solution("中站换车（含留站等待，全程真实运力）",
                  depot3.solve("solve-transfer", ed_before))
    depot.close()
    depot2.close()
    depot3.close()

    # ------------------------------------------------------------ 重开复算
    banner("9. 关闭重开：edition、班车版本、原始事实与旧排名解释均可复算")
    repo.close()
    svc.close()
    svc2 = ScheduleService(sched_db)
    repo2 = StandingsRepository(repo_db, sched_db)
    reloaded = repo2.get_snapshot(snap_after["snapshot_id"])
    print("  重开后 edition 数：", len(svc2.list_editions()))
    print("  重开后请求流水  ：", [r["request_id"] for r in repo2.request_log()])
    print("  重开后 after 快照名次：", reloaded["order"],
          "==", snap_after["order"], "->", reloaded["order"] == snap_after["order"])
    repo2.close()
    svc2.close()
    print("\n演示完成，SQLite 文件位于", RUN_DIR)


if __name__ == "__main__":
    main()
