import unittest

import numpy as np

import pair_gap_scan as pg

DT = 100_000  # 100 ms


def book(bu, au, n=None, sz=50.0):
    """UP bid/ask path (milli) -> mirrored two-token market dict."""
    bu, au = np.asarray(bu, np.int32), np.asarray(au, np.int32)
    n = len(bu)
    t = np.arange(n, dtype=np.int64) * DT
    bid = np.stack([bu, 1000 - au], 1).astype(np.int32)
    ask = np.stack([au, 1000 - bu], 1).astype(np.int32)
    return {
        "t": t,
        "bid": bid,
        "ask": ask,
        "bsz": np.full((n, 2), sz, np.float32),
        "asz": np.full((n, 2), sz, np.float32),
        "upd": np.ones((n, 2), bool),
        "seen": np.stack([t, t], 1),
    }


def flat(n=3000, b=490, a=510):
    return book(np.full(n, b), np.full(n, a))


def ci(c, ttl, imp, f):
    return pg.CFG.index((c, ttl, imp, f))


class T(unittest.TestCase):
    def test_mirrored_random_walk_no_arb(self):
        r = np.random.default_rng(1)
        mid = np.clip(500 + np.cumsum(r.integers(-1, 2, 3000)) * 10, 100, 900)
        row, H, eps, mk = pg.scan(book(mid - 10, mid + 10), 0)
        self.assertEqual(row["raw_eps"], 0)
        self.assertAlmostEqual(row["mirror"], 1.0)
        self.assertAlmostEqual(row["gbar"], 20.0)
        self.assertAlmostEqual(H[1].sum() / H[0].sum(), 20.0)

    def test_desync_episode(self):
        m = flat()
        # DOWN leg lags: ask_D 60 milli too cheap for 5 events (500 ms)
        m["ask"][1000:1005, 1] -= 60
        m["bid"][1000:1005, 1] -= 60
        row, H, eps, mk = pg.scan(m, 0)
        self.assertEqual(row["raw_eps"], 1)
        e = eps[0]
        self.assertEqual(e[0], 1000 * DT)
        self.assertEqual(e[1], 500_000)
        self.assertEqual(e[2], 40)  # 1000 - (510 + 450)
        net = 40 - pg.fee(510) - pg.fee(450)
        self.assertAlmostEqual(e[3], net)
        self.assertAlmostEqual(e[4], net)  # still there at +250 ms
        self.assertEqual(row["exec_eps"], 1)
        self.assertLess(row["mirror"], 1.0)

    def test_short_desync_not_executable(self):
        m = flat()
        m["ask"][1000:1002, 1] -= 60  # 200 ms < latency
        m["bid"][1000:1002, 1] -= 60
        row, *_ = pg.scan(m, 0)
        self.assertEqual(row["raw_eps"], 1)
        self.assertEqual(row["exec_eps"], 0)

    def test_maker_both_legs(self):
        bu, au = np.full(3000, 490), np.full(3000, 510)
        bu[60:80], au[60:80] = 470, 480  # 6.0s: UP sells through 490 -> UP bid filled
        bu[80:100], au[80:100] = 520, 530  # 8.0s: DOWN ask 480 < 490 -> DOWN bid filled
        row, H, eps, mk = pg.scan(book(bu, au), 0)
        r = mk[ci(5, 10, 0, 0)]
        self.assertEqual(r[0], 1)
        self.assertEqual(r[2], 20)
        self.assertEqual(r[3], 20)

    def test_maker_one_leg_adverse(self):
        bu, au = np.full(3000, 490), np.full(3000, 510)
        bu[60:], au[60:] = 460, 470  # UP collapses, DOWN never comes back
        row, H, eps, mk = pg.scan(book(bu, au), 1)  # DOWN wins
        r = mk[ci(5, 10, 0, 0)]
        self.assertEqual(r[0], 2)  # UP leg only
        self.assertEqual(r[2], -490)
        self.assertAlmostEqual(r[3], 1000 - 490 - 540 - pg.fee(540), places=4)

    def test_maker_touch_vs_through(self):
        bu, au = np.full(3000, 490), np.full(3000, 510)
        bu[60:], au[60:] = 480, 490  # ask touches our 490 bid, never through
        _, _, _, mk = pg.scan(book(bu, au), 0)
        self.assertEqual(mk[ci(5, 10, 0, 0), 0], 0)
        self.assertEqual(mk[ci(5, 10, 0, 1), 0], 2)

    def test_maker_improve_and_skip(self):
        _, _, _, mk = pg.scan(flat(b=490, a=510), 0)
        self.assertEqual(mk[ci(5, 10, 10, 0), 0], -2)  # 500+500 leaves no edge
        _, _, _, mk = pg.scan(flat(b=480, a=520), 0)
        self.assertEqual(mk[ci(5, 10, 10, 0), 0], 0)  # 490+490 posted, no fills

    def test_flips(self):
        mid = np.r_[np.full(500, 750), np.full(500, 250), np.full(500, 750), np.full(500, 600)]
        row, *_ = pg.scan(book(mid - 10, mid + 10), 0)
        self.assertEqual(row["flips70"], 2)
        self.assertEqual(row["last70"], 1)


if __name__ == "__main__":
    unittest.main()
