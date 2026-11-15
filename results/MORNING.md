# Morning report — Dublin retest, night 2026-11-14/15 (final 2026-11-15 ~09:00 UTC)

## 1. Needs you / went wrong
- **momentum-eval's daily run (2026-11-15 00:25:27) was OOM-killed** (systemd Result=oom-kill) while my fav-hedge
  stage 1 held ~1.7 GB (00:10–00:29). My fault. Not restarted (rule 1), its output not read (rule 2). Next timer run
  2026-11-16 00:25: check whether it back-fills 2026-11-14. A 2nd global OOM at 00:45:31 (my stage 3 + ohioai-match)
  killed only my process; ohioai-match finished OK at 00:53. Fixes: loaders read per file/asset (peak 0.6 GB); every
  heavy job since 08:40 runs under `systemd-run --user --scope -p MemoryMax=1200M -p MemorySwapMax=0 nice -n 19`.
- **I lost the session 01:00–08:40 UTC**, so this report is late. Background jobs kept running.
- **Feeds recorder bug, fixed 08:50:** the legacy RTDS socket stayed open but went silent (gaps of 100, 67 and 26 min,
  201 of 525 min missing, only 4 reconnects). Added a "silent > 10 s → reconnect" watchdog and restarted feeds_rec.
- `speed_atlas.py` / `test_speed_atlas.py` exist nowhere (box or any git branch). I wrote a minimal stand-in, `atlas.py`.
  Outputs are in `~/dublin-retest/out/atlas/` (rule 7 rules out `~/atlas`).
- No push: no GitHub credentials on the box. The branch is local only (hashes in §7).
- Probe: `order_probe.py` has the single permitted edit (get_order retry + book fallback; diff vs
  `inputs/order_probe.py.orig`). 165 cycles, **0 fills, 0 not-cancelled, no CLEANUP FAILED**. The get_order parse error
  hit 14 times and was absorbed by the retries every time (book fallback never needed). Loop ended 07:20.
- The 30 s TWAP no longer exists. The PolyBolt reference-price channels need CLOB keys (only in probe.env, untouched).
  Recording therefore uses the deprecated public RTDS topics, which will be switched off. Deribit `.raw` needs auth, so
  `.100ms` is recorded instead.
- Rule-3 reading: fav-hedge uses 2026-11-14 only tonight (11-15 is partial; the overnight rule 3 is stricter than the
  follow-up). Candidate B (7 dev days) is scheduled.

## 2. Facts (details: `inputs/facts.md`, `out/settle-check.txt`)
- **Settlement (5m, 15m, 4h):** Chainlink `{asset}-usd-twap-60s` stream. Up iff close ≥ open. Recorded check, BTC 5m+15m:
  **open = 60s-TWAP value at the window-start second, close = 60s-TWAP value at the window-end second: 83/83 agree with
  Gamma.** Spot open + TWAP close: 80/83. Spot/spot: 78/83. Shifting both boundaries by +1/+2 s still gives 83/83,
  −1 s gives 82/83. Only 83 of the 140 covered windows are usable (the RTDS gaps above). 1h/daily markets use Binance
  candles instead.
- Taker hold: CLOB info `itode: true`; the 50 ms length is not exposed by the API and remains an assumption.
- Fees: crypto_fees_v2, taker 0.07·p·(1−p), makers 0, 20% of taker fees as a rebate pool. Min size 5, tick 0.01 (15m
  can show 0.001).
- RTDS: legacy `crypto_prices_chainlink`, `crypto_prices_twap_sixty` (filters ""); `..._twap_thirty` is dead. New:
  `price.crypto`, `price.crypto.twap` (auth).
- Disk: 53 GB free. shadow-rec ~1.2 GB/day. feeds-rec ~1.4 GB/day (no retention yet).

## 3. Order path by hour (dict lines in `out/probe-hourly.log`; valid = 0–2000 ms; `out/probe-by-hour.txt`)
| UTC h | send→visible p10/p50/p90 ms | cancel→gone p10/p50/p90 ms |
|---|---|---|
| 00 | 20 / 25 / 52 | 14 / 19 / 54 |
| 01 | 20 / 25 / 685 | 16 / 18 / 69 |
| 02 | 37 / **228** / 673 | 103 / **221** / 1175 |
| 03 | 16 / 27 / 81 | 11 / 24 / 1152 |
| 04 | 13 / 21 / 95 | 21 / 149 / 545 (n=6) |
| 05 | 18 / 26 / 652 | 17 / 21 / 163 |
| 06 | 13 / 24 / 33 | 13 / 20 / 900 |
| all | 17 / 26 / 290 | 16 / 21 / 484 |

