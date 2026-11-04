import unittest
import same_token_walkforward as wf

C = (.70, .30, .70, 60)

def ev(t, bid, ask):
    return t, {'UP': (bid, ask, 100, 100), 'DOWN': (.1, .2, 100, 100)}

class TestWalkforward(unittest.TestCase):
    def test_roundtrip(self):
        r = wf.replay([ev(0,.6,.62),ev(1,.7,.72),ev(2,.27,.29),ev(3,.71,.73)], [C], gap=301)[C]
        self.assertEqual(r['status'], 'exited')
        self.assertAlmostEqual(r['lo'], .4)

    def test_unknown_settlement(self):
        r = wf.replay([ev(0,.6,.62),ev(1,.7,.72),ev(2,.27,.29)], [C], gap=301)[C]
        self.assertAlmostEqual(r['lo'], -.3)
        self.assertAlmostEqual(r['hi'], .7)

    def test_settlement(self):
        r = wf.replay([ev(0,.6,.62),ev(1,.7,.72),ev(2,.27,.29)], [C], gap=301, winner='UP')[C]
        self.assertEqual(r['status'], 'settled')
        self.assertAlmostEqual(r['lo'], .7)

    def test_gap_across_expiry(self):
        r = wf.replay([ev(0,.6,.62),ev(1,.7,.72),ev(100,.27,.29)], [C])[C]
        self.assertEqual(r['status'], 'censored')
        self.assertLess(r['lo'], 0)

    def test_tail_gap(self):
        self.assertEqual(wf.replay([ev(0,.6,.62),ev(1,.7,.72)], [C])[C]['status'], 'censored')

    def test_strict_through(self):
        r = wf.replay([ev(0,.6,.62),ev(1,.7,.72),ev(2,.28,.30)], [C], gap=301)[C]
        self.assertIsNone(r['entry'])

    def test_first_observation_not_crossing(self):
        r = wf.replay([ev(0,.7,.72),ev(1,.27,.29)], [C], gap=301)[C]
        self.assertEqual(r['status'], 'waiting')

    def test_cash(self):
        h = {d:{C:[{'lo':-1, 'entry':1}]} for d in range(4)}
        self.assertIsNone(wf.choose(h,[C],[0,1],[2,3],1,1))

    def test_future_not_used(self):
        h = {d:{C:[{'lo':1, 'entry':1}]} for d in range(4)}
        h[4] = {C:[{'lo':-10000, 'entry':1}]}
        self.assertEqual(wf.choose(h,[C],[0,1],[2,3],1,1), C)

if __name__ == '__main__':
    unittest.main()
