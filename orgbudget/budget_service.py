"""预算执行仓。

预算仓与组织编制服务物理分离（独立 SQLite 文件），只通过
:class:`~orgbudget.org_service.OrgProduct` 不可变产品衔接：每个预算方案
明确绑定一个组织产品版本，全部预算分解、汇总闭包都沿该产品自身的树
计算，绝不引用“当前树”，因此迁移后的历史查询不会被新树解释。

写操作统一在 ``BEGIN IMMEDIATE`` 事务内完成“读取校验 → 写入”，SQLite
写锁把并发提交串行化：两个连接争抢同一叶剩余额时最多一方成功；请求号
是承诺的全局唯一幂等键，同请求号重试不会二次占额。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .budget import decompose_tree
from .errors import (
    CommitmentClosedError,
    CommitmentNotFound,
    IdempotencyConflict,
    InactiveDepartment,
    InactiveLeaf,
    IncompleteRoster,
    InsufficientFunds,
    InvalidTree,
    NotALeaf,
    OrganizationVersionNotFound,
    RosterNotFound,
    SchemeError,
    SpendEventNotFound,
    TicketConflict,
    TicketNotFound,
    WeightError,
)
from .org_service import OrgProduct, OrgService
from .roster import (
    JOINT_LAYER,
    Roster,
    RosterEnvelope,
    RosterLayer,
    RosterPayment,
    TicketAssignment,
    classify_layer,
    cut_digest,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheme (
    scheme_id    INTEGER PRIMARY KEY,
    org_version  INTEGER NOT NULL,
    root_budget  INTEGER NOT NULL CHECK (root_budget >= 0),
    status       TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weight (
    scheme_id  INTEGER NOT NULL REFERENCES scheme(scheme_id),
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    weight     INTEGER NOT NULL CHECK (weight > 0),
    PRIMARY KEY (scheme_id, parent_id, child_id)
);
CREATE TABLE IF NOT EXISTS allocation (
    scheme_id  INTEGER NOT NULL REFERENCES scheme(scheme_id),
    dept_id    TEXT NOT NULL,
    budget     INTEGER NOT NULL CHECK (budget >= 0),
    PRIMARY KEY (scheme_id, dept_id)
);
-- 承诺事实。request_no 全局唯一：丢响应重试不会第二次占额。
CREATE TABLE IF NOT EXISTS commitment (
    request_no    TEXT PRIMARY KEY,
    dept_id       TEXT NOT NULL,
    amount        INTEGER NOT NULL CHECK (amount > 0),
    status        TEXT NOT NULL CHECK (status IN ('OPEN', 'CANCELLED')),
    created_at    TEXT NOT NULL,
    cancelled_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_commitment_dept ON commitment(dept_id);
-- 实际支出为仅追加事件，永远不删除；取消承诺不回滚已登记支出。
CREATE TABLE IF NOT EXISTS spend (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no    TEXT NOT NULL REFERENCES commitment(request_no),
    dept_id       TEXT NOT NULL,
    amount        INTEGER NOT NULL CHECK (amount > 0),
    idem_key      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_dept ON spend(dept_id);
-- 被拒绝的换版尝试留痕，不影响任何既有方案。
CREATE TABLE IF NOT EXISTS rejected_revision (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_version  INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
-- 具名纸票：一次冻结里同一纸票始终恰好一封。
CREATE TABLE IF NOT EXISTS paper_ticket (
    ticket_id   TEXT PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);
-- 已实付事件的归票。每条实付事件至多归一张纸票；可整条改挂（勘误）。
-- 只引用 spend(idem_key) 且 spend.idem_key 唯一，天然只能归“已实际入账”的事件。
CREATE TABLE IF NOT EXISTS ticket_assignment (
    idem_key     TEXT PRIMARY KEY REFERENCES spend(idem_key),
    ticket_id    TEXT NOT NULL REFERENCES paper_ticket(ticket_id),
    note         TEXT,
    assigned_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_assignment_ticket
    ON ticket_assignment(ticket_id);
-- 冻结签发的封样名册（不可变快照）。旧名册与后续更正/迁移无关，永远可重开。
CREATE TABLE IF NOT EXISTS roster (
    roster_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    org_version  INTEGER NOT NULL,
    cut_digest   TEXT NOT NULL,
    spend_count  INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS roster_envelope (
    roster_id    INTEGER NOT NULL REFERENCES roster(roster_id),
    layer_no     INTEGER NOT NULL,
    layer        TEXT NOT NULL,
    envelope_no  INTEGER NOT NULL,
    ticket_id    TEXT NOT NULL,
    basis        TEXT NOT NULL,
    PRIMARY KEY (roster_id, layer, envelope_no)
);
CREATE TABLE IF NOT EXISTS roster_payment (
    roster_id    INTEGER NOT NULL REFERENCES roster(roster_id),
    layer        TEXT NOT NULL,
    envelope_no  INTEGER NOT NULL,
    idem_key     TEXT NOT NULL,
    request_no   TEXT NOT NULL,
    dept_id      TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    PRIMARY KEY (roster_id, idem_key),
    FOREIGN KEY (roster_id, layer, envelope_no)
        REFERENCES roster_envelope(roster_id, layer, envelope_no)
);
CREATE INDEX IF NOT EXISTS idx_roster_payment_env
    ON roster_payment(roster_id, layer, envelope_no);
"""

