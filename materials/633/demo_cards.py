"""端到端演示：旧场记簿逐球卡接入、冻结出证、检查点反例与听审排庭。

覆盖题面全部关键情节
====================
1. FIXTURE4 的 ab 经原库 played 2:1，从冻结事实取终场主客得分；
2. 三球小例两种合法解释（主主客 / 主客主），制胜球可能是 P 或 Q，球员奖待核；
3. 上游把第 2 球订为主队 => 新版卡卷，制胜球唯一为 P，立即发证（老版保留）；
4. 恢复原卡、奖方改颁培训站且 P/Q 同属 K => 球未查清也能发 K；
5. 关闭成绩库后凭冻结产品、按不同奖务登记重新判定/排庭；
6. 一个累计检查点令看似可能的球序其实不存在；
7. 同场球员奖+培训站奖两案：截止第二天分庭传两次、第三天丙合庭传一次、
   “一传且第二天收完”无共同主席；
8. 只改登记（Q 注册 S）站奖即发；取消场不出奖凭单。

运行：python3 demo_cards.py
产物：./run_data_cards/ 下三个真实 SQLite，可删除重建、可关闭重开复算。
"""

from __future__ import annotations

import os
import shutil

from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository
from league.card_repo import PlayByPlayRepository
from league.playbyplay import (
    build_product, explain, can_cooccur, possible_recipients, HOME, AWAY,
    PlayByPlayProduct,
)
from league.awards import decide_award, build_hearing_case
from league.tribunal import Chair, schedule_hearings, earliest_finish_with_cap
from tests.fixtures import (
    TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY, FIXTURE4,
)

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, "run_data_cards")

