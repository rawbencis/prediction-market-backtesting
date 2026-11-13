"""Match a wallet's Polymarket fills (default OhioAI) to the shadow recorder's trade prints by transaction hash,
then time them against Binance to the millisecond.

Needs recordings made with trade capture (shadow_rec.py tr-*.bin, 2026-11-14 onwards).
Per matched fill: exchange match time, side, price, taker/maker (fee in usdc_size), the UP book just before,
the Binance spot path toward the side bought over the 10 s before the match (by Binance trade time),
and when our momentum rule (M=2 bp, WIN=5 s, recorder-received spot) would have fired and landed (tokyo).
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import shadow_eval as se
import shadow_rec as rec

WALLET = "0x129246beaac356e104c397f0985c11b7469be5a6"
TR_DT = np.dtype([("l", "<i8"), ("x", "<i8"), ("a", "u1"), ("tf", "u1"), ("e", "<u4"), ("tok", "u1"), ("sd", "u1"),
                  ("p", "<u2"), ("sz", "<f4"), ("fee", "<u2"), ("tx", "S32")])  # fmt: skip
assert TR_DT.itemsize == rec.TR_REC.size
DELTAS = (0, 50, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000)


def activity(wallet, start, end):
    rows, cur = [], None
    while True:
        p = dict(user=wallet, limit=1000, start=start, end=end, type="TRADE", sort_direction="ASC")
        if cur:
            p["cursor"] = cur
        req = urllib.request.Request("https://data-api.polymarket.com/v2/activity?" + urllib.parse.urlencode(p),
                                     headers={"User-Agent": "research-ohioai-match"})  # fmt: skip
        with urllib.request.urlopen(req, timeout=30) as r:
            x = json.load(r)
        rows += x.get("data", [])
        cur = (x.get("pagination") or {}).get("next_cursor")
        if not cur:
            return rows
        time.sleep(0.25)


def match_day(root, day, acts, a):
    tr = se.read(root, day, "tr-", TR_DT)
    pm = se.read(root, day, "pm-", se.PM_DT)
    prev = (date.fromisoformat(day) - timedelta(1)).isoformat()
    bn = np.concatenate([se.read(root, prev, "bn-23", se.BN_DT), se.read(root, day, "bn-", se.BN_DT)])
    if not len(tr):
        return []
    live = (pm["x"] > 0) & (pm["k"] > 0)
    d10 = float(np.percentile(pm["l"][live] / 1000 - pm["x"][live], 10)) if live.any() else 0.0
    by_tx = {}
    for j, h in enumerate(tr["tx"]):
        by_tx.setdefault(h, []).append(j)
    out = []
    for r in acts:
        if r.get("side") != "BUY":
            continue
        h = bytes.fromhex(r["transaction_hash"][2:])[:32].ljust(32, b"\0")
        tok = 0 if (r.get("outcome") or "").upper() == "UP" else 1
        js = [j for j in by_tx.get(h, []) if tr["tok"][j] == tok] or by_tx.get(h, [])
        if not js:
            out.append(dict(matched=0, ts=r["timestamp"], slug=r.get("slug")))
            continue
        j = min(js, key=lambda j: tr["x"][j])
        x, ai, tf, ep = int(tr["x"][j]), int(tr["a"][j]), int(tr["tf"][j]), int(tr["e"][j])
        sz, pr, usd = float(r["size"]), float(r["price"]), float(r.get("usdc_size") or 0)
        sgn = 1 if tok == 0 else -1
        row = dict(matched=1, slug=r.get("slug"), asset=rec.ASSETS[ai], tf=tf, epoch=ep, x_ms=x, t_s=x / 1000 - ep, side=tok,
                   px=pr, size=sz, taker=int(usd - sz * pr > 0.5 * 0.07 * pr * (1 - pr) * sz), block_lag_s=int(r["timestamp"]) - x / 1000,
                   tr_side=int(tr["sd"][j]), rx_delay_ms=tr["l"][j] / 1000 - x)  # fmt: skip
        # book just before the match (exchange time)
        m = (pm["a"] == ai) & (pm["tf"] == tf) & (pm["e"] == ep)
        P = pm[m]
        P = P[np.argsort(P["x"], kind="stable")]
        i = np.searchsorted(P["x"], x, "left") - 1
        if i >= 0 and P["k"][i] > 0:
            ask = P["k"][i] / 1e4 if tok == 0 else 1 - P["b"][i] / 1e4
            row.update(ask_before=ask, px_minus_ask=pr - ask, book_age_ms=x - int(P["x"][i]))
        # Binance spot path toward the side, by Binance trade time
        b = bn[(bn["src"] == 0) & (bn["a"] == ai)]
        if len(b) > 10:
            o = np.argsort(b["T"], kind="stable")
            T, Pp, Lr = b["T"][o], b["p"][o], b["l"][o]

            def s_at(ms):
                k = np.searchsorted(T, ms, "right") - 1
                return Pp[max(k, 0)]

            ref = s_at(x - 10_000)
            for d in DELTAS:
                row[f"mv{d}"] = sgn * 1e4 * np.log(s_at(x - d) / ref)
            # our rule: first time the recorder had RECEIVED a >= M bp 5 s move toward the side (10 s before .. 3 s after)
            o2 = np.argsort(b["l"], kind="stable")
            Ls, Ps = b["l"][o2], b["p"][o2]
            k0, k1 = np.searchsorted(Ls, (x - 10_000) * 1000), np.searchsorted(Ls, (x + 3000) * 1000)
            if k1 > k0:
                kb = np.searchsorted(Ls, Ls[k0:k1] - 5_000_000, "right") - 1
                okb = kb >= 0
                mv = np.where(okb, sgn * 1e4 * np.log(Ps[k0:k1] / Ps[np.maximum(kb, 0)]), 0.0)
                hit = np.flatnonzero(okb & (mv >= a.M))
                if len(hit):
                    fire = Ls[k0 + hit[0]] / 1000
                    row["our_fire_minus_match_ms"] = fire - x
                    row["our_land_minus_match_ms"] = fire + d10 + a.proc_ms + a.taker_delay_ms - x
        out.append(row)
    return out


def q(v, ps=(10, 25, 50, 75, 90)):
    v = np.asarray([x for x in v if x == x], float)
    return [round(float(np.percentile(v, p)), 1) for p in ps] if len(v) else []


def cmd_run(a):
    days = a.days or sorted(p.name for p in a.rec.glob("????-??-??") if p.is_dir() and any(p.glob("tr-*.bin")))
    rows = []
    for d in days:
        st = int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())
        acts = [r for r in activity(a.wallet, st, st + 86400 + 60)]
        got = match_day(a.rec, d, acts, a)
        print(d, f"buys {sum(1 for r in acts if r.get('side') == 'BUY')}, matched {sum(r['matched'] for r in got)}", flush=True)
        rows += got
    D = pd.DataFrame(rows)
    a.out.mkdir(parents=True, exist_ok=True)
    D.to_csv(a.out / "ohioai_fills_ms.csv", index=False)
    M = D[D.matched == 1] if len(D) else D
    rep = dict(fills=len(D), matched=int(M.shape[0]))
    if len(M):
        T = M[M.taker == 1]
        rep["taker_fills"] = int(len(T))
        rep["block_lag_s_q"] = q(M.block_lag_s)
        rep["px_minus_ask_before_c_q"] = q(100 * T.px_minus_ask) if "px_minus_ask" in T else []
        rep["book_age_ms_q"] = q(T.book_age_ms) if "book_age_ms" in T else []
        mv = [c for c in M.columns if c.startswith("mv")]
        if mv:
            big = T[T.mv0 >= 2]
            rep["mean_move_toward_side_bps_by_ms_before_match (taker)"] = {c[2:]: round(float(T[c].mean()), 2) for c in mv}
            rep["same, fills with >= 2 bp move in 10 s (n=%d)" % len(big)] = {c[2:]: round(float(big[c].mean()), 2) for c in mv}
            if len(big):
                half = []
                for _, r in big.iterrows():
                    tot = r["mv0"]
                    dd = [d for d in DELTAS if r[f"mv{d}"] <= 0.5 * tot]
                    half.append(min(dd) if dd else np.nan)
                rep["ms_since_half_of_move_q"] = q(half)
        if "our_land_minus_match_ms" in T:
            v = T.our_land_minus_match_ms
            rep["our_rule_fired_share"] = round(float(v.notna().mean()), 3)
            rep["our_land_minus_their_match_ms_q"] = q(v)
            rep["share_we_land_first"] = round(float((v < 0).mean()), 3) if v.notna().any() else None
    (a.out / "ohioai_ms.json").write_text(json.dumps(rep, indent=1, default=float))
    print(json.dumps(rep, indent=1, default=float))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rec", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path.home() / "ohioai")
    ap.add_argument("--wallet", default=WALLET)
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--M", type=float, default=2.0)
    ap.add_argument("--proc-ms", type=float, default=10.0)
    ap.add_argument("--taker-delay-ms", type=float, default=50.0)
    cmd_run(ap.parse_args())


if __name__ == "__main__":
    main()