The p50 holds at 21–27 ms except 02h, which was slow. The p10 floor is ~13–20 ms. Caveat: 19 of 159 send and 49 of 165
cancel timings are ≤ 0. The probe's ±5-shares-at-1¢ detector picks up other traders' orders, so the raw p10 is
meaningless and the tails are noisy.

## 4. Atlas, 2026-11-14 (`out/atlas-2026-11-14.txt`; event = Binance ≥ 2 bp in 500 ms)
- (a) Median reprice delay, Binance trade → PM book (spot): BTC 5m 296 / 15m 233 ms; ETH 104/120; SOL 72/99;
  XRP 87/102; DOGE 78/95. All 106 ms (fut).
- (b) Stale ask toward the move still hittable (≥ 5 shown) at t0+130/160/200 ms: all 57/50/44% (spot); BTC ~80/75/70%,
  alts ~40–60%. Caveat: "stale" includes quotes that didn't need to move a tick (small moves vs the window).
- (c) 5m vs 15m: 5m reprices first 55% (median lead 11 ms). BTC vs alt books on the same move: the ALT book reprices
  first in ~80% of cases (median 210–280 ms ahead of BTC).

## 5. dsim (`repo/dsim.py`, 20 tests pass across dsim/fav_hedge/feeds_rec/settle_check)
- **Print timing (amendment):** a print's match time is the book row, at or before it within 100 ms, where its level
  lost ≈ its size or vanished. 94.3% of 437,553 prints match. Lag print − match: p10 15, p50 21, p90 41, p99 81 ms.
  Unmatched prints use print − 21 ms. The old flat-offset sweep (hits 0:45%, 10:51%, 20:71%, 30:87%, 50:93%) is
  superseded.
- **V2** (1,786 OhioAI taker fills; the fills file grew after the 00:45 ohioai-match run):
  - FAK at the per-print match time fills **69.5%**.
  - The level is visible at the match time **86.0%**: 100% for the 79.7% of OhioAI prints that match a book row, 31%
    for the rest (likely multi-level sweeps).
  - Of matched-print misses, 273/276 fail only because other prints at the same estimated ms get priority (the
    conservative tie rule).
  - Book timing is right; the 20% of unmatched sweeps is the gap to the 90% target.
- **V3** (descriptive only, not a queue calibration; pess stays the judged model): 1,080 OhioAI maker fills, a post-only
  at their price in [x−1 s, x+1 s] fills pess 95.8% / prop 96.1% / front 97.1%.

## 6. Feeds recorder
Running (restarted 08:50 with the watchdog). Rows/hour 23:44–08:50: pmbook ~2.9 M, coinbase ~490 k, deribit ~76 k,
chainlink and twap60 ~11 k each (with the gaps; ~18 k expected). Reconnects: rtds 4, pm 9. CPU ~5–10%.

## 7. Git, and next steps
Branch `research/dublin-retest` (local, not pushed); latest commits listed in `out/git-log.txt`.
1. Re-run the settlement check and V2 once ≥ 24 h of gap-free RTDS data exists. Pin down whether the open is the TWAP
   at the start second for 15m/4h as well.
2. Close V2's sweep gap: use feeds-rec 5-level depth (BTC) to match prints below the top, then retest V2 ≥ 90%.
3. Replace the probe's ±5-at-1¢ detector with an order-id/user-channel confirmation. The book detector is contaminated.

## fav-hedge (follow-up) — dev 2026-11-14 only, DESCRIPTIVE, `fav-hedge/`
- Grid written before running (`grid.json`, 2,950 configs). Stage 1 (2,304, dublin/pess, BTC 5m, flat 50 ms shift)
  looks like noise: median −$10.1/day, 9.2% positive, 2.3% with lo90 > 0. Mirror stage 2 (256): median −$12/day,
  best +$1.4.
- **Candidate A** (frozen 00:40 UTC, `frozen.json`): fav | fixed t=240 s, X=0.60 | leg 1 maker at bid | o=2¢ |
  leg 2 reprice | naked hold. Re-run with per-print timing:

