# Research index: Polymarket crypto up/down (Dublin and Tokyo boxes), 2026-11

**How to use this page:**
- Find the family, then open the report section.
- The code is in the named file on the named branch of `rawbencis/prediction-market-backtesting`, unless another repo is named.
- Reports live in `reports/dublin/` on `research/overnight-1124-dublin` and later branches. "11-24 §D2" means `reports/dublin/2026-11-24-dublin.md`, section D2.
- Box layout, services and data formats: `docs/box-dublin.md`.

**Overall verdict (11-24 §1):**
- No candidate clears +1¢/share net at `live` speed out of sample.
- Polymarket 5m/15m crypto up/down is closed as a venue at our speed.

**Open items:**
- The registered blind tests (R-D1..R-D5, fav-hedge A, the `0xc387c2a4` watch).
- H1, the 1h/4h series.

| family | status | report | code | branch |
|---|---|---|---|---|
| Speed atlas, taker edge at landing (11-14) | descriptive | `~/dublin-retest/MORNING.md` §4 | `atlas.py` | `research/dublin-retest` |
| dsim fill simulator and its validation (V2/V3) | tool | MORNING §5, round 3 | `dsim.py`, `test_dsim.py` | `research/dublin-retest` |
| Settlement source check (TWAP60) | done; extended 11-25 to 1h/4h | MORNING §2; 11-25 §3 | `settle_check.py`; `frozen/H1_plan.json` | `research/dublin-retest`; `research/dublin-1125` |
| R-D1 momentum taker | **registered, read ≥ 11-29** | prompt `overnight-2026-11-16.md` L17; 11-17 §2 | `momentum_shadow.py` | `research/shadow-trades` |
| R-D2 late breakout | **registered, read ≥ 11-29** | 11-17 §2 | `breakout_shadow.py` | `research/shadow-trades` |
| R-D3 Binance-move BTC 5m taker | **registered, read ≥ 11-29** | `frozen/R-D3.md`; MORNING L163 | `frozen/rd3-code/rd3_eval.py` | `research/dublin-retest` |
| fav-hedge A (B never frozen) | **registered, holdout 11-21..27** | MORNING "fav-hedge" | `fav_hedge.py` | `research/dublin-retest` |
| F1 follow-on taker | closed | 11-17 §3 F1 | `f1_events.py`, `f1_sim.py`, `f1_grid.py` | `research/overnight-1116` |
| F2 wallet front-running | closed | 11-17 F2 | `f2_leaders.py` | `research/overnight-1116` |
| F3 / F3i fast maker | closed | 11-17 F3 | `f3_maker.py`, `f3_grid.py` | `research/overnight-1116` |
| F4 15m after 5m print | closed | 11-17 F4 | `f4_cross.py` | `research/overnight-1116` |
| F5 imbalance | closed | 11-17 F5 | `f5_imbalance.py` | `research/overnight-1116` |
| F6 timed bursts | closed | 11-17 F6 | `f6_*.py` | `research/overnight-1116` |
| F7 / F7i alt-pull improve-maker = R-D4/R-D5 | **registered shadow (evaluator not installed), read ≥ 12-01** | 11-17 F7, §4 | `f7_altpull.py`, `rd45_report.py`, `deploy/rd45_*` | `research/overnight-1116` |
| F7i under live latency; F8 grid | closed | `2026-11-17-f7i-latency.md` §1-2 | `f7i_latency_table.py`, `f8_*.py` | `research/overnight-1116` |
| F9 `live` scenario and calm join | closed; `live` is the reference speed | `2026-11-18-live-scenario.md` | `f9_live.py`, `f9_*.py` | `research/overnight-1117` |
| 15m study (P1–P3, F15) | closed | `2026-11-18-15m.md` | `p1_15m.py`, `p2_*.py`, `p3_*.py`, `f7i15_report.py` | `research/overnight-1117` |
| 15m makers (census wallets) | closed | `2026-11-18-15m-makers.md` | `mk_wallets.py`, `act_rec.py fills` | `research/overnight-1117` |
| Late dump (maker buy 95–99¢) | closed | `2026-11-18-late-dump.md` | `ld_*.py` | `research/overnight-1117` |
| Alts `bid` maker | closed | `2026-11-18-alts.md` | `alt_bid.py`, `alt_report.py` | `research/overnight-1117` |
| 15m ladder (`0x239e726f`) | closed | `2026-11-19-15m-ladder.md` | `mk_level.py`, `lvl_report.py` | `research/overnight-1118` |
| MM ablation, one-sided and gated (BTC 15m) | closed (NO-GO) | poly-maker `research/reports/mm/ABLATION_REPORT.md` §9 | `research/mm_replay.py`, `strict_maker.py` | poly-maker `feat/mm-edge-recovery` |
| D1 profitable wallets out of sample | closed | 11-24 §D1 | `dublin1124/d1_rows.py`, `d1_report.py` | `research/overnight-1124-dublin` |
| D1b wallet `0xc387c2a4` | **forward watch 11-24..12-07, read ≥ 12-08** | 11-24 §7 | `dublin1124/d1b_*.py`, `mk_level_w.py`; `frozen/D1b_c387_watch.json` | `research/overnight-1124-dublin` |
| D2 level map, cell W, D2-b basis, final minute | closed | 11-24 §D2 | `dublin1124/d2_samples.py`, `d2_cells.py` | `research/overnight-1124-dublin` |
| D3 taker table at new fees; alt join-maker | closed | 11-24 §D3 | `dublin1124/d3_*.py` | `research/overnight-1124-dublin` |
| D4 lead atlas (Binance, Coinbase, Deribit, Chainlink) | descriptive; Coinbase/Deribit closed | 11-24 §D4 | `dublin1124/d4_atlas.py` | `research/overnight-1124-dublin` |
| D5 conditioning and queue | closed | 11-24 §D5 | `dublin1124/d5_blocks.py` | `research/overnight-1124-dublin` |
| Taker fee history and the 11-21 switch | done | 11-25 §1; `reports/dublin/fees_by_window.csv` | `dublin1125/d1_fees.py`, `d1_switch.py` | `research/dublin-1125` |
| H1: 1h/4h series fair value (Tokyo T4) | **frozen plan, awaiting data** | 11-25 §3; `frozen/H1_plan.json` | `hourly_rec.py` | `research/hourly-rec`, `research/dublin-1125` |
| fair_map (level fair value) | closed for takers | prompt L35 | `fair_map.py` | `research/shadow-trades` |
| OhioAI match / copy | closed | MORNING §5; 11-17 §2 | `ohioai_match.py`, `ohioai_copy.py` | `research/shadow-trades` |
| Octopus paper shadow | running (paper) | 11-17 §2 | `~/octopus/tentacle_ws.py` | octopus `feat/signal-sources` |
| Shadow arena (multi-strategy paper) | not running; no report on this box | — | poly-maker `apps/shadow_arena.py`, `poly_maker/arena/` | poly-maker `research/overnight-shadow-arena` |
| Lighter flips / Lighter shadow; favourite flips; same-token WF; BTC overlay; lead-lag (pre-Dublin) | no report on this box | — | `lighter_*.py`, `favourite_flips.py`, `same_token_wf2.py`, `btc_overlay.py`, `leadlag.py` | `research/shadow-trades` (origin also `research/lighter-*`) |
