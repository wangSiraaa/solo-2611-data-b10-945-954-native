"""迁编流转凭单的纯函数编制（无任何 I/O，可独立重算）。

编制员锁定：

* 两个**真实发布**的组织产品版本（调用方负责保证版本来自
  :meth:`OrgService.get_product`，本模块只消费不可变产品对象）；
* 各自一组**互不为祖先**的栏头部门；
* 同一次预算实账切面（一组 :class:`OccupancyFact`，由预算执行仓在指定
  方案 / 时刻点整体快照，本模块不直接读预算库）。

规则：

* 叶承诺名下的占额 = 已付 + 仍开立（OPEN）的未付部分；已付后取消只留下
  已付；``request_no`` 是承诺的原始身份，迁部不改名、不改挂。
* 对每笔**正占额**，按原叶身份在两版各找**唯一**覆盖它的栏头（栏头自身
  或其祖先中的栏头）；缺一端或一端多归都列为“不能交版”，并指出原单。
* 按 旧栏 → 新栏 汇总有向带宽；每笔明细保留原请求号、金额及两条祖先链。
* 凭单必须能自验入出合计：每个栏头的入/出带宽 = 落到该栏头的明细之和，
  两侧总带宽相等。

本模块与 :mod:`orgbudget.plate` 一样不接触数据库，凭单的 JSON 载荷
（:meth:`FlowVoucher.to_dict`）就是印厂消费端唯一需要的交接物。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .errors import VoucherVerifyError

if TYPE_CHECKING:  # 纯函数模块仅在类型检查时引用组织产品，运行时零依赖。
    from .org_service import OrgProduct

MAX_COLUMNS_PER_SIDE = 7


@dataclass(frozen=True)
class OccupancyFact:
    """预算实账切面上一笔承诺（叶名下）的占额事实。

    ``spent`` 为已实际支出的分；OPEN 承诺的 ``remaining`` 是仍开立的
    未付部分，CANCELLED 承诺该值为 0（取消只释放剩余，已付保留）。
    """

    request_no: str
    dept_id: str
    amount: int
    status: str
    spent: int
    remaining: int

    @property
    def occupancy(self) -> int:
        """已付 + 仍开立未付；已付后取消只留下已付。"""
        open_remaining = self.remaining if self.status == "OPEN" else 0
        return self.spent + open_remaining


@dataclass(frozen=True)
class VoucherLine:
    """一条经费带明细：单笔原始承诺在两版栏头间的流向。"""

    request_no: str
    leaf_id: str
    amount: int
    old_header: str
    new_header: str
    old_ancestors: tuple[str, ...]
    new_ancestors: tuple[str, ...]


@dataclass(frozen=True)
class FlowBand:
    """旧栏 → 新栏 的汇总有向带宽（宽度为正整数分）。"""

    old_header: str
    new_header: str
    width: int
    request_nos: tuple[str, ...]


@dataclass(frozen=True)
class VoucherProblem:
    """一条不能交版记录：问题码、原请求号（栏头级问题可为 None）、说明。"""

    code: str
    request_no: Optional[str]
    message: str


@dataclass(frozen=True)
class FlowVoucher:
    """封存的迁编流转凭单（不可变）。

    凭单只记录封存时刻的事实快照：后来换版、付清或补订都不影响本对象。
    ``scheme_id`` 记录锁定的预算实账切面（预算方案 id），``as_of`` 是
    封存时间戳；二者仅供追溯，重算只依赖 ``lines`` 等明细字段。
    """

    voucher_no: str
    old_version: int
    new_version: int
    scheme_id: int
    old_headers: tuple[str, ...]
    new_headers: tuple[str, ...]
    lines: tuple[VoucherLine, ...]
    bands: tuple[FlowBand, ...]
    total_width: int
    sealed_at: str
    as_of_fingerprint: str = ""

    # ---------------------------------------------------------------- 自验
    def verify(self) -> None:
        """按凭单自身明细重算并核对全部守恒与结构不变量。

        消费端（印厂）拿到 JSON 载荷后可直接调用本方法，无需任何数据库。
        """
        problems: list[str] = []
        if self.total_width <= 0:
            problems.append("凭单总带宽必须为正")
        if not self.old_headers or not self.new_headers:
            problems.append("两侧栏头均不能为空")
        if len(set(self.old_headers)) != len(self.old_headers):
            problems.append("旧栏头存在重复")
        if len(set(self.new_headers)) != len(self.new_headers):
            problems.append("新栏头存在重复")

        line_total = 0
        old_in: dict[str, int] = {}
        new_in: dict[str, int] = {}
        seen_requests: set[str] = set()
        for line in self.lines:
            if line.amount <= 0:
                problems.append(f"明细 {line.request_no} 金额非正")
            if line.request_no in seen_requests:
                problems.append(f"请求号 {line.request_no} 在凭单中重复")
            seen_requests.add(line.request_no)
            if line.old_header not in self.old_headers:
                problems.append(f"明细 {line.request_no} 的旧栏 {line.old_header} 不在栏头清单")
            if line.new_header not in self.new_headers:
                problems.append(f"明细 {line.request_no} 的新栏 {line.new_header} 不在栏头清单")
            line_total += line.amount
            old_in[line.old_header] = old_in.get(line.old_header, 0) + line.amount
            new_in[line.new_header] = new_in.get(line.new_header, 0) + line.amount

        if line_total != self.total_width:
            problems.append(f"明细合计 {line_total} != 凭单总带宽 {self.total_width}")
        if sum(old_in.values()) != self.total_width or sum(new_in.values()) != self.total_width:
            problems.append("入 / 出合计与总带宽不一致")

        # 重算汇总带宽，逐格比对封存值。
        rebuilt: dict[tuple[str, str], list[object]] = {}
        for line in self.lines:
            bucket = rebuilt.setdefault((line.old_header, line.new_header), [0, []])
            bucket[0] = int(bucket[0]) + line.amount
            bucket[1].append(line.request_no)  # type: ignore[union-attr]
        rebuilt_bands = {
            key: (width, tuple(sorted(reqs))) for key, (width, reqs) in rebuilt.items()
        }
        stored_bands = {
            (b.old_header, b.new_header): (b.width, tuple(sorted(b.request_nos)))
            for b in self.bands
        }
        if rebuilt_bands != stored_bands:
            problems.append("汇总带宽与明细重算结果不一致")
        if sum(b.width for b in self.bands) != self.total_width:
            problems.append("带宽表合计与总带宽不一致")
        if problems:
            raise VoucherVerifyError("；".join(problems))

    # ---------------------------------------------------------------- 交接载荷
    def to_dict(self) -> dict:
        """可直接 json.dumps 的纯字典载荷（印厂消费端唯一交接物）。"""
        return {
            "voucher_no": self.voucher_no,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "scheme_id": self.scheme_id,
            "old_headers": list(self.old_headers),
            "new_headers": list(self.new_headers),
            "total_width": self.total_width,
            "sealed_at": self.sealed_at,
            "as_of_fingerprint": self.as_of_fingerprint,
            "lines": [
                {
                    "request_no": ln.request_no,
                    "leaf_id": ln.leaf_id,
                    "amount": ln.amount,
                    "old_header": ln.old_header,
                    "new_header": ln.new_header,
                    "old_ancestors": list(ln.old_ancestors),
                    "new_ancestors": list(ln.new_ancestors),
                }
                for ln in self.lines
            ],
            "bands": [
                {
                    "old_header": b.old_header,
                    "new_header": b.new_header,
                    "width": b.width,
                    "request_nos": list(b.request_nos),
                }
                for b in self.bands
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "FlowVoucher":
        """从交接字典重建凭单并立即自验；结构非法即抛 VoucherVerifyError。"""
        try:
            lines = tuple(
                VoucherLine(
                    request_no=ln["request_no"],
                    leaf_id=ln["leaf_id"],
                    amount=int(ln["amount"]),
                    old_header=ln["old_header"],
                    new_header=ln["new_header"],
                    old_ancestors=tuple(ln["old_ancestors"]),
                    new_ancestors=tuple(ln["new_ancestors"]),
                )
                for ln in payload["lines"]
            )
            bands = tuple(
                FlowBand(
                    old_header=b["old_header"],
                    new_header=b["new_header"],
                    width=int(b["width"]),
                    request_nos=tuple(b["request_nos"]),
                )
                for b in payload["bands"]
            )
            voucher = cls(
                voucher_no=payload["voucher_no"],
                old_version=int(payload["old_version"]),
                new_version=int(payload["new_version"]),
                scheme_id=int(payload["scheme_id"]),
                old_headers=tuple(payload["old_headers"]),
                new_headers=tuple(payload["new_headers"]),
                lines=lines,
                bands=bands,
                total_width=int(payload["total_width"]),
                sealed_at=payload["sealed_at"],
                as_of_fingerprint=payload.get("as_of_fingerprint", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VoucherVerifyError(f"凭单载荷结构非法: {exc}") from exc
        voucher.verify()
        return voucher


# ---------------------------------------------------------------- 编制
def _unique_covering_header(
    leaf_id: str, headers: tuple[str, ...], product: "OrgProduct"
) -> tuple[Optional[str], Optional[str]]:
    """在一版产品中找唯一覆盖叶的栏头。

    覆盖 = 栏头是叶自身或叶的祖先。返回 (栏头, 问题码)；唯一命中时
    问题码为 None，缺归返回 (None, 'MISSING')，多归返回 (None, 'MULTIPLE')。
    """
    if leaf_id not in product.parent_of:
        return None, "LEAF_GONE"
    ancestor_chain = set(product.ancestors(leaf_id)) | {leaf_id}
    hits = [h for h in headers if h in ancestor_chain]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "MISSING"
    return None, "MULTIPLE"


def _assert_antichain(
    headers: tuple[str, ...], product: "OrgProduct", side: str
) -> list[VoucherProblem]:
    problems: list[VoucherProblem] = []
    for header in headers:
        if not product.contains(header):
            problems.append(
                VoucherProblem("HEADER_MISSING", None,
                               f"{side}栏头 {header} 不在组织产品 v{product.version} 中")
            )
    # 任两栏头互不为祖先：一个栏头是另一个栏头的祖先即违反。
    present = [h for h in headers if product.contains(h)]
    for i, head in enumerate(present):
        head_ancestors = set(product.ancestors(head))
        for other in present[i + 1:]:
            other_ancestors = set(product.ancestors(other))
            if other in head_ancestors or head in other_ancestors:
                problems.append(
                    VoucherProblem("HEADER_ANCESTRY", None,
                                   f"{side}栏头 {head} 与 {other} 互为祖先，不满足互不为祖先")
                )
    return problems


def build_voucher(
    voucher_no: str,
    old_product: "OrgProduct",
    new_product: "OrgProduct",
    old_headers: tuple[str, ...] | list[str],
    new_headers: tuple[str, ...] | list[str],
    occupancy: tuple[OccupancyFact, ...] | list[OccupancyFact],
    scheme_id: int,
    sealed_at: str,
    as_of_fingerprint: str = "",
) -> FlowVoucher:
    """从实账切面与两个已发布产品编制流转凭单。

    任一“不能交版”问题都不会部分产出：问题聚合成
    :class:`~orgbudget.errors.VoucherHandOffError` 一次性抛出，逐条指出
    问题码、原请求号与说明。
    """
    old_headers = tuple(old_headers)
    new_headers = tuple(new_headers)
    problems: list[VoucherProblem] = []

    if old_product.version is None or new_product.version is None:
        raise ValueError("凭单只能基于已发布（带版本号）的组织产品编制")
    if not old_headers or not new_headers:
        raise ValueError("两侧栏头均不能为空")
    if len(old_headers) > MAX_COLUMNS_PER_SIDE or len(new_headers) > MAX_COLUMNS_PER_SIDE:
        raise ValueError(f"每侧栏头至多 {MAX_COLUMNS_PER_SIDE} 栏")
    if len(set(old_headers)) != len(old_headers) or len(set(new_headers)) != len(new_headers):
        raise ValueError("同一侧栏头不能重复")
    if old_product.version == new_product.version:
        raise ValueError("迁编凭单必须指定两个不同的组织版本")

    problems.extend(_assert_antichain(old_headers, old_product, "旧"))
    problems.extend(_assert_antichain(new_headers, new_product, "新"))

    # 只对正占额逐笔做双端唯一覆盖；零占额承诺不带经费，不进凭单。
    lines: list[VoucherLine] = []
    for fact in sorted(occupancy, key=lambda f: f.request_no):
        if fact.occupancy <= 0:
            continue
        old_hit, old_code = _unique_covering_header(fact.dept_id, old_headers, old_product)
        new_hit, new_code = _unique_covering_header(fact.dept_id, new_headers, new_product)
        if old_code is not None or new_code is not None:
            if old_code == "LEAF_GONE" or new_code == "LEAF_GONE":
                problems.append(VoucherProblem(
                    "LEAF_GONE", fact.request_no,
                    f"原叶 {fact.dept_id}（{fact.request_no}）在某一版产品中已不存在"))
            if old_code in ("MISSING", "MULTIPLE"):
                problems.append(VoucherProblem(
                    old_code, fact.request_no,
                    f"原叶 {fact.dept_id}（{fact.request_no}）在旧版 v{old_product.version} "
                    f"{'无' if old_code == 'MISSING' else '有多个'}栏头覆盖"))
            if new_code in ("MISSING", "MULTIPLE"):
                problems.append(VoucherProblem(
                    new_code, fact.request_no,
                    f"原叶 {fact.dept_id}（{fact.request_no}）在新版 v{new_product.version} "
                    f"{'无' if new_code == 'MISSING' else '有多个'}栏头覆盖"))
            continue
        lines.append(VoucherLine(
            request_no=fact.request_no,
            leaf_id=fact.dept_id,
            amount=fact.occupancy,
            old_header=old_hit,  # type: ignore[arg-type]
            new_header=new_hit,  # type: ignore[arg-type]
            old_ancestors=old_product.ancestors(fact.dept_id),
            new_ancestors=new_product.ancestors(fact.dept_id),
        ))

    if problems:
        from .errors import VoucherHandOffError

        raise VoucherHandOffError([(p.code, p.request_no, p.message) for p in problems])
    if not lines:
        raise ValueError("切面内没有任何正占额，无可编制的经费带")

    # 旧栏 → 新栏 汇总有向带宽，保留带宽内全部原始请求号。
    band_map: dict[tuple[str, str], list[object]] = {}
    for line in lines:
        bucket = band_map.setdefault((line.old_header, line.new_header), [0, []])
        bucket[0] = int(bucket[0]) + line.amount
        bucket[1].append(line.request_no)  # type: ignore[union-attr]
    bands = tuple(
        FlowBand(old_header=o, new_header=n, width=int(width),
                 request_nos=tuple(sorted(reqs)))
        for (o, n), (width, reqs) in sorted(band_map.items())
    )
    total_width = sum(b.width for b in bands)
    voucher = FlowVoucher(
        voucher_no=voucher_no,
        old_version=int(old_product.version),
        new_version=int(new_product.version),
        scheme_id=scheme_id,
        old_headers=tuple(sorted(old_headers)),
        new_headers=tuple(sorted(new_headers)),
        lines=tuple(lines),
        bands=bands,
        total_width=total_width,
        sealed_at=sealed_at,
        as_of_fingerprint=as_of_fingerprint,
    )
    voucher.verify()
    return voucher
