import unittest
import numpy as np
import dsim as d

BK = np.dtype([("x", "<i8"), ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
PR = np.dtype([("x", "<i8"), ("tok", "u1"), ("sd", "u1"), ("p", "<u2"), ("sz", "<f4"), ("tx", "S32")])


def mkt(book, prints=(), shift=0):
    return d.mk_market(np.array(book, BK), np.array(list(prints), PR), shift)


def filled(o):
    return sum(f["size"] for f in o["fills"])


class Taker(unittest.TestCase):
    def test_flat_book_no_fills(self):
        m = mkt([(0, 4900, 5100, 100, 100), (10_000, 4900, 5100, 100, 100)])
        for t in range(1, 20_000, 997):
            self.assertIsNone(d.taker(m, t, 0, 5000))  # limit below ask
            self.assertIsNone(d.taker(m, t, 1, 5000))  # DOWN ask = 1 - 0.49 = 0.51 > 0.50
        self.assertEqual(filled(d.maker(m, 100, 0, 4800, 5, 19_000, "front")), 0)

    def test_planted_lag(self):
        # true value jumps to 0.80 at t=0, book stays stale at 0.49/0.51 for 300 ms
        m = mkt([(-5000, 4900, 5100, 100, 100), (300, 7900, 8100, 100, 100)])
        sc = d.scenario("colo")
        pnl = {}
        for lat in (100, 1000):
            f = d.taker(m, d.taker_match_time(lat - sc["order"] - sc["hold"], sc), 0, 9900)
            pnl[lat] = d.markouts(m, f)["mo30000"] - f["fee"]
        self.assertGreater(pnl[100], 1.0)
        self.assertLess(pnl[1000], 0)

    def test_hold_and_strict_time(self):
        m = mkt([(0, 4900, 5100, 100, 100), (100, 7900, 8100, 100, 100)])
        sc = dict(order=40, hold=50)
        self.assertEqual(d.taker(m, d.taker_match_time(9, sc), 0, 9900)["px"], 5100)  # matches at 99
        self.assertEqual(d.taker(m, d.taker_match_time(10, sc), 0, 9900)["px"], 8100)  # at 100: the new book (ties -> others first)

    def test_same_ms_other_takers_first(self):
        m = mkt([(0, 4900, 5100, 100, 8)], [(500, 0, 0, 5100, 6, b"a")])
        self.assertIsNone(d.taker(m, 500, 0, 5100))  # 8 shown - 6 taken by another taker at the same ms < 5
        self.assertIsNotNone(d.taker(m, 500, 0, 5100, exclude_tx=b"a".ljust(32, b"\0")))
        self.assertIsNone(d.taker(mkt([(0, 0, 0, 0, 0)]), 10, 0, 9900))  # dropped book


    def test_taker_sell(self):
        m = mkt([(0, 4900, 5100, 100, 3)])
        f = d.taker(m, 10, 0, 4800, sell=True)  # SELL UP hits the UP bid 0.49
        self.assertEqual((f["px"], f["size"]), (4900, 5))
        self.assertIsNone(d.taker(m, 10, 0, 4950, sell=True))  # bid below limit
        self.assertIsNone(d.taker(m, 10, 1, 4800, sell=True))  # SELL DOWN: DOWN bid 0.49 but only 3 shown
        f = d.taker(m, 10, 1, 4800, size=3, min_show=3, sell=True)
        self.assertEqual((f["px"], f["size"]), (4900, 3))


class Maker(unittest.TestCase):
    def stale(self):
        # bid 0.50 x 20 shown; spot drops at t=1000; takers sell 50 into the stale bid at 1010..1100, bid then 0.30
        book = [(0, 5000, 5200, 20, 50), (1101, 3000, 3200, 50, 50), (60_000, 3000, 3200, 50, 50)]
        pr = [(1010 + 10 * i, 0, 1, 5000, 5, b"") for i in range(10)]
        return mkt(book, pr)

    def test_stale_maker_loses_pess(self):
        m = self.stale()
        o = d.maker(m, 100, 0, 5000, 5, 1000 + 120, "pess")
        self.assertEqual(filled(o), 5)
        self.assertEqual(o["fills"][0]["ahead"], 0)  # queue of 20 consumed by the first 4 prints
        mo = sum(d.markouts(m, f)["mo5000"] for f in o["fills"])
        self.assertLess(mo, 0)
        self.assertLess(sum(d.settle(f, "DOWN") for f in o["fills"]), 0)

    def test_cancel_before_print(self):
        m = self.stale()
        for model in ("pess", "prop", "front"):
            self.assertEqual(filled(d.maker(m, 100, 0, 5000, 5, 1005, model)), 0)
        self.assertGreater(filled(d.maker(m, 100, 0, 5000, 5, 1010, "front")), 0)  # tie at the cancel ms fills

    def test_print_shift(self):
        book = [(0, 5000, 5200, 20, 50), (1101, 3000, 3200, 50, 50)]
        pr = [(1010 + 10 * i, 0, 1, 5000, 5, b"") for i in range(10)]
        self.assertEqual(filled(d.maker(mkt(book, pr, 0), 100, 0, 5000, 5, 1040, "front")), 5)
        self.assertEqual(filled(d.maker(mkt(book, pr, 0), 100, 0, 5000, 5, 1005, "front")), 0)
        self.assertEqual(filled(d.maker(mkt(book, pr, 20), 100, 0, 5000, 5, 1005, "front")), 5)  # print really at 990

    def test_per_print_match_time(self):
        book = [(0, 5000, 5200, 20, 50), (990, 5000, 5200, 15, 50), (1000, 5000, 5200, 15, 30), (2000, 4900, 5200, 8, 30),
                (3000, 4900, 5200, 8, 30)]
        pr = [(1010, 0, 1, 5000, 5, b"a"),   # SELL UP 5 at 0.50: bid 20 -> 15 at 990
              (2030, 0, 1, 5000, 15, b"b"),  # level 0.50 vanished at 2000
              (2500, 0, 0, 5200, 7, b"c"),   # BUY UP 7 at 0.52: ask 50 -> 30 at 1000 is 20, not ~7, and > 100 ms: fallback
              (3050, 1, 1, 4800, 4, b"d")]   # SELL DOWN 0.48 = lift UP ask 0.52: no change -> fallback
        m = mkt(book, pr, None)
        P = m["pr"]
        got = dict(zip([bytes(x) for x in P["tx"]], zip(P["x"].tolist(), P["matched"].tolist())))
        self.assertEqual(got[b"a"], (990, True))
        self.assertEqual(got[b"b"], (2000, True))
        self.assertEqual(got[b"c"], (2500 - d.PRINT_FALLBACK_MS, False))
        self.assertEqual(got[b"d"], (3050 - d.PRINT_FALLBACK_MS, False))
        self.assertTrue(np.all(np.diff(P["x"]) >= 0))

    def test_reject_cross_and_improve(self):
        m = self.stale()
        self.assertEqual(d.maker(m, 100, 0, 5200, 5, 2000)["status"], "rejected")
        o = d.maker(m, 100, 0, 5100, 5, 2000, "pess")  # improved the bid: queue 0, print through 0.51 -> 0.50 fills us
        self.assertEqual(filled(o), 5)
        self.assertEqual(o["fills"][0]["ahead"], 0)

    def test_down_token_mirror(self):
        # BUY DOWN at 0.48 rests on the UP ask at 0.52; a taker BUY UP lifting 0.52 (or SELL DOWN at 0.48) fills it
        m = mkt([(0, 5000, 5200, 20, 0.0001)], [(500, 1, 1, 4800, 10, b"")])
        o = d.maker(m, 100, 1, 4800, 5, 2000, "pess")
        self.assertEqual(filled(o), 5)

    def test_models_nested(self):
        rng = np.random.default_rng(7)
        for trial in range(300):
            t, book, pr = 0, [], []
            for _ in range(60):
                t += int(rng.integers(1, 200))
                b = int(rng.choice([4800, 4900, 5000]))
                book.append((t, b, b + 200, float(rng.integers(1, 60)), 30.0))
                if rng.random() < 0.5:
                    pr.append((t - int(rng.integers(0, 3)), 0, 1, int(rng.choice([4800, 4900, 5000])), float(rng.integers(1, 30)), b""))
            pr.sort(key=lambda r: r[0])
            m = mkt(book, pr)
            ta, tc = int(rng.integers(0, t // 2)), int(rng.integers(t // 2, t + 10))
            px = int(rng.choice([4800, 4900, 5000, 5100]))
            got = [d.maker(m, ta, 0, px, 10, tc, k) for k in ("pess", "prop", "front")]
            fs = [filled(o) for o in got]
            self.assertTrue(fs[0] <= fs[1] + 1e-9 <= fs[2] + 2e-9, (trial, fs))
            # first fill time also nested (front fills no later than pess)
            ts = [o["fills"][0]["x"] if o["fills"] else 10**12 for o in got]
            self.assertTrue(ts[2] <= ts[1] <= ts[0], (trial, ts))


class Money(unittest.TestCase):
    def test_fee_and_settle(self):
        self.assertAlmostEqual(d.fee(5000, 5), 0.07 * 0.25 * 5)
        f = dict(tok=1, px=4000, size=5, fee=d.fee(4000, 5))
        self.assertAlmostEqual(d.settle(f, "DOWN"), 5 * 0.6 - d.fee(4000, 5))
        self.assertAlmostEqual(d.settle(f, "UP"), -5 * 0.4 - d.fee(4000, 5))


if __name__ == "__main__":
    unittest.main()
