# Tokyo research box (ap-northeast-1)

Notes for someone arriving with no context. Written 2026-11-24/25 by the overnight session; the facts below
were checked on the box that night unless marked otherwise. Companion: Dublin's `docs/box-dublin.md` on
branch `research/dublin-1125`.

**What this box does for this research.** It records Polymarket crypto up/down books (5m/15m) and Lighter
BTC/ETH 15m binary-option books, and runs the paper shadow of the one live candidate: **T1, Lighter ×
Polymarket arb above 4¢, BTC 15m**. Nothing here holds keys or sends orders for this research.

**The box is shared.** It also runs other people's live trading bots, dashboards and agents (list in §1.3).
Do not stop, restart, edit or "clean up" anything you did not create. No `sudo`/`systemctl` for research
sessions: write unit files into the repo and give the operator the install commands.

Machine: 2 vCPU, 7.6 GiB RAM (swap 4 GiB, usually ~2.4 GiB used), root disk 387 G (≈ 84 G free on 11-24).
Clock: chrony against the AWS time source (169.254.169.123), offset ≈ 0.2 µs on 11-24, so local receive
times are good to well under 1 ms.

---

## 1. Services and timers

### 1.1 Ours, added 2026-11-25 (installed by the operator; unit files in `deploy/`)

| unit | what it does | code | output | check | stop |
|---|---|---|---|---|---|
| `lp-arb-shadow.timer` → `lp-arb-shadow.service` | daily 00:50 UTC (`Persistent=true`): evaluates every completed UTC day from 2026-11-25 not yet evaluated (max 3 per run), compares paper vs batch for yesterday, rewrites the cumulative report. Oneshot, Nice 19, MemoryMax 1500M, CPUQuota 60%. ~1 min per day. | `lp_arb/shadow_eval.py --pending`, `lp_arb/paper_vs_shadow.py --yesterday`, `lp_arb/shadow_report.py` | `~/lp-arb/shadow/` | `systemctl list-timers lp-arb-shadow.timer`; `journalctl -u lp-arb-shadow -n 50`; `cat ~/lp-arb/shadow/report.md` / `status.json` | `sudo systemctl disable --now lp-arb-shadow.timer` |
| `lp-arb-paper.service` | real-time PAPER trader for T1 (BTC 15m): Lighter read-only WS + Polymarket market WS, evaluates the frozen trigger on every book update, logs what it would do (and paper P&L per window at settlement, once Gamma and Lighter both resolve, ≤ 30 min). Restart=always, Nice 10, MemoryMax 300M, CPUQuota 25%. ~33 MB RSS. | `lp_arb/paper.py` | `~/lp-arb/paper/YYYY-MM-DD.jsonl` (≤ 200 MB/day, 60-day retention) | `journalctl -u lp-arb-paper -n 20`; `tail -1 ~/lp-arb/paper/$(date -u +%F).jsonl` (a `health` line every 60 s) | `sudo systemctl disable --now lp-arb-paper` |
| `onetouch-shadow.service` (replaces the 11-13 unit of the same name) | one-touch MM paper shadow + raw recorder for "What price will BTC/ETH/SOL/XRP hit" markets. Restart=always, Nice 15, CPUQuota 30%, MemoryMax 600M. | `~/onetouch-mm/research/onetouch_mm/shadow.py --keep-days 45` (branch `research/onetouch-rec-1125` of rawbencis/poly-maker) | `~/onetouch-mm/research/onetouch_mm/data/rec_YYYYMMDD_HH.jsonl.gz`, `snapshots.jsonl` | `journalctl -u onetouch-shadow -n 5` (a JSON summary per minute) | `sudo systemctl stop onetouch-shadow` (SIGTERM closes the hourly file cleanly since 11-25) |

The lp-arb units run from **`~/lp-arb/code`**, a detached git worktree pinned to a reviewed commit of
`research/lp-arb`, not from `~/lighter-flips` (see §1.4). To move them to a newer commit:
`git -C ~/lp-arb/code fetch origin && git -C ~/lp-arb/code checkout --detach <commit>` then
`sudo systemctl restart lp-arb-paper`.

### 1.2 Research jobs from earlier sessions (running; leave them alone)