| scenario/queue | entries | hedged | locked $ | naked $ | net ¢/$ [90% CI] | $/day [90% CI] |
|---|---|---|---|---|---|---|
| dublin/pess | 49 | 80% | 5.35 | 0.83 | +2.59 [+2.12, +3.16] | +6.18 [+5.08, +7.50] |
| dublin/prop | 50 | 80% | 5.45 | 0.83 | +2.58 [+2.12, +3.14] | +6.28 |
| dublin/front | 72 | 78% | 6.68 | 1.33 | +2.35 [+2.06, +2.71] | +8.01 |
| tokyo/pess | 41 | 78% | 4.65 | 0.82 | +2.74 [+2.10, +3.55] | +5.47 |
| tokyo/front | 63 | 75% | 7.23 | 1.37 | +2.86 | +8.61 |
| colo/pess | 49 | 80% | 5.75 | 0.83 | +2.76 [+2.20, +3.39] | +6.58 |
| colo/front | 73 | 78% | 6.95 | 1.33 | +2.41 | +8.28 |

- **Read this plainly:**
  - A earns ~$0.10 per hedged pair (two maker bids summing to 0.98).
  - Its 10 naked legs were bought at 0.98, held, and all won. One loss costs ≈ −$4.90, so the CI misses this tail risk.
  - Naked losses did not outweigh hedged profit on dev, only because none occurred.
  - The sign flips under no queue model.
  - Speed value is small: dublin − tokyo = +$0.7/day (pess), colo − dublin = +$0.4/day. So A fails the "clearly worse
    under tokyo" part of the pass rule as things stand; the edge is not from speed.
  - On BTC 15m (14 entries) and ETH 5m (45), A is negative (−$1.85 and −$3.50/day).
- Families (33 best-per-family configs): positive under dublin/pess 18/33 (lo90 > 0: 6), under dublin/front 28/33,
  under tokyo/pess 8/33. Median $/day: dublin/pess +0.2, dublin/front +6.3, tokyo/pess −1.3. The queue model, not the
  signal, drives most of the result. Full table: `fav-hedge/table_A_families.md`.
- **Candidate B** is scheduled: `repick_B.sh` (nohup, pid 23567; does not survive a reboot). It runs 2026-11-21 00:30 UTC
  and waits for ohioai-match to finish and load < 1.5. Inputs: 7 dev days, same grid and rule, memory-capped. Output:
  `frozen_B.json`. The holdout command for each candidate is in its frozen file. Holdout days untouched.

## Taker edge at landing (atlas `--edge`, 2026-11-14 only, DESCRIPTIVE; `out/atlas/edge-2026-11-14-{spot,fut}.{txt,csv}`)
Buy the side the Binance ≥ 2 bp/500 ms move favours, FAK landing at t0+L (≥ 5 shown; same-ms prints first). EV per
attempt = mid(t0+2 s) − price − fee in ¢/share (0 if not hittable); settle = vs Gamma winner; 90% CI = window bootstrap.
- **BTC 5m, 2–5 bp, 30–120 s left (spot, n=591):** EV L100 **+3.32¢ [+2.65, +4.12]**, L165 +3.00, L300 +1.99;
  settle +2.95¢ [+0.98, +5.09]. Fut (n=629): L100 +3.72 [+2.99, +4.63], settle +5.25 [+2.99, +7.51]. This is the best
  of 10 BTC 5m cells (selection-biased).
- BTC 5m, 2–5 bp, 120–300 s (n=2,575): L100 +1.41 [+1.22, +1.65], L300 +0.92; settle ≈ +3¢. Edge shrinks with L:
  ~0.5¢ (120–300 s) and ~1.3¢ (30–120 s) are lost going from 100 to 300 ms.
- BTC 15m: best is 2–5 bp, 30–120 s, L100: +6.21¢ [+4.28, +7.80], but n=36 and settle +0.22 [−8.7, +11.0]. With
  300+ s left (n=2,982) the mid-markout EV is ≤ 0.
- Alts mostly negative (DOGE/SOL/XRP −1 to −3¢), except a small positive at L100 with 30–120 s left. 5–10/10+ bp
  buckets have n ≤ 100.
- **Alt-led BTC 5m trigger** (≥ 2 alt 5m books moved, BTC not, within 300 ms; n≈15.8k): negative everywhere, about
  −1.0 to −2.1¢ at every L. Best cell −0.98¢ [−1.28, −0.66], settle −1.91¢. The alt-book lead is not a profitable
  BTC entry.

