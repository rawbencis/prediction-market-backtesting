# The Dublin research box

_Written 2026-11-24 (Dublin 11-25 session) for a reader with no context. Source facts: `out/d1125/factsheet.md` on the box (not committed), the reports in `reports/dublin/`, and the code. Where a fact comes from one place, the place is named._

- **Host:** AWS eu-west-1, `ip-172-31-25-97`, instance type c7a.large (2 vCPU, 3.8 GB RAM, **no swap**), 58 GB root disk. User `ubuntu`. It sits next to Polymarket's London CLOB.
- **Python for everything:** `~/shadow-venv/bin/python` (Octopus uses `~/octopus-venv`).
- **What it is for:** recording Polymarket crypto up/down markets (5m, 15m and, once installed, 1h/4h) plus reference feeds; running blind or registered evaluators; offline research. **No orders and no keys** are used by anything listed here.

**Before you do anything:**
1. Memory is tight. Any job that reads recordings runs in a capped scope: `systemd-run --user --scope -q -p MemoryMax=800M -p CPUQuota=80% nice -n 19 <cmd>`.
   - On 11-24 at 10:44 and 10:52 the kernel OOM-killed two 1 GB jobs.
   - `updown-rec` alone peaks at 1.8 GB.
2. Nothing heavy between 00:12 and 01:10 UTC. The daily evaluators run then.
3. **State on 2026-11-24 17:00 UTC: `updown-rec`, `updown-act` and `feeds-rec` are stopped and DISABLED.** This was a manual `systemctl disable --now` at 10:59:49 UTC. Nothing has been recorded since. See `reports/dublin/2026-11-25-dublin.md` §0.

---

## 1. Services and timers

All are system units in `/etc/systemd/system/`, `User=ubuntu`. The unit sources live in the repo: `~/dublin_setup.sh` (`updown-rec`, `shadow-latency`, `momentum-eval`, `ohioai-match`), `~/dublin-retest/deploy/` (`feeds-rec`, `rd3-eval`, `favhedge-repick`) and `~/octopus/deploy/`.

**Commands for any unit:**
- Stop: `sudo systemctl stop <unit>`.
- Stop permanently: `sudo systemctl disable --now <unit>`. For a timer, disable `<unit>.timer`.
- Status: `systemctl status <unit>`; timers: `systemctl list-timers`.

### Recorders

#### `updown-rec`: Polymarket top of book and prints, plus Binance
- **State 11-24:** disabled, inactive.
- **Runs:** `~/lighter-flips/shadow_rec.py rec --out ~/shadow-rec --keep-days 60`. Nice −5, Restart=always.
- **Writes:** `~/shadow-rec/DAY/{pm,tr,bn}-HH.bin`; `markets.csv`, `markets_down.csv`.
- **Logs:** `journalctl -u updown-rec`. One line a minute: `rows pm=… bn=… tr=… conns=… pm_lag_ms=[p10,p50,p90] … drops=… last_err=…`.
- **Health:**
  - `pm_lag_ms` p50 ≈ 9–10.
  - `conns` 20–40.
  - Today's `pm-HH.bin` grows every minute.
  - `~/shadow-latency.csv` has `pm_n` > 0.

#### `updown-act`: taker census from the Polymarket RTDS `activity/trades` stream (public)
- **State 11-24:** disabled, inactive.
- **Runs:** `~/lighter-flips/act_rec.py rec --out ~/shadow-rec`.
- **Writes:** `~/shadow-rec/DAY/act-HH.bin`.
- **Logs:** journal. Line: `act rows=… lag_ms=[~160,~240,~340] conns stalls`.
- **Health:** `rows` grows; lag p50 < 400 ms.

#### `feeds-rec`: Chainlink and TWAP60 (legacy RTDS), Coinbase, Deribit, Polymarket BTC 5m/15m 5-level books, BTC/ETH 1h books
- **State 11-24:** disabled, inactive.
- **Runs:** `~/dublin-retest/repo/feeds_rec.py --out ~/feeds-rec`. MemoryMax 500M, Nice 10. **No retention** in the installed version.
- **Writes:** `~/feeds-rec/DAY/<feed>-HH.jsonl.gz`.
- **Logs:** `~/dublin-retest/out/feeds_rec.log`. Every 5 min: `rows {...} stats {..._reconnects}`.
- **Health:** today's `twap60-HH.jsonl.gz` and `pmbook-HH.jsonl.gz` grow.
- **Proposed drop-in (not installed):** `deploy/feeds-rec.retention.conf` on branch `research/hourly-rec`. It sets `--keep-days 30 --no-coinbase --no-deribit` and runs from `~/dublin-retest/hourly-rec`.

