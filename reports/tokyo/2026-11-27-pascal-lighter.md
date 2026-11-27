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

Mapped Pascal markets: 3379; found on Gamma: 3378; orientation: {'yes_is_long': 3378, None: 1}; PM closed: 0

**Capacity gate (fails):** mapped markets with Pascal 24 h taker notional >= $1K: **4** (gate: >= 10); total Pascal 24 h taker notional in mapped markets: **$12,338** (gate: >= $10K). Gate FAILS.

| category | mapped | with 24h vol | Pascal 24h $ | Pascal all-time $ | PM 24h $ | median days to res | Pascal fee | PM fee types |
|---|---:|---:|---:|---:|---:|---:|---|---|
| Sports | 1866 | 23 | 5,214 | 30,655 | 11,683,145 | 2 | {0.02: 1866} | {('sports_fees_v3', 0.05): 1866} |
| Politics | 662 | 18 | 4,643 | 176,602 | 5,875,073 | 34 | {0.02: 652, 0.01: 10} | {('politics_elections_fees', 0.05): 253, ('politics_fees', 0.01): 204, (None, 0.0): 167} |
| AI/Tech | 360 | 9 | 1,181 | 35,332 | 656,330 | 34 | {0.02: 360} | {('tech_fees', 0.05): 204, ('culture_fees', 0.01): 84, ('finance_prices_fees', 0.05): 64} |
| Crypto | 63 | 2 | 981 | 88,439 | 137,830 | 307 | {0.02: 45, 0.01: 18} | {('crypto_fees_v2', 0.01): 53, ('finance_prices_fees', 0.05): 9, ('politics_fees', 0.01): 1} |
| Geopolitics | 61 | 9 | 319 | 21,318 | 557,078 | 34 | {0.02: 56, 0.01: 5} | {(None, 0.0): 40, ('politics_fees', 0.01): 15, ('economics_fees', 0.01): 6} |
| Finance/Macro | 226 | 1 | 0 | 223,906 | 4,503,930 | 34 | {0.02: 226} | {('economics_fees', 0.01): 118, ('finance_prices_fees', 0.05): 101, ('politics_fees', 0.01): 6} |
| Culture | 133 | 0 | 0 | 0 | 152,409 | 55 | {0.02: 133} | {('culture_fees', 0.01): 104, ('general_fees', 0.05): 29} |
| Other | 8 | 0 | 0 | 139 | 2,935 | 49 | {0.02: 8} | {('weather_fees', 0.01): 6, (None, 0.0): 2} |

Pascal 24 h volume concentration: top 1 22%, top 5 67%, top 20 96%; markets with any 24 h trade: 62; all-time Pascal notional in mapped markets $576,391.

Top 20 by Pascal 24 h notional:

