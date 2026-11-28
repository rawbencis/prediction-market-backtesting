# Tokyo 2026-11-27: Pascal × Polymarket and the Lighter fast-pull maker

Branch `research/pascal-lighter` (from `research/lp-arb` @ df440e0). Paper research only: no orders, no keys, no accounts.

## Verdict

| study | verdict | deciding number |
|---|---|---|
| **P1** Pascal census | **closed on capacity** (gate fails) | 4 mapped markets with ≥ $1K Pascal 24 h volume (gate 10); all of Pascal did **$12.3K** taker notional in 24 h; 20 markets = 96% of it. Pascal's books mirror Polymarket's (same prices, ~0.94× size), so gaps sit at about minus both fees: 0 pairs > 1¢ after fees except one 16-share print. |
| **P3** recorder | **running** | since 11-27 19:35 UTC, 0 drops, ≈ 150–200 MB/day; user unit written, not installed (operator commands below). |
| **P4** PX-A/B frozen | **waiting on data** | `frozen/PX_plan.json` @ 51f2bf5. Dev = 11-28..11-30 (run after 12-01 00:05 UTC), holdout = 12-01..12-07. Smoke test on the partial 11-27 recording: **0 attempts** in all 18 cells; the Pascal mirror trails Polymarket touch moves by p50 492 ms. **The holdout is at risk from disk (below).** |
| **P4** PX-C MECE | **closed in practice** | 1 MECE event on Pascal (FED_DECISION_26DEC): buy-all $1.028 Pascal, $1.010 Polymarket. |
| **L1** Lighter fast-pull maker | **closed** (holdout FAIL) | frozen pick (BTC, h 2¢, pull ≥ 1 bp/250 ms, premium D = 0): holdout **−1.96¢/share to settlement**, 90% CI −7.9 … +4.0, 3/7 days positive, −$27.7/day; 5 s markout +1.98¢ but 30 s −0.74¢. Only premium-latency cells ever showed a positive 5 s markout on dev (101/288 at D = 0 vs 0/288 at D = 300), and premium quota for this rule would cost ≈ $51/day on top. |
| **P2** Pascal history | descriptive | 5,065 trades ($496K) since 2026-06-23 in the top 30 markets. Takers pay 0.66¢ vs Polymarket at the trade, +0.37¢ in their favour at 60 min; a maker at PM ± 3¢ marks +0.51¢/share at 10 min on ~210 shares/day (≈ $1/day). |
| **P0** venue facts | done | Pascal: 50 ms rounds, cancels first, no taker hold, feed 6.8 ms behind engine time, ping 5–6 ms, no crypto up/down. Lighter: binary maker 0 / taker 1%·p(1−p); premium 0 ms cancel, 140 ms taker, quota $0.0005; 1h series live but settles end-vs-start (Polymarket 1h uses TWAP), and it isn't recorded here. Restricted-jurisdiction list not fetched (rule 1). |

**Operator action needed: disk.** About **3 days** of disk are left: 57.5 GB free, about 12 GB/day measured box-wide. The recorder's 20 GB floor arrives around **12-01**, right as the PX dev days finish. The PX holdout needs recording until 12-08 00:00 UTC, so about 70 GB must be freed (or writers slowed) before ~12-01. See **Disk**.

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

**Smoke test on the partial 11-27 recording** (19:35–21:30 UTC; outside dev and holdout; run after the plan was frozen, to check the pipeline end to end). 150 markets cached, Pascal lag 7.9 ms, PM fee rate resolved for all 150, 46 in the primary set. **0 attempts in every cell.** The cheapest pair cost after both fees was $0.978 (ISR_AIRSPACE_CLSD_BY.26DEC31, which resolves 12-31, outside the 30-day primary set). Only ~7 new Pascal taker trades arrived in 2 h across the 150 markets, so PX-B has almost nothing to fill against.