#### `hourly-rec`: BTC/ETH 1h and 4h up/down books, prints, top-5 depth, REST snapshots, resolutions
- **State 11-24:** not installed.
- **Unit:** `~/dublin-retest/hourly-rec/deploy/hourly-rec.service`. Restart=always, Nice 10, MemoryMax 300M, CPUQuota 20%.
- **Runs:** `~/dublin-retest/hourly-rec/hourly_rec.py --out ~/hourly-rec --keep-days 60`.
- **Writes:** `~/hourly-rec/DAY/{pm,tr,bk,rs}-HH.bin`, `DAY/resolutions.jsonl`, `markets.csv`.
- **Logs:** `journalctl -u hourly-rec`. One line a minute: `rows {...} MB= conns= connects= reconnects= windows= resolved= rest_mismatch= lag_ms= maxrss_MB=`.
- **Health:**
  - `conns` = 4–8.
  - `reconnects` stays low.
  - `resolved` rises about 2 per hour (4 at 4h boundaries).
  - `lag_ms` p50 ≈ 10.
  - `maxrss_MB` < 100.

#### `octopus-shadow`: Octopus paper copy-trader on the RTDS signal (paper backend, maker off)
- **State 11-24:** enabled, running since 11-14.
- **Runs:** `~/octopus/tentacle_ws.py`, with env `TENTACLE_BACKEND=paper` and `OCTOPUS_STATE_DIR=~/octopus-shadow`.
- **Writes:** `~/octopus-shadow/` (paper_state.json, ws_state.json, signal_latency/).
- **Logs:** `~/octopus-shadow/logs/tentacle_ws.log`.
- **Health:** the log advances; RSS ~75 MB.

#### `octopus-shadow-sweeper.timer`: paper settlement, exits and risk (never buys)
- **Schedule:** every minute at :30.
- **Runs:** `python3 -c "import tentacle_paper as tp; tp.run_once()"`.
- **Logs:** `~/octopus-shadow/logs/sweeper.log`.

### Evaluators (one-shot services fired by timers)

**Do not read their outputs** unless you are the registered reader on the read date (§5).

#### `shadow-latency.timer`: recorder receive lag and box load
- **Schedule:** every 15 min at :00:30.
- **Runs:** `~/lighter-flips/latency_watch.py sample --rec ~/shadow-rec --log ~/shadow-latency.csv`.
- **Writes:** `~/shadow-latency.csv`.
- **Health:** the newest row has `pm_n` > 0. It is 0 from 11-24 11:15, because the recorders are off.

#### `breakout-eval.timer`: R-D2 per-day evaluation
- **Schedule:** 00:20 UTC.
- **Runs:** `~/lighter-flips/breakout_shadow.py eval --rec ~/shadow-rec --res ~/breakout-res --taker-delay-ms 50`.
- **Writes:** `~/breakout-res/bo-DAY.csv` (**blind**).
- **Logs:** journal.

#### `momentum-eval.timer`: R-D1 per-day evaluation and report
- **Schedule:** 00:25 UTC.
- **Runs:** `momentum_shadow.py eval …` then `report > ~/mom-res/report.txt`.
- **Writes:** `~/mom-res/` (**blind**).

#### `rd3-eval.timer`: R-D3 per-day evaluation from frozen code
- **Schedule:** 00:40 UTC.
- **Runs:** `~/dublin-retest/deploy/rd3_once.sh`. It checks `frozen/rd3-code/MANIFEST.sha256` first.
- **Retries:** exit 2 = day not settled, so `Restart=on-failure` retries every hour.
- **Writes:** `~/rd3-res/rd3-DAY.csv` (**blind**).
- **Logs:** `~/rd3-res/loop.log`, which holds row counts only and is the only file there you may read.

