"""Dublin 11-24 copy of mk_level.py for one wallet (env LVL_W) and BTC timeframe (env LVL_TF); winners from
~/dublin-1124/settle.csv (no network); output ~/dublin-1124/d1b/lvl. Original docstring:
15m ladder follow-up, steps 0-1: level history of each BTC 15m maker fill of the given wallets (RTDS census),
on the 5-level book recorded by feeds-rec (pmbook: UP and DOWN books, 5 levels each, plus trade prints with tx, same
socket). Polymarket data only.

Per census maker BUY fill (wallet BUY tok at P, fee <= 0 in the census):
  timing   census x (RTDS emit ms) and ts; shadow-rec print x (same tx); pmbook print x (same tx); the pmbook book row
           where tok's bid level P lost ~ the fill (|drop - print size| <= 10% + 0.01, or vanished), searched within
           +-300 ms of the pmbook print -> t_book (the fill as seen in the book) and the row just before it (pre-fill).
  old      the tick vs best bid as in mk_wallets (shadow-rec top book at print x - 1)
  new      the tick vs best bid on the pmbook pre-fill row
  history  at pre-fill - 5 s / 30 s / 60 s: displayed size at P and P's distance below the best bid in cents
           (vis = 0: not visible = P below the 5th level shown; absent = P inside the shown range with no size)
  best     if P is the best bid on the pre-fill row: since when (tb), the size at P on the row it became best, and the
           volume printed at P (pmbook prints hitting tok's bid at P) from tb to the fill (excluding this tx)
  class    stretch start ta = first row of the run (ending at tb, or at the fill if P is not the best bid) in which P was
           never visibly absent; DEEP: tb - ta >= 30 s; NEW: tb - ta < 1 s; OTHER: in between.
           deep_vis: P actually displayed (and below the best) at tb - 30 s (stricter).
  outcome  token-mid markouts at 1 / 5 / 30 s from t_book (only while two-sided and > 30 s remain), settle c/share.
Usage: python mk_level.py DAY [DAY ...] -> out/lvl/lvl-{day}.csv.gz
"""
import array
import gzip
import zlib
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import orjson
import pandas as pd

from act_rec import ACT_DT  # noqa: I001
import pmlat
import dsim

FR = Path.home() / "feeds-rec"
REC = Path.home() / "shadow-rec"
OUT = Path.home() / "dublin-1124" / "d1b" / "lvl"
import os
TF = int(os.environ["LVL_TF"])
WAL = {os.environ["LVL_W"].lower()[2:]: os.environ["LVL_W"].lower()[2:12]}
ADT = np.dtype(ACT_DT)
NL = 5
TAG = f"btc-updown-{TF}m-".encode()
LAST_HOUR = {}  # day still being recorded: only complete hour files


# pmbook hour files with a corrupt gzip block (gzip -t, 11-13 23:00 .. 11-18 20:00); rows after the bad block are lost.
# Windows whose receive span (e - 45 s .. e + 960 s) overlaps one of these hours are excluded everywhere.
BAD_HOURS = (("2026-11-15", 8), ("2026-11-15", 9), ("2026-11-15", 14), ("2026-11-16", 19), ("2026-11-16", 20))
_BAD_T = [int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()) + 3600 * h for d, h in BAD_HOURS]


def bad_window(e):
    return any(e - 45 < t + 3600 and e + 60 * TF + 60 > t for t in _BAD_T)


def lines15(p):
    """15m lines of one pmbook hour file; a corrupt gzip block ends the file there (logged, rows before it kept)."""
    n = 0
    try:
        with gzip.open(p, "rb") as fo:
            for line in fo:
                n += 1
                if TAG in line:
                    yield line
    except (OSError, EOFError, zlib.error) as ex:
        print("BAD", p, "after line", n, repr(ex)[:80], flush=True)


def hours_for(day):
    d = date.fromisoformat(day)
    prev = (d - timedelta(1)).isoformat()
    out = [(prev, 23)] + [(day, h) for h in range(LAST_HOUR.get(day, 23) + 1)]
    nxt = (d + timedelta(1)).isoformat()
    return out + ([(nxt, 0)] if day not in LAST_HOUR else [])


