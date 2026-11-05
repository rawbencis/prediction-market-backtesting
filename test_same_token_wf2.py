import unittest

import numpy as np

import same_token_wf2 as wf


def reference(m, c, winner, lat=wf.LAT_US, gap=wf.GAP_US, age=wf.AGE_US, size=wf.SIZE):
    """Independent event-loop statement of the replay semantics."""
    x, b, s, ttl = c
    B, S = b / 1000, s / 1000
    W = None  # set once the trigger side is known
    t = m["t"]
    n = len(t)
    if n == 0:
        return wf.FLAT, 0.0, 0.0
    good = [
        (m["bid"][i] >= 0).all() and all(t[i] - m["seen"][i, k] <= age for k in (0, 1))
        for i in range(n)
    ]
    st, side, T, te = "wait", None, None, None
    for i in range(n):
        g = t[i] - t[i - 1] > gap if i else t[0] > gap
        bad = g or (not good[i] and t[i] > age)
        if st == "wait":
            if i and good[i] and good[i - 1] and not g and t[i] <= wf.TRIG_MAX_US:
                ms0 = m["bid"][i - 1] + m["ask"][i - 1]
                ms1 = m["bid"][i] + m["ask"][i]
                cr = [k for k in (0, 1) if ms0[k] < 2 * x <= ms1[k]]
                if len(cr) == 2:
                    return wf.FLAT, 0.0, 0.0
                if cr:
                    st, side, T = "pend", cr[0], t[i]
                    W = None if winner is None else float(winner == side)
            continue
        if bad:
            st = "cpend" if st == "pend" else "cpos"
            break
        if st == "pend" and t[i] >= T + ttl * 1_000_000:
            return wf.EXPIRED, 0.0, 0.0
        if not good[i] or not m["upd"][i, side]:
            continue
        if st == "pend" and t[i] >= T + lat and m["ask"][i, side] < b and m["asz"][i, side] >= size:
            st, te = "pos", t[i]
        elif (
            st == "pos" and t[i] >= te + lat and m["bid"][i, side] > s and m["bsz"][i, side] >= size
        ):
            return wf.EXITED, (S - B) * size, (S - B) * size
    tail = wf.MKT_US - t[-1] > gap
    held = [W - B] if W is not None else [-B, 1 - B]
    if st == "wait":
        return wf.FLAT, 0.0, 0.0
    if st == "cpend" or (st == "pend" and tail):
        o = [0.0, S - B] + held
        return wf.CENS_PEND, min(o) * size, max(o) * size
    if st == "pend":
        return wf.OPEN_END, 0.0, 0.0
    if st == "cpos" or tail:
        o = [S - B] + held
        return wf.CENS_POS, min(o) * size, max(o) * size
    if W is not None:
        return wf.SETTLED, held[0] * size, held[0] * size
    return wf.UNSETTLED, -B * size, (1 - B) * size


def random_market(rng, n=700):
    t = np.cumsum(rng.exponential(0.45e6, n)).astype(np.int64)
    if rng.random() < 0.3:
        t[rng.integers(n) :] += int(rng.uniform(4e6, 9e6))  # gap
    t = t[t < wf.MKT_US]
    n = len(t)
    up = np.clip(500 + np.cumsum(rng.normal(0, rng.choice([8, 40]), n)), 20, 980)
    mid = np.column_stack([up, 1000 - up + rng.normal(0, 8, n)])
    spr = rng.choice([10, 20, 30], (n, 2))
    bid = np.rint(mid - spr / 2).astype(np.int32)
    ask = (bid + spr).astype(np.int32)
    bad = rng.random((n, 2)) < rng.choice([0.0, 0.002, 0.01])
    bid[bad], ask[bad] = -1, -1
    upd = rng.random((n, 2)) < rng.choice([0.85, 0.97])
    upd[0] = True
    seen = np.zeros((n, 2), np.int64)
    for k in (0, 1):
        last = -1_000_000
        for i in range(n):
            if upd[i, k]:
                last = t[i]
            else:
                bid[i, k], ask[i, k] = bid[i - 1, k], ask[i - 1, k]
            seen[i, k] = last
    sz = rng.choice([2.0, 10.0, 100.0], (n, 2)).astype(np.float32)
    return {
        "t": t,
        "bid": bid,
        "ask": ask,
        "bsz": sz,
        "asz": sz[:, ::-1].copy(),
        "upd": upd,
        "seen": seen,
    }