#### `ohioai-match.timer`: OhioAI fills matched to prints, yesterday
- **Schedule:** 00:45 UTC.
- **Writes:** `~/ohioai/match-DAY.txt`.
- **State 11-24:** last run failed.

#### `favhedge-repick.timer`: one-shot fav-hedge B re-pick
- **Schedule:** fired 2026-11-21 00:30.
- **State:** **failed (OOM at 1200M)**; `frozen_B.json` was never written.
- **Re-run:** `sudo systemctl start favhedge-repick` (it is guarded by `ConditionPathExists=!frozen_B.json`).

#### `rd45-eval` (R-D4/R-D5)
- **Not installed.** The units are in `~/overnight-wt/deploy/rd45-eval.{service,timer}`.

**Not on the box:** no user crontab; `sig_shadow` is not installed.

---

## 2. Data directories

All times are UTC. Hourly files are named by the **local wall-clock hour at write time**, not by exchange time.
- `local_us`: this box's receive time (µs since epoch, chrony-synced).
- `exchange_ms`: the venue's own timestamp.
- Polymarket prices in the binary rows are **1e-4 $ integers** (5000 = 50¢).
- Asset index in `shadow-rec`: 0 btc, 1 eth, 2 sol, 3 xrp, 4 doge (`shadow_rec.ASSETS`). In `hourly-rec`: 0 btc, 1 eth.

### `~/shadow-rec/YYYY-MM-DD/` (writers `updown-rec` and `updown-act`; retention 60 days by `shadow_rec.py` housekeeping)

| file | struct (little endian) | fields | writer |
|---|---|---|---|
| `pm-HH.bin` | `<qqBBIHHff` (34 B) | local_us, exchange_ms, asset, tf (5/15), epoch (window start, s), bid, ask, bid_size, ask_size. UP token only; DOWN is the mirror. bid=ask=0: socket dropped, book unknown until the next row | `lighter-flips/shadow_rec.py` `on_msg` |
| `tr-HH.bin` | `<qqBBIBBHfH32s` (64 B) | local_us, exchange_ms, asset, tf, epoch, token (0 UP / 1 DOWN), taker side (0 BUY / 1 SELL), price, size, fee_rate_bps, tx hash. From the `last_trade_price` event | same |
| `bn-HH.bin` | `<qqBBd` (26 B) | local_us, trade_ms, src (0 spot / 1 USD-M), asset, price. Binance aggTrade, `<asset>usdt` | `shadow_rec.py` `binance` |
| `act-HH.bin` | `<qqIBBIBBfff20s32s` (106 B) | local_us, server_emit_ms, trade_ts (s), asset, tf, epoch, token, side, price, size, fee (USDC; **> 0 marks the taker row**, maker rows −1), wallet (20 B), tx hash. One row per participant of a fill | `lighter-flips/act_rec.py` `pack` |
| `../markets.csv` | text | tf, epoch, asset, slug, condition_id, up_token | `shadow_rec.py` |
| `../markets_down.csv` | text | tf, epoch, asset, up_token, down_token | same |

- **numpy dtypes:** `dsim.PM_DT/BN_DT/TR_DT` (`~/dublin-retest/repo/dsim.py`) and `act_rec.ACT_DT`.
- **Readers:** `act_rec.read()`, `pmlat.py` (hour-streaming).
- **Size per day** (11-15..23 mean):

| stream | GB/day |
|---|---|
| pm | 1.03 |
| bn | 0.20 |
| act | 0.17 |
| tr | 0.04 |
| **total** | **1.43** |

- **zstd -10 ratio:** pm 4.0×, bn 3.8×, act 3.4×, tr 1.45×.
- **Days on disk:** 11-14 09:22 → 11-24 10:59.

### `~/feeds-rec/YYYY-MM-DD/<feed>-HH.jsonl.gz` (writer `feeds-rec`; installed version has no retention)

One JSON object per line, gzip level 5. Common fields: `r` = local ns, `x` = venue ms.

