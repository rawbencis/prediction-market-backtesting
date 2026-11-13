import csv
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np

import wallet_dissect as wd

EP = 1782000000  # a 5m window start (divisible by 300)


def act(t, side, outcome, px, sz, typ="TRADE", slug=None):
    return dict(type=typ, timestamp=EP + t, side=side, outcome=outcome, price=px, size=sz, usdcSize=round(px * sz, 6) if typ == "TRADE" else sz, slug=slug or f"btc-updown-5m-{EP}", conditionId="0xc", asset=f"a{outcome}", transactionHash=f"0x{t}{side}{outcome}{sz}")


class T(unittest.TestCase):
    def test_books_pair_lock_and_pnl(self):
        rows = [act(10, "BUY", "Up", 0.46, 50), act(20, "BUY", "Down", 0.50, 50), act(30, "BUY", "Up", 0.40, 10), act(200, "SELL", "Up", 0.70, 5)]
        B = wd.books(rows, {f"btc-updown-5m-{EP}": "DOWN"})
        b = B[f"btc-updown-5m-{EP}"]
        cost = 0.46 * 50 + 0.50 * 50 + 0.40 * 10
        self.assertAlmostEqual(b["pnl"], 0.70 * 5 - cost + 50)  # Down wins, 50 shares
        avg_up = (0.46 * 50 + 0.40 * 10) / 60
        self.assertAlmostEqual(b["pair_cost"], avg_up + 0.50)
        self.assertAlmostEqual(b["pair_lock"], 50 * (1 - avg_up - 0.50))
        self.assertEqual(b["q"], [55, 50])

    def test_split_merge(self):
        rows = [act(0, None, None, 0, 100, "SPLIT"), act(100, "SELL", "Up", 0.55, 100), act(290, None, None, 0, 0, "REDEEM")]
        b = wd.books(rows, {f"btc-updown-5m-{EP}": "DOWN"})[f"btc-updown-5m-{EP}"]
        self.assertAlmostEqual(b["pnl"], -100 + 55 + 100)

    def test_book_join_finds_lag_and_maker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c"
            d = cache / "2026-06-20"
            d.mkdir(parents=True)
            t = np.arange(0, 300_000_000, 100_000, dtype=np.int64)
            n = len(t)
            bu = np.full(n, 480, np.int32)
            bu[t >= 50_000_000] = 450  # bid drops at 50s
            m = dict(t=t, bid=np.stack([bu, 1000 - bu - 20], 1), ask=np.stack([bu + 20, 1000 - bu], 1))
            np.savez(d / f"{EP}.npz", **m)
            # filled at 0.48 (bid before the drop) but block-stamped 3 s later at 52 s
            rows = [act(52, "BUY", "Up", 0.48, 10), act(150, "BUY", "Up", 0.45, 10)]
            B = wd.books(rows, {f"btc-updown-5m-{EP}": "UP"})
            r = wd.book_join(B, cache)
            self.assertEqual(r["fills"], 2)
            self.assertIn(r["best_lag_s"], (3, 4, 5, 6, 7, 8, 9, 10))
            self.assertEqual(r["class_counts"].get("at_bid"), 2)
            self.assertAlmostEqual(r["markout_milli_per_share_size_weighted"]["at_bid"]["settle"], 1000 - 465)

    def test_report_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rows = []
            for j in range(30):
                s = f"btc-updown-5m-{EP + 300 * j}"
                rows += [act(10, "BUY", "Up", 0.47, 20, slug=s), act(15, "BUY", "Down", 0.49, 20, slug=s)]
            with (out / "act.jsonl").open("w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            with (out / "winners.csv").open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["slug", "winner"])
                w.writerows([(f"btc-updown-5m-{EP + 300 * j}", "UP" if j % 2 else "DOWN") for j in range(30)])
            wd.cmd_report(Namespace(out=out, cache=None, wallet=wd.WALLET))
            rep = json.loads((out / "report.json").read_text())
            self.assertAlmostEqual(rep["pnl_usd"], 30 * 20 * (1 - 0.96), places=4)
            self.assertEqual(rep["structure"]["btc-5m"]["pct_both_sides"], 1.0)

    def test_fetch_v2_pages_and_report(self):
        def v2(t, side, outcome, px, sz, j):
            return dict(proxy_wallet=wd.WALLET, timestamp=EP + 300 * j + t, condition_id=f"0x{j}", type="TRADE", size=sz, usdc_size=px * sz,
                        transaction_hash=f"0x{j}{t}", price=px, token_id=f"tok{outcome}{j}", side=side, outcome_index=0 if outcome == "Up" else 1,
                        slug=f"btc-updown-5m-{EP + 300 * j}", outcome=outcome)

        act = [v2(5, "BUY", "Up", 0.47, 10, j) for j in range(4)] + [v2(9, "BUY", "Down", 0.50, 10, j) for j in range(4)]
        calls = []

        def fake_get(url, params, tries=6):
            calls.append((url, dict(params)))
            if url.endswith("/activity"):
                if "cursor" not in params:
                    return dict(data=act[:5], pagination=dict(has_more=True, next_cursor="c1"))
                return dict(data=act[5:], pagination=dict(has_more=False, next_cursor=None))
            if url.endswith("/trades"):
                return dict(data=[act[0]], pagination=dict(has_more=False, next_cursor=None))
            return dict(data=dict(points=[dict(timestamp=EP, realized_pnl=1.0, maker_rebate=0.5, volume_usdc=100), dict(timestamp=EP + 86400 * 40, realized_pnl=3.0, maker_rebate=0.7, volume_usdc=300)]))

        orig = wd.get
        wd.get = fake_get
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                wd.cmd_fetch(Namespace(out=out, wallet=wd.WALLET, since=0, sleep=0))
                self.assertEqual(sum(1 for _ in (out / "act.jsonl").open()), 8)
                self.assertEqual(calls[1][1]["cursor"], "c1")
                self.assertEqual(calls[1][1]["type"], calls[0][1]["type"])
                with (out / "winners.csv").open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["slug", "winner"])
                    w.writerows([(f"btc-updown-5m-{EP + 300 * j}", "UP") for j in range(4)])
                wd.cmd_report(Namespace(out=out, cache=None, wallet=wd.WALLET))
                rep = json.loads((out / "report.json").read_text())
                self.assertAlmostEqual(rep["pnl_usd"], 4 * (10 - 4.7 - 5.0), places=4)
                self.assertEqual(rep["maker_taker"]["taker_fill_share"], 0.125)
                self.assertEqual(rep["maker_taker"]["unmatched_taker_rows"], 0)
                self.assertEqual(len(rep["official_monthly"]), 2)
        finally:
            wd.get = orig


if __name__ == "__main__":
    unittest.main()
