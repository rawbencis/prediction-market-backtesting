# Overnight task, Dublin box, night of 2026-11-16 → 17 (UTC)

You are Claude Code on the Dublin recorder box (eu-west-1, c7i.large, 2 vCPU, 3.8 GB RAM). Work unattended until **06:00 UTC**, then make sure the report below is written. The operator reads it in the morning; it must stand on its own.

## Hard rules (read first, never break)
1. **No trading.** Place, cancel or modify no orders, and don't run `order_probe.py` or any script that signs anything. Don't read or print credentials or `.env` contents.
2. **Don't disturb live services.** Never stop, restart, edit or reinstall: `updown-rec`, `updown-act`, `feeds_rec` (or whatever records feeds), `octopus-shadow` + its sweeper timer, `shadow-latency`, `momentum-eval`, `ohioai-match`, the breakout cron, or anything else already scheduled. Read their outputs only.
3. **Memory.** The recorders share this box. Run every analysis heavier than a few MB under `systemd-run --user --scope -q -p MemoryMax=1200M <cmd>`. Stream recorder files hour by hour; never load whole days of `pm-*.bin` or `bn-*.bin` at once (a 2.3 GB job was OOM-killed on 2026-11-16). Check `free -m` before big jobs. Keep `~/shadow-rec` and disk free space healthy (don't write large intermediates there).
4. **Pre-registration.** R-D3 (Binance-futures-triggered BTC 5m taker) is registered "not read before day 14" (~2026-11-29). Don't compute or print R-D3's shadow result, and don't run any Binance-move-triggered analysis on 2026-11-15+ data. (Tonight's research uses no Binance anyway.) R-D1 / R-D2 reports that already run daily may be reported as they are.
5. **Git:** commit locally on a new branch `research/overnight-1116` (in `~/dublin-retest/repo` or `~/lighter-flips`, whichever holds the code you extend). You can't push (no credentials); say in the report which repo/branch/commit holds tonight's code.
6. **Honesty over optimism.** Report every idea tested, including failures, with numbers. Count how many configurations you looked at (multiple comparisons). A positive dev result is a candidate, not a strategy.

## Part A: status of every Polymarket up/down shadow / service on this box
Discover, don't assume: `systemctl list-units --type=service,timer --all | grep -iE 'updown|shadow|momentum|breakout|ohioai|octopus|sig|act|latency|feeds|dsim|probe|fav|repick'`, `crontab -l`, `systemctl --user list-timers`, `ls -t ~ ~/dublin-retest ~/reports 2>/dev/null`, recent logs (`journalctl -u <unit> --since -24h`).