| feed | fields | GB/day | still needed for |
|---|---|---|---|
| `chainlink` | s (btc…doge), v, fa (full-accuracy string), ts (RTDS envelope ms); `snap:1` for the 2-min snapshot after a reconnect | 0.011 | reference |
| `twap60` | same. **This is the settlement source of the 5m/15m/1h/4h series:** Gamma `priceToBeat`/`finalPrice` equal the row stamped `x` = window start/end exactly (checked 11-24) | 0.011 | settlement and H1 |
| `coinbase` | the raw Coinbase `match`/`ticker` message, plus r and x | 0.30 | research closed (11-24 D4) |
| `deribit` | trades: ch, p, a, side, id; ticker: ch, bid, bq, ask, aq, mark, idx, last | 0.025 | research closed |
| `pmbook` | BTC 5m/15m: slug, tok (up/down), b and a = top-5 levels `[[price, size], …]` on every change; `ev:"trade"` rows (p, sz, side, fee, tx); `ev:"drop"` on reconnect | 0.95 | the `0xc387c2a4` watch (ladder classes) until 12-07 |
| `pmbook1h` | the same for legacy BTC/ETH hourly slugs. None found on 11-20; the slug format is outdated | 0 | — |

- **Writer:** `~/dublin-retest/repo/feeds_rec.py`.
- **Caveats:**
  - The last hour before a stop is a truncated gzip, so read it with `try/except EOFError`.
  - The `pmbook` feed has a few corrupt hours, which the 11-19 ladder report excludes.
- **Total:** 1.29 GB/day.

### `~/hourly-rec/YYYY-MM-DD/` (writer `hourly-rec`; retention 60 days)

| file | struct | fields |
|---|---|---|
| `pm-HH.bin` | `<qqBBIHHff` | as shadow-rec `pm`; tf = 60 or 240 |
| `tr-HH.bin` | `<qqBBIBBHfH32s` | as shadow-rec `tr` |
| `bk-HH.bin` | `<qqBBIBBB` + 10 × `Hf` (85 B) | local_us, exchange_ms of the last applied book event, asset, tf, epoch, token, n_bids, n_asks, then 5 bid levels (price, size) best first and 5 ask levels, zero-padded. Sampled each second; written when the top 5 changed or 10 s passed, so forward-fill |
| `rs-HH.bin` | same as `bk` | REST `GET /book` every 60 s per token; exchange_ms = the REST book's own timestamp |
| `resolutions.jsonl` | JSON | slug, asset, tf, epoch, priceToBeat, finalPrice, outcomePrices, umaResolutionStatus, closedTime, resolutionSource, description (resolution text), cryptoMarketConfig, feeSchedule, conditionId, tokens |
| `../markets.csv` | text | tf, epoch, asset, slug, condition_id, up_token, down_token |

- **Size:** 7.05 MB/h ≈ 0.17 GB/day (11-24 test, 17:48–19:10 UTC; `pm` 72%, `bk` 27%), about 45 MB/day after zstd. 60 days raw ≈ 10 GB, so archive days ≥ 2 days old or lower `--keep-days`. Report 11-25 §3.

### Other directories

