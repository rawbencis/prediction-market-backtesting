import contextlib
import io
import tempfile
import time
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import latency_watch as lw
import ohioai_match as om
import shadow_eval as se
import shadow_rec as sr


class T(unittest.TestCase):
    def test_dtypes_match(self):
        self.assertEqual(lw.PM_DT, se.PM_DT)
        self.assertEqual(lw.BN_DT, se.BN_DT)
        self.assertEqual(lw.TR_DT, om.TR_DT)

    def test_sample_and_report(self):
        with tempfile.TemporaryDirectory() as td:
            rec, log = Path(td) / "rec", Path(td) / "lat.csv"
            now = datetime.now(timezone.utc)
            d = rec / now.strftime("%Y-%m-%d")
            d.mkdir(parents=True)
            l0 = int(time.time() * 1e6)
            with (d / f"pm-{now.hour:02d}.bin").open("wb") as f:
                for i in range(1000):  # 99 rows 120 ms late, every 100th row 3 s late
                    lat = 3000 if i % 100 == 0 else 120
                    l = l0 - i * 100_000
                    f.write(sr.PM_REC.pack(l, l // 1000 - lat, 0, 5, 1, 5000, 5100, 10.0, 10.0))
            with (d / f"bn-{now.hour:02d}.bin").open("wb") as f:
                f.write(sr.BN_REC.pack(l0, l0 // 1000 - 4, 0, 0, 1.0))
            lw.cmd_sample(Namespace(rec=rec, log=log, window=15))
            lw.cmd_sample(Namespace(rec=rec, log=log, window=15))
            import pandas as pd

            D = pd.read_csv(log)
            self.assertEqual(len(D), 2)
            self.assertEqual(D.pm_n[0], 1000)
            self.assertAlmostEqual(D.pm_gt1s_pct[0], 1.0)
            self.assertAlmostEqual(D.pm_p50[0], 120.0, delta=1)
            self.assertEqual(D.tr_n[0], 0)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                lw.cmd_report(Namespace(log=log, hours=48))
            self.assertIn("COLLECTING", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
