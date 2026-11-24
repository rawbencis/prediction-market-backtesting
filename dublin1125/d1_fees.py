"""D1 (Dublin 11-25): taker fee rate per asset x timeframe x window from the RTDS census.

fee_rate(row) = fee / (size * p * (1 - p)) on taker rows (fee > 0; maker rows carry fee = -1).
Per window (slug epoch = window start, UTC): median over its taker rows.

Wallet 0xc387c2a4 rows on/after 2026-11-24 are dropped before anything is computed (its watch is frozen).

  python dublin1125/d1_fees.py --rec ~/shadow-rec --start 2026-11-11 --out reports/dublin/fees_by_window.csv \
      --rows-out out/d1125/taker_rows.npz

Writes one CSV row per (asset, tf, window) from --start to the last window that ended before the census stopped;
windows with no taker rows get source=none, fee_rate empty.
"""

import argparse
import calendar
import csv
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path.home() / "lighter-flips"))
from act_rec import ACT_DT, ACT_REC, ASSETS  # noqa: E402

FROZEN_W = bytes.fromhex("c387c2a4")
FROZEN_FROM = "2026-11-24"
COLS = ("x", "ts", "a", "tf", "e", "p", "sz", "fee")


def load_day(root, d):
    parts = []
    for f in sorted((root / d).glob("act-*.bin")):
        b = f.read_bytes()
        b = b[: len(b) - len(b) % ACT_REC.size]
        x = np.frombuffer(b, dtype=np.dtype(ACT_DT))
        x = x[x["fee"] > 0]  # taker rows only
        if d >= FROZEN_FROM:
            x = x[np.array([not w.startswith(FROZEN_W) for w in x["w"]], dtype=bool)] if len(x) else x
        parts.append({k: np.ascontiguousarray(x[k]) for k in COLS})
    if not parts:
        return None
    return {k: np.concatenate([p[k] for p in parts]) for k in COLS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    ap.add_argument("--start", default="2026-11-11")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rows-out", type=Path)
    a = ap.parse_args()

    days = sorted(p.name for p in a.rec.iterdir() if p.is_dir() and p.name >= a.start and list(p.glob("act-*.bin")))
    parts = [x for d in days if (x := load_day(a.rec, d)) is not None]
    r = {k: np.concatenate([p[k] for p in parts]) for k in COLS}
    del parts
    q = r["p"].astype(np.float64)
    rate = r["fee"].astype(np.float64) / (r["sz"].astype(np.float64) * q * (1 - q))
    ok = (q > 0.005) & (q < 0.995) & np.isfinite(rate)
    r = {k: v[ok] for k, v in r.items()}
    rate = rate[ok]
    if a.rows_out:
        a.rows_out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(a.rows_out, x=r["x"], ts=r["ts"], a=r["a"], tf=r["tf"], e=r["e"], p=r["p"],
                            sz=r["sz"], fee=r["fee"], rate=rate)  # fmt: skip

    # last census row (exchange ms) -> latest settled window = end <= that time
    last_ms = int(r["x"].max())
    last_s = last_ms // 1000
    t0 = calendar.timegm(time.strptime(a.start, "%Y-%m-%d"))
    key = (r["a"].astype(np.int64) * 100 + r["tf"].astype(np.int64)) * 10**10 + r["e"].astype(np.int64)
    order = np.argsort(key, kind="stable")
    key, rate_s = key[order], rate[order]
    uk, idx, cnt = np.unique(key, return_index=True, return_counts=True)
    med = {int(k): (float(np.median(rate_s[i : i + c])), int(c)) for k, i, c in zip(uk, idx, cnt)}

    a.out.parent.mkdir(parents=True, exist_ok=True)
    n_rows = n_none = 0
    with a.out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["asset", "tf", "window_start_utc", "fee_rate", "source", "n_taker_rows"])
        for ai, asset in enumerate(ASSETS):
            for tf in (5, 15):
                e = t0
                while e + tf * 60 <= last_s:
                    k = (ai * 100 + tf) * 10**10 + e
                    ws = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))
                    if k in med:
                        m, c = med[k]
                        w.writerow([asset, f"{tf}m", ws, f"{m:.4f}", "census", c])
                    else:
                        w.writerow([asset, f"{tf}m", ws, "", "none", 0])
                        n_none += 1
                    n_rows += 1
                    e += tf * 60
    print(f"days={days[0]}..{days[-1]} taker_rows={len(rate)} windows={n_rows} none={n_none} "
          f"last_row={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(last_s))}")  # fmt: skip


if __name__ == "__main__":
    main()
