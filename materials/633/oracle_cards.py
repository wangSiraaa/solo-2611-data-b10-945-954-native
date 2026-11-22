"""逐球/制胜球/收件者的独立 oracle：直白枚举，与产品代码不共享实现。

与 ``league/playbyplay.py`` 的分层 DP 不同，这里在小域上用最朴素的乘积枚举
逐卡展开*全部*完整球序（最多 30 卡、每卡至多 2 选），再独立过滤检查点与终分、
独立重算制胜卡与收件者集合，用于黑盒双向对照：

* oracle 判为合法的每条球序，产品的层状态图必须允许；
* 产品给出的制胜卡/收件者/可共现结论，必须与枚举结果一致；
* 无解释时 oracle 独立给出首个无合法前缀的卡或终场矛盾。

枚举规模在半场小例上完全可控；整场 30 卡全开时 2^30 过大，因此 oracle 只在
测试构造的小卡卷（<= 12 卡）上调用，产品侧的多项式实现负责全规模。
"""

from __future__ import annotations

from itertools import product
from typing import Any, Mapping, Sequence

from league.playbyplay import (
    PlayByPlayProduct, GoalCard, HOME, AWAY,
)


def _all_sequences(cards: Sequence[GoalCard]):
    """笛卡尔积枚举全部完整球序：每元素 (side, player)。"""
    option_lists = [list(card.options.items()) for card in cards]
    for choices in product(*option_lists):
        yield choices


def _scores(choices: Sequence[tuple[str, str]]) -> list[tuple[int, int]]:
    h = a = 0
    out: list[tuple[int, int]] = []
    for side, _player in choices:
        if side == HOME:
            h += 1
        else:
            a += 1
        out.append((h, a))
    return out


def oracle_legal_paths(product: PlayByPlayProduct) -> list[list[dict[str, Any]]]:
    """枚举所有从 0:0 走到终分、经过全部检查点的完整合法球序。"""
    cards = sorted(product.cards, key=lambda c: c.card_no)
    cp = {c.after_card: (c.home, c.away) for c in product.checkpoints}
    legal: list[list[dict[str, Any]]] = []
    for choices in _all_sequences(cards):
        scores = _scores(choices)
        if scores[-1] != (product.final_home, product.final_away):
            continue
        if any(scores[after - 1] != point for after, point in cp.items()):
            continue
        legal.append([
            {"card_no": k, "side": side, "player": player,
             "score_after": list(scores[k - 1])}
            for k, (side, player) in enumerate(choices, start=1)
        ])
    return legal


def oracle_first_dead(product: PlayByPlayProduct) -> int | str | None:
    """独立定位首个无合法前缀的卡；终场才矛盾返回 'final'，全通返回 None。"""
    cards = sorted(product.cards, key=lambda c: c.card_no)
    cp = {c.after_card: (c.home, c.away) for c in product.checkpoints}
    reachable: set[tuple[int, int]] = {(0, 0)}
    for k, card in enumerate(cards, start=1):
        nxt: set[tuple[int, int]] = set()
        for (h, a) in reachable:
            for side in card.options:
                ns = (h + 1, a) if side == HOME else (h, a + 1)
                if k in cp and ns != cp[k]:
                    continue
                nxt.add(ns)
        if not nxt:
            return k
        reachable = nxt
    if (product.final_home, product.final_away) not in reachable:
        return "final"
    return None


def _winning_card(path: Sequence[Mapping[str, Any]], winner_side: str) -> int:
    last_tie = 0
    for step in path:
        h, a = step["score_after"]
        if h == a:
            last_tie = step["card_no"]
    return last_tie + 1


def oracle_winning_cards(product: PlayByPlayProduct) -> list[int]:
    if product.final_home == product.final_away:
        return []
    winner_side = HOME if product.final_home > product.final_away else AWAY
    return sorted({_winning_card(p, winner_side) for p in oracle_legal_paths(product)})


def oracle_recipients(product: PlayByPlayProduct,
                      registry: Mapping[str, str] | None,
                      kind: str) -> list[str | None]:
    legal = oracle_legal_paths(product)
    if not legal or product.final_home == product.final_away:
        return []
    winner_side = HOME if product.final_home > product.final_away else AWAY
    out: list[str | None] = []
    for path in legal:
        wc = _winning_card(path, winner_side)
        step = next(s for s in path if s["card_no"] == wc)
        if kind == "player":
            out.append(step["player"])
        else:
            out.append(None if registry is None else registry.get(step["player"]))
    concrete = sorted({r for r in out if r is not None})
    return concrete + ([None] if None in out else [])


def oracle_attributions(product: PlayByPlayProduct
                        ) -> dict[int, dict[str, list[str]]]:
    legal = oracle_legal_paths(product)
    cards = sorted(product.cards, key=lambda c: c.card_no)
    attr: dict[int, dict[str, set[str]]] = {
        c.card_no: {HOME: set(), AWAY: set()} for c in cards}
    for path in legal:
        for step in path:
            attr[step["card_no"]][step["side"]].add(step["player"])
    return {no: {s: sorted(p) for s, p in sides.items() if p}
            for no, sides in attr.items()}


def oracle_can_cooccur(product: PlayByPlayProduct,
                       pins: Mapping[int, str]) -> bool:
    """pins: card_no -> side；枚举中是否存在一条球序同时满足全部钉选。"""
    legal = oracle_legal_paths(product)
    for path in legal:
        if all(next(s for s in path if s["card_no"] == no)["side"] == side
               for no, side in pins.items()):
            return True
    return False