| symbol | cat | days | P 24h $ | P trades | P bid/ask (size) | PM long bid/ask (size) | PM 24h $ | P fee | PM fee | gap LP / SP (¢) |
|---|---|---:|---:|---:|---|---|---:|---:|---:|---|
| NBA_26NOV27_MIANYK_SP.NYK_3 | Sports | 0.4 | 2,690 | 10 | 0.457/0.465 (10/2398) | 0.460/0.470 (1252/10200) | 201,727 | 0.02 | 0.05 | -2.2 / -3.0 |
| TAOYUAN_MAYOR_2026.CHANG | Politics | 0.9 | 2,379 | 2 | 0.910/0.920 (5/1715) | 0.910/0.920 (10201/2339) | 75,828 | 0.02 | 0.05 | -1.6 / -1.5 |
| SPAIN_ELECTION_2026.PP | Politics | 2.2 | 1,294 | 1 | 0.731/0.740 (6/2500) | 0.730/0.740 (55088/3491) | 71,861 | 0.02 | 0.05 | -2.4 / -2.3 |
| NBA_26NOV27_MEMMIN_SP.MIN_4 | Sports | 0.4 | 1,027 | 5 | 0.440/0.448 (11/76) | 0.440/0.450 (6316/6406) | 63,694 | 0.02 | 0.05 | -2.5 / -2.7 |
| EXTENDED_FDV_1D_ABOVE.150M | Crypto | 34.4 | 828 | 5 | 0.450/0.470 (82/478) | 0.450/0.470 (160/674) | 4,582 | 0.01 | 0.01 | -2.5 / -2.5 |
| TAOYUAN_MAYOR_2026.HUANG | Politics | 0.9 | 753 | 1 | 0.085/0.091 (42/949) | 0.086/0.090 (45/1069) | 122,071 | 0.02 | 0.05 | -1.1 / -1.1 |
| NBA_26NOV27_ATLBOS_SP.BOS_8 | Sports | 0.4 | 499 | 2 | 0.510/0.517 (5000/9) | 0.510/0.520 (10607/17143) | 22,907 | 0.02 | 0.05 | -2.4 / -2.7 |
| NBA_26NOV27_PHIIND_SP.PHI_2 | Sports | 0.4 | 289 | 2 | 0.525/0.532 (1794/1449) | 0.520/0.530 (12677/1526) | 15,620 | 0.02 | 0.05 | -2.9 / -2.2 |
| NETANYAHU_OUT_BY.26DEC31 | Geopolitics | 34.4 | 237 | 22 | 0.727/0.728 (16/2450) | 0.700/0.710 (1279/4145) | 28,061 | 0.02 | 0.0 | -3.2 / 1.3 |
| AI_MATH_2026.LEGENDRE | AI/Tech | 34.4 | 215 | 4 | 0.112/0.127 (114/133) | 0.113/0.123 (120/141) | 3,322 | 0.02 | 0.05 | -2.1 / -1.8 |
| AI_MATH_2026.P_VS_NP | AI/Tech | 34.4 | 214 | 10 | 0.123/0.129 (323/3) | 0.124/0.126 (320/40) | 74,038 | 0.02 | 0.05 | -1.3 / -1.1 |
| NFL_26NOV29_SEASF_ML.SEA | Sports | 2.2 | 203 | 1 | 0.420/0.430 (4747/4527) | 0.420/0.430 (6453/19328) | 13,981 | 0.02 | 0.05 | -2.7 / -2.7 |
| NBA_26NOV27_BKNTOR_TT.219 | Sports | 0.4 | 194 | 1 | 0.470/0.480 (5000/222) | 0.470/0.480 (20114/240) | 18,665 | 0.02 | 0.05 | -2.7 / -2.7 |
| AI_MATH_2026.YANG_MILLS | AI/Tech | 34.4 | 182 | 6 | 0.050/0.704 (5000/50) | 0.080/0.100 (120/163) | 3,825 | 0.02 | 0.05 | -63.2 / -5.5 |
| AI_MATH_2026.BEAL | AI/Tech | 34.4 | 173 | 5 | 0.121/0.128 (34/95) | 0.122/0.124 (36/100) | 3,397 | 0.02 | 0.05 | -1.4 / -1.1 |
| EXTENDED_FDV_1D_ABOVE.300M | Crypto | 34.4 | 152 | 4 | 0.100/0.120 (1480/868) | 0.100/0.120 (2009/1248) | 942 | 0.01 | 0.01 | -2.2 / -2.2 |
| AI_MATH_2026.RAMSEY_R5_5 | AI/Tech | 34.4 | 147 | 3 | 0.149/0.165 (128/360) | 0.150/0.160 (136/380) | 1,288 | 0.02 | 0.05 | -2.4 / -2.0 |
| AI_MATH_2026.HODGE | AI/Tech | 34.4 | 134 | 4 | 0.059/0.102 (218/74) | 0.060/0.080 (250/125) | 3,103 | 0.02 | 0.05 | -4.7 / -2.6 |
| AI_MATH_2026.COLLATZ | AI/Tech | 34.4 | 98 | 4 | 0.068/0.073 (46/144) | 0.069/0.070 (49/152) | 7,542 | 0.02 | 0.05 | -0.9 / -0.7 |
| NFL_26NOV27_DENPIT_ML.DEN | Sports | 0.2 | 80 | 8 | 0.680/0.690 (4142/5000) | 0.680/0.690 (1436318/537777) | 307,592 | 0.02 | 0.05 | -2.5 / -2.5 |

Spread distribution (¢, p10 / p50 / p90), two-sided books only:

| set | n | Pascal | Polymarket (long token) |
|---|---:|---|---|
| all mapped | 3379 | 0.8 / 1.4 / 15.4 (n=2644) | 0.1 / 1.0 / 6.5 (n=2906) |
| Pascal 24h vol > 0 | 62 | 0.6 / 1.0 / 2.2 (n=62) | 0.2 / 1.0 / 2.0 (n=62) |

Gap now (after both taker fees, top of book). Displayed shares = min of the two top-level sizes; $ = gap x shares:

| direction | > 1¢ n | > 1¢ shares | > 1¢ $ | > 2¢ n | > 2¢ shares | > 2¢ $ | > 4¢ n | > 4¢ shares | > 4¢ $ | best gap ¢ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| LP | 0 | 0 | 0.00 | 0 | 0 | 0.00 | 0 | 0 | 0.00 | -0.1 |
| SP | 1 | 16 | 0.21 | 0 | 0 | 0.00 | 0 | 0 | 0.00 | 1.3 |

Largest gaps now:

| symbol | cat | days | dir | gap ¢ | shares | P bid/ask (size) | PM L bid/ask (size) | PM R ask (size) | P 24h $ |
|---|---|---:|---|---:|---:|---|---|---|---:|
| NETANYAHU_OUT_BY.26DEC31 | Geopolitics | 34.4 | SP | 1.3 | 16 | 0.727/0.728 (16/2450) | 0.700/0.710 (1279/4145) | 0.300 (1279) | 237 |
| RUS_EU_STRIKE_BY.26DEC31 | Politics | 34.4 | SP | 0.8 | 5 | 0.171/0.190 (23/499) | 0.156/0.160 (30/5) | 0.844 (30) | 11 |
| NBA_2027_CHAMPION.POR | Sports | 204.7 | LP | -0.1 | 1872 | 0.033/0.037 (1685/50000) | 0.039/0.040 (1872/577) | 0.961 (1872) | 0 |
| IRAN_HOS_2026.ARAGHCHI | Politics | 33.9 | SP | -0.1 | 2630 | 0.001/0.009 (4380/2508) | 0.001/0.002 (4373/2630) | 0.999 (4373) | 0 |
| IRAN_HOS_2026.GHALIBAF | Politics | 33.9 | SP | -0.1 | 2565 | 0.001/0.009 (5290/2199) | 0.001/0.002 (5411/2565) | 0.999 (5411) | 0 |
| IRAN_HOS_2026.KHOMEINI | Politics | 33.9 | SP | -0.1 | 1562 | 0.001/0.009 (1562/3481) | 0.001/0.002 (1736/3729) | 0.999 (1736) | 0 |
| IRAN_HOS_2026.LARIJANI | Politics | 33.9 | SP | -0.1 | 47 | 0.001/0.009 (47/5991) | 0.001/0.002 (51/3739) | 0.999 (51) | 0 |
| IRAN_HOS_2026.VAHIDI | Politics | 33.9 | SP | -0.1 | 1106 | 0.001/0.009 (1106/4551) | 0.001/0.002 (1175/4855) | 0.999 (1175) | 0 |
| NEXT_LEADER_OUT_2026.ALSHARAA | Politics | 34.4 | SP | -0.1 | 1684 | 0.001/0.009 (5865/1538) | 0.001/0.002 (6002/1684) | 0.999 (6002) | 0 |
| NEXT_LEADER_OUT_2026.DELCY | Politics | 34.4 | SP | -0.1 | 628 | 0.001/0.009 (628/2527) | 0.001/0.002 (671/2660) | 0.999 (671) | 0 |
| NEXT_LEADER_OUT_2026.DIAZCANEL | Politics | 34.4 | SP | -0.1 | 273 | 0.001/0.012 (12037/562) | 0.001/0.002 (13008/273) | 0.999 (13008) | 0 |
| NEXT_LEADER_OUT_2026.KIM | Politics | 34.4 | SP | -0.1 | 466 | 0.001/0.010 (17808/1951) | 0.001/0.002 (18699/466) | 0.999 (18699) | 0 |
| NEXT_LEADER_OUT_2026.MACRON | Politics | 34.4 | SP | -0.1 | 900 | 0.001/0.009 (1962/825) | 0.001/0.002 (2011/900) | 0.999 (2011) | 0 |
| NEXT_LEADER_OUT_2026.PUTIN | Politics | 34.4 | SP | -0.1 | 5413 | 0.001/0.009 (7643/5090) | 0.001/0.002 (7845/5413) | 0.999 (7845) | 0 |
| NEXT_LEADER_OUT_2026.XI | Politics | 34.4 | SP | -0.1 | 1464 | 0.001/0.009 (24715/1311) | 0.001/0.002 (26074/1464) | 0.999 (26074) | 0 |

MECE events on Pascal: 1; with every outcome mapped: 1; fully two-sided on Pascal asks: 1.

| basket | events priced | cost < $1 - 1¢ | < $1 - 2¢ | < $1 - 4¢ | median cost $ | min cost $ |
|---|---:|---:|---:|---:|---:|---:|
| buy every outcome, Pascal | 1 | 0 | 0 | 0 | 1.028 | 1.028 |
| sell every outcome (buy all No), Pascal, per $1 | 0 | 0 | 0 | 0 | nan | nan |
| buy every outcome, Polymarket | 1 | 0 | 0 | 0 | 1.010 | 1.010 |

In the brief's primary set (resolving within 30 days): 1,495 mapped markets, 37 with any Pascal trade in 24 h, $9.8K of the $12.3K.


**What the books look like.** Pascal's mapped books replicate Polymarket's: at the same best bid price (788 of 2,661 two-sided pairs) the median Pascal size is **0.94×** Polymarket's (p10 0.29, p90 1.59), and the top 20 markets show the same prices to the tick on both venues. A mirroring maker quotes Pascal off the Polymarket book, so gaps sit at about minus both fees by construction. The live question for PX is therefore how fast that mirror updates when Polymarket moves, which the recorder measures.