class TestWF2(unittest.TestCase):
    def test_vectorised_matches_reference(self):
        rng = np.random.default_rng(7)
        grid = wf.candidates()
        seen = set()
        for _ in range(120):
            m = random_market(rng)
            winner = rng.choice([None, 0, 1])
            st, lo, hi, _ = wf.evaluate(m, grid, winner)
            for ci in rng.choice(len(grid), 150, replace=False):
                r = reference(m, grid[ci], winner)
                self.assertEqual(
                    (st[ci], round(lo[ci], 9), round(hi[ci], 9)),
                    (r[0], round(r[1], 9), round(r[2], 9)),
                    (grid[ci], winner),
                )
                seen.add(int(r[0]))
        self.assertEqual(seen, set(range(8)))  # every outcome path exercised

    def test_roundtrip_and_settlement(self):
        def m(rows):
            t = np.array([r[0] for r in rows], np.int64) * 1_000_000
            n = len(t)
            bid = np.array([[r[1], 100] for r in rows], np.int32)
            ask = np.array([[r[2], 120] for r in rows], np.int32)
            return {
                "t": t,
                "bid": bid,
                "ask": ask,
                "bsz": np.full((n, 2), 50, np.float32),
                "asz": np.full((n, 2), 50, np.float32),
                "upd": np.ones((n, 2), bool),
                "seen": np.column_stack([t, t]),
            }

        base = [(1, 600, 620), (2, 700, 720), (3, 270, 290)]
        c = [(700, 300, 700, 60)]
        tail = [(k, 400, 420) for k in range(7, 300, 4)]
        st, lo, _, _ = wf.evaluate(m(base + [(4, 710, 730)] + tail), c)
        self.assertEqual(st[0], wf.EXITED)
        self.assertAlmostEqual(lo[0], 0.4 * wf.SIZE)
        st, lo, _, _ = wf.evaluate(m(base + tail), c, winner=0)
        self.assertEqual((st[0], lo[0]), (wf.SETTLED, 0.7 * wf.SIZE))
        st, _, _, _ = wf.evaluate(m(base + tail), c, winner=None)
        self.assertEqual(st[0], wf.UNSETTLED)
        # gap while waiting is not a loss
        st, lo, _, _ = wf.evaluate(m([(1, 500, 520), (20, 510, 530)] + tail), c)
        self.assertEqual((st[0], lo[0]), (wf.FLAT, 0.0))

    def test_side_rows_dedupe(self):
        ts = np.array([1, 2, 2, 5, 5, 9])
        rx = np.array([10, 20, 20, 30, 30, 40])
        q = np.array(
            [
                [0.5, 0.51, 1, 1],
                [0.5, 0.51, 1, 1],
                [0.5, 0.51, 1, 1],
                [0.5, 0.51, 1, 1],
                [0.4, 0.51, 1, 1],
                [0.6, 0.5, 1, 1],
            ]
        )
        r, qq, ok, nconf = wf.side_rows(ts, rx, q)
        self.assertEqual(r.tolist(), [10, 20, 30, 40])
        self.assertEqual(ok.tolist(), [True, True, False, False])  # conflict, crossed
        self.assertEqual(nconf, 1)
        # later exchange time inside one receipt batch wins, no conflict
        r, qq, ok, nconf = wf.side_rows(
            np.array([1, 2]), np.array([7, 7]), np.array([[0.4, 0.5, 1, 1], [0.45, 0.5, 1, 1]])
        )
        self.assertEqual((qq[0, 0], ok[0], nconf), (0.45, True, 0))

    def test_merge_uses_preopen_state(self):
        e = 1000
        e0 = e * 1_000_000
        up = (
            np.array([e0 - 5_000_000, e0 + 1_000_000]),
            np.array([[0.5, 0.52, 9, 9]] * 2),
            np.array([True, True]),
        )
        dn = (np.array([e0 + 500_000]), np.array([[0.47, 0.49, 9, 9]]), np.array([True]))
        m = wf.merge(e, [up, dn])
        self.assertEqual(m["t"].tolist(), [500_000, 1_000_000])
        self.assertEqual(m["bid"][0].tolist(), [500, 470])
        self.assertEqual(m["upd"].tolist(), [[False, True], [True, False]])

    def test_choose_cash_and_no_future(self):
        S = np.array([[1.0, -1]] * 4 + [[-1e9, -1e9]])
        N = np.full((5, 2), 100)
        self.assertEqual(wf.choose(S, N, [0, 1], [2, 3], 1, 2), 0)
        self.assertIsNone(wf.choose(-np.abs(S), N, [0, 1], [2, 3], 1, 2))

    def test_cache_to_grid(self):
        import tempfile
        from pathlib import Path

        rng = np.random.default_rng(5)
        with tempfile.TemporaryDirectory() as d:
            cache, out = Path(d) / "c", Path(d) / "o"
            ddir = cache / "2026-06-24"
            ddir.mkdir(parents=True)
            ms = {1782259200 + 300 * k: random_market(rng) for k in range(3)}
            for e, m in ms.items():
                np.savez_compressed(ddir / f"{e}.npz", **m)
            (ddir / "_done.json").write_text("{}")
            (cache / "2026-06-25").mkdir()  # unfinished day is ignored
            out.mkdir()
            self.assertEqual(wf.done_days(cache), [ddir])
            name, n = wf._grid_day((ddir, out, {}))
            z = np.load(out / f"{name}.npz")
            grid = wf.candidates()
            self.assertEqual((n, z["lo"].shape), (3, (3, len(grid))))
            for i, e in enumerate(z["epochs"]):
                _, lo, _, _ = wf.evaluate(ms[int(e)], grid)
                np.testing.assert_allclose(z["lo"][i], lo, atol=1e-6)


