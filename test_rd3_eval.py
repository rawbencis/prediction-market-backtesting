import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import dsim as d
import rd3_eval as r3

E = 1794704400  # 2026-11-15 01:00 UTC
BKL = np.dtype([("l", "<i8"), ("x", "<i8"), ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
PR = np.dtype([("x", "<i8"), ("tok", "u1"), ("sd", "u1"), ("p", "<u2"), ("sz", "<f4"), ("tx", "S32")])


def book(rows):  # (x, b, k, bs, ks), received 10 ms later
    return np.array([(1000 * (x + 10), x, b, k, bs, ks) for x, b, k, bs, ks in rows], BKL)


def bn_rows(rows):  # (T, receipt ms, src, price)
    return np.array([(int(1000 * l), T, s, 0, p) for T, l, s, p in rows], d.BN_DT)


T1 = E * 1000 + 100_000  # 200 s left


def market(ask_size=100.0, prints=()):
    b = book([(E * 1000 - 40_000, 4900, 5100, 100, ask_size), (T1 + 100, 7900, 8100, 100, 100)])
    return d.mk_market(b, np.array(list(prints), PR), 0)


class T(unittest.TestCase):
    def test_signals_and_debounce(self):
        bn = bn_rows([(T1 - 600, T1 - 595, 1, 100000.0), (T1, T1 + 5, 1, 100030.0), (T1 + 500, T1 + 505, 1, 100070.0),
                      (T1 + 2500, T1 + 2505, 1, 99990.0), (T1 + 2600, T1 + 2605, 0, 50000.0)])
        s = r3.signals(bn, 1)
        self.assertEqual([(t, sg) for t, _, sg in s], [(T1, 1), (T1 + 2500, -1)])  # +3 bp, debounced +7 bp, then -8 bp
        self.assertEqual(s[0][1], T1 + 5)

    def test_timing_per_scenario(self):
        mk = {("btc", 5, E): market()}
        rows = r3.evaluate(mk, [(T1, T1 + 5.0, 1)], "fut", {d.slug(("btc", 5, E)): "UP"}, 22.0)
        got = {r["scn"]: r for r in rows}
        # dublin: decide T+6, match T+78 < reprice at T+100 -> fills at the stale 0.51; colo T+57 fills; tokyo T+173 does not
        self.assertEqual((got["dublin"]["filled"], got["dublin"]["px"], got["dublin"]["match"]), (1, 5100, T1 + 78))
        self.assertAlmostEqual(got["dublin"]["pnl_c"], 100 - 51 - 100 * d.fee(5100, 1))
        self.assertEqual(got["colo"]["filled"], 1)
        self.assertEqual(got["tokyo"]["filled"], 0)
        self.assertEqual(got["dublin"]["seen_ask"], 5100)
        self.assertEqual(got["dublin"]["tl"], "120-300")

    def test_ties_other_takers_first(self):
        # 8 shown, another taker takes 6 at our match ms: < 5 left -> no fill (nothing is excluded: real order)
        mk = {("btc", 5, E): market(ask_size=8.0, prints=[(T1 + 78, 0, 0, 5100, 6.0, b"z")])}
        rows = r3.evaluate(mk, [(T1, T1 + 5.0, 1)], "fut", {}, 22.0)
        self.assertEqual([r["filled"] for r in rows if r["scn"] == "dublin"], [0])

    def test_filters(self):
        mk = {("btc", 5, E): market()}
        self.assertEqual(r3.evaluate(mk, [(E * 1000 + 280_000, E * 1000 + 280_005.0, 1)], "fut", {}, 22.0), [])  # 20 s left
        hi = d.mk_market(book([(E * 1000 - 40_000, 9200, 9300, 100, 100)]), np.zeros(0, PR), 0)
        self.assertEqual(r3.evaluate({("btc", 5, E): hi}, [(T1, T1 + 5.0, 1)], "fut", {}, 22.0), [])  # ask 0.93 > 0.90
        self.assertEqual(r3.send_p50("2026-11-15", Path("/nonexistent")), 22.0)

    def test_end_to_end_synthetic_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "rec", Path(tmp) / "res"
            day = root / "2026-11-15"
            day.mkdir(parents=True)
            pm = np.zeros(2, d.PM_DT)
            for j, (x, b, k) in enumerate([(E * 1000 - 40_000, 4900, 5100), (T1 + 100, 7900, 8100)]):
                pm[j] = (1000 * (x + 10), x, 0, 5, E, b, k, 100, 100)
            pm.tofile(day / "pm-01.bin")
            np.zeros(0, d.TR_DT).tofile(day / "tr-01.bin")
            bn_rows([(T1 - 600, T1 - 595, 1, 100000.0), (T1, T1 + 5, 1, 100030.0)]).tofile(day / "bn-01.bin")
            rc = r3.run("2026-11-15", root=root, out=out, probe=Path("/nonexistent"), winners={d.slug(("btc", 5, E)): "UP"})
            self.assertEqual(rc, 0)
            R = pd.read_csv(out / "rd3-2026-11-15.csv")  # synthetic output only
            self.assertEqual(len(R), 3)
            self.assertEqual(set(R.scn), {"dublin", "tokyo", "colo"})
            (out / "rd3-2026-11-15.csv").unlink()
            r3.time.time = lambda: 1794704400.0 + 86400  # one day later: still waiting for settlement
            try:
                rc = r3.run("2026-11-15", root=root, out=out, probe=Path("/nonexistent"), winners={})
            finally:
                import time as _t
                r3.time.time = _t.time
            self.assertEqual(rc, 2)  # unsettled day: nothing written
            self.assertFalse((out / "rd3-2026-11-15.csv").exists())


if __name__ == "__main__":
    unittest.main()
