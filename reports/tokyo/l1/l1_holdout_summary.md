### L1 holdout (11-21..11-27), frozen pick btc `pm_mid|h2|1bp/250ms|q5|D0|stop60`

| asset | cell | fills_day | shares_day | vsfair | mo1 | mo5 | mo30 | settle | ci_lo | ci_hi | usd_day | days_pos | msgs_per_s | top3 | hedged |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| btc | pm_mid|h2|1bp/250ms|q5|D0|stop60 | 91.57 | 1413.40 | 2.19 | 0.64 | 1.98 | -0.74 | -1.96 | -7.91 | 4.02 | -27.68 | 3 | 1.71 | 0.35 | -0.64 |
| btc | pm_mid|h2|1bp/250ms|q5|D0|stop60 | 92.71 | 434.60 | 2.21 | 0.89 | 2.11 | 0.15 | -2.71 | -7.99 | 2.72 | -11.78 | 2 | 1.71 | 0.31 | -0.45 |

| criterion | result |
|---|---|
| settle >= 1.0¢/share | FAIL |
| 90% lower bound > 0 | FAIL |
| 5 s markout > 0 | pass |
| >= 2,000 filled shares | pass |
| >= 5 of 7 days positive | FAIL |
| >= $10/day | FAIL |

**Holdout: FAIL** (filled shares 9,894).
