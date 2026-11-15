import unittest
import numpy as np
import final_minute as fm


class T(unittest.TestCase):
    def test_fair_value(self):
        e, W = 3600, 300
        T = 1000 * np.arange(0, e + W + 1, dtype=np.int64)
        alt = np.where(np.arange(len(T)) % 2 == 0, 0.5, -0.5)  # sigma ~ 1 $/sqrt(s)
        P = 100.0 + alt
        self.assertAlmostEqual(fm.fair_up(T, P, e, W, 30), 0.5, delta=0.1)  # at the open: near a coin flip
        P2 = P + np.where(T >= (e + W - 60) * 1000, 5.0, 0.0)  # +5 $ through the whole final minute
        self.assertGreater(fm.fair_up(T, P2, e, W, 30), 0.99)
        # +5 $ for most of the final minute, then 3 $ BELOW the open for the last 10 s: spot says Down, the average Up
        end = (e + W) * 1000
        P3 = P + np.where((T >= end - 60_000) & (T < end - 10_000), 5.0, 0.0) + np.where(T >= end - 10_000, -3.0, 0.0)
        self.assertGreater(fm.fair_up(T, P3, e, W, 5), 0.9)

    def test_buckets(self):
        self.assertEqual(fm.bucket(9499, fm.ASK_B), "0.90-0.95")
        self.assertEqual(fm.bucket(5, fm.S_B), "1-5s")
        self.assertEqual(fm.bucket(60, fm.S_B), "31-60s")


if __name__ == "__main__":
    unittest.main()
