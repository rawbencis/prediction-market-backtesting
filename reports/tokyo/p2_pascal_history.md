### P2: Pascal trade history vs Polymarket (top 30 mapped markets by Pascal all-time notional)

History depth: earliest Pascal trade 2026-06-23; pagination stop reasons {'last_page': 30}; trades 5,065. Polymarket reference: CLOB `/prices-history` (long token, fidelity 1 minute; a point older than 3 min counts as unknown).

Trades with a Polymarket price within 3 min: 5,038 of 5,065 ($450,390 of $496,275 notional).

Taker edge vs Polymarket (¢/share, size-weighted, before fees; + = taker bought below / sold above PM):

| set | trades | shares | at trade | +1 min | +10 min | +60 min |
|---|---:|---:|---:|---:|---:|---:|
| all | 5,038 | 1,035,194 | -0.66 | -0.23 | 0.06 | 0.37 |
| taker D6p3DQ… (20% of $) | 10 | 119,902 | -0.93 | -0.79 | -0.79 | -0.98 |
| taker 7ATdPM… (17% of $) | 2,581 | 192,783 | 0.35 | 0.75 | 0.92 | 1.01 |
| taker 5PFH5m… (14% of $) | 21 | 147,046 | -1.06 | 0.14 | 0.13 | -0.11 |
| taker 2o3ktZ… (8% of $) | 121 | 84,032 | -0.78 | -0.57 | -0.29 | -0.41 |
| taker DcTKkV… (6% of $) | 143 | 64,925 | -2.10 | -1.29 | -0.96 | -1.14 |
| taker Ap1HbU… (4% of $) | 7 | 105,016 | -0.50 | -0.50 | 0.49 | 0.98 |
| taker 6HSPUJ… (4% of $) | 6 | 21,076 | -0.34 | 0.61 | 0.84 | 9.76 |
| taker Hf3o5A… (3% of $) | 69 | 21,128 | -0.52 | -0.52 | -0.52 | -0.52 |

Concentration: takers 143 wallets, top 1 20%, top 3 51%; makers 63 wallets, top 1 61%, top 3 80%.

Toward Polymarket's price (taker edge > 0.5¢, arb-like): 9% of shares; away (< -0.5¢): 74%; within ±0.5¢: 17%.

A Pascal maker quoting Polymarket ± h (fills: trades at or through our price), ¢ per filled share (maker side = −taker edge, vs PM price at the trade; hedge fee = PM rate × p(1 − p)):

| h | filled trades | shares | captured vs PM at trade | after PM hedge fee | vs PM +10 min |
|---|---:|---:|---:|---:|---:|
| 1¢ | 770 | 284,532 | 1.00 | 0.76 | -0.31 |
| 2¢ | 259 | 76,325 | 2.00 | 1.65 | -0.01 |
| 3¢ | 104 | 33,287 | 3.00 | 2.69 | 0.51 |
