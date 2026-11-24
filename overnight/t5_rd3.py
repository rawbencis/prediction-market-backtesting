"""T5: R-D3 live attempts (fill / nofill rows of ~/rd3-live/events.jsonl) against the recorded BTC 5m book (exchange time)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])
ROOT = Path("/home/ubuntu/shadow-rec")
ev = [json.loads(x) for x in open("/home/ubuntu/rd3-live/events.jsonl")]
att = [d for d in ev if d.get("ev") in ("fill", "nofill") or (d.get("ev") == "halt" and d.get("raw", {}).get("status") == "matched")]
cache = {}


def book(epoch):
    if epoch in cache:
        return cache[epoch]
    rows = []
    for t in (epoch - 3600, epoch, epoch + 300):
        dt = datetime.fromtimestamp(t, timezone.utc)
        f = ROOT / dt.strftime("%Y-%m-%d") / f"pm-{dt:%H}.bin"
        if f.exists():
            x = np.fromfile(f, dtype=PM_DT)
            rows.append(x[(x["tf"] == 5) & (x["a"] == 0) & (x["e"] == epoch)].copy())
    B = np.concatenate(rows) if rows else np.zeros(0, PM_DT)
    _, u = np.unique(B[["l", "x", "b", "k", "bs", "ks"]], return_index=True)
    B = np.sort(B[u], order="x")
    cache[epoch] = B
    return B


def ask_at(B, tok, t):
    i = np.searchsorted(B["x"], t, "right") - 1
    if i < 0:
        return np.nan, 0.0, np.nan
    r = B[i]
    if r["b"] == 0 or r["k"] == 0:
        return np.nan, 0.0, t - r["x"]
    return ((r["k"] / 1e4, float(r["ks"])) if tok == "up" else (1 - r["b"] / 1e4, float(r["bs"]))) + (t - r["x"],)


rows = []
for d in att:
    B = book(d["epoch"])
    kind = "fill" if d["ev"] != "nofill" else "nofill"
    r = dict(kind=kind, why=d.get("why"), side=d["side"], limit=d["limit"], size=d["size"], nrows=len(B),
             lat=d["t_ack"] - d["t_send"] if "t_send" in d and "t_ack" in d else np.nan)
    if not len(B) or "t_send" not in d:
        rows.append(r)
        continue
    for nm, t in (("T", d["T"]), ("send265", d["t_send"] + 265), ("ack115", d["t_ack"] - 115), ("ack", d["t_ack"])):
        a, s, age = ask_at(B, d["side"], t)
        r[nm] = (a, s, age)
        r[f"pres_{nm}"] = bool(a == a and a <= d["limit"] + 1e-9 and s >= d["size"])
    r["move"] = r["ack115"][0] - r["T"][0]
    rows.append(r)

for kind in ("fill", "nofill"):
    R = [r for r in rows if r["kind"] == kind]
    Rb = [r for r in R if "T" in r]
    print(f"\n{kind}: {len(R)} attempts, {len(Rb)} with recorded book; ack latency ms p50 {np.nanmedian([r['lat'] for r in R]):.0f}")
    for nm in ("T", "send265", "ack115", "ack"):
        print(f"  present at {nm:8s}: {sum(r['pres_' + nm] for r in Rb)}/{len(Rb)} = {np.mean([r['pres_' + nm] for r in Rb]) if Rb else float('nan'):.2f}")
    mv = np.array([r["move"] for r in Rb if r["move"] == r["move"]])
    if len(mv):
        print(f"  ask move T -> ack-115 (c): mean {100 * mv.mean():+.2f}, median {100 * np.median(mv):+.2f}, share up {np.mean(mv > 0):.2f}, share 0 {np.mean(mv == 0):.2f}")
    print("  why:", {w: sum(1 for r in R if r["why"] == w) for w in {r["why"] for r in R}})
    print("  book age at send+265 ms (ms) p50:", np.nanmedian([r["send265"][2] for r in Rb]) if Rb else None)
