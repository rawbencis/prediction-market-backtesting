### L1-D (dev 11-11..11-20)

Lighter reprice after a Binance USD-M move >= 2 bp / 250 ms (onset, exchange time) — time to the next change of Lighter's best bid or ask, ms (p10 / p50 / p90); NaN = no change within 5 s:

| asset | onsets | changed within 5 s | p10 / p50 / p90 ms | share < 50 ms | share 250-400 ms |
|---|---:|---:|---|---:|---:|
| btc | 41551 | 40538 | 18 / 110 / 625 | 29% | 8% |
| eth | 88088 | 85365 | 15 / 86 / 643 | 35% | 6% |

Taker flow, move (within 500 ms after a Binance >= 2 bp / 250 ms move) vs quiet; maker markout vs Polymarket UP mid (exchange time), ¢/share, size-weighted:

| asset | set | trades | shares | share of shares | maker mo 1 s | maker mo 5 s | maker mo 30 s | n with 5 s mid |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| btc | move | 3301 | 180,687 | 17% | -3.96 | -5.57 | -4.84 | 3301 |
| btc | quiet | 20032 | 853,514 | 83% | 1.08 | 0.99 | 0.77 | 20025 |
| eth | move | 2240 | 157,171 | 21% | -4.34 | -4.30 | -2.71 | 2240 |
| eth | quiet | 13050 | 575,870 | 79% | 0.51 | 0.35 | 0.33 | 13048 |

By taker account (top 6 by shares across both assets; maker markout 5 s, ¢/share):

| taker | shares | share | quiet share of its volume | maker mo 5 s (quiet) | maker mo 5 s (move) | maker mo 30 s (all) |
|---|---:|---:|---:|---:|---:|---:|
| 768546 | 258,409 | 15% | 86% | -1.04 | -2.52 | -1.56 |
| 735029 | 174,322 | 10% | 88% | 2.87 | 3.44 | 2.77 |
| 281474976509656 | 147,545 | 8% | 51% | 2.12 | -8.25 | -2.73 |
| 281474976467512 | 137,919 | 8% | 81% | -0.22 | -3.40 | -1.28 |
| 281474976467463 | 79,205 | 4% | 82% | -0.10 | -4.02 | -0.61 |
| 281474976415388 | 78,929 | 4% | 98% | -2.79 | -0.33 | -3.80 |
| others | 890,914 | 50% | 81% | 1.27 | -5.49 | 0.44 |

Top makers by shares: 712772 79%, 763239 17%, 281474976491417 2%, 281474976468907 1%, 762203 0%

**Stop rule (quiet-time maker markout at 5 s > 0?):** btc +0.99¢, eth +0.35¢ -> at least one asset positive: continue to the replay.
