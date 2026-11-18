import csv, io, contextlib, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import candle_count as C

E0 = 1780272000  # 2026-06-01 00:00 UTC


def series(n, p_same, seed=0):
    r = np.random.default_rng(seed)
    u = [1]
    for _ in range(n - 1):
        u.append(u[-1] if r.random() < p_same else 1 - u[-1])
    return E0 + 300 * np.arange(n), np.array(u)


class T(unittest.TestCase):
    def test_rolling_needs_all_predecessors(self):
        E = E0 + 300 * np.array([0, 1, 2, 4, 5, 6, 7])   # window 3 is missing
        U = np.array([1, 1, 0, 1, 0, 0, 1])
        i, f = C.rolling(E, U, 2)
        self.assertEqual(list(i), [2, 5, 6])              # 3 and 4 would reach across the gap
        self.assertEqual(list(f), [1.0, 0.5, 0.0])

    def test_bets_direction_and_ties(self):
        E, U = E0 + 300 * np.arange(5), np.array([1, 1, 0, 1, 1])
        i, w = C.bets(E, U, 2, -1)                        # last two: (1,1)->bet DOWN, (1,0) tie, (0,1) tie
        self.assertEqual((list(i), list(w)), ([2], [1]))
        i, w = C.bets(E, U, 1, 1)                         # with the previous outcome
        self.assertEqual(list(w), [1, 0, 0, 1])

    def test_streaks(self):
        E, U = E0 + 300 * np.arange(7), np.array([1, 1, 1, 0, 0, 1, 1])
        self.assertEqual(C.streaks(E, U), {1: (3, 3), 2: (2, 1), 3: (1, 0)})
        E2 = E.copy()
        E2[3:] += 300                                     # a gap after the third window restarts the run
        self.assertEqual(C.streaks(E2, U), {1: (3, 3), 2: (2, 1)})

    def test_reversal_is_found_and_walk_forward_picks_it(self):
        E, U = series(288 * 90, 0.47)
        i, w = C.bets(E, U, 1, -1)
        p, se = C.pts(w.sum(), len(w))
        self.assertTrue(2.0 < p < 4.0 and p / se > 5, (p, se))
        D, W, picks = C.walk_forward(E, U)
        self.assertGreater(C.pts(W.sum(), len(W))[0], 1.0)
        lo, hi = C.day_ci(D, W)
        self.assertTrue(lo > 0 and lo < C.pts(W.sum(), len(W))[0] < hi)

    def test_no_signal_in_a_fair_coin(self):
        E, U = series(288 * 90, 0.5, seed=3)
        D, W, _ = C.walk_forward(E, U)
        p, se = C.pts(W.sum(), len(W))
        self.assertLess(abs(p), 3 * se)

    def test_quote_at(self):
        m = dict(t=np.array([1_000_000, 4_000_000, 6_000_000, 30_000_000]),
                 bid=np.array([[480, 500], [-1, 500], [470, 500], [470, 500]]), ask=np.array([[500, 520], [510, 520], [490, 520], [490, 520]]),
                 seen=np.array([[1_000_000, 0], [4_000_000, 0], [6_000_000, 0], [20_000_000, 0]]))
        self.assertEqual(C.quote_at(m, 2_000_000), (0.48, 0.5))
        self.assertIsNone(C.quote_at(m, 5_000_000))       # no bid
        self.assertIsNone(C.quote_at(m, 500_000))         # before the first row
        self.assertEqual(C.quote_at(m, 15_000_000), (0.47, 0.49))
        self.assertIsNone(C.quote_at(m, 17_000_000))      # no book event for 10 s
        self.assertIsNone(C.quote_at(m, 31_000_000))      # UP side stale at the last event

    def test_rec_quotes(self):
        rows = [  # l, x, a, tf, e, b, k, bs, ks
            (1, E0 * 1000 - 2000, 0, 5, E0, 4900, 5100, 9, 9),           # standing before the open
            (2, E0 * 1000 + 3000, 0, 5, E0, 5000, 5200, 9, 9),           # the quote at 5 s
            (3, E0 * 1000 + 6000, 0, 5, E0, 5300, 5500, 9, 9),
            (4, E0 * 1000 + 3000, 1, 5, E0, 1000, 1200, 9, 9),           # another asset
            (5, E0 * 1000 + 3000, 0, 15, E0, 1000, 1200, 9, 9),          # 15m
            (6, (E0 + 300) * 1000 + 1000, 0, 5, E0 + 300, 4800, 5000, 9, 9),
            (7, (E0 + 300) * 1000 + 4000, 0, 5, E0 + 300, 0, 0, 0, 0),   # gap marker is the last row before 5 s
            (8, (E0 + 600) * 1000 - 20000, 0, 5, E0 + 600, 4800, 5000, 9, 9),   # nothing in the 15 s before 5 s
            (9, (E0 + 900) * 1000 + 1000, 0, 5, E0 + 900, 0, 5000, 0, 9),       # one-sided
        ]
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d) / "2026-06-01"
            dd.mkdir()
            np.array(rows[:5], C.PM_DT).tofile(dd / "pm-00.bin")
            np.array(rows[5:], C.PM_DT).tofile(dd / "pm-01.bin")
            got = list(C.rec_quotes(d, (5, 30)))
        self.assertEqual(got, [(E0, 5, (0.5, 0.52))])              # at 30 s the last row is 24 s old: skipped

    def test_eval_runs(self):
        E, U = series(288 * 70, 0.48)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.csv"
            with p.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["slug", "winner"])
                w.writerows((f"btc-updown-5m-{e}", "UP" if u else "DOWN") for e, u in zip(E, U))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                C.cmd_eval(SimpleNamespace(outcomes=p, split="2026-07-10", cache=None, rec=None))
        s = out.getvalue()
        self.assertIn("A. Bet AGAINST", s)
        self.assertIn("D. Walk-forward", s)
        self.assertIn(f"markets {288 * 70}", s)


if __name__ == "__main__":
    unittest.main()
