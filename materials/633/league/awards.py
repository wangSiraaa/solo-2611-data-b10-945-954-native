"""奖务桌：制胜球收件者判定、立即发证/待核听审与排庭。

本模块只消费冻结的逐球产品（:class:`~league.playbyplay.PlayByPlayProduct`）
与其解释报告，不回看任何活账；关闭成绩库后，同一份产品配合不同奖务登记
（球员本人 / 培训站注册簿）即可独立重算所有发证与排庭结论。

发证规则
========
* 终场平局或整卷不可解释：无制胜球，不产生奖。
* 制胜球 = 胜队最后一次与对手打平之后第一次建立并一直保持领先的那粒球；
  不同合法解释下制胜卡可能不同，每粒制胜卡按卡面记载的球员产生一个收件者。
* 球员奖收件者即球员本人；培训站奖按奖方*自己的注册簿*把球员归并到培训站，
  未登记的球员该解释为“无人能领”（``None``）。
* 所有合法解释最终给同一具体收件者 => 立即发一份奖证；
  存在多个具体收件者 => 待核，开听审案，附两条足以说明分歧的完整逐球见证；
  全部无人能领 => 不颁；有人与无人并存 => 待核（无人能领没有代理人）。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping

from .playbyplay import (
    PlayByPlayProduct,
    explain, possible_recipients,
)

PLAYER = "player"
STATION = "station"

# 决定状态机
ISSUE_NOW = "issue"          # 所有解释同一具体收件者，立即发一份奖证
PENDING_HEARING = "pending"  # 有真实收件争议（或有人/无人并存），开听审案
NO_AWARD = "none"            # 平局/不可解释，或全部解释无人能领


class AwardError(ValueError):
    """奖项登记或请求结构非法。"""


@dataclass
class AwardDecision:
    """一次奖项请求的完整判定（可持久化、可关闭重开复算）。"""

    award_id: str
    match_id: str
    kind: str                       # player / station
    decision: str                   # issue / pending / none
    recipients: list[str | None]    # 去重后的全部可能收件者（None=无人能领）
    recipient: str | None           # decision=issue 时的唯一收件者
    winning_cards: list[int]
    winner_side: str | None
    feasible: bool
    first_dead_card: int | str | None
    hearing_case_id: str | None
    witnesses: list[list[dict[str, Any]]]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HearingCase:
    """待核奖对应的真实收件争议听审案。

    ``volume_id`` 是该场一卷具名原件，``custodian`` 是这卷的实际保管人，
    听审必须传该保管人出席。``possible_recipients`` 为有真实分歧的收件者集合
    （无人能领不产生代理人，不进此集合，但保留在 decision 里）。
    """

    case_id: str
    award_id: str
    match_id: str
    kind: str
    volume_id: str
    custodian: str
    possible_recipients: list[str]
    witnesses: list[list[dict[str, Any]]]
    conflict_parties: list[str]     # 主席回避清单需要命中的人员/培训站

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_award(
    award_id: str,
    product: PlayByPlayProduct,
    kind: str,
    station_registry: Mapping[str, str] | None = None,
) -> AwardDecision:
    """从冻结产品与奖务登记判定一个奖项；不产生任何写入。"""
    if kind not in (PLAYER, STATION):
        raise AwardError(f"kind={kind!r} 非法，仅 player/station")
    report = explain(product)
    base = {
        "award_id": award_id, "match_id": product.match_id, "kind": kind,
        "winning_cards": list(report.winning_cards),
        "winner_side": report.winner_side, "feasible": report.feasible,
        "first_dead_card": report.first_dead_card,
    }

    # 取消场、不可解释、平局：没有制胜球，不出奖凭单。
    if product.status == "cancelled" or not report.feasible \
            or report.winner_side is None:
        note = ("取消场不出奖凭单" if product.status == "cancelled"
                else "整卷无合法解释，不产生奖" if not report.feasible
                else "终场平局无制胜球，不发奖")
        return AwardDecision(
            decision=NO_AWARD, recipients=[], recipient=None,
            hearing_case_id=None, witnesses=[], note=note, **base)

    registry = dict(station_registry) if (kind == STATION and station_registry) else None
    recipients = possible_recipients(product, report, registry, kind)
    concrete = [r for r in recipients if r is not None]
    has_none = None in recipients

    if not concrete:
        # 所有合法解释都无人能领（典型：培训站奖但球员均未登记）=> 不颁。
        return AwardDecision(
            decision=NO_AWARD, recipients=[None], recipient=None,
            hearing_case_id=None, witnesses=[],
            note="全部合法解释均无培训站登记，无人能领，不颁", **base)

    if len(concrete) == 1 and not has_none:
        # 所有合法解释收敛到同一具体收件者 => 即使制胜卡仍有歧义也立即发证。
        return AwardDecision(
            decision=ISSUE_NOW, recipients=concrete, recipient=concrete[0],
            hearing_case_id=None, witnesses=[],
            note="所有合法解释给同一收件者，立即发证", **base)

    # 多个具体收件者，或有人/无人并存 => 待核，开真实收件争议听审案。
    case_id = f"case-{award_id}"
    conflict_parties = sorted(set(concrete))
    return AwardDecision(
        decision=PENDING_HEARING, recipients=recipients, recipient=None,
        hearing_case_id=case_id,
        witnesses=[list(w) for w in report.witnesses],
        note=("多个具体收件者存在真实争议" if len(concrete) > 1
              else "有人能领与无人能领并存，待核"),
        **base)


def build_hearing_case(decision: AwardDecision,
                       product: PlayByPlayProduct) -> HearingCase:
    """把待核决定具体化为听审案；非待核决定调用即报错。"""
    if decision.decision != PENDING_HEARING:
        raise AwardError(
            f"奖项 {decision.award_id} 决定为 {decision.decision}，无需开听审案")
    if not product.volume_id or not product.custodian:
        raise AwardError(
            f"场次 {product.match_id} 缺少具名原件卷号/实际保管人，无法传卷")
    concrete = sorted({r for r in decision.recipients if r is not None})
    if len(concrete) < 2 and None not in decision.recipients:
        raise AwardError("听审案必须有真实收件争议")
    return HearingCase(
        case_id=decision.hearing_case_id, award_id=decision.award_id,
        match_id=product.match_id, kind=decision.kind,
        volume_id=product.volume_id, custodian=product.custodian,
        possible_recipients=concrete,
        witnesses=[list(w) for w in decision.witnesses],
        conflict_parties=concrete,
    )