For each one: what it is, active/failed and since when, last successful output, data volume (rows/hour), errors or gaps in the last 24 h, and its latest result where one exists and reading it is allowed:
- **R-D1** (momentum, Dublin, BTC 5m, spot M 2 bp / 5 s, count from 2026-11-15): days counted, fills, net ¢/share, day-bootstrap 90% CI, vs pass (≥ 14 days, ≥ 500 fills, ≥ +1.0¢, CI lower > 0) / kill (CI upper < +1.0 after 14 days). Source: `~/mom-res/report.txt` (`tokyo` scenario on this box = a Dublin bot).
- **R-D2** (late breakout, Dublin): same format, from the breakout eval output.
- **R-D3:** operational status only (it runs live on a *second* box you can't reach; say so). No result.
- **Fav-hedge candidate A / repick B** (holdout from 2026-11-21): confirm the `repick_B.sh` job is still alive (it doesn't survive a reboot).
- **Octopus paper shadow** (`octopus-shadow.service`, stream-only signal, state `~/octopus-shadow`): connected?, wallets watched, paper fills/PnL so far, `signal_latency` rows.
- **Recorders:** `updown-rec` (books, prints, Binance) and `updown-act` (taker census: one row per fill participant; the taker row has fee > 0): rows per hour, gaps, receive lag (p50/p90), disk use and days kept.
- **OhioAI match** (`~/ohioai/ohioai_ms.json`): latest day's summary.
- Anything else you find. Tokyo services aren't reachable from here; list them as "not checked".

## Part B: research — low-latency setups from Polymarket data alone
**Premise from the operator:** assume the books are efficient against outside prices; don't use Binance or any external price. Our advantage is speed *inside* Polymarket: from Dublin we see a book change or trade print ~9 ms after it happens (p50; p90 23 ms), our order reaches the book ~22 ms after we send it (post ack RTT ~37 ms), a taker order is held ~50 ms before matching (unverified), cancels aren't held (~20–30 ms). Most other bots are slower: 100–300 ms from Tokyo/US, and copy-traders that react to the public activity stream are ≥ 250 ms to seconds late. **Goal: front-run the predictable flow of slower bots and scalp a few cents per trade, as maker or taker, net of fees.** Be creative, try many setups, and don't give up after the first failures.

### What's already known (don't redo; extend if you have a new angle)
- Fees: taker 0.07·p·(1−p) per share; maker 0, plus a share of a pool of 20% of taker fees (not per fill; report separately, never needed to pass). Tick 0.01 (0.001 near extremes), min size 5. Settlement: Chainlink 60 s TWAP at window start/end.
- Closed: 5m→15m lead-lag (5m reprices first only 55%, median gap 11 ms); alt-book-led BTC 5m taker (−1 to −2¢ everywhere); paired-token (UP+DOWN) gap taker (episodes median < 1 ms); level-based fair value from Binance (no edge for a taker, 2026-11-16 `fair_map.py`); copying a profitable wallet with a 0.3–1 s detection delay (no edge: its own take consumes the ask; later fills are adversely selected).
- Every maker result so far passed only under a queue-front upper bound; realistic queue models lost. A big maker in the census (`0x21f4c1…`, $322k) loses −2.3¢/$; a small buy-only maker (`0x13c621…`) earns +13.6¢/$ on 9-share bids across 368 markets.
- Census (`act_rec.py`, `~/shadow-rec/DAY/act-HH.bin`, from 2026-11-14 21:41 UTC): all taker flow in BTC 5m loses −1.25¢/$; takers buying < 20¢ lose −23.6¢/$ (favourite–longshot bias); makers earn +0.6 to +0.9¢/$ at ≥ 80¢. Tools: `act_rec.py top | pnl | profile | fills`.
- Speed atlas (2026-11-14): BTC books reprice 230–300 ms (median) after a Binance move, alts 70–100 ms.
- Simulator: `dsim.py` in `~/dublin-retest/repo` (taker matches at arrival + hold against the recorded book, others first on ties; maker queue models `pess` / `prop` / `front`). Use it, or match its conventions exactly.

### Candidate families (start here, add your own)
1. **Follow-on flow after a sweep.** After a large taker print (size, levels swept, which token), do other takers keep hitting the same side over the next 50 ms – 5 s (slower bots, copy-traders reacting to the print or the activity stream)? Measure the ask/mid path and the count of same-side taker prints after the event, by event size. Setups: (a) take immediately behind the sweep, exit by posting at a better price into the follow-on flow (maker exit) or by taker exit; (b) post ahead of the predicted follow-on flow as maker.
2. **Wallet-specific front-running via the census.** For wallets that take repeatedly, measure whether *other* wallets' taker prints on the same token follow theirs within 0.2–5 s more than chance (followers / copy-bots), and the price impact of the followers. If a leader is identifiable from the trade print alone (size pattern, timing, price levels), we see its fill ~30 ms after the match versus followers at ≥ 250 ms. Quantify the edge with real latencies.
3. **Fast maker at new levels, book-only pulls.** Post at a newly formed best price ~14 ms after it appears (queue front), pull on book-only triggers: depletion of the level, a sweep on the opposite token, a print through, sibling-book moves. Use only `pess` / `prop` queue models to pass; report `front` for reference. Inventory caps, both tokens or one.
4. **Stale quotes after events.** After a sweep or a big print, slow makers leave quotes that are now off-market on the complementary token or deeper levels; take them before they're pulled. Measure how long such quotes survive and whether ≥ 5 shares are takeable at our landing time.
5. **Order-flow imbalance / microprice.** Top-of-book size imbalance and the last few prints as a short-horizon predictor of the next tick; take or join the queue ahead of the predicted move. Check the horizon against our latency.
6. **Timed bursts.** Use the census and prints to find predictable timing: bursts at the window open (whales at ~295 s left), at fixed seconds, around round prices, before the close. Pre-position as maker before a burst and pull after.
7. Your own ideas. Anything inside Polymarket's own data that slower bots act on predictably.

### Method (non-negotiable)
- **Data:** BTC 5m first (deepest, slowest books), then BTC 15m and the alts if time allows. Days available on this box: `ls ~/shadow-rec`. **Dev = 2026-11-14 and 11-15; holdout = 2026-11-16** (and 11-17 hours if complete). Choose each family's grid on dev, freeze the best config (write it down with a timestamp) before touching holdout, then run it once on holdout.
- **Latency:** this box's measured delays (see a book/print ~9 ms; order on book ~22 ms; taker hold 50 ms; cancel ~20–30 ms). Also run a `tokyo` scenario (+~110 ms each way) for every candidate: an edge that doesn't shrink sharply under `tokyo` isn't a speed edge.
- **Costs:** taker fee on every taker leg; round trips pay two legs. Fill a taker only if the level shows ≥ the size at our arrival; others first on ties. Makers fill only when prints trade through our queue position (pess/prop).
- **Outcome:** markout at +100 ms / 1 s / 5 s / 30 s *and* settlement where held. Report net ¢/share, ¢/$, fills/day, $/day at 5 shares and at realistic depth, 90% CI bootstrapped by market **and** by day (report the wider).
- **Gates:** a candidate survives only if holdout net > 0 with CI lower > 0 under `pess` or `prop` (makers) or with the full taker model, the `tokyo` scenario is clearly worse, and it trades ≥ 20 times/day. Otherwise it's closed, with its numbers.

## Deliverable: `~/reports/2026-11-17-overnight.md` (also print its path at the end)
1. **Executive summary** (≤ 15 lines): service health in one line each; the best research result and whether it survived holdout; what you recommend for today.
2. **Part A table:** service | state | last output | key numbers | issues.
3. **Part B:** for each family tested — hypothesis, exact event/rule definitions, configs tried (count), dev result, frozen pick, holdout result, `tokyo` comparison, verdict (CANDIDATE / CLOSED / NEEDS DATA) and why.
4. **Ranked candidates** with frozen rules ready for a 14-day shadow (exact parameters, pass/kill criteria in the same style as R-D1).
5. **Infrastructure notes:** code locations (repo, branch, commit), new scripts, anything the recorders should start capturing (e.g. deeper levels, order-book deltas) to test a promising idea properly.
6. **Problems hit** (OOM, missing data, gaps) and what you did.

Pace yourself: get Part A done first (≤ 1 h), then spend the night on Part B. If a family dies quickly, move on; if one looks promising, stress-test it hard (holdout, `tokyo`, CIs, depth) rather than polishing the write-up. Write the report incrementally so a partial report exists even if the session ends early.
