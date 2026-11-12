import asyncio
import csv
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

import lighter_eval as le
import lighter_rec as lr

E = 1794391200  # 2026-11-11 10:00 UTC


def lb(rows):
    a = np.zeros(len(rows), le.LB_DT)
    for i, (t, b, k, bs, ks) in enumerate(rows):
        a[i] = (int((E + t) * 1e6), 0, 0, 15, E, int(b * 1e4), bs, 0, 0, 0, 0, int(k * 1e4), ks, 0, 0, 0, 0)
    return a


def pm(rows):
    a = np.zeros(len(rows), le.PM_DT)
    for i, (t, b, k) in enumerate(rows):
        a[i] = (int((E + t) * 1e6), 0, 0, 15, E, int(b * 1e4), int(k * 1e4), 100, 100)
    return a


def lt(rows):
    a = np.zeros(len(rows), le.LT_DT)
    for i, (t, p, s, buy) in enumerate(rows):
        a[i] = (int((E + t + 400) * 1e6), int((E + t) * 1e3), 0, 15, E, int(p * 1e4), s, buy, 11, 22, i + 1)  # received late
    return a


class Rec(unittest.TestCase):
    def state(self):
        S = lr.State(lr.Out(Path("/tmp")))
        S.mk[9195] = dict(a=0, tf=15, e=E, end=E + 900, book=({}, {}), last=None, x=0)
        return S

    def test_parse_event(self):
        ev = {"end_at": (E + 900) * 1000, "order_books": [{"symbol": "ETH-UPDOWN-15M-%d" % E, "market_id": 9196, "status": "active",
                                                           "start_timestamp": 1, "end_timestamp": 2}]}
        self.assertEqual(lr.parse_event(ev), (1, 15, E, 9196, 1, 2, "active", E + 900))
        self.assertIsNone(lr.parse_event({"order_books": []}))

    def test_book_trades_outcome_candles(self):
        S = self.state()
        S.handle(json.dumps({"type": "subscribed/order_book", "channel": "order_book:9195", "timestamp": 5,
                             "order_book": {"bids": [{"price": "0.39", "size": "30.0000"}, {"price": "0.03", "size": "7"}, {"price": "0.02", "size": "1000"}],
                                            "asks": [{"price": "0.41", "size": "30"}, {"price": "0.48", "size": "200"}]}}), 100)
        S.handle(json.dumps({"type": "update/order_book", "channel": "order_book:9195", "timestamp": 6,
                             "order_book": {"bids": [], "asks": [{"price": "0.41", "size": "0"}]}}), 200)
        S.handle(json.dumps({"type": "update/order_book", "channel": "order_book:9195", "timestamp": 7,
                             "order_book": {"bids": [{"price": "0.01", "size": "5"}], "asks": []}}), 300)  # top 3 unchanged
        rows = np.frombuffer(b"".join(S.out.buf["lb"]), le.LB_DT)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["b1"], rows[0]["k1"], rows[0]["k2"], rows[0]["b1s"]), (3900, 4100, 4800, 30.0))
        self.assertEqual((rows[1]["k1"], rows[1]["k1s"], rows[1]["x"], rows[1]["l"]), (4800, 200.0, 6, 200))
        tr = {"type": "update/trade", "channel": "trade:9195", "trades": [
            {"trade_id": 7, "price": "0.41", "size": "12.5", "is_maker_ask": True, "ask_account_id": 763239, "bid_account_id": 55, "timestamp": 9}]}
        S.handle(json.dumps(tr), 400)
        S.handle(json.dumps(tr), 500)  # duplicate
        t = np.frombuffer(b"".join(S.out.buf["lt"]), le.LT_DT)
        self.assertEqual((len(t), t[0]["p"], t[0]["buy"], t[0]["aa"], float(t[0]["s"])), (1, 4100, 1, 763239, 12.5))
        S.handle(json.dumps({"type": "update/up_down_candle", "candles": [{"symbol": "btc", "timestamp": 10, "close": "104000.5"}]}), 600)
        self.assertEqual(np.frombuffer(b"".join(S.out.buf["lc"]), le.LC_DT)[0]["c"], 104000.5)
        S.unknown()
        self.assertEqual(np.frombuffer(b"".join(S.out.buf["lb"]), le.LB_DT)[-1]["k1"], 0)
        root = Path(tempfile.mkdtemp())
        try:
            S.out.root = root
            S.watch[9195] = 0
            S.handle(json.dumps({"type": "update/bo_market_stats", "bo_market_stats": {"9195": {"market_id": 9195, "status": "settled", "outcome": 1},
                                                                                           "1": {"market_id": 1, "outcome": 2}}}), 700)
            self.assertEqual((root / "lighter_outcomes.csv").read_text().split(",")[:3], ["9195", "settled", "1"])
            self.assertNotIn(9195, S.watch)
        finally:
            shutil.rmtree(root)

    def test_stream_subscribes_and_reconnects(self):
        sent = []

        class FakeWS:
            def __init__(self):
                self.msgs = [json.dumps({"type": "subscribed/order_book", "channel": "order_book:9195", "timestamp": 1,
                                         "order_book": {"bids": [{"price": "0.45", "size": "9"}], "asks": [{"price": "0.55", "size": "9"}]}})]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, m):
                sent.append(m)

            async def recv(self):
                if self.msgs:
                    return self.msgs.pop()
                await asyncio.sleep(0.05)
                raise ConnectionError("dropped")

        lr.websockets = types.SimpleNamespace(connect=lambda *a, **k: FakeWS())
        S = self.state()
        import time

        asyncio.run(lr.stream(S, lr.TAGS, stop_at=time.time() + 1.3))
        self.assertIn('{"type": "subscribe", "channel": "order_book/9195"}', sent)
        self.assertIn('{"type": "subscribe", "channel": "up_down_candle/btc/15m/1s"}', sent)
        rows = np.frombuffer(b"".join(S.out.buf["lb"]), le.LB_DT)
        self.assertEqual((rows[0]["b1"], rows[0]["k1"]), (4500, 5500))
        self.assertEqual(rows[1]["k1"], 0)  # drop marks the book unknown
        self.assertIn("dropped", S.err)


