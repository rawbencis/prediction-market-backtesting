import csv
import json
import math
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import martingale_pair as mp

DT = 100_000
N = 3000
PHI = np.vectorize(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))))


def synth(rng, kappa=1.0, lag=0, half=5, take_p=0.3):
    """BTC-like walk -> fair p; makers quote PHI(kappa*z) lagged by `lag` events; mirrored books."""
    sig = 1.0
    x = np.cumsum(rng.normal(0, sig, N))
    left = np.maximum((N - np.arange(N)) * sig**2, 1e-9)
    z = x / np.sqrt(left)
    zq = np.r_[np.zeros(lag), z[: N - lag]] if lag else z
    q = np.clip(PHI(kappa * zq) * 1000, 15, 985)
    bu = (np.floor((q - half) / 10) * 10).astype(np.int32).clip(10, 980)
    au = (bu + 10).astype(np.int32)
    t = np.arange(N, dtype=np.int64) * DT
    bsz = np.full((N, 2), 100.0, np.float32)
    # uninformed taker flow eats into the best bid between quote moves
    for k in (0, 1):
        b = bu if k == 0 else 1000 - au
        for i in range(1, N):
            if b[i] == b[i - 1] and rng.random() < take_p:
                bsz[i, k] = max(bsz[i - 1, k] - rng.integers(1, 30), 5)
            elif b[i] == b[i - 1]:
                bsz[i, k] = bsz[i - 1, k]
    m = dict(
        t=t,
        bid=np.stack([bu, 1000 - au], 1).astype(np.int32),
        ask=np.stack([au, 1000 - bu], 1).astype(np.int32),
        bsz=bsz,
        asz=np.full((N, 2), 100.0, np.float32),
        upd=np.ones((N, 2), bool),
        seen=np.stack([t, t], 1),
    )
    return m, int(x[-1] <= 0)  # winner: 0 UP, 1 DOWN


def write_cache(root, days=30, per_day=40, seed=0, **kw):
    rng = np.random.default_rng(seed)
    d0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = []
    for d in range(days):
        day = d0 + timedelta(days=d)
        dd = root / day.date().isoformat()
        dd.mkdir(parents=True)
        for j in range(per_day):
            e = int(day.timestamp()) + 300 * j
            m, w = synth(rng, **kw)
            np.savez_compressed(dd / f"{e}.npz", **m)
            rows.append(dict(slug=f"btc-updown-5m-{e}", winner=("UP", "DOWN")[w]))
        (dd / "_done.json").write_text("{}")
    with (root / "settlements.csv").open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["slug", "winner"])
        wr.writeheader()
        wr.writerows(rows)


def run(tmp, **kw):
    cache, out = Path(tmp) / "c", Path(tmp) / "o"
    write_cache(cache, **kw)
    mp.cmd_scan(Namespace(cache=cache, out=out, workers=2, days=0, skip="", force=True))
    mp.cmd_report(Namespace(out=out))
    return json.loads((out / "report.json").read_text())


class T(unittest.TestCase):
    def test_fill_models_nested(self):
        m, _ = synth(np.random.default_rng(3))
        gs, S = mp.sides(m)
        PX, AV = mp.f1(m, gs, S)
        f = AV > 0
        for a, b in ((0, 1), (1, 2), (2, 3)):
            self.assertTrue((f[:, a] <= f[:, b]).all(), (a, b))

    def test_flat_book(self):
        m, _ = synth(np.random.default_rng(0), take_p=0.0)
        m["bid"][:] = [490, 490]
        m["ask"][:] = [510, 510]
        gs, S = mp.sides(m)
        PX, AV = mp.f1(m, gs, S)
        self.assertEqual(int((AV > 0).sum()), 0)

    def test_recover_sizes(self):
        px = np.array([[500 - 30 * j for j in range(mp.NR)]], float)
        s = mp.scale_sizes("recover", px)[0]
        Q = np.cumsum(s)
        C = np.cumsum(s * px[0])
        self.assertTrue((Q * 1000 - C >= mp.BASE * 500 - 1e-6).all())

    def test_zone_recovery_last_side_wins_target(self):
        m, _ = synth(np.random.default_rng(5))
        gs, _ = mp.sides(m)
        # path-independent check: rerun with both winners, the last bought side's win must be >= target
        r0, r1 = mp.f3(m, gs, 0), mp.f3(m, gs, 1)
        both = np.maximum(r0[:, 0], r1[:, 0])
        cap0 = np.array([c[5] == 0 for c in mp.F3])
        ok = ~np.isnan(both) & cap0
        self.assertTrue((both[ok] > 0).all())

    def test_end_to_end_calibrated_and_planted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = run(tmp, days=24, per_day=60, seed=1, kappa=1.0, lag=3)
        fam = rep["families"]
        for k in ("F1/through", "F1/touch", "F2/touch"):
            self.assertLess(fam[k]["median_dev_c_per_usd_reb"], 0)
            self.assertNotEqual(rep["verdict"][k], "PASS")
        print("\ncalibrated+lag:", {k: (v["best_dev"]["c_per_usd"], v["best_hold"]["c_per_usd"]) for k, v in fam.items()})
        with tempfile.TemporaryDirectory() as tmp:
            rep = run(tmp, days=24, per_day=60, seed=2, kappa=2.0, lag=0)
        fam = rep["families"]
        print("planted overreaction:", {k: (v["best_dev"]["c_per_usd"], v["best_hold"]["c_per_usd"], v["median_dev_c_per_usd_reb"]) for k, v in fam.items()})
        self.assertGreater(fam["F1/touch"]["median_dev_c_per_usd_reb"], 0)
        self.assertEqual(rep["verdict"]["F1/touch"], "PASS")


if __name__ == "__main__":
    unittest.main()
