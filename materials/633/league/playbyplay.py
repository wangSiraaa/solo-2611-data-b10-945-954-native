"""旧场记簿逐球卡：纯函数解释引擎（不依赖任何存储）。

事实模型
========
* 一场已 played 的比赛持有一卷具名逐球卡：卡按具名序号 1..N 排列，每卡正好一球，
  每卡给出一个或两个*可能记入方*（home/away），每个可能记入方下记载一名球员。
* 当版 fixture 决定主客球队，球员名册决定每名球员属于主队还是客队；
  球队不符的卡面选择在建产品时即被排除并留痕，绝不进入解释。
* 检查点在指定卡之后给出累计主客比分；合法解释必须从 0:0 出发逐球走到终分，
  且经过全部检查点。状态空间只按累计比分层，不保存任何完整排列
  （整场最多 30 卡，状态数只受终分约束），因此不会把不同解释上的前后两球拼在一起：
  一条边只在“某合法前缀可达 + 某合法后缀可达”同时成立时才算数。

制胜球与无解释
==============
* 终场平局没有制胜球，不发奖。
* 制胜球 = 最终胜队最后一次与对手打平之后，第一次建立并一直保持领先的那粒球，
  即“最后一次平局状态之后的下一卡”。最后一次平局的可能位置随解释不同而不同，
  制胜卡因此可能不止一张；引擎按层传播“最后平局位置”集合，不穷举排列。
* 无合法解释时给出*首个无合法前缀的卡*；若所有前缀都活着、只有终场对不上，
  标明 ``"final"``。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping, Sequence

HOME = "home"
AWAY = "away"
SIDES = (HOME, AWAY)
FINAL = "final"  # 首个无合法前缀标记：矛盾到终场才暴露


class PlayByPlayError(ValueError):
    """逐球卡/名册/检查点结构非法；调用方应整卷拒绝，不产生版本。"""


@dataclass(frozen=True)
class GoalCard:
    """一张逐球卡：card_no 为具名序号，options 为 side -> 实际记载球员。"""

    card_no: int
    options: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"card_no": self.card_no, "options": dict(self.options)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GoalCard":
        return cls(card_no=int(payload["card_no"]), options=dict(payload["options"]))


@dataclass(frozen=True)
class CheckPoint:
    """第 after_card 球之后必须出现的累计比分 (home, away)。"""

    after_card: int
    home: int
    away: int

    def to_dict(self) -> dict[str, Any]:
        return {"after_card": self.after_card, "home": self.home,
                "away": self.away}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CheckPoint":
        return cls(after_card=int(payload["after_card"]),
                   home=int(payload["home"]), away=int(payload["away"]))


@dataclass
class PlayByPlayProduct:
    """一卷逐球卡冻结后的可重开数据产品（记分员侧）。

    ``cards`` 只保留按当版主客与名册过滤后的合法选择；被排除的原始选择留在
    ``excluded`` 中供审计。终场比分来自冻结事实，不由卡面推断。
    """

    match_id: str
    edition_id: int
    home_team: str
    away_team: str
    roster: Mapping[str, str]            # player_id -> home/away
    final_home: int
    final_away: int
    cards: list[GoalCard]
    checkpoints: list[CheckPoint]
    excluded: list[dict[str, Any]] = field(default_factory=list)
    volume_id: str | None = None
    custodian: str | None = None
    status: str = "played"

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id, "edition_id": self.edition_id,
            "home_team": self.home_team, "away_team": self.away_team,
            "roster": dict(self.roster),
            "final_home": self.final_home, "final_away": self.final_away,
            "cards": [c.to_dict() for c in self.cards],
            "checkpoints": [cp.to_dict() for cp in self.checkpoints],
            "excluded": [dict(x) for x in self.excluded],
            "volume_id": self.volume_id, "custodian": self.custodian,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlayByPlayProduct":
        return cls(
            match_id=payload["match_id"], edition_id=int(payload["edition_id"]),
            home_team=payload["home_team"], away_team=payload["away_team"],
            roster=dict(payload["roster"]),
            final_home=int(payload["final_home"]),
            final_away=int(payload["final_away"]),
            cards=[GoalCard.from_dict(c) for c in payload["cards"]],
            checkpoints=[CheckPoint.from_dict(cp) for cp in payload["checkpoints"]],
            excluded=[dict(x) for x in payload.get("excluded", [])],
            volume_id=payload.get("volume_id"),
            custodian=payload.get("custodian"),
            status=payload.get("status", "played"),
        )


@dataclass
class InterpretationReport:
    """整卷解释结果（奖务桌侧判定全部基于它，不回看活账）。"""

    match_id: str
    feasible: bool
    final_home: int
    final_away: int
    n_cards: int
    first_dead_card: int | str | None     # 卡序号；终场才矛盾为 "final"；全通为 None
    winning_cards: list[int]              # 各合法解释下可能的制胜卡序号（平局为空）
    winner_side: str | None               # home/away；平局或不可解释为 None
    attributions: dict[int, dict[str, list[str]]]  # 卡 -> side -> 合法解释中出现过的球员
    witnesses: list[list[dict[str, Any]]]          # 两条完整合法逐球见证（带累计比分）
    layer_states: list[list[list[int]]]            # 每层可达状态（审计用）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 建产品与校验
def build_product(
    match_id: str,
    edition_id: int,
    home_team: str,
    away_team: str,
    roster: Mapping[str, str],
    final_home: int,
    final_away: int,
    cards: Sequence[Mapping[str, Any]],
    checkpoints: Sequence[Mapping[str, Any]] | None = None,
    *,
    status: str = "played",
    volume_id: str | None = None,
    custodian: str | None = None,
) -> PlayByPlayProduct:
    """校验并按当版主客/名册过滤卡面，构造冻结产品；结构非法抛 PlayByPlayError。"""
    reasons: list[str] = []
    if status not in ("played", "cancelled"):
        raise PlayByPlayError([f"status={status!r} 非法，仅 played/cancelled"])
    if not isinstance(final_home, int) or isinstance(final_home, bool) \
            or not isinstance(final_away, int) or isinstance(final_away, bool) \
            or final_home < 0 or final_away < 0:
        raise PlayByPlayError(["终场比分必须是非负整数"])
    side_of_team = {HOME: home_team, AWAY: away_team}
    player_side: dict[str, str] = {}
    for player, side in roster.items():
        if side not in SIDES:
            reasons.append(f"名册球员 {player!r} 的归属 {side!r} 不是 home/away")
            continue
        if player in player_side:
            reasons.append(f"名册球员 {player!r} 重复登记")
        player_side[player] = side

    parsed: list[GoalCard] = []
    excluded: list[dict[str, Any]] = []
    seen_nos: set[int] = set()
    for i, raw in enumerate(cards):
        no = raw.get("card_no")
        opts = raw.get("options")
        if not isinstance(no, int) or isinstance(no, bool) or no < 1:
            reasons.append(f"cards[{i}] card_no={no!r} 必须是正整数")
            continue
        if no in seen_nos:
            reasons.append(f"cards[{i}] card_no={no} 序号重复")
        seen_nos.add(no)
        if not isinstance(opts, Mapping) or not (1 <= len(opts) <= 2):
            reasons.append(f"card {no}: 每卡必须有 1 或 2 个可能记入方")
            continue
        kept: dict[str, str] = {}
        for side, player in opts.items():
            if side not in SIDES:
                reasons.append(f"card {no}: 记入方 {side!r} 不是 home/away")
                continue
            if not isinstance(player, str) or not player:
                reasons.append(f"card {no}: {side} 记载球员缺失或非字符串")
                continue
            if player not in player_side:
                reasons.append(f"card {no}: 球员 {player!r} 不在当版名册内")
                continue
            if player_side[player] != side:
                excluded.append({
                    "card_no": no, "side": side, "player": player,
                    "reason": f"球员 {player} 隶属"
                              f"{side_of_team[player_side[player]]}，与 {side} 方不符",
                })
                continue
            kept[side] = player
        if not kept:
            reasons.append(f"card {no}: 过滤后无任何合法记入方，该卡无法解释")
        parsed.append(GoalCard(card_no=no, options=kept))

    n = len(parsed)
    cps: list[CheckPoint] = []
    cp_seen: set[int] = set()
    for i, raw in enumerate(checkpoints or []):
        after, ch, ca = raw.get("after_card"), raw.get("home"), raw.get("away")
        if not isinstance(after, int) or isinstance(after, bool) \
                or not (1 <= after <= n):
            reasons.append(f"checkpoints[{i}] after_card={after!r} 须落在 1..{n}")
            continue
        if after in cp_seen:
            reasons.append(f"checkpoints[{i}] after_card={after} 重复")
        cp_seen.add(after)
        for name, val in (("home", ch), ("away", ca)):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                reasons.append(f"checkpoints[{i}] {name}={val!r} 必须是非负整数")
        if isinstance(ch, int) and isinstance(ca, int) and ch + ca > after:
            reasons.append(f"checkpoints[{i}] 第 {after} 球后累计 {ch}:{ca}"
                           f"，进球数 {ch + ca} 超过已发生球数")
        cps.append(CheckPoint(after, ch, ca))

    if reasons:
        raise PlayByPlayError(reasons)
    parsed.sort(key=lambda c: c.card_no)
    if [c.card_no for c in parsed] != list(range(1, n + 1)):
        raise PlayByPlayError([f"卡序号必须连续覆盖 1..{n}，实际 {sorted(seen_nos)}"])
    cps.sort(key=lambda cp: cp.after_card)
    return PlayByPlayProduct(
        match_id=match_id, edition_id=edition_id,
        home_team=home_team, away_team=away_team, roster=dict(player_side),
        final_home=final_home, final_away=final_away,
        cards=parsed, checkpoints=cps, excluded=excluded,
        volume_id=volume_id, custodian=custodian, status=status,
    )


# ---------------------------------------------------------------- 分层解释
def _delta(side: str) -> tuple[int, int]:
    return (1, 0) if side == HOME else (0, 1)


def explain(product: PlayByPlayProduct) -> InterpretationReport:
    """对冻结产品做整卷解释（分层状态图 + 前后缀可达性，不保存完整排列）。"""
    cards = sorted(product.cards, key=lambda c: c.card_no)
    n = len(cards)
    fh, fa = product.final_home, product.final_away
    cp_at = {cp.after_card: (cp.home, cp.away) for cp in product.checkpoints}

    # parents[k][state] = {prev_state: (side, player)}；layer 0 只有 0:0。
    # 分层状态图保留全部转移（不按检查点/终分剪枝），检查点合法性完全交给
    # 前向 prefix_ok、终分可达性完全交给后向 can_finish，两者互不污染，
    # 才能区分“首个无合法前缀的卡”与“矛盾到终场才暴露”。
    layers: list[dict[tuple[int, int], dict[tuple[int, int], tuple[str, str]]]] = \
        [{(0, 0): {}}]
    for k, card in enumerate(cards, start=1):
        prev_layer = layers[k - 1]
        cur: dict[tuple[int, int], dict[tuple[int, int], tuple[str, str]]] = {}
        for state in prev_layer:
            for side, player in card.options.items():
                dh, da = _delta(side)
                nxt = (state[0] + dh, state[1] + da)
                cur.setdefault(nxt, {})[state] = (side, player)
        layers.append(cur)

    final_state = (fh, fa)

    # 前缀合法性（前向）：从 0:0 出发、逐卡经过全部检查点的状态才算合法前缀。
    # 第一张没有任何合法前缀状态的卡即“首个无合法前缀的卡”；这与终分能否达到无关。
    prefix_ok: list[set[tuple[int, int]]] = [{(0, 0)}]
    first_dead: int | str | None = None
    for k in range(1, n + 1):
        ok: set[tuple[int, int]] = set()
        for state, edges in layers[k].items():
            if k in cp_at and state != cp_at[k]:
                continue  # 检查点是硬约束：本层只有该累计比分可作合法前缀
            if any(prev in prefix_ok[k - 1] for prev in edges):
                ok.add(state)
        prefix_ok.append(ok)
        if not ok and first_dead is None:
            first_dead = k

    # 终分可达性（后向）：从终分沿前驱传播。合法子图 = 前缀合法（prefix_ok）
    # 且能走到终分（can_finish）的交集，因此反向传播只允许落进 prefix_ok 的
    # 前驱，检查点在两个方向上都不会被绕过。终分上界同样自然生效：越界状态
    # 从不在通向终分的路径上。
    can_finish: list[set[tuple[int, int]]] = [set() for _ in range(n + 1)]
    if first_dead is None and final_state in prefix_ok[n]:
        can_finish[n] = {final_state}
        for k in range(n, 0, -1):
            for state, edges in layers[k].items():
                if state not in can_finish[k]:
                    continue
                for prev_state in edges:
                    if prev_state in prefix_ok[k - 1]:
                        can_finish[k - 1].add(prev_state)

    if first_dead is None and final_state not in can_finish[n]:
        first_dead = FINAL
    feasible = first_dead is None

    winner_side: str | None = None
    winning_cards: list[int] = []
    attributions: dict[int, dict[str, list[str]]] = {
        c.card_no: {HOME: [], AWAY: []} for c in cards
    }
    witnesses: list[list[dict[str, Any]]] = []

    if feasible:
        winner_side = HOME if fh > fa else AWAY if fa > fh else None
        # 合法边 = 前缀可达且后缀可达；按此汇总每卡合法记入方/球员，无需穷举排列。
        for k, card in enumerate(cards, start=1):
            found: dict[str, set[str]] = {HOME: set(), AWAY: set()}
            for state, edges in layers[k].items():
                if state not in can_finish[k]:
                    continue
                for prev_state, (side, player) in edges.items():
                    if prev_state in can_finish[k - 1]:
                        found[side].add(player)
            attributions[card.card_no] = {s: sorted(p) for s, p in found.items()}

        if winner_side is not None:
            # 传播每个可达状态上“最后一次平局位置”的集合（0 表示开场 0:0）。
            last_tie: list[dict[tuple[int, int], set[int]]] = [{(0, 0): {0}}]
            for k in range(1, n + 1):
                cur_ties: dict[tuple[int, int], set[int]] = {}
                for state, edges in layers[k].items():
                    if state not in can_finish[k]:
                        continue
                    bag: set[int] = set()
                    for prev_state in edges:
                        if prev_state not in can_finish[k - 1]:
                            continue
                        bag |= last_tie[k - 1].get(prev_state, set())
                    if state[0] == state[1]:
                        bag = {k}  # 当前即平局，最后平局位置确定为本层
                    cur_ties[state] = bag
                last_tie.append(cur_ties)
            winning_cards = sorted(t + 1 for t in last_tie[n][final_state])

            # 见证用前驱指针线性重建：trace[k][(state, tie_pos)] 指向一个
            # (prev_state, prev_tie_pos, side, player)，按制胜卡（=最后平局+1）
            # 各取一条完整路径，绝不枚举排列。
            trace = _build_trace(layers, can_finish, cards)
            for target in winning_cards[:2]:
                witness = _trace_witness(trace, cards, final_state, target - 1)
                if witness is not None:
                    witnesses.append(witness)
        else:
            if n == 0:
                # 0:0 且无卡：没有任何逐球见证可给，平局无奖本身已由 winner_side=None 表达。
                pass
            else:
                trace = _build_trace(layers, can_finish, cards)
                # 平局没有制胜卡概念，任取一条完整合法路径作见证（tie_pos 任意可达）。
                final_keys = [key for key in trace[n] if key[0] == final_state]
                if final_keys:
                    witnesses.append(_trace_witness(trace, cards, final_state,
                                                    final_keys[0][1]))

    return InterpretationReport(
        match_id=product.match_id, feasible=feasible,
        final_home=fh, final_away=fa, n_cards=n,
        first_dead_card=first_dead, winning_cards=winning_cards,
        winner_side=winner_side, attributions=attributions,
        witnesses=witnesses,
        layer_states=[[list(s) for s in layer] for layer in layers],
    )


def _build_trace(
    layers: Sequence[Mapping[tuple[int, int], Mapping[tuple[int, int], tuple[str, str]]]],
    can_finish: Sequence[set[tuple[int, int]]],
    cards: Sequence[GoalCard],
) -> list[dict[tuple[tuple[int, int], int],
               tuple[tuple[int, int], int, str, str] | None]]:
    """在合法子图（前缀可达且后缀可达）上按 (状态, 最后平局位置) 建一个前驱指针。

    键 ``(state, tie_pos)`` 里 tie_pos 是走到 state 时最后一次平局的卡序号
    （0 表示开场 0:0）；值为 ``(prev_state, prev_tie_pos, side, player)``，
    第 0 层根节点值为 None。表大小只随层数与状态数多项式增长，与排列数无关。
    """
    n = len(cards)
    trace: list[dict[tuple[tuple[int, int], int],
                     tuple[tuple[int, int], int, str, str] | None]] = \
        [{((0, 0), 0): None}]
    for k, card in enumerate(cards, start=1):
        cur: dict[tuple[tuple[int, int], int],
                  tuple[tuple[int, int], int, str, str] | None] = {}
        # prev_ties[state] = 该前驱状态在第 k-1 层携带的全部最后平局位置。
        prev_ties: dict[tuple[int, int], list[int]] = {}
        for (st, tie_pos) in trace[k - 1]:
            prev_ties.setdefault(st, []).append(tie_pos)
        for state, edges in layers[k].items():
            if state not in can_finish[k]:
                continue
            tie_here = k if state[0] == state[1] else None
            for prev_state, (side, player) in edges.items():
                if prev_state not in can_finish[k - 1]:
                    continue
                if tie_here is not None:
                    # 当前状态是平局：本节点最后平局位置重置为 k；入边指针仍携带
                    # 前驱自己的平局位置，回溯到前驱后才按前驱键继续。
                    if (state, tie_here) not in cur:
                        prev_tie = prev_ties[prev_state][0]
                        cur[(state, tie_here)] = \
                            (prev_state, prev_tie, side, player)
                else:
                    # 非平局：前驱携带的每个最后平局位置各自传播，各留一个指针。
                    for prev_tie in prev_ties.get(prev_state, ()):
                        key = (state, prev_tie)
                        if key not in cur:
                            cur[key] = (prev_state, prev_tie, side, player)
        trace.append(cur)
    return trace


def _trace_witness(
    trace: Sequence[Mapping[tuple[tuple[int, int], int],
                            tuple[tuple[int, int], int, str, str] | None]],
    cards: Sequence[GoalCard],
    final_state: tuple[int, int],
    last_tie_pos: int,
) -> list[dict[str, Any]] | None:
    """沿前驱指针线性回溯一条完整合法路径；其制胜卡为 last_tie_pos+1。"""
    n = len(cards)
    key = (final_state, last_tie_pos)
    if key not in trace[n]:
        return None
    reversed_choices: list[tuple[str, str]] = []
    for k in range(n, 0, -1):
        pointer = trace[k][key]
        if pointer is None:  # pragma: no cover - 根在第 0 层，不会在 k>=1 命中
            return None
        prev_state, prev_tie, side, player = pointer
        reversed_choices.append((side, player))
        key = (prev_state, prev_tie)
    if key != ((0, 0), 0):
        return None  # 未回到开场 0:0 即非完整前缀，防御性检查
    choices = list(reversed(reversed_choices))
    out: list[dict[str, Any]] = []
    h = a = 0
    for k, (side, player) in enumerate(choices, start=1):
        dh, da = _delta(side)
        h, a = h + dh, a + da
        out.append({"card_no": k, "side": side, "player": player,
                    "score_after": [h, a]})
    return out


# ---------------------------------------------------------------- 独立核对产品
def possible_attributions(report: InterpretationReport, card_no: int,
                          ) -> dict[str, list[str]]:
    """独立核某卡：在所有合法解释中该卡可能的记入方与实际记载球员。"""
    if card_no not in report.attributions:
        raise KeyError(f"卡 {card_no} 不存在（共 {report.n_cards} 张）")
    if not report.feasible:
        return {HOME: [], AWAY: []}
    return {s: list(p) for s, p in report.attributions[card_no].items() if p}


def can_cooccur(
    product: PlayByPlayProduct,
    report: InterpretationReport,
    pins: Sequence[tuple[int, str] | Mapping[str, Any]],
) -> bool:
    """核若干指定前后段能否出现在*同一条*真实记录里。

    每个钉选为 ``(card_no, side)`` 或 ``{"card_no", "side", "player"?}``；
    不同解释上的球不得拼接，因此所有钉选必须在同一条 0:0→终分路径上同时成立。
    """
    if not report.feasible:
        return False
    wanted_pins: dict[int, tuple[str, str | None]] = {}
    for item in pins:
        if isinstance(item, Mapping):
            no, side = int(item["card_no"]), item["side"]
            player = item.get("player")
        else:
            no, side = int(item[0]), item[1]
            player = None
        if side not in SIDES or not (1 <= no <= report.n_cards):
            return False
        if no in wanted_pins and wanted_pins[no] != (side, player):
            return False
        wanted_pins[no] = (side, player)

    cards = sorted(product.cards, key=lambda c: c.card_no)
    cp_at = {cp.after_card: (cp.home, cp.away) for cp in product.checkpoints}

    # 分层可达 DP：每层只保留累计比分状态，钉选约束在选边时生效，
    # 不保存完整排列，因此“同一真实记录”由状态图的连通性而非拼接给出。
    reachable: set[tuple[int, int]] = {(0, 0)}
    for k, card in enumerate(cards, start=1):
        wanted = wanted_pins.get(k)
        nxt_reachable: set[tuple[int, int]] = set()
        for state in reachable:
            sides = [wanted[0]] if wanted is not None else list(card.options)
            for side in sides:
                if side not in card.options:
                    continue
                player = card.options[side]
                if wanted is not None and wanted[1] is not None and wanted[1] != player:
                    continue
                dh, da = _delta(side)
                nxt = (state[0] + dh, state[1] + da)
                if nxt[0] > product.final_home or nxt[1] > product.final_away:
                    continue
                if k in cp_at and nxt != cp_at[k]:
                    continue
                nxt_reachable.add(nxt)
        reachable = nxt_reachable
        if not reachable:
            return False

    return (product.final_home, product.final_away) in reachable


def possible_recipients(
    product: PlayByPlayProduct,
    report: InterpretationReport,
    registry: Mapping[str, str] | None,
    kind: str,
) -> list[str | None]:
    """按奖项登记汇总所有合法解释的收件者；``None`` 表示该解释无人能领。

    kind="player" 时 registry 被忽略，收件者即制胜卡记载球员；
    kind="station" 时按奖方自己的培训站注册簿 player_id -> station_id 归并，
    未登记球员产生 ``None``（无人能领）。
    """
    if kind not in ("player", "station"):
        raise PlayByPlayError([f"kind={kind!r} 非法，仅 player/station"])
    if not report.feasible or report.winner_side is None:
        return []
    side = report.winner_side
    recipients: list[str | None] = []
    for card_no in report.winning_cards:
        card = next(c for c in product.cards if c.card_no == card_no)
        player = card.options.get(side)
        if player is None:
            continue
        if kind == "player":
            recipients.append(player)
        else:
            recipients.append(None if registry is None else registry.get(player))
    # 去重但稳定排序；None 始终在末尾，语义为“无人能领”。
    concrete = sorted({r for r in recipients if r is not None})
    return concrete + ([None] if None in recipients else [])
