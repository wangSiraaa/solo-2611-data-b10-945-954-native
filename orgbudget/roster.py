"""封样名册的公共模型。

内审把**同一张原票装成一封**：一枚具名纸票（``ticket_id``）在同一份
名册里始终对应恰好一个封；封再按其全部实付部门相对组织根的归属归层。

* 某张原票的实付部门全部落在根的同一个直接分部之下 => 归该分部层；
* 跨过两个及以上直接分部 => 归“联合”层（``JOINT_LAYER``）。

本模块只放纯数据模型与确定性摘要，不接触 SQLite，供预算仓与独立审计
共用，保证双方对“层 / 依据 / 摘要”的解释完全一致。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .errors import InvalidTree

#: 跨分部原票统一归入的层名。
JOINT_LAYER = "联合"


@dataclass(frozen=True)
class TicketAssignment:
    """一条实付事件当前登记到的具名纸票。"""

    idem_key: str
    ticket_id: str
    request_no: str
    dept_id: str
    amount: int
    assigned_at: str
    note: Optional[str] = None


@dataclass(frozen=True)
class RosterPayment:
    """名册内的一条实际支付（只可能是已入账的 spend 事件）。"""

    idem_key: str
    request_no: str
    dept_id: str
    amount: int


@dataclass(frozen=True)
class RosterEnvelope:
    """一封 = 一张具名原票及其全部实付明细。

    ``envelope_no`` 是**层内**封号（从 1 开始），便于按层独立抽封；
    ``layer_no`` / ``layer`` 标明归层，``basis`` 给出归层依据。
    """

    layer: str
    layer_no: int
    envelope_no: int
    ticket_id: str
    basis: str
    payments: tuple[RosterPayment, ...]

    @property
    def payment_count(self) -> int:
        return len(self.payments)


@dataclass(frozen=True)
class RosterLayer:
    """一层：分部层（以根的直接分部 ID 命名）或“联合”层。"""

    layer: str
    layer_no: int
    envelope_count: int
    envelopes: tuple[RosterEnvelope, ...]

    @property
    def payment_count(self) -> int:
        return sum(e.payment_count for e in self.envelopes)


@dataclass(frozen=True)
class Roster:
    """一次冻结签发的完整名册快照。

    名册一经签发即不可变：之后勘误归票、迁移实付部门或新增支出，都只会
    在**下一次冻结**生成新名册；旧名册可用 ``roster_id`` 原样重开、复点。
    """

    roster_id: int
    org_version: int
    created_at: str
    cut_digest: str
    spend_count: int
    layers: tuple[RosterLayer, ...]
    note: Optional[str] = None

    @property
    def envelope_count(self) -> int:
        return sum(layer.envelope_count for layer in self.layers)

    @property
    def payment_count(self) -> int:
        return sum(layer.payment_count for layer in self.layers)

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(layer.layer for layer in self.layers)

    def layer(self, name: str) -> RosterLayer:
        for layer in self.layers:
            if layer.layer == name:
                return layer
        raise KeyError(name)

    def layer_sizes(self) -> tuple[tuple[str, int], ...]:
        """按名册层序给出 ``(层名, 封数)``，可直接喂给抽样求解器。"""
        return tuple((layer.layer, layer.envelope_count) for layer in self.layers)

    def envelopes(self) -> tuple[RosterEnvelope, ...]:
        return tuple(
            envelope
            for layer in self.layers
            for envelope in layer.envelopes
        )


def cut_digest(payments: Iterable[RosterPayment]) -> str:
    """对“本次冻结真正包含的实付集合”做确定性摘要。

    排序后按 ``幂等键 / 请求号 / 部门 / 金额`` 逐行编码，与服务重启、
    行序变化无关；独立复点时由名册自己的支付行重算即可核对。
    """
    rows = sorted(
        (p.idem_key, p.request_no, p.dept_id, p.amount) for p in payments
    )
    hasher = hashlib.sha256()
    hasher.update(f"{len(rows)}\n".encode("utf-8"))
    for idem_key, request_no, dept_id, amount in rows:
        hasher.update(
            f"{idem_key}\t{request_no}\t{dept_id}\t{amount}\n".encode("utf-8")
        )
    return hasher.hexdigest()


def classify_layer(product, dept_ids: Iterable[str]) -> tuple[str, str]:
    """按原票实付部门归层，返回 ``(层名, 归层依据)``。

    只沿传入的**已发布组织产品**计算：每个部门先找到它在根下的直接分部，
    若所有部门同属一个直接分部则归该分部层，否则归联合层。部门不属于该
    产品时报 ``InvalidTree``（冻结必须绑定一个真实、完整的组织版本）。
    """
    ids = sorted(set(dept_ids))
    if not ids:
        raise InvalidTree("没有实付部门的原票不能入封")
    branches: set[str] = set()
    for dept_id in ids:
        if not product.contains(dept_id):
            raise InvalidTree(
                f"实付部门 {dept_id} 不在组织产品 v{product.version} 中，不能入封"
            )
        branches.add(product.direct_branch(dept_id))
    ordered = sorted(branches)
    if len(ordered) == 1:
        return ordered[0], f"分部 {ordered[0]}"
    return JOINT_LAYER, "跨分部: " + " + ".join(ordered)
