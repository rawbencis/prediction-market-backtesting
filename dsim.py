"""dsim: event-replay simulator for Polymarket up/down windows, in exchange time (ms).
Book = recorded UP top of book (shadow_rec pm rows), prints = recorded trade prints (tr rows). Everything is in UP terms:
a BUY of DOWN at p rests on / hits the UP ask / bid at 1 - p. Prices are ints in 1e-4 $.
  taker(): FAK at match time T (= arrival + hold) against the book strictly before T, minus same-ms prints of other takers.
  maker(): post-only; queue ahead from the displayed size; prints / cancels advance it per model pess|prop|front.
CLI: winners | v2 | v3 (validation against OhioAI fills). Tests: test_dsim.py.
"""
import argparse, csv, json, sys, time, urllib.request
from pathlib import Path

import numpy as np

HOME = Path.home()
REC, OUT = HOME / "shadow-rec", HOME / "dublin-retest"
WINNERS = OUT / "winners.csv"
ASSETS = ("btc", "eth", "sol", "xrp", "doge")
FEE_RATE, REBATE_SHARE, HOLD_MS, MIN_SHOW = 0.07, 0.2, 50, 5.0
# trade prints are stamped later than the book change they cause. Default = smallest book offset before the print at
# which >= 90% of OhioAI's 2026-11-14 taker fills are hittable (V2 sweep: 0:44.5% 5:46.5% 10:50.6% 15:57.5% 20:72.8%
# 30:89.6% 50:93.6%). Prints are moved this much earlier (maker fills beat a cancel more often: conservative).
PRINT_SHIFT_MS = 50
OFFSETS = (0, 5, 10, 15, 20, 30, 50)
MARKOUTS = (100, 1000, 5000, 30000)
PM_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"),
                  ("b", "<u2"), ("k", "<u2"), ("bs", "<f4"), ("ks", "<f4")])  # fmt: skip
BN_DT = np.dtype([("l", "<i8"), ("T", "<i8"), ("src", "u1"), ("a", "u1"), ("p", "<f8")])
TR_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("tok", "u1"), ("sd", "u1"),
                  ("p", "<u2"), ("sz", "<f4"), ("fee", "<u2"), ("tx", "S32")])  # fmt: skip

# ---------------------------------------------------------------- scenarios


def scenario(name, pm=None, bn=None, probe=None):
    """Latency scenario (ms). book: exchange event -> strategy; bn: Binance trade -> strategy; order/cancel: strategy ->
    exchange (order arrival, before the taker hold); hold: taker hold. dublin uses the day's measured medians."""
    if name == "colo":  # London colo: Binance still ~100 ms away (assumption: same as Dublin's measured leg)
        return dict(book=1.0, bn=_bn_delay(bn), order=1.0, cancel=1.0, hold=HOLD_MS)
    if name == "tokyo":  # Binance leg from Tokyo ~2 ms (assumption)
        return dict(book=116.0, bn=2.0, order=120.0, cancel=120.0, hold=HOLD_MS)
    if name == "dublin":
        pr = probe_p50(probe)
        return dict(book=_pm_delay(pm), bn=_bn_delay(bn), order=pr["send_to_ex_visible"], cancel=pr["cxl_to_ex_gone"], hold=HOLD_MS)
    raise ValueError(name)


def _pm_delay(pm):
    if pm is None or not len(pm):
        return 9.0
    live = (pm["x"] > 0) & (pm["k"] > 0)
    return float(np.median(pm["l"][live] / 1000 - pm["x"][live]))


def _bn_delay(bn):
    if bn is None or not len(bn):
        return 109.0
    s = bn["src"] == 0
    return float(np.median(bn["l"][s] / 1000 - bn["T"][s]))


def probe_p50(path=None):
    rows = list(csv.DictReader(open(path or HOME / "order-probe.csv")))
    f = lambda k: float(np.median([float(r[k]) for r in rows if r[k] not in ("", None)]))  # noqa: E731
    return {k: f(k) for k in ("send_to_ex_visible", "cxl_to_ex_gone")}


def taker_match_time(t_decide, sc):
    return t_decide + sc["order"] + sc["hold"]


def maker_times(t_decide, t_cancel_decide, sc):
    return t_decide + sc["order"], t_cancel_decide + sc["cancel"]


# ---------------------------------------------------------------- data


