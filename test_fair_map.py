import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np

import fair_map as fm
from leadlag import fee
from test_shadow import DAY, EP, record


def args(td, **kw):
    a = dict(rec=td, days=[DAY], asset="btc", tf=5, src=0, step_ms=50, lat_ms=72, cap=0.01, size=5.0, min_ask=0.02,
             max_ask=0.99, fee_rate=0.07, top=5, settle=td / "s.csv", no_fetch=True)  # fmt: skip
    a.update(kw)
    return Namespace(**a)


class TestFairMap(unittest.TestCase):
    def test_samples_follow_the_recorded_book_and_binance(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)  # Binance +30 bp seen at 100.03 s; UP 50/52 until exchange 100.27 s (seen 100.39 s), then 95/97
            (td / "s.csv").write_text(f"slug,winner\nbtc-updown-5m-{EP},UP\n")
            R, n, _ = fm.build(args(td))
        self.assertEqual(n, 1)
        up = R[R["d"] > 20]
        t = 300 - up["t"]  # decision time into the window, s
        stale = up[(t >= 100.03) & (t < 100.198)]  # seen the jump, still lands before the reprice: .52 fills
        self.assertTrue(len(stale) >= 3 and stale["ok"].all())
        np.testing.assert_allclose(stale["ev"], 1 - 0.52 - fee(0.52, 0.07), atol=1e-4)
        gap = up[(t > 100.21) & (t < 100.38)]  # lands after the reprice, limit still .53: no fill
        self.assertTrue(len(gap) >= 2 and not gap["ok"].any())
        late = up[t > 100.4]  # sees .97, pays .97
        self.assertTrue(late["ok"].all())
        np.testing.assert_allclose(late["ev"], 1 - 0.97 - fee(0.97, 0.07), atol=1e-4)
        down = R[(R["d"] < -20) & ((300 - R["t"]) < 100.198)]
        np.testing.assert_allclose(down["ev"], -0.5 - fee(0.5, 0.07), atol=1e-4)

    def test_unsettled_windows_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)
            (td / "s.csv").write_text("slug,winner\n")
            R, n, _ = fm.build(args(td))
        self.assertEqual((len(R), n), (0, 0))

    def test_cell_stats_average_windows_not_samples(self):
        dt = np.dtype([("w", "<i4"), ("d", "<f4"), ("t", "<f4"), ("ok", "?"), ("ev", "<f4"), ("ask", "<f4"), ("win", "<f4")])
        R = np.array([(0, 5, 100, True, 0.5, 0.5, 1)] * 9 + [(1, 5, 100, True, -0.5, 0.5, 0), (1, 5, 100, False, np.nan, 0.5, 0)],
                     dtype=dt)  # fmt: skip
        s = fm.cell_stats(R)
        self.assertAlmostEqual(s["ev"], 0.0)  # one window +50c, one -50c: per-window, not 9:1 by samples
        self.assertEqual((s["windows"], s["samples"]), (2, 10))
        self.assertAlmostEqual(s["fill"], 10 / 11)

    def test_report_runs(self):
        dt = np.dtype([("w", "<i4"), ("d", "<f4"), ("t", "<f4"), ("ok", "?"), ("ev", "<f4"), ("ask", "<f4"), ("win", "<f4")])
        rng = np.random.default_rng(0)
        R = np.array([(w, 6, 100, True, 0.05 + rng.normal(0, 0.01), 0.9, 1) for w in range(30)], dtype=dt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cells = fm.report(R, 30, [DAY], args(Path(".")))
        self.assertEqual(len(cells), 1)
        self.assertIn("move    5..10 bp,  120-90s left", buf.getvalue())
        self.assertIn("*", buf.getvalue().split("Best")[0])


if __name__ == "__main__":
    unittest.main()
