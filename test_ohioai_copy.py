import contextlib
import re
import io
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import act_rec as ar
import ohioai_copy as oc
from leadlag import fee
from test_shadow import DAY, EP, record

TX = "ab" * 32


def fills(root, x, px=0.52, taker=1):
    p = root / "o"
    p.mkdir(exist_ok=True)
    pd.DataFrame([dict(matched=1, slug=f"btc-updown-5m-{EP}", asset="btc", tf=5, epoch=EP, x_ms=x, side=0, px=px,
                       size=10.0, taker=taker, tx=TX)]).to_csv(p / "f.csv", index=False)  # fmt: skip
    (p / "settlements.csv").write_text(f"slug,winner\nbtc-updown-5m-{EP},UP\n")
    return p / "f.csv"


class TestCopy(unittest.TestCase):
    def run_copy(self, root, f, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            oc.main(["--rec", str(root), "--fills", str(f), "--days", DAY, "--no-fetch", *extra])
        return buf.getvalue()

    def test_copy_fills_only_before_the_book_reprices(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)  # UP 50/52 until exchange 100.27 s, then 95/97
            x = EP * 1000 + 100_200
            out = self.run_copy(td, fills(td, x))
            net = 100 * (1 - 0.52 - fee(0.52, 0.07))
            # land = x + d + 10 + 50 + 5: d=0 -> 100.265 s fills at .52; d=100 -> 100.365 s sees .97 > .54 -> no fill
            self.assertRegex(out, r"\b0 ms\s+1\s+100.0\s+\+0.00c\s+" + re.escape(f"{net:+.2f}"))
            self.assertRegex(out, r"100 ms\s+0\s+0.0")
            self.assertIn(f"their own net {net:+.2f} c/share", out)
            self.assertIn("no taker-census rows", out)

    def test_measured_delay_from_the_census(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)
            x = EP * 1000 + 100_200
            p = dict(proxyWallet="0x129246beaac356e104c397f0985c11b7469be5a6", side="BUY", outcome="Up", price=0.52,
                     size=10, fee=0.0, transactionHash="0x" + TX, timestamp=x // 1000, slug=f"btc-updown-5m-{EP}")
            (td / DAY / "act-01.bin").write_bytes(ar.pack((x + 380) * 1000, x + 300, p) + ar.pack((x + 900) * 1000, 0, p))
            out = self.run_copy(td, fills(td, x))
            self.assertIn("p10/p50/p90 = 380/380/380 ms", out)  # first sighting of the tx
            self.assertRegex(out, r"measured p50 380\s+0\s+0.0")

    def test_maker_fill_reference_has_no_fee(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            record(td)
            out = self.run_copy(td, fills(td, EP * 1000 + 100_200, px=0.50, taker=0))
            self.assertIn("maker fills: 1 of their buys; their own net +50.00", out)

    def test_ci_clusters_by_market(self):
        lo, hi = oc.ci(np.array([1.0] * 10 + [-1.0] * 10), np.array(["a"] * 10 + ["b"] * 10))
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)


if __name__ == "__main__":
    unittest.main()
