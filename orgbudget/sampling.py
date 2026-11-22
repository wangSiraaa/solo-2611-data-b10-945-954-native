"""内审抽查的配额求解器：精确有理数、最不利分布下的保证。

题面要求的保证是“在所有可能的问题封分布中，都须达到 p 的至少查到一封
概率”。设第 i 层共有 n_i 封、实际开 x_i 封，整批有问题的封数为 k（恰
k，未知落在哪几层）。对一种问题封分布（各层问题封数 y_i，0<=y_i<=n_i，
Σy_i = k），一层一封都没查到的概率是 C(n_i−y_i, x_i)/C(n_i, x_i)（约定
C(0,0)=1），各层独立故整批漏检概率是各层之积。

对固定配额 x，**最不利分布**使漏检概率最大——即求
max ∏ C(n_i−y_i, x_i)/C(n_i, x_i)，约束 Σy_i=k。这是一个分组背包
（按层推进、按已消耗的问题封数 DP），整数分子分母精确比较，无需浮点。
每个候选配额用同一 DP 求最不利分布及其精确分数，再在所有达到
`漏检概率 <= 1−p` 的配额中取总成本最小者；成本相同的配额按层序与
开封面数保留首次出现者。

为了给工单执行前复核，求解结果会同时列出各层实际**可选的封号**以及
会对它达到最不利的那个分布。整层开 0 封或全开 n 封都作为合法边界参与
枚举；当 p=1 或某层被全部开完时，DP 自然把该层的最坏漏检概率压到 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from math import comb
from typing import Sequence

from .errors import InvalidAuditRequest

#: 显式枚举全部配额时允许的最大规模（4 层 × 13 个取值 = 28561）。
_MAX_ENUMERATED_QUOTAS = 40_000


@dataclass(frozen=True)
class LayerQuota:
    """单层的抽封配额与核对信息。"""

    layer: str
    layer_no: int
    n: int          # 该层封数
    quota: int      # 实际开几封
    cost: int       # 每开封成本
    envelope_nos: tuple[int, ...]  # 实际可选的层内封号（1..n 取前 quota 个）
    worst_misses: int     # 最不利分布下该层问题封数
    worst_prob: Fraction  # 最不利分布下该层至少查中一封的概率

    @property
    def layer_cost(self) -> int:
        return self.quota * self.cost


@dataclass(frozen=True)
class SamplingPlan:
    """整批抽封方案，可独立复点。"""

    probability: Fraction         # 要求的最低发现概率 p
    problem_count: int            # 恰 k 封有问题（未知落点）
    total_cost: int
    quotas: tuple[int, ...]       # 各层配额，与入参层序一致
    layers: tuple[LayerQuota, ...]
    worst_misses: tuple[int, ...]  # 最不利分布：各层问题封数（层序一致）
    worst_miss_probability: Fraction  # 最不利分布下的漏检概率（整批）
    guarantee: Fraction           # 1 − worst_miss_probability（至少查到一封）

    @property
    def envelope_nos(self) -> tuple[tuple[int, ...], ...]:
        return tuple(layer.envelope_nos for layer in self.layers)


def _worst_distribution(
    sizes: Sequence[int], quotas: Sequence[int], k: int
) -> tuple[int, tuple[int, ...]]:
    """对固定配额求最不利问题封分布，返回 ``(漏检分子, 各层问题封数)``。

    DP 以“已分配的问题封数”为状态，概率分子为各层 C(n−y, x) 之积；
    分母 ∏C(n,x) 与分布无关，统一放到最后比较。并列时保留先出现的
    字典序靠前者，结果确定、可复跑。
    """
    # state: used -> (numerator, choices)
    frontier: dict[int, tuple[int, tuple[int, ...]]] = {0: (1, ())}
    for n, x in zip(sizes, quotas):
        nxt: dict[int, tuple[int, tuple[int, ...]]] = {}
        for used, (numerator, choices) in frontier.items():
            hi = min(n, k - used)
            for y in range(hi + 1):
                value = numerator * comb(n - y, x)
                state = used + y
                current = nxt.get(state)
                if current is None or value > current[0]:
                    nxt[state] = (value, choices + (y,))
        frontier = nxt
    found = frontier.get(k)
    if found is None:
        raise InvalidAuditRequest(
            f"整批仅有 {sum(sizes)} 封，无法容纳 {k} 封问题封"
        )
    return found


def worst_case_miss(
    sizes: Sequence[int], quotas: Sequence[int], k: int
) -> tuple[Fraction, tuple[int, ...]]:
    """给定任意配额，返回最不利分布下的漏检**概率**与分布。

    工单执行前可拿它独立复核某个配额；``solve_quota`` 内部也用同一 DP。
    """
    numerator, worst = _worst_distribution(tuple(sizes), tuple(quotas), k)
    den = _quota_denominators(sizes, quotas)
    return Fraction(numerator, den), worst


def _quota_denominators(sizes: Sequence[int], quotas: Sequence[int]) -> int:
    den = 1
    for n, x in zip(sizes, quotas):
        den *= comb(n, x)
    return den


def _validate(
    sizes: Sequence[int], costs: Sequence[int], k: int, probability: Fraction
) -> None:
    if not sizes:
        raise InvalidAuditRequest("至少需要一层")
    if len(sizes) > 4:
        raise InvalidAuditRequest("最多 4 层")
    if len(costs) != len(sizes):
        raise InvalidAuditRequest("每层必须给出一个开封成本")
    for n in sizes:
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 12:
            raise InvalidAuditRequest(f"每层封数必须是 0..12 的整数: {n!r}")
    for c in costs:
        if not isinstance(c, int) or isinstance(c, bool) or c < 0:
            raise InvalidAuditRequest(f"每开一封成本必须是非负整数: {c!r}")
    if not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise InvalidAuditRequest(f"问题封数 k 必须是非负整数: {k!r}")
    if k > sum(sizes):
        raise InvalidAuditRequest(
            f"问题封数 k={k} 超过整批封数 {sum(sizes)}"
        )
    if not isinstance(probability, Fraction):
        raise InvalidAuditRequest("p 必须是 fractions.Fraction（有理数）")
    if not 0 <= probability <= 1:
        raise InvalidAuditRequest(f"概率 p 必须在 [0,1] 内: {probability}")


def solve_quota(
    sizes: Sequence[int],
    costs: Sequence[int],
    k: int,
    probability: Fraction,
    *,
    names: Sequence[str] | None = None,
) -> SamplingPlan:
    """求总开封成本最低、且在最不利分布下仍达到 p 的各层配额。

    ``sizes`` 为各层封数（至多 4 层，每层 0..12），``costs`` 为各层每开
    一封的成本，``k`` 为整批恰有的问题封数，``probability`` 为要求的最低
    发现概率（至少查到一封）。返回的 :class:`SamplingPlan` 同时给出各层
    实际可选封号与最不利分布，便于工单执行前独立复核。
    """
    sizes = tuple(sizes)
    costs = tuple(costs)
    _validate(sizes, costs, k, probability)
    if names is None:
        names = tuple(f"L{i + 1}" for i in range(len(sizes)))
    elif len(names) != len(sizes):
        raise InvalidAuditRequest("层名数量与层数不一致")

    total_states = 1
    for n in sizes:
        total_states *= n + 1
    if total_states > _MAX_ENUMERATED_QUOTAS:
        raise InvalidAuditRequest(
            f"配额组合 {total_states} 超过上限 {_MAX_ENUMERATED_QUOTAS}，请拆小批次"
        )

    required_miss = 1 - probability  # 漏检概率上界
    if k == 0:
        # 整批没有问题封：没有可漏掉的问题，约定发现概率恒为 1，
        # 最低成本即全层 0 封（与 p 无关）。
        return _build_plan(
            names, sizes, costs, tuple(0 for _ in sizes), k, probability,
            tuple(0 for _ in sizes), 0, 1,
        )
    candidates = sorted(
        (
            (sum(c * x for c, x in zip(costs, quota)), quota)
            for quota in product(*(range(n + 1) for n in sizes))
        ),
        key=lambda item: (item[0], item[1]),
    )

    for total_cost, quotas in candidates:
        numerator, worst = _worst_distribution(sizes, quotas, k)
        den = _quota_denominators(sizes, quotas)
        # numerator/den <= required_miss，既约分数精确比较。
        if Fraction(numerator, den) <= required_miss:
            return _build_plan(
                names, sizes, costs, quotas, k, probability,
                worst, numerator, den,
            )

    # 理论上取配额 = (n_1,...,n_m) 时漏检概率恒为 0，永远有解；
    # 走到这里说明校验或枚举有漏洞。
    raise InvalidAuditRequest(  # pragma: no cover - 防御
        "不存在满足要求的配额，请检查参数"
    )


def _build_plan(
    names: Sequence[str],
    sizes: Sequence[int],
    costs: Sequence[int],
    quotas: Sequence[int],
    k: int,
    probability: Fraction,
    worst: Sequence[int],
    numerator: int,
    den: int,
) -> SamplingPlan:
    layers: list[LayerQuota] = []
    for idx, (name, n, x, c, y) in enumerate(
        zip(names, sizes, quotas, costs, worst)
    ):
        # 层内至少查中一封的概率 = 1 − C(n−y,x)/C(n,x)；
        # 空层（n=0）不存在可查的封，概率记 0。
        miss = Fraction(comb(n - y, x), comb(n, x)) if n else Fraction(1)
        layers.append(
            LayerQuota(
                layer=name,
                layer_no=idx + 1,
                n=n,
                quota=x,
                cost=c,
                envelope_nos=tuple(range(1, x + 1)),
                worst_misses=y,
                worst_prob=1 - miss,
            )
        )
    worst_miss = Fraction(numerator, den)
    return SamplingPlan(
        probability=probability,
        problem_count=k,
        total_cost=sum(layer.layer_cost for layer in layers),
        quotas=tuple(quotas),
        layers=tuple(layers),
        worst_misses=tuple(worst),
        worst_miss_probability=worst_miss,
        guarantee=1 - worst_miss,
    )


def plan_for_roster(
    roster,
    costs: dict[str, int] | Sequence[int],
    k: int,
    probability: Fraction,
) -> SamplingPlan:
    """按名册各层封数直接求配额。

    ``costs`` 可给 ``{层名: 每封成本}``，也可按名册层序给序列。
    """
    sizes = [layer.envelope_count for layer in roster.layers]
    names = [layer.layer for layer in roster.layers]
    if isinstance(costs, dict):
        missing = [name for name in names if name not in costs]
        if missing:
            raise InvalidAuditRequest(f"缺少各层成本: {missing}")
        cost_seq = [costs[name] for name in names]
    else:
        cost_seq = list(costs)
    return solve_quota(sizes, cost_seq, k, probability, names=names)