| unit / process | what it does | runs from | output |
|---|---|---|---|
| `updown-rec.service` (system, enabled, Nice −5) | **the Polymarket recorder** (`shadow_rec.py rec`): Binance spot+USD-M aggTrades, Polymarket Up-token top of book for every 5m/15m crypto window, trade prints of both tokens. Prunes day folders older than **14 days** (default `--keep-days`). | `~/lighter-flips` | `~/shadow-rec/` |
| Lighter recorder — **not a service**: `python3 lighter_rec.py rec --out ~/lighter-data`, a nohup process started 2026-11-11 (PID 3570579 on 11-24) | Lighter BTC/ETH 15m books (top 3), trades, Chainlink 1 s candles, outcomes. No pruning. **Will not come back after a reboot.** | `~/lighter-shadow` | `~/lighter-data/`, log `~/lighter-shadow/lighter_rec.log` |
| `shadow-latency.timer` (every 15 min at :30 s) | `latency_watch.py sample`: recorder lag stats | `~/lighter-flips` | `~/shadow-latency.csv` |
| `momentum-eval.timer` (00:25 UTC) | `momentum_shadow.py eval/report` | `~/lighter-flips` | `~/mom-res/` |
| `ohioai-match.timer` (00:45 UTC) | `ohioai_match.py` on yesterday | `~/lighter-flips` | `~/ohioai/match-*.txt` |

Installed but **disabled**: `updown-shadow.service` (older copy of the recorder, from `~/stwf2`),
`rd3-live.service` (R-D3 live micro-bot: it trades with a shared wallet — not research-shadow, do not
start), the pre-11-25 `onetouch-shadow.service` (stopped by the operator 2026-11-13 23:21).

Check a recorder is alive: `journalctl -u updown-rec -n 2` (one line per minute: rows, `pm_lag_ms`
p10/p50/p90, drops, last error) and `tail -1 ~/lighter-shadow/lighter_rec.log`.

### 1.3 Not ours: do not touch (read their outputs only)

Octopus (`~/releases`, `octopus-*.service`, `tentacle_ws`), `/home/polymaker` and poly-maker-live
(`run_paper.py` as user `polymaker`), agent-zero / `a0` (`agent-zero.service`, `a0-*` user units, the
dockerised `/a0`), `backtest.null_baskets` jobs, `poly-smart-flow-*` (collector, resolver, recorder),
`ladder-*` (live Lighter/Hyperliquid bots), `polymarket-*`, `dolphin-*`, `superscalper-*`, `statarb-*`,
`mimic-psp`, `polyparrot-*`, `quark-dashboard`, `jaguar`, `marlin`, `n8n*`, webhooks, and the weather
collectors in `.venv-research` (`collect_ws.py`, `collect_obs.py`). Also never touch `updown-rec` and the
Lighter recorder above unless the operator asks: the shadow depends on them.

### 1.4 Hazard: `~/lighter-flips` is a live working directory

`updown-rec`, `shadow-latency`, `momentum-eval` and `ohioai-match` all run with
`WorkingDirectory=/home/ubuntu/lighter-flips`, which is a git worktree of rawbencis/prediction-market-backtesting.
Whatever branch is checked out there is what those jobs import on their next start. **Never check out a
branch there that lacks `shadow_rec.py`, `latency_watch.py`, `momentum_shadow.py` or `ohioai_match.py`**, and
never edit `shadow_rec.py` there (the recorder would pick it up on its next restart). On 11-25 it is on
`research/lp-arb`, which has all four unchanged.

---

## 2. Data directories

All times are UTC. "local" = this box's receive time, `time.time_ns() // 1000` (µs since the epoch).
"exchange" = the venue's own message timestamp (ms). Prices are stored as integers in 1e-4 $.
Hourly files are named by the **local** UTC hour at write time and are appended raw (fixed-width,
little-endian, no header); read with `numpy.fromfile(path, dtype)` using the dtypes in
`lp_arb/frozen_code/lighter_eval.py` (`PM_DT`, `LB_DT`, `LT_DT`, `LC_DT`).

### 2.1 `~/shadow-rec/YYYY-MM-DD/` — Polymarket + Binance (`shadow_rec.py`, service `updown-rec`)