def read(root, day, kind, dt):
    parts = [np.fromfile(f, dtype=dt) for f in sorted((Path(root) / day).glob(f"{kind}-*.bin"))]
    return np.concatenate(parts) if parts else np.zeros(0, dt)


def mk_market(book, prints, shift=None):
    """book: rows with x, b, k, bs, ks (one market); prints: rows with x, tok, sd, p, sz, tx -> market dict."""
    o = np.argsort(book["x"], kind="stable")
    B = {c: np.asarray(book[c])[o] for c in ("x", "b", "k", "bs", "ks")}
    o = np.argsort(prints["x"], kind="stable")
    tok, sd, p = (np.asarray(prints[c])[o].astype(np.int64) for c in ("tok", "sd", "p"))
    sh = PRINT_SHIFT_MS if shift is None else shift
    P = dict(x=np.asarray(prints["x"])[o].astype(np.int64) - sh, sz=np.asarray(prints["sz"])[o].astype(float),
             hb=((tok == 0) & (sd == 1)) | ((tok == 1) & (sd == 0)),  # True: taker sold UP (hit the UP bid)
             up=np.where(tok == 0, p, 10000 - p), tx=np.asarray(prints["tx"])[o] if "tx" in prints.dtype.names else None)
    return dict(book=B, pr=P)


