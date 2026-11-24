# Overnight report — Dublin box, night 2026-11-16 → 17 (UTC)

_Status: FINAL, 05:10 UTC 2026-11-17._

## 1. Executive summary
- **updown-rec** OK (45 h up, receive lag p50 9 ms, ~30 server-side socket drops/day, auto-reconnect). **updown-act** OK (~73 k rows/h, activity lag p50 ~220 ms).
- **feeds-rec** OOM-restarted at 19:56 (its own 500 MB cap; ~8 s gap; truncated gzip member in the 11-16 `-19` files), manually restarted at 20:01:54; growing ~32 MB/h → next OOM ≈ 11:30 UTC today.
- **R-D1 (momentum-eval) FAILED both nights (OOM), 0 days counted. R-D2 (breakout-eval) OOM-killed tonight, missing 11-16** (1 day so far: n 23 fills, −5.4¢). Both need a streaming fix by the operator.
- **R-D3:** blind eval wrote `rd3-2026-11-16.csv` (not read); live leg on the 2nd box not checked. **Fav-hedge B** systemd one-shot timer armed for 11-21 00:30. **OhioAI match** OK.
- **Octopus paper shadow:** connected, but **BUYs blocked since 11-14 18:18 (watchlist 146 h old > 96 h)**: 9 paper fills ever, −$3.59. Needs a watchlist refresh.
- **Research (Polymarket-only, ≈ 980 configs, 6 frozen picks, holdout 11-16 once):** taker ideas (follow/fade prints, wallet-following, sibling 15m, imbalance, burst-fade) are all
  dead after fees. The one setup that survives is a **fast BTC 5m maker that improves the spread by one tick and pulls when the ETH/SOL/XRP/DOGE books move against it**
  (those books reprice ~150–200 ms before BTC's). **Holdout: F7i-B +1.85¢/share [+0.99, +2.74], 7.4 k fills/day; F7i-A +3.52¢ [+0.78, +6.20], 1.0 k/day.**
  Both collapse under tokyo latency on that day, and a placebo pull and an ETH-target control both fail, as the mechanism predicts.
  **But the extra 5-h sample (11-17 00–05) splits the two parts:** the speed-bought 1 s markout replicated (+0.57¢ dublin vs +0.24¢ tokyo), while settle didn't
  (dublin F7i-B +0.26¢ [−1.45, +1.88], tokyo +2.87¢). So F7i is a CANDIDATE with a robust speed-dependent markout and an unproven settlement P&L.
- **Caveats:** one holdout day; our order is a phantom (other bots' reactions unmodelled); at p90 latency the settle edge loses significance.
- **Recommend today:** (1) fix the R-D1/R-D2 eval OOMs (stream per hour or MemoryMax + serialize), (2) refresh the Octopus watchlist, (3) register **R-D4 = F7i-B** (and R-D5 = F7i-A) as a 14-day
  recorded-data shadow with the frozen rules in §4. Decide separately whether a 5-share live probe is wanted, since only real orders can test the reaction risk.

## 2. Part A — service status (checked 2026-11-16 19:59–20:05 UTC)

| service | state | last output | key numbers | issues |
|---|---|---|---|---|
| `updown-rec` (shadow_rec.py: UP-token top of book for 5 assets × 5m/15m, `last_trade_price` prints both tokens, Binance spot+fut) | active since 2026-11-14 23:22 | live (pm-20.bin growing) | 11-16: pm ~1.43 M rows/h (min 1.03 M), prints ~26 k/h, Binance ~245 k/h; receive lag p10/50/90 = 8/9/~17 ms (12-min stats); shadow-latency 15-min p99 60–165 ms | ~30 PM socket drops/day (counter 56 → 119 over 24 h), all `1013 slow consumer: send buffer full` from the server side; reconnects automatically. RSS 623 MB after 45 h (watch for growth). 11-14 only from 09:00. |
| `updown-act` (act_rec.py: RTDS activity census, one row per fill participant) | active since 2026-11-15 06:33 | live (act-20.bin) | ~73 k rows/h (57–98 k); activity lag p50 ~220 ms (p10/p90 ~120/340 ms); 90 connects / 89 stalls in 37 h | data from 2026-11-14 21:41; 0 errors logged in 24 h |
| `feeds-rec` (Chainlink/TWAP, Coinbase, Deribit, **PM BTC 5m/15m 5-level depth both tokens**, BTC/ETH 1h) | active, **restarted 19:57:01 after an OOM kill at 19:56:56** | live | pmbook ~4.2 M rows/h; 1.3–1.6 GB/day, no retention | **OOM-killed by its own 500 MB `MemoryMax`** after 29.5 h of uptime (RSS 506 MB at kill) → memory grows ~17 MB/h; it will be killed again in ~1 day. Auto-restart (systemd) recovered it in 5 s. **Gap: pmbook last readable row 19:56:53.6 → first after restart 19:57:01.8 (~8 s; chainlink 19:57:02).** The kill left a truncated gzip member inside every `*-19.jsonl.gz` of 2026-11-16, and the restart appended a new member → `zcat`/`gzip.open` stop at the break, so 19:57–20:00 rows are only readable by decompressing from the second member (byte 40,619,201 in pmbook-19). Readers should handle that. Not touched by me (rule 2). Recommend: find the leak (the per-window task/dict state?) or a daily restart. |
| `momentum-eval` (R-D1) | **FAILED** both nights — 2026-11-16 00:27:20 and 2026-11-17 00:25:12, OOM (its python at 2.3–2.5 GB RSS) | `~/mom-res/mom-2026-11-14.csv` only; **`~/mom-res/report.txt` does not exist** | **R-D1: 0 days counted, no result.** 11-15 was never evaluated. | Its eval loads whole days; 11-15 alone takes > 2.5 GB. It did OOM again on 2026-11-17 00:25 (global OOM, no MemoryMax on the unit). Needs a code fix (stream per hour) — not done (rule 2). |
| `breakout-eval` (R-D2) | **FAILED 2026-11-17 00:20:17 (OOM, 2.36 GB)**; previous run 2026-11-16 00:20 ok (7 s) | `~/breakout-res/bo-2026-11-15.csv` | R-D2 primary (BTC 5m, last 60 s, first trigger/market, tokyo≙Dublin scenario, cap +5c), counting from 11-15: **1 day, n 27, fill 0.85, 23 fills, net −5.36 ¢/share, CI n/a (1 day), $/day −6.2 → running** (pass needs ≥ 14 d, ≥ 500 fills, ≥ +1.0 ¢, CI low > 0). Note: 23 fills/day ⇒ 500 fills needs ~22 days. | I ran its `report` on copies in scratch (the report writes `settlements.csv` into the res dir). |
| `rd3-eval` (R-D3) | ok, last run 2026-11-16 00:40 | `rd3-2026-11-15.csv` exists (not read, blind to day 14) | R-D3 live signal runs on the second box: **not checked (unreachable)** | `rd3_once.sh` can't write `~/rd3-res/loop.log` (owned by root, "Permission denied") → the blind row-count log is lost, eval itself wrote its CSV. |
| fav-hedge candidate A / repick B | A frozen (`fav-hedge/frozen.json`, 2026-11-15 00:40); B = `favhedge-repick.timer` (systemd, `Persistent=true`, enabled) fires 2026-11-21 00:30 | — | holdout from 11-21 | The old ad-hoc `repick_B.sh` process is **not running**; it was replaced by the systemd one-shot timer (survives reboot; `ConditionPathExists=!frozen_B.json`). OK. |
| `octopus-shadow` (+ sweeper timer, every minute) | active since 2026-11-14 10:44; sweeper ok (19:59) | `paper_state.json`, `ws_state.json` 19:59 | RTDS connected, 3,571 events, 6 wallets (sweeper says 5 watched), lag p10/50/90 225/369/445 ms; 154 connects / 180 stalls (Cloudflare proxy restarts); **9 paper fills, all on 2026-11-14, all closed, PnL −$3.59** (fees $0.97). `signal_latency`: 787 / 1,334 / 1,453 rows (11-14/15/16), trade→local p50 0.78 s, p90 1.18 s | **BUYs blocked since 2026-11-14 18:18: "watchlist stale" (145.7 h old, blocks at 96 h)** → 905 signals skipped; shadow is not producing new fills. Needs a watchlist refresh by the operator. |
| `shadow-latency` (every 15 min) | ok | `~/shadow-latency.csv` 20:00 | pm p50 ~9 ms, p90 ~18–21 ms, >1 s 0.003% | one 15-min window with 0.26% > 1 s (19:45) — the OOM period |
| `ohioai-match` | ok, last run 2026-11-16 00:45–01:02 | `match-2026-11-15.txt` / `ohioai_ms.json` | 11-15: 4,853 OhioAI buys, 4,804 matched, 2,708 taker fills; their book age p50 7 ms; we would land first 42%; our-rule-fired 52% | — |
| other | `order_probe` not running (good); no crontab | | | Tokyo services: **not checked** (not reachable). |

**Later feeds-rec events:** a clean manual stop/start at **2026-11-16 20:01:54** (`Stopping…`/`Started`, not an OOM; not done by me, since I issued no systemctl commands).
At 05:07 its RSS was **295 MB after 9 h 05 m (~32 MB/h)**, so at this rate it reaches its 500 MB `MemoryMax` again around **11:30 UTC today** (another OOM restart and gzip break).

**Operator note (20:10) vs kernel log:** the operator attributed the feeds-rec kill to an earlier 1.2 GB job. The kernel log says `oom_memcg=/system.slice/feeds-rec.service` (its own 500 MB cap, RSS 506 MB), not a global OOM; the two 1.2 GB ad-hoc jobs were killed inside their own scopes at 19:38, 18 min earlier. Either way, from 20:10 every analysis job tonight ran under `MemoryMax=800M`, one at a time.


**Nightly evals, 2026-11-17 (checked 01:06 UTC):**
- **`breakout-eval` (R-D2) FAILED: OOM-killed at 00:20:17** (global OOM; its python at 2.36 GB anon RSS), so `bo-2026-11-16.csv` was **not written**.
  R-D2 now misses 11-16 and will keep failing nightly: it retries every day without a file, and the days keep getting bigger.
- **`momentum-eval` (R-D1) FAILED again: OOM-killed at 00:25:12** (2.34 GB). R-D1 still has **0 counted days** after 2 nights.
- `rd3-eval` ok (00:40, `rd3-2026-11-16.csv` written, not read; `loop.log` still "Permission denied", root-owned). `ohioai-match` ok (00:45–01:02):
  11-16: 5,215 OhioAI buys, 5,176 matched, 3,595 taker fills, we'd land first 37%, our rule fired 46%.
- None of my jobs ran between 00:12 and 01:10 (`guard.sh`). The kernel's OOM victims were only the two eval pythons; updown-rec and octopus run with
  `oom_score_adj −800` and survived; all recorders stayed active. Cause: each eval loads a whole day (> 2.3 GB) on a 3.8 GB box with ~2.1 GB available.
- **Needs the operator (rule 2 kept me from touching them):** make `momentum_shadow.py eval` and `breakout_shadow.py eval` stream per hour (as `pmlat.hours` does), or
  give the units `MemoryMax` + run them one at a time. Until then **R-D1 and R-D2 are not accumulating days**.

**System:** 2 vCPU, 3.8 GB, no swap; free ~2.3 GB available at 20:00; disk 47 GB free (shadow-rec 3.3 GB, feeds-rec 2.9 GB).
**OOM history (kernel log):** 11-15 00:25 & 00:45 (earlier sessions' jobs), 11-16 00:27 (momentum-eval), 11-16 17:46 ×3 (a 2.3 GB ad-hoc python3, global OOM), 11-16 19:38 ×2 (ad-hoc jobs killed inside their 1.2 GB scopes — the cap worked), 11-16 19:56 (feeds-rec's own cap).

## 3. Part B — Polymarket-only low-latency research

**Common method.** Code: `~/overnight-wt` (git worktree of `~/lighter-flips`, branch `research/overnight-1116`). `pmlat.py` streams the
`~/shadow-rec` hour files (3 hours of one asset/timeframe at a time, peak RSS < 200 MB) and builds markets with `dsim.mk_market`
from `~/dublin-retest/repo/dsim.py` (imported, not copied), so the print match-time model and the taker/maker fill rules are dsim's own:
taker = FAK at arrival + 50 ms hold against the top level, needs ≥ 5 shown after same-ms prints of others; maker = post-only,
pess/prop/front queues. Scenarios: **dublin** book/print seen +9 ms, order on book +22, cancel +21, hold 50; **tokyo** +116/+120/+120, hold 50
(dsim's tokyo). Fees: taker 0.07·p·(1−p)/share on every taker leg; maker rebate never counted. Every job ran under
`systemd-run --user --scope -p MemoryMax=800M` (1200M before the operator's 20:10 note), one at a time.
Data: BTC 5m. Dev = 2026-11-14 (09–23 h; census from 21:41) + 2026-11-15; holdout = 2026-11-16 (00–23 h).
Each print's taker wallet comes from the census (`act` rows with fee > 0, joined by tx hash; 98% of prints found).
Facts checked at the start: one `last_trade_price` print per tx. Print receive lag p50 9.5 ms. **Activity-stream (wallet) lag p50 246 ms (p10 159, p90 355)**,
so a print's wallet is *not* known at decision time; only price, size, side and timing are.

### F1 — follow-on flow after a print/sweep (taker behind the print; maker or taker exit) — dev: CLOSED
- **Hypothesis:** after a large taker print, slower bots keep hitting the same side; we take right behind it (we see the print 9 ms after its stamp)
  and exit into the follow-on flow.
- **Descriptive (dev, 725,574 BTC 5m prints, `f1_events.py`):** there *is* same-side follow-on flow. Same-direction prints in (50, 250] ms after
  a print are 1.5–2.0 vs 0.7–1.3 opposite. But the mid move it buys is small: prints that swept the top level move the mid in their direction
  by +0.2–1.0¢ at 1 s (0–5 $ prints +1.0¢; > $50 prints +0.2–0.6¢). Prints > $50 **reverse** at 30 s (−0.8 to −1.7¢). Round-trip cost at 50¢:
  2 × 1.75¢ fee + spread ≈ 4.5¢. The move is 4–20× too small.
- **Rule simulated (`f1_sim.py`, dsim fills):** decide at print stamp + 9 ms. BUY 5 shares of the print-direction token (`follow`) or the other token (`fade`) FAK,
  cap = ask seen + 1 tick, matching at +22 +50 ms. Exits: settle; taker sell at +1/5/30 s; post-only exit +1/+3 ticks (pess, 10 s TTL, then taker / settle).
  One trade per market per 30 s.
- **Configs:** side 2 × min $ {20, 50, 100, 250} × swept {any, yes} × time-left {all, > 60 s} × exit 6 = **192**. Fill rate 95.5%.
- **Dev (dublin):** median config by exit: follow taker exits **−3.4 to −3.7¢/share**, follow maker exits −3.1¢, follow settle −0.6¢;
  fade −3.2 to −4.2¢. The best of 192 is follow, ≥ $100, swept, hold to settle: **+0.49¢ (n 1,677; CI market [−1.25, +2.23], day [−3.09, +2.80])**.
  That is a settlement coin flip, not a speed edge, and no config has CI-low > 0 even on dev.
- Frozen pick written to `frozen/F1.json` (20:18 UTC) for one holdout run (below).

### F2 — wallet-specific front-running via the census — dev: CLOSED
- **Measurement (`f2_leaders.py`, dev):** for every print with a known taker wallet (64% of prints; census starts 11-14 21:41), I counted other wallets'
  same-direction prints 0.2–3 s after it, minus the mirror window 0.2–3 s before, minus the same difference for the opposite direction.
  Over all prints the after/before counts are identical (12.18 vs 12.17 same-dir), so **there's no generic copy flow after a print.**
- Specific structure found: (i) a swarm of ~20 wallets with near-identical stats (~120 prints each, integer share sizes 5/22/25/27/29/35,
  median $9/print, 746 bursts, median 1 print/burst, 98% same direction). It trades 0.1–4.4 s after a same-direction print by various
  wallets (top: `eebde7a0…` 77 of 746). It is too small to move a price (burst $ median 10, p90 286).
  (ii) Leader→follower pairs, e.g. `d15696ef…→70399ab9…` (2,092 after vs 736 before) and `50ea9d87…` followed by 6 wallets; `50ea9d87…` itself follows `eebde7a0…` and OhioAI.
- **PnL of following a leader's prints** (F1 sim joined on wallet, dublin, 5 s taker exit): `eebde7a0…` −2.9¢ (n 1,645); OhioAI −7.8¢;
  `d15696ef…` +0.2¢ (n 58); `84389cfc…` −4.0¢. Over all 293 wallets with ≥ 30 events: **4 / 293 positive at a 5 s taker exit (max +0.7¢)**,
  which is what noise looks like. Settle-hold results per wallet (e.g. `84389cfc…` +6.9¢, CI [+0.7, +13.0], n 52) are directional information
  picked from 293 tries, not a speed edge.
- **Identifiability:** the wallet arrives on the activity stream ~250 ms after the trade (p10 159 ms), and a print alone carries no wallet.
  Integer-share sizes fingerprint the swarm, but the swarm isn't worth front-running. Configs: 293 wallets × 2 sides × 4 exits looked at.
- Not taken to holdout: no dev candidate.

### F3 — fast maker at newly formed levels, book-only pulls — dev (first pass)
- **Event:** a book row where the UP bid rises or the UP ask falls (a new best level ≥ 5 shares) with ≥ 20 s left. We decide at +9 ms
  (88% of new levels are still best then) and post-only BUY 5 at that price (+22 ms). **One live order per side** (new levels are skipped while ours is live).
  Pulls (seen +9 ms, cancel lands +21 ms, not held): `out` (someone improves past us), `opp` (the opposite best moves toward us), `dep` (our level's
  size < 50% of its size when we joined), TTL 5 s. Exit: hold to settlement, or post-only +1 tick (10 s), then taker.
- Volume: ~6,600 new-level events per hour in BTC 5m; with the one-order-per-side gate, ~12–20 k orders/day.
- **Configs:** 2 rule sets × 3 spread filters × 2 price bands × 2 time-left × 4 exits × 3 models = 288 (96 per model).
- **Dev, dublin:** median net per filled order: **pess** settle −0.85¢, two-sided exit −2.21¢, markout 1 s −0.66¢;
  **prop** −0.65 / −1.98 / −0.50; **front** (reference only) +0.20 / −0.46 / **+0.38**.
  Best pess: join only when spread ≥ 2 ticks, all rules, > 60 s left, settle **+0.49¢ (n 4,011; CI market [−0.63, +1.62], day [−0.19, +1.19])**.
  Same picture as every earlier maker study: positive only with a queue-front assumption. Under pess/prop, fills arrive when price moves through
  the level (adverse selection ≈ 1¢ within 1 s), and the book-only pulls don't land in time to avoid them.

### F4 — stale quotes on the sibling book after a print — dev: CLOSED (as a speed edge)
- In Polymarket's unified book, UP and DOWN of one market are the same book (DOWN ask = 1 − UP bid), so the complementary token can't be stale
  on its own. The remaining candidates are deeper levels (that is just F1's taker paying the next level) and **sibling markets**. I tested BTC 15m after BTC 5m prints ≥ $50.
- **Dev (dublin, 33,985 events):** the 15m mid moves in the print's direction by ≤ +0.2¢ within 5 s (+0.1–0.6¢ at 30 s). 15m taker exits at
  +5 s / +30 s: **−2.5 to −3.4¢** in every size/sweep bucket. 24 configs.
- One settle-hold config is positive on dev: the first ≥ $50 5m print per 15m window, buy that direction on 15m and hold: **+7.4¢ (n 154; CI market
  [+0.8, +13.9])**. This is a directional bet held 0–15 min, not a quote that's stale for milliseconds. Frozen (`frozen/F4.json`) and checked once on holdout below.

### F5 — order-flow imbalance / microprice — dev: CLOSED for takers
- Signal I = (bid size − ask size)/(sum) on the UP top of book, sampled ≤ 1 per 250 ms per market (335,308 samples).
- **It predicts the next move well:** I > 0.8 → mid +0.38¢ at 100 ms, +1.23¢ at 1 s, +1.33¢ at 5 s (mirror for I < −0.8); monotone across buckets.
  The move is mostly the thin side being consumed: ~1 tick, of which the taker must pay half spread + 1.75¢ fee.
- **Taker** (|I| ≥ 0.8, spread 1 tick, buy the favoured token at its ask): exits at 1 s / 5 s **−2.83 / −2.59¢ (n 81k, CI ±0.05)**; settle −0.10 [−0.84, +0.62].
- Reused as a filter for the F3 maker (only join when our side is the heavy one); see F3 v2.

### F6 — timed bursts — dev
- **Timing (dev, prints):** taker flow is flat across the millisecond-in-second (no fixed-clock bots). Flow by 15-s block peaks at the open
  (0–30 s: ~100 prints, $1,040–1,100 per market per 15 s) and at **180–255 s (45–120 s left: $1,360–1,590)**. The single busiest second is **240 s
  (exactly 60 s left): $195/market, ~3× an average second.** In the half second after 240.0 s, **87–95% of taker $ buys the favourite**; the favourite's mid then rises ~+1.2¢ within 1 s.
  Top wallets at 240.0–240.5 s are mostly unknown $1–3 k clips (one or two prints each). 253 of 464 dev markets show the burst.
- **Setups simulated (`f6_sim.py`, 140 configs):** (A) maker pre-positions before the burst: at 239.0 / 239.7 s, sell the favourite at its ask + K ticks
  (K 0/1/2), cancel at 240.5 / 241.5 s; (C) taker fades the burst after seeing ≥ X $ of favourite buying (X 0/50/200, wait 0.25/0.5/1 s), buying the underdog;
  (F) taker follows it.
- **Dev:** (C) fade: settle −4.3 to +0.2¢, 30-s exit −1.2 to −4.0¢ → closed. (F) follow: +5.2¢ settle on n 32 (Wd 0.25 s, X 50), +1.9¢ on n 96 (Wd 0.5),
  negative elsewhere → noise. (A) maker at 239.0 s: settle **+2.4 to +3.5¢ under pess/prop**, but every market CI includes 0
  (pick: K 0, cancel 240.5 s, pess: **+3.35¢, n 150, 75 fills/day, CI market [−1.61, +8.80], day [+1.86, +4.32]**). Posting at 239.7 s is negative (−1.2 to −2.6¢).
  Most fills come at 239.0–239.5 s, i.e. from ordinary flow before the burst, so this is mostly "sell the favourite at the ask late in the window"
  (favourite–longshot carry), not burst-specific or speed-specific. Frozen (`frozen/F6.json`) for one holdout run.
- **Pitfall found:** 30-s mid markouts of these late fills looked like +13–15¢. That's survivorship bias: when the favourite runs to 98–99¢ the book
  goes one-sided, the mid is missing, and exactly the losing fills drop out. Settle-based numbers only for late-window fills.

### F3 v2 — imbalance-filtered join, and "improve by one tick" — dev → **CANDIDATE F3i frozen**
- Same events as F3. Added at the decision: own-side imbalance (own = I when we bid UP, −I when we offer UP, i.e. bid DOWN) and an
  **improve** mode: if the spread is ≥ 2 ticks, post one tick inside it instead of joining. We are then alone at a new price, so the queue model no longer matters (pess ≈ prop ≈ front).
- **Configs:** 2 modes × 2 rule sets × 4 imbalance filters × 2 spread filters × 3 exits × 3 models = 288 more (F3 total 576).
- **Join + imbalance (dev, 1 s markout):** pess −0.51¢ unfiltered → +1.07 / +1.34¢ with own > 0.3 / 0.6 and spread ≥ 2 ticks. Settle is noisy
  (own > 0.6: +2.08¢ but n 466, CI [−1.35, +5.42]).
- **Improve (dev):** 1 s markout pess +0.28¢ unfiltered, **+1.04¢ with own > 0.6**; own < −0.3 → −0.11¢. Monotone in the filter, as F5 predicts.
- **Frozen pick (`frozen/F3i.json`, 20:3x UTC, before any holdout read):** improve, own > 0.6, pulls out+opp(+dep, inert)+TTL 5 s,
  one live order per side, hold to settlement, pess.
  Dev: 7,799 orders, **2,866 fills (37%; 95% full 5 shares), ~1,430 fills/day, settle +2.04¢/share (CI market [+0.53, +3.60], day [+1.30, +2.85]),
  markout +1.04¢ at 1 s, +0.95¢ at 5 s.** Robustness on dev: 1 s markout +1.07 / +1.02¢ by day, +0.5 to +1.2¢ in every price band,
  +0.8 to +1.4¢ in every time-left bucket, positive in 23 / 24 hours. Settle by band: +4.8 / +2.9 / +3.6 / −0.4 / −1.5¢ for token price 0–20 / 20–40 / 40–60 / 60–80 / 80–100¢ (noisy).
  A two-sided exit (post +1 tick for 10 s, then taker) costs too much: −0.5¢. Pull timing: order live median 115 ms; fills median 111 ms after the level appears.

### F7 (own idea) — F3i + **alt-book pull trigger** — dev → **CANDIDATES F7i-A / F7i-B frozen**
- **Hypothesis:** a BTC maker's adverse fills come from takers sniping stale BTC quotes after an outside move. BTC books reprice 230–300 ms after
  Binance, the ETH/SOL/XRP/DOGE books 70–100 ms (speed atlas 2026-11-14). The alt books are Polymarket's own data, so we can pull a BTC quote when the
  alts move, ~150 ms before the snipers' orders match. Our cancel lands at alt row +9 +21 ms and isn't held; their takers are held 50 ms.
  (The earlier "alt-book-led BTC taker" study closed only the *taker* use of this lead.)
- **Trigger:** for each alt 5m market of the same window, dm = UP mid now − UP mid 500 ms earlier. If we bid UP on BTC, pull at the first alt row
  with dm ≤ −THR; if we offer UP (bid DOWN), at dm ≥ +THR. THR ∈ {0.5¢, 1¢}. Rule sets {out+ttl (control), out+alt+ttl, out+opp+dep+alt+ttl} × pess/prop × imbalance filter {none, own > 0.6}: **24 configs**.
- **Dev (dublin, improve mode):**

| rules | THR | filter | fills | 1 s markout | settle ¢/share [CI market] |
|---|---|---|---|---|---|
| out+ttl (no alt) | – | none | 18,393 | +0.27 | +0.22 [−0.19, +0.62] |
| out+alt+ttl | 0.5¢ | none | 13,670 | **+0.90** | **+1.25 [+0.63, +1.85]** |
| out+alt+ttl | 1¢ | none | 15,362 | +0.86 | +1.21 [+0.63, +1.79] |
| out+ttl (no alt) | – | own > 0.6 | 2,138 | +1.05 | +1.77 [+0.09, +3.39] |
| out+opp+dep+alt+ttl | 0.5¢ | own > 0.6 | 1,730 | **+1.75** | **+3.13 [+1.24, +5.03]** |
| out+opp+dep+alt+ttl | 1¢ | own > 0.6 | 1,919 | +1.68 | +2.90 [+1.20, +4.64] |

  pess and prop agree to ±0.02¢ (we're alone at our price). The alt pull roughly triples the markout of the unfiltered maker and
  raises the filtered one by ~70%. Pulls make orders short-lived (median live 38–67 ms vs 473 ms without), and fill rate drops 54% → 25%.
- **Frozen (`frozen/F7i.json`, before any holdout read):** **A (primary)** = out+opp+dep+alt+ttl, THR 0.5¢, own > 0.6;
  **B (volume)** = out+alt+ttl, THR 0.5¢, no imbalance filter. Both hold to settlement, pess.
- Join-mode F7 (does the alt pull rescue plain joining?) was killed twice by the 800 MB cap (rows held in memory); fixed with a per-hour flush and rerun (see below).

### `tokyo` comparison on dev (frozen picks only; book +116 / order +120 / cancel +120 ms, hold 50)
| pick | dublin | tokyo | reading |
|---|---|---|---|
| F1 follow ≥ $100 swept, settle | +0.49¢ [−1.25, +2.23] | **+1.32¢** [−0.25, +2.90] | no better at speed → not a speed edge |
| F3i improve own > 0.6, settle | +1.99¢ [+0.51, +3.47] | +1.43¢ [−0.10, +2.96] | settle noisy; **1 s markout +1.04¢ → +0.10¢**: the edge is speed |
| F4 15m after 5m ≥ $50, settle | +7.40¢ [+0.79, +13.90] | **+8.24¢** [+1.89, +14.62] | no better at speed → directional, not speed |
| F6 maker sell favourite at 239.0 s | +3.35¢ [−1.61, +8.80] | +1.09¢ [−3.98, +6.21] | worse, CIs wide |
| F1 grid medians (all 192 configs) | follow taker exits −3.4 to −3.7¢ | −3.4 to −3.5¢ | — |
| F7i-A (alt pull, own > 0.6), settle | +3.13¢ [+1.24, +5.03], 865 fills/day | **+4.20¢** [+2.20, +6.18], 640/day | markout 1 s +1.75 → +0.95¢; per-share settle does **not** shrink |
| F7i-B (alt pull, no filter), settle | +1.25¢ [+0.63, +1.85], 6,835/day | +1.22¢ [+0.34, +2.13], 3,490/day | markout 1 s +0.90 → +0.47¢; fills halve; per-share settle unchanged |

**Reading F7i under tokyo (important):** speed roughly doubles the 1 s markout and the number of fills, so $/day falls 50–75% under tokyo.
But the *per-share settlement P&L* does not shrink. By the brief's rule ("an edge that doesn't shrink sharply under tokyo isn't a speed edge"),
the settle component is mostly *not* a speed edge: the imbalance/microprice information persists to settlement and a slow maker earns it too.
The speed-dependent part is the short-horizon markout and the volume.

**F7 join mode (exploratory, run after the F7i freeze, not frozen; 20 more configs → F7 total 44):** the alt pull lifts the plain *join* maker
(queue behind the new level, pess) from a 1 s markout of −0.70¢ / settle −0.71¢ [−0.94, −0.47] (out+ttl, n 28,942) to **+0.42¢ / +0.42¢ [−0.24, +1.05]**
(out+alt+ttl, THR 0.5¢, n 13,983). With own > 0.6: settle +3.31¢ [+0.62, +5.77] (n 776). It is the first join-maker in these studies whose markout is
positive under pess. The alt pull removes most of the adverse selection; it doesn't create spread capture.

**Latency stress on dev (`dublin_p90`: book +23, order +52, cancel +69 ms).** 1 s markout, dublin → p90 → tokyo:
F3i +1.04 → +0.71 → +0.10¢; F7i-A +1.75 → +1.45 → +0.95¢; F7i-B +0.90 → +0.69 → +0.47¢ (all CIs tight, ±0.1–0.2¢).
Settle ¢/share: F3i +1.99 / +0.96 / +1.43; F7i-A +3.13 / +1.70 / +4.20; F7i-B +1.25 / +1.02 / +1.22. The markout degrades smoothly and
monotonically with latency, a speed signature. Settle is non-monotone (noise + slow information). Fills/day: F7i-B 6,835 / 6,003 / 3,490.

**F7i on BTC 15m (dev; triggers = ETH/SOL/XRP/DOGE 15m books; same frozen rules).** dublin: no alt pull 1 s markout −0.38¢ (settle +0.34 [−0.46, +1.16], n 5,761);
B +0.17¢ (settle +0.11 [−1.17, +1.51], n 3,092); A +0.81¢ (settle +2.37 [−2.57, +7.40], n 306). tokyo: −0.72 / −0.32 / +0.01¢, settle −1.0 / −1.0 / −3.8¢.
Same direction as BTC 5m, a third of the size, not significant on dev → NEEDS DATA (not taken to holdout).

**Control: same F7i rules with ETH 5m as the target** (triggers = BTC/SOL/XRP/DOGE 5m; dev). dublin: no pull 1 s markout −1.03¢, settle −1.36 [−1.74, −0.98] (n 18,347);
B −0.69¢ / −1.01 [−1.68, −0.34] (n 13,651); A −0.22¢ / +0.90 [−1.88, +3.86] (n 721). tokyo: −1.13 / −1.10 / −1.15¢.
The improve-maker *loses* on ETH and cross-book pulls barely help. ETH's book is among the fastest to reprice, so no other book leads it.
That's what the mechanism predicts and supports the reading that F7i on BTC rests on the alt→BTC repricing lag, not on a quirk of the maker sim.

**Placebo control for the alt pull (dev, dublin):** same rules, but pull on alt moves in the *favourable* direction (same kind of trigger, wrong sign).
| variant | fills | live (ms, median) | 1 s markout | settle ¢/share [CI market] |
|---|---|---|---|---|
| control out+ttl (no alt) | 18,393 | 473 | +0.23 | +0.20 [−0.22, +0.59] |
| **real alt pull, B** | 13,670 | 47 | **+0.88** | **+1.25 [+0.62, +1.85]** |
| placebo alt pull, B | 11,393 | 19 | −0.53 | −0.79 [−1.52, −0.06] |
| **real alt pull, A** | 1,730 | 45 | **+1.76** | **+3.18 [+1.25, +5.10]** |
| placebo alt pull, A | 1,229 | 16 | +0.33 | +2.36 [+0.14, +4.61] |

The placebo is *worse* than no pull: it removes the favourable fills and keeps the adverse ones. The gain is in the *direction* of the alt move, not in pulling more often.

**Fill-model realism fix (`dsim_strict.py`):** dsim's maker fills our whole remaining order when a print trades *through* our price. That's right when our order really
sat in the book, but our order is a phantom absent from history, and in improve mode every fill is a through-print. Strict variant: a through-print fills at most its own
size (minus queue ahead). Dev, BTC 5m: at 5 shares the change is small (shares/fill 4.9 → 4.6; F7i-B settle +1.25 → +1.27¢, A +3.18 → +2.96¢, F3i +1.99 → +1.92¢,
markouts −0.03¢). **At 25 shares (strict):** 15–17 shares/fill; F7i-B +1.24¢/share [+0.56, +1.92] (≈ $1.4k/day on dev), A +2.44¢ [+0.14, +4.70];
the no-alt control turns negative (−0.14¢). Holdout is reported under both models. This change of fill model after freezing is a realism correction, not a parameter choice.

**More F7i realism checks (dev):** (i) receive lag per asset (pm rows, three sample hours of 11-15): p50 9.2–11.7 ms and p90 15–33 ms for BTC and every alt alike
(p99 60–500 ms in busy hours). The sim's flat +9 ms for alt rows isn't optimistic at the median, and the `dublin_p90` scenario covers the p90.
(ii) F7i-B 1 s markout by day-hour: **positive in 39 / 39 hours** (min +0.04, median +0.91, max +1.47¢); settle positive in 28 / 39. By token price band
(0–20 / 20–40 / 40–60 / 60–80 / 80–100¢): markout +0.95 / +0.74 / +1.03 / +1.02 / +0.65¢, settle +1.2 / +1.3 / +1.6 / +0.6 / +2.0¢.

### HOLDOUT — 2026-11-16 (00–23 h), frozen picks only, run once at 01:10–01:47 UTC 2026-11-17
Output: `~/overnight-wt/out/pmlat/holdout-2026-11-16.txt` (dsim fill model as frozen) and `holdout-2026-11-16-strict.txt` (strict through-print fills).
Net = ¢/share, share-weighted; CI = 90% bootstrap by market. **A day-bootstrap CI can't be computed on a 1-day holdout.** The dev day-CIs are in the frozen files;
the market CI is the only holdout CI, and it ignores day-to-day regime risk.

| pick | scenario | fills/day | net ¢/share [CI market] | 1 s markout | $/day @5 sh | gate |
|---|---|---|---|---|---|---|
| **F7i-A** (improve + own > 0.6 + alt pull) | dublin | 1,008 | **+3.52 [+0.78, +6.20]** | +1.87 [+1.57, +2.18] | +169 | **PASS** |
|  | dublin strict | 1,008 | +3.52 [+0.83, +6.21] | +1.82 | +162 | pass |
|  | dublin_p90 | 836 | −0.19 [−2.73, +2.47] | +1.44 | −8 | fails |
|  | tokyo | 495 | +0.66 [−2.81, +4.00] | +0.03 | +16 | clearly worse ✔ |
| **F7i-B** (improve + alt pull, no filter) | dublin | 7,415 | **+1.85 [+0.99, +2.74]** | +0.81 [+0.70, +0.93] | +663 | **PASS** |
|  | dublin strict | 7,415 | +1.71 [+0.88, +2.59] | +0.77 | +590 | pass |
|  | dublin_p90 | 5,499 | +0.83 [−0.09, +1.72] | +0.59 | +222 | fails CI |
|  | tokyo | 2,084 | −0.77 [−2.66, +1.13] | +0.09 | −79 | clearly worse ✔ |
| F3i (improve + own > 0.6, no alt) | dublin | 1,561 | +1.58 [−0.55, +3.62] | +1.04 [+0.83, +1.25] | +120 | **fails** (CI low < 0) |
|  | tokyo | 995 | −1.37 [−3.77, +0.88] | −0.77 | −67 | |
| F1 follow ≥ $100 swept, settle | dublin / tokyo | 1,085 / 1,066 | −1.83 [−4.08, +0.28] / −1.85 | – | −100 | **fails** |
| F4 15m after 5m ≥ $50, settle | dublin / tokyo | 94 / 94 | +1.39 [−7.17, +9.55] / +1.49 | – | +7 | **fails** |
| F6 maker sells favourite at 239.0 s | dublin / tokyo | 77 / 69 | +1.32 [−5.27, +8.46] / +1.01 | – | +5 | **fails** |

**Reading.** Of 6 frozen picks, the two F7i picks pass every gate on the holdout day: net > 0 with market-CI low > 0 under pess (and under the strict fill model),
tokyo clearly worse (markout +1.87 → +0.03¢ and +0.81 → +0.09¢; settle +3.52 → +0.66¢ and +1.85 → −0.77¢), and ≥ 20 trades/day.
Unlike dev, on the holdout the settle P&L also collapses under tokyo. F3i (same maker without the alt pull) keeps its markout (+1.04¢), but its settle CI includes 0.
**Latency sensitivity is the main risk:** at this box's p90-ish delays (book +23, order +52, cancel +69 ms) F7i-A's settle goes to −0.19¢ and F7i-B's to +0.83¢ (CI includes 0),
although the markouts stay positive (+1.44 / +0.59¢). The edge needs the median Dublin path, not the tail.

### Multiple comparisons (everything looked at tonight)
F1 192 configs · F2 293 wallets × 2 sides × 4 exits ≈ 2,344 wallet screens (+ swarm/pair tables) · F3 576 · F4 24 · F5 3 taker configs + 7 imbalance buckets ·
F6 140 · F7 44 (+ 6 BTC 15m, 6 ETH-control, 6 placebo, size-25 and strict re-runs as robustness, not selection). **≈ 980 strategy configs + ~2,300 wallet screens on dev;
6 frozen picks taken to holdout once; 2 passed (F7i-A, F7i-B, which are the same family and highly correlated).** With 6 holdout tests at a one-sided 5% level,
~0.3 false passes are expected by chance. F7i is supported beyond its p-value by four structural checks that its mechanism predicts: the placebo pull hurts;
the ETH-target control loses; the markout falls monotonically with latency (dublin > p90 > tokyo) on dev and holdout; and it's positive in 39/39 dev hours.
It is still **one holdout day**. Treat it as a candidate, not a strategy.

## 4. Ranked candidates (frozen rules for a 14-day shadow)

**#1 — R-D4 = F7i-B, "BTC 5m improve-maker with alt-book pulls" (volume version).** Frozen: `~/overnight-wt/frozen/F7i.json` (B) + `frozen/F3i.json` (base).
- Market: Polymarket BTC 5m up/down, both tokens, from the Dublin box.
- Event: a book row where the UP best bid rises or the UP best ask falls (new best level ≥ 5 shares), ≥ 20 s left in the window.
- Entry: at row + book delay, if the spread is ≥ 2 ticks and the new level is still best, post-only BUY 5 shares one tick inside the spread on that side
  (UP bid + 1 tick, or DOWN bid + 1 tick = UP ask − 1 tick). One live order per side.
- Pull (cancel immediately; cancels aren't held) at the first of: someone improves past our price; **any of the ETH/SOL/XRP/DOGE 5m UP mids moves ≥ 0.5¢ against us
  within 500 ms** (against = down when we hold a bid on UP, up when we hold a bid on DOWN); 5 s TTL.
- Exit: hold to settlement. Inventory per market is naturally ≤ a few fills per side; UP+DOWN pairs merge to $1.
- Dev / holdout: +1.25 / **+1.85¢/share** (strict +1.27 / +1.71), 6,835 / 7,415 fills/day, 1 s markout +0.90 / +0.81¢; tokyo −0.77¢ on holdout.
- **Pass** (all after ≥ 14 counted days): ≥ 500 fills, net ≥ +1.0¢/share, day-bootstrap 90% CI lower > 0, *and* mean 1 s markout ≥ +0.4¢.
  **Kill:** day-CI upper < +1.0¢ after 14 days, or 1 s markout ≤ 0 on 3 consecutive days (the mechanism has gone away).

**#2 — R-D5 = F7i-A (selective version: own-side imbalance > 0.6, pulls also on opposite-best move toward us; THR 0.5¢).** Frozen: `frozen/F7i.json` (A).
- Dev / holdout: +3.13 / **+3.52¢/share** (strict +2.96 / +3.52), 865 / 1,008 fills/day, markout +1.75 / +1.87¢; tokyo +0.66¢ (markout +0.03).
- **Pass:** ≥ 14 days, ≥ 500 fills, net ≥ +1.5¢/share, day-CI lower > 0, 1 s markout ≥ +0.8¢. **Kill:** day-CI upper < +1.0¢ after 14 days, or 1 s markout ≤ 0 on 3 consecutive days.

**How to shadow it honestly.** A nightly *recorded-data* eval (like R-D1) of exactly `f7_altpull.py DAY dublin improve` + `holdout_eval.py` (strict and dsim) gives a
stationarity test over 14 days and is cheap (runs under 800 MB, ~5 min/day). It can't test what matters most for a maker that improves the book: how other bots
react to our order (penny-jumping, takers routed to it, cancels). Only real post-only orders measure that. That is trading, so it's the operator's call, not mine.
If approved later, a 5-share live probe with the same rules and an explicit loss cap would be the next step.
Also required: the p90 sensitivity says the bot must hit the *median* Dublin path (decide ≤ 10 ms after the alt row, cancel landing ≤ 30 ms).

**Not candidates:** F3i (holdout CI includes 0; superseded by F7i), F1, F2, F4, F5, F6 (closed with numbers above).
**NEEDS DATA:** F7i on BTC 15m (same sign on dev, n too small); F7 join mode (markout +0.42¢ under pess, CI includes 0).

## 5. Infrastructure notes
- **Code:** `~/overnight-wt` = git worktree of `~/lighter-flips`, branch **`research/overnight-1116`** (local only, not pushed: no credentials).
  The live services' checkout `~/lighter-flips` (branch `research/shadow-trades`) was not switched or modified. Final commit hash: see §5 last line.
  `dsim.py` is imported from `~/dublin-retest/repo` (branch `research/dublin-retest`, untouched).
- **New scripts:** `pmlat.py` (hour-streaming loader, scenarios, bootstrap), `f1_events.py` (print event table), `f1_sim.py`/`f1_grid.py`,
  `f2_leaders.py`, `f3_maker.py`/`f3_grid.py` (join / improve maker, `PMLAT_SIZE`), `f4_cross.py`, `f5_imbalance.py`, `f6_bursts.py`/`f6_sim.py`/`f6_grid.py`,
  `f7_altpull.py` (alt-book pull trigger, any asset/tf/hours), `holdout_eval.py` (frozen picks only), `frozen/*.json` (timestamped picks),
  runners `run_*.sh` + `guard.sh` (no job starts 00:12–01:10 UTC or while a nightly eval is active; one job at a time; `MemoryMax=800M`).
  Outputs (not committed, 239 MB): `~/overnight-wt/out/pmlat/` (event tables, sims, grids, `holdout-*.txt`).
- **What the recorders should capture to test F7i properly (and shadow it):**
  1. **Our own orders in the book.** The sim's biggest gap is that our improve orders are absent from the recorded book, so other bots' reactions to them
     (penny-jumping, cancels, takers routed to us) are unmodelled. Only a live paper/shadow with real post-only orders measures this. A 14-day *paper* shadow can't.
  2. **Full depth and `price_change` deltas for BTC and the alt 5m books** in updown-rec (today: UP top of book only; 5 levels exist only for BTC in feeds-rec).
     F7i's trigger uses alt mids only, so top of book is enough for the trigger, but queue / size at our price needs depth.
  3. **Server `timestamp` vs local receipt per message for every book**: the alt-vs-BTC lead is measured on exchange ms; a per-connection lag
     histogram per asset would confirm we receive the alt books as fast as the BTC book (they arrive on separate sockets).
  4. feeds-rec: fix the memory growth (OOM after ~29 h at its 500 MB cap) and make its readers handle concatenated / truncated gzip members.

## 6. Problems hit, and what I did
- **feeds-rec OOM at 19:56:56 (before I started):** its own 500 MB cap. ~8 s gap (19:56:53.6 → 19:57:01.8) and a truncated gzip member in every 11-16 `*-19.jsonl.gz` (details in Part A). Not touched.
- **Operator note 20:10** (MemoryMax 800M, one job at a time, report the feeds-rec gap): followed from then on. All later jobs ran one at a time under `MemoryMax=800M`,
  except a few small read-only post-processing reads (< 300 MB) of finished outputs while a sim job ran.
- **My own jobs killed by their 800 MB cap, twice** (20:47, 20:50: F7 join mode held ~750k rows in memory). Only my process died, inside its scope. Fixed with a per-hour flush; rerun OK.
- **`pkill -f` matched my own shell twice** (killed the controlling shell, not any service); relaunched the affected runners by PID. No service affected.
- **Nightly evals OOM (00:20 breakout-eval, 00:25 momentum-eval):** not caused by my jobs (`guard.sh` kept me idle 00:12–01:10). R-D1 and R-D2 aren't accumulating days (Part A).
- **Methodological traps found and handled:** (i) late-window mid markouts have survivorship bias (one-sided books near resolution → missing mids), so settle is used for late fills (F6);
  (ii) dsim's maker fills a phantom order completely on any through-print. I added `dsim_strict.py` and report both models.
- **Data limits:** dev day 11-14 is partial (books from 09:00, census from 21:41). The holdout is a single day, so no day-bootstrap CI. Only the UP top of book is recorded for the alt books
  (enough for the F7 trigger). No data was missing in BTC 5m hours used. Gamma winners: 450 fetched, cached in `out/pmlat/winners.csv` (the shared `~/dublin-retest/winners.csv` was only read).
- Rules: no orders, no signing, no credentials read; no live service stopped, restarted or edited; R-D3 not read; no Binance data used tonight.

**Holdout descriptives (post hoc, not used for selection), F7i-B strict:** 1 s markout positive in **24 / 24 hours** (min +0.08, median +0.97, max +1.80¢);
settle positive in 19 / 24 hours. By token price 0–20 / 20–40 / 40–60 / 60–80 / 80–100¢: markout +0.96 / +0.91 / +0.87 / +0.78 / +0.66¢; settle +4.2 / +5.9 / +2.9 / **−3.7** / −1.2¢
(on dev the 60–80¢ band was +0.6¢). A "skip buying ≥ 60¢" filter is tempting but would be a post-holdout selection; don't add it without a new test.
- **Final commit:** `~/overnight-wt` (worktree of `~/lighter-flips`), branch `research/overnight-1116`, commit **`fe49081`** (local only; no push credentials).
- **Ready-to-install shadow for R-D4/R-D5 (not installed, operator's call):** `~/overnight-wt/deploy/rd45_eval.sh` + `rd45-eval.service` / `.timer` (01:20 UTC, after the other evals,
  `MemoryMax=900M`; evaluates each finished day from 2026-11-17 under the dsim and strict fill models) and `rd45_report.py` (day-bootstrap pass/kill exactly as §4;
  smoke-tested on 11-16, where it reproduces the holdout numbers). Install: `sudo cp ~/overnight-wt/deploy/rd45-eval.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now rd45-eval.timer`.

### EXTRA out-of-sample check — 2026-11-17 00:00–05:00 (5 hours, same frozen picks; run 05:03–05:06)
Output: `out/pmlat/holdout-2026-11-17-h00-04.txt` (+ `-strict`). Counts are per 5 hours, not per day.

| pick | scenario | fills (5 h) | settle ¢/share [CI market] | 1 s markout [CI] |
|---|---|---|---|---|
| F7i-B | dublin | 2,284 | +0.26 [−1.45, +1.88] (strict +0.22) | **+0.57 [+0.38, +0.76]** |
| F7i-B | tokyo | 990 | **+2.87 [+1.05, +4.78]** (strict +3.17) | +0.24 [−0.05, +0.53] |
| F7i-A | dublin | 348 | +3.15 [−2.07, +8.63] (strict +3.12) | **+1.11 [+0.54, +1.70]** |
| F7i-A | tokyo | 208 | +5.93 [+0.58, +11.73] | +0.48 [−0.31, +1.31] |
| F3i | dublin / tokyo | 514 / 287 | +5.01 [+0.38, +9.40] / +5.17 [+1.38, +9.04] | +0.88 / +0.11 |

**Reading (honest):** the **speed component replicated a third time**: the 1 s markout is positive under dublin and roughly halves or vanishes under tokyo.
**Settlement P&L did not:** in these 5 hours F7i-B under dublin made +0.26¢ (CI includes 0), while the slower tokyo variant made +2.87¢. Together with dev,
where per-share settle didn't shrink under tokyo either, this says **the settle P&L is mostly not a speed edge**. It's noisy directional carry from the
imbalance/microprice side, and only the ~+0.6–1.9¢ short-horizon markout is clearly bought by speed. The holdout-day pass (+1.85¢, CI > 0) is one day.
**Net verdict for F7i: CANDIDATE (speed-dependent markout, robust across 3 samples) with an unproven settle P&L.**
The R-D4/R-D5 pass criteria in §4 already require both (settle net and CI over 14 days, *and* the markout floor). A 14-day shadow is how to resolve it.
