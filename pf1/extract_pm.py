"""PF1: extract Dublin's Polymarket BTC 15m books (Up token, top 3) and trades into compact per-hour npz files.

Source: ~/feeds-rec/DAY/pmbook-HH.jsonl.gz (Dublin receive ns `r`, exchange ms `x`, top-5 depth, both tokens).
The Down book is the mirror of the Up book on the CLOB (1 mismatch in 745k same-receive rows, 11-20 12h),
so only Up rows are kept, and only when the top 3 levels changed. `drop` rows (socket lost) are kept as NaN rows.
Fallback for days without pmbook (11-14 has 1 hour): ~/shadow-rec/DAY/pm-HH.bin (Up top of book only; levels 2-3 NaN).

Out: OUT/DAY/pm-HH.npz with r (Dublin receive ms, f8), x (exchange ms, i8; 0 on drop rows), e (window epoch, i4),
bp/bs (bid px/size x3, best first), ap/as (ask px/size x3), and tr (trades: r, x, e, tok 0 up/1 down, side 0 BUY/1 SELL, p, sz).
Streaming (one hour in memory, compact arrays). Read-only on the recorder files.
Usage: python -I extract_pm.py DAY [--out ~/pf1-cache] [--source auto|pmbook|shadow]
"""
import argparse
import gzip
import struct
import sys
import time
from array import array
from pathlib import Path

import numpy as np
import orjson
import zlib

L = 3
SKIPPED = {"members_bad": 0, "bytes_skipped": 0}


def gz_lines(fp, chunk=1 << 20):
    """Lines of a (possibly multi-member, possibly damaged) gzip file. A recorder restart can leave a truncated member;
    on a decode error, skip to the next gzip header and count the skipped bytes (SKIPPED)."""
    data = fp.read_bytes()
    pos, n, tail = 0, len(data), b""
    while pos < n:
        d = zlib.decompressobj(31)
        p = pos
        try:
            while p < n and not d.eof:
                out = d.decompress(data[p:p + chunk])
                p += chunk
                if out:
                    buf = tail + out
                    lines = buf.split(b"\n")
                    tail = lines.pop()
                    yield from lines
            if d.eof:
                pos = n - len(d.unused_data) if p >= n else p - len(d.unused_data)
            else:
                pos = n
        except zlib.error:
            # the bad block is inside the chunk that failed: resume at the first gzip header after its start
            SKIPPED["members_bad"] += 1
            start = max(p, pos + 1)
            nxt = data.find(b"\x1f\x8b\x08", start)
            nxt = n if nxt < 0 else nxt
            SKIPPED["bytes_skipped"] += nxt - p
            pos, tail = nxt, b""
    if tail:
        yield tail
NAN = float("nan")


class Cols:
    def __init__(self):
        self.r, self.x, self.e = array("d"), array("q"), array("i")
        self.lv = [array("f") for _ in range(4 * L)]  # bp0..2, bs0..2, ap0..2, as0..2
        self.tr = array("d")

    def add(self, r, x, e, bp, bs, ap, as_):
        self.r.append(r); self.x.append(x); self.e.append(e)
        for i, v in enumerate(bp + bs + ap + as_):
            self.lv[i].append(v)

    def save(self, fp, source):
        n = len(self.r)
        o = np.argsort(np.frombuffer(self.r, dtype=np.float64), kind="stable") if n else np.zeros(0, int)
        lv = [np.frombuffer(c, dtype=np.float32)[o] if n else np.zeros(0, np.float32) for c in self.lv]
        tr = np.frombuffer(self.tr, dtype=np.float64).reshape(-1, 7) if len(self.tr) else np.zeros((0, 7))
        np.savez_compressed(fp, r=np.frombuffer(self.r, dtype=np.float64)[o], x=np.frombuffer(self.x, dtype=np.int64)[o],
                            e=np.frombuffer(self.e, dtype=np.int32)[o], bp=np.stack(lv[0:L], 1), bs=np.stack(lv[L:2 * L], 1),
                            ap=np.stack(lv[2 * L:3 * L], 1), **{"as": np.stack(lv[3 * L:], 1)},
                            tr=tr[np.argsort(tr[:, 0], kind="stable")], source=np.asarray(source))
        return n, len(tr)


