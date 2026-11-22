"""抽查配额求解器：题面小册数值、边界与最不利分布。"""

from __future__ import annotations

import unittest
from fractions import Fraction
from math import comb

from orgbudget.errors import InvalidAuditRequest
from orgbudget.sampling import solve_quota, worst_case_miss


class SamplerExampleTest(unittest.TestCase):
    """题面例：X、Y 两分部 4 封 / 2 封，成本 1、2，k=1，p=1/2。"""

    def test_min_cost_quotas_and_exact_fraction(self) -> None:
        plan = solve_quota([4, 2], [1, 2], 1, Fraction(1, 2), names=["X", "Y"])
        # 最省开 X 2 封 + Y 1 封，费用 2*1 + 1*2 = 4。
        self.assertEqual(plan.quotas, (2, 1))
        self.assertEqual(plan.total_cost, 4)
        # k=1 时最不利分布是把唯一问题封放在任一层；开 2/4、1/2 的漏检都是 1/2。
        self.assertEqual(plan.worst_miss_probability, Fraction(1, 2))
        self.assertEqual(plan.guarantee, Fraction(1, 2))
        # 实际可选封号是层内 1..quota，联合层也照此。
        self.assertEqual(plan.envelope_nos, ((1, 2), (1,)))
        self.assertEqual(sum(plan.worst_misses), 1)
        # 精确分数可独立复核：最不利分布必落在某一层上。
        for i, y in enumerate(plan.worst_misses):
            self.assertIn(y, (0, 1))

    def test_moving_one_leaf_needs_more_and_costs_more(self) -> None:
        # 把 Y 的一叶移出后，层规模变为 X 3 / Y 3（总 6 封不变）。
        plan = solve_quota([3, 3], [1, 2], 1, Fraction(1, 2), names=["X", "Y"])
        self.assertEqual(plan.quotas, (2, 2))
        self.assertEqual(plan.total_cost, 6)

    def test_p_two_thirds_same_tree(self) -> None:
        plan = solve_quota([4, 2], [1, 2], 1, Fraction(2, 3), names=["X", "Y"])
        self.assertEqual(plan.quotas, (3, 2))
        self.assertEqual(plan.total_cost, 7)
        self.assertGreaterEqual(plan.guarantee, Fraction(2, 3))

    def test_p_one_opens_every_envelope_for_k_one(self) -> None:
        # k=1 时那一封可能落在任一层；任一层留空都可能漏掉，故 p=1 必须全开。
        plan = solve_quota([4, 2], [1, 2], 1, Fraction(1), names=["X", "Y"])
        self.assertEqual(plan.quotas, (4, 2))
        self.assertEqual(plan.total_cost, 8)
        self.assertEqual(plan.guarantee, Fraction(1))


class WorstCaseDistributionTest(unittest.TestCase):
    def _brute_force_miss(self, sizes, quotas, k):
        """枚举所有问题封分布，返回最大漏检概率（独立朴素实现）。"""
        from itertools import product

        worst = Fraction(0)
        for dist in product(*(range(n + 1) for n in sizes)):
            if sum(dist) != k:
                continue
            miss = Fraction(1)
            for n, x, y in zip(sizes, quotas, dist):
                miss *= Fraction(comb(n - y, x), comb(n, x))
            worst = max(worst, miss)
        return worst

    def test_sampler_matches_bruteforce_worst_case(self) -> None:
        cases = [
            ([4, 2], [2, 1], 1),
            ([3, 3], [2, 2], 1),
            ([5, 4, 3], [2, 2, 1], 2),
            ([2, 2], [1, 0], 1),
        ]
        for sizes, quotas, k in cases:
            brute = self._brute_force_miss(sizes, quotas, k)
            prob, dist = worst_case_miss(sizes, quotas, k)
            self.assertEqual(prob, brute)
            self.assertEqual(sum(dist), k)
            self.assertTrue(all(0 <= y <= n for y, n in zip(dist, sizes)))


