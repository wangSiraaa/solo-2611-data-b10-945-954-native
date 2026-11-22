"""流转凭单封存仓（独立 SQLite，第三个物理分离的库）。

与组织库、预算库并列但互不建外键：本仓只保存**封存时刻的凭单快照**。
封存后任何换版、付清、补订都不会回写已封存凭单——读取时直接还原快照
并跑一次 :meth:`FlowVoucher.verify`，若落库内容被篡改会抛出
:class:`VoucherVerifyError`。

同号封存是幂等的：相同编制输入（同一对版本、同一组栏头、同一次实账
切面）重试直接返回原凭单；同号不同输入抛 :class:`VoucherConflict`，
已封存内容不受影响。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .errors import VoucherConflict, VoucherNotFound, VoucherVerifyError
from .flow_voucher import FlowVoucher, OccupancyFact, build_voucher
from .org_service import OrgService

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sealed_voucher (
    voucher_no          TEXT PRIMARY KEY,
    old_version         INTEGER NOT NULL,
    new_version         INTEGER NOT NULL,
    scheme_id           INTEGER NOT NULL,
    old_headers_json    TEXT NOT NULL,
    new_headers_json    TEXT NOT NULL,
    total_width         INTEGER NOT NULL CHECK (total_width > 0),
    sealed_at           TEXT NOT NULL,
    input_fingerprint   TEXT NOT NULL,
    payload_json        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voucher_line (
    voucher_no    TEXT NOT NULL REFERENCES sealed_voucher(voucher_no),
    request_no    TEXT NOT NULL,
    leaf_id       TEXT NOT NULL,
    amount        INTEGER NOT NULL CHECK (amount > 0),
    old_header    TEXT NOT NULL,
    new_header    TEXT NOT NULL,
    old_chain_json TEXT NOT NULL,
    new_chain_json TEXT NOT NULL,
    PRIMARY KEY (voucher_no, request_no)
);
CREATE TABLE IF NOT EXISTS voucher_band (
    voucher_no    TEXT NOT NULL REFERENCES sealed_voucher(voucher_no),
    old_header    TEXT NOT NULL,
    new_header    TEXT NOT NULL,
    width         INTEGER NOT NULL CHECK (width > 0),
    request_nos_json TEXT NOT NULL,
    PRIMARY KEY (voucher_no, old_header, new_header)
);
"""


