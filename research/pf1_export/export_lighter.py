"""PF1 Part A (Tokyo): export Lighter BTC 15m books and the live probe's records for the Dublin test.

Read-only. Streams one hourly file at a time; no keys, no network.

books  OUT/lighter-btc15m/YYYY-MM-DD.csv.gz, one row per top-3 book change of a BTC 15m binary
       (the recorder writes a row only when the top 3 levels change):
         t_exch_ms, t_recv_ms, symbol, window_start, bid1, bidsz1, ask1, asksz1, ... ask3, asksz3
       prices in $ (4 dp). An empty level is 0,0. A row with every level 0 is an empty book or a
       book the recorder no longer knows (socket reconnect; then exch_ms = last seen + 1). Either
       way: no liquidity until the next non-zero row.
       Also OUT/lighter-btc15m/markets.csv (window_start -> market_id, trade start/end ms) and
       outcomes.csv (market_id, status, outcome), copied as-is for BTC 15m.
probe  OUT/probe-1126-1127.jsonl.gz: the trade-path records of ~/lp-arb/probe/*.jsonl (triggers,
       checks, persistence, skips, Lighter orders/fills/fees, Polymarket orders/fills, pairs,
       unwinds, unhedged, paper hedges, settles, window/meta, health lags). Account snapshots
       (balance, reconcile, probe_start) are left out. Every record is scanned for key-like strings
       first; a hit aborts the export.

Source format (~/lighter-shadow/lighter_rec.py): lb-HH.bin, struct "<qqBBI" + "Hf"*6 =
local_us, exch_ms, asset, tf, epoch, 3 x (bid px 1e-4 $, size), 3 x (ask px, size); asset 0 = btc.
"""

import argparse
import csv
import datetime as dt
import gzip
import json
import re
import statistics
import struct
import sys
from pathlib import Path

LB_REC = struct.Struct("<qqBBI" + "Hf" * 6)
BTC = 0
COLS = ["t_exch_ms", "t_recv_ms", "symbol", "window_start"] + [
    f"{s}{k}{i}" for i in (1, 2, 3) for s, k in (("bid", ""), ("bid", "sz"), ("ask", ""), ("ask", "sz"))
]
PROBE_KINDS = {
    "trigger", "check", "persist", "skip", "l_order", "l_fill", "l_fee", "pm_order", "pm_fill", "pair",
    "fees", "unwind", "unhedged", "hedge", "paper_hedge", "settle", "paper_settle", "probe_settle",
    "lighter_settled", "redeem_due", "window", "meta", "health", "start", "stop", "probe_stop", "error",
}
# Field names that must never appear; hex blobs allowed only under fields known to hold public ids.
BAD_KEY = re.compile(r"priv|secret|passphrase|mnemonic|seed|api_?key|password|signature|auth", re.I)
HEX64 = re.compile(r"(?:0x)?[0-9a-fA-F]{64,}")
HEX_OK = {"order_id", "tx", "cond", "up_token", "down_token", "token"}
ORDER_ID_IN_TEXT = re.compile(r"'orderID': '0x[0-9a-fA-F]{64}'")  # PM order hash echoed in FAK-killed errors


def days(a, b):
    d = dt.date.fromisoformat(a)
    while d <= dt.date.fromisoformat(b):
        yield d.isoformat()
        d += dt.timedelta(days=1)


def lvl(p, s):
    return (f"{p / 10000:.4f}", f"{s:g}")


def export_day(src, out, day):
    files = sorted((src / day).glob("lb-*.bin"))
    if not files:
        return None
    lags, n, unknown, hours = [], 0, 0, set()
    tmp = out / f".{day}.csv.gz.part"
    with gzip.open(tmp, "wt", newline="", compresslevel=6) as g:
        w = csv.writer(g)
        w.writerow(COLS)
        for f in files:
            hours.add(f.name[3:5])
            data = f.read_bytes()
            usable = len(data) - len(data) % LB_REC.size  # a file being appended may end mid-record
            for r in LB_REC.iter_unpack(memoryview(data)[:usable]):
                lus, xms, a, tf, ep = r[:5]
                if a != BTC or tf != 15:
                    continue
                v = r[5:]
                row = [xms, f"{lus / 1000:.3f}", f"BTC-UPDOWN-15M-{ep}", ep]
                for i in range(3):  # interleave bid_i, ask_i
                    row += [*lvl(v[2 * i], v[2 * i + 1]), *lvl(v[6 + 2 * i], v[7 + 2 * i])]
                w.writerow(row)
                n += 1
                if not any(v):
                    unknown += 1
                else:
                    lags.append(lus / 1000 - xms)
    tmp.rename(out / f"{day}.csv.gz")
    q = statistics.quantiles(lags, n=20) if len(lags) > 20 else [float("nan")] * 19
    return dict(day=day, rows=n, unknown_rows=unknown, hours=len(hours),
                lag_p05=q[0], lag_med=statistics.median(lags) if lags else float("nan"), lag_p95=q[18],
                mb=(out / f"{day}.csv.gz").stat().st_size / 1e6)


