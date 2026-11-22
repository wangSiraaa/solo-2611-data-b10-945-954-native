"""印务室备印与指派的纯求解器（不含任何 I/O）。

一轮业务的输入：

* 本批请付单（每张单给出询证冻结下来的经办 / 复核候选“人、角色”）；
* 当班人、各人按角色的一次印金（每对按印金只收费一次）；
* 本轮不能在同一张单配对的互斥人名单；
* 若干互斥的人员缺席预案（含全员到岗，预案间只揭晓其一）。

硬约束：

* 每张单必须由两个不同的人盖全经办、复核两道印；
* 一人实际一轮只能动一次印（跨单全局占用，按人不按角色）；
* 开柜前只有一次机会决定取出哪些“人、角色”的印；预案揭晓后只能
  在已备印集合内重新指派，不能再借新章；
* 缺席者的印不可用。

:func:`plan_prep` 求覆盖全部预案的最低备印费，并给出每个预案可实际签的
逐单见证；若全备也不行，指出失败预案与堵住的单。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .auth import HANDLER, REVIEWER
from .errors import InfeasiblePrep, StampFeeMissing


@dataclass(frozen=True)
class DocCandidate:
    """一张请付单冻结下来的两道印候选人。"""

    sheet_id: str
    request_no: str
    handlers: tuple[str, ...]
    reviewers: tuple[str, ...]


@dataclass(frozen=True)
class PrintRound:
    """一轮印务室业务的完整输入。"""

    round_id: str
    sheets: tuple[DocCandidate, ...]
    # (人, 角色) -> 一次印金；同一对无论被几张单用都只收一次。
    fees: dict[tuple[str, str], int]
    # 本轮不能在同一张单配对的人对（无序，存储时已规范化）。
    incompatible_pairs: frozenset[tuple[str, str]] = frozenset()
    duty_people: tuple[str, ...] = ()

    def fee(self, person_id: str, role: str) -> int:
        try:
            return self.fees[(person_id, role)]
        except KeyError:
            raise StampFeeMissing(
                f"缺少 {person_id} 的 {role} 印金，开柜前无法决定是否备印"
            ) from None


@dataclass(frozen=True)
class AbsencePlan:
    """一份缺席预案；缺席集合为空即全员到岗。"""

    plan_id: str
    absent: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Witness:
    """一张单在某预案下的实际见证：哪两个人各盖哪道印。"""

    sheet_id: str
    handler: str
    reviewer: str


@dataclass(frozen=True)
class BlockedSheet:
    """全备也无法盖的单及堵点说明。"""

    sheet_id: str
    reason: str


@dataclass(frozen=True)
class PlanFailure:
    plan_id: str
    blocked: tuple[BlockedSheet, ...]


@dataclass(frozen=True)
class PrepPlan:
    """备印结论。"""

    feasible: bool
    total_fee: int
    prepared: tuple[tuple[str, str], ...]
    # 可行时：每个预案的逐单见证。
    per_plan: dict[str, tuple[Witness, ...]] = field(default_factory=dict)
    # 不可行时：失败预案与堵住的单。
    failures: tuple[PlanFailure, ...] = ()


def _normalize_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _incompatible(round: PrintRound, a: str, b: str) -> bool:
    return _normalize_pair(a, b) in round.incompatible_pairs


def _options(
    round: PrintRound,
    sheet: DocCandidate,
    present: frozenset[str],
    prepared: frozenset[tuple[str, str]],
    occupied: frozenset[str],
) -> tuple[list[str], list[str]]:
    """某张单在当前占用下仍可用的经办 / 复核候选（已排序，保证确定性）。"""
    handlers = [
        p for p in sorted(sheet.handlers)
        if p in present and (p, HANDLER) in prepared and p not in occupied
    ]
    reviewers = [
        p for p in sorted(sheet.reviewers)
        if p in present and (p, REVIEWER) in prepared and p not in occupied
    ]
    return handlers, reviewers


def _first_stuck(
    round: PrintRound,
    present: frozenset[str],
    prepared: frozenset[tuple[str, str]],
    sheet_by_id: dict[str, DocCandidate],
) -> tuple[str, frozenset[str]]:
    """按 MRV 顺序找出当前最先无配对可填的单（供失败诊断）。"""
    occupied: frozenset[str] = frozenset()
    remaining = set(sheet_by_id)
    while remaining:
        target: DocCandidate | None = None
        best_count: int | None = None
        for sheet_id in sorted(remaining):
            sheet = sheet_by_id[sheet_id]
            handlers, reviewers = _options(round, sheet, present, prepared, occupied)
            count = sum(
                1 for h in handlers for r in reviewers
                if h != r and not _incompatible(round, h, r)
            )
            if best_count is None or count < best_count:
                target, best_count = sheet, count
            if best_count == 0:
                break
        assert target is not None
        if best_count == 0:
            return target.sheet_id, occupied
        # 取该单第一个可行配对占位，继续模拟后续单。
        handlers, reviewers = _options(round, target, present, prepared, occupied)
        h, r = next(
            (h, r) for h in handlers for r in reviewers
            if h != r and not _incompatible(round, h, r)
        )
        remaining.discard(target.sheet_id)
        occupied = occupied | {h, r}
    return next(iter(sheet_by_id)), occupied


def _assign(
    round: PrintRound,
    plan: AbsencePlan,
    prepared: frozenset[tuple[str, str]],
) -> tuple[tuple[Witness, ...] | None, tuple[str, frozenset[str]] | None]:
    """回溯求一套盖齐全批的指派（MRV：先填选择最少的单）。

    成功返回见证元组；失败返回 (None, (卡住的单, 当时占用集合))，供诊断
    与分支搜索复用。
    """
    present = frozenset(p for p in round.duty_people if p not in plan.absent)
    sheet_by_id = {s.sheet_id: s for s in round.sheets}

    def backtrack(
        remaining: frozenset[str],
        occupied: frozenset[str],
        chosen: dict[str, Witness],
    ) -> dict[str, Witness] | None:
        """成功返回完整见证字典；失败返回 None。失败不残留任何状态。

        每帧用 MRV 选定一张单并在本帧内固定，只把“剩余单 + 新占用”传给
        下一帧，因此同一单不会被重复处理，占用集合也严格随路径增长。
        """
        if not remaining:
            return chosen
        target: DocCandidate | None = None
        target_pairs: list[tuple[str, str]] = []
        best_key: tuple[int, int, str] | None = None
        for sheet_id in sorted(remaining):
            sheet = sheet_by_id[sheet_id]
            handlers, reviewers = _options(round, sheet, present, prepared, occupied)
            pairs = [
                (h, r) for h in handlers for r in reviewers
                if h != r and not _incompatible(round, h, r)
            ]
            # MRV：可行配对少的先填；并列时候选总人数少（更受限）的先填，
            # 再并列按单 ID，保证结果确定。注意 0 配对只代表“当前占用”下
            # 无解，不能全局剪枝——本帧配对为空自然不产生分支、正确回溯。
            key = (len(pairs), len(handlers) + len(reviewers), sheet_id)
            if best_key is None or key < best_key:
                target, target_pairs, best_key = sheet, pairs, key
        assert target is not None
        next_remaining = remaining - {target.sheet_id}
        for h, r in target_pairs:
            solution = backtrack(
                next_remaining,
                occupied | {h, r},
                {**chosen, target.sheet_id: Witness(target.sheet_id, h, r)},
            )
            if solution is not None:
                return solution
        return None

    solution = backtrack(frozenset(sheet_by_id), frozenset(), {})
    if solution is not None:
        return tuple(solution[s.sheet_id] for s in round.sheets), None
    # 失败诊断：重跑一次定位当前最先卡死的单。
    return None, _first_stuck(round, present, prepared, sheet_by_id)


def _diagnose(
    round: PrintRound,
    plan: AbsencePlan,
    prepared: frozenset[tuple[str, str]],
) -> tuple[BlockedSheet, ...]:
    """全备失败时逐单说明堵点（先结构性、再全局占用）。"""
    present = frozenset(p for p in round.duty_people if p not in plan.absent)
    blocked: list[BlockedSheet] = []
    for sheet in round.sheets:
        handlers, reviewers = _options(
            round, sheet, present, prepared, frozenset()
        )
        if not handlers:
            blocked.append(BlockedSheet(sheet.sheet_id, "无在岗且已备印的经办人"))
            continue
        if not reviewers:
            blocked.append(BlockedSheet(sheet.sheet_id, "无在岗且已备印的复核人"))
            continue
        pairs = [
            (h, r) for h in handlers for r in reviewers
            if h != r and not _incompatible(round, h, r)
        ]
        if not pairs:
            if all(h in reviewers for h in handlers) and len(set(handlers) | set(reviewers)) == 1:
                blocked.append(BlockedSheet(sheet.sheet_id, "经办与复核只剩同一人，不能自核"))
            else:
                blocked.append(BlockedSheet(sheet.sheet_id, "候选组合全部命中本轮互斥名单"))
    if blocked:
        return tuple(blocked)
    # 结构上每单都能配对，但全局一人一轮一次仍排不开：报告回溯卡死的单。
    _, stuck = _assign(round, plan, prepared)
    assert stuck is not None
    return (BlockedSheet(stuck[0], "候选在岗但一人一轮只能动一次印，全批排不开"),)


def _cost(round: PrintRound, pairs: frozenset[tuple[str, str]]) -> int:
    return sum(round.fee(p, role) for p, role in pairs)


def plan_prep(round: PrintRound, plans: tuple[AbsencePlan, ...]) -> PrepPlan:
    """求覆盖全部预案的最低备印费与逐预案见证。"""
    if not round.sheets:
        raise ValueError("本轮没有请付单")
    if not plans:
        raise ValueError("至少登记一个缺席预案（含全员到岗）")
    # 候选涉及的每对“人、角色”必须有印金，否则开柜前无法决定。
    universe: set[tuple[str, str]] = set()
    for sheet in round.sheets:
        for p in sheet.handlers:
            universe.add((p, HANDLER))
        for p in sheet.reviewers:
            universe.add((p, REVIEWER))
    for pair in universe:
        round.fee(*pair)  # 缺印金直接 StampFeeMissing

    full = frozenset(universe)
    failures: list[PlanFailure] = []
    full_witness: dict[str, tuple[Witness, ...]] = {}
    for plan in plans:
        unknown = plan.absent - frozenset(round.duty_people)
        if unknown:
            raise ValueError(f"预案 {plan.plan_id} 缺席人不在当班名单: {sorted(unknown)}")
        witnesses, _ = _assign(round, plan, full)
        if witnesses is None:
            failures.append(PlanFailure(plan.plan_id, _diagnose(round, plan, full)))
        else:
            full_witness[plan.plan_id] = witnesses
    if failures:
        return PrepPlan(
            feasible=False,
            total_fee=_cost(round, full),
            prepared=tuple(sorted(full)),
            failures=tuple(failures),
        )

    # 强制备印：去掉它就有某个预案盖不齐，任何可行解都必须包含。
    forced: frozenset[tuple[str, str]] = frozenset(
        pair for pair in sorted(full)
        if any(_assign(round, plan, full - {pair})[0] is None for plan in plans)
    )

    # 贪心初解给出费用上界：每个预案取一套全备指派，合并所需印。
    greedy: set[tuple[str, str]] = set(forced)
    for plan in plans:
        for w in full_witness[plan.plan_id]:
            greedy.add((w.handler, HANDLER))
            greedy.add((w.reviewer, REVIEWER))
    best = frozenset(greedy)
    best_cost = _cost(round, best)

    optional = sorted(full - forced, key=lambda pair: (round.fee(*pair), pair))

    def branch(
        current: frozenset[tuple[str, str]],
        idx: int,
    ) -> None:
        """对可选印做完备的“取 / 不取”深度优先搜索。

        只做安全剪枝：当前费用已不优于已知最优时，再叠加只会更贵。
        不能按“卡住单的候选”裁剪分支，因为解锁一张单可能需要先为另一张
        单补印以腾出被占用的人。可选印按印金升序，使便宜解先被发现、
        下界剪枝尽早生效。
        """
        nonlocal best, best_cost
        if _cost(round, current) >= best_cost:
            return
        if all(_assign(round, plan, current)[0] is not None for plan in plans):
            best = current
            best_cost = _cost(round, current)
            return
        if idx >= len(optional):
            return
        pair = optional[idx]
        # 先尝试取这枚印（沿升序更可能先得到便宜的可行解），再尝试不取。
        branch(current | {pair}, idx + 1)
        branch(current, idx + 1)

    branch(forced, 0)

    prepared = frozenset(best)
    per_plan: dict[str, tuple[Witness, ...]] = {}
    for plan in plans:
        witnesses, stuck = _assign(round, plan, prepared)
        if stuck is not None:  # 理论不可达：best 已验证覆盖全部预案。
            raise InfeasiblePrep(f"预案 {plan.plan_id} 在最优备印下仍无法盖齐")
        per_plan[plan.plan_id] = witnesses  # type: ignore[assignment]
    return PrepPlan(
        feasible=True,
        total_fee=best_cost,
        prepared=tuple(sorted(prepared)),
        per_plan=per_plan,
    )