def _fingerprint(
    old_version: int,
    new_version: int,
    old_headers: tuple[str, ...],
    new_headers: tuple[str, ...],
    occupancy: tuple[OccupancyFact, ...],
) -> str:
    """编制输入指纹：版本 + 栏头 + 整切面占额事实的规范化哈希。

    同号重试时用它判断“是否同一次编制”；切面内任何一笔占额变化都会改指纹。
    """
    body = {
        "old_version": old_version,
        "new_version": new_version,
        "old_headers": list(old_headers),
        "new_headers": list(new_headers),
        "occupancy": [
            {
                "request_no": f.request_no,
                "dept_id": f.dept_id,
                "amount": f.amount,
                "status": f.status,
                "spent": f.spent,
                "remaining": f.remaining,
                "occupancy": f.occupancy,
            }
            for f in sorted(occupancy, key=lambda f: f.request_no)
        ],
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class VoucherService:
    """流转凭单封存仓。每个实例持有独立 SQLite 文件。"""

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
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def seal_voucher(
        self,
        voucher_no: str,
        old_version: int,
        new_version: int,
        old_headers: tuple[str, ...] | list[str],
        new_headers: tuple[str, ...] | list[str],
        occupancy: tuple[OccupancyFact, ...] | list[OccupancyFact],
        scheme_id: Optional[int] = None,
    ) -> FlowVoucher:
        """从实账切面编制并封存一张流转凭单（同号幂等）。

        ``scheme_id`` 为 None 时锁定预算仓当前生效方案；两个组织版本均
        必须是组织服务中真实发布的产品。编制失败（缺覆盖 / 多归等）不
        会留下任何封存行。
        """
        old_headers = tuple(old_headers)
        new_headers = tuple(new_headers)
        occupancy = tuple(occupancy)
        fingerprint = _fingerprint(
            old_version, new_version, old_headers, new_headers, occupancy
        )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT input_fingerprint FROM sealed_voucher WHERE voucher_no = ?",
                (voucher_no,),
            ).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    raise VoucherConflict(
                        f"凭单号 {voucher_no} 已按另一次编制输入封存，"
                        "已封存凭单不可改写"
                    )
                con.commit()
                return self.get_voucher(voucher_no)

            old_product = self.org.get_product(old_version)
            new_product = self.org.get_product(new_version)
            if scheme_id is None:
                scheme_id = self._active_scheme_id()
            sealed_at = datetime.now(timezone.utc).isoformat()
            # build_voucher 抛 VoucherHandOffError 时事务随 with 回滚，不留痕。
            voucher = build_voucher(
                voucher_no=voucher_no,
                old_product=old_product,
                new_product=new_product,
                old_headers=old_headers,
                new_headers=new_headers,
                occupancy=occupancy,
                scheme_id=int(scheme_id),
                sealed_at=sealed_at,
                as_of_fingerprint=fingerprint,
            )
            payload = voucher.to_dict()
            con.execute(
                "INSERT INTO sealed_voucher(voucher_no, old_version, new_version, "
                "scheme_id, old_headers_json, new_headers_json, total_width, "
                "sealed_at, input_fingerprint, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    voucher.voucher_no,
                    voucher.old_version,
                    voucher.new_version,
                    voucher.scheme_id,
                    json.dumps(list(voucher.old_headers), ensure_ascii=False),
                    json.dumps(list(voucher.new_headers), ensure_ascii=False),
                    voucher.total_width,
                    voucher.sealed_at,
                    fingerprint,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            con.executemany(
                "INSERT INTO voucher_line(voucher_no, request_no, leaf_id, amount, "
                "old_header, new_header, old_chain_json, new_chain_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        voucher.voucher_no,
                        ln.request_no,
                        ln.leaf_id,
                        ln.amount,
                        ln.old_header,
                        ln.new_header,
                        json.dumps(list(ln.old_ancestors), ensure_ascii=False),
                        json.dumps(list(ln.new_ancestors), ensure_ascii=False),
                    )
                    for ln in voucher.lines
                ],
            )
            con.executemany(
                "INSERT INTO voucher_band(voucher_no, old_header, new_header, "
                "width, request_nos_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        voucher.voucher_no,
                        b.old_header,
                        b.new_header,
                        b.width,
                        json.dumps(list(b.request_nos), ensure_ascii=False),
                    )
                    for b in voucher.bands
                ],
            )
            con.commit()
        return self.get_voucher(voucher_no)

    def get_voucher(self, voucher_no: str) -> FlowVoucher:
        """还原封存凭单并立即自验；封存内容被破坏即 VoucherVerifyError。"""
        with self._connect() as con:
            row = con.execute(
                "SELECT payload_json FROM sealed_voucher WHERE voucher_no = ?",
                (voucher_no,),
            ).fetchone()
        if row is None:
            raise VoucherNotFound(f"封存凭单 {voucher_no} 不存在")
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise VoucherVerifyError(f"凭单 {voucher_no} 载荷损坏: {exc}") from exc
        return FlowVoucher.from_dict(payload)

    def voucher_numbers(self) -> tuple[str, ...]:
        with self._connect() as con:
            return tuple(
                r[0]
                for r in con.execute(
                    "SELECT voucher_no FROM sealed_voucher ORDER BY voucher_no"
                ).fetchall()
            )

    def _active_scheme_id(self) -> int:
        # 不直接打开预算库（物理分离）：方案 id 由编制员显式传入更稳妥，
        # 缺省时给出明确错误，避免凭单仓对预算仓产生隐式连接依赖。
        raise ValueError(
            "封存凭单必须显式指定锁定的预算方案 scheme_id（同一次实账切面）"
        )