def copy_meta(src, out):
    with (src / "lighter_markets.csv").open() as f, (out / "markets.csv").open("w") as g:
        g.write("asset,tf,window_start,market_id,trade_start_ms,trade_end_ms\n")
        ids = set()
        for line in f:
            p = line.strip().split(",")
            if len(p) == 6 and p[0] == "btc" and p[1] == "15":
                g.write(line if line.endswith("\n") else line + "\n")
                ids.add(p[3])
    with (src / "lighter_outcomes.csv").open() as f, (out / "outcomes.csv").open("w") as g:
        g.write("market_id,status,outcome,local_s\n")
        for line in f:
            if line.split(",", 1)[0] in ids:
                g.write(line)
    return len(ids)


def scan(obj, path=""):
    """-> list of suspicious (path, reason)"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if BAD_KEY.search(str(k)):
                hits.append((f"{path}.{k}", "key name"))
            if isinstance(v, str) and HEX64.search(ORDER_ID_IN_TEXT.sub("", v)) and k not in HEX_OK:
                hits.append((f"{path}.{k}", "long hex"))
            hits += scan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += scan(v, f"{path}[{i}]")
    elif isinstance(obj, str) and BAD_KEY.search(obj) and len(obj) > 40:
        hits.append((path, "suspicious text"))
    return hits


def export_probe(files, dest):
    keep, kinds, hits = [], {}, []
    for f in files:
        for line in f.open():
            r = json.loads(line)
            if r.get("kind") not in PROBE_KINDS:
                continue
            hits += [(f.name, r.get("kind"), *h) for h in scan(r)]
            keep.append(line if line.endswith("\n") else line + "\n")
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    if hits:
        for h in hits[:20]:
            print("KEY-LIKE:", h, file=sys.stderr)
        raise SystemExit(f"{len(hits)} key-like fields; probe export aborted")
    with gzip.open(dest, "wt") as g:
        g.writelines(keep)
    return kinds


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("books")
    b.add_argument("--src", type=Path, default=Path.home() / "lighter-data")
    b.add_argument("--out", type=Path, default=Path.home() / "export/lighter-btc15m")
    b.add_argument("--since", required=True)
    b.add_argument("--until", required=True)
    p = sp.add_parser("probe")
    p.add_argument("--src", type=Path, default=Path.home() / "lp-arb/probe")
    p.add_argument("--days", nargs="+", default=["2026-11-26", "2026-11-27"])
    p.add_argument("--out", type=Path, default=Path.home() / "export/probe-1126-1127.jsonl.gz")
    a = ap.parse_args()
    if a.cmd == "books":
        a.out.mkdir(parents=True, exist_ok=True)
        print("markets:", copy_meta(a.src, a.out), "BTC 15m")
        print("day         rows  unknown hours  MB    recv-exch ms p05 / med / p95")
        for d in days(a.since, a.until):
            s = export_day(a.src, a.out, d)
            if s is None:
                print(f"{d}  MISSING")
                continue
            print(f"{s['day']} {s['rows']:8d} {s['unknown_rows']:6d} {s['hours']:4d} {s['mb']:6.1f}"
                  f"   {s['lag_p05']:6.1f} / {s['lag_med']:6.1f} / {s['lag_p95']:7.1f}", flush=True)
    else:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        files = [a.src / f"{d}.jsonl" for d in a.days]
        kinds = export_probe(files, a.out)
        print(a.out, f"{a.out.stat().st_size / 1e3:.0f} kB", kinds)


if __name__ == "__main__":
    main()
