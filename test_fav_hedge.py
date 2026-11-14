import unittest
import numpy as np
import dsim as d
import fav_hedge as fh
from test_dsim import mkt

SC = dict(book=1.0, bn=100.0, order=1.0, cancel=1.0, hold=50)
E0 = np.zeros(0, np.int64)
MV = dict(pull=(E0, E0), stop=(E0, E0))
CFG = dict(trigger=dict(kind="fixed", t=60, X=0.6), leg1="taker", o=0, leg2="static", naked=dict(rule="hold"), variant="fav")


def market(print_fill=True):
    book = [(0, 6900, 7000, 100, 100), (90_000, 6900, 7200, 100, 3)]
    pr = [(100_000, 0, 0, 7200, 10, b"")] if print_fill else []  # taker BUY UP at 0.72 = SELL DOWN 0.28 into our bid
    return mkt(book, pr)


class T(unittest.TestCase):
    def test_hedged_pair_locks_profit_either_winner(self):
        f1 = 0.07 * 0.7 * 0.3
        for w in ("UP", "DOWN"):
            r = fh.simulate(market(), 0, 300, w, CFG, SC, "pess", MV)
            self.assertEqual((r["p1"], r["q"], r["h2"], r["hedged"]), (7000, 2800, 5, 1))
            self.assertAlmostEqual(r["pnl"], 5 * (1 - 0.70 - 0.28) - 5 * f1)

    def test_naked_hold_settles(self):
        f1 = 0.07 * 0.7 * 0.3
        r = fh.simulate(market(False), 0, 300, "DOWN", CFG, SC, "pess", MV)
        self.assertEqual((r["h2"], r["hedged"]), (0, 0))
        self.assertAlmostEqual(r["pnl"], -5 * 0.70 - 5 * f1)

    def test_cut_sells_leg1(self):
        cfg = dict(CFG, naked=dict(rule="cut", c=60))
        r = fh.simulate(market(False), 0, 300, "DOWN", cfg, SC, "pess", MV)
        self.assertEqual((r["exited"], r["sold"]), (1, 5))
        f1, fs = 0.07 * 0.7 * 0.3, 0.07 * 0.69 * 0.31
        self.assertAlmostEqual(r["pnl"], 5 * (0.69 - 0.70) - 5 * (f1 + fs))

    def test_queue_matters(self):
        r = fh.simulate(market(), 0, 300, "UP", CFG, SC, "pess", MV)
        self.assertEqual(r["t_hedge"], 100_000 - (60_000 + 1 + 50))
        # 8 ahead instead of 3: the 10-share print only fills 2 under pess
        book = [(0, 6900, 7000, 100, 100), (90_000, 6900, 7200, 100, 8)]
        r = fh.simulate(mkt(book, [(100_000, 0, 0, 7200, 10, b"")]), 0, 300, "UP", CFG, SC, "pess", MV)
        self.assertEqual((r["h2"], r["hedged"]), (2, 0))


if __name__ == "__main__":
    unittest.main()