| file | row (struct) | fields |
|---|---|---|
| `pm-HH.bin` | `<qqBBIHHff` (32 B) | local_us, exchange_ms, asset (0 btc 1 eth 2 sol 3 xrp 4 doge), tf (5/15), epoch (window start, s), bid, ask (1e-4 $), bid_size, ask_size. **Up token only**; Down ask = 1 − Up bid. One row per change of top of book. `bid = ask = 0` marks a dropped socket: book unknown until the next row. |
| `tr-HH.bin` | `<qqBBIBBHfH32s` (59 B) | local_us, exchange_ms, asset, tf, epoch, token (0 Up 1 Down), taker side (0 BUY 1 SELL), price (1e-4), size, fee_rate_bps (as sent by the feed), transaction hash (32 bytes). From `last_trade_price`. |
| `bn-HH.bin` | `<qqBBd` (26 B) | local_us, trade_ms, src (0 spot, 1 USD-M), asset, price. Binance aggTrade. |
| `../markets.csv` | | `tf,epoch,asset,slug,condition_id,up_token` (appended per window) |
| `../markets_down.csv` | | `tf,epoch,asset,up_token,down_token` |

Size: **≈ 1.0–1.4 GB/day** (11-23: pm 1,120 MB, bn 224 MB, tr 42 MB). Retention: the recorder deletes day
folders older than 14 days (so 11-10 goes on 11-25 00:00, 11-13 on 11-28). Feed lag (11-24): Polymarket
p50 ≈ 112–117 ms, p90 ≈ 130 ms, max several seconds in bursts; Binance spot 3 ms.
Known recorder issues: Polymarket closes slow sockets with code **1013 "slow consumer: send buffer full"**
(22 minutes with such an error on 11-23, lower bound) and **8.6% of BTC 15m `pm` rows on 11-23 arrived
> 1 s after their exchange time** (backlog bursts). Both are in every shadow day's health columns.

### 2.2 `~/lighter-data/YYYY-MM-DD/` — Lighter (`~/lighter-shadow/lighter_rec.py`, nohup process)

| file | row | fields |
|---|---|---|
| `lb-HH.bin` | `<qqBBI` + 6×`Hf` (42 B) | local_us, exchange_ms, asset, tf, epoch, 3 × (bid px, size) best first, 3 × (ask px, size). One row per change of the top 3. All-zero = book unknown (reconnect). |
| `lt-HH.bin` | `<qqBBIHfBqqq` (48 B) | local_us, trade_ms, asset, tf, epoch, px, size, taker_is_buy (from `is_maker_ask`), ask_account, bid_account, trade_id. Can repeat after a reconnect: dedupe on trade_id. |
| `lc-HH.bin` | `<qqBd` (25 B) | local_us, candle_ts, asset, close: the Chainlink 1 s candles Lighter settles on. |
| `../lighter_markets.csv` | | `asset,tf,epoch,market_id,trade_start_ms,trade_end_ms` |
| `../lighter_outcomes.csv` | | `market_id,status,outcome,local_s`. **outcome 2 = Up won, 1 = Down won** (2,097/2,097 vs Polymarket on 11-13..23). |

Size ≈ 45–60 MB/day. No retention (grows forever; small). Feed lag ≈ 2 ms (p50 2.1, p90 3.4 ms on 11-24).
Maker account **712772** makes ~82% of Lighter 15m maker volume.

### 2.3 `~/lp-arb/` — ours (created 2026-11-24)

| path | content |
|---|---|
| `shadow/YYYY-MM-DD.json` | one evaluated day: `assets.btc/eth.row` (the days.csv row), `per_window` $ lists (aligned with `window_e`), `triggers` (per 500+500 trigger: local time `t_us`, window `e`, side a/b, Lighter px/size, PM px/size, edge, displayed at +100/+300/+500 ms), `fills` (ext() rows at 500+500 and 1000+1000), fee types, resolution-text hashes, settlement disagreements, journal health |
| `shadow/days.csv` | one row per (day, asset); columns in `lp_arb/shadow_eval.py` `COLS` |
| `shadow/report.md`, `shadow/status.json` | cumulative table and the pass/kill state |
| `paper/YYYY-MM-DD.jsonl` | paper trader log; kinds `start`, `window`, `trigger`, `check` (+100/+300/+500), `hedge` (+1000), `settle`, `health`, `error`, `cap`, `stop`. `t_local` is epoch seconds. |
| `paper/compare-YYYY-MM-DD.json` | paper vs batch matching for that day |
| `shadow-test/`, `paper-test/`, `onetouch-test/` | the 11-24 test runs (can be deleted) |
| `code/` | detached worktree the units run from |