**MECE.** Only one Pascal event is flagged `is_mece` (FED_DECISION_26DEC, 5 outcomes): buy-all costs $1.028 on Pascal and $1.010 on Polymarket. PX-C has one event to work with.

---

## P3. Recorder

`pascal/pascal_rec.py` (read-only, public data, no keys). **Running since 2026-11-27 19:35 UTC** in `tmux -L research` session `pascal`, window `rec`, through `~/pascal-data/run_rec.sh` (a restart-on-failure loop under `systemd-run --user --scope -p MemoryMax=400M -p CPUQuota=25%`, `nice 10`; exit 0 = disk guard, stays down).

- **What it records.** Pascal: `book` + `trades` for the top 150 mapped markets (Pascal 24 h taker notional, then all-time, then long interest; list refreshed every 6 h from `/api/v1/markets`, each snapshot kept gzipped under `rec/markets/`), plus `all_trades` (trades outside the set kept as `pta`) and `market_update`. Polymarket: the public market channel for both tokens of the same 150 markets, sharded 20 tokens per socket (15 sockets; one Tokyo↔London socket backlogs, 11-14). Stored per line: receive time, Pascal `seq` / `state_time_ms` / `server_time_ms`, Polymarket exchange `timestamp`, top 3 levels on change, all trades with both Pascal wallets and position-before fields, Polymarket trade prints with fee bps and tx hash, gap markers on any socket drop, and a lag/health line every minute. Files: `~/pascal-data/rec/YYYY-MM-DD/HH.jsonl.gz` (format in the module docstring).
- **Status at 20:45 UTC:** 1 Pascal + 15 Polymarket sockets up, 0 drops, RSS 48 MB, 8.5 MB written in 68 min (≈ 150–200 MB/day; cap 300 MB/day), Pascal lag p50 7–8 ms, Polymarket lag p50 ≈ 120 ms.
- **Guards:** RSS > 300 MB → exit 1 (restart); free disk < 20 GB (1e9 B) → exit 0 (stays down); per UTC day it stops writing books at 270 MB and everything at 300 MB.
- **Tests** (`pascal/tests/test_pascal_rec.py`, 4/4 pass, on a 40 s raw-frame capture `pascal/tests/fixtures/raw_frames.jsonl.gz`): parse (books sorted, uncrossed, trades with both wallets, PM legs oriented), reconnect (gap line written; a new snapshot replaces the book; update-before-snapshot flagged), list refresh (ordering, exclusions, `gen` bump, `hdr` rewritten), disk guard (book drop at 90 % of the cap, full stop at 100 %, reset next UTC day, free-disk floor → exit 0, RSS ceiling → exit 1).

**Operator: install as a user service** (then stop the tmux copy so there is one writer):

```bash
tmux -L research send-keys -t pascal:rec C-c      # stop the tmux loop (Ctrl-C twice if the loop restarts it)
install -m 644 ~/pascal-lighter/deploy/pascal-rec.service ~/.config/systemd/user/pascal-rec.service
systemctl --user daemon-reload
systemctl --user enable --now pascal-rec.service
systemctl --user status pascal-rec.service --no-pager; tail -1 ~/pascal-data/logs/rec.log
```

The unit (`deploy/pascal-rec.service`): `MemoryMax=400M`, `CPUQuota=25%`, `Nice=10`, `Restart=on-failure`, `RestartSec=30`, logs appended to `~/pascal-data/logs/rec.log`. The hourly files are append-safe across restarts (a restart adds a gzip member).

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

**Reading.** Pascal history reaches the first trade of every market (no `HISTORY_NOT_READY`; earliest 2026-06-23). Across the 30 busiest mapped markets, about five months of Pascal taker flow is 5,065 trades, $496K. Takers pay 0.66¢/share against Polymarket at the trade, and the gap closes over the next hour, ending at +0.37¢/share in the takers' favour at 60 min. Most of the tape is uninformed crossing (74% of shares trade more than 0.5¢ worse than Polymarket); 9% trades toward Polymarket's price, i.e. arb-like flow. One wallet (`7ATdPM…`, 17% of $, 2,581 trades, also seen as a maker on 11-27) is informed: +0.35¢ at the trade, +1.0¢ an hour later. A Pascal maker at Polymarket ± 3¢ would have filled 33K shares in five months (~210 shares/day) and marked +0.51¢/share against Polymarket 10 min later before any hedge. That is real but about $1/day. Resolution caveat: `/prices-history` points are ~1 minute apart (Data API v1 is retired; v2 `/trades` carries prints, not quotes), so "at the trade" means the last minute point. This is descriptive and selects nothing.

---

## Disk

<!-- DISKDETAIL -->

## Deviations from the brief

<!-- DEVIATIONS -->
