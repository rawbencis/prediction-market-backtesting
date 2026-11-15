import unittest
import atlas
import dsim as d
from test_dsim import mkt


class T(unittest.TestCase):
    def test_land_edges_stale_vs_repriced(self):
        m = mkt([(-5000, 4900, 5100, 100, 100), (150, 7900, 8100, 100, 100)])
        r = atlas.land_edges(m, 0, 0, "UP")
        f51, f81 = 100 * d.fee(5100, 1), 100 * d.fee(8100, 1)
        for L in (100, 130):
            self.assertEqual(r[f"hit_{L}"], 1.0)
            self.assertAlmostEqual(r[f"edge_{L}"], 80 - 51 - f51)
            self.assertAlmostEqual(r[f"settle_{L}"], 100 - 51 - f51)
        for L in (165, 200, 300):
            self.assertAlmostEqual(r[f"edge_{L}"], 80 - 81 - f81)
        r = atlas.land_edges(mkt([(-5000, 4900, 5100, 100, 3)]), 0, 0, None)  # < 5 shown: not hittable
        self.assertEqual(r["hit_100"], 0.0)
        self.assertTrue(r["settle_100"] != r["settle_100"])

    def test_alt_led_trigger(self):
        flat = [(-60_000, 4900, 5100, 100, 100)]
        up_at = lambda t: flat + [(t, 5000, 5200, 100, 100)]  # noqa: E731
        mk = {("btc", 5, 0): mkt(flat + [(5000, 5000, 5200, 100, 100)]), ("eth", 5, 0): mkt(up_at(1000)),
              ("sol", 5, 0): mkt(up_at(1100)), ("xrp", 5, 0): mkt(up_at(9000))}
        A = atlas.alt_led(mk, {"btc-updown-5m-0": "UP"})
        self.assertEqual(list(A.t0), [1100])  # eth + sol within 300 ms; xrp alone later; BTC moved only at 5000
        self.assertEqual((A.sgn[0], A.bucket[0], A.tl[0]), (1, "2 alts", "120-300s"))
        mk2 = {k: mkt(up_at(-2000)) for k in (("eth", 5, 0), ("sol", 5, 0))}
        mk2[("btc", 5, 0)] = mkt(flat)
        self.assertEqual(len(atlas.alt_led(mk2, {})), 0)  # trigger before the window opens
        mk[("btc", 5, 0)] = mkt(flat + [(1050, 5000, 5200, 100, 100)])  # BTC already moved inside the span
        self.assertEqual(len(atlas.alt_led(mk, {})), 0)


if __name__ == "__main__":
    unittest.main()
