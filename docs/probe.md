# lp-arb live probe (`lp_arb/probe.py`)

The smallest live test of the frozen T1 rule (Lighter × Polymarket, BTC 15m, pair cost after fees > 4¢ under $1).
It trades real money in tiny clips on two dedicated accounts (~$50 Lighter sub-account, ~$40 Polymarket) and stops
itself well before either could matter.

## How it works

`Probe` subclasses `paper.Paper` (the `lp-arb-paper` trader). Discovery, both websockets, the trigger (`evaluate`),
the +100/+300/+500 ms quote checks (`check`, `lquote_there`) and the cool-downs are the paper's own methods, not
copies (a test asserts `Probe.evaluate is Paper.evaluate`, and a replay of the 11-25 paper log fires the same 153
trigger ids). The probe adds one check and hooks one moment.

**Persistence (+250 ms, added 2026-11-26).** Each paper `trigger` record schedules `Probe.persist_check` 250 ms
later. It re-prices the pair from the current best quotes with the trigger's own after-fee formula
(`pair_edge`, the same expression as `paper.evaluate`). It logs a `persist` record for every trigger. If the edge is
now < 4c (`PERSIST_MIN_EDGE`), or either book is invalid, it also logs `skip` reason `not_persistent`, and the
trigger is not traded even if the +500 ms check says the quote is still displayed. A trigger that reaches +500 ms
with no verdict is skipped `persist_unchecked` (fail closed). The paper's checks and cool-downs are not changed, so
trigger ids are unchanged. Replay of 11-25: `lp_arb/probe_persist_replay.py` (see the Tokyo report).

**Stale books (same two moments; log-only by default).** At the trigger (in the `trigger` log hook) and again
at +250 ms the probe measures, per venue: `age_ms` = local now − exchange timestamp of the book's last update,
`lag_ms` = local receive time of that update − its exchange timestamp, and `since_recv_ms`. Polymarket `age_ms` or
`lag_ms` > 300 ms (`STALE_PM_MS`) → `stale_pm`; Lighter > 100 ms (`STALE_L_MS`) → `stale_lighter`. A book with no
exchange timestamp yet is stale. Every `persist` record carries `ages.trigger`, `ages.at_250`, the reason (`stale`,
null if fresh) and `stale_guard`, so the thresholds can be judged from the log.
`STALE_GUARD` at the top of `probe.py` decides what happens next:
- `"log"` (**default**): nothing more; a stale trigger still goes on to the 4¢ check and can trade.
- `"skip"`: the trigger is skipped with `skip` reason `stale_pm` / `stale_lighter`, checked before the edge. The skip
  carries `at` (`trigger` / `at_250`), both age snapshots and the limits. Note that
`age_ms` ≥ `lag_ms` by construction, and Polymarket sends only on change, so a quiet but correct book also counts as
stale under this rule (see the Tokyo report §10 for how much this drops on 11-25).

The hooked moment is the +500 ms check that the paper counts as its Lighter fill. At that moment:

1. **Gates**, in order: not persistent, or stale with `STALE_GUARD = "skip"` (already logged at +250 ms) → halted → reconcile failed → kill switch → daily loss cap → an execution already open → no
   +250 ms verdict → venue metadata missing → quote gone. A blocked trigger is logged as `skip` with the reason.
2. **Size**: q = max(5, Lighter `min_base_amount`, Polymarket `min_order_size`, ⌈$1 / hedge ask⌉), whole shares.
   Whole shares fit both size steps (Lighter 0.0001, Polymarket 0.01) and keep Polymarket BUY amounts at ≤ 2 decimals.
   Skip if q > Lighter displayed size, either leg > $10, open cost over its cap, or the tracked free balance is short.
3. **Lighter IOC** limit at the trigger's Lighter price, size q. Side a buys Up; side b sells Up (= buys Down). The
   order carries the **nil expiry** (0) that a limit IOC requires; Stat_Arb's `place_order` passes the 28-day default,
   which the signer rejects (`OrderExpiry is invalid`), so the probe sends IOCs itself (`ioc_order_args`). The probe polls
   the authoritative order status (`account_orders`) for ≤ 3 s. No terminal status → `halt`.
