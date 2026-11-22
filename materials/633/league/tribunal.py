"""听审排庭：合庭/分庭、主席回避、传卷次数与截止日的约束求解。

输入（全部来自冻结逐球产品与奖务登记，不依赖活账）
====================================================
* 待核听审案列表：每案绑定一场比赛的一卷具名原件与其唯一实际保管人；
  同场两案可合庭（共传保管人一次），也可分庭。
* 主席可用性：每位主席给出可出席的日子；另有一份事先登记的回避清单
  （人员或培训站），只要该案任一可能收件者命中清单，该主席就不得主持。
* 办奖截止日、可选的传卷次数上限；求解目标按优先级：
  1. 传卷次数最少（合庭优先）；
  2. 同样少传时最早收完（最后一个庭的日子最小）；
  3. 仍平手再省传卷（本就由第 1 条保证，保留为显式 tie-break 文档）。

硬约束
======
* 每庭占一整天；主席与保管人是不同的人；同一个人一天只能在一个庭出现。
* 合庭只允许同场案件，且须有一位对两案都不回避的主席；分庭各自主持。
* 排期不得晚于截止日；给定传卷上限时不可超过，超出即无解。
* 每批最多三场、六案、四位主席、五天；每场固定一位保管人。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from itertools import product
from typing import Any, Mapping, Sequence

from .awards import HearingCase


class SchedulingError(ValueError):
    """排庭输入非法或约束无解。"""


@dataclass(frozen=True)
class Chair:
    """主席：可出席日子与事先登记的回避清单（人员/培训站 ID）。"""

    chair_id: str
    available_days: tuple[int, ...]
    recused: frozenset[str]

    def can_hear(self, case: HearingCase) -> bool:
        return not any(party in self.recused for party in case.conflict_parties)


@dataclass
class Tribunal:
    """一个庭：一天、一位主席、一位到场保管人、合并或单独审理的案件。"""

    day: int
    chair_id: str
    custodian: str
    case_ids: list[str]
    match_id: str
    volume_id: str
    joint: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SchedulePlan:
    """排庭结果：庭通知、传卷次数、收完日与可行性说明。"""

    feasible: bool
    tribunals: list[Tribunal]
    summons: int
    finished_day: int | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "tribunals": [t.to_dict() for t in self.tribunals],
            "summons": self.summons, "finished_day": self.finished_day,
            "reason": self.reason,
        }


# 批次硬上限（题面约束）
MAX_MATCHES = 3
MAX_CASES = 6
MAX_CHAIRS = 4
MAX_DAYS = 5


def _validate(cases: Sequence[HearingCase], chairs: Sequence[Chair],
              deadline: int) -> dict[str, list[HearingCase]]:
    if not cases:
        raise SchedulingError("没有待核案件，无需排庭")
    if len(cases) > MAX_CASES:
        raise SchedulingError(f"每批最多 {MAX_CASES} 案，实际 {len(cases)}")
    if len(chairs) > MAX_CHAIRS:
        raise SchedulingError(f"每批最多 {MAX_CHAIRS} 位主席，实际 {len(chairs)}")
    by_match: dict[str, list[HearingCase]] = {}
    for c in cases:
        by_match.setdefault(c.match_id, []).append(c)
    if len(by_match) > MAX_MATCHES:
        raise SchedulingError(f"每批最多 {MAX_MATCHES} 场，实际 {len(by_match)}")
    for mid, group in by_match.items():
        if len(group) > 2:
            raise SchedulingError(f"场 {mid} 同场案件超过 2 件，模型只支持两案")
        custodians = {c.custodian for c in group}
        if len(custodians) != 1:
            raise SchedulingError(f"场 {mid} 保管人不唯一：{custodians}")
    if not isinstance(deadline, int) or deadline < 1 or deadline > MAX_DAYS:
        raise SchedulingError(f"截止日须落在 1..{MAX_DAYS}")
    for chair in chairs:
        if any(d < 1 or d > MAX_DAYS for d in chair.available_days):
            raise SchedulingError(f"主席 {chair.chair_id} 可用日超出 1..{MAX_DAYS}")
    return by_match


def _eligible_chairs(case_group: Sequence[HearingCase],
                     chairs: Sequence[Chair]) -> list[Chair]:
    """对一组（合庭）案件全部不回避的主席。"""
    return [c for c in chairs if all(c.can_hear(case) for case in case_group)]


def schedule_hearings(
    cases: Sequence[HearingCase],
    chairs: Sequence[Chair],
    deadline: int,
    summon_cap: int | None = None,
) -> SchedulePlan:
    """求传卷最少、其次最早收完的排庭；无解返回 feasible=False 并说明。"""
    by_match = _validate(cases, chairs, deadline)
    chair_by_id = {c.chair_id: c for c in chairs}

    # 每场生成两种庭结构选项：合庭（1 庭、传卷 1 次）或分庭（每案 1 庭、传卷 1 次/庭，
    # 但同场保管人相同 => 分庭也是每场传卷次数 = 庭数，因为不同天各传一次）。
    # 传卷次数 = 庭数（每个庭都要保管人到场一次；合庭把两案压成一个庭）。
    options: list[list[dict[str, Any]]] = []
    for mid, group in by_match.items():
        custodian = group[0].custodian
        volume = group[0].volume_id
        choices: list[dict[str, Any]] = []
        # 合庭选项
        joint_eligible = _eligible_chairs(group, chairs)
        if joint_eligible:
            choices.append({
                "joint": True, "match_id": mid, "custodian": custodian,
                "volume_id": volume,
                "case_ids": [c.case_id for c in group],
                "chairs": [c.chair_id for c in joint_eligible],
                "n_tribunals": 1,
            })
        # 分庭选项（每案独立，主席只需不回避该案）
        split_parts = []
        split_ok = True
        for case in group:
            elig = [c.chair_id for c in chairs if c.can_hear(case)]
            if not elig:
                split_ok = False
                break
            split_parts.append({"case_id": case.case_id, "chairs": elig})
        if split_ok:
            choices.append({
                "joint": False, "match_id": mid, "custodian": custodian,
                "volume_id": volume, "parts": split_parts,
                "n_tribunals": len(group),
            })
        if not choices:
            return SchedulePlan(False, [], 0, None,
                                f"场 {mid} 无任何不回避的可用主席，无法排庭")
        options.append(choices)

    best: dict[str, Any] | None = None
    # 枚举每场合/分选择（至多 3 场 => 2^3=8 种结构），再在每种结构上做天/主席指派。
    for struct in product(*options):
        total_tribunals = sum(s["n_tribunals"] for s in struct)
        if summon_cap is not None and total_tribunals > summon_cap:
            continue
        assignment = _assign_days(struct, chair_by_id, deadline)
        if assignment is None:
            continue
        finished = max(t["day"] for t in assignment)
        key = (total_tribunals, finished)
        if best is None or key < (best["summons"], best["finished_day"]):
            best = {"summons": total_tribunals, "finished_day": finished,
                    "struct": struct, "assignment": assignment}

    if best is None:
        cap_note = f"，传卷上限 {summon_cap}" if summon_cap is not None else ""
        return SchedulePlan(
            False, [], 0, None,
            f"截止日 {deadline}{cap_note} 内无满足回避/人力约束的排庭")

    tribunals = [
        Tribunal(day=a["day"], chair_id=a["chair_id"],
                 custodian=a["custodian"], case_ids=list(a["case_ids"]),
                 match_id=a["match_id"], volume_id=a["volume_id"],
                 joint=a["joint"])
        for a in best["assignment"]
    ]
    tribunals.sort(key=lambda t: (t.day, t.chair_id))
    return SchedulePlan(True, tribunals, best["summons"],
                        best["finished_day"])


def _assign_days(
    struct: Sequence[Mapping[str, Any]],
    chair_by_id: Mapping[str, Chair],
    deadline: int,
) -> list[dict[str, Any]] | None:
    """对固定庭结构，回溯指派 (天, 主席)，返回最早收完的可行指派；无解返回 None。"""
    # 展平成待排“庭槽”：合庭 1 槽，分庭每案 1 槽。
    slots: list[dict[str, Any]] = []
    for choice in struct:
        if choice["joint"]:
            slots.append({
                "joint": True, "match_id": choice["match_id"],
                "custodian": choice["custodian"], "volume_id": choice["volume_id"],
                "case_ids": list(choice["case_ids"]),
                "eligible": list(choice["chairs"]),
            })
        else:
            for part in choice["parts"]:
                slots.append({
                    "joint": False, "match_id": choice["match_id"],
                    "custodian": choice["custodian"], "volume_id": choice["volume_id"],
                    "case_ids": [part["case_id"]],
                    "eligible": list(part["chairs"]),
                })

    days = list(range(1, deadline + 1))
    result: list[dict[str, Any]] | None = None

    def backtrack(idx: int, busy_chair_day: set[tuple[str, int]],
                  busy_custodian_day: set[tuple[str, int]],
                  acc: list[dict[str, Any]]) -> bool:
        nonlocal result
        if idx == len(slots):
            result = [dict(x) for x in acc]
            return True
        slot = slots[idx]
        custodian = slot["custodian"]
        # 日子从小到大尝试，天然偏向尽早收完。
        for day in days:
            if (custodian, day) in busy_custodian_day:
                continue  # 保管人一天只能在一庭（含同场分庭不得同日）
            for chair_id in slot["eligible"]:
                chair = chair_by_id[chair_id]
                if day not in chair.available_days:
                    continue
                if (chair_id, day) in busy_chair_day:
                    continue  # 主席一天只能在一庭
                if chair_id == custodian:
                    continue  # 主席与保管人必须是不同的人
                acc.append({"day": day, "chair_id": chair_id,
                            "custodian": custodian,
                            "case_ids": list(slot["case_ids"]),
                            "match_id": slot["match_id"],
                            "volume_id": slot["volume_id"],
                            "joint": slot["joint"]})
                busy_chair_day.add((chair_id, day))
                busy_custodian_day.add((custodian, day))
                if backtrack(idx + 1, busy_chair_day, busy_custodian_day, acc):
                    return True
                acc.pop()
                busy_chair_day.discard((chair_id, day))
                busy_custodian_day.discard((custodian, day))
        return False

    if backtrack(0, set(), set(), []):
        return result
    return None


def earliest_finish_with_cap(
    cases: Sequence[HearingCase],
    chairs: Sequence[Chair],
    summon_cap: int,
    max_day: int = MAX_DAYS,
) -> SchedulePlan:
    """馆方给定传卷上限时，查询最快哪天能审完（截止日放宽到 max_day 扫描）。"""
    if summon_cap < 1:
        return SchedulePlan(
            False, [], 0, None, f"传卷次数上限 {summon_cap} 非法：至少需要 1 次")
    best: SchedulePlan | None = None
    for deadline in range(1, max_day + 1):
        plan = schedule_hearings(cases, chairs, deadline, summon_cap=summon_cap)
        if plan.feasible and (best is None or plan.finished_day < best.finished_day):
            best = plan
            break  # 日子递增，第一个可行即最快
    if best is None:
        return SchedulePlan(
            False, [], 0, None,
            f"传卷上限 {summon_cap} 下，{max_day} 天内无可行排庭")
    return best
