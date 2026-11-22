"""会签询证与印务室登记服务。

两个服务共用一个独立于组织库 / 预算库 / 授权库的 SQLite 文件：

* :class:`CountersignService` —— 请付询证。授权仅在询证那一刻取值：
  按生效预算方案绑定的组织产品找承诺叶的叶→根链，用最新版授权本解析
  两道印候选人，连同预算方案、组织版本、授权本版本、承诺切面整体冻结
  为不可变可签名单；逐人说明从哪一层继承或被哪一条禁掉。无款、已取消
  或某道印无人可盖时只说明理由，不产出空白凭证。已签出的旧名单可复读。
* :class:`PrintRoomService` —— 开柜备印结论落库后的正式登记。按实际
  到岗只开立一套签单，在事务内校验备印、在岗、互斥与“一人一轮一次”，
  回执号幂等，旧回执重复打开不能二次占印。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .auth import AuthBook, HANDLER, REVIEWER, ROLES, PersonAuthority, granted_people, resolve_role
from .auth_service import AuthService
from .budget_service import BudgetService
from .errors import (
    CommitmentNotOpen,
    IncompatibleAssignees,
    InsufficientCommitmentRemaining,
    InvalidPaymentAmount,
    NoSignerAvailable,
    PersonOccupied,
    PersonOffDuty,
    PrintRoomError,
    ReceiptConflict,
    SignableListNotFound,
    StampNotPrepared,
)
from .print_room import AbsencePlan, DocCandidate, PrepPlan, PrintRound, Witness

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signable_list (
    list_id        TEXT PRIMARY KEY,
    request_no     TEXT NOT NULL,
    payment_amount INTEGER NOT NULL,
    scheme_id      INTEGER NOT NULL,
    org_version    INTEGER NOT NULL,
    auth_edition   INTEGER NOT NULL,
    commitment_dept   TEXT NOT NULL,
    commitment_amount INTEGER NOT NULL,
    commitment_spent  INTEGER NOT NULL,
    commitment_remaining INTEGER NOT NULL,
    handler_people  TEXT NOT NULL,
    reviewer_people TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_list_request ON signable_list(request_no);

CREATE TABLE IF NOT EXISTS print_round (
    round_id      TEXT PRIMARY KEY,
    duty_people   TEXT NOT NULL,
    fees_json     TEXT NOT NULL,
    incompat_json TEXT NOT NULL,
    plans_json    TEXT NOT NULL,
    prepared_json TEXT NOT NULL,
    total_fee     INTEGER NOT NULL,
    feasible      INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS round_sheet (
    round_id  TEXT NOT NULL REFERENCES print_round(round_id),
    sheet_id  TEXT NOT NULL,
    list_id   TEXT NOT NULL,
    PRIMARY KEY (round_id, sheet_id)
);
CREATE TABLE IF NOT EXISTS receipt (
    round_id     TEXT NOT NULL REFERENCES print_round(round_id),
    sheet_id     TEXT NOT NULL,
    receipt_no   TEXT NOT NULL UNIQUE,
    handler      TEXT NOT NULL,
    reviewer     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (round_id, sheet_id)
);
"""


@dataclass(frozen=True)
class AuthorityExplanation:
    """逐人核验说明：从哪一层继承，或被哪一条禁掉，或全程查不到。"""

    person_id: str
    role: str
    granted: bool
    layer: Optional[str]
    denied_by: Optional[str]
    found: bool


@dataclass(frozen=True)
class SignableList:
    """一次询证冻结下来的完整切面，申请人不看排班也能核验。"""

    list_id: str
    request_no: str
    payment_amount: int
    scheme_id: int
    org_version: int
    auth_edition: int
    commitment_dept: str
    commitment_amount: int
    commitment_spent: int
    commitment_remaining: int
    handlers: tuple[str, ...]
    reviewers: tuple[str, ...]
    explanations: tuple[AuthorityExplanation, ...]

    def candidate(self, sheet_id: str) -> DocCandidate:
        """转成印务室求解器使用的候选结构（冻结候选人不重算授权）。"""
        return DocCandidate(
            sheet_id=sheet_id,
            request_no=self.request_no,
            handlers=self.handlers,
            reviewers=self.reviewers,
        )