**How fast the Pascal mirror follows Polymarket** (same partial day, descriptive): over 10,598 Polymarket touch moves of ≥ 1¢, Pascal's same-side touch moved at least half as far within 10 s in 76% of cases, after **p10 82 / p25 149 / p50 492 / p75 1,694 / p90 4,027 ms** (PM exchange time → Pascal state time). We see Polymarket at +116 ms and reach Pascal in ≤ ~60 ms, so in about half of moves the stale Pascal quote is still up when we could act. Whether any of those gaps clears 2¢ after both taker fees with ≥ $25 on each side is the PX-A dev question.

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

Plan `frozen/L1_plan.json` pushed at **faa4c8f** (20:20 UTC) before any dev run; pick `frozen/L1_pick.json` pushed at **6025233** (21:26 UTC) before any holdout day was opened. Code: `pascal/l1.py`. Data: `~/lighter-data` (BTC/ETH 15m books top 3, trades with both accounts), Binance USD-M aggTrades and Polymarket 15m UP books from `~/shadow-rec` (11-20 on) and `~/archive/shadow-rec/*.tar.zst` (11-11..11-19; one day at a time extracted, compacted to `~/pascal-data/l1cache/DAY.npz`, deleted). Exchange-time join; views Lighter +2 ms, Binance +4 ms, Polymarket +116 ms.

### Validation (before reading dev P&L)