class AdditivityTrapTest(unittest.TestCase):
    def test_probabilities_do_not_add_for_k_greater_than_one(self) -> None:
        # 两封两层、k=2 意味着两封都有问题。各层开 1 封时，
        # 最不利分布是 (1,1)：每层漏检 1/2，整批漏检 1/4 => 发现 3/4。
        miss, dist = worst_case_miss([2, 2], [1, 1], 2)
        self.assertEqual(dist, (1, 1))
        self.assertEqual(miss, Fraction(1, 4))
        self.assertEqual(1 - miss, Fraction(3, 4))
        # 若把“各层发现一封”简单相加：1/2+1/2=1，会高估真实保证 3/4。
        # 事件并不互斥/独立相加，正确口径是漏检概率之积的补。
        self.assertLess(Fraction(3, 4), Fraction(1))

    def test_p_one_needs_enough_to_cover_hiding_capacity(self) -> None:
        # 层规模 2/2、k=2：未开的空位共 (2-x1)+(2-x2)，若 >=2 则问题
        # 可能全藏进未开部分而漏检。要保证必查中需 x1+x2 >= 3。
        miss_partial, _ = worst_case_miss([2, 2], [2, 0], 2)
        self.assertEqual(miss_partial, Fraction(1))
        miss_partial2, _ = worst_case_miss([2, 2], [1, 1], 2)
        self.assertEqual(miss_partial2, Fraction(1, 4))
        plan = solve_quota([2, 2], [1, 1], 2, Fraction(1))
        self.assertEqual(sum(plan.quotas), 3)
        self.assertEqual(plan.total_cost, 3)
        self.assertEqual(plan.guarantee, Fraction(1))

    def test_k_equals_total_any_single_open_suffices(self) -> None:
        # k=总封数 => 每一封都有问题，开任意一封必中；最省是开 1 封。
        plan = solve_quota([3, 2], [1, 1], 5, Fraction(1))
        self.assertEqual(sum(plan.quotas), 1)
        self.assertEqual(plan.guarantee, Fraction(1))
        plan_cheap = solve_quota([3, 2], [1, 5], 5, Fraction(1))
        self.assertEqual(plan_cheap.quotas, (1, 0))  # 开便宜的 X 层


class ValidationTest(unittest.TestCase):
    def test_rejects_bad_parameters(self) -> None:
        with self.assertRaises(InvalidAuditRequest):
            solve_quota([13], [1], 1, Fraction(1, 2))  # 每层至多 12
        with self.assertRaises(InvalidAuditRequest):
            solve_quota([1, 1, 1, 1, 1], [1] * 5, 1, Fraction(1, 2))  # 至多 4 层
        with self.assertRaises(InvalidAuditRequest):
            solve_quota([2, 2], [1, 2], 5, Fraction(1, 2))  # k 超过总封数
        with self.assertRaises(InvalidAuditRequest):
            solve_quota([2, 2], [1, 2], 1, 0.5)  # p 必须是 Fraction
        with self.assertRaises(InvalidAuditRequest):
            solve_quota([2, 2], [1, 2], 1, Fraction(3, 2))  # p 越界


class BoundaryTest(unittest.TestCase):
    def test_empty_layer_is_legal(self) -> None:
        plan = solve_quota([3, 0], [1, 2], 1, Fraction(1, 2), names=["X", "Y"])
        self.assertEqual(plan.quotas[1], 0)
        # 空层没有可查的封，层内发现概率记 0，但不参与整批乘积。
        self.assertEqual(plan.layers[1].worst_prob, Fraction(0))
        self.assertGreaterEqual(plan.guarantee, Fraction(1, 2))

    def test_zero_cost_layer_quota_is_used(self) -> None:
        # k=1 时任一非空层开 0 封都可能成为最不利落点（漏检=1），
        # 故每层至少开 1；成本为 0 的 Y 层自由多开也不降低总成本，
        # 最省仍是两层各 1 封。
        plan = solve_quota([2, 2], [1, 0], 1, Fraction(1, 2))
        self.assertEqual(plan.quotas, (1, 1))
        self.assertEqual(plan.total_cost, 1)
        self.assertGreaterEqual(plan.guarantee, Fraction(1, 2))

    def test_full_open_layer_is_legal(self) -> None:
        # (2,0) / (0,2) 都合法但不是最优：留空的非空层在 k=1 下漏检=1。
        self.assertEqual(worst_case_miss([2, 2], [2, 0], 1)[0], Fraction(1))
        self.assertEqual(worst_case_miss([2, 2], [0, 2], 1)[0], Fraction(1))
        plan = solve_quota([2, 2], [1, 5], 1, Fraction(1, 2))
        self.assertEqual(plan.quotas, (1, 1))  # 两层各开 1 才把漏检压到 1/2
        full = solve_quota([2, 2], [1, 5], 1, Fraction(1))
        self.assertEqual(full.quotas, (2, 2))  # p=1 需全开

    def test_k_zero_opens_nothing(self) -> None:
        plan = solve_quota([4, 2], [1, 2], 0, Fraction(1))
        self.assertEqual(plan.quotas, (0, 0))
        self.assertEqual(plan.total_cost, 0)
        self.assertEqual(plan.guarantee, Fraction(1))


if __name__ == "__main__":
    unittest.main()