def _explain(authorities: tuple[PersonAuthority, ...]) -> tuple[AuthorityExplanation, ...]:
    return tuple(
        AuthorityExplanation(
            person_id=a.person_id,
            role=a.role,
            granted=a.granted,
            layer=a.layer,
            denied_by=a.denied_by,
            found=a.matched is not None,
        )
        for a in authorities
    )


class CountersignService:
    """请付询证服务，数据落在独立 SQLite 文件。"""

    def __init__(
        self,
        db_path: str | Path,
        budget: BudgetService,
        auth: AuthService,
        *,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        self.db_path = str(db_path)
        self.budget = budget
        self.auth = auth
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def inquiry(
        self,
        list_id: str,
        request_no: str,
        payment_amount: int,
        *,
        book: Optional[AuthBook] = None,
    ) -> SignableList:
        """对一张请付单询证，冻结并签发可签名单。

        金额非正、承诺已取消、超过真实剩余、任一道印无人可盖都直接拒绝，
        不写入任何名单（不交空白可签凭证）。
        """
        if (
            not isinstance(payment_amount, int)
            or isinstance(payment_amount, bool)
            or payment_amount <= 0
        ):
            raise InvalidPaymentAmount("请付金额必须为正整数分")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM signable_list WHERE list_id = ?", (list_id,)
            ).fetchone():
                con.commit()
                return self.get_list(list_id)

            scheme = self.budget.active_scheme()
            product = self.budget.org.get_product(scheme.org_version)
            commitment = self.budget.get_commitment(request_no)
            if not commitment.is_open:
                raise CommitmentNotOpen(f"承诺 {request_no} 已取消，不能再请付会签")
            if payment_amount > commitment.remaining:
                raise InsufficientCommitmentRemaining(
                    f"承诺 {request_no} 真实剩余 {commitment.remaining} 分，"
                    f"请付 {payment_amount} 分超限"
                )
            if commitment.dept_id not in product.parent_of:
                raise CommitmentNotOpen(
                    f"承诺部门 {commitment.dept_id} 不在生效组织产品 {product.version} 中"
                )
            ancestors = product.ancestors(commitment.dept_id)
            if book is None:
                book = self.auth.require_latest_book()

            by_role: dict[str, tuple[PersonAuthority, ...]] = {}
            for role in ROLES:
                by_role[role] = resolve_role(
                    book, role, commitment.dept_id, ancestors
                )
            handlers = granted_people(by_role[HANDLER])
            reviewers = granted_people(by_role[REVIEWER])
            missing = []
            if not handlers:
                missing.append("没有可盖经办印的人")
            if not reviewers:
                missing.append("没有可盖复核印的人")
            if missing:
                raise NoSignerAvailable(
                    f"承诺 {request_no} 在组织 v{scheme.org_version} / 授权本 "
                    f"v{book.edition} 下：{'；'.join(missing)}"
                )

            explanations = _explain(by_role[HANDLER]) + _explain(by_role[REVIEWER])
            result = SignableList(
                list_id=list_id,
                request_no=request_no,
                payment_amount=payment_amount,
                scheme_id=scheme.scheme_id,
                org_version=scheme.org_version,
                auth_edition=book.edition if book.edition is not None else 0,
                commitment_dept=commitment.dept_id,
                commitment_amount=commitment.amount,
                commitment_spent=commitment.spent,
                commitment_remaining=commitment.remaining,
                handlers=handlers,
                reviewers=reviewers,
                explanations=explanations,
            )
            con.execute(
                "INSERT INTO signable_list(list_id, request_no, payment_amount, "
                "scheme_id, org_version, auth_edition, commitment_dept, "
                "commitment_amount, commitment_spent, commitment_remaining, "
                "handler_people, reviewer_people, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    list_id, request_no, payment_amount,
                    scheme.scheme_id, scheme.org_version,
                    book.edition if book.edition is not None else 0,
                    commitment.dept_id, commitment.amount, commitment.spent,
                    commitment.remaining,
                    json.dumps(handlers), json.dumps(reviewers),
                    json.dumps(asdict(result), ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            con.commit()
        return result

    def get_list(self, list_id: str) -> SignableList:
        """复读已签出的旧名单；后续授权变化不影响其冻结内容。"""
        with self._connect() as con:
            row = con.execute(
                "SELECT payload_json FROM signable_list WHERE list_id = ?", (list_id,)
            ).fetchone()
        if row is None:
            raise SignableListNotFound(f"可签名单 {list_id} 不存在")
        return _decode_list(row[0])

    def lists(self) -> tuple[SignableList, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload_json FROM signable_list ORDER BY list_id"
            ).fetchall()
        return tuple(_decode_list(r[0]) for r in rows)


def _decode_list(payload: str) -> SignableList:
    data = json.loads(payload)
    data["explanations"] = tuple(AuthorityExplanation(**e) for e in data["explanations"])
    data["handlers"] = tuple(data["handlers"])
    data["reviewers"] = tuple(data["reviewers"])
    return SignableList(**data)


class PrintRoomService:
    """印务室：备印轮次落库、按实际到岗正式登记签单。"""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.db_path = str(db_path)
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def open_round(
        self,
        round_id: str,
        round_input: PrintRound,
        plans: tuple[AbsencePlan, ...],
        plan_result: PrepPlan,
    ) -> None:
        """把一轮业务输入与备印结论整体落库；不可行结论不得开轮。"""
        if not plan_result.feasible:
            failures = "; ".join(
                f"{f.plan_id}: {[b.sheet_id for b in f.blocked]}"
                for f in plan_result.failures
            )
            raise PrintRoomError(f"备印不可行，拒绝开轮（{failures}）")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM print_round WHERE round_id = ?", (round_id,)
            ).fetchone():
                raise PrintRoomError(f"轮次 {round_id} 已开柜")
            con.execute(
                "INSERT INTO print_round(round_id, duty_people, fees_json, "
                "incompat_json, plans_json, prepared_json, total_fee, feasible, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    round_id,
                    json.dumps(list(round_input.duty_people)),
                    json.dumps([[list(pair), fee] for pair, fee in round_input.fees.items()]),
                    json.dumps([list(p) for p in round_input.incompatible_pairs]),
                    json.dumps([[p.plan_id, sorted(p.absent)] for p in plans]),
                    json.dumps([list(pair) for pair in plan_result.prepared]),
                    plan_result.total_fee,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            con.executemany(
                "INSERT INTO round_sheet(round_id, sheet_id, list_id) VALUES (?, ?, ?)",
                [(round_id, s.sheet_id, s.request_no) for s in round_input.sheets],
            )
            con.commit()

    def register_receipts(
        self,
        round_id: str,
        actual_absent: frozenset[str],
        witnesses: tuple[Witness, ...],
        receipt_no_of: dict[str, str],
    ) -> tuple[tuple[str, str, str, str, str], ...]:
        """按实际到岗正式开立一套签单（一个事务内整体校验落库）。

        旧回执重复打开：同回执号同内容直接幂等复读，不二次占印；同号不同
        内容抛 :class:`ReceiptConflict`。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            header = con.execute(
                "SELECT duty_people, prepared_json FROM print_round WHERE round_id = ?",
                (round_id,),
            ).fetchone()
            if header is None:
                raise PrintRoomError(f"轮次 {round_id} 不存在或未开柜")
            duty = set(json.loads(header[0]))
            prepared = {tuple(pair) for pair in json.loads(header[1])}
            incompat = {tuple(pair) for pair in json.loads(
                con.execute(
                    "SELECT incompat_json FROM print_round WHERE round_id = ?",
                    (round_id,),
                ).fetchone()[0]
            )}
            sheet_rows = con.execute(
                "SELECT sheet_id FROM round_sheet WHERE round_id = ? ORDER BY sheet_id",
                (round_id,),
            ).fetchall()
            sheet_ids = {r[0] for r in sheet_rows}

            # 幂等复读：回执号已存在时只允许同内容整批重放。
            existing = {
                r[0]: r for r in con.execute(
                    "SELECT receipt_no, sheet_id, handler, reviewer FROM receipt "
                    "WHERE round_id = ?", (round_id,)
                ).fetchall()
            }
            if existing:
                replay = tuple(
                    sorted(
                        (r[1], r[0], r[2], r[3])  # sheet, receipt, handler, reviewer
                        for r in existing.values()
                    )
                )
                requested = tuple(
                    sorted(
                        (w.sheet_id, receipt_no_of[w.sheet_id], w.handler, w.reviewer)
                        for w in witnesses
                    )
                )
                if replay != requested:
                    raise ReceiptConflict(
                        "轮次已正式登记，旧回执不能以不同内容二次打开"
                    )
                con.commit()
                return self.receipts(round_id)

            if {w.sheet_id for w in witnesses} != sheet_ids:
                raise PrintRoomError("正式登记必须为全批每张单各开一套签单")
            occupied: dict[str, str] = {}
            created_at = datetime.now(timezone.utc).isoformat()
            for w in sorted(witnesses, key=lambda x: x.sheet_id):
                receipt_no = receipt_no_of.get(w.sheet_id)
                if not receipt_no:
                    raise PrintRoomError(f"单 {w.sheet_id} 缺少回执号")
                if w.sheet_id not in sheet_ids:
                    raise PrintRoomError(f"单 {w.sheet_id} 不属于轮次 {round_id}")
                for person, role in ((w.handler, HANDLER), (w.reviewer, REVIEWER)):
                    if person not in duty or person in actual_absent:
                        raise PersonOffDuty(f"{person} 实际未到岗，不能盖 {role} 印")
                    if (person, role) not in prepared:
                        raise StampNotPrepared(f"{person} 的 {role} 印未备，不能再借新章")
                if w.handler == w.reviewer:
                    raise PrintRoomError(f"单 {w.sheet_id} 必须两个人盖全，不能自核")
                if tuple(sorted((w.handler, w.reviewer))) in incompat:
                    raise IncompatibleAssignees(
                        f"单 {w.sheet_id} 的 {w.handler} 与 {w.reviewer} "
                        "在本轮互斥名单上，不能配对"
                    )
                for person in (w.handler, w.reviewer):
                    if person in occupied:
                        raise PersonOccupied(
                            f"{person} 已在单 {occupied[person]} 动过印，"
                            "一人一轮只能动一次"
                        )
                    occupied[person] = w.sheet_id
            con.executemany(
                "INSERT INTO receipt(round_id, sheet_id, receipt_no, handler, reviewer, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (round_id, w.sheet_id, receipt_no_of[w.sheet_id],
                     w.handler, w.reviewer, created_at)
                    for w in sorted(witnesses, key=lambda x: x.sheet_id)
                ],
            )
            con.commit()
        return self.receipts(round_id)

    def receipts(self, round_id: str) -> tuple[tuple[str, str, str, str, str], ...]:
        """已开立签单：(单, 回执号, 经办人, 复核人, 开立时刻)。"""
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT sheet_id, receipt_no, handler, reviewer, created_at "
                    "FROM receipt WHERE round_id = ? ORDER BY sheet_id",
                    (round_id,),
                ).fetchall()
            )
