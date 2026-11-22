"""印厂制版排位（纯函数，消费端模块）。

本模块**不导入**组织服务、预算仓或凭单封存仓，也不打开任何数据库：
印厂只接收旧/新栏凭单（:class:`~orgbudget.flow_voucher.FlowVoucher` 或其
JSON 载荷）和自己的制版约束，产出可直接制版的排位工单。

规则（题定）：

* 每边为实际栏头安排一个**完整顺序**，栏头在左右边不能擅自拆分、增删；
  每侧至多 :data:`~orgbudget.flow_voucher.MAX_COLUMNS_PER_SIDE` 栏。
* 局部先后约束 ``before``：``(u, v)`` 表示 u 必须严格排在 v 前；
  ``fixed_first`` 把指定栏头钉在该边第一位（固定左栏亦由此支持）。
* 两条正宽带在一边的端点严格在前、另一边严格在后才**相交**；共享任一
  端点不交。每对交带耗 ``宽度乘积`` 个隔墨点。
* 在满足题定先后的全部排位中找最低耗点；先后自相矛盾（有环）便退单。
* 最低耗点超过隔墨预算则退给制版员并列超额，否则签出该版。

算法：两侧排列都自由的二层带交叉最小化不存在“左一元项 + 右一元项”的
可加分解（交互项是两侧顺序符号的乘积），一般情形为 NP-hard。题定每侧
至多 7 栏，因此：

1. 枚举左栏满足先后约束的全部完整排列（至多 7! = 5040）；
2. 对每个左排列，构造右栏逐对代价矩阵 ``c[x][y]``（右栏 x 先于 y 时
   全部相交带对的宽度乘积之和），右栏问题化为带逐对代价的最优线性排列，
   用 Held–Karp 子集 DP 在 O(n²·2ⁿ) 内精确求解，并在受右栏约束的可行
   排列集合上取最优；
3. 全部热路径用整数索引与定长列表，7×7 满格秒级完成，结果确定性、
   同耗点取字典序最小。单元测试用全排列暴力枚举逐例核对等价性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import permutations
from typing import Optional

from .errors import PlateInputError
from .flow_voucher import MAX_COLUMNS_PER_SIDE, FlowVoucher

SIGNED = "SIGNED"
REJECTED_OVER_BUDGET = "REJECTED_OVER_BUDGET"
REJECTED_CONFLICT = "REJECTED_CONFLICT"


@dataclass(frozen=True)
class SideConstraints:
    """一边的局部先后约束。

    ``before`` 中的 ``(u, v)`` 要求 u 严格先于 v；``fixed_first`` 非空时
    该栏头必须排在第一位（固定左栏场景）。
    """

    before: tuple[tuple[str, str], ...] = ()
    fixed_first: Optional[str] = None


@dataclass(frozen=True)
class PlateConstraints:
    """印厂对左右两栏的制版约束与单页隔墨点预算。"""

    left: SideConstraints = field(default_factory=SideConstraints)
    right: SideConstraints = field(default_factory=SideConstraints)
    ink_dot_budget: int = 0


@dataclass(frozen=True)
class Crossing:
    """逐对带宽交叉表中的一行。"""

    band_a: tuple[str, str]
    band_b: tuple[str, str]
    width_a: int
    width_b: int
    crossed: bool
    reason: str
    ink_dots: int


@dataclass(frozen=True)
class WorkOrder:
    """可直接制版的排位工单（或退单结果）。"""

    voucher_no: str
    status: str
    left_order: tuple[str, ...]
    right_order: tuple[str, ...]
    ink_dots: int
    ink_dot_budget: int
    crossings: tuple[Crossing, ...]
    conflict_pairs: tuple[tuple[str, str], ...]
    reason: str

    @property
    def is_signed(self) -> bool:
        return self.status == SIGNED

    def to_dict(self) -> dict:
        return {
            "voucher_no": self.voucher_no,
            "status": self.status,
            "left_order": list(self.left_order),
            "right_order": list(self.right_order),
            "ink_dots": self.ink_dots,
            "ink_dot_budget": self.ink_dot_budget,
            "crossings": [
                {
                    "band_a": list(c.band_a),
                    "band_b": list(c.band_b),
                    "width_a": c.width_a,
                    "width_b": c.width_b,
                    "crossed": c.crossed,
                    "reason": c.reason,
                    "ink_dots": c.ink_dots,
                }
                for c in self.crossings
            ],
            "conflict_pairs": [list(p) for p in self.conflict_pairs],
            "reason": self.reason,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------- 约束处理
def _normalise_side(
    side: SideConstraints, index: dict[str, int], side_name: str
) -> frozenset[tuple[int, int]]:
    """把栏头名约束翻译成整数索引对，并并入固定首位约束。"""
    pairs: set[tuple[int, int]] = set()
    for u, v in side.before:
        if u not in index or v not in index:
            raise PlateInputError(
                f"{side_name}边先后约束引用了不存在的栏头: ({u}, {v})"
            )
        if u == v:
            raise PlateInputError(f"{side_name}边先后约束不能要求栏头 {u} 先于自身")
        pairs.add((index[u], index[v]))
    if side.fixed_first is not None:
        if side.fixed_first not in index:
            raise PlateInputError(f"{side_name}边固定首位栏头 {side.fixed_first} 不存在")
        fixed = index[side.fixed_first]
        for other in index.values():
            if other != fixed:
                pairs.add((fixed, other))
    return frozenset(pairs)


def _conflict_pairs(
    names: tuple[str, ...], pairs: frozenset[tuple[int, int]]
) -> tuple[tuple[str, str], ...]:
    """约束图成环（相互可达）的矛盾对，按栏头名字典序返回。"""
    n = len(names)
    reach: list[set[int]] = [set() for _ in range(n)]
    for u, v in pairs:
        reach[u].add(v)
    for mid in range(n):  # Floyd–Warshall 传递闭包，n ≤ 7。
        for u in range(n):
            if mid in reach[u]:
                reach[u].update(reach[mid])
    conflicts = [
        tuple(sorted((names[u], names[v])))
        for u in range(n)
        for v in range(u + 1, n)
        if v in reach[u] and u in reach[v]
    ]
    return tuple(sorted(set(conflicts)))


def _feasible_perms(
    n: int, pairs: frozenset[tuple[int, int]]
) -> list[tuple[int, ...]]:
    """满足全部先后约束的完整排列（整数索引），按排列字典序产出。"""
    result: list[tuple[int, ...]] = []
    for perm in permutations(range(n)):
        position = [0] * n
        for pos, node in enumerate(perm):
            position[node] = pos
        if all(position[u] < position[v] for u, v in pairs):
            result.append(perm)
    return result


# ---------------------------------------------------------------- 隔墨点求解
def _cost_matrix_for_other_side(
    enum_perm: tuple[int, ...],
    n_enum: int,
    n_other: int,
    width: list[list[int]],
    *,
    enum_is_left: bool,
) -> list[list[int]]:
    """已知一侧（枚举侧）的完整排列，构造另一侧的逐对交叉代价矩阵。

    返回 ``c[i][j]``：另一侧栏头 i 严格先于 j 时，所有因此相交的带对
    宽度乘积之和。带 (e1, i)、(e2, j) 在枚举侧方向相反（e2 先于 e1）时
    相交；共享枚举侧端点（e1=e2）不交。``enum_is_left`` 决定宽度矩阵的
    取值下标方向，使本函数对“枚举左排 / 枚举右排”两种情形通用。
    """
    enum_pos = [0] * n_enum
    for pos, node in enumerate(enum_perm):
        enum_pos[node] = pos

    def w(enum_node: int, other_node: int) -> int:
        return width[enum_node][other_node] if enum_is_left \
            else width[other_node][enum_node]

    matrix = [[0] * n_other for _ in range(n_other)]
    for i in range(n_other):
        for j in range(n_other):
            if i == j:
                continue
            total = 0
            for e1 in range(n_enum):
                w1 = w(e1, i)
                if w1 == 0:
                    continue
                for e2 in range(n_enum):
                    if e1 == e2:
                        continue  # 共享枚举侧端点，不相交。
                    w2 = w(e2, j)
                    if w2 and enum_pos[e2] < enum_pos[e1]:
                        total += w1 * w2
            matrix[i][j] = total
    return matrix


def _prereq_masks(
    n: int, pairs: frozenset[tuple[int, int]]
) -> list[int]:
    """每个栏头的前置栏头位图：prereq[v] 中的栏头必须严格先于 v。"""
    prereq = [0] * n
    for u, v in pairs:
        prereq[v] |= 1 << u
    return prereq


def _best_order_dp(
    n: int,
    matrix: list[list[int]],
    prereq: list[int],
) -> tuple[tuple[int, ...], int]:
    """带先后约束的 Held–Karp 子集 DP，求逐对代价和最低的完整排列。

    总耗点对排列是逐对可加的（``Σ_{i<j} c[perm_i][perm_j]``），具备最优
    子结构：把 v 接到已选集合末尾时，增量为 Σ_{u∈mask} c[u][v]。约束以
    前置位图剪枝（v 的前置栏头未排齐则该状态不可行）。同耗点保留字典序
    最小排列。n ≤ 7，状态至多 2ⁿ=128 个。
    """
    full = (1 << n) - 1
    # dp[mask] = (最低耗点, 达到该耗点的字典序最小排列)。
    dp: dict[int, tuple[int, tuple[int, ...]]] = {0: (0, ())}
    for mask in range(1, full + 1):
        best: Optional[tuple[int, tuple[int, ...]]] = None
        bits = mask
        while bits:
            bit = bits & -bits
            bits -= bit
            v = bit.bit_length() - 1
            if prereq[v] & ~mask:
                continue  # v 的前置栏头还没排齐，状态不可行。
            prev_mask = mask ^ bit
            if prev_mask not in dp:
                continue
            prev_cost, prev_perm = dp[prev_mask]
            added = 0
            earlier = prev_mask
            while earlier:
                eb = earlier & -earlier
                earlier -= eb
                u = eb.bit_length() - 1
                added += matrix[u][v]
            candidate = (prev_cost + added, prev_perm + (v,))
            if best is None or candidate < best:
                best = candidate
        if best is not None:
            dp[mask] = best
    if full not in dp:
        # 理论上不会发生：调用方已先用 _conflict_pairs 排除成环约束。
        raise PlateInputError("先后约束无解")
    return dp[full][1], dp[full][0]


def _crossing_table(
    width: list[list[int]],
    left_names: tuple[str, ...],
    right_names: tuple[str, ...],
    left_perm: tuple[int, ...],
    right_perm: tuple[int, ...],
) -> tuple[Crossing, ...]:
    """逐对带宽交叉表：共享端点标注不交，其余按两端相对位置判定。"""
    left_pos = [0] * len(left_perm)
    for pos, node in enumerate(left_perm):
        left_pos[node] = pos
    right_pos = [0] * len(right_perm)
    for pos, node in enumerate(right_perm):
        right_pos[node] = pos

    bands = [
        (a, x, width[a][x])
        for a in range(len(left_names))
        for x in range(len(right_names))
        if width[a][x] > 0
    ]
    rows: list[Crossing] = []
    for i in range(len(bands)):
        a, x, wa = bands[i]
        for j in range(i + 1, len(bands)):
            b, y, wb = bands[j]
            key_a = (left_names[a], right_names[x])
            key_b = (left_names[b], right_names[y])
            if a == b or x == y:
                rows.append(Crossing(
                    band_a=key_a, band_b=key_b, width_a=wa, width_b=wb,
                    crossed=False, reason="共享端点，不相交", ink_dots=0,
                ))
                continue
            crossed = (left_pos[a] < left_pos[b]) != (right_pos[x] < right_pos[y])
            rows.append(Crossing(
                band_a=key_a, band_b=key_b, width_a=wa, width_b=wb,
                crossed=crossed,
                reason=("一端严格在前、另一端严格在后，相交" if crossed
                        else "两端同向，不相交"),
                ink_dots=wa * wb if crossed else 0,
            ))
    return tuple(rows)


def _validate_headers(voucher: FlowVoucher) -> None:
    if not voucher.bands:
        raise PlateInputError("凭单没有正带宽，无法排位制版")
    if len(voucher.old_headers) > MAX_COLUMNS_PER_SIDE or \
            len(voucher.new_headers) > MAX_COLUMNS_PER_SIDE:
        raise PlateInputError(
            f"每侧至多 {MAX_COLUMNS_PER_SIDE} 栏，"
            f"实际 {len(voucher.old_headers)}/{len(voucher.new_headers)}"
        )
    for band in voucher.bands:
        if band.old_header not in voucher.old_headers \
                or band.new_header not in voucher.new_headers:
            raise PlateInputError(
                f"带宽 ({band.old_header}→{band.new_header}) 引用了凭单栏头清单之外的部门"
            )


def issue_work_order(voucher: FlowVoucher, constraints: PlateConstraints) -> WorkOrder:
    """按制版约束为凭单出具排位工单（签出或退单）。"""
    voucher.verify()
    _validate_headers(voucher)
    if not isinstance(constraints.ink_dot_budget, int) \
            or isinstance(constraints.ink_dot_budget, bool) \
            or constraints.ink_dot_budget < 0:
        raise PlateInputError("隔墨点预算必须是非负整数")

    left_names = tuple(sorted(voucher.old_headers))
    right_names = tuple(sorted(voucher.new_headers))
    left_index = {name: i for i, name in enumerate(left_names)}
    right_index = {name: i for i, name in enumerate(right_names)}
    left_pairs = _normalise_side(constraints.left, left_index, "左")
    right_pairs = _normalise_side(constraints.right, right_index, "右")

    left_conflicts = _conflict_pairs(left_names, left_pairs)
    right_conflicts = _conflict_pairs(right_names, right_pairs)
    if left_conflicts or right_conflicts:
        return WorkOrder(
            voucher_no=voucher.voucher_no,
            status=REJECTED_CONFLICT,
            left_order=(),
            right_order=(),
            ink_dots=0,
            ink_dot_budget=constraints.ink_dot_budget,
            crossings=(),
            conflict_pairs=tuple(sorted(set(left_conflicts) | set(right_conflicts))),
            reason="题定先后自相矛盾（约束成环），退单",
        )

    # 带宽矩阵 W[a][x]：缺格为 0（零宽不是带，不参与交叉）。
    width = [[0] * len(right_names) for _ in range(len(left_names))]
    for band in voucher.bands:
        width[left_index[band.old_header]][right_index[band.new_header]] = band.width

    n_left, n_right = len(left_names), len(right_names)
    feasible_left = _feasible_perms(n_left, left_pairs)
    feasible_right = _feasible_perms(n_right, right_pairs)
    left_prereq = _prereq_masks(n_left, left_pairs)
    right_prereq = _prereq_masks(n_right, right_pairs)

    # 选择可行排列较少的一侧枚举，另一侧用子集 DP 精确求解（对称等价）。
    enumerate_left = len(feasible_left) <= len(feasible_right)
    best: Optional[tuple[int, tuple[int, ...], tuple[int, ...]]] = None
    if enumerate_left:
        for left_perm in feasible_left:
            matrix = _cost_matrix_for_other_side(
                left_perm, n_left, n_right, width, enum_is_left=True
            )
            right_perm, cost = _best_order_dp(n_right, matrix, right_prereq)
            candidate = (cost, left_perm, right_perm)
            if best is None or candidate < best:
                best = candidate
    else:
        for right_perm in feasible_right:
            matrix = _cost_matrix_for_other_side(
                right_perm, n_right, n_left, width, enum_is_left=False
            )
            left_perm, cost = _best_order_dp(n_left, matrix, left_prereq)
            # 统一按 (耗点, 左排列, 右排列) 比较，保证确定性。
            candidate = (cost, left_perm, right_perm)
            if best is None or candidate < best:
                best = candidate
    assert best is not None
    ink_dots, left_perm, right_perm = best
    left_order = tuple(left_names[i] for i in left_perm)
    right_order = tuple(right_names[i] for i in right_perm)
    crossings = _crossing_table(
        width, left_names, right_names, left_perm, right_perm
    )
    # 交叉表自洽：表内耗点合计必须等于排位算出的最低耗点。
    assert sum(c.ink_dots for c in crossings) == ink_dots

    if ink_dots > constraints.ink_dot_budget:
        return WorkOrder(
            voucher_no=voucher.voucher_no,
            status=REJECTED_OVER_BUDGET,
            left_order=left_order,
            right_order=right_order,
            ink_dots=ink_dots,
            ink_dot_budget=constraints.ink_dot_budget,
            crossings=crossings,
            conflict_pairs=(),
            reason=(f"最低耗点 {ink_dots} 超过隔墨预算 {constraints.ink_dot_budget}，"
                    f"超额 {ink_dots - constraints.ink_dot_budget} 点，退给制版员"),
        )
    return WorkOrder(
        voucher_no=voucher.voucher_no,
        status=SIGNED,
        left_order=left_order,
        right_order=right_order,
        ink_dots=ink_dots,
        ink_dot_budget=constraints.ink_dot_budget,
        crossings=crossings,
        conflict_pairs=(),
        reason=f"最低耗点 {ink_dots} ≤ 预算 {constraints.ink_dot_budget}，签出该版",
    )


def issue_work_order_from_payload(
    payload: dict, constraints: PlateConstraints
) -> WorkOrder:
    """印厂消费端入口：只凭 JSON 载荷 + 自己的约束开工单，不接任何数据库。"""
    return issue_work_order(FlowVoucher.from_dict(payload), constraints)