def load_day(day, root=REC, shift=None):
    """-> ({(asset, tf, epoch): market}, pm, bn) for one recorded day."""
    pm, tr, bn = read(root, day, "pm", PM_DT), read(root, day, "tr", TR_DT), read(root, day, "bn", BN_DT)
    mk = {}
    for name, arr in (("pm", pm), ("tr", tr)):
        key = arr["a"].astype(np.int64) * 10**12 + arr["tf"].astype(np.int64) * 10**10 + arr["e"]
        o = np.argsort(key, kind="stable")
        ks, starts = np.unique(key[o], return_index=True)
        for k, s, t in zip(ks, starts, list(starts[1:]) + [len(o)]):
            mk.setdefault((ASSETS[k // 10**12], int(k // 10**10 % 100), int(k % 10**10)), {})[name] = arr[o[s:t]]
    out = {}
    for k, d in mk.items():
        if "pm" in d:
            out[k] = mk_market(d["pm"], d.get("tr", np.zeros(0, TR_DT)), shift)
    return out, pm, bn


# ---------------------------------------------------------------- book helpers


def state(m, t, strict=True):
    """Book row index in force at t (strict: events with x < t only). -1 if none."""
    return int(np.searchsorted(m["book"]["x"], t, "left" if strict else "right")) - 1


def mid(m, t, tok=0):
    B, i = m["book"], state(m, t, strict=False)
    if i < 0 or B["b"][i] == 0 or B["k"][i] == 0:
        return None
    u = (int(B["b"][i]) + int(B["k"][i])) / 2
    return u if tok == 0 else 10000 - u


def fee(px, size):
    p = px / 1e4
    return FEE_RATE * p * (1 - p) * size


# ---------------------------------------------------------------- taker


def taker(m, t_match, tok, limit, size=5.0, min_show=MIN_SHOW, exclude_tx=None, sell=False):
    """BUY `tok` (0 UP / 1 DOWN) FAK with limit (1e-4) matching at t_match. Top level only.
    -> fill dict or None. Same-ms ties: other takers go first (book row at t_match used if present, else the book
    before t_match minus same-ms prints on that level). exclude_tx (validation only): book strictly before, minus
    same-ms prints other than that tx."""
    B, i = m["book"], state(m, t_match)
    i2 = state(m, t_match, strict=False)
    same_ms = exclude_tx is None and i2 > i  # a book row at the match ms: other takers/cancels went first, use it as is
    if same_ms:
        i = i2
    if i < 0 or (B["b"][i] == 0 and B["k"][i] == 0):  # nothing recorded yet / dropped connection
        return None
    if (tok == 0) != sell:  # BUY UP / SELL DOWN lift the UP ask
        lvl, shown, hit_bid = int(B["k"][i]), float(B["ks"][i]), False
    else:  # BUY DOWN / SELL UP hit the UP bid
        lvl, shown, hit_bid = int(B["b"][i]), float(B["bs"][i]), True
    px = lvl if tok == 0 else 10000 - lvl
    if lvl == 0 or (px < limit if sell else px > limit):
        return None
    P = m["pr"]
    j0, j1 = np.searchsorted(P["x"], t_match, "left"), np.searchsorted(P["x"], t_match, "right")
    ex = None if exclude_tx is None else bytes(exclude_tx).rstrip(b"\0")
    for j in range(0 if same_ms else j0, j1 if not same_ms else 0):
        if P["hb"][j] == hit_bid and P["up"][j] == lvl and not (ex is not None and bytes(P["tx"][j]).rstrip(b"\0") == ex):
            shown -= P["sz"][j]
    if shown < min_show - 1e-9:
        return None
    q = min(size, shown)
    return dict(x=t_match, tok=tok, px=px, size=q, fee=fee(px, q), rebate=0.0, maker=0, ahead=0.0, book_age=t_match - int(B["x"][i]),
                sell=int(sell))


# ---------------------------------------------------------------- maker


def maker(m, t_arr, tok, price, size, t_cxl, model="pess"):
    """Post-only BUY `tok` at price (1e-4) arriving t_arr, cancel arriving t_cxl (not held).
    -> dict(status, fills=[...]) ; status 'rejected' if it would cross on arrival."""
    s, q = (0, price) if tok == 0 else (1, 10000 - price)
    better = (lambda a, c: a > c) if s == 0 else (lambda a, c: a < c)
    B, P = m["book"], m["pr"]
    i = state(m, t_arr, strict=False)
    if i < 0 or (B["b"][i] == 0 and B["k"][i] == 0):
        return dict(status="nobook", fills=[])
    best, opp = (int(B["b"][i]), int(B["k"][i])) if s == 0 else (int(B["k"][i]), int(B["b"][i]))
    if opp and (q >= opp if s == 0 else q <= opp):
        return dict(status="rejected", fills=[])
    if best == 0 or better(q, best):
        ahead, disp = 0.0, 0.0
    elif q == best:
        disp = float(B["bs"][i] if s == 0 else B["ks"][i])
        ahead = disp
    else:
        ahead, disp = None, None  # behind the top: unknown until our level becomes top
    if model == "front":
        ahead = 0.0
    left, fills, since = size, [], 0.0

    def fill(x, n, ah):
        nonlocal left
        n = min(left, n)
        if n > 1e-9:
            left -= n
            fills.append(dict(x=int(x), tok=tok, px=price, size=n, fee=0.0, rebate=REBATE_SHARE * fee(price, n), maker=1,
                              ahead=float(ah if ah is not None else -1)))

    jb = i + 1
    jp = int(np.searchsorted(P["x"], t_arr, "right"))
    nb, npr = len(B["x"]), len(P["x"])
    while left > 1e-9:
        tb = B["x"][jb] if jb < nb and B["x"][jb] < t_cxl else None  # book rows strictly before the cancel
        tp = P["x"][jp] if jp < npr and P["x"][jp] <= t_cxl else None  # prints up to and incl. the cancel ms
        if tb is None and tp is None:
            break
        if tp is not None and (tb is None or tp <= tb):  # print first on ties: the book row is the post-trade state
            j = jp
            jp += 1
            if bool(P["hb"][j]) != (s == 0):
                continue
            px, n = int(P["up"][j]), float(P["sz"][j])
            if better(q, px):  # through our price: everything at our level, us included, was taken
                fill(P["x"][j], left, ahead)
            elif px == q:
                since += n
                if ahead is None:
                    ahead = 0.0 if model == "front" else (disp if disp is not None else None)
                if ahead is None:
                    continue
                a0 = ahead
                d = min(ahead, n)
                ahead -= d
                fill(P["x"][j], n - d, a0)
            continue
        j = jb
        jb += 1
        if B["b"][j] == 0 and B["k"][j] == 0:  # dropped connection: book unknown, keep state
            disp, since = None, 0.0
            continue
        nbest, nopp = (int(B["b"][j]), int(B["k"][j])) if s == 0 else (int(B["k"][j]), int(B["b"][j]))
        nsz = float(B["bs"][j] if s == 0 else B["ks"][j])
        if nopp and (q >= nopp if s == 0 else q <= nopp):  # a resting order crossed our synthetic order: it would have hit us
            oz = float(B["ks"][j] if s == 0 else B["bs"][j])
            fill(B["x"][j], oz - (ahead or 0.0), ahead)
        if nbest == 0 or better(q, nbest):  # our level emptied (or we are alone at the best)
            ahead, disp = 0.0, 0.0
        elif nbest == q:
            if model == "front":
                ahead = 0.0
            elif ahead is None:
                ahead = nsz
            elif disp is not None and model == "prop":
                cxl = disp - nsz - since
                D = disp - since
                if cxl > 0 and D > 0:
                    ahead = max(0.0, ahead - cxl * ahead / D)
            ahead = min(ahead, nsz)
            disp = nsz
        else:
            disp = None  # a better level exists; our level not visible
        since = 0.0
    return dict(status="ok", fills=fills)


# ---------------------------------------------------------------- money


def markouts(m, f):
    return {f"mo{dt}": (None if mid(m, f["x"] + dt, f["tok"]) is None else (mid(m, f["x"] + dt, f["tok"]) - f["px"]) / 1e4 * f["size"])
            for dt in MARKOUTS}


def settle(f, winner):
    """Settlement PnL in $ of a BUY fill: winner 'UP' / 'DOWN' (None -> None). Rebate kept separate."""
    if winner is None:
        return None
    pay = 1.0 if (winner.upper() == "UP") == (f["tok"] == 0) else 0.0
    return f["size"] * (pay - f["px"] / 1e4) - f["fee"]


def winners():
    if not WINNERS.exists():
        return {}
    return {r["slug"]: r["winner"] for r in csv.DictReader(open(WINNERS))}


def fetch_winners(slugs):
    have = winners()
    todo = [s for s in slugs if s not in have]
    new = not WINNERS.exists()
    with open(WINNERS, "a") as f:
        if new:
            f.write("slug,winner\n")
        for i in range(0, len(todo), 20):
            q = "&".join(f"slug={s}" for s in todo[i:i + 20])
            req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets?closed=true&{q}", headers={"User-Agent": "dsim"})
            for mk in json.load(urllib.request.urlopen(req, timeout=20)):
                try:
                    pr, names = json.loads(mk["outcomePrices"]), json.loads(mk["outcomes"])
                except (KeyError, TypeError, ValueError):
                    continue
                if mk.get("closed") and "1" in pr:
                    f.write(f"{mk['slug']},{names[pr.index('1')].upper()}\n")
            time.sleep(0.2)
    return winners()


def slug(k):
    return f"{k[0]}-updown-{k[1]}m-{k[2]}"


# ---------------------------------------------------------------- validation vs OhioAI


def ohio_fills(day, taker_flag):
    import pandas as pd

    d = pd.read_csv(HOME / "ohioai" / "ohioai_fills_ms.csv")
    d = d[(d.matched == 1) & (d.taker == taker_flag)].copy()
    d["day"] = pd.to_datetime(d.x_ms, unit="ms").dt.strftime("%Y-%m-%d")
    return d[d.day == day]


def cmd_v2(a):
    mk, _, _ = load_day(a.day, shift=0)
    d = ohio_fills(a.day, 1)
    res = []
    for r in d.itertuples():
        m = mk.get((r.asset, int(r.tf), int(r.epoch)))
        tok, lim, x = int(r.side), int(round(r.px * 1e4)), int(r.x_ms)
        row = dict(x=x, slug=r.slug, tok=tok, px=lim, size=r.size)
        if m is None:
            res.append(dict(row, why="no market"))
            continue
        P = m["pr"]  # their own print: same ms, their token's price level, closest size (fills csv has no tx column)
        up = lim if tok == 0 else 10000 - lim
        js = [j for j in range(np.searchsorted(P["x"], x, "left"), np.searchsorted(P["x"], x, "right"))
              if P["hb"][j] == (tok == 1) and P["up"][j] == up]
        tx = bytes(P["tx"][min(js, key=lambda j: abs(P["sz"][j] - r.size))]) if js else b"\xff" * 32
        f_any = taker(m, x, tok, lim, size=r.size, min_show=1e-6, exclude_tx=tx)
        f_5 = taker(m, x, tok, lim, size=5, min_show=min(5.0, r.size), exclude_tx=tx)
        B, i = m["book"], state(m, x)
        lvl = None if i < 0 else (int(B["k"][i]) if tok == 0 else 10000 - int(B["b"][i]))
        why = "hit" if f_any else ("nobook" if i < 0 else "price" if lvl is None or lvl > lim or lvl in (0, 10000) else "size")
        # does the book at match + / - 50 ms or after same-ms events show it? (timing diagnosis)
        # book offset o before the print: FAK matched at x - o (strict book), their own print excluded
        alt = {f"off{o}": bool(taker(m, x - o, tok, lim, size=r.size, min_show=1e-6, exclude_tx=tx)) for o in OFFSETS}
        res.append(dict(row, hit=bool(f_any), hit5=bool(f_5), exact=bool(f_any and f_any["px"] == lim), why=why, book_px=lvl,
                        book_age=(x - int(B["x"][i])) if i >= 0 else None, **alt))
    import pandas as pd

    R = pd.DataFrame(res)
    R.to_csv(OUT / "out" / f"v2-{a.day}.csv", index=False)
    n = len(R)
    print(f"V2 {a.day}: OhioAI taker fills matched to prints n={n}")
    print(f"  FAK at exact match time, limit = their price: fills {R.hit.mean():.1%} (exact same price {R.exact.mean():.1%}),"
          f" with >= min(5, their size) shown {R.hit5.mean():.1%}")
    print("  miss reasons:", R.why.value_counts().to_dict())
    hits = {o: float(R[f"off{o}"].mean()) for o in OFFSETS}
    print("  V2 hit rate by book offset before the print (ms):", {o: round(h, 3) for o, h in hits.items()})
    ok = [o for o in OFFSETS if hits[o] >= 0.9]
    print(f"  smallest offset with >= 90% hits: {ok[0] if ok else None} ms (PRINT_SHIFT_MS is {PRINT_SHIFT_MS})")
    miss = R[~R.hit.astype(bool)]
    if len(miss):
        print("  misses: book_px - their px (c) quantiles", np.percentile((miss.book_px.dropna() - miss.px[miss.book_px.notna()]) / 100, [10, 50, 90]).round(2) if miss.book_px.notna().any() else None,
              " book_age ms q", np.percentile(miss.book_age.dropna(), [10, 50, 90]).round(0) if miss.book_age.notna().any() else None)
        print(miss.head(12).to_string())


def cmd_v3(a):
    mk, _, _ = load_day(a.day, shift=0)
    d = ohio_fills(a.day, 0)
    res = []
    for r in d.itertuples():
        m = mk.get((r.asset, int(r.tf), int(r.epoch)))
        if m is None:
            continue
        tok, px, x = int(r.side), int(round(r.px * 1e4)), int(r.x_ms)
        row = dict(x=x, slug=r.slug, tok=tok, px=px, size=r.size)
        B = m["book"]  # arrive at x-1s, or at the first later book state (before x) where post-only is accepted
        cands = [x - 1000] + [int(t) + 1 for t in B["x"][(B["x"] >= x - 1000) & (B["x"] < x - 1)]]
        for ta in cands:
            if maker(m, ta, tok, px, 5.0, ta, "front")["status"] == "ok":
                break
        row["arr_before_ms"] = x - ta
        for model in ("pess", "prop", "front"):
            o = maker(m, ta, tok, px, min(5.0, r.size), x + 1000, model)
            row[model] = o["status"] == "ok" and sum(f["size"] for f in o["fills"]) > 0
            row["status"] = o["status"]
        res.append(row)
    import pandas as pd

    R = pd.DataFrame(res)
    R.to_csv(OUT / "out" / f"v3-{a.day}.csv", index=False)
    print(f"V3 {a.day}: OhioAI maker fills n={len(R)}; our post-only at their price, arrive x-1s (or the first later book"
          f" state where it would not cross), cancel x+1s. arrival ms before x q10/50/90:",
          np.percentile(R.arr_before_ms, [10, 50, 90]).round(0))
    print("  status:", R.status.value_counts().to_dict())
    ok = R[R.status == "ok"]
    print("  filled share (all):", {k: round(float(R[k].mean()), 3) for k in ("pess", "prop", "front")},
          " (not rejected):", {k: round(float(ok[k].mean()), 3) for k in ("pess", "prop", "front")})


def cmd_winners(a):
    mk, _, _ = load_day(a.day)
    w = fetch_winners(sorted(slug(k) for k in mk))
    print(f"{sum(slug(k) in w for k in mk)}/{len(mk)} winners cached in {WINNERS}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("winners", "v2", "v3"))
    ap.add_argument("--day", default="2026-11-14")
    a = ap.parse_args()
    if a.day != "2026-11-14" and a.cmd in ("v2", "v3"):
        sys.exit("validation is on 2026-11-14 only")
    {"winners": cmd_winners, "v2": cmd_v2, "v3": cmd_v3}[a.cmd](a)
