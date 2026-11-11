import unittest

import numpy as np

import lighter_eval as le
import lighter_flips as lf

E = 1_800_000_000


def pm_rows(points):
    """Polymarket Up book on a 1 s grid through (t_s, mid) knots, 1c spread."""
    ts = np.arange(0, 900)
    mid = np.interp(ts, [p[0] for p in points], [p[1] for p in points])
    P = np.zeros(len(ts), le.PM_DT)
    P["l"] = (E + ts) * 1_000_000
    P["b"] = np.rint((mid - 0.005) * 1e4)
    P["k"] = np.rint((mid + 0.005) * 1e4)
    P["tf"] = 15
    return P


def lighter_book(bid, ask):
    L = np.zeros(1, le.LB_DT)
    L["l"] = E * 1_000_000
    L["b1"], L["k1"], L["b1s"], L["k1s"] = bid * 1e4, ask * 1e4, 100, 100
    return L


def trades(rows):
    """rows: (t_s, price_up, size, taker_bought_up)"""
    T = np.zeros(len(rows), le.LT_DT)
    for i, (t, p, s, buy) in enumerate(rows):
        T[i]["l"] = (E + t) * 1_000_000
        T[i]["T"] = (E + t) * 1000
        T[i]["p"], T[i]["s"], T[i]["buy"], T[i]["id"] = p * 1e4, s, buy, i
    return T


# Up becomes favourite at 0.70 around t=100 s; Down never reaches 0.70 -> one flip at X=0.70
PATH = [(0, 0.50), (100, 0.705), (899, 0.80)]


def pick(rows, **kw):
    return [r for r in rows if all(r[k] == v for k, v in kw.items())]


class TestLighterFlips(unittest.TestCase):
    def test_flip_detection(self):
        g = np.arange(10)
        mid = np.array([0.5, 0.71, 0.72, 0.5, 0.28, 0.6, 0.71, 0.5, 0.5, 0.5])
        self.assertEqual(
            [(k, s) for k, _, s in lf.flips(g, mid, np.ones(10, bool), 0.70)],
            [(1, 0), (2, 1), (3, 0)],
        )

    def test_queue_rules_and_settlement(self):
        P = pm_rows(PATH)
        L = lighter_book(0.60, 0.75)  # wide Lighter book: Up 0.60 / 0.75
        # taker sells Up at 0.60 at t=200 (prints at the resting bid), 30 shares
        T = trades([(200, 0.60, 30, False)])
        rows = lf.window(L, T, P, 1.0, E)
        fav = pick(rows, x=0.70, k=1, who="fav", life=900)
        join = pick(fav, rule="join")[0]
        self.assertEqual(
            (join["q0"], join["first"], join["filled"]), (0.60, False, 0.0)
        )  # behind queue
        f2 = pick(fav, rule="fair2")[
            0
        ]  # fair ~0.705 -> 0.68: improves the 0.60 bid, front of queue
        self.assertTrue(f2["first"])
        self.assertEqual(f2["filled"], lf.Q)
        self.assertAlmostEqual(f2["pnl"], (1.0 - f2["q"]) * lf.Q)
        f30 = pick(rows, x=0.70, k=1, who="fav", rule="fair2", life=30)[0]
        self.assertEqual(f30["filled"], 0.0)  # the trade comes ~100 s after the order

    def test_trade_through_fills_joined_order_and_down_side(self):
        P = pm_rows(PATH)
        L = lighter_book(0.60, 0.75)
        # taker sells Up at 0.59 (through 0.60) and later a taker BUYS Up at 0.76 (sells Down at 0.24)
        T = trades([(150, 0.59, 4, False), (160, 0.59, 20, False), (170, 0.76, 50, True)])
        rows = lf.window(L, T, P, 0.0, E)
        join = pick(rows, x=0.70, k=1, who="fav", rule="join", life=900)[0]
        self.assertEqual(join["filled"], lf.Q)
        self.assertAlmostEqual(join["pnl"], -0.60 * lf.Q)  # Up lost
        dog = pick(rows, x=0.70, k=1, who="dog", rule="join", life=900)[0]  # Down bid = 1 - 0.75
        self.assertAlmostEqual(dog["q0"], 0.25)
        self.assertEqual(
            dog["filled"], lf.Q
        )  # taker bought Up at 0.76 > 0.75 = sold Down through 0.25
        self.assertAlmostEqual(dog["pnl"], (1 - 0.25) * lf.Q)

    def test_no_order_before_the_flip_trades(self):
        P = pm_rows(PATH)
        L = lighter_book(0.60, 0.75)
        T = trades([(50, 0.55, 100, False)])  # before the 0.70 flip (~98 s), after the 0.60 one
        rows = lf.window(L, T, P, 1.0, E)
        self.assertTrue(all(r["filled"] == 0 for r in pick(rows, x=0.70)))
        self.assertTrue(any(r["filled"] > 0 for r in pick(rows, x=0.60)))

    def test_refresh_follows_fair_and_avoids_the_stale_fill(self):
        # Up flips to fav at ~98 s, then fair falls back to 0.60 by 300 s (no new flip at 0.70)
        P = pm_rows([(0, 0.50), (100, 0.705), (300, 0.60), (899, 0.60)])
        L = lighter_book(0.60, 0.75)
        T = trades([(400, 0.62, 30, False)])  # a taker sells Up at 0.62 long after the flip
        rows = lf.window(L, T, P, 0.0, E)
        static = pick(rows, x=0.70, k=1, who="fav", rule="fair3", life=900)[0]
        self.assertEqual(static["filled"], lf.Q)  # posted once at 0.67: picked off at 0.62
        self.assertLess(static["mo0"], 0)  # fair ~0.60 when hit: stale fill
        rf = pick(rows, x=0.70, k=1, who="fav", rule="rf3", life=900)[0]
        self.assertAlmostEqual(rf["q0"], static["q0"])  # same first order
        self.assertEqual(rf["filled"], 0.0)  # re-posted at 0.57 by then: 0.62 doesn't reach it
        short = pick(rows, x=0.70, k=1, who="fav", rule="fair3", life=20)[0]
        self.assertEqual(short["filled"], 0.0)  # a 20 s order had expired

    def test_faster_refresh_dodges_a_jump_inside_the_10s_window(self):
        # fair 0.705 after the flip, jumps to 0.60 at ~200.5 s; a taker sells Up at 0.66 at 206 s
        P = pm_rows([(0, 0.50), (100, 0.705), (200, 0.705), (200.5, 0.60), (899, 0.60)])
        L = lighter_book(0.60, 0.75)
        T = trades([(206, 0.66, 30, False)])
        rows = lf.window(L, T, P, 0.0, E)
        slow = pick(rows, x=0.70, k=1, who="fav", rule="rf3", life=900)[0]
        fast = pick(rows, x=0.70, k=1, who="fav", rule="rf3s3", life=900)[0]
        self.assertEqual(slow["filled"], lf.Q)  # posted at 198 s at 0.67, hit after the jump
        self.assertLess(slow["mo0"], 0)
        self.assertEqual(fast["filled"], 0.0)  # re-posted at 203 s at 0.57: not reached
        self.assertGreater(fast["posts"], slow["posts"])
        self.assertEqual(pick(rows, x=0.70, k=1, who="fav", rule="fair3", life=900)[0]["posts"], 1)


if __name__ == "__main__":
    unittest.main()