**V1, synthetic** (40 windows; martingale mid; Lighter at mid ± 3¢; uninformed takers at Lighter's touch; then informed takers 200 ms after each Binance jump, with Polymarket repricing 4¢ at +300 ms). Expected: P&L ≈ spread captured, and pull > none at D = 0 but < none at D = 300. **Pass:**

| takers | cell | shares | ¢/share settle | ¢/share vs fair | $ total |
|---|---|---:|---:|---:|---:|
| uninformed | pm_mid|h2|none|D0|to_close | 194370 | 1.89 | 2.00 | 3673.20 |
| uninformed | pm_mid|h2|none|D300|to_close | 188910 | 1.86 | 1.97 | 3510.60 |
| uninformed | pm_mid|h2|2bp/250ms|q1|D0|to_close | 194370 | 1.89 | 2.00 | 3673.20 |
| uninformed | pm_mid|h2|2bp/250ms|q1|D300|to_close | 188910 | 1.86 | 1.97 | 3510.60 |
| uninformed + informed | pm_mid|h2|none|D0|to_close | 216090 | 1.69 | 2.00 | 3649.80 |
| uninformed + informed | pm_mid|h2|none|D300|to_close | 209250 | 1.65 | 1.96 | 3452.10 |
| uninformed + informed | pm_mid|h2|2bp/250ms|q1|D0|to_close | 189450 | 2.08 | 2.00 | 3937.50 |
| uninformed + informed | pm_mid|h2|2bp/250ms|q1|D300|to_close | 206400 | 1.66 | 1.97 | 3418.80 |

{"uninformed_pnl_close_to_spread": true, "pull_helps_at_D0": true, "pull_hurts_at_D300": true}

**V2, reproduction of `lighter_flips.py` fair3** (dev 11-11..11-13; x = 0.70, first flip, Q = 10). The reference code rerun on the same recordings, then the same one-shot orders through this engine, then the brief's continuous cell. **Pass:** the one-shot orders reproduce the reference's negative 5 s markout within noise at every lifetime, and the continuous cell (no pull, D = 300, pm_mid, h = 3¢) is negative too. It is smaller than fair3 because it requotes on every 1¢ move instead of resting stale for the order's life.

| source | life s | orders | filled shares | mo5 ¢/share |
|---|---:|---:|---:|---:|
| lighter_flips.window (reference, rerun) | 10 | 968 | 209 | -0.16 |
| lighter_flips.window (reference, rerun) | 60 | 968 | 1,153 | -3.92 |
| lighter_flips.window (reference, rerun) | 300 | 968 | 3,821 | -8.56 |
| lighter_flips.window (reference, rerun) | 900 | 968 | 5,962 | -10.17 |
| this engine, same one-shot orders | 10 | 977 | 193 | -0.08 |
| this engine, same one-shot orders | 60 | 977 | 1,150 | -4.01 |
| this engine, same one-shot orders | 300 | 977 | 3,834 | -8.64 |
| this engine, same one-shot orders | 900 | 977 | 6,005 | -10.55 |
| continuous two-sided btc pm_mid|h3|none|D300|to_close | - | - | 26,070 | -2.03 |
| continuous two-sided btc pm_mid|h3|none|D0|to_close | - | - | 21,329 | -1.47 |
| continuous two-sided eth pm_mid|h3|none|D300|to_close | - | - | 9,784 | -3.46 |
| continuous two-sided eth pm_mid|h3|none|D0|to_close | - | - | 6,638 | -2.25 |

### L1-D (dev 11-11..11-20, descriptive)

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

So Lighter's touch usually moves well inside 300 ms of a Binance move (median 86–110 ms, a third within 50 ms). The dominant maker, 712772, makes 79% of maker volume, which fits a premium (0 ms cancel) account; standard-account repricing would cluster at ~300 ms, and only 6–8% of changes do. Taker flow in the 500 ms after a ≥ 2 bp move is 17–21% of volume and costs makers 4–6¢/share at 5 s. Quiet flow pays makers +1.0¢ (BTC) / +0.35¢ (ETH) at 5 s, so the stop rule did not trigger.

### Dev replay (288 cells × 2 assets, 30-share quotes; 5-share beside)

Cells: 576 (both assets). D = 0 cells with 5 s markout > 0: 101 of 288. D = 300: 0 of 288.
Stop-early rule (no D = 0 cell with 5 s markout > 0): **not triggered**.

Eligible for the pick (5 s markout > 0 and >= 300 shares/day): 89 cells.

Top 15 cells by 90% lower bound on ¢/share to settlement (all cells):

| asset | cell | fills_day | shares_day | vsfair | mo1 | mo5 | mo30 | settle | ci_lo | ci_hi | usd_day | days_pos | msgs_per_s | top3 | hedged |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| btc | lighter_mid|h3|1bp/250ms|q5|D0|stop60 | 89.90 | 1650.21 | 3.08 | -2.67 | -2.49 | 0.15 | 5.11 | 0.76 | 9.68 | 84.37 | 7 | 2.97 | 0.25 | -4.65 |
| btc | lighter_mid|h3|1bp/250ms|q5|D0|to_close | 101.30 | 1892.30 | 3.11 | -3.00 | -2.68 | -0.09 | 4.47 | 0.36 | 8.74 | 84.61 | 7 | 2.82 | 0.23 | -4.99 |
| btc | pm_mid|h2|1bp/250ms|q5|D0|stop60 | 148.50 | 2866.94 | 2.31 | 0.73 | 0.48 | 1.12 | 3.50 | -0.53 | 7.39 | 100.27 | 6 | 1.51 | 0.21 | -0.88 |
| eth | pm_mid|h5|1bp/250ms|q1|D0|stop60 | 10.00 | 173.11 | 5.45 | 4.36 | 4.79 | 8.86 | 8.10 | -0.87 | 16.35 | 14.03 | 7 | 2.76 | 0.34 | 2.82 |
| btc | pm_mid|h2|1bp/250ms|q5|D0|to_close | 164.50 | 3197.82 | 2.31 | 0.86 | 0.83 | 0.79 | 3.04 | -0.88 | 6.69 | 97.11 | 5 | 1.48 | 0.19 | -0.76 |
| btc | pm_mid|h2|1bp/250ms|q1|D0|stop60 | 241.00 | 4705.48 | 2.29 | 0.87 | 0.62 | 0.98 | 2.03 | -0.95 | 4.87 | 95.57 | 7 | 1.85 | 0.23 | -0.79 |
| btc | pm_mid|h2|1bp/250ms|q1|D0|to_close | 267.10 | 5235.98 | 2.30 | 0.99 | 0.85 | 0.95 | 1.82 | -1.01 | 4.53 | 95.45 | 6 | 1.80 | 0.21 | -0.70 |
| btc | pm_mid|h3|2bp/250ms|q1|D0|to_close | 219.60 | 4303.03 | 3.26 | 0.78 | 0.42 | 0.95 | 1.57 | -1.14 | 4.38 | 67.68 | 6 | 1.44 | 0.20 | -0.73 |
| btc | pm_mid|h2|2bp/250ms|q1|D0|stop60 | 366.60 | 7094.64 | 2.26 | 0.21 | -0.11 | 0.07 | 1.13 | -1.18 | 3.44 | 80.45 | 6 | 1.47 | 0.26 | -1.32 |
| btc | pm_mid|h3|2bp/1s|q1|D0|to_close | 194.40 | 3797.20 | 3.28 | 0.91 | 0.59 | 1.37 | 1.81 | -1.19 | 4.81 | 68.62 | 6 | 1.53 | 0.19 | -0.66 |
| btc | pm_mid|h2|2bp/250ms|q1|D0|to_close | 396.20 | 7694.32 | 2.27 | 0.32 | 0.11 | -0.03 | 0.88 | -1.29 | 3.11 | 67.83 | 7 | 1.44 | 0.25 | -1.26 |
| btc | pm_mid|h3|2bp/250ms|q1|D0|stop60 | 197.10 | 3850.62 | 3.24 | 0.63 | 0.21 | 1.05 | 1.69 | -1.40 | 4.86 | 64.99 | 7 | 1.47 | 0.23 | -0.83 |
| btc | pm_mid|h2|2bp/1s|q1|D0|to_close | 347.30 | 6739.25 | 2.29 | 0.43 | 0.24 | 0.22 | 0.95 | -1.40 | 3.27 | 64.26 | 6 | 1.53 | 0.23 | -1.19 |
| btc | pm_mid|h2|2bp/250ms|q5|D0|stop60 | 316.90 | 6091.73 | 2.27 | 0.20 | -0.08 | 0.36 | 1.05 | -1.41 | 3.64 | 64.17 | 6 | 1.40 | 0.26 | -1.37 |
| btc | pm_mid|h2|2bp/1s|q1|D0|stop60 | 320.00 | 6184.24 | 2.28 | 0.30 | -0.02 | 0.15 | 1.03 | -1.53 | 3.45 | 63.82 | 6 | 1.56 | 0.25 | -1.29 |

Best cell per (asset, pull, D) by lower bound:

| asset | cell | fills_day | shares_day | vsfair | mo1 | mo5 | mo30 | settle | ci_lo | ci_hi | usd_day | days_pos | msgs_per_s | top3 | hedged |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| btc | lighter_mid|h3|1bp/250ms|q5|D0|stop60 | 89.90 | 1650.21 | 3.08 | -2.67 | -2.49 | 0.15 | 5.11 | 0.76 | 9.68 | 84.37 | 7 | 2.97 | 0.25 | -4.65 |
| btc | pm_mid|h3|2bp/1s|q1|D0|to_close | 194.40 | 3797.20 | 3.28 | 0.91 | 0.59 | 1.37 | 1.81 | -1.19 | 4.81 | 68.62 | 6 | 1.53 | 0.19 | -0.66 |
| btc | pm_mid|h3|2bp/250ms|q1|D0|to_close | 219.60 | 4303.03 | 3.26 | 0.78 | 0.42 | 0.95 | 1.57 | -1.14 | 4.38 | 67.68 | 6 | 1.44 | 0.20 | -0.73 |
| btc | pm_mid|h2|4bp/1s|q1|D0|stop60 | 425.00 | 8266.67 | 2.26 | -0.26 | -0.61 | -0.47 | 0.44 | -1.68 | 2.50 | 36.29 | 5 | 1.36 | 0.28 | -1.62 |
| btc | pm_mid|h2|none|D0|stop60 | 524.90 | 10383.57 | 2.27 | -1.39 | -2.02 | -1.76 | -0.46 | -2.19 | 1.26 | -47.29 | 3 | 1.30 | 0.27 | -2.17 |
| btc | lighter_mid|h3|1bp/250ms|q5|D300|stop60 | 166.90 | 3165.11 | 0.42 | -2.48 | -3.15 | -2.42 | 1.10 | -2.29 | 4.55 | 34.87 | 5 | 2.97 | 0.26 | -3.19 |
| btc | pm_mid|h2|2bp/1s|q5|D300|to_close | 367.90 | 7089.13 | 1.79 | -1.23 | -1.54 | -0.83 | -0.72 | -2.90 | 1.31 | -50.92 | 3 | 1.41 | 0.28 | -2.39 |
| btc | lighter_mid|h3|2bp/250ms|q1|D300|stop60 | 347.40 | 6556.40 | 0.68 | -2.83 | -3.08 | -2.85 | -0.68 | -2.80 | 1.72 | -44.29 | 6 | 2.93 | 0.29 | -4.02 |
| btc | lighter_mid|h3|4bp/1s|q1|D300|stop60 | 374.60 | 7113.39 | 0.60 | -3.04 | -3.24 | -2.82 | -0.37 | -2.46 | 1.86 | -26.46 | 5 | 2.82 | 0.29 | -4.22 |
| btc | lighter_mid|h3|none|D300|stop60 | 413.00 | 7894.65 | 0.60 | -3.40 | -3.71 | -3.21 | -0.81 | -2.79 | 1.24 | -63.86 | 5 | 2.76 | 0.29 | -4.37 |
| eth | pm_mid|h5|1bp/250ms|q1|D0|stop60 | 10.00 | 173.11 | 5.45 | 4.36 | 4.79 | 8.86 | 8.10 | -0.87 | 16.35 | 14.03 | 7 | 2.76 | 0.34 | 2.82 |
| eth | pm_mid|h3|2bp/1s|q1|D0|stop60 | 55.50 | 1141.28 | 3.26 | 0.63 | 0.41 | 1.16 | 1.80 | -2.36 | 5.91 | 20.58 | 4 | 2.42 | 0.47 | -1.12 |
| eth | pm_mid|h2|2bp/250ms|q5|D0|stop60 | 115.10 | 2458.65 | 2.26 | 0.23 | 0.18 | 0.13 | 0.57 | -2.49 | 3.58 | 14.05 | 5 | 2.17 | 0.54 | -1.63 |
| eth | pm_mid|h2|4bp/1s|q5|D0|stop60 | 165.40 | 3535.56 | 2.23 | -0.56 | -0.80 | -0.53 | 0.25 | -2.35 | 2.64 | 8.74 | 6 | 2.10 | 0.56 | -2.30 |
| eth | pm_mid|h2|none|D0|stop60 | 242.00 | 5365.04 | 2.22 | -1.25 | -1.66 | -1.35 | -2.01 | -4.21 | 0.13 | -108.01 | 1 | 2.04 | 0.52 | -2.77 |
| eth | lighter_mid|h2|1bp/250ms|q1|D300|stop60 | 203.00 | 4454.71 | -0.12 | -3.40 | -3.57 | -3.44 | -2.32 | -4.60 | 0.08 | -103.26 | 3 | 5.08 | 0.53 | -5.38 |
| eth | lighter_mid|h2|2bp/1s|q5|D300|stop60 | 155.30 | 3392.25 | -0.46 | -3.91 | -4.01 | -2.95 | -1.27 | -3.87 | 1.46 | -42.97 | 5 | 4.52 | 0.57 | -5.85 |
| eth | lighter_mid|h2|2bp/250ms|q5|D300|stop60 | 215.70 | 4730.99 | -0.47 | -3.60 | -3.74 | -3.51 | -1.67 | -3.75 | 0.58 | -78.89 | 3 | 4.49 | 0.56 | -5.57 |
| eth | lighter_mid|h2|4bp/1s|q1|D300|stop60 | 304.60 | 6727.54 | -0.35 | -3.46 | -3.64 | -3.46 | -1.96 | -3.64 | -0.03 | -131.94 | 3 | 4.48 | 0.55 | -5.30 |
| eth | pm_mid|h2|none|D300|to_close | 308.60 | 6966.34 | 1.23 | -2.32 | -2.74 | -2.40 | -2.68 | -4.64 | -0.83 | -186.57 | 1 | 1.99 | 0.55 | -3.74 |

Same cells, no pull vs pull, settlement ¢/share (mean over fair, h, stop, repost) by asset and D:

| asset | D | pull | settle | mo5 | shares_day |
|---|---|---|---|---|---|
| btc | 0 | 1bp/250ms | 0.40 | -0.59 | 2222.98 |
| btc | 0 | 2bp/1s | -0.67 | -1.82 | 3447.43 |
| btc | 0 | 2bp/250ms | -0.97 | -2.07 | 4148.31 |
| btc | 0 | 4bp/1s | -1.36 | -2.58 | 4986.61 |
| btc | 0 | none | -1.83 | -3.34 | 6307.06 |
| btc | 300 | 1bp/250ms | -0.46 | -2.13 | 3710.33 |
| btc | 300 | 2bp/1s | -1.64 | -2.15 | 4637.63 |
| btc | 300 | 2bp/250ms | -1.98 | -2.23 | 5378.87 |
| btc | 300 | 4bp/1s | -1.75 | -2.36 | 6034.14 |
| btc | 300 | none | -1.60 | -2.76 | 7062.16 |
| eth | 0 | 1bp/250ms | -2.30 | -1.55 | 1085.59 |
| eth | 0 | 2bp/1s | -3.31 | -3.10 | 1975.06 |
| eth | 0 | 2bp/250ms | -3.65 | -2.90 | 2479.83 |
| eth | 0 | 4bp/1s | -4.25 | -3.67 | 3117.16 |
| eth | 0 | none | -4.80 | -3.95 | 4020.66 |
| eth | 300 | 1bp/250ms | -3.40 | -3.73 | 1594.84 |
| eth | 300 | 2bp/1s | -3.18 | -3.71 | 2230.31 |
| eth | 300 | 2bp/250ms | -3.38 | -3.51 | 2786.29 |
| eth | 300 | 4bp/1s | -3.74 | -3.65 | 3276.40 |
| eth | 300 | none | -3.84 | -3.62 | 4019.41 |

5-share quotes: top 5 by lower bound:

| asset | cell | fills_day | shares_day | vsfair | mo1 | mo5 | mo30 | settle | ci_lo | ci_hi | usd_day | days_pos | msgs_per_s | top3 | hedged |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| btc | lighter_mid|h3|1bp/250ms|q5|D0|to_close | 104.60 | 514.67 | 3.10 | -3.33 | -2.81 | 0.01 | 4.54 | 0.18 | 9.13 | 23.36 | 7 | 2.82 | 0.21 | -5.28 |
| btc | pm_mid|h2|1bp/250ms|q5|D0|stop60 | 157.10 | 764.92 | 2.30 | 0.76 | 0.68 | 1.55 | 4.57 | 0.15 | 8.77 | 34.92 | 7 | 1.51 | 0.19 | -0.90 |
| btc | pm_mid|h2|1bp/250ms|q5|D0|to_close | 174.80 | 853.39 | 2.31 | 0.89 | 1.06 | 1.25 | 4.05 | 0.02 | 7.92 | 34.54 | 7 | 1.48 | 0.18 | -0.78 |
| btc | pm_mid|h2|1bp/250ms|q1|D0|stop60 | 259.50 | 1262.32 | 2.29 | 0.88 | 0.78 | 1.40 | 3.30 | -0.08 | 6.34 | 41.63 | 8 | 1.85 | 0.20 | -0.79 |
| btc | lighter_mid|h3|1bp/250ms|q5|D0|stop60 | 91.60 | 449.98 | 3.07 | -2.81 | -2.63 | -0.36 | 4.47 | -0.12 | 9.33 | 20.13 | 8 | 2.97 | 0.23 | -4.76 |

**Dev pick:** btc `pm_mid|h2|1bp/250ms|q5|D0|stop60` — settle 3.50¢/share (90% CI -0.53 … 7.39), 5 s markout 0.48¢, 2867 shares/day, $100.27/day, days positive 6/10.

**Reading the dev table.** Pulling on Binance is the only thing that turns the 5 s markout positive, and only at D = 0: 101 of 288 D = 0 cells mark out above zero, against 0 of 288 at D = 300. On BTC at D = 0 the mean settlement goes −1.83¢ (no pull) → −1.36 (4 bp/1 s) → −0.97 (2 bp/250 ms) → +0.40¢ (1 bp/250 ms). At D = 300 every pull rule stays at −0.5 to −2.0¢. ETH is negative almost everywhere: its quiet-flow edge is a third of BTC's, and 50%+ of its fills come from three takers. Settlement ¢/share is much noisier than markout (each window is one coin flip on ~30–150 shares), which is why even the best cells have dev lower bounds near or below zero. The Polymarket hedge column is negative in every top cell: the edge exists only held to settlement or marked on Polymarket's mid, not after paying Polymarket's taker fee plus spread 300 ms later.

### Frozen pick

`frozen/L1_pick.json` (6025233): **BTC, fair = Polymarket mid as seen, h = 2¢, pull both sides on a Binance move ≥ 1 bp / 250 ms, re-post after 5 s quiet, D = 0 (premium), stop 60 s before the close, 30-share quotes.** Dev: +3.50¢/share to settlement (90% CI −0.53 … +7.39), 5 s markout +0.48¢, 2,867 shares/day, $100/day, 6 of 10 days positive. It was the highest dev lower bound among the 89 eligible cells. The cell with the highest lower bound overall, `lighter_mid|h3|…`, was ineligible because its 5 s markout is −2.5¢.

**Premium cost of the pick** (dev, BTC, from the simulator's message counts): it consumes **≈ 108K volume quota/day** (creates and modifies; single cancels are free) but earns only ≈ 820 from fills (≈ $1,630/day notional at $2 per quota) plus 5,760 free transactions. The remaining ≈ 102K/day would have to be bought at $0.0005 each: **≈ $51/day**, about half the dev P&L. Batching both sides as grouped orders would roughly halve the posts but not the modifies.

### Holdout (11-21..11-27, read once, 00:05 UTC 11-28)


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

Per-share terms: the 5 s markout stayed positive (+1.98¢) but went negative by 30 s (−0.74¢), and settlement was −1.96¢/share with 3 of 7 days positive. The top-3 taker share rose from 21% on dev to 35%. **L1 is closed.** Even with a premium account and before its quota cost, quoting around Polymarket's mid and pulling on Binance doesn't keep the edge to settlement in this market.

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

Free at 23:28 UTC: **57.5 GB** (`df` Avail; the guards use 1e9-byte GB). **Measured box-wide burn: about 12 GB/day.** Free disk fell 60.5 → 57.5 GB from 19:36 to 23:28 UTC; take away this session's ~1.1 GB of caches and ~1.9 GB went in 3.9 h. Writers seen growing in the last hour:

| writer | rate seen | note |
|---|---:|---|
| `onetouch-shadow` (`~/onetouch-mm/research/onetouch_mm/data`, keeps 45 days) | ≈ 100 MB/h (4.0 GB on 11-27 by 23:30) | brief: 6.4 GB/day |
| `dolphin-v2` WS capture (`~/dolphin-v2/data/research/ws/msgs_*.jsonl.gz`) | ≈ 160 MB/h at 22:00 (≈ 3.8 GB/day if sustained) | not in the brief |
| `poly-smart-flow/data/run3.sqlite3` | 2.5 GB file, written in the last hour | growth rate not measured |
| `shadow_rec.py` (`~/shadow-rec`) | ≈ 1.1 GB/day | |
| `up_down_research/copypants/logs/tentacle_ws.log` | 122 MB, written in the last hour | |
| `lighter_rec.py` | ≈ 0.05 GB/day | |
| `pascal_rec.py` (new) | ≈ 0.15–0.2 GB/day (cap 0.3) | 26 MB on 11-27 by 23:28 |
| this session's caches | 1.1 GB once (`~/pascal-data/l1cache`) | deleted at the end of the session (00:10 UTC 11-28); rebuild with `pascal/l1.py prep DAY` |

**Days of disk left: about 3** (37.5 GB above the recorder's 20 GB floor ÷ ~12 GB/day), so `pascal_rec.py` stops itself around **12-01 UTC**, and `onetouch-shadow` stops at 15 GB about half a day later. That is roughly when the PX dev days (11-28..11-30) finish recording. The **holdout (12-01..12-07) cannot be recorded unless ≥ 70 GB is freed, or the writers above slow down, before ~12-01.** This session deleted nothing of anyone else's; what to prune or stop is the operator's call. Candidates by size: `~/shadow-rec` (18 GB; 11-20..11-23 are also in `~/archive/shadow-rec`), `~/archive/shadow-rec` (4 GB), `poly-smart-flow` sqlite, `dolphin-v2` ws captures, onetouch retention (45 days).

## Deviations from the brief

1. **Session launch (rule 3).** The operator started this session; I didn't run `claude --settings …` myself. I used the existing `tmux -L research` session `pascal` and added a window `rec` for the recorder.
2. **Restricted-jurisdiction list (P0)** not copied: the only terms document is on `app.pascal.trade`, which rule 1 forbids opening.
3. **Lighter premium terms on binaries:** the docs give the account-tier fee figures for perps/spot only and say so; binary fees come from the `bo` schedule. I took the latencies (0 ms maker/cancel, 140 ms taker premium; 300 ms standard) as account-wide, because the docs don't restrict them.
4. **PX conventions the brief left open** (frozen in `PX_plan.json` before any data): Pascal rounds on multiples of 50 ms, matched against the book published after the round; the Polymarket reverse-token ask is taken as 1 − long bid (only the long book is replayed); fills walk the recorded top 3 levels (a PX-A unwind remainder beyond them is priced at the 3rd level − 5¢, an unfilled PX-B hedge remainder at the 3rd level + 1¢); PX-B has no re-post within a quote epoch after a fill; PM 1 s latency is a sensitivity, not a cell.
5. **PX dev/holdout not run:** no full recorded day exists yet (by design). Commands and earliest dates are in P4.
6. **L1 cells:** 288 distinct cells per asset at 30 shares (the brief's ~320 counts "re-post after 1/5 s" for the no-pull cells too, where it changes nothing), plus 5-share results beside them. Quoting is limited to each window's own 15 minutes (Lighter lists windows 2 h early; Polymarket's 15m book is recorded only from 45 s before the start). A requote is a modify that lands at decision + 2 ms + D. The re-post price after a pull is the tracked quoted fair (within 1¢ of the current fair). A filled side is replenished 4 ms later.
7. **L1 data:** Binance = USD-M aggTrades only (`src 1`; measured receive lag 4.9 ms, the brief's 4 ms used); 11-11 starts at ~10:00 UTC (first Lighter file), so dev has 9.6 days of windows, counted as 10 in per-day figures.
8. **L1-D "does 712772 reprice < 50 ms":** answered from book changes (Lighter's book does not show owners) plus maker shares from trades, not from order-level data.
9. **V2** run on 3 of the 10 dev days (11-11..11-13), because the reference code (`lighter_flips.window`, every rule and lifetime) takes ~9 min per day; the one-shot comparison is order-for-order on those days.
10. **Python:** `~/shadow-venv/bin/python` without `-I` for the numpy scripts (numpy is in the user site); scripts live in the repo, and data directories are only read through paths.
11. **First dev replay attempt died** (cgroup memory cap, no traceback) after holding all 10 days of windows. Rewritten to one day at a time with deduped top-of-book rows; results identical on a check day; V1 re-run passes.
