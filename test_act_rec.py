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


if __name__ == "__main__":
    unittest.main()