def census(day):
    parts = []
    for f in sorted((REC / day).glob("act-*.bin")):
        a = np.fromfile(f, dtype=ADT)
        a = a[(a["a"] == 0) & (a["tf"] == TF)]
        if len(a):
            parts.append(a)
    a = np.concatenate(parts)
    wh = np.array([w.ljust(20, b"\0").hex() for w in a["w"]])
    keep = np.isin(wh, list(WAL))
    return a[keep], wh[keep]


def shadow_prints(day, txs):
    parts = []
    for dd, h in hours_for(day) + [((date.fromisoformat(day) + timedelta(1)).isoformat(), 0)]:
        if day in LAST_HOUR and dd > day:
            continue
        p = REC / dd / f"tr-{h:02d}.bin"
        if p.exists():
            x = np.fromfile(p, dtype=dsim.TR_DT)
            parts.append(x[(x["a"] == 0) & (x["tf"] == TF) & np.isin(x["tx"], txs)])
    return np.concatenate(parts) if parts else np.zeros(0, dsim.TR_DT)


def shadow_top(day, eps):
    parts = []
    for dd, h in hours_for(day):
        p = REC / dd / f"pm-{h:02d}.bin"
        if p.exists():
            x = np.fromfile(p, dtype=dsim.PM_DT)
            parts.append(x[(x["a"] == 0) & (x["tf"] == TF) & np.isin(x["e"], eps)])
    pm = np.concatenate(parts)
    return {int(e): np.sort(pm[pm["e"] == e], order="x", kind="stable") for e in eps}


