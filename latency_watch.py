"""Watch the shadow recorder's receive latency and the box's load; decide whether the recorder needs its own box.

sample: read the last WINDOW minutes of pm/tr/bn rows from the recorder's hourly files and append one line to
        CSV: Polymarket->recorder delay (p50/p90/p99, % > 1 s), trade-print delay, Binance spot delay,
        load, memory, swap, CPU steal. Run every 15 min (systemd timer).
report: hourly table for the last N hours + the frozen rule (set 2026-11-14, before any sample was read):
        NEW BOX if, over >= 48 h of samples, more than 10% of samples have pm p90 > 200 ms
        or the pooled share of pm rows > 1 s late exceeds 0.5%. Otherwise KEEP.
"""

import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])  # fmt: skip
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
TR_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("tok", "u1"), ("sd", "u1"),
                  ("p", "<u2"), ("sz", "<f4"), ("fee", "<u2"), ("tx", "S32")])  # fmt: skip
COLS = ["ts", "window_min", "pm_n", "pm_p50", "pm_p90", "pm_p99", "pm_gt1s_pct", "tr_n", "tr_p50", "tr_p90",
        "spot_n", "spot_p50", "spot_p90", "load1", "load5", "mem_avail_mb", "swap_used_mb", "steal_pct", "busy_pct"]  # fmt: skip


