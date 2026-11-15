# R-D3 — registered test (file written 2026-11-15T14:12:43Z)

R-D3 (frozen 2026-11-15 ~09:15 UTC; count from 2026-11-15 00:00 UTC). Signal: Binance BTCUSDT USD-M aggTrade completes
a move >= 2 bp vs the price 500 ms earlier; 2 s debounce. Side: the side the move favours. Market: the live BTC 5m
window with 30–300 s left; seen ask of that side 0.10–0.90. Order: FAK buy 5 shares, limit = seen ask + 1c; several
entries per market allowed. Timing (dsim dublin): decide at the signal's receipt on this box + 1 ms; on the exchange
after the day's measured send-to-book p50 (22 ms if unmeasured); match after the 50 ms hold against the recorded book;
fill only if >= 5 shares at <= limit; other takers first on ties (excluding nothing — this is a real order). Fee
0.07·p·(1−p); hold to the Gamma winner. Descriptive extras: spot signal, 30–120 vs 120–300 s, 2 s mid markout,
tokyo and colo scenarios. Pass at >= 14 settled days: >= 1,000 fills, net >= +1.0c/share vs settlement, day-bootstrap
90% CI lower bound > 0, tokyo clearly worse than dublin. Kill: CI upper bound < +1.0c after 14 days.

Results (~/rd3-res/rd3-<day>.csv) are blind: never print, summarise or read them; only check existence and row count.
