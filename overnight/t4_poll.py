"""T4.3: poll public CLOB books of the live BTC/ETH 1h, 4h and daily up/down markets every 10 s.

Public reads only (Gamma market lookup on rollover, CLOB /book). At most 1 request/s in total.
Writes one JSON line per book read to OUT (gzip). Stops at --until (UTC epoch seconds).
Window anchors (from Gamma slugs): 1h on the hour, 4h at 01/05/09/13/17/21 UTC, 1d at 17:00 UTC (noon ET).
"""

import argparse
import gzip
import json
import time
import urllib.request

UA = {"User-Agent": "research"}
TFS = (("1h", 3600, 0), ("4h", 14400, 3600), ("1d", 86400, 61200))
last_req = [0.0]


def get(url):
    wait = 1.0 - (time.time() - last_req[0])
    if wait > 0:
        time.sleep(wait)
    last_req[0] = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--until", type=int, required=True)
    a = ap.parse_args()
    tokens = {}  # (asset, tf, e) -> (up_token, end_iso, fee)
    with gzip.open(a.out, "at") as f:
        while time.time() < a.until:
            t0 = time.time()
            for asset in ("btc", "eth"):
                for tf, step, off in TFS:
                    e = int((t0 - off) // step * step + off)
                    k = (asset, tf, e)
                    if k not in tokens:
                        try:
                            m = get(f"https://gamma-api.polymarket.com/markets?slug={asset}-updown-{tf}-{e}")
                            m = m[0] if m else None
                            if m:
                                names = [n.upper() for n in json.loads(m["outcomes"])]
                                toks = json.loads(m["clobTokenIds"])
                                tokens[k] = (toks[names.index("UP")], m["endDate"], (m.get("feeSchedule") or {}).get("rate"))
                            else:
                                tokens[k] = None
                        except Exception as ex:  # noqa: BLE001
                            print("gamma", k, ex, flush=True)
                            continue
                    if not tokens.get(k):
                        continue
                    tok, end, fee = tokens[k]
                    try:
                        b = get(f"https://clob.polymarket.com/book?token_id={tok}")
                    except Exception as ex:  # noqa: BLE001
                        print("book", k, ex, flush=True)
                        continue
                    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)[:10]
                    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))[:10]
                    f.write(json.dumps(dict(t=round(time.time(), 3), a=asset, tf=tf, e=e, end=end, fee=fee, ts=b.get("timestamp"),
                                            h=b.get("hash"), ltp=b.get("last_trade_price"), tick=b.get("tick_size"),
                                            bids=bids, asks=asks)) + "\n")
            f.flush()
            dt = 10 - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)


if __name__ == "__main__":
    main()