_OPEN = "OPEN"
_CANCELLED = "CANCELLED"
_ACTIVE = "ACTIVE"
_SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class Commitment:
    request_no: str
    dept_id: str
    amount: int
    status: str
    spent: int
    created_at: str
    cancelled_at: Optional[str]

    @property
    def remaining(self) -> int:
        return self.amount - self.spent

    @property
    def is_open(self) -> bool:
        return self.status == _OPEN


@dataclass(frozen=True)
class SchemeInfo:
    scheme_id: int
    org_version: int
    root_budget: int
    status: str
    created_at: str


@dataclass(frozen=True)
class NodeSummary:
    dept_id: str
    budget: int
    spent: int
    open_committed: int
    available: int
    is_leaf: bool


class BudgetService:
    """预算执行仓。每个实例持有独立 SQLite 文件。"""

    def __init__(
        self,
        db_path: str | Path,
        org_service: OrgService,
        *,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        self.db_path = str(db_path)
        self.org = org_service
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('active_scheme_id', '0')"
            )
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def _product(self, con: sqlite3.Connection, scheme_id: int) -> OrgProduct:
        """按方案绑定的版本实时向组织服务读取本次产品，不使用旧缓存。"""
        org_version = con.execute(
            "SELECT org_version FROM scheme WHERE scheme_id = ?", (scheme_id,)
        ).fetchone()[0]
        return self.org.get_product(org_version)

    def _active_scheme_id(self, con: sqlite3.Connection) -> Optional[int]:
        value = int(
            con.execute(
                "SELECT value FROM meta WHERE key = 'active_scheme_id'"
            ).fetchone()[0]
        )
        return value or None

    # ---------------------------------------------------------------- 方案与分解

    def register_root_budget(
        self, org_version: int, root_budget: int, weights: dict[tuple[str, str], int]
    ) -> SchemeInfo:
        """为指定组织产品登记首个根预算并一次性分解到全部节点。"""
        if root_budget < 0:
            raise SchemeError("根预算不能为负")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if self._active_scheme_id(con) is not None:
                raise SchemeError("已有生效方案，换版请使用 revise_budget")
            product = self._require_product(org_version)
            allocations = self._validate_and_decompose(product, root_budget, weights)
            scheme_id = self._insert_scheme(con, product, root_budget, weights, allocations)
            con.execute(
                "UPDATE meta SET value = ? WHERE key = 'active_scheme_id'",
                (str(scheme_id),),
            )
            con.commit()
        return self.get_scheme(scheme_id)

    def revise_budget(
        self, org_version: int, root_budget: int, weights: dict[tuple[str, str], int]
    ) -> SchemeInfo:
        """采用新组织产品换版重算全部预算。

        新权重按新产品的直接孩子关系补齐并重算；随后用全部历史承诺 /
        支出事实做覆盖校验，任一叶覆盖不了实际支出与未结承诺则整次
        换版拒绝（旧方案继续生效、组织新产品不受影响）。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old_scheme_id = self._active_scheme_id(con)
            if old_scheme_id is None:
                raise SchemeError("尚无生效方案，首个方案请使用 register_root_budget")
            old_org_version = con.execute(
                "SELECT org_version FROM scheme WHERE scheme_id = ?", (old_scheme_id,)
            ).fetchone()[0]
            if org_version == old_org_version:
                raise SchemeError("换版必须绑定不同的组织产品版本")
            product = self._require_product(org_version)
            try:
                allocations = self._validate_and_decompose(
                    product, root_budget, weights
                )
                self._assert_coverage(con, product, allocations)
            except (WeightError, InsufficientFunds, InactiveDepartment) as exc:
                con.execute(
                    "INSERT INTO rejected_revision(org_version, reason, created_at) "
                    "VALUES (?, ?, ?)",
                    (org_version, str(exc), datetime.now(timezone.utc).isoformat()),
                )
                con.commit()
                raise
            scheme_id = self._insert_scheme(con, product, root_budget, weights, allocations)
            con.execute(
                "UPDATE scheme SET status = ? WHERE scheme_id = ?",
                (_SUPERSEDED, old_scheme_id),
            )
            con.execute(
                "UPDATE meta SET value = ? WHERE key = 'active_scheme_id'",
                (str(scheme_id),),
            )
            con.commit()
        return self.get_scheme(scheme_id)

    def rejected_revisions(self) -> tuple[tuple[int, str, str], ...]:
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT org_version, reason, created_at FROM rejected_revision "
                    "ORDER BY id"
                ).fetchall()
            )

    def active_scheme(self) -> SchemeInfo:
        with self._connect() as con:
            scheme_id = self._active_scheme_id(con)
            if scheme_id is None:
                raise SchemeError("尚无生效预算方案")
            return self._load_scheme(con, scheme_id)

    def get_scheme(self, scheme_id: int) -> SchemeInfo:
        with self._connect() as con:
            return self._load_scheme(con, scheme_id)

    def _load_scheme(self, con: sqlite3.Connection, scheme_id: int) -> SchemeInfo:
        row = con.execute(
            "SELECT scheme_id, org_version, root_budget, status, created_at "
            "FROM scheme WHERE scheme_id = ?",
            (scheme_id,),
        ).fetchone()
        if row is None:
            raise SchemeError(f"预算方案 {scheme_id} 不存在")
        return SchemeInfo(*row)

    def _require_product(self, org_version: int) -> OrgProduct:
        try:
            return self.org.get_product(org_version)
        except OrganizationVersionNotFound:
            raise

    def _validate_and_decompose(
        self,
        product: OrgProduct,
        root_budget: int,
        weights: dict[tuple[str, str], int],
    ) -> dict[str, int]:
        children_of = {
            dept: list(product.children(dept)) for dept in product.department_ids
        }
        expected_edges = {
            (parent, kid)
            for parent, kids in children_of.items()
            for kid in kids
        }
        given_edges = set(weights)
        missing = sorted(expected_edges - given_edges)
        extra = sorted(given_edges - expected_edges)
        if missing or extra:
            details = []
            if missing:
                details.append(f"缺权重 {missing}")
            if extra:
                details.append(f"多余权重 {extra}")
            raise WeightError("；".join(details))
        for edge, w in weights.items():
            if not isinstance(w, int) or isinstance(w, bool) or w <= 0:
                raise WeightError(f"边 {edge} 权重必须是正整数: {w!r}")

        allocations = decompose_tree(
            root_budget=root_budget,
            root_id=product.root,
            children_of=children_of,
            weights=weights,
        )
        # 守恒不变量：每级孩子预算之和严格等于父预算。
        for parent, kids in children_of.items():
            if kids and sum(allocations[k] for k in kids) != allocations[parent]:
                raise InvalidTree(f"部门 {parent} 的分解不守恒")
        return allocations

    def _assert_coverage(
        self,
        con: sqlite3.Connection,
        product: OrgProduct,
        allocations: dict[str, int],
    ) -> None:
        """新方案必须让每个叶仍能覆盖实际支出与未结承诺剩余额。

        承诺的部门身份永不改写：按承诺原始 dept_id 归集，只校验该部门
        在新产品中仍是叶，不允许把承诺“搬”到新部门。
        """
        leaves = {d for d in product.department_ids if product.is_leaf(d)}
        rows = con.execute(
            "SELECT dept_id, "
            "COALESCE((SELECT SUM(amount) FROM spend s WHERE s.dept_id = c.dept_id), 0) AS spent, "
            "COALESCE(SUM(CASE WHEN c.status = ? THEN c.amount - "
            "(SELECT COALESCE(SUM(amount),0) FROM spend s WHERE s.request_no = c.request_no) "
            "ELSE 0 END), 0) AS open_remaining "
            "FROM commitment c GROUP BY c.dept_id",
            (_OPEN,),
        ).fetchall()
        for dept_id, spent, open_remaining in rows:
            if dept_id not in product.parent_of:
                raise InactiveDepartment(
                    f"部门 {dept_id} 带着承诺/支出但不在新组织产品中"
                )
            if dept_id not in leaves:
                raise NotALeaf(
                    f"部门 {dept_id} 在新产品中不再是叶，承诺身份不能改挂"
                )
            required = spent + open_remaining
            if allocations[dept_id] < required:
                raise InsufficientFunds(
                    f"叶 {dept_id} 新预算 {allocations[dept_id]} 分不足以覆盖 "
                    f"实际支出 {spent} 分 + 未结承诺剩余 {open_remaining} 分"
                )

    def _insert_scheme(
        self,
        con: sqlite3.Connection,
        product: OrgProduct,
        root_budget: int,
        weights: dict[tuple[str, str], int],
        allocations: dict[str, int],
    ) -> int:
        cur = con.execute(
            "INSERT INTO scheme(org_version, root_budget, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                product.version,
                root_budget,
                _ACTIVE,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        scheme_id = int(cur.lastrowid)
        con.executemany(
            "INSERT INTO weight(scheme_id, parent_id, child_id, weight) VALUES (?, ?, ?, ?)",
            [(scheme_id, p, c, w) for (p, c), w in weights.items()],
        )
        con.executemany(
            "INSERT INTO allocation(scheme_id, dept_id, budget) VALUES (?, ?, ?)",
            [(scheme_id, d, b) for d, b in allocations.items()],
        )
        return scheme_id

    # ---------------------------------------------------------------- 承诺与支出

    def create_commitment(self, request_no: str, dept_id: str, amount: int) -> Commitment:
        """在叶部门开立支出承诺。

        叶可用额 = 分配预算 − 实际支出 − 未结承诺剩余，不得为负；
        同请求号重试直接返回既有承诺（金额/部门一致时），不二次占额。
        """
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("承诺金额必须是正整数分")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            scheme_id = self._require_active(con)
            product = self._product(con, scheme_id)
            existing = con.execute(
                "SELECT request_no FROM commitment WHERE request_no = ?", (request_no,)
            ).fetchone()
            if existing is not None:
                return self._load_commitment(con, request_no, expected_dept=dept_id,
                                             expected_amount=amount)
            if not product.contains(dept_id):
                raise OrganizationVersionNotFound(
                    f"部门 {dept_id} 不在本次组织产品 {product.version} 中"
                )
            if not product.is_leaf(dept_id):
                raise NotALeaf(f"部门 {dept_id} 不是叶，承诺只能开在叶上")
            if not product.is_active(dept_id):
                raise InactiveLeaf(f"叶部门 {dept_id} 已停用，不能开立承诺")
            summary = self._summary_locked(con, scheme_id, product, dept_id)
            if summary.available < amount:
                raise InsufficientFunds(
                    f"叶 {dept_id} 可用 {summary.available} 分，申请承诺 {amount} 分"
                )
            con.execute(
                "INSERT INTO commitment(request_no, dept_id, amount, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (request_no, dept_id, amount, _OPEN, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        return self.get_commitment(request_no)

    def register_spend(
        self, request_no: str, amount: int, idem_key: str
    ) -> Commitment:
        """针对未结承诺登记实际支出，不得超过承诺剩余额。

        ``idem_key`` 是支出事件幂等键；同键重试不重复登记。
        """
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("支出金额必须是正整数分")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT request_no, amount FROM spend WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            if existing is not None:
                if existing[0] != request_no or existing[1] != amount:
                    raise IdempotencyConflict(
                        f"幂等键 {idem_key} 已用于另一笔支出"
                    )
                con.commit()
                return self.get_commitment(request_no)
            row = con.execute(
                "SELECT dept_id, amount, status FROM commitment WHERE request_no = ?",
                (request_no,),
            ).fetchone()
            if row is None:
                raise CommitmentNotFound(f"承诺 {request_no} 不存在")
            dept_id, committed, status = row
            if status == _CANCELLED:
                raise CommitmentClosedError(f"承诺 {request_no} 已取消，不能再支出")
            spent = self._spent_locked(con, request_no)
            if spent + amount > committed:
                raise InsufficientFunds(
                    f"承诺 {request_no} 剩余 {committed - spent} 分，申请支出 {amount} 分"
                )
            con.execute(
                "INSERT INTO spend(request_no, dept_id, amount, idem_key, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (request_no, dept_id, amount, idem_key, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        return self.get_commitment(request_no)

    def cancel_commitment(self, request_no: str) -> Commitment:
        """取消未结承诺；已实际支出的部分不可撤销，仅释放剩余占额。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status FROM commitment WHERE request_no = ?", (request_no,)
            ).fetchone()
            if row is None:
                raise CommitmentNotFound(f"承诺 {request_no} 不存在")
            if row[0] == _OPEN:
                con.execute(
                    "UPDATE commitment SET status = ?, cancelled_at = ? WHERE request_no = ?",
                    (_CANCELLED, datetime.now(timezone.utc).isoformat(), request_no),
                )
            con.commit()
        return self.get_commitment(request_no)

    def get_commitment(self, request_no: str) -> Commitment:
        with self._connect() as con:
            return self._load_commitment(con, request_no)

    # ---------------------------------------------------------------- 查询

    def budget_of(self, dept_id: str, scheme_id: Optional[int] = None) -> int:
        """读取某部门在指定方案（默认当前生效方案）下的分配预算。"""
        with self._connect() as con:
            sid = scheme_id or self._require_active(con)
            row = con.execute(
                "SELECT budget FROM allocation WHERE scheme_id = ? AND dept_id = ?",
                (sid, dept_id),
            ).fetchone()
            if row is None:
                raise OrganizationVersionNotFound(
                    f"方案 {sid} 中没有部门 {dept_id} 的预算"
                )
            return int(row[0])

    def summary(self, dept_id: str, scheme_id: Optional[int] = None) -> NodeSummary:
        """单部门汇总，沿该方案绑定的组织产品树计算。"""
        with self._connect() as con:
            sid = scheme_id or self._require_active(con)
            product = self._product(con, sid)
            if not product.contains(dept_id):
                raise OrganizationVersionNotFound(
                    f"部门 {dept_id} 不在组织产品 {product.version} 中"
                )
            return self._summary_locked(con, sid, product, dept_id)

    def subtree_summary(
        self, root_id: str, scheme_id: Optional[int] = None
    ) -> tuple[NodeSummary, ...]:
        """子树内每个部门的汇总（含自身），按部门 ID 升序。"""
        with self._connect() as con:
            sid = scheme_id or self._require_active(con)
            product = self._product(con, sid)
            return tuple(
                self._summary_locked(con, sid, product, d)
                for d in sorted(product.subtree(root_id))
            )

    def open_commitments(self, dept_id: Optional[str] = None) -> tuple[Commitment, ...]:
        sql = (
            "SELECT request_no FROM commitment WHERE status = ? "
            + ("AND dept_id = ? " if dept_id else "")
            + "ORDER BY request_no"
        )
        params: tuple = (_OPEN, dept_id) if dept_id else (_OPEN,)
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
            return tuple(self._load_commitment(con, r[0]) for r in rows)

    def spend_events(self, dept_id: Optional[str] = None) -> tuple[tuple[str, str, int, str], ...]:
        """仅追加的实际支出事件：(请求号, 部门, 金额, 幂等键)。"""
        sql = (
            "SELECT request_no, dept_id, amount, idem_key FROM spend "
            + ("WHERE dept_id = ? " if dept_id else "")
            + "ORDER BY id"
        )
        params: tuple = (dept_id,) if dept_id else ()
        with self._connect() as con:
            return tuple(con.execute(sql, params).fetchall())

    # ---------------------------------------------------------------- 装封与名册

    def register_ticket(self, ticket_id: str, note: Optional[str] = None) -> str:
        """登记一张具名原票。

        幂等：同名纸票重复登记不报错，只保留首次的备注。
        """
        if not isinstance(ticket_id, str) or not ticket_id:
            raise ValueError("纸票名不能为空")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT OR IGNORE INTO paper_ticket(ticket_id, note, created_at) "
                "VALUES (?, ?, ?)",
                (ticket_id, note, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        return ticket_id

    def assign_spend(
        self,
        idem_key: str,
        ticket_id: str,
        *,
        note: Optional[str] = None,
    ) -> TicketAssignment:
        """把一条**已实际入账**的支出事件归到具名纸票。

        只有 ``spend`` 表里真实存在的幂等键才能入封；同一事件在同一切面
        恰归一张票，重复归到同一票是幂等空操作；要换票请显式
        :meth:`reassign_spend`（勘误）——本方法遇到不同票会报
        ``TicketConflict``，避免误覆盖。
        """
        if not isinstance(idem_key, str) or not idem_key:
            raise ValueError("幂等键不能为空")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_spend_locked(con, idem_key)
            self._require_ticket_locked(con, ticket_id)
            row = con.execute(
                "SELECT ticket_id FROM ticket_assignment WHERE idem_key = ?",
                (idem_key,),
            ).fetchone()
            if row is not None:
                if row[0] != ticket_id:
                    raise TicketConflict(
                        f"支出事件 {idem_key} 已归票 {row[0]}，勘误请用 reassign_spend"
                    )
                con.commit()
                return self._load_assignment(con, idem_key)
            con.execute(
                "INSERT INTO ticket_assignment(idem_key, ticket_id, note, assigned_at) "
                "VALUES (?, ?, ?, ?)",
                (idem_key, ticket_id, note, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            return self._load_assignment(con, idem_key)

    def reassign_spend(
        self,
        idem_key: str,
        ticket_id: str,
        *,
        note: Optional[str] = None,
    ) -> TicketAssignment:
        """勘误归票：把已入账事件改挂到另一张具名纸票（或首次归票）。"""
        if not isinstance(idem_key, str) or not idem_key:
            raise ValueError("幂等键不能为空")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_spend_locked(con, idem_key)
            self._require_ticket_locked(con, ticket_id)
            con.execute(
                "INSERT INTO ticket_assignment(idem_key, ticket_id, note, assigned_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(idem_key) DO UPDATE SET "
                "ticket_id = excluded.ticket_id, note = excluded.note, "
                "assigned_at = excluded.assigned_at",
                (idem_key, ticket_id, note, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            return self._load_assignment(con, idem_key)

    def ticket_assignments(
        self, ticket_id: Optional[str] = None
    ) -> tuple[TicketAssignment, ...]:
        """当前各支出事件的归票（含纸票/事件明细），按幂等键排序。"""
        sql = (
            "SELECT a.idem_key, a.ticket_id, s.request_no, s.dept_id, s.amount, "
            "a.assigned_at, a.note FROM ticket_assignment a "
            "JOIN spend s ON s.idem_key = a.idem_key "
            + ("WHERE a.ticket_id = ? " if ticket_id else "")
            + "ORDER BY a.idem_key"
        )
        params: tuple = (ticket_id,) if ticket_id else ()
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return tuple(TicketAssignment(*row) for row in rows)

    def unassigned_spends(self) -> tuple[RosterPayment, ...]:
        """尚未归票的已入账支出事件（完整名册必须为空）。"""
        with self._connect() as con:
            rows = con.execute(
                "SELECT idem_key, request_no, dept_id, amount FROM spend "
                "WHERE idem_key NOT IN (SELECT idem_key FROM ticket_assignment) "
                "ORDER BY id"
            ).fetchall()
        return tuple(RosterPayment(*row) for row in rows)

    def seal_roster(
        self, org_version: int, *, note: Optional[str] = None
    ) -> Roster:
        """冻结一次完整封样名册，绑定一个**已发布组织版本**。

        冻结时校验：所有已入账支出事件都已归票（否则报
        ``IncompleteRoster``，不签发残缺名册）；各原票按本组织版本的根下
        直接分部归层，跨分部归联合层。名册一经签发即不可变，之后勘误或
        迁移不影响它；下一次冻结才产生新名册。
        """
        product = self.org.get_product(org_version)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            unassigned = con.execute(
                "SELECT COUNT(*) FROM spend "
                "WHERE idem_key NOT IN (SELECT idem_key FROM ticket_assignment)"
            ).fetchone()[0]
            if unassigned:
                raise IncompleteRoster(
                    f"仍有 {unassigned} 条已实际入账支出未归票，不能签发完整名册"
                )
            rows = con.execute(
                "SELECT s.idem_key, s.request_no, s.dept_id, s.amount, a.ticket_id "
                "FROM spend s JOIN ticket_assignment a ON a.idem_key = s.idem_key "
                "ORDER BY s.id"
            ).fetchall()
            if not rows:
                raise IncompleteRoster("没有任何已入账支出，不能签发空名册")

            by_ticket: dict[str, list[RosterPayment]] = {}
            for idem_key, request_no, dept_id, amount, ticket_id in rows:
                by_ticket.setdefault(ticket_id, []).append(
                    RosterPayment(idem_key, request_no, dept_id, amount)
                )

            # 逐票归层；同层按纸票名排序，层内封号从 1 连续编号。
            envelopes_by_layer: dict[str, list[tuple[str, str, list[RosterPayment]]]] = {}
            for ticket_id in sorted(by_ticket):
                payments = by_ticket[ticket_id]
                layer, basis = classify_layer(
                    product, (p.dept_id for p in payments)
                )
                envelopes_by_layer.setdefault(layer, []).append(
                    (ticket_id, basis, payments)
                )
            # 层序：先按直接分部 ID 升序，联合层固定最后。
            layer_names = sorted(
                envelopes_by_layer, key=lambda name: (name == JOINT_LAYER, name)
            )

            all_payments = [p for group in by_ticket.values() for p in group]
            digest = cut_digest(all_payments)
            created_at = datetime.now(timezone.utc).isoformat()
            cur = con.execute(
                "INSERT INTO roster(org_version, cut_digest, spend_count, created_at, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (org_version, digest, len(all_payments), created_at, note),
            )
            roster_id = int(cur.lastrowid)

            for layer_no, layer in enumerate(layer_names, start=1):
                for envelope_no, (ticket_id, basis, payments) in enumerate(
                    envelopes_by_layer[layer], start=1
                ):
                    con.execute(
                        "INSERT INTO roster_envelope"
                        "(roster_id, layer_no, layer, envelope_no, ticket_id, basis) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (roster_id, layer_no, layer, envelope_no, ticket_id, basis),
                    )
                    con.executemany(
                        "INSERT INTO roster_payment"
                        "(roster_id, layer, envelope_no, idem_key, request_no, dept_id, amount) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (roster_id, layer, envelope_no, p.idem_key,
                             p.request_no, p.dept_id, p.amount)
                            for p in payments
                        ],
                    )
            con.commit()
        return self.get_roster(roster_id)

    def get_roster(self, roster_id: int) -> Roster:
        """原样重开一份旧名册（独立于后续勘误、迁移与新增支出）。"""
        with self._connect() as con:
            head = con.execute(
                "SELECT roster_id, org_version, created_at, cut_digest, spend_count, note "
                "FROM roster WHERE roster_id = ?",
                (roster_id,),
            ).fetchone()
            if head is None:
                raise RosterNotFound(f"封样名册 {roster_id} 不存在")
            env_rows = con.execute(
                "SELECT layer_no, layer, envelope_no, ticket_id, basis "
                "FROM roster_envelope WHERE roster_id = ? "
                "ORDER BY layer_no, envelope_no",
                (roster_id,),
            ).fetchall()
            pay_rows = con.execute(
                "SELECT layer, envelope_no, idem_key, request_no, dept_id, amount "
                "FROM roster_payment WHERE roster_id = ? ORDER BY idem_key",
                (roster_id,),
            ).fetchall()
        payments: dict[tuple[str, int], list[RosterPayment]] = {}
        for layer, envelope_no, idem_key, request_no, dept_id, amount in pay_rows:
            payments.setdefault((layer, envelope_no), []).append(
                RosterPayment(idem_key, request_no, dept_id, amount)
            )
        layers: list[RosterLayer] = []
        by_layer: dict[str, list[RosterEnvelope]] = {}
        layer_order: dict[str, int] = {}
        for layer_no, layer, envelope_no, ticket_id, basis in env_rows:
            layer_order.setdefault(layer, layer_no)
            by_layer.setdefault(layer, []).append(
                RosterEnvelope(
                    layer=layer,
                    layer_no=layer_no,
                    envelope_no=envelope_no,
                    ticket_id=ticket_id,
                    basis=basis,
                    payments=tuple(payments.get((layer, envelope_no), [])),
                )
            )
        for layer in sorted(by_layer, key=lambda name: layer_order[name]):
            envelopes = tuple(by_layer[layer])
            layers.append(
                RosterLayer(
                    layer=layer,
                    layer_no=layer_order[layer],
                    envelope_count=len(envelopes),
                    envelopes=envelopes,
                )
            )
        return Roster(
            roster_id=head[0],
            org_version=head[1],
            created_at=head[2],
            cut_digest=head[3],
            spend_count=head[4],
            layers=tuple(layers),
            note=head[5],
        )

    def rosters(self) -> tuple[tuple[int, int, str, int, str], ...]:
        """全部已签发名册摘要：(名册号, 组织版本, 签发时间, 支付数, 摘要)。"""
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT roster_id, org_version, created_at, spend_count, cut_digest "
                    "FROM roster ORDER BY roster_id"
                ).fetchall()
            )

    # ---------------------------------------------------------------- 内部

    def _require_active(self, con: sqlite3.Connection) -> int:
        sid = self._active_scheme_id(con)
        if sid is None:
            raise SchemeError("尚无生效预算方案")
        return sid

    def _require_spend_locked(self, con: sqlite3.Connection, idem_key: str) -> None:
        row = con.execute(
            "SELECT 1 FROM spend WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        if row is None:
            raise SpendEventNotFound(
                f"支出事件 {idem_key} 不存在或尚未实际入账，不能入封"
            )

    def _require_ticket_locked(self, con: sqlite3.Connection, ticket_id: str) -> None:
        row = con.execute(
            "SELECT 1 FROM paper_ticket WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise TicketNotFound(f"纸票 {ticket_id} 不存在，请先登记")

    def _load_assignment(
        self, con: sqlite3.Connection, idem_key: str
    ) -> TicketAssignment:
        row = con.execute(
            "SELECT a.idem_key, a.ticket_id, s.request_no, s.dept_id, s.amount, "
            "a.assigned_at, a.note FROM ticket_assignment a "
            "JOIN spend s ON s.idem_key = a.idem_key WHERE a.idem_key = ?",
            (idem_key,),
        ).fetchone()
        return TicketAssignment(*row)
    def _spent_locked(self, con: sqlite3.Connection, request_no: str) -> int:
        return int(
            con.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM spend WHERE request_no = ?",
                (request_no,),
            ).fetchone()[0]
        )

    def _leaf_facts_locked(
        self, con: sqlite3.Connection
    ) -> dict[str, tuple[int, int]]:
        """全部叶的 (实际支出, 未结承诺剩余) 事实，按承诺原始部门归集。"""
        spent_rows = dict(
            con.execute("SELECT dept_id, COALESCE(SUM(amount), 0) FROM spend GROUP BY dept_id")
            .fetchall()
        )
        open_rows = con.execute(
            "SELECT c.dept_id, COALESCE(SUM(c.amount - "
            "(SELECT COALESCE(SUM(amount),0) FROM spend s WHERE s.request_no = c.request_no)), 0) "
            "FROM commitment c WHERE c.status = ? GROUP BY c.dept_id",
            (_OPEN,),
        ).fetchall()
        open_map = dict(open_rows)
        depts = set(spent_rows) | set(open_map)
        return {d: (int(spent_rows.get(d, 0)), int(open_map.get(d, 0))) for d in depts}

    def _summary_locked(
        self,
        con: sqlite3.Connection,
        scheme_id: int,
        product: OrgProduct,
        dept_id: str,
    ) -> NodeSummary:
        budget = int(
            con.execute(
                "SELECT budget FROM allocation WHERE scheme_id = ? AND dept_id = ?",
                (scheme_id, dept_id),
            ).fetchone()[0]
        )
        facts = self._leaf_facts_locked(con)
        if product.is_leaf(dept_id):
            spent, open_remaining = facts.get(dept_id, (0, 0))
            available = budget - spent - open_remaining
            if available < 0:
                raise InvalidTree(f"叶 {dept_id} 可用额为负 {available}，不变量被破坏")
            return NodeSummary(dept_id, budget, spent, open_remaining, available, True)
        spent = sum(facts[d][0] for d in product.descendants(dept_id) if d in facts)
        open_remaining = sum(
            facts[d][1] for d in product.descendants(dept_id) if d in facts
        )
        return NodeSummary(dept_id, budget, spent, open_remaining,
                           budget - spent - open_remaining, False)

    def _load_commitment(
        self,
        con: sqlite3.Connection,
        request_no: str,
        *,
        expected_dept: Optional[str] = None,
        expected_amount: Optional[int] = None,
    ) -> Commitment:
        row = con.execute(
            "SELECT request_no, dept_id, amount, status, created_at, cancelled_at "
            "FROM commitment WHERE request_no = ?",
            (request_no,),
        ).fetchone()
        if row is None:
            raise CommitmentNotFound(f"承诺 {request_no} 不存在")
        if expected_dept is not None and (row[1] != expected_dept or row[2] != expected_amount):
            raise IdempotencyConflict(
                f"请求号 {request_no} 已用于部门 {row[1]} 金额 {row[2]} 分的承诺，"
                f"与重试内容({expected_dept}, {expected_amount})不一致"
            )
        spent = self._spent_locked(con, request_no)
        return Commitment(
            request_no=row[0],
            dept_id=row[1],
            amount=row[2],
            status=row[3],
            spent=spent,
            created_at=row[4],
            cancelled_at=row[5],
        )
