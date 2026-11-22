"""逐球解释引擎：建产品过滤、检查点、制胜球、无解释定位与独立 oracle 对照。

期望数字全部来自题面三球小例的显式手算与独立枚举，不由待测函数自证。
"""

from __future__ import annotations

import unittest

from league.playbyplay import (
    build_product, explain, possible_attributions, can_cooccur,
    possible_recipients, PlayByPlayError, HOME, AWAY, FINAL,
)
from oracle_cards import (
    oracle_legal_paths, oracle_first_dead, oracle_winning_cards,
    oracle_recipients, oracle_attributions, oracle_can_cooccur,
)

ROSTER = {"P": HOME, "U": HOME, "Q": HOME, "V": AWAY, "W": AWAY}
THREE_CARDS = [
    {"card_no": 1, "options": {HOME: "P"}},
    {"card_no": 2, "options": {HOME: "U", AWAY: "V"}},
    {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
]


def make(cards=THREE_CARDS, fh=2, fa=1, checkpoints=None, **kw):
    return build_product("ab", 1, "A", "B", dict(ROSTER), fh, fa, cards,
                         checkpoints or [], **kw)


class TestBuildProduct(unittest.TestCase):
    def test_team_mismatch_option_excluded_and_audited(self):
        # 卡1 客队选择记了主队球员 P：按当版名册必须排除并留痕
        prod = make([
            {"card_no": 1, "options": {HOME: "P", AWAY: "P"}},
            {"card_no": 2, "options": {HOME: "U", AWAY: "V"}},
            {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
        ])
        self.assertEqual(prod.cards[0].options, {HOME: "P"})
        self.assertEqual(len(prod.excluded), 1)
        self.assertEqual(prod.excluded[0]["card_no"], 1)
        self.assertEqual(prod.excluded[0]["side"], AWAY)

    def test_unknown_player_and_bad_side_rejected(self):
        with self.assertRaises(PlayByPlayError) as ctx:
            make([
                {"card_no": 1, "options": {HOME: "P"}},
                {"card_no": 2, "options": {HOME: "X", AWAY: "V"}},
                {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
            ])
        self.assertTrue(any("名册" in r for r in ctx.exception.args[0]))

    def test_card_with_no_valid_side_rejected(self):
        with self.assertRaises(PlayByPlayError):
            make([
                {"card_no": 1, "options": {AWAY: "P"}},  # P 是主队，客队选择被排除后空
                {"card_no": 2, "options": {HOME: "U", AWAY: "V"}},
                {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
            ])

    def test_sequence_must_be_contiguous(self):
        with self.assertRaises(PlayByPlayError):
            make([
                {"card_no": 1, "options": {HOME: "P"}},
                {"card_no": 3, "options": {HOME: "U", AWAY: "V"}},
            ])

    def test_structurally_impossible_checkpoint_rejected_upfront(self):
        # 第 1 球后不可能累计 1:1（两场进球）
        with self.assertRaises(PlayByPlayError):
            make(checkpoints=[{"after_card": 1, "home": 1, "away": 1}])

    def test_round_trip_dict(self):
        prod = make(volume_id="v1", custodian="管")
        from league.playbyplay import PlayByPlayProduct
        again = PlayByPlayProduct.from_dict(prod.to_dict())
        self.assertEqual(again.cards, prod.cards)
        self.assertEqual(again.custodian, "管")


class TestWorkedExampleThreeGoals(unittest.TestCase):
    def setUp(self):
        self.prod = make(volume_id="VOL-ab", custodian="管")
        self.rep = explain(self.prod)

    def test_two_legal_interpretations_and_winning_cards(self):
        # 手算：HHA（主主客）最后平局在开场 => 制胜卡1（P）；
        #       HAH（主客主）卡2 后 1:1 => 制胜卡3（Q）
        self.assertTrue(self.rep.feasible)
        self.assertEqual(self.rep.winner_side, HOME)
        self.assertEqual(self.rep.winning_cards, [1, 3])
        legal = oracle_legal_paths(self.prod)
        self.assertEqual(len(legal), 2)
        self.assertEqual(oracle_winning_cards(self.prod), [1, 3])

    def test_per_card_attributions(self):
        self.assertEqual(possible_attributions(self.rep, 1), {HOME: ["P"]})
        self.assertEqual(possible_attributions(self.rep, 2),
                         {HOME: ["U"], AWAY: ["V"]})
        self.assertEqual(possible_attributions(self.rep, 3),
                         {HOME: ["Q"], AWAY: ["W"]})
        self.assertEqual(oracle_attributions(self.prod),
                         {1: {HOME: ["P"]},
                          2: {HOME: ["U"], AWAY: ["V"]},
                          3: {HOME: ["Q"], AWAY: ["W"]}})

    def test_witnesses_are_full_paths_with_running_scores(self):
        self.assertEqual(len(self.rep.witnesses), 2)
        for w in self.rep.witnesses:
            self.assertEqual([x["card_no"] for x in w], [1, 2, 3])
            self.assertEqual(w[0]["score_after"], [1, 0])
            self.assertEqual(w[-1]["score_after"], [2, 1])
        # 两条见证的制胜卡不同，足以说明 P/Q 分歧
        winners = set()
        for w in self.rep.witnesses:
            last_tie = max([x["card_no"] for x in w
                            if x["score_after"][0] == x["score_after"][1]] + [0])
            winners.add(last_tie + 1)
        self.assertEqual(winners, {1, 3})

    def test_cooccur_blocks_cross_interpretation_splicing(self):
        # 主客主（卡2客、卡3主）可同场；主主客（卡2主、卡3客）可同场；
        # 卡2主+卡3主拼接不出任何一条真实记录
        self.assertTrue(can_cooccur(self.prod, self.rep, [(2, AWAY), (3, HOME)]))
        self.assertTrue(can_cooccur(self.prod, self.rep, [(2, HOME), (3, AWAY)]))
        self.assertFalse(can_cooccur(self.prod, self.rep, [(2, HOME), (3, HOME)]))
        self.assertTrue(oracle_can_cooccur(self.prod, {2: AWAY, 3: HOME}))
        self.assertFalse(oracle_can_cooccur(self.prod, {2: HOME, 3: HOME}))

    def test_player_recipients(self):
        self.assertEqual(possible_recipients(self.prod, self.rep, None, "player"),
                         ["P", "Q"])
        self.assertEqual(oracle_recipients(self.prod, None, "player"), ["P", "Q"])

    def test_station_collapse_and_unclaimed(self):
        # P、Q 同属 K => 即使球未查清也可发 K
        reg = {"P": "K", "Q": "K", "U": "K"}
        self.assertEqual(possible_recipients(self.prod, self.rep, reg, "station"),
                         ["K"])
        self.assertEqual(oracle_recipients(self.prod, reg, "station"), ["K"])
        # 无任何登记 => 全部解释无人能领
        self.assertEqual(possible_recipients(self.prod, self.rep, {}, "station"),
                         [None])
        # 有人/无人并存
        self.assertEqual(
            possible_recipients(self.prod, self.rep, {"P": "K", "U": "K"},
                                "station"),
            ["K", None])


class TestCorrectionAndCheckpoints(unittest.TestCase):
    def test_upstream_fixes_card_two_to_home_winner_is_P(self):
        prod = make([
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {HOME: "U"}},
            {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
        ])
        rep = explain(prod)
        self.assertEqual(rep.winning_cards, [1])
        self.assertEqual(possible_recipients(prod, rep, None, "player"), ["P"])

    def test_checkpoint_kills_plausible_sequence(self):
        # 卡2后检查点 2:0 => 卡2只能主队，HAH（主客主）解释不复存在
        prod = make(checkpoints=[{"after_card": 2, "home": 2, "away": 0}])
        rep = explain(prod)
        self.assertTrue(rep.feasible)
        self.assertEqual(rep.winning_cards, [1])
        self.assertEqual(len(oracle_legal_paths(prod)), 1)
        # 被排除的球序现在无法共现
        self.assertFalse(can_cooccur(prod, rep, [(2, AWAY)]))

    def test_checkpoint_unreachable_marks_first_dead_card(self):
        # 卡1只有主队，却要求卡1后 0:1 => 卡1即首个无合法前缀
        prod = make(checkpoints=[{"after_card": 1, "home": 0, "away": 1}])
        rep = explain(prod)
        self.assertFalse(rep.feasible)
        self.assertEqual(rep.first_dead_card, 1)
        self.assertEqual(oracle_first_dead(prod), 1)

    def test_final_only_contradiction_marked_final(self):
        # 三卡全主队，终分却要 2:1（客队一球无处记）；各前缀都活着
        prod = make([
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {HOME: "U"}},
            {"card_no": 3, "options": {HOME: "Q"}},
        ])
        rep = explain(prod)
        self.assertFalse(rep.feasible)
        self.assertEqual(rep.first_dead_card, FINAL)
        self.assertEqual(oracle_first_dead(prod), "final")

    def test_mid_dead_card_distinct_from_final(self):
        prod = make([
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {AWAY: "V"}},
            {"card_no": 3, "options": {HOME: "Q", AWAY: "W"}},
        ], checkpoints=[{"after_card": 2, "home": 2, "away": 0}])
        rep = explain(prod)
        self.assertEqual(rep.first_dead_card, 2)
        self.assertEqual(oracle_first_dead(prod), 2)


class TestDrawAndScale(unittest.TestCase):
    def test_draw_has_no_winning_goal(self):
        prod = make([
            {"card_no": 1, "options": {HOME: "P"}},
            {"card_no": 2, "options": {AWAY: "V"}},
        ], fh=1, fa=1)
        rep = explain(prod)
        self.assertTrue(rep.feasible)
        self.assertIsNone(rep.winner_side)
        self.assertEqual(rep.winning_cards, [])
        self.assertEqual(len(rep.witnesses), 1)
        self.assertEqual(possible_recipients(prod, rep, None, "player"), [])

    def test_thirty_cards_polynomial_no_permutation_storage(self):
        # 30 卡全开、终分 16:14；制胜卡集合有多种，但只取两条见证且毫秒级返回
        cards = [{"card_no": i, "options": {HOME: "P", AWAY: "V"}}
                 for i in range(1, 31)]
        prod = build_product("m", 1, "A", "B", dict(ROSTER), 16, 14, cards)
        import time
        t0 = time.time()
        rep = explain(prod)
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(rep.feasible)
        self.assertGreater(len(rep.winning_cards), 1)
        self.assertEqual(len(rep.witnesses), 2)
        # 层状态数只受比分约束，绝不随 2^30 排列爆炸
        self.assertLessEqual(max(len(s) for s in rep.layer_states), 17 * 15)

    def test_oracle_parity_on_random_small_volumes(self):
        import random
        rng = random.Random(633)
        for trial in range(40):
            n = rng.randint(1, 10)
            cards = []
            for i in range(1, n + 1):
                opts = {}
                if rng.random() < 0.9:
                    opts[HOME] = "P"
                if rng.random() < 0.9:
                    opts[AWAY] = "V"
                if not opts:
                    opts[HOME] = "P"
                cards.append({"card_no": i, "options": opts})
            fh = rng.randint(0, n)
            fa = n - fh
            try:
                prod = build_product("m", 1, "A", "B", dict(ROSTER), fh, fa, cards)
            except PlayByPlayError:
                continue
            rep = explain(prod)
            oracle_paths = oracle_legal_paths(prod)
            self.assertEqual(rep.feasible, len(oracle_paths) > 0)
            if rep.feasible:
                self.assertEqual(rep.winning_cards,
                                 oracle_winning_cards(prod))
                self.assertEqual(
                    possible_recipients(prod, rep, None, "player"),
                    oracle_recipients(prod, None, "player"))


if __name__ == "__main__":
    unittest.main()
