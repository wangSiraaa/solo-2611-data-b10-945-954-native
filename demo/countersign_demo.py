"""会签询证与印务室备印端到端演示。

运行：

    python -m demo.countersign_demo

复现题面给定的五个费用结论，并演示：

* 叶→根最近一条授权解析、下级禁令覆盖上级授予；
* 行政改树但预算换版未获准时不提前换用新印；
* 询证名单冻结预算方案 / 组织 / 授权本 / 承诺切面，旧名单可复读；
* 印务室最低备印费、逐预案见证、两单抢同一人、正式登记与回执幂等。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from orgbudget import (
    AuthService,
    BudgetService,
    HANDLER,
    OrgService,
    REVIEWER,
)
from orgbudget.countersign_service import CountersignService, PrintRoomService
from orgbudget.print_room import AbsencePlan, PrintRound, Witness, plan_prep

ALL = AbsencePlan("ALL")


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[通过]' if ok else '[失败]'} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def _fees(b_review: int = 1, c_review: int = 5) -> dict:
    return {
        ("A", HANDLER): 1,
        ("B", HANDLER): 1,
        ("B", REVIEWER): b_review,
        ("C", REVIEWER): c_review,
    }


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="countersign_demo_"))
    try:
        org = OrgService(tmp / "org.db")
        budget = BudgetService(tmp / "budget.db", org)
        auth = AuthService(tmp / "auth.db")
        cs = CountersignService(tmp / "sign.db", budget, auth)
        room = PrintRoomService(tmp / "sign.db")

        # 根 ROOT 下 X、Y；叶 D 在 X；两个分部各留固定叶 XE、YE。
        for dept, parent in [
            ("ROOT", None), ("X", "ROOT"), ("Y", "ROOT"),
            ("XE", "X"), ("YE", "Y"), ("D", "X"),
        ]:
            org.create_department(dept, parent)
        v1 = org.publish("初始树：D 在 X 下").version
        p1 = org.get_product(v1)
        w1 = {(d, k): 1 for d in p1.department_ids for k in p1.children(d)}
        budget.register_root_budget(v1, 1_000, w1)
        budget.create_commitment("REQ-1", "D", 100)  # 足额承诺

        # X 给 A 经办、Y 给 B 经办；根给 B、C 复核。
        auth.grant("X", HANDLER, "A")
        auth.grant("Y", HANDLER, "B")
        auth.grant("ROOT", REVIEWER, "B")
        auth.grant("ROOT", REVIEWER, "C")
        auth.publish_edition("初始授权本")
        duty = ("A", "B", "C")

        _line("场景 1：基线一单，全员到岗，最省 A/B")
        lst = cs.inquiry("L1", "REQ-1", 1)
        print(f"  经办候选 {lst.handlers}；复核候选 {lst.reviewers}")
        pr = PrintRound("R1", (lst.candidate("S1"),), _fees(), frozenset(), duty)
        res = plan_prep(pr, (ALL,))
        print(f"  最低备印费 {res.total_fee}；备印 {res.prepared}")
        _check("基线费用为 2", res.total_fee == 2)
        _check("见证为 A 经办 + B 复核",
               res.per_plan["ALL"] == (Witness("S1", "A", "B"),))

        _line("场景 2：迁 D 到 Y，足额新预算换版成功，B 不能自核")
        org.move_subtree("D", "Y")
        v2 = org.publish("D 迁到 Y 下").version
        p2 = org.get_product(v2)
        w2 = {(d, k): 1 for d in p2.department_ids for k in p2.children(d)}
        budget.revise_budget(v2, 1_000, w2)
        moved = cs.inquiry("L2", "REQ-1", 1)
        print(f"  换版后经办候选 {moved.handlers}；复核候选 {moved.reviewers}；承诺仍绑 {moved.commitment_dept}")
        pr2 = PrintRound("R2", (moved.candidate("S1"),), _fees(), frozenset(), duty)
        res2 = plan_prep(pr2, (ALL,))
        print(f"  最低备印费 {res2.total_fee}；见证 {res2.per_plan['ALL']}")
        _check("经办只剩 B，自核被排除，必须 B/C", res2.total_fee == 6)

        _line("场景 3：用原组织（旧名单 L1），印务室 B 复核费提到 3")
        old = cs.get_list("L1")  # 旧名单冻结原组织、原授权，可直接复读
        pr3 = PrintRound("R3", (old.candidate("S1"),), _fees(b_review=3), frozenset(), duty)
        res3 = plan_prep(pr3, (ALL,))
        print(f"  最低备印费 {res3.total_fee}（A 经办 1 + B 复核 3 + 仍优于 C 的 5）")
        _check("提价后仍选 A/B，费用 4", res3.total_fee == 4)

        _line("场景 4：固定原组织原费，全员预案之外加“B 缺席”")
        pr4 = PrintRound("R4", (old.candidate("S1"),), _fees(), frozenset(), duty)
        plans4 = (ALL, AbsencePlan("NO-B", frozenset({"B"})))
        res4 = plan_prep(pr4, plans4)
        print(f"  最低备印费 {res4.total_fee}；备印 {res4.prepared}")
        for pid, wits in res4.per_plan.items():
            print(f"    预案 {pid} 见证 {wits}")
        _check("只能备 A 经办 + C 复核，费用 6", res4.total_fee == 6)

        _line("场景 5：再加“C 缺席”，须备 B、C 两枚复核印")
        plans5 = plans4 + (AbsencePlan("NO-C", frozenset({"C"})),)
        res5 = plan_prep(pr4, plans5)
        print(f"  最低备印费 {res5.total_fee}；备印 {res5.prepared}")
        _check("A 经办 + B/C 两枚复核，费用 7", res5.total_fee == 7)
        room.open_round("R5", pr4, plans5, res5)
        # 实际全员到岗，按 NO-C 预案的见证正式登记一套签单。
        witnesses = res5.per_plan["ALL"]
        receipts = room.register_receipts(
            "R5", frozenset(), witnesses, {"S1": "RC-1"}
        )
        print(f"  正式签单 {receipts}")
        replay = room.register_receipts(
            "R5", frozenset(), witnesses, {"S1": "RC-1"}
        )
        _check("旧回执重复打开不二次占印（幂等复读）", replay == receipts)

        _line("附：下级禁令覆盖上级授予")
        auth.deny("X", REVIEWER, "C")  # D 原在 X 下，X 的禁令近于根授予
        auth.publish_edition("X 禁 C 复核")
        # 用换版前冻结的 L1 复读，内容不受新禁令影响。
        _check("已签出旧名单复读不变", "C" in cs.get_list("L1").reviewers)

        _line("附：两单抢同一人 -> 全备也排不开")
        from orgbudget.print_room import DocCandidate
        clash = PrintRound(
            "RBAD",
            (DocCandidate("S1", "R", ("A",), ("B", "C")),
             DocCandidate("S2", "R", ("A",), ("B", "C"))),
            _fees(), frozenset(), duty,
        )
        bad = plan_prep(clash, (ALL,))
        _check("判定不可行并指出堵住的单",
               not bad.feasible and bad.failures[0].blocked[0].sheet_id in {"S1", "S2"})
        print(f"  失败诊断：{[(b.sheet_id, b.reason) for b in bad.failures[0].blocked]}")

        print("\n全部会签演示完成。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
