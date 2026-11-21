"""合同验收、退货与逐合同应付单服务。

本服务与预算执行仓物理分离（独立 SQLite 文件），只通过 :class:`BudgetService`
的**只读查询**衔接旧库：

* 一笔新合同只绑定一个真实采购承诺（``commitment`` 行），合同总价即承诺额；
* 验收单凭单号登记到货数量，退货必须指向已验收的具体批次，累退不得超该批；
* 应付单按切面净验收件数重算 ``floor(总价 × q / N)``，再减去旧库已经实际
  支付给该承诺的款——旧库实付即使此前登记过退货也只扣一次；
* 负数标追回额、不得作为可付单；承诺已取消则阻塞，不能再开支付单。

本服务**绝不代替旧库登记实付**：对预算仓的全部访问都是只读的。
应付单每次重算都会留存一个不可变版本（可重开、可回溯），版本内说明
所使用的交货、退货、旧库实付事件与预算 / 组织产品版本。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .errors import (
    ContractConflict,
    ContractNotFound,
    IdempotencyConflict,
    InvalidContract,
    PayableError,
    ReceiptError,
    ReturnError,
)

if TYPE_CHECKING:
    from .budget_service import BudgetService

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contract (
    contract_id     TEXT PRIMARY KEY,
    request_no      TEXT NOT NULL UNIQUE,
    supplier_id     TEXT NOT NULL,
    total_qty       INTEGER NOT NULL CHECK (total_qty > 0),
    total_price     INTEGER NOT NULL CHECK (total_price > 0),
    earliest_day    INTEGER NOT NULL CHECK (earliest_day >= 1),
    latest_day      INTEGER NOT NULL CHECK (latest_day >= earliest_day),
    preferred_day   INTEGER NOT NULL CHECK (preferred_day >= earliest_day
                                            AND preferred_day <= latest_day),
    late_fee        INTEGER NOT NULL CHECK (late_fee >= 0),
    created_at      TEXT NOT NULL
);
-- 验收批次：单号全局唯一，同一合同累计到货不得超过总件数。
CREATE TABLE IF NOT EXISTS receipt (
    receipt_no   TEXT PRIMARY KEY,
    contract_id  TEXT NOT NULL REFERENCES contract(contract_id),
    seq          INTEGER NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipt_contract ON receipt(contract_id);
-- 退货仅追加：必须指向已验收批次，同一批次累退不得超过其验收数量。
CREATE TABLE IF NOT EXISTS goods_return (
    return_no    TEXT PRIMARY KEY,
    contract_id  TEXT NOT NULL REFERENCES contract(contract_id),
    receipt_no   TEXT NOT NULL REFERENCES receipt(receipt_no),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_return_contract ON goods_return(contract_id);
-- 应付单不可变版本：完整事实快照以 JSON 留存，重开只追加新版本。
CREATE TABLE IF NOT EXISTS payable_statement (
    version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id  TEXT NOT NULL REFERENCES contract(contract_id),
    version_no   INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (contract_id, version_no)
);
"""

# 应付单状态。
PAYABLE = "PAYABLE"                    # 有正数应付款，可开支付单
ZERO_DUE = "ZERO_DUE"                  # 净额为 0，无需付款
RECOVERY = "RECOVERY"                  # 净额为负，标追回额，不得作为可付单
BLOCKED_CANCELLED = "BLOCKED_CANCELLED"  # 承诺已取消，不能再开支付单


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InvalidContract(f"{name} 必须是正整数: {value!r}")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidContract(f"{name} 必须是非负整数: {value!r}")
    return value


@dataclass(frozen=True)
class Contract:
    contract_id: str
    request_no: str
    supplier_id: str
    total_qty: int
    total_price: int
    earliest_day: int
    latest_day: int
    preferred_day: int
    late_fee: int
    created_at: str


@dataclass(frozen=True)
class Receipt:
    receipt_no: str
    contract_id: str
    seq: int
    qty: int
    created_at: str


@dataclass(frozen=True)
class GoodsReturn:
    return_no: str
    contract_id: str
    receipt_no: str
    qty: int
    created_at: str


@dataclass(frozen=True)
class ReceiptNet:
    """单个验收批次的交货、累退与净验收。"""

    receipt_no: str
    seq: int
    gross_qty: int
    returned_qty: int
    net_qty: int


