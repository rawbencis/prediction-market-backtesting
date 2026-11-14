import unittest
import numpy as np
import settle_check as s


class T(unittest.TestCase):
    def test_outcome_tie_is_up(self):
        self.assertEqual(s.outcome(100.0, 100.0), "UP")
        self.assertEqual(s.outcome(100.0, 99.999), "DOWN")

    def test_value_at_uses_last_update_not_after_boundary(self):
        ser = (np.array([1000, 2000, 3000]), np.array([1.0, 2.0, 3.0]))
        self.assertEqual(s.value_at(ser, 2999), 2.0)
        self.assertEqual(s.value_at(ser, 3000), 3.0)
        self.assertIsNone(s.value_at(ser, 9000))  # stale beyond tolerance
        self.assertIsNone(s.value_at(ser, 500))


if __name__ == "__main__":
    unittest.main()
