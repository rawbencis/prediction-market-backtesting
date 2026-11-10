import csv
import io
import json
import math
import shutil
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import xasset_pair as xp

S1 = 1e-4  # per-second vol
phi = xp.ncdf


def world(root, ndays=36, per_day=40, rho=0.8, lead=0, seed=0):
    """Two assets, 1 s prices; each Polymarket book priced on its OWN state only (fair iff lead == 0).

    lead > 0: eth copies btc's return `lead` seconds later, so eth's own-state price is stale information.
    """
    r = np.random.default_rng(seed)
    d0 = datetime(2026, 6, 20, tzinfo=timezone.utc)
    t0 = int(d0.timestamp())
    n = ndays * 86400
    ra = r.normal(0, S1, n)
    rb = rho * np.roll(ra, lead) + math.sqrt(1 - rho**2) * r.normal(0, S1, n)
    px = {"btc": 50000 * np.exp(np.cumsum(ra)), "eth": 3000 * np.exp(np.cumsum(rb))}
    out = root / "out"
    (out / "kl").mkdir(parents=True)
    W = {a: [] for a in px}
    tt = np.arange(1, 300)
    for a, c in px.items():
        np.savez(out / "kl" / f"{a}.npz", t0=t0, close=c)
        lc = np.log(c)
        for d in range(ndays):
            day = (d0 + timedelta(days=d)).date().isoformat()
            dd = root / f"{a}_cache" / day
            dd.mkdir(parents=True)
            for k in range(per_day):
                e = t0 + d * 86400 + 3600 + 300 * k
                x = lc[e + tt - t0 - 1] - lc[e - t0 - 1]
                p = phi(x / (S1 * np.sqrt(300 - tt)))
                bu = np.clip(np.rint(p * 1000) - 5, 1, 989).astype(np.int32)
                au = bu + 10
                t = tt.astype(np.int64) * 1_000_000
                m = dict(t=t, bid=np.stack([bu, 1000 - au], 1), ask=np.stack([au, 1000 - bu], 1),
                         bsz=np.full((299, 2), 50, np.float32), asz=np.full((299, 2), 50, np.float32),
                         upd=np.ones((299, 2), bool), seen=np.stack([t, t], 1))
                np.savez_compressed(dd / f"{e}.npz", **m)
                W[a].append((e, "UP" if lc[e + 300 - t0 - 1] >= lc[e - t0 - 1] else "DOWN"))
            (dd / "_done.json").write_text("{}")
    with (root / "btc_cache" / "settlements.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "winner"])
        w.writerows((f"btc-updown-5m-{e}", x) for e, x in W["btc"])
    with (out / "settle_eth.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["epoch", "winner"])
        w.writerows(W["eth"])
    return out


def run(root, out):
    a = Namespace(out=out, btc_cache=root / "btc_cache", assets=["btc", "eth"], workers=2, days=0, force=False,
                  npz=[f"btc={root / 'btc_cache'}", f"eth={root / 'eth_cache'}"])
    xp.cmd_grid(a)
    xp.cmd_pairs(a)
    xp.cmd_report(a)
    return json.loads((out / "report.json").read_text())


def flat_grid(n=1, bu=490, au=510):
    f = lambda v, dt: np.full((n, xp.NG), v, dt)  # noqa: E731
    return dict(bU=f(bu, np.int16), aU=f(au, np.int16), szU=f(50, np.int16), szD=f(50, np.int16), bzU=f(50, np.int16),
                bzD=f(50, np.int16), v=f(True, bool), vm=f(True, bool))


class Units(unittest.TestCase):
    def test_parse_klines(self):
        ms = b"1717200000000,1,2,0.5,1.5,9,1717200000999,1,1,1,1,0\n1717200002000,1,2,0.5,1.7,9,1717200002999,1,1,1,1,0\n"
        us = b"open_time,open,high,low,close,volume,x,x,x,x,x,x\n1780272000000000,1,2,0.5,2.5,9,0,1,1,1,1,0\n"
        s, c = xp.parse_klines(ms)
        self.assertEqual(list(s), [1717200000, 1717200002])
        t0, d, gaps = xp.dense(s, c)
        self.assertEqual((t0, list(d), gaps), (1717200000, [1.5, 1.5, 1.7], 1))
        s, c = xp.parse_klines(us)
        self.assertEqual((int(s[0]), float(c[0])), (1780272000, 2.5))

    def test_ncdf(self):
        z = np.array([-3.0, -1.0, 0.0, 0.5, 2.0])
        ref = np.array([0.5 * (1 + math.erf(v / math.sqrt(2))) for v in z])
        np.testing.assert_allclose(xp.ncdf(z), ref, atol=1e-6)

    def test_logit_fit(self):
        r = np.random.default_rng(0)
        X = np.column_stack([np.ones(40000), r.normal(size=(40000, 2))])
        y = (r.random(40000) < xp.sig(X @ np.array([0.2, 1.5, -0.7]))).astype(float)
        b = xp.logit_fit(X, y)
        np.testing.assert_allclose(b, [0.2, 1.5, -0.7], atol=0.06)
        self.assertTrue((xp.cluster_se(X, y, b, np.arange(40000) % 50) < 0.05).all())

    def test_fetch_winners(self):
        def get(slugs):
            return [dict(slug=s, closed=True, outcomes='["Up","Down"]', outcomePrices='["1","0"]' if int(s[-1]) % 2 else '["0","1"]') for s in slugs]

        w = xp.fetch_winners("eth", [101, 102, 103], get=get, log=lambda s: None)
        self.assertEqual(w, {101: "UP", 102: "DOWN", 103: "UP"})

    def test_to_grid(self):
        t = np.array([1_000_000, 2_000_000, 10_000_000], np.int64)
        m = dict(t=t, bid=np.array([[490, 490], [490, 480], [-1, -1]], np.int32), ask=np.array([[510, 510], [510, 510], [-1, -1]], np.int32),
                 bsz=np.full((3, 2), 7, np.float32), asz=np.full((3, 2), 9, np.float32), upd=np.ones((3, 2), bool), seen=np.stack([t, t], 1))
        g = xp.to_grid(m)
        i = lambda s: int(s * 4)  # noqa: E731
        self.assertFalse(g["v"][i(0.5)])  # before first quote
        self.assertTrue(g["v"][i(1)] and g["v"][i(1.75)])
        self.assertTrue(g["vm"][i(2)] and not g["v"][i(2)])  # fresh but not mirrored
        self.assertFalse(g["vm"][i(4.25)])  # stale > 2 s
        self.assertFalse(g["v"][i(10)])  # invalid book
        self.assertEqual((g["bU"][i(1)], g["aU"][i(1)], g["szU"][i(1)], g["bzD"][i(1)]), (490, 510, 9, 7))

    def test_pair_exec(self):
        ci = xp.PRE
        # A_UP ask 400, B_UP bid 500 -> o=0 cost 400+500 = 900, fees 16.8+17.5 -> 934.3 <= 950
        ga, gb = flat_grid(bu=390, au=400), flat_grid(bu=500, au=510)
        snap, hist, trig, _ = xp.pair_day(ga, gb, np.array([1]), np.array([0]))
        r = trig[0, ci]
        self.assertEqual((r[0], r[1], r[2]), (1, 0, 5.0))
        self.assertAlmostEqual(r[3], 934.3, places=1)
        self.assertEqual(r[4], 2)
        self.assertAlmostEqual(r[5], 2000 - 934.3, places=1)
        self.assertEqual(xp.pair_day(ga, gb, np.array([0]), np.array([1]))[2][0, ci, 4], 0)
        self.assertAlmostEqual(xp.pair_day(ga, gb, np.array([0]), np.array([1]))[2][0, ci, 5], -934.3, places=1)
        self.assertAlmostEqual(hist[-1, 1 + xp.M.index(50)] / hist[-1, 0], 1.0)
        self.assertEqual(xp.pair_day(ga, gb, np.array([1]), np.array([1]))[2][0, xp.CFG.index((80, 0)), 0], -1)  # 934.3 > 920
        # leg B runs away during the 250 ms: B_UP bid drops at the fill instant -> B_DOWN ask rises -> orphan A leg
        gb2 = flat_grid(bu=500, au=510)
        gb2["bU"][0, 21:] = 480
        gb2["aU"][0, 21:] = 490
        r = xp.pair_day(ga, gb2, np.array([0]), np.array([0]))[2][0, ci]
        self.assertEqual(r[0], 2)
        self.assertAlmostEqual(r[5], -400 - 16.8, places=1)
        # other orientation: A_DOWN + B_UP cheap
        r = xp.pair_day(gb, ga, np.array([0]), np.array([0]))[2][0, ci]
        self.assertEqual((r[0], r[1], r[4]), (1, 1, 1))
        self.assertAlmostEqual(r[5], 1000 - 934.3, places=1)
        # convergence exit: later both legs bid up -> sell both, pay two more fees
        ga3, gb3 = flat_grid(bu=390, au=400), flat_grid(bu=500, au=510)
        ga3["bU"][0, 100:], ga3["aU"][0, 100:] = 520, 530
        r = xp.pair_day(ga3, gb3, np.array([0]), np.array([1]))[2][0, ci]
        bnet = 520 + 490 - xp.fee(520) - xp.fee(490)  # 975.0: clears target 30, not 60
        self.assertAlmostEqual(r[6], bnet - 934.3, places=1)
        self.assertAlmostEqual(r[7], -934.3, places=1)
        self.assertAlmostEqual(r[5], -934.3, places=1)

    def test_klines_cmd_from_local_zip(self):
        import zipfile

        root = Path(tempfile.mkdtemp())
        try:
            raw = root / "kl" / "raw"
            raw.mkdir(parents=True)
            rows = "".join(f"{(1780272000 + i) * 1000000},1,1,1,{100 + i},1,0,1,1,1,1,0\n" for i in range(0, 600, 2))
            with zipfile.ZipFile(raw / "ETHUSDT-1s-2026-06.zip", "w") as z:
                z.writestr("ETHUSDT-1s-2026-06.csv", rows)
            xp.cmd_klines(Namespace(out=root, assets=["eth"], start="2026-06", end="2026-06"))
            t0, c = xp.load_px(root, ["eth"])["eth"]
            self.assertEqual((t0, len(c), c[1], c[2]), (1780272000, 599, 100.0, 102.0))
        finally:
            shutil.rmtree(root)

    def test_inv_and_parquet_grid_path(self):
        root = Path(tempfile.mkdtemp())
        orig = xp.wf.load_market
        try:
            data = root / "data" / "quotes_threshold_5m"
            data.mkdir(parents=True)
            e0 = 1780272000  # 2026-06-01 00:00 UTC
            for e in (e0, e0 + 300):
                for a in ("btc", "eth"):
                    for sd in ("UP", "DOWN"):
                        (data / f"{a}-updown-5m-{e}_{sd}.parquet").write_bytes(b"x")
            out = root / "out"
            xp.cmd_inv(Namespace(out=out, data=[root / "data"], btc_cache=None))
            inv = json.loads((out / "inv.json").read_text())
            self.assertEqual((inv["eth-updown-5m"]["n"], inv["eth-updown-5m"]["first"]), (2, "2026-06-01"))
            t = np.arange(1, 300, dtype=np.int64) * 1_000_000
            m = dict(t=t, bid=np.tile([490, 490], (299, 1)).astype(np.int32), ask=np.tile([510, 510], (299, 1)).astype(np.int32),
                     bsz=np.full((299, 2), 9, np.float32), asz=np.full((299, 2), 9, np.float32), upd=np.ones((299, 2), bool), seen=np.stack([t, t], 1))
            seen = []

            def fake(args):
                d, e = args
                assert (d / f"btc-updown-5m-{e}_DOWN.parquet").resolve().name == f"eth-updown-5m-{e}_DOWN.parquet"
                return e, m, {}

            xp.wf.load_market = fake
            xp.cmd_grid(Namespace(out=out, assets=["eth"], npz=[], workers=1, days=0, force=False))
            g = xp.load_grid(out, "eth", "2026-06-01")
            self.assertEqual(sorted(g["epochs"]), [e0, e0 + 300])
            self.assertTrue(g["v"][:, 8:].all())
        finally:
            xp.wf.load_market = orig
            shutil.rmtree(root)

    def test_maker_pair(self):
        ci = xp.MK_PRE
        # A_UP 390/400, B_UP 500/510 -> B_DOWN 490/500: mid cost 395 + 495 = 890 <= 950, orientation 0, trigger at 10.0 s
        ga, gb = flat_grid(bu=390, au=400), flat_grid(bu=500, au=510)
        ga["bU"][0, 50:], ga["aU"][0, 50:] = 375, 385  # A_UP ask 385 < 390 -> A bid fills
        gb2 = {k: v.copy() for k, v in gb.items()}
        gb2["bU"][0, 60:], gb2["aU"][0, 60:] = 515, 525  # B_DOWN ask 485 < 490 -> B bid fills
        mk = xp.pair_day(ga, gb2, np.array([1]), np.array([0]))[3][0, ci]
        self.assertEqual((mk[0], mk[4]), (1, 10.0))
        self.assertAlmostEqual(mk[1], 110, places=3)
        self.assertAlmostEqual(mk[2], 2000 - 390 - 490, places=3)
        self.assertAlmostEqual(mk[3], mk[2], places=3)
        # only the A leg fills; both down -> A_UP orphan loses 390 if held; completing B_DOWN at ask 500 + fee pays $1
        mk = xp.pair_day(ga, gb, np.array([0]), np.array([0]))[3][0, ci]
        self.assertEqual(mk[0], 2)
        self.assertAlmostEqual(mk[2], -390, places=3)
        self.assertAlmostEqual(mk[3], 1000 - 390 - 500 - xp.fee(500), places=3)
        # outside 10-90 s nothing is posted
        g0 = flat_grid(bu=490, au=500)
        self.assertEqual(xp.pair_day(g0, gb, np.array([0]), np.array([0]))[3][0, ci, 0], -1)

    def test_walk_cash_on_noise(self):
        days = [f"2026-06-{d:02d}" for d in range(1, 31)] + [f"2026-07-{d:02d}" for d in range(1, 32)]
        r = np.random.default_rng(1)
        Dm = r.normal(-0.5, 1, (61, 40))
        w = xp.walk(Dm, np.full((61, 40), 10.0), days)
        self.assertEqual(w["wf_days"], 61 - 13 - 21)
        self.assertIn("frozen_hold", w)


class EndToEnd(unittest.TestCase):
    def test_efficient_world_no_edge(self):
        root = Path(tempfile.mkdtemp())
        try:
            R = run(root, world(root, rho=0.8, lead=0, seed=1))
            u = R["under"]["pairs"]["btc-eth"]
            self.assertAlmostEqual(u["joint"]["same"], 0.5 + math.asin(0.8) / math.pi, delta=0.02)
            for T in ("btc", "eth"):
                self.assertLess(abs(u[f"target_{T}"]["b"][-1] / u[f"target_{T}"]["se"][-1]), 3.5)
            self.assertLess(abs(u["pair_edge"][1]["edge_milli"]), 12)
            self.assertAlmostEqual(R["under"]["proxy"]["eth"], 1.0)
            p = R["pairs"]["btc-eth"]
            self.assertEqual(p["markets"], 36 * 40)
            big = [c for c in p["calib"] if c["n"] > 3000]
            self.assertTrue(big and all(abs(c["edge_mid"][0]) < 45 for c in big))  # noise at 1,440 markets is ~20-30 milli
            self.assertLess(p["pre_registered"]["pre"][0], 0)  # pays spread + fees for nothing
            for T in ("btc", "eth"):
                self.assertLess(abs(p["cross"][T]["b"][-1] / p["cross"][T]["se"][-1]), 3.5)
        finally:
            shutil.rmtree(root)

    def test_power_detects_lead_lag(self):
        root = Path(tempfile.mkdtemp())
        try:
            R = run(root, world(root, rho=0.8, lead=60, seed=2))
            u = R["under"]["pairs"]["btc-eth"]
            self.assertGreater(u["target_eth"]["b"][-1] / u["target_eth"]["se"][-1], 5)  # btc state predicts eth
            self.assertGreater(u["target_eth"]["oos_ll_gain"][1], 0)
            self.assertGreater(u["pair_edge"][1]["ci90"][0], 0)  # laggard catch-up edge is found
            c = R["pairs"]["btc-eth"]["cross"]["eth"]
            self.assertGreater(c["b"][-1] / c["se"][-1], 5)  # eth market ignores btc market
            self.assertGreater(c["trades"]["cross_m40"]["usd"][0], 0)
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