class Mk:
    """One window's pmbook rows (UP book only; DOWN is its mirror) and prints."""

    def __init__(self):
        self.x = array.array("q")
        self.r = array.array("q")  # receive time, ms
        self.lv = array.array("f")  # per row: 5 bid px, 5 bid sz, 5 ask px, 5 ask sz (px in $)
        self.tr = []  # (x, tok, side, p, sz, tx)

    def add(self, d):
        if d.get("ev") == "trade":
            self.tr.append((int(d["x"]), 0 if d["tok"] == "up" else 1, 0 if d["side"] == "BUY" else 1, float(d["p"]),
                            float(d["sz"]), d["tx"]))
            return
        if d.get("ev") == "drop":
            self.x.append(-1)  # marker: book unknown until the next row; stored as x = -1 then fixed in arrays()
            self.r.append(int(d["r"]) // 1_000_000)
            self.lv.extend([0.0] * 20)
            return
        if d.get("tok") != "up":
            return
        self.x.append(int(d["x"]))
        self.r.append(int(d["r"]) // 1_000_000)
        b, a = d["b"][:NL], d["a"][:NL]
        bp = [v[0] for v in b] + [0.0] * (NL - len(b))
        bs = [v[1] for v in b] + [0.0] * (NL - len(b))
        ap = [v[0] for v in a] + [0.0] * (NL - len(a))
        az = [v[1] for v in a] + [0.0] * (NL - len(a))
        self.lv.extend(bp + bs + ap + az)

    def arrays(self):
        x = np.frombuffer(self.x, dtype=np.int64).copy()
        rr = np.frombuffer(self.r, dtype=np.int64).copy()
        L = np.frombuffer(self.lv, dtype=np.float32).reshape(-1, 20).copy()
        drop = x < 0
        for i in np.flatnonzero(drop):  # a drop row inherits the previous time
            x[i] = x[i - 1] if i > 0 else 0
        o = np.argsort(x, kind="stable")
        self.rr = rr[o]
        return x[o], L[o], drop[o]


def tok_book(L, tok):
    """-> (bid px int 1e-4 [n,5], bid sz [n,5], ask px, ask sz) for tok (DOWN = 1 - UP asks)."""
    bp, bs, ap, az = (L[:, i * 5:(i + 1) * 5] for i in range(4))
    r = lambda v: np.rint(v * 1e4).astype(np.int64)  # noqa: E731
    if tok == 0:
        return r(bp), bs, r(ap), az
    dbp = np.where(ap > 0, 10000 - r(ap), 0)
    dap = np.where(bp > 0, 10000 - r(bp), 0)
    return dbp, az, dap, bs


def at_level(BP, BS, i, P):
    """-> (state, size, dist_c): state 'best'/'below'/'absent'/'hidden'/'above'/'none'."""
    row = BP[i]
    if row[0] <= 0:
        return "none", 0.0, None
    if P > row[0]:
        return "above", 0.0, None
    k = np.flatnonzero(row == P)
    if len(k):
        return ("best" if k[0] == 0 else "below"), float(BS[i, k[0]]), (row[0] - P) / 100
    last = row[row > 0].min()
    full = (row > 0).sum() == NL
    if P < last and full:
        return "hidden", 0.0, (row[0] - P) / 100
    return "absent", 0.0, (row[0] - P) / 100


def process(e, mk, fills, top, win):
    x, L, drop = mk.arrays()
    if not len(x):
        return []
    end = (e + 60 * TF) * 1000
    T = sorted(mk.tr)
    tx_ = np.array([t[0] for t in T], np.int64)
    books = {tok: tok_book(L, tok) for tok in (0, 1)}
    out = []
    for f in fills:
        tok, P, sz, txh = f["tok"], f["P"], f["sz"], f["tx"]
        BP, BS, AP, AS = books[tok]
        # the pmbook print of this tx at P (in tok terms) and its time
        cand = [t for t in T if t[5] == txh]
        hit = [t for t in cand if (t[1] == tok and t[2] == 1 and round(t[3] * 1e4) == P) or (t[1] != tok and t[2] == 0 and round(t[3] * 1e4) == 10000 - P)]
        pr = (hit or cand or [None])[0]
        f["pm_print_x"] = pr[0] if pr else None
        f["pm_print_n"] = len(cand)
        f["pm_print_sz"] = pr[4] if pr else None
        ref = pr[0] if pr else (f["sh_print_x"] if f.get("sh_print_x") is not None else f["cx"])
        # book row where P on tok's bid lost ~ the print size or vanished, within +-300 ms of ref
        lo, hi = np.searchsorted(x, ref - 300, "left"), np.searchsorted(x, ref + 300, "right")
        tb_row, best_d = None, None
        n = pr[4] if pr else sz
        for r in range(max(lo, 1), hi):
            if drop[r] or drop[r - 1]:
                continue
            s0, z0, _ = at_level(BP, BS, r - 1, P)
            if s0 not in ("best", "below"):
                continue
            s1, z1, _ = at_level(BP, BS, r, P)
            lost = z0 - (z1 if s1 in ("best", "below") else 0.0)
            gone = s1 not in ("best", "below", "hidden")
            if gone or (lost > 0 and abs(lost - n) <= 0.1 * n + 0.01) or (lost >= sz - 0.01 and lost > 0):
                d = abs(int(x[r]) - ref)
                if best_d is None or d < best_d:
                    tb_row, best_d = r, d
        f["book_x"] = int(x[tb_row]) if tb_row is not None else None
        tf_ = int(x[tb_row]) if tb_row is not None else ref - 21  # fallback: dsim's median print lag
        ip = (tb_row - 1) if tb_row is not None else int(np.searchsorted(x, tf_, "left")) - 1
        f["matched"] = tb_row is not None
        f["lag_ms"] = int(mk.rr[max(ip, 0)] - x[max(ip, 0)]) if len(x) else None
        if ip < 0:
            out.append(f)
            continue
        st, z, dc = at_level(BP, BS, ip, P)
        f["new_tick"] = None if BP[ip, 0] <= 0 else round((P - int(BP[ip, 0])) / 100)
        f["new_state"], f["sz_at_P"] = st, z
        f["spr_c"] = (int(AP[ip, 0]) - int(BP[ip, 0])) / 100 if AP[ip, 0] > 0 and BP[ip, 0] > 0 else None
        # old alignment: shadow-rec top book at shadow print x - 1
        tp = top.get(e)
        if tp is not None and len(tp) and f.get("sh_print_x") is not None:
            j = int(np.searchsorted(tp["x"], f["sh_print_x"] - 1, "right")) - 1
            if j >= 0 and tp["b"][j] > 0 and tp["k"][j] > 0:
                bb = int(tp["b"][j]) if tok == 0 else 10000 - int(tp["k"][j])
                f["old_tick"] = round((P - bb) / 100)
            # shadow-rec top book just before its own matched change (dsim.match_times logic, +-300 ms around its print)
            sx = f["sh_print_x"]
            tb_, tk_ = tp["b"].astype(np.int64), tp["k"].astype(np.int64)
            lvl = tb_ if tok == 0 else np.where(tk_ > 0, 10000 - tk_, 0)
            siz = tp["bs"] if tok == 0 else tp["ks"]
            l2, h2 = np.searchsorted(tp["x"], sx - 300, "left"), np.searchsorted(tp["x"], sx + 300, "right")
            bestj = None
            for r in range(max(l2, 1), h2):
                if lvl[r - 1] != P or (tb_[r] == 0 and tk_[r] == 0):
                    continue
                if lvl[r] != P or abs((float(siz[r - 1]) - float(siz[r])) - n) <= 0.1 * n + 0.01:
                    if bestj is None or abs(int(tp["x"][r]) - sx) < abs(int(tp["x"][bestj]) - sx):
                        bestj = r
            if bestj is not None:
                f["sh_book_x"] = int(tp["x"][bestj])
                f["sh_tick"] = round((P - int(lvl[bestj - 1])) / 100) if lvl[bestj - 1] > 0 else None
        # history at -5 / -30 / -60 s
        for h in (5, 30, 60):
            j = int(np.searchsorted(x, tf_ - h * 1000, "right")) - 1
            if j < 0 or drop[j]:
                f[f"st{h}"], f[f"z{h}"], f[f"d{h}"] = "none", None, None
                continue
            s_, z_, d_ = at_level(BP, BS, j, P)
            f[f"st{h}"], f[f"z{h}"], f[f"d{h}"] = s_, z_, d_
        # since when P has been best, and the run in which P was never visibly absent
        best_now = BP[ip, 0] == P
        k = ip
        if best_now:
            nb = np.flatnonzero((BP[:ip + 1, 0] != P) | drop[:ip + 1])
            rb = int(nb[-1]) + 1 if len(nb) else 0
            tbest = int(x[rb])
            f["best_ms"] = tf_ - tbest
            f["sz_became_best"] = float(BS[rb, 0])
            v = 0.0
            for t in T[np.searchsorted(tx_, tbest, "left"):np.searchsorted(tx_, ref, "left")]:
                if t[5] == txh:
                    continue
                if (t[1] == tok and t[2] == 1 and round(t[3] * 1e4) == P) or (t[1] != tok and t[2] == 0 and round(t[3] * 1e4) == 10000 - P):
                    v += t[4]
            f["vol_since_best"] = v
            k = rb if tf_ - tbest >= 100 else ip  # P became best inside the same sweep: reference the fill instead
        tref = int(x[k])
        # walk back from row k while P is not visibly absent
        rows = np.arange(k + 1)
        bpk = BP[:k + 1]
        has = (bpk == P).any(1)
        full = (bpk > 0).sum(1) == NL
        lastp = np.where(bpk > 0, bpk, 10**9).min(1)
        hidden = full & (P < lastp)
        abs_ = ~(has | hidden) | drop[:k + 1]
        ab = rows[abs_]
        ra = int(ab[-1]) + 1 if len(ab) else 0
        ta = int(x[ra]) if ra <= k else tref
        f["stretch_ms"] = tref - ta  # lenient: rows where P was hidden below the 5 shown levels count as present
        f["stretch_censored"] = ra == 0
        f["sweep"] = bool(best_now and k == ip)
        seen = np.flatnonzero(has[ra:k + 1])  # rows of the run where P was actually displayed
        tobs = int(x[ra + seen[0]]) if len(seen) else tref
        f["seen_ms"] = tref - tobs
        j30 = int(np.searchsorted(x, tref - 30000, "right")) - 1
        f["deep_vis"] = bool(j30 >= ra and j30 >= 0 and at_level(BP, BS, j30, P)[0] == "below")
        f["cls"] = "DEEP" if tref - tobs >= 30000 else ("NEW" if tref - tobs < 1000 else "OTHER")
        f["t_fill"] = tf_
        f["left_s"] = (end - tf_) / 1000
        for hh in (1000, 5000, 30000):
            t = tf_ + hh
            j = int(np.searchsorted(x, t, "right")) - 1
            ok = t < end - 30000 and j >= 0 and not drop[j] and BP[j, 0] > 0 and AP[j, 0] > BP[j, 0]
            f[f"mo{hh}"] = ((int(BP[j, 0]) + int(AP[j, 0])) / 2 - P) / 100 if ok else None
        if win is not None:
            f["settle_c"] = 100 * ((1.0 if (win == "UP") == (tok == 0) else 0.0) - P / 1e4)
        out.append(f)
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    import csv
    W = {r["slug"]: r["winner"] for r in csv.DictReader(open(Path.home() / "dublin-1124/settle.csv"))}
    for day in sys.argv[1:]:
        a, wh = census(day)
        d0 = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        last_e = d0 + 3600 * (LAST_HOUR.get(day, 23) + 1) - 60 * TF
        sel = (a["e"] >= d0) & (a["e"] < min(d0 + 86400, last_e + 1))
        a, wh = a[sel], wh[sel]
        mk_sel = a["fee"] <= 0
        sp = shadow_prints(day, a["tx"])
        spx = {}
        for t in sp:
            spx.setdefault(bytes(t["tx"]), []).append(t)
        fills = {}
        nsell = 0
        for r, w in zip(a[mk_sel], wh[mk_sel]):
            if int(r["sd"]) != 0:
                nsell += 1
                continue
            tok, P = int(r["tok"]), int(round(float(r["p"]) * 1e4))
            cands = spx.get(bytes(r["tx"]), [])
            m = [t for t in cands if (int(t["tok"]) == tok and int(t["sd"]) == 1 and int(t["p"]) == P) or (int(t["tok"]) != tok and int(t["sd"]) == 0 and int(t["p"]) == 10000 - P)]
            pr = (m or cands or [None])[0]
            f = dict(w=WAL[w], day=day, e=int(r["e"]), tok=tok, P=P, sz=float(r["sz"]), tx="0x" + bytes(r["tx"]).hex(),
                     cx=int(r["x"]), cts=int(r["ts"]), sh_print_x=int(pr["x"]) if pr is not None else None,
                     sh_print_sz=float(pr["sz"]) if pr is not None else None, sh_print_exact=bool(m),
                     sh_print_szeq=bool(pr is not None and abs(float(pr["sz"]) - float(r["sz"])) <= 0.01))
            fills.setdefault(int(r["e"]), []).append(f)
        top = shadow_top(day, np.array(sorted(fills), dtype=np.uint32))
        rows, open_ = [], {}
        for dd, h in hours_for(day):
            p = FR / dd / f"pmbook-{h:02d}.jsonl.gz"
            if p.exists():
                for line in lines15(p):
                    if True:
                        dct = orjson.loads(line)
                        e = int(dct["slug"].rsplit("-", 1)[1])
                        if e not in fills:
                            continue
                        open_.setdefault(e, Mk()).add(dct)
            hend = int(datetime.fromisoformat(dd).replace(tzinfo=timezone.utc).timestamp()) + 3600 * (h + 1)
            for e in [e for e in open_ if e + 60 * TF + 3600 <= hend]:  # rows are filed by receive time: wait one more file
                rows += process(e, open_.pop(e), fills[e], top, W.get(pmlat.slug(("btc", TF, e))))
            print(day, dd, h, len(rows), flush=True)
        for e in list(open_):
            rows += process(e, open_.pop(e), fills[e], top, W.get(pmlat.slug(("btc", TF, e))))
        done = {r["e"] for r in rows}
        for e, fl in fills.items():  # windows with no pmbook rows at all
            if e not in done:
                rows += [dict(f, nobook=True) for f in fl]
        D = pd.DataFrame(rows)
        with gzip.open(OUT / f"lvl-{TF}-{day}.csv.gz", "wt") as fo:
            D.to_csv(fo, index=False)
        print("wrote", day, len(D), "maker sells skipped", nsell, flush=True)


if __name__ == "__main__":
    main()