def levels(side):
    side = side[:L]
    return [float(p) for p, _ in side] + [NAN] * (L - len(side)), [float(s) for _, s in side] + [0.0] * (L - len(side))


def hour_pmbook(fp):
    c, last = Cols(), {}
    for ln in gz_lines(fp):
            if b"btc-updown-15m" not in ln:
                continue
            try:
                d = orjson.loads(ln)
            except orjson.JSONDecodeError:  # a line cut by a damaged member
                SKIPPED["bad_lines"] = SKIPPED.get("bad_lines", 0) + 1
                continue
            e = int(d["slug"].rsplit("-", 1)[1])
            ev = d.get("ev")
            if ev == "trade":
                try:
                    c.tr.extend((d["r"] / 1e6, d["x"], e, 0 if d["tok"] == "up" else 1, 0 if d["side"] == "BUY" else 1, float(d["p"]), float(d["sz"])))
                except (KeyError, TypeError, ValueError):
                    pass
                continue
            if ev == "drop":
                last.pop(e, None)
                c.add(d["r"] / 1e6, 0, e, [NAN] * L, [0.0] * L, [NAN] * L, [0.0] * L)
                continue
            if d.get("tok") != "up":
                continue
            bp, bs = levels(d["b"])
            ap, as_ = levels(d["a"])
            k = (tuple(bp), tuple(bs), tuple(ap), tuple(as_))
            if last.get(e) == k:
                continue
            last[e] = k
            c.add(d["r"] / 1e6, d["x"], e, bp, bs, ap, as_)
    return c


PM_REC = struct.Struct("<qqBBIHHff")
TR_REC = struct.Struct("<qqBBIBBHfH32s")


def hour_shadow(pm_fp, tr_fp):
    c = Cols()
    buf = pm_fp.read_bytes()
    for lus, x, asset, tf, ep, bid, ask, bsz, asz in PM_REC.iter_unpack(buf[: len(buf) // PM_REC.size * PM_REC.size]):
        if asset != 0 or tf != 15:
            continue
        if bid == 0 and ask == 0:
            c.add(lus / 1000, 0, ep, [NAN] * L, [0.0] * L, [NAN] * L, [0.0] * L)
            continue
        c.add(lus / 1000, x, ep, [bid / 1e4 if bid else NAN, NAN, NAN], [bsz if bid else 0.0, 0.0, 0.0],
              [ask / 1e4 if ask else NAN, NAN, NAN], [asz if ask else 0.0, 0.0, 0.0])
    if tr_fp.exists():
        buf = tr_fp.read_bytes()
        for lus, x, asset, tf, ep, tok, side, p, sz, _fee, _tx in TR_REC.iter_unpack(buf[: len(buf) // TR_REC.size * TR_REC.size]):
            if asset == 0 and tf == 15:
                c.tr.extend((lus / 1000, x, ep, tok, side, p / 1e4, sz))
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--out", type=Path, default=Path.home() / "pf1-cache")
    ap.add_argument("--source", choices=["auto", "pmbook", "shadow"], default="auto")
    a = ap.parse_args()
    t0 = time.time()
    fdir, sdir = Path.home() / "feeds-rec" / a.day, Path.home() / "shadow-rec" / a.day
    pmb = sorted(fdir.glob("pmbook-*.jsonl.gz"))
    nsh = len(list(sdir.glob("pm-*.bin")))
    src = a.source if a.source != "auto" else ("pmbook" if pmb and len(pmb) >= nsh - 1 else "shadow")
    od = a.out / a.day
    od.mkdir(parents=True, exist_ok=True)
    tot = 0
    if src == "pmbook":
        jobs = [(fp.name[7:9], lambda fp=fp: hour_pmbook(fp)) for fp in pmb]
        label = "feeds-rec pmbook top3"
    else:
        jobs = [(fp.name[3:5], lambda fp=fp: hour_shadow(fp, sdir / f"tr-{fp.name[3:5]}.bin")) for fp in sorted(sdir.glob("pm-*.bin"))]
        label = "shadow-rec pm top1"
    for hh, job in jobs:
        n, nt = job().save(od / f"pm-{hh}.npz", label)
        tot += n
        print(a.day, hh, label, "rows", n, "trades", nt, f"{time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    print(a.day, label, "total rows", tot, "gzip", SKIPPED, f"{time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