4. **Polymarket FAK BUY** of the complementary token (side a: Down, side b: Up), f = filled shares, worst price
   min(0.99, ask + 0.03). If short, it retries at the fresh ask + 0.03, up to 3 more times within 2 s.
5. **Unwind**: any remainder still unhedged goes back on Lighter as an IOC at the best price ∓ 5¢ (it fills at book
   prices). Then `unhedged` is logged with the loss. A remainder below Lighter's 5-share minimum cannot be sent; it is
   held to settlement and logged (`residual_held`).
6. **Settlement**: after the paper's own settle (both venues' outcomes), the probe logs `probe_settle` (payout, cost,
   P&L), `lighter_settled` (live: the position is gone from the account) and `redeem_due` for Polymarket winners.

An ambiguous order on either venue sets `halt`: no further trades until a restart and a human look at the accounts.
The probe never blind-unwinds a leg it is unsure about.

**Deterministic local rejections halt at the first occurrence (2026-11-26).** An order rejected before it reached a
venue would be rejected the same way on every later trigger. Three cases, each a `halt` with the venue's text in `error`;
later triggers are then logged as `skip` reason `halted`:

| case | trigger | what happens |
|---|---|---|
| Lighter order rejected with no tx hash, and the reason matches the signer's validation errors (`LOCAL_REJECT_RE`, e.g. "MarketIndex is not valid", "OrderExpiry is invalid") or Stat_Arb's local checks | entry IOC | `halt` reason `lighter_local_reject`; nothing is open |
| the same on an unwind IOC | unwind | `unhedged` logged (residual held), then `lighter_local_reject` |
| Polymarket order that fails while being built or signed (before sending) | hedge | no retries; the Lighter leg is unwound, then `halt` reason `pm_local_reject` |
| sign-only preflight of each window's market fails (live) | window start | `halt` reason `lighter_sign_reject` before any trigger can fire |

Venue rejections (an HTTP error with a code, e.g. not enough margin) are not deterministic and do not halt.

**Sign-only preflight.** At every window's start in live mode, the probe signs, with the real key, the exact IOC it
would send on that market (buy 5 sh @0.50, nil expiry, explicit nonce 0) and discards it; nothing is submitted. The
result is in `meta.sign_check` (`ok` or the error). `--mode check` runs the same step on the current market.

Dry mode (`--mode dry`, the default) runs the same code with simulated venues. The Lighter IOC meets the live book
150 ms after sending; the Polymarket FAK meets it 100 ms after sending. No credentials are loaded, SSM is never
called, and no SDK is imported. Dry logs go to `~/lp-arb/probe-dry` and carry `mode: dry`, so they can never enter
the live ledger.

## Limits (constants at the top of `lp_arb/probe.py`)

| limit | value |
|---|---|
| realised loss per UTC day | $10 → open nothing until 00:00 UTC (logging continues) |
| open cost, Polymarket | $30 (price + fee of unsettled shares) |
| open cost, Lighter | $40 (locked collateral + fee of unsettled shares) |
| either leg of one pair | $10 |
| executions at a time | 1 (no new pair while a leg is unhedged) |
| kill switch | `/home/ubuntu/lp-arb/probe/STOP` exists → open nothing; a hedge in progress is finished |
| hedge chase | ask + 3¢, cap 0.99, 1 + 3 attempts within 2 s |
| persistence | edge after fees ≥ 4¢ at +250 ms, else `not_persistent` |
| stale book | PM age or lag ≤ 300 ms, Lighter ≤ 100 ms, at the trigger and at +250 ms; **logged only** (`STALE_GUARD = "log"`); `"skip"` makes it a `stale_pm` / `stale_lighter` skip |

"Realised" counts these when they happen:

- each hedged pair's locked P&L, at hedge time;
- unwind losses;
- the residual difference at settlement.

