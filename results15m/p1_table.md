### Book anatomy by minutes left (1-s samples; prints per minute per window; dev days 2026-11-14, 2026-11-15, 2026-11-16)

| tf | minutes left | spread ¢ p10/50/90 | share <= 1¢ | min(best sizes) p10/50/90 sh | prints/min | print size p50/p90 sh |
|---|---|---|---|---|---|---|
| 5m | 0-1 | 1.0/1.0/3.0 | 0.81 | 5/69/483 | 177.7 | 10/100 |
| 5m | 1-3 | 1.0/1.0/2.0 | 0.87 | 9/117/741 | 327.0 | 8/60 |
| 5m | 3-5 | 1.0/1.0/2.0 | 0.86 | 8/103/552 | 363.8 | 6/46 |
| 15m | 0-1 | 0.3/1.0/2.0 | 0.85 | 8/44/276 | 109.6 | 6/50 |
| 15m | 1-3 | 0.2/1.0/1.0 | 0.96 | 10/98/441 | 96.8 | 12/85 |
| 15m | 3-5 | 0.2/1.0/1.0 | 0.96 | 13/121/499 | 64.2 | 12/88 |
| 15m | 5-10 | 1.0/1.0/1.0 | 0.96 | 20/131/502 | 38.4 | 13/77 |
| 15m | 10-15 | 1.0/1.0/1.0 | 0.96 | 19/114/469 | 38.3 | 12/72 |

### Speed of the book (ms; p10/p50/p90; share resolved within the window)

| measure | BTC 5m | BTC 15m |
|---|---|---|
| new best level: lifetime | 11/153/1459 (100% happen; n 373,692) | 17/195/2633 (100% happen; n 222,556) |
| best bid 1 tick past a newly joined level | 15/332/9046 (94% happen; n 373,692) | 42/1044/33202 (86% happen; n 222,556) |
| best bid 2 ticks past | 59/1128/18432 (91% happen; n 373,692) | 157/3471/59700 (78% happen; n 222,556) |
| own imbalance >= 0.6 -> < 0 | 14/173/1177 (100% happen; n 556,497) | 12/175/1900 (100% happen; n 419,106) |

### Load and receive delay

| | BTC 5m | BTC 15m |
|---|---|---|
| book rows/s (seconds with rows) p50/p99/max | 57/309/647 | 46/264/587 |
| receive delay l - x ms, all p50/p90/p99 | 9.8/22.0/165.8 | 10.0/25.2/169.5 |
| receive delay in the busiest 1% seconds p50/p90/p99 | 9.8/25.2/189.7 | 10.0/32.2/219.4 |

### Pull signals for a resting BTC 15m UP bid (per window minute; survival = no signal for the hold time, from random times)

| signal | THR | alts | raw triggers/min | episodes/min (>1 s apart) | survive 15 s | 60 s | 180 s |
|---|---|---|---|---|---|---|---|
| alt15 | 0.5c | 1 | 1281 | 8.7 | 0.08 | 0.05 | 0.01 |
| alt15 | 0.5c | 2 | 1116 | 10.8 | 0.13 | 0.09 | 0.03 |
| alt15 | 0.5c | 3 | 848 | 9.7 | 0.19 | 0.14 | 0.06 |
| alt15 | 1.0c | 1 | 898 | 12.2 | 0.10 | 0.07 | 0.03 |
| alt15 | 1.0c | 2 | 737 | 10.5 | 0.17 | 0.12 | 0.06 |
| alt15 | 1.0c | 3 | 542 | 7.6 | 0.26 | 0.18 | 0.09 |
| alt5 | 0.5c | 1 | 2690 | 7.4 | 0.05 | 0.02 | 0.00 |
| alt5 | 0.5c | 2 | 2476 | 9.8 | 0.09 | 0.03 | 0.00 |
| alt5 | 0.5c | 3 | 2053 | 10.5 | 0.13 | 0.05 | 0.00 |
| alt5 | 1.0c | 1 | 2150 | 11.1 | 0.06 | 0.02 | 0.00 |
| alt5 | 1.0c | 2 | 1902 | 11.4 | 0.11 | 0.04 | 0.00 |
| alt5 | 1.0c | 3 | 1515 | 10.0 | 0.16 | 0.07 | 0.00 |
| btc5 | 0.5c | 1 | 1589 | 10.3 | 0.12 | 0.04 | 0.00 |
| btc5 | 1.0c | 1 | 1460 | 9.9 | 0.14 | 0.05 | 0.00 |

### Which book moves first (episode starts: mid move >= 0.5c in 500 ms after 1 s quiet; nearest same-direction start of the other book within +-1 s)

| A -> B | matched | share A first | lag B - A ms p25/p50/p75 | share lag > 190 ms |
|---|---|---|---|---|
| alt15 -> btc15 | 16,073 of 66,102 | 0.82 | 73/308/615 | 0.62 |
| alt5 -> btc15 | 12,911 of 55,724 | 0.82 | 82/325/637 | 0.63 |
| alt5 -> btc5 | 18,137 of 55,724 | 0.80 | 80/357/658 | 0.66 |
| btc15 -> alt15 | 15,907 of 58,857 | 0.18 | -617/-312/-78 | 0.12 |
| btc15 -> btc5 | 35,451 of 58,857 | 0.61 | -158/68/258 | 0.31 |
| btc5 -> alt5 | 17,949 of 77,153 | 0.20 | -660/-361/-85 | 0.14 |
| btc5 -> btc15 | 35,547 of 77,153 | 0.39 | -259/-68/159 | 0.23 |