@dataclass(frozen=True)
class SpendFact:
    """旧库一笔实际支出事件（只读快照，本服务不代登记）。"""

    request_no: str
    amount: int
    idem_key: str


@dataclass(frozen=True)
class PayableStatement:
    """逐合同应付单的一个不可变版本。"""

    version_id: Optional[int]
    contract_id: str
    version_no: int
    request_no: str
    commitment_status: str
    supplier_id: str
    total_qty: int
    total_price: int
    net_qty: int
    confirmed_price: int
    old_spent: int
    payable_amount: int
    recovery_amount: int
    status: str
    receipts: tuple[ReceiptNet, ...]
    returns: tuple[GoodsReturn, ...]
    spend_events: tuple[SpendFact, ...]
    scheme_id: Optional[int]
    org_version: Optional[int]
    created_at: str

    @property
    def can_pay(self) -> bool:
        """只有正数应付款且承诺未取消的版本才能进入付款排程。"""
        return self.status == PAYABLE and self.payable_amount > 0

    @property
    def is_recovery(self) -> bool:
        return self.status == RECOVERY

    @property
    def is_blocked(self) -> bool:
        return self.status == BLOCKED_CANCELLED


class ContractService:
    """合同 / 验收 / 退货 / 应付单仓，持有独立 SQLite 文件。"""

    def __init__(self, db_path: str | Path, budget: "BudgetService") -> None:
        self.db_path = str(db_path)
        self.budget = budget
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------------------------------------------------------------- 合同

    def create_contract(
        self,
        contract_id: str,
        request_no: str,
        total_qty: int,
        supplier_id: str,
        earliest_day: int,
        latest_day: int,
        preferred_day: int,
        late_fee: int,
    ) -> Contract:
        """把一笔新合同绑定到一个真实且未取消的采购承诺。

        合同总价取承诺额（题面：总价就是原承诺额）；一个承诺只能被一笔
        合同绑定。同合同号重试且内容一致时返回既有合同，不重复建立。
        """
        _positive_int(total_qty, "合同总件数")
        if not isinstance(supplier_id, str) or not supplier_id:
            raise InvalidContract("供应商 ID 不能为空")
        _positive_int(earliest_day, "最早准许付款日")
        _positive_int(latest_day, "最迟准许付款日")
        _positive_int(preferred_day, "首选付款日")
        _non_negative_int(late_fee, "每日迟费")
        if not (earliest_day <= preferred_day <= latest_day):
            raise InvalidContract(
                f"付款窗口非法：要求 earliest({earliest_day}) <= "
                f"preferred({preferred_day}) <= latest({latest_day})"
            )
        commitment = self.budget.get_commitment(request_no)
        if not commitment.is_open:
            raise InvalidContract(
                f"承诺 {request_no} 已取消，不能再绑定新合同 / 开支付单"
            )
        total_price = commitment.amount

        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT contract_id FROM contract WHERE contract_id = ?", (contract_id,)
            ).fetchone()
            if existing is not None:
                con.commit()
                return self._load_existing_contract(
                    con,
                    contract_id,
                    request_no,
                    total_qty,
                    supplier_id,
                    earliest_day,
                    latest_day,
                    preferred_day,
                    late_fee,
                )
            bound = con.execute(
                "SELECT contract_id FROM contract WHERE request_no = ?", (request_no,)
            ).fetchone()
            if bound is not None:
                raise ContractConflict(
                    f"承诺 {request_no} 已绑定合同 {bound[0]}，不能重复绑定"
                )
            created_at = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO contract(contract_id, request_no, supplier_id, total_qty, "
                "total_price, earliest_day, latest_day, preferred_day, late_fee, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (contract_id, request_no, supplier_id, total_qty, total_price,
                 earliest_day, latest_day, preferred_day, late_fee, created_at),
            )
            con.commit()
        return self.get_contract(contract_id)

    def _load_existing_contract(
        self,
        con: sqlite3.Connection,
        contract_id: str,
        request_no: str,
        total_qty: int,
        supplier_id: str,
        earliest_day: int,
        latest_day: int,
        preferred_day: int,
        late_fee: int,
    ) -> Contract:
        row = con.execute(
            "SELECT contract_id, request_no, supplier_id, total_qty, total_price, "
            "earliest_day, latest_day, preferred_day, late_fee, created_at "
            "FROM contract WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        current = Contract(*row)
        retry_tuple = (request_no, total_qty, supplier_id, earliest_day,
                       latest_day, preferred_day, late_fee)
        stored_tuple = (current.request_no, current.total_qty, current.supplier_id,
                        current.earliest_day, current.latest_day,
                        current.preferred_day, current.late_fee)
        if retry_tuple != stored_tuple:
            raise IdempotencyConflict(
                f"合同号 {contract_id} 已存在且内容与重试不一致："
                f"库内 {stored_tuple} vs 重试 {retry_tuple}"
            )
        return current

    def get_contract(self, contract_id: str) -> Contract:
        with self._connect() as con:
            row = con.execute(
                "SELECT contract_id, request_no, supplier_id, total_qty, total_price, "
                "earliest_day, latest_day, preferred_day, late_fee, created_at "
                "FROM contract WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
        if row is None:
            raise ContractNotFound(f"合同 {contract_id} 不存在")
        return Contract(*row)

    def list_contracts(self) -> tuple[Contract, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT contract_id, request_no, supplier_id, total_qty, total_price, "
                "earliest_day, latest_day, preferred_day, late_fee, created_at "
                "FROM contract ORDER BY contract_id"
            ).fetchall()
        return tuple(Contract(*r) for r in rows)

    # ---------------------------------------------------------------- 验收与退货

    def register_receipt(self, contract_id: str, receipt_no: str, qty: int) -> Receipt:
        """按验收单号登记一批到货；同合同累计到货不得超过合同总件数。"""
        _positive_int(qty, "验收数量")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            contract = self._require_contract_locked(con, contract_id)
            existing = con.execute(
                "SELECT receipt_no, contract_id, qty FROM receipt WHERE receipt_no = ?",
                (receipt_no,),
            ).fetchone()
            if existing is not None:
                con.commit()
                if existing[1] != contract_id or existing[2] != qty:
                    raise IdempotencyConflict(
                        f"验收单号 {receipt_no} 已用于别的内容: {existing}"
                    )
                return self._load_receipt(con, receipt_no)
            gross = int(
                con.execute(
                    "SELECT COALESCE(SUM(qty), 0) FROM receipt WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()[0]
            )
            if gross + qty > contract.total_qty:
                raise ReceiptError(
                    f"合同 {contract_id} 总件数 {contract.total_qty}，已验收 {gross}，"
                    f"本批 {qty} 到货后超出总件数"
                )
            seq = int(
                con.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM receipt WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()[0]
            ) + 1
            created_at = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO receipt(receipt_no, contract_id, seq, qty, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (receipt_no, contract_id, seq, qty, created_at),
            )
            con.commit()
            return self._load_receipt(con, receipt_no)

    def register_return(
        self, return_no: str, contract_id: str, receipt_no: str, qty: int
    ) -> GoodsReturn:
        """登记退货：必须指向该合同已验收的具体批次，累退不得超过该批。"""
        _positive_int(qty, "退货数量")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_contract_locked(con, contract_id)
            existing = con.execute(
                "SELECT return_no, contract_id, receipt_no, qty FROM goods_return "
                "WHERE return_no = ?",
                (return_no,),
            ).fetchone()
            if existing is not None:
                con.commit()
                if existing[1] != contract_id or existing[2] != receipt_no or existing[3] != qty:
                    raise IdempotencyConflict(
                        f"退货单号 {return_no} 已用于别的内容: {existing}"
                    )
                return self._load_return(con, return_no)
            receipt_row = con.execute(
                "SELECT receipt_no, qty FROM receipt "
                "WHERE receipt_no = ? AND contract_id = ?",
                (receipt_no, contract_id),
            ).fetchone()
            if receipt_row is None:
                raise ReturnError(
                    f"合同 {contract_id} 下没有验收批次 {receipt_no}，"
                    "退货必须指向已验收的具体批次"
                )
            returned = int(
                con.execute(
                    "SELECT COALESCE(SUM(qty), 0) FROM goods_return "
                    "WHERE receipt_no = ?",
                    (receipt_no,),
                ).fetchone()[0]
            )
            if returned + qty > receipt_row[1]:
                raise ReturnError(
                    f"批次 {receipt_no} 验收 {receipt_row[1]} 件、已退 {returned} 件，"
                    f"再退 {qty} 件超过该批数量"
                )
            created_at = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO goods_return(return_no, contract_id, receipt_no, qty, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (return_no, contract_id, receipt_no, qty, created_at),
            )
            con.commit()
            return self._load_return(con, return_no)

    def list_receipts(self, contract_id: str) -> tuple[Receipt, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT receipt_no, contract_id, seq, qty, created_at FROM receipt "
                "WHERE contract_id = ? ORDER BY seq",
                (contract_id,),
            ).fetchall()
        return tuple(Receipt(*r) for r in rows)

    def list_returns(self, contract_id: str) -> tuple[GoodsReturn, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT return_no, contract_id, receipt_no, qty, created_at "
                "FROM goods_return WHERE contract_id = ? ORDER BY rowid",
                (contract_id,),
            ).fetchall()
        return tuple(GoodsReturn(*r) for r in rows)

    # ---------------------------------------------------------------- 应付单

    def build_payable_statement(self, contract_id: str) -> PayableStatement:
        """重算并留存一版逐合同应付单（可反复重开，旧版本不修改）。

        计算口径（题面）：

        * 每切面累计净验收 ``q`` 件，可确认累计价 ``floor(总价 × q / N)``；
        * 减去旧库已实际支付给该承诺的全部实付（只扣一次，与退货无关）；
        * 净额为负 -> 追回额，不得作为可付单；承诺已取消 -> 阻塞支付。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            contract = self._require_contract_locked(con, contract_id)
            receipt_rows = con.execute(
                "SELECT receipt_no, seq, qty FROM receipt "
                "WHERE contract_id = ? ORDER BY seq",
                (contract_id,),
            ).fetchall()
            return_rows = con.execute(
                "SELECT return_no, contract_id, receipt_no, qty, created_at "
                "FROM goods_return WHERE contract_id = ? ORDER BY rowid",
                (contract_id,),
            ).fetchall()
            next_version = int(
                con.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM payable_statement "
                    "WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()[0]
            )

            returned_by_receipt: dict[str, int] = {}
            for _, _, receipt_no, qty, _ in return_rows:
                returned_by_receipt[receipt_no] = returned_by_receipt.get(receipt_no, 0) + qty
            receipts_net = tuple(
                ReceiptNet(
                    receipt_no=r_no,
                    seq=seq,
                    gross_qty=qty,
                    returned_qty=returned_by_receipt.get(r_no, 0),
                    net_qty=qty - returned_by_receipt.get(r_no, 0),
                )
                for r_no, seq, qty in receipt_rows
            )
            returns = tuple(GoodsReturn(*r) for r in return_rows)
            net_qty = sum(r.net_qty for r in receipts_net)

            # 对旧库只读：承诺状态与仅追加实付事件都来自预算仓。
            commitment = self.budget.get_commitment(contract.request_no)
            spend_events = tuple(
                SpendFact(request_no=req, amount=amount, idem_key=idem)
                for req, _dept, amount, idem in self.budget.spend_events()
                if req == contract.request_no
            )
            old_spent = sum(e.amount for e in spend_events)
            assert old_spent == commitment.spent, "旧库实付汇总必须与承诺 spent 一致"

            confirmed_price = (
                contract.total_price * net_qty // contract.total_qty
            )
            payable_amount = confirmed_price - old_spent
            recovery_amount = max(0, -payable_amount)

            if not commitment.is_open:
                status = BLOCKED_CANCELLED
            elif payable_amount < 0:
                status = RECOVERY
            elif payable_amount == 0:
                status = ZERO_DUE
            else:
                status = PAYABLE

            try:
                scheme = self.budget.active_scheme()
                scheme_id: Optional[int] = scheme.scheme_id
                org_version: Optional[int] = scheme.org_version
            except Exception:  # noqa: BLE001 - 旧库尚未建方案时版本留空，事实仍可算
                scheme_id = None
                org_version = None

            created_at = datetime.now(timezone.utc).isoformat()
            payload = {
                "contract_id": contract_id,
                "version_no": next_version,
                "request_no": contract.request_no,
                "commitment_status": commitment.status,
                "supplier_id": contract.supplier_id,
                "total_qty": contract.total_qty,
                "total_price": contract.total_price,
                "net_qty": net_qty,
                "confirmed_price": confirmed_price,
                "old_spent": old_spent,
                "payable_amount": payable_amount,
                "recovery_amount": recovery_amount,
                "status": status,
                "receipts": [
                    {
                        "receipt_no": r.receipt_no,
                        "seq": r.seq,
                        "gross_qty": r.gross_qty,
                        "returned_qty": r.returned_qty,
                        "net_qty": r.net_qty,
                    }
                    for r in receipts_net
                ],
                "returns": [
                    {
                        "return_no": g.return_no,
                        "contract_id": g.contract_id,
                        "receipt_no": g.receipt_no,
                        "qty": g.qty,
                        "created_at": g.created_at,
                    }
                    for g in returns
                ],
                "spend_events": [
                    {"request_no": e.request_no, "amount": e.amount, "idem_key": e.idem_key}
                    for e in spend_events
                ],
                "scheme_id": scheme_id,
                "org_version": org_version,
                "created_at": created_at,
            }
            cur = con.execute(
                "INSERT INTO payable_statement(contract_id, version_no, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (contract_id, next_version, json.dumps(payload, ensure_ascii=False), created_at),
            )
            version_id = int(cur.lastrowid)
            con.commit()

        return self.get_statement(version_id)

    def latest_statement(self, contract_id: str) -> PayableStatement:
        with self._connect() as con:
            self._require_contract_locked(con, contract_id)
            row = con.execute(
                "SELECT version_id FROM payable_statement WHERE contract_id = ? "
                "ORDER BY version_no DESC LIMIT 1",
                (contract_id,),
            ).fetchone()
        if row is None:
            raise PayableError(f"合同 {contract_id} 还没有应付单版本")
        return self.get_statement(row[0])

    def statement_versions(self, contract_id: str) -> tuple[PayableStatement, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT version_id FROM payable_statement WHERE contract_id = ? "
                "ORDER BY version_no",
                (contract_id,),
            ).fetchall()
        return tuple(self.get_statement(r[0]) for r in rows)

    def get_statement(self, version_id: int) -> PayableStatement:
        with self._connect() as con:
            row = con.execute(
                "SELECT payload FROM payable_statement WHERE version_id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise PayableError(f"应付单版本 {version_id} 不存在")
        data = json.loads(row[0])
        return PayableStatement(
            version_id=version_id,
            contract_id=data["contract_id"],
            version_no=data["version_no"],
            request_no=data["request_no"],
            commitment_status=data["commitment_status"],
            supplier_id=data["supplier_id"],
            total_qty=data["total_qty"],
            total_price=data["total_price"],
            net_qty=data["net_qty"],
            confirmed_price=data["confirmed_price"],
            old_spent=data["old_spent"],
            payable_amount=data["payable_amount"],
            recovery_amount=data["recovery_amount"],
            status=data["status"],
            receipts=tuple(ReceiptNet(**r) for r in data["receipts"]),
            returns=tuple(GoodsReturn(**r) for r in data["returns"]),
            spend_events=tuple(SpendFact(**e) for e in data["spend_events"]),
            scheme_id=data["scheme_id"],
            org_version=data["org_version"],
            created_at=data["created_at"],
        )

    def old_ledger_spend(self, request_no: str) -> tuple[SpendFact, ...]:
        """只读查看旧库针对某承诺的实付事件（不代替旧库入账）。"""
        return tuple(
            SpendFact(request_no=req, amount=amount, idem_key=idem)
            for req, _dept, amount, idem in self.budget.spend_events()
            if req == request_no
        )

    # ---------------------------------------------------------------- 内部

    def _require_contract_locked(self, con: sqlite3.Connection, contract_id: str) -> Contract:
        row = con.execute(
            "SELECT contract_id, request_no, supplier_id, total_qty, total_price, "
            "earliest_day, latest_day, preferred_day, late_fee, created_at "
            "FROM contract WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if row is None:
            raise ContractNotFound(f"合同 {contract_id} 不存在")
        return Contract(*row)

    def _load_receipt(self, con: sqlite3.Connection, receipt_no: str) -> Receipt:
        row = con.execute(
            "SELECT receipt_no, contract_id, seq, qty, created_at "
            "FROM receipt WHERE receipt_no = ?",
            (receipt_no,),
        ).fetchone()
        return Receipt(*row)

    def _load_return(self, con: sqlite3.Connection, return_no: str) -> GoodsReturn:
        row = con.execute(
            "SELECT return_no, contract_id, receipt_no, qty, created_at "
            "FROM goods_return WHERE return_no = ?",
            (return_no,),
        ).fetchone()
        return GoodsReturn(*row)
