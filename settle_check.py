"""Settlement check: recompute BTC 5m/15m outcomes from recorded Chainlink feeds (feeds_rec) and compare with Gamma.
Rules text: Up iff TWAP (Chainlink BTC/USD 60s-TWAP stream) of the range >= the price at the beginning of the range.
The opening definition is not given, so several readings are scored; a window counts only if both feeds have a
value within 2 s of start and end. Usage: python settle_check.py [--feeds ~/feeds-rec]
"""
import argparse, glob, zlib
from pathlib import Path

import numpy as np
import orjson

import dsim as d

DEFS = {  # name: (open source, close source); value = the last update with venue timestamp <= the boundary second
    "twap@start vs twap@end": ("twap60", "twap60"),
    "spot@start vs twap@end": ("chainlink", "twap60"),
    "spot@start vs spot@end": ("chainlink", "chainlink"),
}


def read_gz(path):
    """Rows of a gzip JSONL that may still be open for writing (tolerates a truncated tail)."""
    z, buf = zlib.decompressobj(31), b""
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            try:
                buf += z.decompress(chunk)
            except zlib.error:
                break
            while z.eof and z.unused_data:  # concatenated members (append mode reopen)
                rest = z.unused_data
                z = zlib.decompressobj(31)
                buf += z.decompress(rest)
    lines = buf.split(b"\n")
    out = []
    for ln in lines:
        try:
            out.append(orjson.loads(ln))
        except orjson.JSONDecodeError:
            pass
    return out


def series(root, feed, sym="btc"):
    rows = [r for p in sorted(glob.glob(f"{root}/*/{feed}-*.jsonl.gz")) for r in read_gz(p)]
    xs = np.array([r["x"] for r in rows if r.get("s") == sym and not r.get("snap")], dtype=np.int64)
    vs = np.array([float(r["v"]) for r in rows if r.get("s") == sym and not r.get("snap")])
    o = np.argsort(xs, kind="stable")
    return xs[o], vs[o]


def value_at(s, t_ms, tol=2000):
    xs, vs = s
    i = np.searchsorted(xs, t_ms, "right") - 1
    return float(vs[i]) if i >= 0 and t_ms - xs[i] <= tol else None


def outcome(open_v, close_v):
    return "UP" if close_v >= open_v else "DOWN"


def main(a):
    S = {f: series(a.feeds, f) for f in ("twap60", "chainlink")}
    lo = max(S[f][0][0] for f in S) if all(len(S[f][0]) for f in S) else None
    hi = min(S[f][0][-1] for f in S) if lo else None
    if lo is None:
        print("no feed data")
        return
    wins = [(tf, e) for tf in (5, 15) for e in range(lo // 1000 // (tf * 60) * tf * 60, hi // 1000, tf * 60)
            if e * 1000 >= lo and (e + tf * 60) * 1000 <= hi]
    W = d.fetch_winners([f"btc-updown-{tf}m-{e}" for tf, e in wins])
    print(f"feeds cover {(hi - lo) / 3.6e6:.2f} h; BTC windows fully covered: {len(wins)}")
    for name, (fo, fc) in DEFS.items():
        n = agree = 0
        bad = []
        for tf, e in wins:
            w = W.get(f"btc-updown-{tf}m-{e}")
            ov, cv = value_at(S[fo], e * 1000), value_at(S[fc], (e + tf * 60) * 1000)
            if w is None or ov is None or cv is None:
                continue
            n += 1
            if outcome(ov, cv) == w:
                agree += 1
            else:
                bad.append(f"    {tf}m {e} gamma={w} open={ov:.4f} close={cv:.4f} diff={cv - ov:+.4f}")
        print(f"  {name:26s} agree {agree}/{n}" + (f" ({agree / n:.1%})" if n else ""))
        if a.verbose or len(bad) <= 10:
            print("\n".join(bad))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default=str(Path.home() / "feeds-rec"))
    ap.add_argument("-v", "--verbose", action="store_true")
    main(ap.parse_args())
