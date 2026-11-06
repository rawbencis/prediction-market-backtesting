import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import btc_overlay as bo
from test_favourite_flips import market, path


def btc_dir(d, start, px):
    sec = np.arange(start, start + len(px), dtype=np.int64)
    np.savez_compressed(Path(d) / "BTCUSDT-1s-2026-06.npz", sec=sec, close=np.asarray(px, float))
    return bo.Btc(d)


class TestOverlay(unittest.TestCase):
    def test_features_use_only_completed_klines(self):
        rng = np.random.default_rng(1)
        e = 1_000_000
        px = 60000 * np.exp(np.cumsum(rng.normal(0, 1e-4, 2000)))
        with tempfile.TemporaryDirectory() as d:
            b = btc_dir(d, e - 1000, px)
            f = b.features(e, (e + 60) * 1_000_000)
            # strike = close of the kline opened at e-1; spot = close of the kline opened at e+59
            i0, i = 999, 1059
            sig = np.std(np.diff(np.log(px))[i - bo.VOL_S : i])
            self.assertAlmostEqual(f["z"], np.log(px[i] / px[i0]) / (sig * np.sqrt(240)), 6)
            px2 = px.copy()
            px2[i + 1 :] *= 1.05  # the future must not matter
        with tempfile.TemporaryDirectory() as d:
            f2 = btc_dir(d, e - 1000, px2).features(e, (e + 60) * 1_000_000)
        self.assertEqual(f, f2)

    def test_logistic_recovers_weights(self):
        rng = np.random.default_rng(2)
        X = np.column_stack([np.ones(20000), rng.normal(size=(20000, 2))])
        w = np.array([0.3, 1.5, -0.8])
        y = (rng.random(20000) < 1 / (1 + np.exp(-X @ w))).astype(float)
        np.testing.assert_allclose(bo.fit_logistic(X, y), w, atol=0.08)

    def test_snapshots_skip_stale_books(self):
        e = 1_000_000
        m = market(path([(0, 500), (299, 600)]))
        m["bid"][100:140, 0] = -1  # UP book invalid 100-139 s -> snapshots 110..130 dropped
        with tempfile.TemporaryDirectory() as d:
            b = btc_dir(d, e - 400, np.full(800, 60000.0) + np.arange(800) * 0.1)
            rows = bo.snapshots(e, m, b, winner=0)
        got = [r["s"] for r in rows]
        self.assertNotIn(110, got)
        self.assertIn(100, got)
        self.assertIn(140, got)
        self.assertTrue(all(r["up_won"] == 1 for r in rows))

    def test_eval_detects_btc_information(self):
        rng = np.random.default_rng(3)
        rows = []
        for day in range(30):
            for mk in range(150):
                z = rng.normal()
                up = rng.random() < 1 / (1 + np.exp(-2.5 * z))  # outcome driven by BTC
                for s in (30, 150, 270):
                    mid = float(np.clip(0.5 + 0.1 * rng.normal(), 0.05, 0.95))  # uninformative
                    rows.append(
                        {
                            "day": f"2026-06-{day + 1:02d}",
                            "epoch": day * 1000 + mk,
                            "s": s,
                            "mid": mid,
                            "ask_up": mid + 0.01,
                            "ask_dn": 1.01 - mid,
                            "up_won": int(up),
                            "z": z,
                            "sig_bps": 1.0,
                            **{f"m{h}": rng.normal() for h in bo.MOM_S},
                        }
                    )
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "snaps.csv"
            pd.DataFrame(rows).to_csv(f, index=False)
            r = subprocess.run(
                [sys.executable, "btc_overlay.py", "eval", "--snaps", str(f)],
                capture_output=True,
                text=True,
                check=True,
            )
            ll = pd.DataFrame(json.loads((Path(d) / "overlay_eval.json").read_text())["loglik"])
        allh = ll[(ll.secs_in == "ALL") & (ll.phase == "holdout")].iloc[0]
        self.assertGreater(allh["B_gain"], 0.1, r.stdout)
        self.assertGreater(allh["B_gain_ci90"][0], 0)


if __name__ == "__main__":
    unittest.main()
