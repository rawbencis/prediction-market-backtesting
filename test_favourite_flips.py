import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import favourite_flips as ff


def market(rows):
    """rows: (t_s, up_bid, up_ask, dn_bid, dn_ask) in milli; both sides update every row."""
    t = np.rint(np.array([r[0] for r in rows]) * 1e6).astype(np.int64)
    n = len(t)
    return {
        "t": t,
        "bid": np.array([[r[1], r[3]] for r in rows], np.int32),
        "ask": np.array([[r[2], r[4]] for r in rows], np.int32),
        "bsz": np.full((n, 2), 50, np.float32),
        "asz": np.full((n, 2), 50, np.float32),
        "upd": np.ones((n, 2), bool),
        "seen": np.column_stack([t, t]),
    }


def path(points):
    """Dense 1 s path through (t, up_mid) knots; DOWN mirrors; 1c spreads."""
    ts = np.arange(0, 300, 1.0)
    up = np.rint(np.interp(ts, [p[0] for p in points], [p[1] for p in points]))
    return [(t, u - 5, u + 5, 1000 - u - 5, 1000 - u + 5) for t, u in zip(ts, up)]


FLIPS = [(0, 500), (20, 720), (60, 500), (100, 280), (150, 500), (200, 760), (299, 900)]


class TestFlips(unittest.TestCase):
    def test_three_flips(self):
        ep = ff.episodes(market(path(FLIPS)), xs=(700,))[700]
        self.assertEqual([(k, s) for k, _, s, _, _ in ep], [(1, 0), (2, 1), (3, 0)])
        self.assertEqual(ff.episodes(market(path(FLIPS)), xs=(950,))[950], [])

    def test_maker_limit_and_trade_through_fill(self):
        m = market(path(FLIPS))
        _, T, sd, fav, dog = ff.episodes(m, xs=(700,))[700][1]  # DOWN becomes favourite
        lim, place, fill, dark = fav
        j0 = int(np.searchsorted(m["t"], T + ff.wf.LAT_US))
        self.assertEqual((sd, lim, place, dark), (1, m["bid"][j0, 1], m["t"][j0], -1))
        later = np.arange(j0 + 1, len(m["t"]))
        first = later[m["ask"][later, 1] < lim][0]  # DOWN falls back as UP recovers
        self.assertEqual(fill, m["t"][first])
        self.assertGreater(dog[0], 0)  # underdog (UP) order was placed too

    def test_no_flip_back_without_other_side_reaching_x(self):
        m = market(path([(0, 500), (30, 710), (90, 550), (150, 720), (299, 800)]))
        self.assertEqual(len(ff.episodes(m, xs=(700,))[700]), 1)

    def test_invalid_quotes_do_not_create_flips(self):
        m = market(path([(0, 500), (30, 720), (299, 800)]))
        m["bid"][100:103, 1] = -1
        self.assertEqual(len(ff.episodes(m, xs=(700,))[700]), 1)

    def test_dark_close_counts_resting_bid_as_filled_on_the_loser(self):
        # UP becomes favourite at 0.72 and rises (our bid at ~0.71 never trades through),
        # then the UP book goes invalid for the last 40 s: outcome decided in the dark
        m = market(path([(0, 500), (30, 720), (259, 850), (299, 850)]))
        m["bid"][260:, 0] = -1
        ep = ff.episodes(m, xs=(700,))[700]
        self.assertEqual(len(ep), 1)
        lim, _, fill, dark = ep[0][3]
        self.assertEqual((fill, dark), (-1, 259_000_000))
        rows = []
        for winner in (0, 1):  # 0: UP won, 1: UP lost
            k, T, sd, fav, dog = ep[0]
            info = [
                v if i == 0 else (v / 1e6 if v >= 0 else -1)
                for side in (fav, dog)
                for i, v in enumerate(side)
            ]
            rows.append(["2026-06-01", winner, 700, k, T / 1e6, sd, 1, winner, *info])
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e.csv"
            pd.DataFrame(rows, columns=ff.COLS).to_csv(f, index=False)
            df = ff.load(f)
        up_won, up_lost = df.iloc[0], df.iloc[1]
        self.assertEqual(up_won["fav_filled_300"], 0.0)  # winner's bid is not hit
        self.assertEqual(up_lost["fav_filled_300"], 1.0)  # loser's bid assumed hit
        self.assertAlmostEqual(up_lost["fav_pnl_300"], -lim / 1000)
        self.assertEqual(up_lost["fav_filled_10"], 0.0)  # order expired long before the dark


if __name__ == "__main__":
    unittest.main()
