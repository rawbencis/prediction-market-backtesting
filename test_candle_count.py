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

    def test_window_dips(self):
        nan = float("nan")
        t = np.array([0, 1000, 3000, 5000, 9000, 12000])
        bid = np.array([0.48, 0.48, 0.44, 0.36, nan, 0.30])
        ask = np.array([0.50, 0.50, 0.46, 0.38, 0.35, 0.33])
        b0, a0, ts, tl = C.window_dips(t, bid, ask, P=(0.30, 0.35, 0.40, 0.49), t0=2000)
        self.assertEqual((b0, a0), (0.48, 0.50))
        self.assertEqual(list(ts), [C.NEVER, 9000, 5000, 3000])      # first time the ask is at or under P
        self.assertEqual(list(tl), [C.NEVER, 12000, 5000, 3000])     # first time the best bid is under P
        self.assertIsNone(C.window_dips(np.array([5000]), np.array([0.4]), np.array([0.5]), t0=2000)[2])   # no quote yet

    def test_dip_rows_and_sequences(self):
        # outcomes: three DOWN closes, then windows 3..6 are bet UP at 0.35 (run of 3+): lose, lose, win, then no signal
        seq = ["DOWN", "DOWN", "DOWN", "DOWN", "DOWN", "UP", "UP"]
        O = {E0 + 300 * i: v for i, v in enumerate(seq)}
        def win(fill_at):
            t = np.array([0, 1000, 50000])
            ub = np.array([0.50, 0.50, 0.30 if fill_at else 0.50])
            return C._both_sides(t, ub, ub + 0.02)
        W = {E0 + 300 * i: win(True) for i in range(3, 7)}
        W[E0 + 300 * 4] = win(True)
        pi = C.DIP_P.index(0.35)
        rows = C.dip_rows(W, O, C.dip_signals(O)["run of 3+, against it"], pi)
        self.assertEqual([(r[1], r[2], r[4]) for r in rows], [("placed", True, False), ("placed", True, False), ("placed", True, True)])
        self.assertEqual(C.dip_stat(rows, 2)[:3], (3, 3, 1))
        flat = C.dip_sequences(W, O, 3, pi, "flat", 99)
        self.assertEqual((flat["seqs"], flat["fills"], flat["unrecovered"]), (1, 3, 0))
        self.assertAlmostEqual(flat["pnl"], -0.35 - 0.35 + 0.65)
        rec = C.dip_sequences(W, O, 3, pi, "recover", 4)
        self.assertAlmostEqual(rec["pnl"], 0.65)                     # the win pays back both losses plus one unit's profit
        self.assertAlmostEqual(rec["max_units"], (0.35 + 0.35 * (1 + 0.35 / 0.65) + 0.65) / 0.65)
        W[E0 + 300 * 5] = win(False)                                 # not filled in the window that wins
        rec = C.dip_sequences(W, O, 3, pi, "recover", 4)
        self.assertEqual(rec["unrecovered"], 1)
        self.assertAlmostEqual(rec["pnl"], rec["worst"])
        self.assertEqual(C.streak_before(O, E0 + 300 * 5), 5)

    def test_lighter_paths_fill_from_taker_trades_only(self):
        e = E0
        lb = np.zeros(3, C.LB_DT)
        for i, (x, b, k) in enumerate(((e * 1000 - 5000, 100, 9900), (e * 1000 + 1000, 4800, 5200), (e * 1000 + 60000, 3000, 3400))):
            lb[i] = (x * 1000, x, 0, 15, e, b, 50, 0, 0, 0, 0, k, 50, 0, 0, 0, 0)
        lt = np.zeros(5, C.LT_DT)
        for i, (T, px, buy) in enumerate(((1500, 3000, 0),      # before the bid is placed: ignored
                                          (50000, 3500, 0),     # a taker sells UP at 0.35: at a 0.35 bid's price
                                          (70000, 3300, 0),     # ... and at 0.33: through it
                                          (80000, 6800, 1),     # a taker buys UP at 0.68 = sells DOWN at 0.32
                                          (80000, 6800, 1))):   # duplicate id below
            lt[i] = (0, e * 1000 + T, 0, 15, e, px, 10, buy, 1, 2, min(i, 3))
        old = (C.STEP, C.TF)
        C.STEP, C.TF = 900, 15
        self.addCleanup(lambda: (setattr(C, "STEP", old[0]), setattr(C, "TF", old[1])))
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d) / "2026-06-01"
            dd.mkdir()
            lb.tofile(dd / "lb-00.bin"), lt.tofile(dd / "lt-00.bin")
            (ee, w), = list(C.lighter_paths(d))
        pi = {p: i for i, p in enumerate(C.DIP_P)}
        self.assertEqual(ee, e)
        self.assertEqual((w[True][1], w[False][1]), (0.52, 0.52))                     # asks at placement: UP 0.52, DOWN 1 - 0.48
        self.assertEqual((w[True][2][pi[0.35]], w[True][3][pi[0.35]]), (70000, 50000))   # through at 70 s, touched at 50 s
        self.assertEqual((w[True][2][pi[0.30]], w[True][3][pi[0.30]]), (C.NEVER, C.NEVER))
        self.assertEqual((w[False][2][pi[0.35]], w[False][3][pi[0.35]]), (80000, 80000))  # DOWN sold at 0.32
        self.assertEqual(w[False][2][pi[0.30]], C.NEVER)
        self.assertEqual(w[True][2][pi[0.45]], 50000)                                   # the book quote alone (ask 0.34 at 60 s) fills nothing

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
