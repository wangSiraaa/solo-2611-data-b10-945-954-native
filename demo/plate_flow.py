"""馆刊对页经费带：迁编流转凭单与印厂排位工单的端到端演示。

运行：

    python -m demo.plate_flow

每次运行先清空 demo_data/plate_flow/，从三个物理分离的 SQLite 库从零
建立事实，复跑题面要求的三张工单，并核一笔“只看栏合计无法定位走向”
的迁编：

* 工单 1：样张四承诺 2/1/1/2，左栏 A 在 B 前、右栏自由，隔墨预算 3，
  右排 X、Y 耗 1，签出；
* 工单 2：ay 另开一笔 4 的真实承诺，从同一新版关系开新凭单，右排
  Y、X 最低耗 4，超预算退单；
* 工单 3：保持原凭单，仅指定右栏 Y 必须在 X 前，同样最低 4、退单，
  交带清单与工单 1 的自由排位不同；
* 走向核对：构造两侧栏合计完全相同、但逐笔请求去向不同的两张凭单，
  证明对页上的合计数字本身无法定位经费带，必须读凭单明细。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from orgbudget import (
    BudgetService,
    OrgService,
    PlateConstraints,
    SideConstraints,
    VoucherService,
    issue_work_order_from_payload,
)
from orgbudget.errors import VoucherHandOffError

DATA_DIR = Path(__file__).resolve().parent.parent / "demo_data" / "plate_flow"


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    mark = "[通过]" if ok else "[失败]"
    print(f"  {mark} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def _build_services() -> tuple[OrgService, BudgetService, VoucherService]:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)
    org = OrgService(DATA_DIR / "org.db")
    budget = BudgetService(DATA_DIR / "budget.db", org)
    vault = VoucherService(DATA_DIR / "voucher.db", org)
    return org, budget, vault


def _print_order(order) -> None:
    print(f"  状态={order.status}  左排={list(order.left_order)}  "
          f"右排={list(order.right_order)}  隔墨耗点={order.ink_dots}/"
          f"预算 {order.ink_dot_budget}")
    print(f"  说明: {order.reason}")
    for crossing in order.crossings:
        flag = "交" if crossing.crossed else "不交"
        print(f"    [{flag}] {crossing.band_a} × {crossing.band_b}  "
              f"宽 {crossing.width_a}×{crossing.width_b} -> {crossing.ink_dots} 点"
              f"（{crossing.reason}）")


def main() -> None:
    org, budget, vault = _build_services()

    # ------------------------------------------------ 两版组织产品
    _line("准备：两个真实发布的组织版本（互不为祖先的栏头 A/B 与 X/Y）")
    org.create_department("HQ", None)
    for dept, parent in [("A", "HQ"), ("B", "HQ"), ("X", "HQ"), ("Y", "HQ")]:
        org.create_department(dept, parent)
    for leaf, parent in [("ax", "A"), ("ay", "A"), ("bx", "B"), ("by", "B")]:
        org.create_department(leaf, parent)
    v1 = org.publish("迁编前：ax/ay 在 A，bx/by 在 B").version
    for leaf, parent in [("ax", "X"), ("ay", "Y"), ("bx", "X"), ("by", "Y")]:
        org.move_subtree(leaf, parent)
    v2 = org.publish("迁编后：ax/bx 在 X，ay/by 在 Y").version
    p1, p2 = org.get_product(v1), org.get_product(v2)
    print(f"  旧版 v{v1} 栏头 A、B；新版 v{v2} 栏头 X、Y（两组各自互不为祖先）")
    print(f"  ax 双祖先链：旧 {' -> '.join(p1.ancestors('ax'))}；"
          f"新 {' -> '.join(p2.ancestors('ax'))}")

    # 旧入口开承诺 2/1/1/2（在有足够预算的叶上）。
    weights = {(d, k): 1 for d in p1.department_ids for k in p1.children(d)}
    scheme = budget.register_root_budget(v1, 100_000, weights)
    for req, leaf, amount in [
        ("REQ-ax", "ax", 2), ("REQ-ay", "ay", 1),
        ("REQ-bx", "bx", 1), ("REQ-by", "by", 2),
    ]:
        budget.create_commitment(req, leaf, amount)
    print("  经旧版入口在 ax/ay/bx/by 开立承诺 2/1/1/2（请求号迁部不改名）")

    # ------------------------------------------------ 第一张凭单与工单 1
    _line("工单 1：原切面凭单，左 A 在 B 前、右栏自由，隔墨预算 3")
    facts = budget.occupancy_snapshot(scheme.scheme_id)
    voucher1 = vault.seal_voucher(
        "VCH-1", v1, v2, ("A", "B"), ("X", "Y"), facts, scheme.scheme_id
    )
    print(f"  封存凭单 VCH-1：总带宽 {voucher1.total_width}，"
          f"带宽格 {[(b.old_header, b.new_header, b.width) for b in voucher1.bands]}")
    _check("凭单入出合计守恒（读凭单即可验证）",
           sum(b.width for b in voucher1.bands) == voucher1.total_width == 6)
    order1 = issue_work_order_from_payload(
        voucher1.to_dict(),
        PlateConstraints(left=SideConstraints(before=(("A", "B"),)),
                         ink_dot_budget=3),
    )
    _print_order(order1)
    _check("右排 X、Y 耗 1 且签出",
           order1.is_signed and order1.right_order == ("X", "Y")
           and order1.ink_dots == 1)
    (DATA_DIR / "work_order_1.json").write_text(
        order1.to_json(), encoding="utf-8"
    )

    # ------------------------------------------------ 工单 2：ay 补订 4
    _line("工单 2：ay 另开一笔 4 的真实承诺，同一新版关系开新凭单")
    budget.create_commitment("REQ-ay-2", "ay", 4)
    facts2 = budget.occupancy_snapshot(scheme.scheme_id)
    voucher2 = vault.seal_voucher(
        "VCH-2", v1, v2, ("A", "B"), ("X", "Y"), facts2, scheme.scheme_id
    )
    print(f"  新凭单 VCH-2 总带宽 {voucher2.total_width}；"
          f"A->Y 带宽 {next(b.width for b in voucher2.bands if b.old_header == 'A' and b.new_header == 'Y')}")
    # 原凭单仍是封存时的 6。
    _check("原凭单 VCH-1 不受补订影响", vault.get_voucher("VCH-1").total_width == 6)
    order2 = issue_work_order_from_payload(
        voucher2.to_dict(),
        PlateConstraints(left=SideConstraints(before=(("A", "B"),)),
                         ink_dot_budget=3),
    )
    _print_order(order2)
    _check("右排 Y、X 最低耗 4，超预算 1 点退单",
           (not order2.is_signed) and order2.right_order == ("Y", "X")
           and order2.ink_dots == 4)
    (DATA_DIR / "work_order_2.json").write_text(
        order2.to_json(), encoding="utf-8"
    )

    # ------------------------------------------------ 工单 3：原凭单 + 右栏约束
    _line("工单 3：保持原凭单 VCH-1，仅指定右栏 Y 必须在 X 前")
    order3 = issue_work_order_from_payload(
        voucher1.to_dict(),
        PlateConstraints(
            left=SideConstraints(before=(("A", "B"),)),
            right=SideConstraints(before=(("Y", "X"),)),
            ink_dot_budget=3,
        ),
    )
    _print_order(order3)
    cross1 = {tuple(sorted((c.band_a, c.band_b)))
              for c in order1.crossings if c.crossed}
    cross3 = {tuple(sorted((c.band_a, c.band_b)))
              for c in order3.crossings if c.crossed}
    _check("同样最低 4 退单，且交带清单与自由排位不同",
           (not order3.is_signed) and order3.ink_dots == 4
           and cross1 != cross3,
           f"工单1交带 {cross1} vs 工单3交带 {cross3}")
    (DATA_DIR / "work_order_3.json").write_text(
        order3.to_json(), encoding="utf-8"
    )

    # ---------------------------------------- 矛盾约束退单（附演）
    _line("附演：题定先后自相矛盾时直接退单，不做排位")
    conflict_order = issue_work_order_from_payload(
        voucher1.to_dict(),
        PlateConstraints(
            left=SideConstraints(before=(("A", "B"), ("B", "A"))),
            ink_dot_budget=3,
        ),
    )
    print(f"  状态={conflict_order.status} 矛盾对={conflict_order.conflict_pairs}")
    _check("约束成环退单", conflict_order.status == "REJECTED_CONFLICT")

    # ---------------------------------------- 栏合计相同、走向不同
    _line("走向核对：只看栏合计无法定位经费带来源（合计相同、流向不同）")
    # 两张凭单的每侧栏头合计完全一致：
    #   旧栏 A=B=3，新栏 X=Y=3；但逐笔请求的 (旧栏, 新栏) 归属不同。
    from orgbudget.flow_voucher import FlowBand, FlowVoucher, VoucherLine

    def _voucher(no: str, lines: list[VoucherLine]) -> FlowVoucher:
        bucket: dict[tuple[str, str], list[object]] = {}
        for ln in lines:
            b = bucket.setdefault((ln.old_header, ln.new_header), [0, []])
            b[0] = int(b[0]) + ln.amount
            b[1].append(ln.request_no)  # type: ignore[union-attr]
        bands = tuple(
            FlowBand(o, n, w, tuple(sorted(reqs)))
            for (o, n), (w, reqs) in sorted(bucket.items())
        )
        return FlowVoucher(
            no, v1, v2, scheme.scheme_id,
            ("A", "B"), ("X", "Y"), tuple(lines), bands,
            sum(b.width for b in bands), "t",
        )

    chain_old, chain_new = ("HQ", "A"), ("HQ", "X")
    # 走向甲：A 的钱去 X，B 的钱去 Y（对角清晰）。
    voucher_a = _voucher("VCH-A", [
        VoucherLine("RA1", "ax", 3, "A", "X", chain_old, chain_new),
        VoucherLine("RB1", "by", 3, "B", "Y", ("HQ", "B"), ("HQ", "Y")),
    ])
    # 走向乙：同样 A=B=3、X=Y=3，但 A 的钱去 Y、B 的钱去 X（反向）。
    voucher_b = _voucher("VCH-B", [
        VoucherLine("RA2", "ay", 3, "A", "Y", chain_old, ("HQ", "Y")),
        VoucherLine("RB2", "bx", 3, "B", "X", ("HQ", "B"), chain_new),
    ])

    def _column_totals(v: FlowVoucher) -> tuple[dict, dict]:
        old_tot = {h: 0 for h in v.old_headers}
        new_tot = {h: 0 for h in v.new_headers}
        for b in v.bands:
            old_tot[b.old_header] += b.width
            new_tot[b.new_header] += b.width
        return old_tot, new_tot

    tot_a, tot_b = _column_totals(voucher_a), _column_totals(voucher_b)
    print(f"  走向甲栏合计：旧 {tot_a[0]} 新 {tot_a[1]}")
    print(f"  走向乙栏合计：旧 {tot_b[0]} 新 {tot_b[1]}")
    _check("两张凭单两侧栏合计完全相同", tot_a == tot_b)
    flows_a = {(b.old_header, b.new_header) for b in voucher_a.bands}
    flows_b = {(b.old_header, b.new_header) for b in voucher_b.bands}
    print(f"  走向甲带宽: {sorted(flows_a)}")
    print(f"  走向乙带宽: {sorted(flows_b)}")
    _check("但逐笔经费带走向完全不同（凭单明细才能定位）",
           flows_a.isdisjoint(flows_b))
    # 印厂侧两张凭单的带宽格不同，排位工单的交叉表也不同，证明印厂仅凭
    # 单载荷（不看预算数据库）就能把两种走向区分开。
    wo_a = issue_work_order_from_payload(
        voucher_a.to_dict(), PlateConstraints(ink_dot_budget=99))
    wo_b = issue_work_order_from_payload(
        voucher_b.to_dict(), PlateConstraints(ink_dot_budget=99))
    keys_a = {(c.band_a, c.band_b) for c in wo_a.crossings}
    keys_b = {(c.band_a, c.band_b) for c in wo_b.crossings}
    _check("印厂仅凭单载荷即可区分两种走向（交叉表带对集合不同）",
           keys_a != keys_b,
           f"甲 {sorted(keys_a)} vs 乙 {sorted(keys_b)}")

    # ---------------------------------------- 不能交版示例
    _line("附演：缺一端覆盖时不能交版，并指出原请求号")
    try:
        vault.seal_voucher(
            "VCH-BAD", v1, v2, ("A", "B"), ("X",),
            budget.occupancy_snapshot(scheme.scheme_id), scheme.scheme_id,
        )
        raise AssertionError("应当不能交版")
    except VoucherHandOffError as exc:
        missing = [p for p in exc.problems if p[0] == "MISSING"]
        print(f"  拦截 {len(exc.problems)} 项，缺覆盖原单: "
              f"{sorted({p[1] for p in missing})}")
        _check("逐条指出缺覆盖的原请求号",
              {"REQ-ay", "REQ-ay-2", "REQ-by"} <= {p[1] for p in missing})
    _check("失败不留封存行", "VCH-BAD" not in vault.voucher_numbers())

    _line("全部演示完成")
    print(f"  数据库与工单目录: {DATA_DIR}")
    print("  - org.db / budget.db / voucher.db  三库物理分离")
    print("  - work_order_1.json / work_order_2.json / work_order_3.json  "
          "可直接制版的排位工单")
    print(f"  封存凭单清单: {vault.voucher_numbers()}")
    # 印厂消费端只读 JSON，不接触任何数据库：落盘工单可独立复核。
    for name in ("work_order_1", "work_order_2", "work_order_3"):
        payload = json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert payload["voucher_no"].startswith("VCH")
    print("  工单 JSON 可脱离三个数据库单独读取复核")


if __name__ == "__main__":
    main()
