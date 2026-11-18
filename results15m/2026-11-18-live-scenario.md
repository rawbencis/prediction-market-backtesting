# F7i calibration and new maker ideas under the live scenario, 2026-11-17 evening

**Answer.** Under the live-measured latency nothing tried here makes money:
- 59 configs were run on dev (11-14, 11-15). **None has a dev settle CI above 0.**
- The two rows frozen by the pre-written rule were run once on 11-16. They lose **−3.0¢/share [−6.5, +0.3]**, with negative 1 s and 5 s markouts.
- No row has a holdout settle CI above 0.
- The calibration also shows the simulator is *more generous to makers than live*, so the true numbers are, if anything, worse.

Code: `~/overnight-wt` (`f9_live.py`, `f9_calib.py`, `f9_grid.py`, `f9_report.py`, `frozen/F9_rule.json`, `frozen/F9.json`), commit `d9f6cfa` on `research/overnight-1117`. Read cap `PMLAT_MAXDAY=2026-11-16`; nothing after 11-16 was read. Every job ran one at a time under `MemoryMax=800M`. No orders, no credentials, no service touched, no R-D3/4/5 results read.

## Scenario `live`

| | value |
|---|---|
| book seen | +9 ms |
| post on book | +55 ms if the BTC 5m best bid and ask were unchanged for 1 s, else +190 ms (`live_q`: sampled p10 106 / p50 190 / p90 559) |
| cancel | +90 ms after the signal, and never before order-on-book + 90 ms (a cancel needs the ack) |
| taker hold | 150 ms (unused: everything here is a maker) |
| queue | pess: an existing level at our price is ahead of us by its displayed size |

## 1. Calibration: frozen F7i-B under `live` (11-14, 11-15, 11-16)

Strategy logic is F7i's frozen code. It reproduces the existing F7i-B `live_lean`+ackc file exactly (19,130 orders, 4,043 fills on 11-14).

| | live (measured) | sim `live` | sim `live_q` | match |
|---|---|---|---|---|
| orders with a pull signal before they landed | 76% | **75.5%** (74.6–76.7 by day) | 78.4% | good |
| fill rate of those | 6.7% | 9.7% (7.1% on 11-16) | 8.5% | fair (sim higher) |
| fill rate of the rest | **0 of 65** | **17.4%** | 19.3% | **bad** |
| 1 s markout of fills | ≈ 0 | +0.05¢ | +0.03¢ | good |

- **Why the rest fills in the sim:**
  - The strict fill model gives the same result (12.6% on 11-16), so it isn't the through-print rule.
  - In the sim, 87% of accepted orders find their level, or a better one, already occupied when they land. Live it was 98%.
  - The other 13% land alone at a new price with nobody ahead, and fill 45% of the time. They produce most of the sim's "rest" fills.
  - Without them the rest would still fill ~7.7%; 0 of 65 at that rate has p ≈ 0.005.
- **Reading:** live, some faster maker is at our price before us almost every time, and the recorded book can't fully show that. Treat every maker fill rate and P&L below as an upper bound.

## 2–8. Grid on dev (`live`, pess, dsim fills; 11-14 + 11-15)

Per item, over all its configs: count, best settle ¢/share and best CI lower bound, medians of settle, 1 s and 5 s markout. Full per-row tables: `~/overnight-wt/out/f9_dev.md`; the columns are orders/day, fill rate, fills, mo1s, mo5s, settle with market CI, $/day and pair cost.

| item | idea | configs | best settle | best CI low | median settle | median mo1s | median mo5s |
|---|---|---:|---:|---:|---:|---:|---:|
| 2 | alt-triggered BTC 5m entry | 16 | +0.11 | −0.51 | −0.71 | −0.38 | −0.17 |
| 3 | + resting hedge (pair cost ≤ 0.95/0.97/0.99) on the best item-2 row | 3 | −0.55 | −0.71 | −0.56 | −0.61 | −0.53 |
| 4 | pair maker at the best bids in calm moments | 2 | −2.53 | −3.27 | −2.63 | −1.67 | −1.90 |
| 5 | pre-open queue at 49/48/47¢ | 6 | (+14.5, n 8) | −2.75 | −1.93 | −1.60 | −1.95 |
| 6 | calm join with imbalance | 8 | +3.04 | −0.09 | +1.18 | −0.36 | −0.06 |
| 7 | item 2 with BTC 15m as the target | 16 | −0.53 | −1.30 | −0.86 | −0.79 | −0.85 |
| 8 | imbalance join (imb > 0.3/0.6 and mid up ≥ 0.25¢ in 1 s) | 8 | +0.07 | −1.29 | −1.10 | −1.16 | −1.41 |

