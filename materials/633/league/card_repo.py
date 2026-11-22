"""逐球卡仓与奖务快照：版本化冻结、关闭重开复算与不可变奖证。

与既有两仓的边界
================
* 本仓持有独立 SQLite 文件，以只读 URI 连接赛程库读取当版主客，
  并从排名事实仓（或显式传入的冻结比分）取一场 played 比赛的终场主客得分。
* 逐球卡卷按 ``(match_id, edition_id)`` 版本化：比分或卡面涂改订正后*另发新版*，
  老版整卷保留、永不更新；request_id 幂等语义与排名仓一致（同号同文复读、
  同号异文拒绝），任何结构非法整卷拒绝、库内无流水。
* 冻结产品与基于它产生的奖务决定/奖证整体 JSON 落快照；之后关账或更换奖务
  登记都不改写旧快照，关闭重开后凭同一份产品可按不同注册簿重新排庭。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .playbyplay import (
    PlayByPlayProduct, PlayByPlayError, build_product, explain,
)
from .awards import AwardDecision, decide_award

MAX_CARDS = 30


class CardBatchRejected(ValueError):
    """逐球卡卷未通过校验；携带原因列表，整卷无副作用。"""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = list(reasons)
        super().__init__(f"card batch rejected: {self.reasons}")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _content_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class PlayByPlayRepository:
    """逐球卡版本仓 + 冻结产品/奖证快照（真实 SQLite，可关闭重开）。"""

    def __init__(self, pb_db_path: str, schedule_db_path: str):
        self.pb_db_path = pb_db_path
        first_time = not os.path.exists(pb_db_path)
        self.conn = sqlite3.connect(pb_db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        uri = "file:" + os.path.abspath(schedule_db_path) + "?mode=ro"
        self.sched = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.sched.row_factory = sqlite3.Row
        if first_time:
            self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE request_log (
                request_id   TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                seq          INTEGER NOT NULL,
                applied_at   TEXT NOT NULL
            );
            -- 逐球卡卷历史：同一 (match, edition) 每版一行，老版永不更新
            CREATE TABLE card_volume (
                volume_version INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id       TEXT NOT NULL,
                edition_id     INTEGER NOT NULL,
                volume_id      TEXT NOT NULL,
                custodian      TEXT NOT NULL,
                status         TEXT NOT NULL,
                final_home     INTEGER NOT NULL,
                final_away     INTEGER NOT NULL,
                payload        TEXT NOT NULL,
                content_hash   TEXT NOT NULL,
                request_id     TEXT NOT NULL,
                seq            INTEGER NOT NULL,
                created_at     TEXT NOT NULL,
                UNIQUE(match_id, edition_id, volume_version)
            );
            -- 冻结解释产品快照：固定卡卷版本 + 事实切面，之后不可改写
            CREATE TABLE product_snapshot (
                snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                volume_version INTEGER NOT NULL,
                request_id     TEXT NOT NULL,
                payload        TEXT NOT NULL,
                report         TEXT NOT NULL,
                created_at     TEXT NOT NULL
            );
            -- 奖务决定/奖证快照：固定冻结产品 + 奖方自己的登记，关账后仍可重算
            CREATE TABLE award_snapshot (
                award_id       TEXT PRIMARY KEY,
                snapshot_id    INTEGER NOT NULL,
                kind           TEXT NOT NULL,
                station_registry TEXT,
                decision       TEXT NOT NULL,
                payload        TEXT NOT NULL,
                created_at     TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------ 赛程只读
    def _edition_teams(self, edition_id: int, match_id: str) -> tuple[str, str]:
        row = self.sched.execute(
            "SELECT home, away FROM edition_match"
            " WHERE edition_id = ? AND match_id = ?",
            (edition_id, match_id),
        ).fetchone()
        if row is None:
            raise CardBatchRejected(
                [f"edition {edition_id} 中不存在 match_id={match_id}"])
        return row["home"], row["away"]

    # ------------------------------------------------------------ 幂等/序号
    def _check_idempotent(self, request_id: str, kind: str,
                          payload: Any) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT kind, content_hash, seq FROM request_log WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        digest = _content_hash(payload)
        if row["kind"] != kind or row["content_hash"] != digest:
            raise CardBatchRejected([
                f"request_id={request_id} 已以不同内容/类型接收"
                f"（已存 kind={row['kind']}），同号异文拒绝"])
        return {"request_id": request_id, "kind": kind,
                "seq": row["seq"], "replayed": True}

    def _next_seq(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM request_log").fetchone()
        return int(row["s"])

    # ------------------------------------------------------------ 卡卷接收
    def submit_card_volume(
        self,
        request_id: str,
        match_id: str,
        edition_id: int,
        roster: Mapping[str, str],
        cards: Sequence[Mapping[str, Any]],
        checkpoints: Sequence[Mapping[str, Any]] | None = None,
        *,
        final_score: tuple[int, int] | None = None,
        frozen_results: Mapping[str, Mapping[str, Any]] | None = None,
        status: str = "played",
        volume_id: str | None = None,
        custodian: str | None = None,
    ) -> dict[str, Any]:
        """原子接收一卷逐球卡并冻结为新版本；老版保留。

        终场比分优先取 ``final_score`` 显式入参，否则从 ``frozen_results`` 中
        该 match 的 active played 事实读取（取消场走 status，不携带比分要求）。
        """
        payload = {
            "match_id": match_id, "edition_id": edition_id,
            "roster": dict(roster), "cards": list(cards),
            "checkpoints": list(checkpoints or []), "status": status,
            "volume_id": volume_id, "custodian": custodian,
            "final_score": list(final_score) if final_score else None,
        }
        replay = self._check_idempotent(request_id, "card_volume", payload)
        if replay is not None:
            return replay

        reasons: list[str] = []
        if len(cards) > MAX_CARDS:
            reasons.append(f"每卷最多 {MAX_CARDS} 张卡，实际 {len(cards)}")
        if not volume_id or not custodian:
            reasons.append("逐球卡卷必须给出具名卷号 volume_id 与实际保管人 custodian")
        home, away = self._edition_teams(edition_id, match_id)

        final_home = final_away = None
        if status == "played":
            if final_score is not None:
                final_home, final_away = final_score
            elif frozen_results and match_id in frozen_results:
                fr = frozen_results[match_id]
                if fr.get("state") != "played":
                    reasons.append(
                        f"冻结事实中 {match_id} state={fr.get('state')}，非 played 不能取终分")
                else:
                    final_home, final_away = fr["score_home"], fr["score_away"]
            else:
                reasons.append(f"未提供 {match_id} 的终场比分且冻结事实中无该场")
        else:
            final_home, final_away = 0, 0  # 取消场占位，解释阶段直接不出奖

        if reasons:
            raise CardBatchRejected(reasons)

        try:
            product = build_product(
                match_id=match_id, edition_id=edition_id,
                home_team=home, away_team=away, roster=roster,
                final_home=int(final_home), final_away=int(final_away),
                cards=cards, checkpoints=checkpoints or [],
                status=status, volume_id=volume_id, custodian=custodian)
        except PlayByPlayError as exc:
            raise CardBatchRejected(exc.args[0]) from exc

        seq = self._next_seq()
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO request_log(request_id, kind, content_hash, seq, applied_at)"
                " VALUES(?, 'card_volume', ?, ?, ?)",
                (request_id, _content_hash(payload), seq, now))
            cur.execute(
                "INSERT INTO card_volume(match_id, edition_id, volume_id, custodian,"
                " status, final_home, final_away, payload, content_hash,"
                " request_id, seq, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (match_id, edition_id, volume_id, custodian, status,
                 product.final_home, product.final_away,
                 json.dumps(product.to_dict(), ensure_ascii=False, sort_keys=True),
                 _content_hash(product.to_dict()), request_id, seq, now))
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            raise CardBatchRejected([f"数据库错误，整卷回滚: {exc}"]) from exc
        version = cur.lastrowid
        return {"request_id": request_id, "kind": "card_volume", "seq": seq,
                "replayed": False, "volume_version": version,
                "match_id": match_id, "edition_id": edition_id,
                "excluded": list(product.excluded)}

    # ------------------------------------------------------------ 版本读取
    def list_versions(self, match_id: str, edition_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT volume_version, match_id, edition_id, volume_id, custodian,"
            " status, final_home, final_away, request_id, seq, created_at"
            " FROM card_volume WHERE match_id = ? AND edition_id = ?"
            " ORDER BY volume_version", (match_id, edition_id))]

    def load_product(self, volume_version: int) -> PlayByPlayProduct:
        row = self.conn.execute(
            "SELECT payload FROM card_volume WHERE volume_version = ?",
            (volume_version,)).fetchone()
        if row is None:
            raise KeyError(f"卡卷版本 {volume_version} 不存在")
        return PlayByPlayProduct.from_dict(json.loads(row["payload"]))

    def latest_version(self, match_id: str, edition_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(volume_version) AS v FROM card_volume"
            " WHERE match_id = ? AND edition_id = ?", (match_id, edition_id)).fetchone()
        return row["v"]

    # ------------------------------------------------------------ 冻结解释快照
    def freeze_interpretation(self, request_id: str,
                              volume_version: int) -> dict[str, Any]:
        """对指定卡卷版本计算解释并整体落快照；重开库后原样可取。"""
        product = self.load_product(volume_version)
        report = explain(product)
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO product_snapshot(volume_version, request_id, payload,"
            " report, created_at) VALUES(?, ?, ?, ?, ?)",
            (volume_version, request_id,
             json.dumps(product.to_dict(), ensure_ascii=False, sort_keys=True),
             json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True), now))
        self.conn.commit()
        return {"snapshot_id": cur.lastrowid, "volume_version": volume_version,
                "feasible": report.feasible,
                "first_dead_card": report.first_dead_card,
                "winning_cards": list(report.winning_cards)}

    def get_product_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload, report FROM product_snapshot WHERE snapshot_id = ?",
            (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"产品快照 {snapshot_id} 不存在")
        return {"product": json.loads(row["payload"]),
                "report": json.loads(row["report"])}

    # ------------------------------------------------------------ 奖证快照
    def register_award(
        self,
        award_id: str,
        snapshot_id: int,
        kind: str,
        station_registry: Mapping[str, str] | None = None,
    ) -> AwardDecision:
        """凭冻结产品快照按奖方登记判定奖项并持久化奖证/待核决定。"""
        snap = self.get_product_snapshot(snapshot_id)
        product = PlayByPlayProduct.from_dict(snap["product"])
        decision = decide_award(award_id, product, kind,
                                dict(station_registry) if station_registry else None)
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                "INSERT INTO award_snapshot(award_id, snapshot_id, kind,"
                " station_registry, decision, payload, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (award_id, snapshot_id, kind,
                 json.dumps(dict(station_registry or {}), ensure_ascii=False, sort_keys=True),
                 decision.decision,
                 json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True), now))
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise CardBatchRejected([f"奖项 {award_id} 已登记，奖证不可重复签发"]) from exc
        return decision

    def get_award(self, award_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload FROM award_snapshot WHERE award_id = ?",
            (award_id,)).fetchone()
        if row is None:
            raise KeyError(f"奖项 {award_id} 不存在")
        return json.loads(row["payload"])

    def list_awards(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM award_snapshot ORDER BY award_id")]

    def request_log(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT request_id, kind, content_hash, seq, applied_at"
            " FROM request_log ORDER BY seq")]

    def inspect(self) -> dict[str, Any]:
        return {
            "requests": self.request_log(),
            "volumes": [dict(r) for r in self.conn.execute(
                "SELECT volume_version, match_id, edition_id, volume_id,"
                " custodian, status, request_id, seq FROM card_volume"
                " ORDER BY volume_version")],
            "product_snapshots": [dict(r) for r in self.conn.execute(
                "SELECT snapshot_id, volume_version, request_id"
                " FROM product_snapshot ORDER BY snapshot_id")],
            "awards": self.list_awards(),
        }

    def close(self) -> None:
        self.conn.close()
        self.sched.close()

    def __enter__(self) -> "PlayByPlayRepository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
