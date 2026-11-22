"""会签授权本服务（独立 SQLite 库）。

行政在工作副本上对任意部门的“角色、人”登记授予或禁止；同一位置
（部门、角色、人）只保留一项，重复登记即覆盖。``publish_edition`` 把
工作副本整体冻结为一版不可变授权本，版本号严格递增，旧版永不改写。

授权本不绑定具体组织版本：询证时按当时生效预算方案绑定的组织产品
取叶到根路径解析。总部行政改了树而预算换版尚未获准时，生效方案仍绑
旧组织产品，解析仍沿旧树，天然不会提前换用新印。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .auth import AuthBook, AuthEntry, DENY, GRANT, ROLES
from .errors import AuthEditionNotFound, InvalidRole

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_edition (
    edition      INTEGER PRIMARY KEY,
    published_at TEXT NOT NULL,
    note         TEXT
);
-- 已发布切面：一版内 (部门, 角色, 人) 唯一。
CREATE TABLE IF NOT EXISTS auth_entry (
    edition       INTEGER NOT NULL REFERENCES auth_edition(edition),
    dept_id       TEXT NOT NULL,
    role          TEXT NOT NULL,
    person_id     TEXT NOT NULL,
    effect        TEXT NOT NULL CHECK (effect IN ('GRANT', 'DENY')),
    PRIMARY KEY (edition, dept_id, role, person_id)
);
CREATE INDEX IF NOT EXISTS idx_auth_entry_person
    ON auth_entry(edition, person_id, role);
-- 工作副本：尚未发布的登记，位置唯一即“某版同一位置只许一项”。
CREATE TABLE IF NOT EXISTS auth_staging (
    dept_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    person_id  TEXT NOT NULL,
    effect     TEXT NOT NULL CHECK (effect IN ('GRANT', 'DENY')),
    PRIMARY KEY (dept_id, role, person_id)
);
"""


class AuthService:
    """授权本服务。独立 SQLite 文件，与组织库、预算库物理分离。"""

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

    # ------------------------------------------------------------ 工作副本登记

    def register(
        self, department_id: str, role: str, person_id: str, effect: str
    ) -> None:
        """在工作副本登记一项授予/禁止；同位置重复登记直接覆盖。"""
        if role not in ROLES:
            raise InvalidRole(f"非法角色 {role!r}，只允许 {ROLES}")
        if effect not in (GRANT, DENY):
            raise ValueError(f"非法授权效果 {effect!r}，只允许 GRANT / DENY")
        if not department_id or not person_id:
            raise ValueError("部门 ID 与人 ID 均不能为空")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO auth_staging(dept_id, role, person_id, effect) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(dept_id, role, person_id) DO UPDATE SET effect = excluded.effect",
                (department_id, role, person_id, effect),
            )
            con.commit()

    def grant(self, department_id: str, role: str, person_id: str) -> None:
        self.register(department_id, role, person_id, GRANT)

    def deny(self, department_id: str, role: str, person_id: str) -> None:
        self.register(department_id, role, person_id, DENY)

    def staging_entries(self) -> tuple[AuthEntry, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT dept_id, role, person_id, effect FROM auth_staging "
                "ORDER BY dept_id, role, person_id"
            ).fetchall()
        return tuple(AuthEntry(*row) for row in rows)

    # ---------------------------------------------------------------- 发布

    def publish_edition(self, note: Optional[str] = None) -> AuthBook:
        """把工作副本冻结为新版不可变授权本（即使为空也允许发布）。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT COALESCE(MAX(edition), 0) FROM auth_edition"
            ).fetchone()[0]
            edition = current + 1
            published_at = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO auth_edition(edition, published_at, note) VALUES (?, ?, ?)",
                (edition, published_at, note),
            )
            rows = con.execute(
                "SELECT dept_id, role, person_id, effect FROM auth_staging"
            ).fetchall()
            con.executemany(
                "INSERT INTO auth_entry(edition, dept_id, role, person_id, effect) "
                "VALUES (?, ?, ?, ?, ?)",
                [(edition, *row) for row in rows],
            )
            con.commit()
        return self.get_book(edition)

    def get_book(self, edition: int) -> AuthBook:
        with self._connect() as con:
            head = con.execute(
                "SELECT published_at FROM auth_edition WHERE edition = ?",
                (edition,),
            ).fetchone()
            if head is None:
                raise AuthEditionNotFound(f"授权本版本 {edition} 不存在")
            rows = con.execute(
                "SELECT dept_id, role, person_id, effect FROM auth_entry WHERE edition = ?",
                (edition,),
            ).fetchall()
        return AuthBook(
            edition=edition,
            entries=tuple(AuthEntry(*row) for row in rows),
            published_at=head[0],
        )

    @property
    def current_edition(self) -> Optional[int]:
        with self._connect() as con:
            value = con.execute(
                "SELECT COALESCE(MAX(edition), 0) FROM auth_edition"
            ).fetchone()[0]
        return value or None

    def require_latest_book(self) -> AuthBook:
        """取最新版授权本；从未发布则报 AuthEditionNotFound（全部无权）。"""
        edition = self.current_edition
        if edition is None:
            raise AuthEditionNotFound("授权本尚未发布任何版本")
        return self.get_book(edition)

    def editions(self) -> tuple[tuple[int, str, Optional[str]], ...]:
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT edition, published_at, note FROM auth_edition ORDER BY edition"
                ).fetchall()
            )
