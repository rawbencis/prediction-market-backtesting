"""Settlement winners for {btc,eth,sol,xrp,doge} x {5m,15m} windows starting 2026-11-14..23 (Gamma, <= 2 req/s),
plus the live feeSchedule of one current market of each type. Public data only."""
import csv, json, sys, time, urllib.request
from pathlib import Path
OUT = Path.home() / "dublin-1124/settle.csv"
have = {}
for p in (Path.home() / "act-settlements.csv", OUT):
    if p.exists():
        have.update({r["slug"]: r["winner"] for r in csv.DictReader(p.open())})
t0, t1 = 1794614400, 1795478400  # 2026-11-14 00:00 .. 2026-11-24 00:00 UTC
now = time.time()
slugs = [f"{a}-updown-{tf}m-{e}" for a in ("btc", "eth", "sol", "xrp", "doge") for tf in (5, 15)
         for e in range(t0, t1, 60 * tf) if e + 60 * tf + 120 < now]
todo = [s for s in slugs if s not in have]
print("slugs", len(slugs), "todo", len(todo), flush=True)
def get(url):
    time.sleep(0.6)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research-client"}), timeout=30) as r:
        return json.load(r)
for n in range(0, len(todo), 50):
    try:
        rows = get("https://gamma-api.polymarket.com/markets?closed=true&limit=500&" + "&".join(f"slug={s}" for s in todo[n:n + 50]))
    except Exception as e:
        print("fail", n, repr(e), flush=True); continue
    for m in rows:
        try:
            names = [x.upper() for x in json.loads(m["outcomes"])]; pr = [float(v) for v in json.loads(m["outcomePrices"])]
        except Exception:
            continue
        if m.get("closed") and sorted(names) == ["DOWN", "UP"] and max(pr) >= 0.99 and min(pr) <= 0.01:
            have[m["slug"]] = names[pr.index(max(pr))]
with OUT.open("w", newline="") as f:
    w = csv.writer(f); w.writerow(["slug", "winner"]); w.writerows(sorted(have.items()))
print("have", sum(s in have for s in slugs), "of", len(slugs))
for a in ("btc", "eth", "sol", "xrp", "doge"):
    for tf in (5, 15):
        e = int(now // (60 * tf)) * 60 * tf
        try:
            m = get(f"https://gamma-api.polymarket.com/markets?slug={a}-updown-{tf}m-{e}")[0]
            print(a, tf, e, "feeSchedule", json.dumps(m.get("feeSchedule")), "feesEnabled", m.get("feesEnabled"),
                  "makerBaseFee", m.get("makerBaseFee"), "takerBaseFee", m.get("takerBaseFee"), flush=True)
        except Exception as ex:
            print(a, tf, "fee fetch failed", repr(ex))