## Operator round 3 (2026-11-15 ~09:30 UTC)
1. **Taker edge, L ∈ {80,130,180,250}, last-60 s bucket added** (`out/atlas/edge2-2026-11-14-{spot,fut}.txt`, csv):
   - BTC 5m 2–5 bp, **last 60 s**: spot n=150, EV L80 **+5.03¢ [+3.14, +7.67]**, L250 +2.69; settle +7.92¢ [+3.91, +12.62].
     Fut n=154: +6.11¢ [+4.02, +8.42], settle +8.91¢. This is the best of 12 cells (selection-biased).
   - BTC 5m 2–5 bp, 60–120 s: +3.19¢ (L80) → +2.45¢ (L250). 120–300 s (n=2,575): +1.44 → +1.07¢, settle ≈ +3¢.
   - BTC 15m best: 2–5 bp, 120–300 s: +1.57¢ [+1.06, +2.16], but settle +0.51 [−1.27, +2.23]. 300+ s is ≤ 0.
   - Alt-led BTC 5m trigger: still negative everywhere (−0.8 to −1.6¢; best −0.79¢ [−1.74, +0.27]).
   - Overlap note: the registered momentum test is BTC 5m, Binance 2 bp / 5 s, 30–300 s left. These tables use only
     2026-11-14 and must not be extended to later days.
2. **V2 tie rule:** excluding all 1,453 OhioAI prints changed nothing (69.5%). The competing same-ms prints were
   other prints with different print stamps (−32..+67 ms) that my match-time step assigned to the same book row.
   Replay rule now: a same-ms print goes first only if stamped earlier than the replayed print. **V2 = 80.3% overall,
   93.5% for prints matched to a book row** (unmatched 20% ≈ sweeps: 31% visible). Our own simulated orders keep the
   conservative rule (all same-ms prints first).
3. **Final minute: run on 2026-11-14 ONLY** (`out/final-minute-2026-11-14.txt`). Reason: a 90–99¢ favourite in the
   last 60 s on later days largely reveals the outcome of the registered late-breakout test (ask first reaching
   60–80¢ in the last 60 s, BTC 5m primary), which must stay blind until 11-29. Say if you still want all days.
   Results are thin (≤ 70 markets per cell):
   - BTC 5m ask 0.97–0.99, 31–60 s: 288 snapshots / 70 markets, win 100%, +2.31¢ [+2.25, +2.37].
   - 0.90–0.95 and 0.95–0.97 at 31–60 s: −1.5 / −0.7¢ with CIs spanning ±8¢.
   - FV ≥ 0.99: 100% wins at +2.9 to +3.1¢.
   - The TWAP fair value uses Binance as the Chainlink proxy (no Chainlink recording on 11-14). It is unreliable
     ≤ 15 s before the close (FV 0.15–0.3 on tokens that won), so rerun it on Chainlink data.
4. PolyBolt with keys from probe.env: **not done, waiting for your yes.**
5. **feeds_rec now records BTC and ETH 1h books** (`pmbook1h-HH.jsonl.gz`, top 5 levels, both tokens). Verified on
   the 09:00 UTC (4am ET) markets. I caused a ~4 min gap in all feeds (09:00:02–09:04:15) with a pkill that killed
   its own shell before the restart ran.

## R-D3 registered (2026-11-15 14:12 UTC; rule text in `frozen/R-D3.md`, commit 62575ef)
- `repo/rd3_eval.py` (dsim): one row per signal × src (fut primary, spot) × scenario (dublin primary, tokyo, colo).
  Columns: filled/px/fee and pnl vs the Gamma winner, plus a 2 s mid markout. 5 synthetic-day tests pass.
- Loop: `rd3_loop.sh` (nohup) runs daily at 00:40 UTC, memory-capped, after the nightly evals are idle. It writes
  `~/rd3-res/rd3-<day>.csv` for each finished, fully settled day from 2026-11-15; first file 2026-11-16 ~00:40.
  The log holds row counts only. `~/rd3-res` is outside rule 7's directories, at your explicit request. Blind: I will
  only check existence and row count.
- Modelling choices to know:
  - The box clock is taken as the exchange clock.
  - "Seen ask" = the newest book row received on this box by the decision time.
  - The day's send p50 uses probe rows from 0 to 2000 ms; ≤ 0 rows are detector artefacts.
  - Tokyo decides at Binance T + 2 + 1 ms with the book 116 ms stale and a 120 ms send.
  - Colo decides at this box's Binance receipt + 1 ms with 1 ms book/send.
- Disclosure: before registration I measured the closely related "taker edge at landing" (2 bp/500 ms, BTC 5m) on
  2026-11-14 only (dev). No R-D3-like number was computed on 2026-11-15 or later.
