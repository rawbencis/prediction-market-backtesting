import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np

import shadow_eval as se
import shadow_rec as sr

DAY = "2026-11-02"
EP = 1793577600 + 3600  # 2026-11-02 01:00:00 UTC
assert EP % 900 == 0


class Sink:
    def __init__(self):
        self.rows = []

    def add(self, kind, row):
        self.rows.append(sr.PM_REC.unpack(row))


class TestOnMsg(unittest.TestCase):
    def setUp(self):
        self.toks = {"U": (2, "sol-updown-5m-1", "0xc")}
        self.books, self.last, self.out = {"U": ({}, {})}, {}, Sink()

    def feed(self, msg, lus=1_000):
        return sr.on_msg(json.dumps(msg), lus, self.toks, 5, 1, self.books, self.last, self.out)

    def test_book_then_changes(self):
        x = self.feed([{"event_type": "book", "asset_id": "U", "timestamp": "500",
                        "bids": [{"price": ".48", "size": "30"}, {"price": "0.47", "size": "9"}],
                        "asks": [{"price": "0.52", "size": "25"}, {"price": "0.6", "size": "0"}]},
                       {"event_type": "book", "asset_id": "OTHER", "timestamp": "500", "bids": [], "asks": []}])  # fmt: skip
        self.assertEqual(x, 500)
        self.assertEqual(self.out.rows, [(1_000, 500, 2, 5, 1, 4800, 5200, 30.0, 25.0)])
        # deeper level: top unchanged -> no row; other token ignored
        self.feed({"event_type": "price_change", "timestamp": "600", "price_changes": [
            {"asset_id": "U", "price": "0.46", "size": "5", "side": "BUY"},
            {"asset_id": "OTHER", "price": "0.9", "size": "5", "side": "SELL"}]})  # fmt: skip
        self.assertEqual(len(self.out.rows), 1)
        # best ask removed -> empty ask side (0); better bid
        self.feed({"event_type": "price_change", "timestamp": "700", "price_changes": [
            {"asset_id": "U", "price": "0.52", "size": "0", "side": "SELL"},
            {"asset_id": "U", "price": "0.49", "size": "7", "side": "BUY"}]}, 2_000)  # fmt: skip
        self.assertEqual(self.out.rows[-1], (2_000, 700, 2, 5, 1, 4900, 0, 7.0, 0.0))
        # legacy shape
        self.feed({"event_type": "price_change", "asset_id": "U", "timestamp": "800",
                   "changes": [{"price": "0.55", "size": "3", "side": "SELL"}]}, 3_000)  # fmt: skip
        self.assertEqual(self.out.rows[-1][5:], (4900, 5500, 7.0, 3.0))
        self.assertEqual(self.feed({"event_type": "last_trade_price", "asset_id": "U"}), 0)

    def test_rows_match_numpy_dtypes(self):
        r = np.frombuffer(sr.PM_REC.pack(7, 8, 1, 15, EP, 4800, 5200, 3.5, 4.5), se.PM_DT)[0]
        self.assertEqual((r["l"], r["x"], r["a"], r["tf"], r["e"], r["b"], r["k"]), (7, 8, 1, 15, EP, 4800, 5200))
        b = np.frombuffer(sr.BN_REC.pack(7, 8, 1, 4, 0.1234), se.BN_DT)[0]
        self.assertEqual((b["src"], b["a"], b["p"]), (1, 4, 0.1234))


def record(root, reprice_s=100.27):
    """BTC 5m market: Binance flat at S0 until a +0.3% jump received 100.03 s in; UP book 50/52 until
    it reprices to 95/97 at exchange time reprice_s. Polymarket rows reach the recorder 120 ms late."""
    d = root / DAY
    d.mkdir(parents=True)
    r = np.random.default_rng(0)
    e = EP * 1_000_000
    pre = np.arange(e - 1900 * 1_000_000, e, 100_000)
    p = 60000 * np.exp(np.cumsum(r.normal(0, 1e-5, len(pre))))
    s0 = p[-1]
    live = np.arange(e, e + 300 * 1_000_000, 100_000)
    rows = [(int(t), v) for t, v in zip(pre, p)] + [(int(t), s0) for t in live if t < e + 100_030_000]
    rows += [(e + 100_030_000, s0 * 1.003)] + [(int(t), s0 * 1.003) for t in live if t > e + 100_030_000]
    with (d / "bn-01.bin").open("wb") as f:
        for t, v in rows:
            f.write(sr.BN_REC.pack(t, t // 1000 - 4, 0, 0, v))
    with (d / "pm-01.bin").open("wb") as f:
        for x_ms, b, k in ((EP * 1000 - 30_000, 5000, 5200), (EP * 1000 + int(reprice_s * 1000), 9500, 9700)):
            f.write(sr.PM_REC.pack(x_ms * 1000 + 120_000, x_ms, 0, 5, EP, b, k, 50.0, 50.0))


def run_eval(td, delay):
    a = Namespace(rec=td, res=td / f"res{delay}", days=[DAY], taker_delay_ms=delay, proc_ms=10.0,
                  relay_save_ms=[0.0, 10.0, 20.0], k=0.65, fee_rate=0.07, force=True)  # fmt: skip
    a.res.mkdir()
    lat = se.eval_day(td, DAY, a)
    import pandas as pd

    return a, lat, pd.read_csv(a.res / f"orders-{DAY}.csv")


class TestEval(unittest.TestCase):
    def test_timing_decides_who_fills(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)
            a, lat, O = run_eval(td, 50.0)
            self.assertEqual(lat["pm_ms"], [120.0, 120.0, 120.0])
            self.assertEqual(lat["spot_ms"][1], 4.0)
            self.assertEqual(lat["markets"], 1)
            o = O[O.theta == 0.08].set_index("scen")
            # decision at 100.1 s; book reprices at 100.27 s. tokyo matches at +180 ms, colo-20 at +161 ms
            self.assertEqual(o.filled.to_dict(), {"tokyo": 0, "colo-0": 0, "colo-10": 0, "colo-20": 1})
            f = o.loc["colo-20"]
            self.assertEqual((f.side, f.px, f.asz), ("UP", 0.52, 50.0))
            self.assertAlmostEqual(f.t_s, 100.1)
            self.assertGreater(f.fair, 0.99)
            # no taker delay: everyone gets there first
            _, _, O0 = run_eval(td, 0.0)
            self.assertTrue((O0[O0.theta == 0.08].filled == 1).all())
            # report: UP wins -> 1 - 0.52 - fee
            (a.res / "settlements.csv").write_text(f"slug,winner\nbtc-updown-5m-{EP},UP\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                se.cmd_report(Namespace(res=a.res, theta=0.08, cap=100.0, fee_rate=0.07, no_fetch=True))
            self.assertIn("uplift colo-20 vs tokyo: +2.31 $/day", buf.getvalue())
            self.assertIn("uplift colo-0 vs tokyo: +0.00 $/day", buf.getvalue())

    def test_dropped_connection_blocks_fills(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td, reprice_s=250.0)
            x = EP * 1000 + 50_000  # connection drops 50 s in and never comes back before the jump
            with (td / DAY / "pm-01.bin").open("ab") as f:
                f.write(sr.PM_REC.pack(x * 1000 + 120_000, x, 0, 5, EP, 0, 0, 0.0, 0.0))
            _, _, O = run_eval(td, 50.0)
            self.assertTrue(O[(O.t_s < 250) & (O.filled == 1)].empty)


if __name__ == "__main__":
    unittest.main()