class TestPrepParquet(unittest.TestCase):
    def test_load_market_parquet(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("pyarrow not installed")
        import tempfile
        from pathlib import Path

        e = 1782876000
        e0 = e * 1_000_000
        with tempfile.TemporaryDirectory() as d:
            for side, px in (("UP", ("0.5", "0.52")), ("DOWN", ("0.47", "0.49"))):
                rx = [e0 - 3_000_000, e0 + 1_000_000, e0 + 1_000_000]
                tb = pa.table(
                    {
                        "timestamp_us": pa.array([r - 8000 for r in rx], pa.int64()),
                        "local_timestamp_us": pa.array(rx, pa.int64()),
                        "bid_price": [px[0], px[0], "bad"],
                        "bid_size": ["10", "10", "10"],
                        "ask_price": [px[1], px[1], px[1]],
                        "ask_size": ["10", "10", "10"],
                    }
                )
                pq.write_table(tb, Path(d) / f"btc-updown-5m-{e}_{side}.parquet")
            got, m, diag = wf.load_market((Path(d), e))
            (Path(d) / "day").mkdir()
            wf.prep_market((Path(d), e, Path(d) / "day"))
            self.assertEqual([x for x, _ in wf.markets(Path(d) / "day")], [e])
        self.assertEqual(got, e)
        self.assertEqual(m["t"].tolist(), [1_000_000])
        self.assertEqual(diag["conflicts"], 2)  # same receipt+exchange time, different quote
        self.assertTrue((m["bid"] == -1).all())


if __name__ == "__main__":
    unittest.main()