### 2.4 Other recordings (never delete)

| path | content | size |
|---|---|---|
| `~/onetouch-mm/research/onetouch_mm/data/` | `rec_YYYYMMDD_HH.jsonl.gz`: one JSON per line `{"t": local s, "s": "pm"/"bn"/"meta", "d": raw}`. `pm` = raw Polymarket market-channel messages for every Yes/No token of the price-hit markets; `bn` = Binance bookTicker mid `[asset, mid]` throttled to 1 s or 0.5 bp moves; `meta` = market metadata and resolutions. `snapshots.jsonl` = the paper MM's state each minute. Old data: 11-12 21h and 11-13 10h–23h (3 truncated tails). | see the 11-25 report, T4 |
| `~/stwf2-cache/` | one npz per BTC 5m window, 2026-06-01..07-31, + settlements.csv | 9.6 G |
| `~/binance_1s/` | BTCUSDT 1 s closes per day (.npy), 07-13..11-12 | 155 M |
| `~/btc1s/` | BTCUSDT 1 s monthly npz, June–July | 14 M |
| `~/poly-smart-flow/data/books.sqlite3` | psf books recorder DB, 10-18..11-04 (recorder stopped 11-04) | 29 G |
| `~/poly-smart-flow/data/run3.sqlite3` | live psf collector DB (written now; not ours) | 2.2 G |

Retention plan: `deploy/tokyo-retention.md`.

---

## 3. Evaluators: how to run

From the repo root (`~/lp-arb/code` or `~/lighter-flips`), system `python3` (numpy, websockets 10.4):

```bash
# T1 batch shadow for one completed UTC day (≈ 1 min, ≈ 720 MB peak RSS; run heavy jobs capped):
systemd-run --user --scope -q -p MemoryMax=1500M -p CPUQuota=60% nice -n 19 \
    python3 lp_arb/shadow_eval.py --day 2026-11-25            # -> ~/lp-arb/shadow/2026-11-25.json, days.csv
python3 lp_arb/shadow_eval.py --pending                       # every unevaluated day from 11-25 (max 3)
python3 lp_arb/shadow_report.py                               # cumulative table + pass/kill -> report.md
python3 lp_arb/paper_vs_shadow.py --day 2026-11-25            # paper decisions vs batch
python3 lp_arb/paper.py --out /tmp/paper-test --minutes 30    # paper trader in the foreground

# T1 holdout driver (registered read and the 11-24 fee correction):
python3 overnight/t1_arb.py day --day 2026-11-20 --fee-mode census     # also: inforce (Gamma), frozen07 (0.07)
python3 overnight/t1_arb.py agg --since 2026-11-13 --until 2026-11-23 --fee-mode census
python3 overnight/t1_desc.py census 2026-11-13 2026-11-23

# one-touch replay on recorded data (truncated hours are skipped, not fatal):
cd ~/onetouch-mm/research/onetouch_mm && .venv/bin/python replay.py data/ LAT=1 PULL_SIG=3 SIZE=100
```

The shadow evaluator does not re-implement the rule. It calls the frozen `lighter_eval.market_metrics`
(vendored byte-identical copy in `lp_arb/frozen_code/`, blob hashes checked at start) and
`overnight/t1_arb.ext`. Tested 11-24 on the holdout days: identical to the holdout per window, except that
the holdout driver skipped each day's 23:45 window and the shadow evaluator includes it.

Fees: `lp_arb/fees.py` — Dublin's census (`lp_arb/census/fees_by_window.csv`, stops 11-24 10:55) where it
covers a window, 0.07 for uncovered windows before the 2026-11-21 00:03 switch, Gamma's `feeSchedule`
after. From 11-25 every window takes Gamma's rate (BTC 15m 0.05, `crypto_btc_5m_15m_fees`).

---

## 4. Latency facts

