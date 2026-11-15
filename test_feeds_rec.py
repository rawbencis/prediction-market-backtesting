import unittest
from feeds_rec import apply_msg, slug_1h, top


class T(unittest.TestCase):
    def test_depth_rebuild(self):
        books = {"U": ({}, {}), "D": ({}, {})}
        snap = {"event_type": "book", "asset_id": "U", "bids": [{"price": f"0.{40+i}", "size": "10"} for i in range(7)],
                "asks": [{"price": "0.50", "size": "5"}, {"price": "0.51", "size": "0"}, {"price": "0.52", "size": "7"}]}
        self.assertEqual(apply_msg(books, snap), {"U"})
        b, a = top(books["U"])
        self.assertEqual([p for p, _ in b], [0.46, 0.45, 0.44, 0.43, 0.42])
        self.assertEqual(a, [[0.50, 5.0], [0.52, 7.0]])
        ch = {"event_type": "price_change", "price_changes": [
            {"asset_id": "U", "price": "0.46", "side": "BUY", "size": "0"},
            {"asset_id": "U", "price": "0.47", "side": "BUY", "size": "3"},
            {"asset_id": "U", "price": "0.50", "side": "SELL", "size": "2"},
            {"asset_id": "X", "price": "0.10", "side": "BUY", "size": "1"}]}
        self.assertEqual(apply_msg(books, ch), {"U"})
        b, a = top(books["U"])
        self.assertEqual(b[0], [0.47, 3.0])
        self.assertNotIn(0.46, [p for p, _ in b])
        self.assertEqual(len(b), 5)
        self.assertEqual(a[0], [0.50, 2.0])
        self.assertEqual(top(books["D"]), ([], []))
        # new snapshot replaces the book entirely
        apply_msg(books, {"event_type": "book", "asset_id": "U", "bids": [], "asks": [{"price": "0.6", "size": "1"}]})
        self.assertEqual(top(books["U"]), ([], [[0.6, 1.0]]))


    def test_slug_1h(self):
        self.assertEqual(slug_1h("bitcoin", 1794733200), "bitcoin-up-or-down-november-15-2026-4am-et")  # 09:00 UTC
        self.assertEqual(slug_1h("ethereum", 1794762000), "ethereum-up-or-down-november-15-2026-12pm-et")  # 17:00 UTC
        self.assertEqual(slug_1h("bitcoin", 1794718800), "bitcoin-up-or-down-november-15-2026-12am-et")  # 05:00 UTC


if __name__ == "__main__":
    unittest.main()