class Eval(unittest.TestCase):
    def test_arb_and_snipe(self):
        # Polymarket 0.49/0.51 all window. Lighter 0.30/0.44 from t=10 s to 20 s (stale ask), else 0.42/0.58.
        L = lb([(0, 0.42, 0.58, 50, 50), (10, 0.30, 0.44, 50, 50), (20, 0.42, 0.58, 50, 50)])
        r = le.market_metrics(L, lt([]), pm([(0, 0.49, 0.51)]), 1.0, E)
        edge = 1 - (0.44 + 0.51 + le.fl(0.44) + le.fp(0.51))
        self.assertEqual((r["arb_trig"], r["arb_fill"]), (1, 1))
        self.assertAlmostEqual(r["arb_usd"], edge * 50, places=6)
        self.assertEqual(r["arb_time"], 40)
        sn = 0.50 - 0.44 - le.fl(0.44)  # 5.75c: passes 2c and 4c, not 6c
        self.assertEqual((r["sn0.02_fill"], r["sn0.04_fill"], r["sn0.06_fill"]), (1, 1, 0))
        self.assertAlmostEqual(r["sn0.04_model"], sn * 50, places=6)
        self.assertAlmostEqual(r["sn0.04_settle"], (1 - 0.44 - le.fl(0.44)) * 50, places=6)
        self.assertAlmostEqual(float(np.median(r["spreads"])), 0.16, places=6)
        # sequential: Lighter Up at 0.44 (500 ms), then Polymarket Down at 0.51 -> same locked pair, once (5 s pause, then again)
        self.assertEqual((r["seq0.0_trig"], r["seq0.0_fill"], r["seq0.0_pos"]), (2, 2, 2))
        self.assertAlmostEqual(r["seq0.0_usd"], edge * 50 * 2, places=6)
        self.assertEqual(r["seq0.04_trig"], 0)  # the pair is only 2.5c under $1
        self.assertEqual((r["seq0.02_fill"], r["seq0.02_raced"], r["seq0.02_late_usd"], r["seq0.02_small_usd"]), (2, 0, 0, 0))
        self.assertAlmostEqual(r["seq0.02_8_8_usd"], edge * 50 * 2, places=6)  # 2 s + 2 s still gets both round trips
        r3 = le.market_metrics(L, lt([(10.4, 0.44, 5, 1)]), pm([(0, 0.49, 0.51)]), 1.0, E)
        self.assertEqual(r3["seq0.02_raced"], 1)  # a real taker bought the same quote within a second
        self.assertAlmostEqual(r["sn0.04_mo30"], (0.50 - 0.44 - le.fl(0.44)) * 50, places=6)
        # a 300 ms blip is gone before the order matches
        L2 = lb([(0, 0.42, 0.58, 50, 50), (10, 0.30, 0.44, 50, 50), (10.3, 0.42, 0.58, 50, 50)])
        r2 = le.market_metrics(L2, lt([]), pm([(0, 0.49, 0.51)]), 1.0, E)
        self.assertEqual((r2["arb_trig"], r2["arb_fill"]), (1, 0))
        self.assertEqual((r2["seq0.0_trig"], r2["seq0.0_fill"]), (1, 0))

    def test_sequential_hedge_runs_away(self):
        # Lighter ask 0.44 looks cheap against Polymarket 0.49/0.51, but Polymarket is the stale side:
        # 0.75 s later it reprices to 0.41/0.43, so the Down hedge costs 0.59 and the "arb" loses.
        L = lb([(0, 0.42, 0.58, 50, 50), (10, 0.30, 0.44, 50, 50), (20, 0.42, 0.58, 50, 50)])
        P = pm([(0, 0.49, 0.51), (10.75, 0.41, 0.43)])
        r = le.market_metrics(L, lt([]), P, 0.0, E)
        self.assertEqual((r["seq0.0_fill"], r["seq0.0_pos"]), (1, 0))
        self.assertAlmostEqual(r["seq0.0_usd"], (1 - 0.44 - le.fl(0.44) - 0.59 - le.fp(0.59)) * 50, places=6)
        self.assertAlmostEqual(r["seq0.02_1_1_usd"], (1 - 0.44 - le.fl(0.44) - 0.51 - le.fp(0.51)) * 50, places=6)  # 250+250 ms beats the reprice
        self.assertGreater(r["arb_usd"], 0)  # the simultaneous version still books a profit on the first 500 ms... 

    def test_lead_lag_sign(self):
        r_ = np.random.default_rng(0)
        steps = np.cumsum(r_.choice([-0.01, 0.0, 0.01], 3400))
        tt = 20 + np.arange(3400) * 0.25
        mid = np.clip(0.5 + steps, 0.2, 0.8).round(2)
        L = lb([(float(a), float(m) - 0.02, float(m) + 0.02, 50, 50) for a, m in zip(tt, mid)])
        P = pm([(float(a) + 0.5, float(m) - 0.01, float(m) + 0.01) for a, m in zip(tt, mid)])  # Polymarket 0.5 s behind
        r = le.market_metrics(L, lt([]), P, 1.0, E)
        self.assertEqual(max(le.LAGS, key=lambda k: r[f"ll{k}"]), 2)

    def test_trades_and_tighter_maker(self):
        L = lb([(0, 0.42, 0.58, 50, 50)])
        P = pm([(0, 0.49, 0.51)])
        T = lt([(30, 0.58, 20, 1), (40, 0.42, 10, 0), (50, 0.51, 10, 1)])  # buy at 0.58, sell at 0.42, buy at 0.51
        r = le.market_metrics(L, T, P, 1.0, E)
        t0, t1, _ = r["trades"]
        self.assertEqual((t0["maker"], t0["taker"], t1["maker"], t1["taker"]), (11, 22, 22, 11))
        self.assertAlmostEqual(t0["mk_mid"], (0.58 - 0.50) * 20)
        self.assertAlmostEqual(t0["mk_settle"], (0.58 - 1.0) * 20)
        self.assertAlmostEqual(t1["mk_settle"], (1.0 - 0.42) * 10)
        # h = 2c: ask 0.52 < 0.58 fills 20; bid 0.48 > 0.42 fills 10; the buy at 0.51 is not improved (0.52 > 0.51)
        self.assertEqual((r["mm0.02_n"], r["mm0.02_sh"]), (2, 30))
        hedged = (0.52 - 0.51 - le.fp(0.51)) * 20 + (0.49 - 0.48 - le.fp(0.51)) * 10
        self.assertAlmostEqual(r["mm0.02_hedged"], hedged, places=6)
        self.assertAlmostEqual(r["mm0.02_settle"], (0.52 - 1) * 20 + (1 - 0.48) * 10, places=6)
        self.assertEqual(r["mm0.01_n"], 2)  # ask 0.51 is not strictly better than 0.51
        self.assertEqual(r["mm0.05_n"], 2)

    def test_report_runs(self):
        root = Path(tempfile.mkdtemp())
        try:
            d = root / "lt" / "2026-11-11"
            d.mkdir(parents=True)
            late = lb([(2000, 0.4, 0.6, 9, 9)])
            np.concatenate([lb([(0, 0.42, 0.58, 50, 50), (10, 0.30, 0.44, 50, 50), (20, 0.42, 0.58, 50, 50)]), late]).tofile(d / "lb-10.bin")
            lt([(30, 0.58, 20, 1), (40, 0.42, 10, 0)]).tofile(d / "lt-10.bin")
            p = root / "pm" / "2026-11-11"
            p.mkdir(parents=True)
            pm([(0, 0.49, 0.51)]).tofile(p / "pm-10.bin")
            (root / "lt" / "lighter_markets.csv").write_text(f"btc,15,{E},9195,0,0\n")
            (root / "lt" / "lighter_outcomes.csv").write_text("9195,settled,1,0\n")
            with (root / "lt" / "pm_winners.csv").open("w", newline="") as f:
                csv.writer(f).writerow(("btc", E, 1.0))
            buf = io.StringIO()
            sys.argv = ["x", "report", "--lt", str(root / "lt"), "--pm", str(root / "pm"), "--no-gamma"]
            with redirect_stdout(buf):
                le.main()
            out = buf.getvalue()
            self.assertIn("BTC 15m: 1 windows", out)
            self.assertIn("S6 maker at Polymarket mid +- 2c: ", out)
            self.assertIn("S4 sequential, start at > 0c: ", out)
            self.assertIn("LL correlation", out)
            buf = io.StringIO()
            sys.argv += ["--since", "2026-11-12"]
            with redirect_stdout(buf):
                le.main()
            self.assertIn("markets: 0 finished", buf.getvalue().replace("only windows starting at or after 2026-11-12 UTC\n", ""))
            self.assertIn("top makers", out)
            self.assertIn("agreement 1.0000 over 1 windows", out)
            self.assertIn("to settlement -249.60", out)  # makers: ((0.58-1)*20 + (1-0.42)*10) x 96 windows a day
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