def load_rows(rec, kind, dt, since_us, now):
    parts = []
    for h in range(0, 3):  # current hour and up to two before (covers windows up to 60 min across midnight)
        t = now - timedelta(hours=h)
        f = rec / t.strftime("%Y-%m-%d") / f"{kind}-{t.hour:02d}.bin"
        if f.exists():
            parts.append(np.fromfile(f, dtype=dt, count=f.stat().st_size // dt.itemsize))
    x = np.concatenate(parts) if parts else np.zeros(0, dt)
    return x[x["l"] >= since_us]


def pcts(v, ps=(50, 90, 99)):
    return [round(float(np.percentile(v, p)), 1) for p in ps] if len(v) else [np.nan] * len(ps)


def cpu_sample(dt=1.0):
    def read():
        f = open("/proc/stat").readline().split()[1:]
        return [int(v) for v in f]

    a = read()
    time.sleep(dt)
    b = read()
    d = [y - x for x, y in zip(a, b)]
    tot = sum(d) or 1
    idle = d[3] + (d[4] if len(d) > 4 else 0)
    steal = d[7] if len(d) > 7 else 0
    return round(100 * steal / tot, 2), round(100 * (tot - idle) / tot, 1)


def meminfo():
    m = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        m[k] = int(v.split()[0]) // 1024
    return m.get("MemAvailable", 0), m.get("SwapTotal", 0) - m.get("SwapFree", 0)


def cmd_sample(a):
    now = datetime.now(timezone.utc)
    since = int((now - timedelta(minutes=a.window)).timestamp() * 1e6)
    pm = load_rows(a.rec, "pm", PM_DT, since, now)
    pm = pm[pm["x"] > 0]
    d = pm["l"] / 1000 - pm["x"]
    tr = load_rows(a.rec, "tr", TR_DT, since, now)
    td = tr["l"] / 1000 - tr["x"]
    bn = load_rows(a.rec, "bn", BN_DT, since, now)
    bn = bn[bn["src"] == 0]
    bd = bn["l"] / 1000 - bn["T"]
    steal, busy = cpu_sample()
    l1, l5, _ = os.getloadavg()
    mem, swap = meminfo()
    row = [now.isoformat(timespec="seconds"), a.window, len(d), *pcts(d), round(100 * float((d > 1000).mean()), 3) if len(d) else np.nan,
           len(td), *pcts(td, (50, 90)), len(bd), *pcts(bd, (50, 90)), round(l1, 2), round(l5, 2), mem, swap, steal, busy]  # fmt: skip
    new = not a.log.exists()
    with a.log.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLS)
        w.writerow(row)
    print(dict(zip(COLS, row)))


def cmd_report(a):
    import pandas as pd

    D = pd.read_csv(a.log, parse_dates=["ts"])
    D = D[D.ts >= D.ts.max() - pd.Timedelta(hours=a.hours)]
    D["hour"] = D.ts.dt.floor("h")
    H = D.groupby("hour").agg(samples=("pm_n", "size"), pm_rows=("pm_n", "sum"), pm_p50=("pm_p50", "median"),
                              pm_p90=("pm_p90", "median"), pm_p90_max=("pm_p90", "max"), pm_p99=("pm_p99", "median"),
                              gt1s_pct=("pm_gt1s_pct", "mean"), tr_rows=("tr_n", "sum"), tr_p90=("tr_p90", "median"),
                              spot_p90=("spot_p90", "median"), load5=("load5", "mean"), busy=("busy_pct", "mean"),
                              steal=("steal_pct", "mean"), swap_mb=("swap_used_mb", "max"))  # fmt: skip
    pd.set_option("display.width", 250)
    print(H.round(1).to_string())
    span_h = (D.ts.max() - D.ts.min()).total_seconds() / 3600
    bad_share = float((D.pm_p90 > 200).mean())
    gt1 = float((D.pm_gt1s_pct * D.pm_n).sum() / max(D.pm_n.sum(), 1))
    print(f"\nspan {span_h:.1f} h, {len(D)} samples | samples with pm p90 > 200 ms: {100 * bad_share:.1f}% | pooled pm rows > 1 s late: {gt1:.3f}%")
    if span_h < 48:
        v = "COLLECTING (rule needs >= 48 h)"
    elif bad_share > 0.10 or gt1 > 0.5:
        v = "NEW BOX: the recorder (and a lead-lag bot) cannot keep up on this host"
    else:
        v = "KEEP: latency is clean on this host"
    print("verdict:", v, "  [rule: NEW BOX if > 10% of samples have pm p90 > 200 ms or > 0.5% of pm rows arrive > 1 s late]")


def cmd_detail(a):
    """Delay of rows received since --since (UTC ISO), split by time into the window and by asset."""
    t0 = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    parts, t = [], t0.replace(minute=0, second=0, microsecond=0)
    while t <= now:
        f = a.rec / t.strftime("%Y-%m-%d") / f"pm-{t.hour:02d}.bin"
        if f.exists():
            parts.append(np.fromfile(f, dtype=PM_DT, count=f.stat().st_size // PM_DT.itemsize))
        t += timedelta(hours=1)
    pm = np.concatenate(parts) if parts else np.zeros(0, PM_DT)
    pm = pm[(pm["x"] > 0) & (pm["l"] >= int(t0.timestamp() * 1e6))]
    d = pm["l"] / 1000 - pm["x"]
    tw = pm["x"] // 1000 - pm["e"].astype(np.int64)
    tf = pm["tf"].astype(int)

    def P(v):
        return f"n={len(v):>8} p50 {np.percentile(v, 50):>6.0f} p90 {np.percentile(v, 90):>7.0f} p99 {np.percentile(v, 99):>7.0f} >1s {100 * (v > 1000).mean():5.1f}%" if len(v) else "n=0"

    print(f"since {t0.isoformat()} ({(now - t0).total_seconds() / 60:.0f} min)")
    print("all                         ", P(d))
    for w in (5, 15):
        for lo, hi in ((-60, 0), (0, 30), (30, 120), (120, 60 * w)):
            m = (tf == w) & (tw >= lo) & (tw < hi)
            print(f"{w:>2}m {lo:>4}..{hi:<4}s into window   ", P(d[m]))
    for i, n in enumerate(("btc", "eth", "sol", "xrp", "doge")):
        print(f"{n:>5}                       ", P(d[pm["a"] == i]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("sample")
    p.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    p.add_argument("--log", type=Path, default=Path.home() / "shadow-latency.csv")
    p.add_argument("--window", type=int, default=15, help="minutes")
    p = sp.add_parser("detail")
    p.add_argument("--rec", type=Path, default=Path.home() / "shadow-rec")
    p.add_argument("--since", required=True, help="UTC, e.g. 2026-11-14T00:05")
    p = sp.add_parser("report")
    p.add_argument("--log", type=Path, default=Path.home() / "shadow-latency.csv")
    p.add_argument("--hours", type=int, default=48)
    a = ap.parse_args()
    {"sample": cmd_sample, "report": cmd_report, "detail": cmd_detail}[a.cmd](a)


if __name__ == "__main__":
    main()