**Item 2:**
- The alt trigger fires on most alt rows, so 70–190 k orders/day go out and are cancelled within a second.
- Two alts at 1¢ without the skip is best: +0.11¢ [−0.51, +0.74], 1 s markout +0.03¢.
- Every single-alt or skip row is negative; the TTL never matters.

**Item 3:**
- Hedging the item-2 pick raised the pair-completion rate but made it worse: about −0.56¢.
- Pair cost was 0.96–0.99 when completed.

**Item 4:**
- `pair|n` (both legs rest to 20 s before the close): both legs fill in 85% of pairs. The one-leg fills lose −38¢/share (−$67/day) and swamp the pair gain.
- `pair|y` (cancel both on an alt move): both legs fill in only 6% of filled pairs, at −2.7¢.

**Item 5:**
- With the alt cancel, the orders almost never fill (4–13 fills).
- With no cancel they fill 61–75% of the time, at −1.1 to −2.4¢: the queue place sells to informed flow.
- By level (no cancel): 49¢ −2.39¢, 48¢ −1.47¢, 47¢ −1.11¢; CIs touch 0.

**Item 6:** the only positive family on settle (+0.8 to +3.0¢), but every 1 s markout is negative. Fills are rare: 0.3–1.1% of orders, 145–710 fills/day.

**Item 7:** 15m is negative everywhere: −0.5 to −1.0¢, 1 s markout −0.6 to −0.9¢.

**Item 8, by token price band (imb > 0.6, 120 s TTL, alt pull; settle ¢/share):** the time-left pattern is the only notable one. Fills with more than 120 s left are positive (+2.1 / +2.6¢), fills in the last 60 s strongly negative (−5.6¢, CI below 0). That is a post-hoc slice, not a rule.

| band | 30 s TTL, no pull | same, alt pull |
|---|---:|---:|
| < 0.30 | −0.1 | −0.3 |
| 0.30–0.50 | −2.0 | +0.9 |
| 0.50–0.70 | −3.5 | −2.2 |
| ≥ 0.70 | −2.3 | −1.1 |

## Freeze and holdout

- **Rule** (`frozen/F9_rule.json`, written 19:26 UTC before any grid result): rank dev rows by the market-CI lower bound of settle, needing ≥ 200 dev fills.
- **Picks** (`frozen/F9.json`, 22:2x UTC): `calm|0.3|-1|close` and `calm|0.3|-1|30000`.
  - Both join one tick below the best bid on the token with own-side imbalance > 0.3, after 1 s of calm, and are pulled on an alt move against.
  - They are practically the same rule.
  - Dev: +3.0¢ [−0.09, +6.4], ~210 fills/day.

| pick | 11-16 orders/day | fills | mo1s | mo5s | settle ¢/share [90% CI market] | $/day |
|---|---:|---:|---:|---:|---|---:|
| calm 0.3 −1 tick, to close | 74,939 | 303 | −1.34 | −1.19 | **−3.03 [−6.54, +0.27]** | −46 |
| calm 0.3 −1 tick, 30 s | 74,949 | 303 | −1.34 | −1.14 | −3.09 [−6.60, +0.20] | −47 |

**Count:**
- 59 strategy configs were run on dev and 2 were taken to the holdout. With 59 rows, 3 would show a 90% CI above 0 by chance alone; none did.
- 11-16 had already served as holdout for F7i and F8, so this was a re-used day. A pass would have been weak evidence; a fail is still a fail.
- Calibration adds 1 frozen rule × 2 scenarios × 3 days, plus 1 strict rerun. Those were not selected on.

## What this means
- At `live` speed the F7i family's edge is gone in the sim: F7i-B 1 s markout +0.05¢, settle ±0. Every new maker idea is at or below zero on short markouts.
- The one family with positive settle (calm join) has negative markouts and failed its holdout. That is the signature of a settlement coin flip, not an edge.
- The calibration gap (sim fills ~17% of orders without an early pull vs 0/65 live) means real P&L would be lower still.