| path | what |
|---|---|
| `~/shadow-latency.csv` | per-15-min recorder lag percentiles |
| `~/order-probe.csv`, `.log` | the 11-14 order probe (post-only 5 @ 1¢), source of the `dublin` latency |
| `~/mom-res`, `~/breakout-res`, `~/rd3-res` | **blind registered outputs: do not read before the read date** |
| `~/dublin-1124/` | intermediates of the 11-24 D1–D5 work (settle.csv, d1b/*.pkl, d2/*.npz) |
| `~/archive/` | target of `deploy/archive-recordings.sh` (does not exist until that script is first run) |
| `~/sports_research/` | the operator's sports in-play work; not ours |

---

## 3. Simulation scenarios

| scenario | book seen (ms) | order on book | cancel lands | taker hold | implemented in | calibrated against |
|---|---|---|---|---|---|---|
| dsim `dublin` | the day's measured pm lag | the day's probe send→visible p50 (~22–26) | probe cancel p50 (~21) | 50 (assumed) | `~/dublin-retest/repo/dsim.py` L36-46 | order probe on this box, 11-14/15 (`~/dublin-retest/MORNING.md` §3) |
| dsim `tokyo` / `colo` | 116 / 1 | 120 / 1 | 120 / 1 | 50 | dsim.py | assumptions |
| pmlat `dublin`, `dublin_p90`, `tokyo` | 9 / 23 / 116 | 22 / 52 / 120 | 21 / 69 / 120 | 50 | `~/overnight-wt/pmlat.py` L33-36 | probe percentiles |
| pmlat `live_ack`, `live_ack200`, `live_lean` | 9 | 250 / 200 / 55-or-157 | 130–136 | 150 | pmlat.py L37-43 | live F7i bot on "dublin 2", 11-17 (`2026-11-17-f7i-latency.md`) |
| **`live`** | 9 | 55 in a calm book, else 190 | signal + 90, and never before on-book + 90 (`ackc`) | 150 | `~/overnight-wt/f9_live.py` | Polymarket private order feed of the live bot, 11-17/18 (`2026-11-18-live-scenario.md` §1). Sim fills 1.6× live, 0 partials vs 24% live (`2026-11-18-alts.md` §2) |
| `live_q` | 9 | sampled: p10 106, p50 190, p90 559 | as live | 150 | f9_live.py | same |
| R-D3 `dublin` | box's Binance receipt + 1 ms | day's probe send p50 (22 default) | — | 50 | `~/dublin-retest/frozen/rd3-code/rd3_eval.py` | probe |
| MM replay | 10 ms book; fills known 1 s late; 5 s cycle | 150 | 100 | — | `~/poly-maker-mm/research/mm_replay.py`, `strict_maker.py` | assumptions (ABLATION §2) |

**Fill models:**
- **`pess`** (dsim maker default): we are behind all displayed size, and only prints at our price advance us. `prop` and `front` are the alternatives.
- **`dsim_strict.maker_strict`** (`~/overnight-wt/dsim_strict.py`, env `PMLAT_STRICT=1`): a through-print fills at most (print size − queue ahead).
- **The 15m harness** is `pmlat.py` plus `p1_15m.py` / `p2_*.py` / `p3_*.py`. The **5m harness** is `pmlat.py` plus `f7_altpull.py`, `p2_join.py` and `alt_bid.py`.

The scenario that matches this team's real speed is **`live`**. `dublin` is the box's floor, and nothing passed at `live`.

---

## 4. Evaluators and research scripts (run with `~/shadow-venv/bin/python`, inside a capped scope)

**Census** (`~/lighter-flips/act_rec.py`):
- `top --rec ~/shadow-rec --days 2026-11-20 --asset btc --tf 5`: wallets by volume.
- `pnl --rec ~/shadow-rec --days … --min-usd 2000`: wallets by settled PnL.
- `profile --wallets 0x523c 0x13c6 --asset btc --tf 5`
- `fills --wallet 0x239e726f --asset btc --tf 15 --summary --no-binance`: every fill with book state and markouts.

**Fair value, lead-lag, latency:**
- `fair_map.py --rec ~/shadow-rec --days … --asset btc --tf 5 --src fut --lat-ms 72`: empirical fair-value map. Closed for takers.
- `latency_watch.py report --log ~/shadow-latency.csv`
- `leadlag.py`, `shadow_eval.py eval|report`, `ohioai_match.py`, `ohioai_copy.py`.

**Ladder** (`~/overnight-wt`):
- `mk_level.py DAY …` writes `out/lvl/lvl-DAY.csv.gz`; then run `lvl_report.py`.
- Wallet version: `env LVL_W=<wallet> LVL_TF=15 PYTHONPATH=~/overnight-wt:~/dublin-retest/repo python dublin1124/mk_level_w.py DAY`.

**Strategy simulators** (`~/overnight-wt`):
- `f7_altpull.py DAY SCEN improve`
- `p2_join.py DAY live btc 15` (with `PMLAT_STRICT=1`)
- `alt_bid.py DAY ASSET`
- `ld_sim.py DAY SCEN`
- `holdout_eval.py DAY`

**Dublin 11-24 (D1–D5)** (`~/overnight-wt/dublin1124/`; outputs in `~/dublin-1124/`; exact invocations in its `queue*.sh`):
- `d1_rows.py WALLET ASSET TF DAY OUT.pkl`, then `d1_report.py` / `d1b_report.py`.
- `d2_samples.py --asset btc --tf 5 --days … --out X.npz [--final] [--basis]`, then `d2_cells.py grid|final|cells X.npz FEE [CELLS.json]`.
- `d3_taker.py DAY ASSET TF`, `d3_report.py dev|holdout`, `d3_altbid.py`.
- `d4_atlas.py DAY` (one day at a time; two days OOM at 800M).
- `d5_blocks.py TF`.
- `prefetch.py`: Gamma winners and fee schedules for 11-14..23.

**Dublin 11-25:**
- `dublin1125/d1_fees.py --rec ~/shadow-rec --start 2026-11-11 --out reports/dublin/fees_by_window.csv`
- `dublin1125/d1_switch.py out/d1125/taker_rows.npz` (needs `--rows-out` from the first)

**MM replay** (`~/poly-maker-mm`):
- `research/mm_replay.py cache --days 2026-11-16 --out logs/mm/cache`
- `research/mm_replay.py run --days … --cache … --klines … --winners …` (see `run_ablation.sh`)
- `research/mm_leaks.py logs/mm/ab/00_base`

**`~/dublin-retest/repo`:**
- `dsim.py winners|v2|v3 --day D`
- `fav_hedge.py stage1|stage2|stage3 …`
- `atlas.py`, `final_minute.py`, `settle_check.py`, `probe_summary.py`

**Archive and fees:**
- `deploy/archive-recordings.sh` (this repo; dry run by default)
- `reports/dublin/fees_by_window.csv` (fee by window, 11-11..11-24)

---

## 5. Registered and frozen tests

Read each **once**, on or after its date. Exact commands are in `reports/dublin/2026-11-25-dublin.md` §4.
- **All evaluators charge a flat 0.07 taker fee.** The real fee fell on 2026-11-21 ~00:03 UTC (BTC 0.05, alts 0.01), so post-switch BTC numbers are conservative.
- **The recorders stopped 11-24 10:59.** Every window that reaches past that date is incomplete until they are re-enabled.

| test | registration | what | counting window | read date | pass | needs |
|---|---|---|---|---|---|---|
| R-D1 momentum taker | `~/lighter-flips/momentum_shadow.py` docstring; `prompts/overnight-2026-11-16.md` L17 | BTC 5m, Binance spot ≥ 2 bp/5 s, `tokyo` | from 11-15 (pass `--since 2026-11-15`) | ≥ 11-29 | ≥ 14 d, ≥ 500 fills, ≥ +1¢, day-CI low > 0 | shadow-rec pm, bn |
| R-D2 late breakout | `breakout_shadow.py` docstring | BTC 5m last 60 s, first ask to X¢ | from 11-15 (`--since 2026-11-15`) | ≥ 11-29 | same | shadow-rec pm |
| R-D3 Binance-move taker | `~/dublin-retest/frozen/R-D3.md` | USD-M ≥ 2 bp/500 ms, FAK at ask + 1¢, `dublin` | from 11-15 | ≥ 11-29 (14 settled days) | ≥ 14 d, ≥ 1,000 fills, ≥ +1¢, CI low > 0, tokyo worse | shadow-rec pm, bn; order-probe.csv. **No aggregation script exists** |
| R-D4 / R-D5 F7i improve-maker | `frozen/F7i.json`; 11-17 report §4 | BTC 5m maker pulled on alt moves; B = R-D4, A = R-D5 | from 11-17 | ≥ 12-01 (reconstructed) | R-D4 ≥ +1¢ and markout ≥ +0.4¢; R-D5 ≥ +1.5¢ and ≥ +0.8¢; ≥ 14 d, ≥ 500 fills | shadow-rec pm, tr (BTC + 4 alts 5m). **Evaluator not installed;** run per day first |
| fav-hedge A | `~/dublin-retest/fav-hedge/frozen.json` | cfg 1392 favourite hedge | holdout 11-21..27 | ≥ 11-28 | market-CI low > 0, ≥ $3/day, ≥ 300 entries | shadow-rec pm, tr |
| fav-hedge B | `favhedge-repick` | re-pick of A on 11-14..20 | 11-21..27 | — | **never frozen (OOM)** | — |
| D1b watch `0xc387c2a4` BTC 15m | `frozen/D1b_c387_watch.json` | wallet's maker buys, settled ¢/$ | markets 11-24..12-07 | once ≥ 12-08 | CI low > 0 and ≥ +1¢/share | shadow-rec act and pm; pmbook for ladder classes. **Do not read its 11-24+ rows before then** |
| H1 1h/4h fair value | `frozen/H1_plan.json` | TWAP60-aware FV taker | dev = first 21 days after the start date, holdout = next 14 | after D + 35 d | holdout ≥ +1¢, 90% CI low > 0 | hourly-rec and feeds-rec twap60 |
| Already read (closed) | `frozen/D2.json`, `D2_final_watch.json`, `F1`, `F3i`, `F4`, `F6`, `F8`, `F9`, `F15`, `late_dump` | — | — | read | failed | — |

---

## 6. Measured latencies

**Order path, this box** (order probe 11-14/15, post-only 5 @ 1¢; MORNING §3):
- Send→visible p10/p50/p90 = 17/26/290 ms.
- Cancel→gone 16/21/484 ms.
- Earlier ack RTT ~37 ms.

**Order path, live bot ("dublin 2")** (`2026-11-18-live-scenario.md` §1):
- Order on book after a book move: p10 106, **p50 ~190**, p90 559 ms; 55 ms in a calm book.
- Cancel lands +90 ms.
- **Taker hold ~150 ms** (`itode: true`; 50 ms was the earlier assumption).

**Recorder receive lag** (`~/shadow-latency.csv`, 967 × 15 min, 11-14..24):
- Polymarket book p50 9.4 ms (p90 17, p99 68).
- Prints 9.2 ms.
- Binance spot ~102 ms (~104–109 in other reports).
- Binance USD-M: computed per day by `dsim._bn_delay`, not reported.

**Relative feed timing** (11-24 D4):
- Coinbase ≈ Binance (Binance first 55–74%, median gap 10–39 ms).
- Deribit 100 ms ticker trails by ~230–310 ms.
- Chainlink (RTDS) trails exchanges by 0.2–0.9 s.
- Absolute Coinbase/Deribit/Chainlink delays were not measured.

**RTDS activity census lag** p50 ~220–250 ms. Its emit is ~1.8 s after the print by RTDS timestamp.

**Exchange move → BTC book reprice:** ~200–290 ms (5m), ~130–145 ms (15m).

**`hourly-rec` test 11-24:** see the report §3.

---

## 7. Git: where this box's work lives

| checkout | branch | pushes to | notes |
|---|---|---|---|
| `~/overnight-wt` (worktree of `~/lighter-flips`) | `research/dublin-1125` (from `research/overnight-1124-dublin`) | origin is **https without credentials**. Push with the ssh URL: `git push git@github.com:rawbencis/prediction-market-backtesting.git <branch>` | reports in `reports/dublin/`, code in `dublin1124/` and `dublin1125/`, `frozen/`, `deploy/`. Untracked `out/` is intermediate output |
| `~/lighter-flips` | `research/shadow-trades` | same https remote | **the systemd checkout** for `updown-rec`, `updown-act` and the evaluators: don't switch its branch. 9 commits behind origin (candle_count) |
| `~/dublin-retest/repo` | `research/dublin-retest` | ssh | **the checkout `rd3-eval` and `favhedge-repick` run from**, and the installed `feeds-rec`: don't switch its branch |
| `~/dublin-retest/hourly-rec` (worktree of the above) | `research/hourly-rec` | ssh | `hourly_rec.py`, the patched `feeds_rec.py`, `deploy/hourly-rec.service`, `deploy/feeds-rec.retention.conf`. The proposed units run from here |
| `~/poly-maker` | `research/overnight-shadow-arena` | ssh (poly-maker) | shadow arena; pushed 11-24 |
| `~/poly-maker-mm` | `feat/mm-edge-recovery` | ssh | MM replay and ABLATION_REPORT |
| `~/octopus` | `feat/signal-sources` | ssh (octopus) | Octopus paper shadow runs from here |

**Pushed research branches of prediction-market-backtesting from this box:**
- `research/overnight-1116`, `-1117`, `-1118`
- `research/overnight-1124-dublin`
- `research/dublin-1125`
- `research/dublin-retest`
- `research/hourly-rec`

**Push rules used so far:**
- Research branches only; never `main`, no force, no deletes.
- Dry run first.
- Grep the staged diff for key-like strings; no blobs over 5 MB.