The ledger (open legs, open cost, today's realised P&L) is rebuilt at start from the probe's own log records, so the
limits survive a restart.

## Lighter: what a sell of a binary locks (answer)

**A sell at price P locks (1 − P) per share. A buy locks P.** Lighter's binary options are fully collateralised in
USDC, with no leverage, funding or liquidation. The book is quoted in the Up price. `isAsk=1` (sell) is a Down order,
and the resting reserve is `(1 − P) × size` for sells and `P × size` for buys. Opposite-side shares offset the
requirement. IOC and market orders are funded at fill time. Sources:
docs.lighter.xyz/developers/binary-options/trading-and-fees.md and …/positions-and-settlement.md (read 2026-11-25).

The balance check uses those numbers: side a needs q·P + fee, side b needs q·(1 − P) + fee on Lighter.

Other Lighter facts the code depends on:

- **Fees:** taker fee = 1% × P(1 − P) per share (`fl()` in paper.py); makers pay nothing.
- **Order rules:** `reduceOnly` must be 0, and no trigger orders are allowed. An IOC limit is allowed. The price must
  be strictly between 0 and 1.00. Orders are rejected outside the window `start_timestamp`..`end_timestamp`.
- **Settlement:** automatic to the USDC balance. There is no claim step, and settlement trades carry no fee. If no
  outcome can be determined, both sides settle at 0.50.
- **Market config:** comes from `orderBooks?market_id=…` (`min_base_amount` 5, size decimals 4, price decimals 2).
  The default `orderBooks` listing has **no** binary markets, so the probe adds each window's market by id.
- **Positions:** binary positions are **not** in `/account` `positions`. They come back as `bo_positions`
  `[{market_id, sign (1 Up / −1 Down), size, avg_entry_price, entry_quote}]` (checked on a public account,
  2026-11-25). lighter-sdk 1.1.8 models the field (`DetailedAccount.bo_positions`); 1.1.2 left it in
  `additional_properties`. The probe reads both (`account_positions`): on 1.1.8 the old path alone would return nothing
  and the reconcile would be blind.
  Reconcile and `lighter_settled` key Lighter positions by market id. A position the log does not know is a
  blocking problem only in a BTC 15m market (symbol `BTC-UPDOWN-15M-*`, looked up by id; unknown → blocking); perps and
  other markets are warnings (see "Reconcile scope").
- **Rate limits:** a standard account gets 60 REST requests per rolling minute, `sendTx` included, limited per IP and
  per L1 address. Going over returns 429 with a 60 s firewall cooldown. One probe order costs 1 `sendTx` plus at most
  12 status polls (every 250 ms for ≤ 3 s). Public reads (`orderBooks`, `account`, `recentTrades`) count against the
  box's **IP** limit, which is shared with lp-arb-paper, xsmom, ladder and the recorders. A 429 during the fill poll
  ends as an ambiguous leg → `halt`, which is safe but stops the probe.
- **Fees as charged, and in the P&L (2026-11-26).** The market reports `taker_fee: "0.0000"` with
  `is_taker_fee_enabled: true`, so the market field is not the fee. Each trade carries an integer `taker_fee` /
  `maker_fee`: per Lighter's docs (docs.lighter.xyz, binary options › trading and fees) the **effective rate in fee
  ticks, 1e6 = 100%**, and the fee is `0.01 × bo_fee_rate × size × P × (1 − P)` (Up/Down: taker 1%, maker 0%;
  `bo_fee_rate` = the account's binary-options volume × LIT-staking tier, 1 by default; from `accountLimits`). So
  fee = rate × `usd_amount` / 1e6; the docs' example is 4900 at 0.51 = $0.2499 on 100 sh. Public trades agree: the rate
  does not scale with size and is ≈ 10000 × (1 − p) × bo_fee_rate (observed 7,650–10,000 × (1 − p) across takers).
  At `bo_fee_rate = 1` the charged fee equals the probe's model 0.01·p(1−p). How the probe uses it:
  - When a Lighter IOC fills (entry or unwind), P&L is booked at the model fee; no venue call is made on the hot path.
  - 5, 10 and 20 s later the probe looks up the order's own trades: the account's trades (authenticated), else the
    public `recentTrades`, matched on our client order id, as taker or maker.
  - It then logs `l_fee` with `fee_model_usd`, `fee_charged_usd`, the raw per-trade rates and
    `realised_usd = model − charged`. The ledger books that as realised, and settlement nets it out (the leg cost rises
    and the locked amount falls by the same), so the day's P&L carries the **charged** fee, counted once.
  - If the trades are not found, or the charged fee is more than 5× the model (+1¢), the model stays and the record
    says so (`applied: false`).
  - Polymarket's charged `fee_rate_bps` is still logged (`fees`) but not booked; its P&L stays on the model.

## Polymarket: redemption (answer)

There is **no redeem implementation on this box** to reuse:

- Octopus only tracks other wallets' REDEEM events.
- rd3_live says "claim in the UI".
- poly-maker's `stop_all_positions` says "redeem manually".

The probe therefore logs `redeem_due` in two ways:

- per settled window, for its own winning Polymarket shares;
- live only, a sweep of all redeemable positions on the account (Data API v2 `/v2/positions?user=<funder>`, see below) at
  every window.

**Positions source (2026-11-26).** Data API v1 `/positions` was retired on 2026-11-02 and returns HTTP 410
(`{"error":"Data API v1 has been retired"}`). Both the reconcile and check mode use
`GET https://data-api.polymarket.com/v2/positions` with:
- `user=<funder>&limit=500&filter_type=TOKENS&filter_amount=0`. v2 replaced `sizeThreshold` with
  `filter_type`/`filter_amount`, whose default floor is 0.1 sh; 0 keeps dust. If the API ever rejects
  `filter_amount=0` (a 400 naming the filter), the probe retries without the filter and says so in the warnings.
- Cursor pagination: `pagination.has_more` / `next_cursor`, each cursor request keeping the `user` anchor, at most 20
  pages. A repeated or missing cursor raises an error.
- `status` defaults to OPEN, which includes settled but unredeemed winners.

Rows are snake_case (`token_id`, `condition_id`, `current_size`, `slug`, `outcome`, `redeemable`, `current_value`) and
are normalised to `{token, condition, size, slug, title, outcome, redeemable, value, status}`.

**Claim them in the Polymarket UI.** Unclaimed winners are not cash. With ~$40 on Polymarket and ~$4 per 5-share
hedge, PM cash runs short after about 10 pairs unless claimed; the probe then skips with `pm_balance`.

## Reused code and versions

| piece | from | how |
|---|---|---|
| Lighter signing, orders, status | `Stat_Arb/statarb/exchange/lighter_client.py` @b7ef8d3 (the client Stat_Arb runs live) | subclassed (`add_market`, `snapshot`, `active_orders`, `order_raw`), hash-pinned |
| Polymarket client setup, FAK parsing | `rd3-repo/research/rd3_live/rd3_live.py` @5c81bb8 (R-D3 live taker run) | `make_client`, `parse_post`, `classify_api_error`, `resolve_fill`, `fill_within_order`, `cash`; hash-pinned |
| trigger, checks, books, sockets, log files | `lp_arb/paper.py` | subclassed |

`lighter-sdk` **1.1.8** @56b3537 (2026-10-19), in the probe venv only; Stat_Arb, xsmom and ladder stay on 1.1.2 @fd4ee25.
1.1.2's signer (lighter-go 7b5bba93, 2026-08-19) has an `int16` market index and accepts only perps 0–254 and spot
2048–4094, so every binary-option order (ids ≈ 12,800 and rising ~480/day) is rejected locally: "MarketIndex is not
valid". That is what the first live run hit (3 orders, no tx, 1–25 ms). lighter-go #93 (2026-10-12) widened the index to
`int64` (≤ 2⁴⁸ − 1); lighter-python v1.1.7 shipped that signer and v1.1.8 (signer eb70ce6a) added the binary-options
models (`bo_positions`, order fields). Lighter's docs: "use lighter-sdk 1.1.8 or newer". Live and check mode refuse any
other lighter-sdk version or commit (`verify_pins`).

Stat_Arb's `place_order` is reused for everything except the IOC, which the probe builds itself with the nil expiry.
Stat_Arb's own Lighter IOC path (`execution.py`, `OrderType.IOC`) passes the 28-day default expiry and would be
rejected by any signer version with `OrderExpiry is invalid`; that is outside this repo and was not changed.

`py_clob_client_v2` **1.2.2**, the newest release (2026-11-12). rd3 used 1.2.0 and Octopus live uses 1.1.0. The only
difference between 1.2.0 and 1.2.2 is the `exchange_v3` contract address: 1.2.0 has `0x9fE6…`, and 1.2.2 has
`0xe3333700…`, the V3 exchange Octopus sees on-chain. The CLOB's `/version` answers 2 today, so all three versions
sign against the v2 exchange now. 1.2.0 would sign v3 orders against the wrong contract once the CLOB moves to v3.

In live mode the probe refuses to start if any reused file's sha256 differs from the pinned one, or if the CLOB client
is not 1.2.2. It also logs the CLOB's order version at start (`probe_start.pm_clob_version`).

## Reconcile scope (2026-11-26)

At a live start, and in `--mode check`, both accounts are compared with the probe's log:

| found on the venue, not in the probe's log | result |
|---|---|
| position in a **BTC 15m up/down** market (PM slug `btc-updown-15m-*`, Lighter symbol `BTC-UPDOWN-15M-*`) | **problem**: `reconcile_fail`, the probe will not trade |
| position whose market cannot be identified (no slug / symbol lookup failed) | **problem** (fail closed) |
| position in any other market (other assets, 5m markets, perps, non-crypto) | **warning** only, logged in `reconcile.warnings` |
| a probe position whose size differs from the log | problem |
| any open order on either venue | problem (the probe only sends IOC / FAK) |

## Polymarket API credentials

`rd3_live.make_client` (pinned) calls the library's `create_or_derive_api_key`, which tries **create** first. Once a
key exists, that logs `Could not create api key` (400) on every start. The probe wraps that one call (`derive_first`)
for the duration of `make_client`: it **derives** the existing key first and creates one only if deriving fails.
`--mode check` reports which happened (`pm_client.api_creds`: `derived` / `created`).

## Shutdown

`main` closes both venues on every exit path, including errors and a failed start:
- Lighter: both aiohttp sessions (Stat_Arb's ApiClient and the SignerClient's own), then its IO loop.
- Polymarket: the httpx client.

This removes the `Unclosed client session` warnings.

## Secrets

At start, live mode reads 6 SSM parameters (ap-northeast-1, instance role `donchian-ec2-role`) with one
`get_parameters` call:

- `/lparb/probe/lighter/{ACCOUNT_INDEX, API_KEY_INDEX, API_KEY_PRIVATE_KEY}`
- `/lparb/probe/polymarket/{PRIVATE_KEY, FUNDER, SIGNATURE_TYPE}`

Values stay in memory, and the dict is cleared once both clients are built. Every value of ≥ 4 characters is
registered with the redactor, along with the derived Polymarket API key, secret and passphrase. The redactor scrubs
these places:

- every log record, both string and numeric fields;
- tracebacks (`sys.excepthook`);
- everything on stdout/stderr, and therefore the journal.

Keys named `signature`, `tx_info` and similar are dropped, and any 0x-hex blob of signature length or longer is
replaced with `[SIG]`.

`API_KEY_INDEX` and `SIGNATURE_TYPE` are 1–3 digit selectors. They are not redacted, because redacting "2" would
corrupt every number; they are never logged on purpose.

## Venv (one-off, already built on Tokyo)

```
python3 -m venv /home/ubuntu/lp-arb/probe-venv
/home/ubuntu/lp-arb/probe-venv/bin/pip install -r /home/ubuntu/lp-arb/probe-code/deploy/probe-requirements.txt
cd /home/ubuntu/lp-arb/probe-code && /home/ubuntu/lp-arb/probe-venv/bin/python -m pytest lp_arb/tests -q -p no:cacheprovider --rootdir lp_arb/tests -c /dev/null
```

The unit runs this venv's Python, not `/usr/bin/python3`, because the system Python has neither SDK.

## Operator runbook

Before the first start, check these:

- `lp-arb-paper` is running; the probe does not need it, but the comparison does.
- `/home/ubuntu/lp-arb/probe/` holds no stray `.jsonl` (dry logs live in `probe-dry`).
- Both accounts are funded and hold **no** positions or open orders. The probe refuses to trade otherwise
  (`reconcile_fail`).

0. Account check (read-only; loads SSM exactly as live does, places and cancels nothing, writes no log):

   ```
   cd /home/ubuntu/lp-arb/probe-code && /home/ubuntu/lp-arb/probe-venv/bin/python lp_arb/probe.py --mode check; echo exit=$?
   ```

   It prints one JSON document; `exit=0` means `ok: true`, `exit=1` lists `problems`. It refuses before reading any
   secret if a pinned reused file changed (as live does). Read:

   - `lighter.account`: `account_type`, `account_trading_mode` (0 classic, 1 unified), `status` (raw codes),
     `user_tier_name` (Standard / Premium), fee ticks; `lighter.balance`, `collateral`, `positions` (binary positions by
     market id from `bo_positions`), `open_orders`. Positions and open orders must be empty.
   - `lighter_btc15m`: the current window's market, `status` (`active`), `market_type` (`binary_options`), and
     `checks` (market active, the signer client registered the market, account read, authenticated read, balance ≥ one
     $10 leg, `signs_ioc`). `sign_check` signs, with the real key, the exact IOC the probe would send on the current
     market and discards it (`submitted: false`); `ok: false` with the signer's error means live orders would be rejected. `verdict: yes_by_api` means every API-side check passed. **Lighter has no per-account binary permission
     endpoint: only the first live order proves the account may trade it.**
   - `pm_client`: installed `py_clob_client_v2` (must be 1.2.2) and the CLOB's `/version`.
   - `pm`: cash `balance`; `allowance` to the exchange contract a BUY is signed for (v2 non-neg-risk → `exchange_v2`,
     v3 → `exchange_v3`), `ok` if ≥ $30 or unlimited; `allowances_raw` for every spender; `closed_only` (must be
     false); `open_orders`, `positions` (must be empty or redeemed).
   - `reconcile`: what the live start-up reconcile would say against `~/lp-arb/probe` (must be `ok: true`); positions
     in other markets appear under `warnings` and do not block (see "Reconcile scope").
   - `pm_client.api_creds` should read `derived` (an existing key was reused).

   The key, funder and account index are registered with the redactor before anything is printed; they appear as
   `[REDACTED]` if a venue echoes them.

1. Install the unit:

   ```
   sudo cp /home/ubuntu/lp-arb/probe-code/deploy/lp-arb-probe.service /etc/systemd/system/ && sudo systemctl daemon-reload
   ```

2. Start it:

   ```
   sudo systemctl start lp-arb-probe
   ```

   Do **not** `enable` it until the first day has been reviewed.

3. Watch the first hour:

   ```
   journalctl -u lp-arb-probe -f                      # should be quiet; any traceback = stop
   tail -f ~/lp-arb/probe/$(date -u +%F).jsonl | grep -vE '"kind":"(health|check)"'
   ```

   - `probe_start` should show `pm_clob_version` 2, and `reconcile` should show `"ok":true`. `reconcile_fail` means
     it will not trade: read `problems`.
   - The first `balance` (within a minute) should show `lighter` ≈ $50, `pm` ≈ $40, open costs 0, `lighter_positions`
     `{}`.
   - After the first `l_fill`, the next `balance.lighter_positions` must show that window's symbol (±q). **If it stays
     `{}`, binary positions are not in `/account` positions, and the reconcile is blind: stop and report.**
   - Per trigger: `trigger` → `persist` (+250 ms) → `check` ×3 → `skip` (reason) or `l_order` → `l_fill` → `pm_order`/`pm_fill` → `pair`.
     A `pm_order` with `status: rejected` that reads "version" or "invalid signature" means the CLOB client is out of
     date: stop.
   - With the default `STALE_GUARD = "log"`, staleness is not a skip. Expect most `persist` records to show
     `stale: "stale_pm"` (on 11-25, 24 of the 53 edge survivors were stale at the trigger alone); those triggers still
     trade. If **every** trigger is stale, look at `persist.ages.*.pm.lag_ms`: a lag that never drops below 300 ms is
     the box's feed or clock (check the `timedatectl`/chrony offset), not the market.
   - Each window's `meta` should show `sign_check: "ok"`. Anything else is followed by `halt` `lighter_sign_reject`.
   - `unhedged`, `halt` or `error` records: look at each one.
   - After each window (~6–8 min after its end): `paper_settle` (simulated), `probe_settle`, `lighter_settled`
     (`position_gone: true`), and `redeem_due` for Polymarket winners. Claim those in the UI.

4. Stop it:

   ```
   touch ~/lp-arb/probe/STOP                         # open nothing new; a hedge in progress finishes
   sudo systemctl stop lp-arb-probe                  # SIGTERM: waits <= 15 s for a hedge in progress
   ```

   Remove `~/lp-arb/probe/STOP` before the next start, or the probe will run but skip every trigger.

After a `halt` or `reconcile_fail`, compare both accounts with the log by hand. Close or claim anything left over,
then restart. The ledger is rebuilt from the log, so a position the probe opened and logged is not "foreign".

Summary of a run against the paper trader:

```
python3 lp_arb/probe_summary.py --probe ~/lp-arb/probe --paper ~/lp-arb/paper
python3 lp_arb/probe_vs_paper.py --day $(date -u +%F)        # per day, live records only (--mode dry for probe-dry)
```

`probe_vs_paper.py` joins the probe's and the paper's day logs by trigger id (then by window, side and ≤ 250 ms, since
the two processes see the books on separate sockets), within the probe's uptime, and reports: triggers in both /
probe-only / paper-only; probe skips by reason; Lighter IOC fill rate live vs the paper's +500 ms "displayed" on the
same triggers; per pair live vs paper c/share, Lighter fill slippage vs the trigger price, Polymarket hedge slippage vs
the trigger price and vs the ask when the FAK was sent, fees modelled vs charged; every `unhedged`/`unwind` with its
cost; halts. The Lighter fee charged comes from the probe's `l_fee` records. The probe log's simulated paper records
(`paper_hedge`, `paper_settle`, `sim: true`; `hedge`/`settle` in logs before 2026-11-26) are excluded and counted
(`sim_records_excluded`); the paper side comes only from the paper trader's own log.

## Record kinds

The paper trader's records keep their kinds and ids, except its two simulated P&L records, which are renamed in the
probe log so they cannot be read as trades (logs written before 2026-11-26 still have `hedge` / `settle`):

| kind | meaning |
|---|---|
| `start`, `window`, `trigger`, `check`, `health`, `error`, `stop` | as in the paper trader |
| `paper_hedge` (`sim: true`) | the paper model's simulated hedge. **Not a trade.** |
| `paper_settle` (`sim: true`) | the paper model's settle. **Not a trade.** It also tells the probe both venues' outcomes. |

The probe adds these:

| kind | content |
|---|---|
| `probe_start` / `probe_stop` | limits, ledger state, CLOB version |
| `meta` | per window: Lighter min/decimals, PM min/tick, condition id |
| `persist` | every trigger at +250 ms: re-priced `edge` after fees, `ok` (≥ 4c), `stale` (reason or null), `stale_guard` (`log` / `skip`), `ages` {trigger, at_250} × {pm, lighter} × {age_ms, lag_ms, since_recv_ms}, both books |
| `skip` | trigger id + reason + sizing (`not_persistent` carries `edge`, `min_edge`; `stale_pm`/`stale_lighter`, only with `STALE_GUARD = "skip"`, carry `at`, `ages`, `limits_ms`) |
| `l_order` | send/ack/fill times (local), client order id, tx hash, status, filled, avg px, exchange timestamps; `purpose: unwind` for unwinds |
| `l_fill` | qty, px, model fee |
| `l_fee` | live, 5–35 s after a Lighter fill (`purpose` entry / unwind): `fee_model_usd`, `fee_charged_usd`, per-trade raw rates, `applied`, `realised_usd` = model − charged (booked) |
| `pm_order` | per hedge attempt: ask, limit, size, status, order id, times |
| `pm_fill` | shares, cost, px, fee |
| `fees` | live, ~5 s later: Polymarket trades' `fee_rate_bps` as charged (logged, not booked) |
| `pair` | hedged qty, both prices, fees, attempts, `pnl_usd` (= `realised_usd`), over-hedge |
| `unwind` / `unhedged` | unwind fill and loss; residual held |
| `balance` | every 60 s: free balances, collateral, Lighter positions, open costs, realised today, flags, skip counts |
| `probe_settle`, `lighter_settled`, `redeem_due`, `settle_unknown` | settlement records (see above) |
| `reconcile`, `reconcile_fail`, `halt`, `loss_cap` | start-up check and stop conditions; `halt.reason` includes `lighter_local_reject`, `pm_local_reject`, `lighter_sign_reject` (with `error`) |
