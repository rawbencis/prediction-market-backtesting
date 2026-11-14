import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import sig_shadow as ss

W1 = "0x" + "ab" * 20
W2 = "0x" + "cd" * 20


def pad(a):
    return "0x" + "0" * 24 + a[2:]


class T(unittest.TestCase):
    def test_wallets_any_format(self):
        with tempfile.TemporaryDirectory() as td:
            f1, f2 = Path(td) / "m.json", Path(td) / "w.json"
            f1.write_text(json.dumps([{"address": W1.upper().replace("0X", "0x"), "segment": "weather"}]))
            f2.write_text(json.dumps({"wallets": {W2: {"score": 1}}, "cond": "0x" + "ef" * 32,
                                      "ex": ss.EXCHANGES[0], "zero": "0x" + "0" * 40}))
            self.assertEqual(ss.load_wallets([f1, f2, Path(td) / "missing.json"]), {W1, W2})

    def test_parse_log(self):
        data = "0x" + f"{0:064x}" + f"{12345:064x}" + "0" * 192
        lg = {"topics": [ss.FILL_TOPIC, "0x" + "11" * 32, pad(W1), pad(W2)], "data": data,
              "transactionHash": "0xT", "blockNumber": "0x10"}
        r = ss.parse_log(lg, {W1})
        self.assertEqual((r["wallet"], r["role"], r["side"], r["asset"], r["block"]), (W1, "owner", "BUY", "12345", 16))
        self.assertEqual(ss.parse_log(lg, {W2})["role"], "counterparty")
        self.assertIsNone(ss.parse_log(lg, {"0x" + "99" * 20}))
        self.assertIsNone(ss.parse_log({**lg, "removed": True}, {W1}))

    def test_parse_rtds(self):
        msg = {"topic": "activity", "type": "trades", "timestamp": 1763100000123,
               "payload": {"proxyWallet": W1.upper().replace("0X", "0x"), "transactionHash": "0xT", "side": "BUY",
                           "asset": "123", "price": 0.55, "size": 10, "slug": "x", "timestamp": 1763100000}}
        r, ts = ss.parse_rtds(msg, {W1})
        self.assertEqual((r["wallet"], r["tx"], r["side"], ts), (W1, "0xT", "BUY", 1763100000123))
        self.assertIsNone(ss.parse_rtds(msg, {W2})[0])

    def test_compare(self):
        with tempfile.TemporaryDirectory() as td:
            A, B = Path(td) / "a", Path(td) / "b"
            for d, alch_us, rtds_us in ((A, 3_000_000, 300_000), (B, 2_800_000, 100_000)):
                o = ss.Out(d)
                o.write(dict(feed="alch", local_us=alch_us, tx="0xT", wallet=W1))
                o.write(dict(feed="alch", local_us=alch_us + 50, tx="0xT", wallet=W1))  # duplicate: first wins
                o.write(dict(feed="rtds", local_us=rtds_us, tx="0xT", wallet=W1))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ss.cmd_compare(Namespace(a=A, b=B, since=""))
            s = buf.getvalue()
            self.assertIn("alch: n=    1 p10    -200", s)
            self.assertIn("rtds: n=    1 p10    -200", s)
            self.assertIn("box A: alch - rtds n=    1 p10    2700", s)


if __name__ == "__main__":
    unittest.main()