| path | value | source |
|---|---|---|
| Lighter market data → this box | ≈ 2 ms (p50 2.1, p90 3.4 ms, local − exchange ts) | paper trader and recorder, 11-24 |
| Polymarket market data → this box | ≈ 116 ms (p50 112–117, p90 ≈ 130 ms; bursts of seconds) | same; `updown-rec` journal |
| Tokyo → London order path (Polymarket CLOB) | ≈ 120 ms one way | operator brief / earlier sessions; R-D3 live acks took ~860 ms send→ack round trip from Tokyo |
| Polymarket taker hold (matching delay) | ≈ 150 ms | earlier sessions (`clob-market` info) |
| Lighter taker hold | 300 ms standard, 140 ms premium | earlier sessions |
| Tokyo → Dublin box (54.73.242.206) | **TCP connect 202–206 ms round trip → ≈ 101 ms one way** (ICMP is blocked) | measured 2026-11-24 21:54 UTC, 3 connects to :22 |

## 5. Why one box is enough

**Paper stage.** The holdout that passed was measured with exactly this box's receive times for both
venues (Lighter ~2 ms, Polymarket ~116 ms late). The shadow must reproduce those conditions, so it uses the
same two recorders on the same box; a Dublin feed would change the measurement, not improve it. Dublin's
recorders were stopped on 11-24 anyway.

**A later live probe.** The order of events is fixed: the Polymarket hedge can only be sent after the Lighter
fill is known, and the fill is known in Tokyo first (Lighter matches ~2 ms away). From there:
- Tokyo → London directly: ≈ 120 ms + 150 ms Polymarket hold ≈ 270 ms.
- Relay the fill to Dublin (≈ 101 ms one way, measured) and send from Dublin (Dublin → London ≈ 10–15 ms;
  typical figure, not measured here) + 150 ms hold ≈ 265 ms.

The two are the same to within the noise, and the Tokyo-only path has one fewer process, socket and box to
fail. Dublin only helps if Polymarket's data arrives there earlier *and* the decision is made there, which
would mean seeing Lighter ~100 ms late instead — the trade's edge decays with delay on both legs (T1
delay grid: +3.6¢ at 500+500, +1.1¢ at 1 s + 1 s). One box (Tokyo) is the plan.

## 6. Registered and frozen tests on this box

| test | frozen file (commit) | data | read | status |
|---|---|---|---|---|
| T1 Lighter × PM arb > 4¢ | `frozen/t1_lighter_pm_arb.json` (8537010, rule from 11-12 18:50) | holdout 11-13..23 | read 2026-11-24 (Gamma fee); fee-corrected rerun 2026-11-24 22:00 (census fee) | **PASS** both times |
| T1 shadow (14 days, BTC) | `frozen/t1_shadow_candidate.json` (b8b35e6) + `frozen/t1_shadow_eval_spec.json` (4eb810a, before any 11-25 data) | 2026-11-25 → 14 counted days | daily, automatic; final read after the 14th counted day (≈ 12-09) | running |
| T2 legs without communication | `frozen/t2_grid.json`, `t2_cells.json` | 11-13..23 | read 11-24 | CLOSED |
| T3 level map | `frozen/t3_cells.json` | June–July | read 11-24 | CLOSED |
| One-touch MM | none frozen yet | from 2026-11-25 | **not before 2026-12-16** (≥ 3 weeks and ≥ 50 resolved levels) | recording |

Freeze before reading: any new rule goes to `frozen/*.json` and is committed before its data is read.

## 7. Git branches holding this box's work

| repo | branch | what |
|---|---|---|
| rawbencis/prediction-market-backtesting | `research/lp-arb` | lp_arb code, units, this doc, retention doc, 11-25 report |
| same | `research/overnight-1124-tokyo-code` | 11-24 overnight code (`overnight/`), frozen T1–T3 files |
| same | `research/overnight-1124-tokyo` | 11-24 report and tables (`reports/tokyo/`) |
| same | `research/lighter-shadow` | `lighter_rec.py`, `lighter_eval.py` (the frozen T1 rule @1348b2e), `lighter_latency.py` |
| same | `research/dublin-1125` | Dublin's fee census and box notes (read-only for us) |
| rawbencis/poly-maker | `research/onetouch-rec-1125` | one-touch recorder fixes (clean close, keyset discovery, pruning, truncation-tolerant replay) |
| same | `research/onetouch-mm-shadow` | the original one-touch shadow |