# 四人按实际球队登记：P/U/Q 属主队 A，V/W 属客队 B
ROSTER = {"P": HOME, "U": HOME, "Q": HOME, "V": AWAY, "W": AWAY}
THREE_CARDS = [
    {"card_no": 1, "options": {HOME: "P"}},
    {"card_no": 2, "options": {HOME: "U", AWAY: "V"}},
    {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
]
FIXED_CARDS = [
    {"card_no": 1, "options": {HOME: "P"}},
    {"card_no": 2, "options": {HOME: "U"}},            # 上游订正：第2球确定主队
    {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
]


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_witnesses(report) -> None:
    for i, w in enumerate(report.witnesses, 1):
        seq = " ".join(f"{x['card_no']}{'主' if x['side'] == HOME else '客'}"
                       f"({x['player']})={x['score_after'][0]}:{x['score_after'][1]}"
                       for x in w)
        print(f"  见证{i}: {seq}")


def main() -> None:
    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR)
    os.makedirs(RUN_DIR)

    # ------------------------------------------------ 赛程 + 冻结事实
    banner("1. 赛程库发布 edition，排名仓冻结 ab played 2:1（终分来自事实，不靠猜）")
    sched_db = os.path.join(RUN_DIR, "schedule.db")
    stand_db = os.path.join(RUN_DIR, "standings.db")
    card_db = os.path.join(RUN_DIR, "cards.db")
    svc = ScheduleService(sched_db)
    svc.initialize(TEAMS4, REGISTRY4, ROUND_SLOTS4, BLACKOUTS_EMPTY)
    edition = svc.publish(FIXTURE4)["edition_id"]
    fx = svc.locate_match(edition, "ab")
    print(f"  match ab 当版主客：{fx['home']}(主) vs {fx['away']}(客)")

    standings = StandingsRepository(stand_db, sched_db)
    standings.apply_result_batch("initial-results", [{
        "result_id": "r-ab", "match_id": "ab", "state": "played",
        "score_home": 2, "score_away": 1}])
    frozen = standings.active_results()
    print("  冻结事实 ab：", {k: frozen["ab"][k] for k in
          ("state", "score_home", "score_away")})

    pb = PlayByPlayRepository(card_db, sched_db)

    # ------------------------------------------------ 原三球卷
    banner("2. 原三球逐球卡卷冻结为 v1：两种合法解释，球员奖待核 P/Q")
    r1 = pb.submit_card_volume(
        "cards-v1", "ab", edition, ROSTER, THREE_CARDS,
        frozen_results=frozen, volume_id="VOL-ab-原", custodian="管卷人")
    v1 = r1["volume_version"]
    snap1 = pb.freeze_interpretation("snap-v1", v1)
    prod_v1 = pb.load_product(v1)
    rep_v1 = explain(prod_v1)
    print(f"  卡卷版本 v{v1}，解释 feasible={rep_v1.feasible}，"
          f"制胜卡={rep_v1.winning_cards}（最后平局之后第一球）")
    show_witnesses(rep_v1)
    d_player = pb.register_award("球员奖-ab", snap1["snapshot_id"], "player")
    print(f"  球员奖决定：{d_player.decision}，可能收件者={d_player.recipients}")
    hc_player = build_hearing_case(d_player, prod_v1)

    # 独立核对产品：某卡可能归谁、某前后段能否同场
    print("  独立核对 卡2 可能记入方：",
          {("主" if s == HOME else "客"): p for s, p in
           rep_v1.attributions[2].items() if p})
    print("  钉选(卡2客,卡3主)=主客主 可同场：",
          can_cooccur(prod_v1, rep_v1, [(2, AWAY), (3, HOME)]))
    print("  钉选(卡2主,卡3主) 跨解释拼接，禁止：",
          can_cooccur(prod_v1, rep_v1, [(2, HOME), (3, HOME)]))

    # ------------------------------------------------ 上游订正
    banner("3. 上游把第 2 球订为主队：另发新版 v2，老版 v1 保留；制胜球唯一为 P")
    r2 = pb.submit_card_volume(
        "cards-v2", "ab", edition, ROSTER, FIXED_CARDS,
        frozen_results=frozen, volume_id="VOL-ab-订", custodian="管卷人")
    v2 = r2["volume_version"]
    snap2 = pb.freeze_interpretation("snap-v2", v2)
    rep_v2 = explain(pb.load_product(v2))
    print(f"  版本列表：{[v['volume_version'] for v in pb.list_versions('ab', edition)]}")
    print(f"  v2 制胜卡={rep_v2.winning_cards}，球员收件者="
          f"{possible_recipients(pb.load_product(v2), rep_v2, None, 'player')}")
    d_fixed = pb.register_award("球员奖-ab-订正版", snap2["snapshot_id"], "player")
    print(f"  订正后球员奖：{d_fixed.decision}，收件者={d_fixed.recipient}（立即发证）")
    old = pb.get_product_snapshot(snap1["snapshot_id"])
    print(f"  老快照 v1 不被改写，制胜卡仍为={old['report']['winning_cards']}")

    # ------------------------------------------------ 培训站 K
    banner("4. 恢复原卡 v1，奖方改颁培训站：P、Q 同属 K，球未查清也发 K")
    d_station_k = decide_award("站奖-K", prod_v1, "station",
                               {"P": "K", "Q": "K", "U": "K"})
    print(f"  培训站奖（P/Q->K）：{d_station_k.decision}，收件者={d_station_k.recipient}")
    d_no_reg = decide_award("站奖-无登记", prod_v1, "station", {})
    print(f"  无培训站登记：{d_no_reg.decision}（{d_no_reg.note}）")
    d_mix = decide_award("站奖-并存", prod_v1, "station", {"P": "K", "U": "K"})
    print(f"  仅 P 登记 K、Q 未登记：{d_mix.decision}，收件者={d_mix.recipients}（待核）")

    # ------------------------------------------------ 关账后重判
    banner("5. 关闭成绩库后，仅凭冻结产品按不同奖务登记重算（不回看活账）")
    standings.close()
    pb.close()
    svc.close()
    svc2 = ScheduleService(sched_db)
    pb2 = PlayByPlayRepository(card_db, sched_db)
    frozen_snap = pb2.get_product_snapshot(snap1["snapshot_id"])
    prod_offline = PlayByPlayProduct.from_dict(frozen_snap["product"])
    d_s = decide_award("离线站奖-S/T", prod_offline, "station",
                       {"P": "S", "Q": "T", "U": "S"})
    d_k = decide_award("离线站奖-K", prod_offline, "station",
                       {"P": "K", "Q": "K", "U": "K"})
    print(f"  同一冻结产品，登记 S/T：{d_s.decision} {d_s.recipients}")
    print(f"  同一冻结产品，登记 K  ：{d_k.decision} 收件者={d_k.recipient}")

    # ------------------------------------------------ 检查点反例
    banner("6. 累计检查点令看似可能的球序其实不存在")
    # 卡2后检查点 2:0 => 卡2 只能主队，主客主（HAH）解释被排除
    cp_prod = build_product("ab", edition, "A", "B", ROSTER, 2, 1, THREE_CARDS,
                            [{"after_card": 2, "home": 2, "away": 0}],
                            volume_id="VOL-ab-cp", custodian="管卷人")
    cp_rep = explain(cp_prod)
    print("  检查点：第 2 球后累计必须 2:0")
    print(f"  合法解释数={len(cp_rep.witnesses)}，制胜卡={cp_rep.winning_cards}")
    print("  原卡中看似可能的 卡2客(V) 球序，现在能否共现：",
          can_cooccur(cp_prod, cp_rep, [(2, AWAY)]), "（False=不存在）")
    # 不可达检查点 => 首个无合法前缀卡
    dead_prod = build_product("ab", edition, "A", "B", ROSTER, 2, 1, THREE_CARDS,
                              [{"after_card": 1, "home": 0, "away": 1}],
                              volume_id="VOL-ab-dead", custodian="管卷人")
    print("  卡1后强标 0:1（卡1只有主队）首个无合法前缀卡：",
          explain(dead_prod).first_dead_card)
    # 终场才矛盾：三卡全主队（无任何客队选择），终分却要客队进一球
    all_home = [
        {"card_no": 1, "options": {HOME: "P"}},
        {"card_no": 2, "options": {HOME: "U"}},
        {"card_no": 3, "options": {HOME: "Q"}},
    ]
    final_prod = build_product("ab", edition, "A", "B", ROSTER, 2, 1, all_home,
                               volume_id="VOL-ab-final", custodian="管卷人")
    print("  三卡全主队却要终场 2:1，矛盾到终场才暴露，标记：",
          explain(final_prod).first_dead_card)

    # ------------------------------------------------ 两案排庭
    banner("7. 同场球员奖(P/Q) + 培训站奖(S/T) 两案排庭")
    d_st = decide_award("站奖-S/T", prod_offline, "station",
                        {"P": "S", "Q": "T", "U": "S"})
    hc_station = build_hearing_case(d_st, prod_offline)
    # 离线产品的卷号/保管人沿用 v1
    hc_player = build_hearing_case(d_player, prod_v1)
    chairs = [
        Chair("甲", (1,), frozenset({"S"})),      # 第一天，回避 S
        Chair("乙", (2,), frozenset({"P"})),      # 第二天，回避 P
        Chair("丙", (3,), frozenset()),           # 第三天，无回避
    ]
    p2 = schedule_hearings([hc_player, hc_station], chairs, deadline=2)
    print(f"  截止第2天：传卷 {p2.summons} 次，第 {p2.finished_day} 天收完（分庭）")
    for t in p2.tribunals:
        print(f"    第{t.day}天 主席={t.chair_id} 保管人={t.custodian}"
              f" 案件={t.case_ids} 合庭={t.joint}")
    p3 = schedule_hearings([hc_player, hc_station], chairs, deadline=3)
    print(f"  等到第3天：传卷 {p3.summons} 次，第 {p3.finished_day} 天收完"
          f"（丙一庭合审，共传保管人一次）")
    for t in p3.tribunals:
        print(f"    第{t.day}天 主席={t.chair_id} 保管人={t.custodian}"
              f" 案件={t.case_ids} 合庭={t.joint}")
    cap1 = schedule_hearings([hc_player, hc_station], chairs, deadline=2,
                             summon_cap=1)
    print(f"  原工单“只传一次且第2天收完”：feasible={cap1.feasible}"
          f"（{cap1.reason}）——没有共同主席可排")
    fast = earliest_finish_with_cap([hc_player, hc_station], chairs, summon_cap=1)
    print(f"  传卷上限 1 次时最快收完日：第 {fast.finished_day} 天")

    # ------------------------------------------------ 只改登记即发
    banner("8. 保持原逐球产品，只把 Q 注册到 S：站奖直接发 S，人奖仍只传一次")
    d_q_s = decide_award("站奖-Q入S", prod_v1, "station",
                         {"P": "S", "Q": "S", "U": "S"})
    print(f"  站奖：{d_q_s.decision}，收件者={d_q_s.recipient}（立即发证，不必传卷）")
    only_player = schedule_hearings([hc_player], chairs, deadline=2)
    t = only_player.tribunals[0]
    print(f"  仅剩人奖待核：传卷 {only_player.summons} 次，"
          f"第{t.day}天 主席={t.chair_id}（甲不回避 P/Q）即可审")

    # ------------------------------------------------ 取消场
    banner("9. 取消场不出奖凭单")
    standings2 = StandingsRepository(stand_db, sched_db)
    standings2.apply_result_batch("cancel-cd", [{
        "result_id": "r-cd", "match_id": "cd", "state": "cancelled"}])
    rc = pb2.submit_card_volume(
        "cards-cancel", "cd", edition, ROSTER, THREE_CARDS,
        frozen_results=standings2.active_results(), status="cancelled",
        volume_id="VOL-cd", custodian="管卷人2")
    prod_c = pb2.load_product(rc["volume_version"])
    d_cancel = decide_award("取消场奖", prod_c, "player")
    print(f"  取消场奖项决定：{d_cancel.decision}（{d_cancel.note}）")

    # ------------------------------------------------ 重开复算
    banner("10. 关闭重开：版本、流水、快照、奖证全部可复算")
    pb2.close()
    standings2.close()
    svc2.close()
    svc3 = ScheduleService(sched_db)
    pb3 = PlayByPlayRepository(card_db, sched_db)
    insp = pb3.inspect()
    print("  卡卷版本数：", len(insp["volumes"]))
    print("  请求流水：", [r["request_id"] for r in insp["requests"]])
    print("  已登记奖项：", [(a["award_id"], a["decision"]) for a in insp["awards"]])
    print("  重开取 v1 制胜卡：",
          pb3.get_product_snapshot(snap1["snapshot_id"])["report"]["winning_cards"])
    pb3.close()
    svc3.close()
    print("\n演示完成，SQLite 文件位于", RUN_DIR)


if __name__ == "__main__":
    main()
