import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np
import pandas as pd

import momentum_shadow as ms
import ohioai_match as om
import shadow_rec as sr
from leadlag import fee
from test_shadow import DAY, EP, record

TX = "0x" + "ab" * 32


class Sink:
    def __init__(self):
        self.rows = {"pm": [], "tr": []}

    def add(self, kind, row):
        self.rows[kind].append(row)


class TestTradeCapture(unittest.TestCase):
    def test_last_trade_price_row(self):
        out = Sink()
        trk = {"U": (0, 0), "D": (0, 1)}
        msg = [{"event_type": "last_trade_price", "asset_id": "D", "price": "0.47", "size": "12.5", "side": "BUY",
                "fee_rate_bps": "1000", "timestamp": "1700000000123", "transaction_hash": TX},
               {"event_type": "last_trade_price", "asset_id": "X", "price": "0.5", "size": "1", "side": "SELL", "timestamp": "1"}]  # fmt: skip
        x = sr.on_msg(json.dumps(msg), 99, {"U": (0, "s", "c")}, 5, EP, {"U": ({}, {})}, {}, out, None, trk)
        self.assertEqual(x, 0)
        self.assertEqual(len(out.rows["tr"]), 1)
        r = np.frombuffer(out.rows["tr"][0], om.TR_DT)[0]
        self.assertEqual((r["l"], r["x"], r["tok"], r["sd"], r["p"], r["fee"]), (99, 1700000000123, 1, 0, 4700, 1000))
        self.assertAlmostEqual(float(r["sz"]), 12.5)
        self.assertEqual(r["tx"], bytes.fromhex(TX[2:]))
        # no trk -> ignored (recorder run with --no-trades)
        out2 = Sink()
        sr.on_msg(json.dumps(msg), 99, {"U": (0, "s", "c")}, 5, EP, {"U": ({}, {})}, {}, out2)
        self.assertEqual(out2.rows["tr"], [])


class TestMomentum(unittest.TestCase):
    def test_fill_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)  # +0.3% Binance jump received at 100.03 s; UP book 50/52 until 100.27 s; PM rows 120 ms late
            a = Namespace(rec=td, res=td / "res", days=[DAY], since="2000-01-01", taker_delay_ms=50.0, proc_ms=10.0,
                          relay_save_ms=[0.0, 10.0, 20.0], force=True)  # fmt: skip
            a.res.mkdir()
            ms.eval_day(td, DAY, a)
            O = pd.read_csv(a.res / f"mom-{DAY}.csv")
            p = O[(O.src == 0) & (O.scen == "tokyo") & (O.M == 2.0) & (O.win == 5.0) & (O.cap == 0.02) & (O["mode"] == "first")]
            self.assertEqual(len(p), 1)
            r = p.iloc[0]
            # decision at 100.05 s; tokyo lands +120+10+50 = 180 ms later = 100.23 s < reprice at 100.27 s
            self.assertEqual((r.filled, r.side, r.px, r.asz), (1, "UP", 0.52, 50.0))
            self.assertAlmostEqual(r.t_s, 100.05)
            self.assertFalse((O.side == "DOWN").any())
            (a.res / "settlements.csv").write_text(f"slug,winner\nbtc-updown-5m-{EP},UP\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ms.cmd_report(Namespace(res=a.res, since="2000-01-01", fee_rate=0.07, no_fetch=True))
            want = 100 * (1 - 0.52 - fee(0.52, 0.07))
            self.assertIn(f"fills 1 over 1 days; net {want:+.2f} c/share", buf.getvalue())
            self.assertIn("verdict: RUNNING", buf.getvalue())

    def test_price_band(self):
        side = np.array([1, 1, 1])
        seen = {"UP": np.array([0.10, 0.50, 0.85]), "DOWN": np.full(3, np.nan)}
        arr = {"UP": np.array([0.10, 0.51, 0.85]), "DOWN": np.full(3, np.nan)}
        asz = {"UP": np.full(3, 50.0), "DOWN": np.zeros(3)}
        f, anys = ms.fills_for(side, seen, arr, asz, 0.02, "multi", np.arange(3) * 20_000_000)
        self.assertEqual(f, [(1, "UP")])
        arr["UP"][1] = 0.53  # beyond seen + cap
        self.assertEqual(ms.fills_for(side, seen, arr, asz, 0.02, "first", np.arange(3))[0], [])


class TestMatch(unittest.TestCase):
    def test_match_by_tx(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)
            x = EP * 1000 + 100_200  # matched 200 ms after the jump left Binance, before the book reprices
            with (td / DAY / "tr-01.bin").open("wb") as f:
                f.write(sr.TR_REC.pack(x * 1000 + 120_000, x, 0, 5, EP, 0, 0, 5200, 10.0, 1000, bytes.fromhex(TX[2:])))
            acts = [dict(side="BUY", transaction_hash=TX, outcome="Up", price=0.52, size=10.0,
                         usdc_size=5.2 + 0.07 * 0.52 * 0.48 * 10, timestamp=x // 1000 + 2, slug=f"btc-updown-5m-{EP}"),
                    dict(side="BUY", transaction_hash="0x" + "cd" * 32, outcome="Down", price=0.4, size=5, usdc_size=2, timestamp=x // 1000)]  # fmt: skip
            rows = om.match_day(td, DAY, acts, Namespace(M=2.0, proc_ms=10.0, taker_delay_ms=50.0))
            self.assertEqual([r["matched"] for r in rows], [1, 0])
            r = rows[0]
            self.assertEqual((r["side"], r["taker"], r["px_minus_ask"]), (0, 1, 0.0))
            self.assertAlmostEqual(r["mv0"], 1e4 * np.log(1.003), places=3)
            self.assertAlmostEqual(r["mv500"], 0.0, places=3)
            # we receive the jump at 100.03 s, land at +120 + 10 + 50 -> 100.21 s: 10 ms after their match
            self.assertAlmostEqual(r["our_land_minus_match_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
