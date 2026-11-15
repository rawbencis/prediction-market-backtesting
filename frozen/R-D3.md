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

## Frozen evaluator (added 2026-11-15T14:24:08Z; rule text above unchanged)
R-D3 is evaluated only by the code in ~/dublin-retest/frozen/rd3-code (read-only), taken verbatim from commit 9113475:
    73269126b3156315fe1709a4b2811d7b4c62e0e294ffb621dfbbb9ac1dd38f0e  rd3_eval.py
    435eb9807677055462ccf964dac28c50e7c891b40c2eeb40b7b2a7de8996b3a6  dsim.py
    0daceb76a1a5f979b552293560f920398e9fd476ffc2223903713e8e8b447b57  test_rd3_eval.py
Runner: ~/dublin-retest/deploy/rd3_once.sh (read-only), sha256 7f7e2170a8e3b17645e1deddddeb9e33217157918b1155d7410bf3107fd096e5. It checks MANIFEST.sha256 before every run
and refuses to run on a mismatch. Started by rd3-eval.timer (00:40 UTC daily). Any later change to the repo's dsim.py
or rd3_eval.py does not affect R-D3.
