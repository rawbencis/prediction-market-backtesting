import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

import leadlag as ll

D0 = "2026-06-02"


def fake(root, days=4, per_day=24, lag_s=3.0, seed=1):
    """Cache + 1 s BTC grids. Book UP mid = fair lagged by lag_s, 1c tick, 2c spread."""
    r = np.random.default_rng(seed)
    cache, btc = root / "cache", root / "btc" / "spot1s_1000"
    btc.mkdir(parents=True)
    alld = [(np.datetime64(D0) + np.timedelta64(i, "D")).astype(str) for i in range(-1, days)]
    px = 60000 * np.exp(np.cumsum(r.normal(0, 1.5e-4, len(alld) * 86400)))
    for i, d in enumerate(alld):
        np.save(btc / f"{d}.npy", px[i * 86400 : (i + 1) * 86400])
    G = ll.Btc(btc, alld[-1], 1000)
    G.g, G.t0 = px, ll.day_ms(alld[0])
    sett = []
    for d in alld[1:]:
        dd = cache / d
        dd.mkdir(parents=True)
        e0 = ll.day_ms(d) // 1000
        for j in range(per_day):
            ep = e0 + 3600 + j * 300
            e = ep * 1000
            s0, sig = G.at(e), G.sigma(e, 1800)
            t = np.arange(0, 300, 0.1)
            f = ll.ndtr(ll.fair_z(G, e, np.maximum(t - lag_s, 0), s0, sig))
            mid = np.clip(np.rint(f * 100) * 10, 20, 980).astype(np.int32)
            tu = np.rint(t * 1e6).astype(np.int64)
            n = len(t)
            np.savez_compressed(
                dd / f"{ep}.npz",
                t=tu,
                bid=np.column_stack([mid - 10, 1000 - mid - 10]).astype(np.int32),
                ask=np.column_stack([mid + 10, 1000 - mid + 10]).astype(np.int32),
                bsz=np.full((n, 2), 50, np.float32),
                asz=np.full((n, 2), 50, np.float32),
                upd=np.ones((n, 2), bool),
                seen=np.column_stack([tu, tu]),
            )
            sett.append((f"btc-updown-5m-{ep}", "UP" if G.at(e + 300000) >= s0 else "DOWN"))
        (dd / "_done.json").write_text("{}")
    with (cache / "settlements.csv").open("w") as fh:
        fh.write("slug,winner\n" + "".join(f"{a},{b}\n" for a, b in sett))
    return cache, root / "btc"


class TestGrid(unittest.TestCase):
    def test_last_price_at_bin_end_and_ffill(self):
        d0 = ll.day_ms(D0)
        t = np.array([d0 + 10, d0 + 900, d0 + 2500, d0 + 2999])
        g = ll.build_grid(t, np.array([1.0, 2.0, 3.0, 4.0]), d0, 1000)
        self.assertEqual(list(g[:4]), [2.0, 2.0, 4.0, 4.0])
        b = ll.Btc.__new__(ll.Btc)
        b.g, b.t0, b.res = g, d0, 1000
        self.assertTrue(np.isnan(b.at(d0 + 999)))  # first bin not complete yet
        self.assertEqual(float(b.at(d0 + 1000)), 2.0)
        self.assertEqual(float(b.at(d0 + 2999)), 2.0)  # bin 2 completes at 3000
        self.assertEqual(float(b.at(d0 + 3000)), 4.0)

    def test_microsecond_archive(self):
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "a.zip"
            d0 = ll.day_ms(D0) * 1000
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("a.csv", f"1,60000.5,0.1,1,1,{d0 + 1500000},True,True\n")
            t, p = ll.read_trades(z, 5, 1)
            self.assertEqual(int(t[0]), d0 // 1000 + 1500)
            self.assertEqual(float(p[0]), 60000.5)

    def test_um_header_row_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "a.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("a.csv", "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
                           "transact_time,is_buyer_maker\n1,60000,1,1,1,1780358400123,true\n")
            t, p = ll.read_trades(z, 5, 1)
            self.assertEqual(list(t), [1780358400123])


class TestEntries(unittest.TestCase):
    def day(self, ua, ua_arr):
        n = len(ua)
        d = {c: np.full(n, np.nan, np.float32) for c in ll.COLS}
        d.update(t=np.arange(10, 10 + n, dtype=np.float32), z=np.full(n, 2.0), w=np.zeros(n),
                 ua=np.array(ua, np.float32), da=np.full(n, 0.99, np.float32),
                 uasz=np.full(n, 50.0), dasz=np.full(n, 50.0), epoch=np.zeros(n, np.int64))
        d["ua250"] = np.array(ua_arr, np.float32)
        d["uasz250"] = np.full(n, 50.0)
        return d

    def test_ioc_no_chase(self):
        # fair = N(2) = .977; ask .60 signals, but arrives at .65 -> miss; next row fills at .61
        d = self.day([0.60, 0.61, 0.61], [0.65, 0.61, 0.61])
        e = ll.entries(d, 1.0, 0.02, (5, 296), 250, 0.07)
        self.assertEqual(e, [(1, 0, float(np.float32(0.61)))])
        pnl = ll.trade_pnl(d, e, 250, "settle", 0.07)[0]
        self.assertAlmostEqual(pnl, 1 - 0.61 - 0.07 * 0.61 * 0.39, 5)

    def test_fee(self):
        self.assertAlmostEqual(ll.fee(0.5, 0.07), 0.0175)


class TestEndToEnd(unittest.TestCase):
    def test_planted_lag_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            cache, btc = fake(Path(td))
            out = Path(td) / "out"
            run = lambda *a: subprocess.run(  # noqa: E731
                [sys.executable, "leadlag.py", *a], check=True, capture_output=True, text=True,
                cwd=Path(__file__).parent)
            run("extract", "--cache", str(cache), "--btc", str(btc), "--out", str(out))
            run("stats", "--out", str(out), "--holdout-days", "2", "--min-trades", "5")
            S = json.loads((out / "summary.json").read_text())
            self.assertEqual(S["rule_check"]["close_vs_open_agree"], 1.0)
            self.assertEqual(S["xcorr_all"]["peak_lag_s"], 3.0)
            self.assertGreater(S["gap_closure_beta"]["5"][0], 0.3)
            best = S["trades_lat250"]["selected_on_dev"][0]
            self.assertGreater(best["dev_pnl"], 0)
            self.assertGreater(best["hold_pnl"], 0)

    def test_no_lag_peaks_at_zero(self):
        with tempfile.TemporaryDirectory() as td:
            cache, btc = fake(Path(td), days=3, lag_s=0.0)
            out = Path(td) / "out"
            subprocess.run([sys.executable, "leadlag.py", "extract", "--cache", str(cache), "--btc",
                            str(btc), "--out", str(out)], check=True, capture_output=True,
                           cwd=Path(__file__).parent)
            subprocess.run([sys.executable, "leadlag.py", "stats", "--out", str(out),
                            "--holdout-days", "1", "--min-trades", "5"], check=True,
                           capture_output=True, cwd=Path(__file__).parent)
            S = json.loads((out / "summary.json").read_text())
            self.assertEqual(S["xcorr_all"]["peak_lag_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
