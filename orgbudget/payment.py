"""出纳付款排程：在供应商准许日内整单一次付清，最小化迟费 + 信用费。

纯函数模块，不含任何 I/O。输入是若干张可付应付单、每日开账前确定入款、
初始现金与信用参数；输出每张单子的实际付款日与逐日余额、费用明细。

费用口径（题面）：

* 每张单子只能在供应商准许的 ``[earliest_day, latest_day]`` 内**整单一次
  付清**；付款日超过合同首选日 ``preferred_day`` 的，每迟一天收固定迟费
  ``late_fee``，即 ``max(0, pay_day - preferred_day) × late_fee``；
* 每日付款完毕后现金余额允许下探至信用额度（``-credit_limit``），按当日
  **负余额的绝对值 × credit_rate** 计信用费；
* 信用费独立存放、不进次日现金，因此次日开账余额仍是上一日的现金余额；
* 最后一天结尾余额不得为负（信用额度只用于日间周转）。

至多 10 张合同、8 天，用按天的位移动态规划穷举全部“哪天付哪些单”的组合，
保证找到全局费用最小的安排；不存在合法安排时抛 :class:`InfeasibleSchedule`。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import InfeasibleSchedule, InvalidContract


@dataclass(frozen=True)
class Bill:
    """进入排程的一张可付应付单（正数金额，整单一次付清）。"""

    bill_id: str
    amount: int
    earliest_day: int
    latest_day: int
    preferred_day: int
    late_fee: int

    def late_fee_on(self, pay_day: int) -> int:
        return max(0, pay_day - self.preferred_day) * self.late_fee

    def payable_on(self, day: int) -> bool:
        return self.earliest_day <= day <= self.latest_day


@dataclass(frozen=True)
class DayLedger:
    """某一营业日的开账、付款与余额事实。"""

    day: int
    opening_cash: int
    deposit: int
    paid_bill_ids: tuple[str, ...]
    paid_amount: int
    late_fee: int
    credit_fee: int
    closing_balance: int


@dataclass(frozen=True)
class PaymentSchedule:
    """一份最小费用的合法付款安排。"""

    horizon_days: int
    initial_cash: int
    credit_limit: int
    credit_rate: int
    bill_pay_days: dict[str, int]
    ledgers: tuple[DayLedger, ...]
    total_late_fee: int
    total_credit_fee: int
    total_fee: int

    @property
    def closing_balances(self) -> tuple[int, ...]:
        return tuple(ledger.closing_balance for ledger in self.ledgers)


def _validate_inputs(
    bills: tuple[Bill, ...],
    horizon_days: int,
    initial_cash: int,
    credit_limit: int,
    credit_rate: int,
    deposits: dict[int, int],
) -> None:
    if not isinstance(horizon_days, int) or isinstance(horizon_days, bool) or horizon_days <= 0:
        raise InvalidContract(f"排程天数必须是正整数: {horizon_days!r}")
    if not isinstance(initial_cash, int) or isinstance(initial_cash, bool) or initial_cash < 0:
        raise InvalidContract(f"初始现金必须是非负整数: {initial_cash!r}")
    if not isinstance(credit_limit, int) or isinstance(credit_limit, bool) or credit_limit < 0:
        raise InvalidContract(f"信用额度必须是非负整数: {credit_limit!r}")
    if not isinstance(credit_rate, int) or isinstance(credit_rate, bool) or credit_rate < 0:
        raise InvalidContract(f"信用费率必须是非负整数: {credit_rate!r}")
    for day, amount in deposits.items():
        if not isinstance(day, int) or not 1 <= day <= horizon_days:
            raise InvalidContract(f"入款日 {day} 超出排程范围 1..{horizon_days}")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise InvalidContract(f"第 {day} 天入款必须是非负整数: {amount!r}")
    ids = [b.bill_id for b in bills]
    if len(set(ids)) != len(ids):
        raise InvalidContract(f"应付单 ID 重复: {ids}")
    for b in bills:
        if not isinstance(b.amount, int) or isinstance(b.amount, bool) or b.amount <= 0:
            raise InvalidContract(f"应付单 {b.bill_id} 金额必须是正整数: {b.amount!r}")
        for name, value in (
            ("earliest_day", b.earliest_day),
            ("preferred_day", b.preferred_day),
            ("latest_day", b.latest_day),
        ):
            if not isinstance(value, int) or not 1 <= value <= horizon_days:
                raise InvalidContract(
                    f"应付单 {b.bill_id} 的 {name}={value} 超出排程范围 1..{horizon_days}"
                )
        if not (b.earliest_day <= b.preferred_day <= b.latest_day):
            raise InvalidContract(f"应付单 {b.bill_id} 的付款窗口非法")
        if not isinstance(b.late_fee, int) or isinstance(b.late_fee, bool) or b.late_fee < 0:
            raise InvalidContract(f"应付单 {b.bill_id} 迟费必须是非负整数")


def schedule_payments(
    bills: list[Bill] | tuple[Bill, ...],
    *,
    horizon_days: int,
    initial_cash: int,
    deposits: dict[int, int],
    credit_limit: int,
    credit_rate: int,
) -> PaymentSchedule:
    """求迟费与信用费总和最小的整单付款安排。

    :param bills: 可付应付单（``can_pay`` 为真且金额为正的版本）；
    :param deposits: 每日开账前的确定入款 ``{day: amount}``，缺省日按 0；
    :returns: :class:`PaymentSchedule`，含每张单的实际付款日与逐日余额；
    :raises InfeasibleSchedule: 任何合法安排都无法同时兑现全部到期款。
    """
    bill_tuple = tuple(bills)
    _validate_inputs(
        bill_tuple, horizon_days, initial_cash, credit_limit, credit_rate, deposits
    )

    n = len(bill_tuple)
    size = 1 << n
    # 预先按掩码缓存金额，便于按当天付款子集求和。
    amount_of_mask = [0] * size
    for mask in range(1, size):
        low = mask & -mask
        idx = low.bit_length() - 1
        amount_of_mask[mask] = amount_of_mask[mask ^ low] + bill_tuple[idx].amount

    # 每张单各天的迟费。
    late_fee_table = [
        [bill.late_fee_on(day) for day in range(horizon_days + 1)]
        for bill in bill_tuple
    ]

    # 单独不可行的单子（准许窗口完全落在排程范围之外）先给出明确诊断。
    for bill in bill_tuple:
        if not any(
            bill.earliest_day <= day <= bill.latest_day
            for day in range(1, horizon_days + 1)
        ):
            raise InfeasibleSchedule(
                f"应付单 {bill.bill_id} 的准许窗口 "
                f"[{bill.earliest_day},{bill.latest_day}] 落在排程 {horizon_days} 天之外"
            )

    INF = 10**30
    # dp[mask]：处理到当前天为止、已付集合为 mask 时的最小累计费用。
    dp = [INF] * size
    dp[0] = 0
    # cash_of[mask]：达到该最小费用状态时，当天付款完毕后携带到次日开账的
    # 现金余额。各状态前几天已付的单不同，携带现金也不同，信用下限必须按
    # 各状态自己的余额判断，不能用一个全局标量。
    cash_of = [-INF] * size
    cash_of[0] = initial_cash
    # 回溯条目按天独立保存 (parent_state, pay_mask)，键含天号，杜绝跨天
    # 复用陈旧指针：第 d 天产生的状态只引用第 d 天写入的条目。
    # link[(day, state)] = (prev_state, pay_mask)
    link: dict[tuple[int, int], tuple[int, int]] = {}

    for day in range(1, horizon_days + 1):
        deposit = deposits.get(day, 0)
        # 当天可付的单：准许窗口覆盖当天。
        eligible_bits = 0
        for idx, bill in enumerate(bill_tuple):
            if bill.payable_on(day):
                eligible_bits |= 1 << idx

        prev_dp = dp          # 快照：每个状态在当天只转移一次，杜绝同日重复付款。
        prev_cash = cash_of
        # 新一天的可达状态从空表重建，避免沿用上一天的残留值。
        next_dp = [INF] * size
        next_cash = [-INF] * size
        # 结尾余额下限：最后一天不得为负（信用额度只供日间周转）。
        floor = 0 if day == horizon_days else -credit_limit

        def record(new_state: int, new_cost: int, closing: int,
                   prev_state: int, pay_mask: int) -> None:
            """按费用 / 现金择优写入当天状态与回溯条目。"""
            if new_cost < next_dp[new_state] or (
                new_cost == next_dp[new_state] and closing > next_cash[new_state]
            ):
                next_dp[new_state] = new_cost
                next_cash[new_state] = closing
                link[(day, new_state)] = (prev_state, pay_mask)

        for state in range(size):
            base_cost = prev_dp[state]
            if base_cost == INF:
                continue
            # 该状态当天开账现金 = 上日结尾余额 + 今日开账前确定入款。
            opening = prev_cash[state] + deposit
            remaining = eligible_bits & ~state

            # 空子集：当天不付任何单，是状态到自身的转移。仍要按当天
            # 负余额计信用费、并把入款并入携带现金；记一条 pay_mask=0 的
            # 同状态回溯（带当天天号，不会与历史条目混淆）。
            idle_closing = opening
            if idle_closing >= floor:
                idle_cost = base_cost + max(0, -idle_closing) * credit_rate
                record(state, idle_cost, idle_closing, state, 0)

            # 非空子集：从剩余可付单中任选若干张当天整单付清。
            sub = remaining
            while sub:
                paid_amount = amount_of_mask[sub]
                closing = opening - paid_amount
                if closing >= floor:
                    day_credit = max(0, -closing) * credit_rate
                    day_late = 0
                    bits = sub
                    while bits:
                        low = bits & -bits
                        idx = low.bit_length() - 1
                        day_late += late_fee_table[idx][day]
                        bits ^= low
                    new_state = state | sub
                    new_cost = base_cost + day_late + day_credit
                    # 非空子集必令 new_state 严格新增已付位，不会自指。
                    record(new_state, new_cost, closing, state, sub)
                sub = (sub - 1) & remaining

        dp = next_dp
        cash_of = next_cash

    if dp[size - 1] == INF:
        raise InfeasibleSchedule(
            "在给定现金、入款、信用额度与各供应商准许窗口下，"
            "不存在任何合法付款安排能同时兑现全部到期款"
        )

    # 回溯：从满状态沿带天标的 prev 指针反向走，直到回到空状态。
    # link 键自带天号，因此指针永远指向“写入该条目的那一天”，不会跨天错位。
    # 空转移（pay_mask=0，prev 指向自身）只是把当天信用费 / 入款并入，
    # 不改变已付集合，回溯时跳过，只跟随真正付款的转移。
    chosen_by_day: dict[int, int] = {}
    state = size - 1
    guard = 0
    while state != 0:
        entry_day = None
        entry: tuple[int, int] | None = None
        # 找指向当前 state 的、天号最大且为真实付款的条目。
        for d in range(horizon_days, 0, -1):
            cand = link.get((d, state))
            if cand is not None and cand[1] != 0:
                entry_day = d
                entry = cand
                break
        if entry is None:  # pragma: no cover - 可达性已由 dp 保证
            raise InfeasibleSchedule("付款安排回溯失败：找不到到达状态的付款转移")
        prev_state, pay_mask = entry
        assert entry_day is not None
        chosen_by_day[entry_day] = chosen_by_day.get(entry_day, 0) | pay_mask
        state = prev_state
        guard += 1
        if guard > size:  # pragma: no cover - 每次真实付款都新增已付位，必在 n 步内回 0
            raise InfeasibleSchedule("付款安排回溯出现环")

    # 依据确定的安排重算逐日余额与费用（信用费不滚入次日现金）。
    bill_pay_days: dict[str, int] = {}
    ledgers: list[DayLedger] = []
    opening = initial_cash
    total_late = 0
    total_credit = 0
    for day in range(1, horizon_days + 1):
        deposit = deposits.get(day, 0)
        available = opening + deposit
        mask = chosen_by_day.get(day, 0)
        paid_amount = amount_of_mask[mask]
        ids: list[str] = []
        day_late = 0
        bits = mask
        while bits:
            low = bits & -bits
            idx = low.bit_length() - 1
            bill = bill_tuple[idx]
            ids.append(bill.bill_id)
            bill_pay_days[bill.bill_id] = day
            day_late += late_fee_table[idx][day]
            bits ^= low
        closing = available - paid_amount
        day_credit = max(0, -closing) * credit_rate
        total_late += day_late
        total_credit += day_credit
        ledgers.append(
            DayLedger(
                day=day,
                opening_cash=opening,
                deposit=deposit,
                paid_bill_ids=tuple(ids),
                paid_amount=paid_amount,
                late_fee=day_late,
                credit_fee=day_credit,
                closing_balance=closing,
            )
        )
        # 信用费独立存放、不进次日现金。
        opening = closing

    final_balance = ledgers[-1].closing_balance
    if final_balance < 0:
        raise InfeasibleSchedule(
            f"最后一天结尾余额 {final_balance} 为负，信用额度只供日间周转，"
            "结尾余额不得为负"
        )

    total_fee = total_late + total_credit
    if total_fee != dp[size - 1]:  # pragma: no cover - 回溯与 DP 必须一致
        raise InfeasibleSchedule("回溯费用与动态规划最优值不一致")

    return PaymentSchedule(
        horizon_days=horizon_days,
        initial_cash=initial_cash,
        credit_limit=credit_limit,
        credit_rate=credit_rate,
        bill_pay_days=bill_pay_days,
        ledgers=tuple(ledgers),
        total_late_fee=total_late,
        total_credit_fee=total_credit,
        total_fee=total_fee,
    )
