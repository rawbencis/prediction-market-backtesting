# Tokyo 2026-11-27: Pascal × Polymarket and the Lighter fast-pull maker

Branch `research/pascal-lighter` (from `research/lp-arb` @ df440e0). Paper research only: no orders, no keys, no accounts.

## Verdict

<!-- VERDICT -->

**Operator action needed: disk.** <!-- DISK -->

---

## P0. Venue checks

### Pascal (docs `docs.pascal.trade/llms/{general,api}.txt`, read 11-27 19:29 UTC; public endpoints)

| item | finding | status |
|---|---|---|
| Rounds and priority | Orders batched into rounds **every 50 ms**; within a round: (1) cancels, (2) post-only place/replace, (3) everything else (IOC, GTC non-post-only, GTT, market); FCFS within a group by write-API receipt. A cancel naming a `client_order_id` placed later in the same round applies right after that placement. "Comparable to Hyperliquid." | confirmed |
| Taker hold | None documented (no delay, speed bump or hold anywhere in either doc). | confirmed (none found) |
| Order types | `GTC`, `GTT` (`expires_ts_ms`), `IOC`; `post_only`, `reduce_only`; display type `LIMIT` / `MARKET` (MARKET = IOC with a limit). Atomic replace via `replace_client_order_id` (keeps queue position if it only reduces size), `replace_with_open_size`, mass cancel. Order-entry WebSocket keeps submission order. | confirmed |
| Minimum order size | Size is an integer number of contracts (`u64`): minimum 1 contract. No minimum notional documented. Tick: `max(tick_size_min, 10^(floor(log10(min(p,1-p)))+1-tick_sig_figs))`; live markets use `tick_size_min 0.001`, `tick_sig_figs 3` (0.001 ticks near 50¢). Max 50 resting orders per account per market. | confirmed |
| Read rate limits | "Some read endpoints have their own lower limits" → HTTP 429 `RATE_LIMITED`; **no figures given** for reads. Order REST: 100 req/s, 50 orders/batch; order WS: 1,000 msg/s, 128 in flight. Market-data WS: 10,000 connections, 5,000 subscriptions per connection. We never saw a 429 at ≤ 2 req/s. | different (no read figures) |
| Book messages | `book` channel: first message a full **snapshot** (with `MarketSpec`), then **price-level deltas** (`size = 0` removes a level). Every message carries the global `seq` (non-contiguous per channel), `state_time_ms` (matching-engine time as of the last round) and `server_time_ms`. REST `/books` may be up to 1 s old. | confirmed |
| Trades expose both wallets | Yes: `PublicTradeMsg` has `maker` and `taker` owner wallets, `taker_side`, `seq`, `trade_ts_ms`, and `maker/taker_position_size_prev`, `maker/taker_reduce_only`. | confirmed |
| Historical trades | `GET /api/v1/trades?symbols=…&limit≤500&before_cursor=…` (or `at_or_before_seq`), up to 120 symbols; `HISTORY_NOT_READY` (503) beyond indexed history. In P2 every market paged to its **first trade** (last page reached, no `HISTORY_NOT_READY`): history reaches back to listing (see P2). | confirmed |
| Feed delay to this box | 5-minute window at 19:36–19:41 UTC on 150 markets: **recv − `state_time_ms` p50 6.8 ms (p10 5.8, p90 9.1)**, **recv − `server_time_ms` p50 3.7 ms**; WS ping round trip **5–6 ms**. Clock: chrony, 0.3 µs off NTP. Stable since (recorder minute logs: p50 6.6–7.6 ms). For comparison Polymarket on the same box: p50 119–124 ms. | measured |
| Crypto price markets | **No crypto up/down and no short-dated crypto price market.** Crypto-tagged (72) markets are: FDV-after-launch ladders (Extended, Papertrade, Predict.fun; one day after a token launch), Lighter LIT "price hit in 2026" ladder (`LIT_PRICE_2026.*`, fee 0.02, resolves 2026-12-31), `BTC_GOLD_SPX_2026` (best 2026 performer, 0.02, 2026-12-31), token/airdrop-by-date markets (Polymarket, Predict.fun, Hyperliquid), Kraken valuation, Clarity Act (0.01). Stock closes: `META_CLOSE_26NOV_ABOVE.560`, `SPCX_CLOSE_26NOV_ABOVE.110` (0.02, resolve 2026-11-30). | confirmed (none) |
| Restricted jurisdictions | **Not fetched.** The only terms document is `https://app.pascal.trade/terms.pdf` (linked from the docs' rewards section); `app.pascal.trade` is off limits under rule 1, and neither the docs nor `pascal.trade` carry a jurisdiction list. The API has `GEO_RESTRICTED` and `ACCOUNT_RESTRICTED` error codes. The brief's third-party list stands unverified. | not found (rule 1) |
| Fees | Taker `size × p × (1 − p) × taker_fee_rate`; maker rebate = `maker_rebate_share` × taker fee. On 11-27: 3,469 markets 0.02 / 0.25, 44 markets 0.01 / 0.50. | confirmed |
| Polymarket mapping | `display_attributes.reference = {kind: polygon, condition_id, market_outcome_token_id, reverse_outcome_token_id}`; Pascal long = 1.0 iff the `market_outcome_token_id` wins. 3,379 of 3,513 live markets carry it (more than the 2,600 the third-party review counted). Resolution: Pascal follows the referenced venue's resolution, immutably; it can deviate only "when following is impossible" (e.g. Polymarket reverses trades). | confirmed |
| Midterms rewards | A 304K USDC rewards campaign ran 2026-09-17 → 11-05 (weekly pools on volume and open positions); ended before this study. | context |

### Lighter (docs `docs.lighter.xyz`, read 11-27 19:44 UTC; `feeTiers?filter=bo`, `events?active=true`)

| item | finding | status |
|---|---|---|
| Binary fees | Per-market max rates from `events`: Up/Down series **taker 1 %, maker 0 %**, charged as `marketFee × bo_fee_rate × size × P × (1 − P)` (1¢ × p(1−p) taker, 0 maker). `bo_fee_rate` = volume tier (bo_0 1.0 … bo_7 0.6 at $2M) × LIT staking tier (0.975 … 0.7). | confirmed |
| Account tiers | Premium/Advanced: **maker and cancel latency 0 ms, taker latency 140 ms**, 4,000–72,000 tx/min by stake; Plus: 300 ms taker, 300 ms cancel/modify, 0 ms maker; Standard (default): **300 ms taker, 300 ms cancel/modify, 0 ms maker**. | confirmed |
| Premium fees on binaries | The tier table's fees (0.004 % / 0.028 %) are **perps/spot only**; the docs say explicitly that the tier tables "do not determine binary options fees". The **latency** figures are per account tier and are not stated as perps-only; no binary-specific latency is given. So: figures are for perps fees; latencies apply account-wide as far as the docs say. | perps-only fees (said plainly) |
| Premium cost | No stake required (0-LIT row exists); switch via `changeAccountTier` (downgrade after a 24 h cooldown). Premium is in the **volume-quota program**: every `CreateOrder` / `ModifyOrder` / `CancelAllOrders` consumes 1 quota; quota is earned at 1 per $2 of fill notional by default ($1 on listed perps; binaries not listed → $2); **single-order cancels are free and unlimited**; one free tx per 15 s. Pending-order limits: premium 2,000 per account / 160 per market (standard 200 / 16). | confirmed |
| 1h up/down | Live: `btc-updown-1h` and `eth-updown-1h` (3 windows listed each, recurrence 3,600 s). **Not recorded here**: `lighter_rec.py` subscribes only `btc-updown-15m` / `eth-updown-15m`. | confirmed |

**1h settlement rule, Lighter vs Polymarket.** Lighter's 1h market (`btc-updown-1h`): "Up if the chainlink BTC price at the end of the 1h window is at least the one at its start" (Chainlink feed `0x0002ee67…`, `provider: chainlink`) — a point-to-point rule, and if no outcome can be determined it settles at 0.50 to both sides. Polymarket's 1h market (`btc-updown-1h-1795806000`, read 11-27): "Up if the **time-weighted average price (TWAP)** of Bitcoin, generated by Chainlink, of the time range … is greater than or equal to the price at the beginning of that range", source the Chainlink BTC/USD TWAP 60 s stream. The two pay differently whenever the end price and the hour's TWAP are on opposite sides of the start price, so a Lighter × Polymarket 1h pair is **not** a $1 lock.

---

## P1. Pascal census and mapping (snapshot 2026-11-27 19:30–19:50 UTC)

Files: `reports/tokyo/pascal_census_20261127.md` (full tables), `…_20261127.csv` (one row per mapped market). Pascal and Polymarket books were read in interleaved 50-market batches (each pair within ~1 s). Polymarket fee = Gamma `feeSchedule.rate × p(1 − p)` when `feesEnabled`; verified against the CLOB market info (`fd.r`) on one market of every (category, fee type): equal everywhere; markets without a schedule have no `fd` (fee 0). Only one of those markets had the taker-delay flag (`itode`: AMZN).

<!-- CENSUS -->

**What the books look like.** Pascal's mapped books replicate Polymarket's: at the same best bid price (788 of 2,661 two-sided pairs) the median Pascal size is **0.94×** Polymarket's (p10 0.29, p90 1.59), and the top 20 markets show the same prices to the tick on both venues. A mirroring maker quotes Pascal off the Polymarket book, so gaps sit at about minus both fees by construction. The live question for PX is therefore how fast that mirror updates when Polymarket moves, which the recorder measures.

**MECE.** Only one Pascal event is flagged `is_mece` (FED_DECISION_26DEC, 5 outcomes): buy-all costs $1.028 on Pascal and $1.010 on Polymarket. PX-C has one event to work with.

---

## P3. Recorder

<!-- RECORDER -->

---

## P4. Frozen PX plan

`frozen/PX_plan.json`, committed and pushed at **51f2bf5** (code `pascal/px_replay.py` @ a018011) at 20:05 UTC, before any recorded full day existed. It holds the rules, cells (18 = 12 PX-A + 3 PX-B + 3 PX-C; PM 1 s as a sensitivity), primary set, splits, pick and pass criteria as in the brief, plus two conventions the brief left open: Pascal round phase on multiples of 50 ms, and the Polymarket reverse-token ask taken as 1 − long bid (checked: 92 % of paired updates mirror exactly; the rest only by message timing).

Synthetic checks (`pascal/tests/test_px_replay.py`, 5 tests, pass):

| planted case | Pascal first | PM first | together |
|---|---|---|---|
| flat books | no attempt | no attempt | no attempt |
| 3¢ gap after fees, 5 s, on Polymarket | +3.0¢/share | +3.0¢/share | +3.0¢/share |
| 3¢ gap, 100 ms, on Polymarket | −3.0¢/share (Pascal filled, hedge gone, unwound) | 0 (PM leg never fills) | −3.0¢/share |
| 2.5¢ gap, 100 ms, on Pascal | +2.5¢/share (Pascal is 2 ms away) | −2.0¢/share | +2.5¢/share |

PX-B fill rule checked separately (joining the best: a sell at our bid does not fill us, one through it does, at our price).

**Dates.** Recorder started 2026-11-27 19:35 UTC → dev = **11-28, 11-29, 11-30**; holdout = **12-01 … 12-07**.

```bash
# after 2026-12-01 00:05 UTC — cache then dev (one heavy job at a time)
cd ~/pascal-lighter
systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 ~/shadow-venv/bin/python pascal/px_replay.py cache 2026-11-28 2026-11-29 2026-11-30
systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 ~/shadow-venv/bin/python pascal/px_replay.py run 2026-11-28 2026-11-29 2026-11-30 --tag dev
#   sensitivities (descriptive): ... run <dev days> --tag dev --pm-lat 1000 ; ... run <dev days> --tag dev --set inplay
#   then write frozen/PX_pick.json (the printed pick, or "no pick") and push it
# after 2026-12-08 00:05 UTC and only after PX_pick.json is pushed — read once
systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 ~/shadow-venv/bin/python pascal/px_replay.py cache 2026-12-01 2026-12-02 2026-12-03 2026-12-04 2026-12-05 2026-12-06 2026-12-07
systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 ~/shadow-venv/bin/python pascal/px_replay.py run 2026-12-01 2026-12-02 2026-12-03 2026-12-04 2026-12-05 2026-12-06 2026-12-07 --tag holdout
```

---

## L1. Lighter fast-pull maker

<!-- L1 -->

---

## P2. Pascal trade history against Polymarket

<!-- P2 -->

---

## Disk

<!-- DISKDETAIL -->

## Deviations from the brief

<!-- DEVIATIONS -->
