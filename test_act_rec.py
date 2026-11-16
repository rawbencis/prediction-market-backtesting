import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
import contextlib
import io

import act_rec as ar

W = "0x129246beaac356e104c397f0985c11b7469be5a6"
TX = "0x" + "ab" * 32


def payload(**kw):
    p = dict(proxyWallet=W, side="BUY", outcome="Up", outcomeIndex=0, price=0.52, size=10.0, fee=0.0175,
             transactionHash=TX, timestamp=1794650656, slug="btc-updown-5m-1794650400")
    p.update(kw)
    return p


class TestAct(unittest.TestCase):
    def test_pack_roundtrip(self):
        b = ar.pack(1_794_650_656_500_000, 1_794_650_656_400, payload())
        r = ar.ACT_REC.unpack(b)
        self.assertEqual(r[2:8], (1794650656, 0, 5, 1794650400, 0, 0))
        self.assertAlmostEqual(r[8], 0.52, places=5)
        self.assertEqual("0x" + r[11].hex(), W)
        self.assertEqual("0x" + r[12].hex(), TX)
        r = ar.ACT_REC.unpack(ar.pack(1, 2, payload(outcome="Down", side="SELL", slug="doge-updown-15m-9", fee=None)))
        self.assertEqual((r[3], r[4], r[5], r[6], r[7]), (4, 15, 9, 1, 1))
        self.assertEqual(r[10], -1.0)

    def test_pack_skips_other_markets_and_bad_rows(self):
        for bad in (dict(slug="btc-updown-1h-1"), dict(slug="will-x-happen"), dict(outcome="Yes"), dict(side="HOLD"),
                    dict(price=None), dict(proxyWallet="zz"), dict(transactionHash=None)):
            self.assertIsNone(ar.pack(1, 2, payload(**bad)), bad)

    def test_frames(self):
        f = json.dumps({"topic": "activity", "type": "trades", "timestamp": 5, "payload": payload()})
        self.assertEqual([e for e, _ in ar.frames(f)], [5])
        self.assertEqual(list(ar.frames("pong")), [])
        self.assertEqual(list(ar.frames(json.dumps({"topic": "comments", "payload": {}}))), [])

    def test_top(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "2026-11-14"
            d.mkdir()
            rows = [ar.pack(1, 2, payload()), ar.pack(1, 2, payload(proxyWallet="0x" + "11" * 20, side="SELL", size=100.0)),
                    ar.pack(1, 2, payload(fee=0.0))]
            (d / "act-10.bin").write_bytes(b"".join(rows) + b"\0" * 5)  # torn tail ignored
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ar.cmd_top(Namespace(rec=Path(td), days=None, asset="btc", tf=5, n=10))
            out = buf.getvalue()
            self.assertIn("rows=3 wallets=2 txs=1", out)
            self.assertIn("<- OhioAI", out)
            self.assertIn("OhioAI rank by volume: 2 of 2", out)


    def test_settled_pnl_and_ranking(self):
        import numpy as np

        mk = "0x" + "22" * 20
        rows = [
            ar.pack(1, 2, payload(price=0.40, size=10, fee=0.02)),                       # OhioAI taker buys UP at .40, UP wins
            ar.pack(1, 2, payload(proxyWallet=mk, side="SELL", price=0.40, size=10, fee=None)),  # maker sells it
            ar.pack(1, 2, payload(proxyWallet=mk, outcome="Down", price=0.55, size=10, fee=None)),  # maker buys DOWN, loses
        ]
        r = np.frombuffer(b"".join(rows), dtype=np.dtype(ar.ACT_DT))
        pnl, key = ar.settled_pnl(r, {"btc-updown-5m-1794650400": "UP"})
        self.assertAlmostEqual(pnl[0], (1 - 0.40) * 10 - 0.07 * 0.4 * 0.6 * 10, places=4)
        self.assertAlmostEqual(pnl[1], (0.40 - 1) * 10, places=4)
        self.assertAlmostEqual(pnl[2], -0.55 * 10, places=4)
        self.assertTrue(np.isnan(ar.settled_pnl(r, {})[0]).all())
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "2026-11-14"
            d.mkdir()
            (d / "act-10.bin").write_bytes(b"".join(rows))
            st = Path(td) / "s.csv"
            st.write_text("slug,winner\nbtc-updown-5m-1794650400,UP\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ar.cmd_pnl(Namespace(rec=Path(td), days=None, asset="btc", tf=5, min_usd=0, n=5, settle=st, no_fetch=True))
            out = buf.getvalue()
            self.assertIn("OhioAI rank by PnL: 1 of 2", out)
            self.assertIn("+6 ", out.split("TOP")[1].splitlines()[2])  # OhioAI +5.83 $ on top


    def test_profile_buckets_by_time_left_and_price(self):
        # window opens 1794650400 (5m): ts 1794650690 -> 10 s left; ts 1794650500 -> 200 s left
        late = ar.pack(1, 2, payload(price=0.60, size=10, fee=0.02, timestamp=1794650690))
        early = ar.pack(1, 2, payload(price=0.40, size=10, fee=0.02, timestamp=1794650500, outcome="Down"))
        maker = ar.pack(1, 2, payload(proxyWallet="0x" + "33" * 20, side="SELL", price=0.60, size=10, fee=None,
                                      timestamp=1794650690))
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "2026-11-14"
            d.mkdir()
            (d / "act-10.bin").write_bytes(late + early + maker)
            st = Path(td) / "s.csv"
            st.write_text("slug,winner\nbtc-updown-5m-1794650400,UP\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ar.cmd_profile(Namespace(rec=Path(td), days=None, asset="btc", tf=5, wallets=["0x1292", "0xdead"],
                                         settle=st, no_fetch=True))
            out = buf.getvalue()
        self.assertIn("ALL TAKER FLOW: 2 fills", out)
        self.assertIn("ALL MAKER FLOW: 1 fills", out)
        self.assertIn("300-180s n=1 -104.2c/$", out)     # bought DOWN at .40, lost: -4 $ - fee on 4 $
        self.assertIn("30-10s n=1 +63.9c/$", out)        # bought UP at .60 with 10 s left, won: +4 $ - fee on 6 $
        self.assertIn("0xdead: no rows", out)
        self.assertIn("<- OhioAI", out)


    def test_fills_joins_exchange_time_binance_book_and_counterparties(self):
        from test_shadow import DAY, EP, record

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)  # BTC 5m window EP: Binance +0.3% at 100.03 s, UP book 50/52 until 100.27 s
            tx = "0x" + "cd" * 32
            p = dict(slug=f"btc-updown-5m-{EP}", transactionHash=tx, timestamp=EP + 101)
            rows = [ar.pack(1, 2, payload(price=0.52, size=600, fee=0.02, **p)),
                    ar.pack(1, 2, payload(proxyWallet="0x" + "44" * 20, side="SELL", price=0.52, size=600, fee=None, **p))]
            (td / DAY / "act-01.bin").write_bytes(b"".join(rows))
            import ohioai_match as om
            import shadow_rec as sr

            with (td / DAY / "tr-01.bin").open("ab") as f:
                f.write(sr.TR_REC.pack(1, EP * 1000 + 100_200, 0, 5, EP, 0, 0, 5200, 600.0, 0, bytes.fromhex(tx[2:])))
            st = td / "s.csv"
            st.write_text(f"slug,winner\nbtc-updown-5m-{EP},UP\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ar.cmd_fills(Namespace(rec=td, days=[DAY], asset="btc", tf=5, wallet="0x1292", settle=st, no_fetch=True))
            out = buf.getvalue()
            _ = om
        self.assertIn("1 of 1 txs found in trade prints", out)
        line = [ln for ln in out.splitlines() if "taker" in ln][0]
        self.assertIn(" 199.8 UP   BUY  taker  0.52     600", line)  # exchange ms 100.200 s into a 300 s window
        self.assertIn("+30.0bp", line)                               # Binance +0.3% since the open
        self.assertIn("0.510", line)                                  # UP mid 50/52 before the 100.27 s reprice
        self.assertIn(" W    1: 0x444444", line)


if __name__ == "__main__":
    unittest.main()
