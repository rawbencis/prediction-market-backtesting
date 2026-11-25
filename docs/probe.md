# lp-arb live probe (`lp_arb/probe.py`)

The smallest live test of the frozen T1 rule (Lighter × Polymarket, BTC 15m, pair cost after fees > 4¢ under $1).
It trades real money in tiny clips on two dedicated accounts (~$50 Lighter sub-account, ~$40 Polymarket) and stops
itself well before either could matter.

## How it works

`Probe` subclasses `paper.Paper` (the `lp-arb-paper` trader). Discovery, both websockets, the trigger (`evaluate`),
the +100/+300/+500 ms quote checks (`check`, `lquote_there`) and the cool-downs are the paper's own methods, not
copies (a test asserts `Probe.evaluate is Paper.evaluate`, and a replay of the 11-25 paper log fires the same 153
trigger ids). The probe hooks one moment: the +500 ms check that the paper counts as its Lighter fill.

At that moment:

1. **Gates**, in order: halted → reconcile failed → kill switch → daily loss cap → an execution already open → venue
   metadata missing → quote gone. A blocked trigger is logged as `skip` with the reason.
2. **Size**: q = max(5, Lighter `min_base_amount`, Polymarket `min_order_size`, ⌈$1 / hedge ask⌉), whole shares.
   Whole shares fit both size steps (Lighter 0.0001, Polymarket 0.01) and keep Polymarket BUY amounts at ≤ 2 decimals.
   Skip if q > Lighter displayed size, either leg > $10, open cost over its cap, or the tracked free balance is short.
3. **Lighter IOC** limit at the trigger's Lighter price, size q. Side a buys Up; side b sells Up (= buys Down). The
   probe polls the authoritative order status (`account_orders`) for ≤ 3 s. No terminal status → `halt`.
4. **Polymarket FAK BUY** of the complementary token (side a: Down, side b: Up), f = filled shares, worst price
   min(0.99, ask + 0.03). If short, it retries at the fresh ask + 0.03, up to 3 more times within 2 s.
5. **Unwind**: any remainder still unhedged goes back on Lighter as an IOC at the best price ∓ 5¢ (it fills at book
   prices). Then `unhedged` is logged with the loss. A remainder below Lighter's 5-share minimum cannot be sent; it is
   held to settlement and logged (`residual_held`).
6. **Settlement**: after the paper's own settle (both venues' outcomes), the probe logs `probe_settle` (payout, cost,
   P&L), `lighter_settled` (live: the position is gone from the account) and `redeem_due` for Polymarket winners.

An ambiguous order on either venue sets `halt`: no further trades until a restart and a human look at the accounts.
The probe never blind-unwinds a leg it is unsure about.

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

## Polymarket: redemption (answer)

There is **no redeem implementation on this box** to reuse:

- Octopus only tracks other wallets' REDEEM events.
- rd3_live says "claim in the UI".
- poly-maker's `stop_all_positions` says "redeem manually".

The probe therefore logs `redeem_due` in two ways:

- per settled window, for its own winning Polymarket shares;
- live only, a sweep of all redeemable positions on the account (data-api `positions?user=<funder>`) at every window.

**Claim them in the Polymarket UI.** Unclaimed winners are not cash. With ~$40 on Polymarket and ~$4 per 5-share
hedge, PM cash runs short after about 10 pairs unless claimed; the probe then skips with `pm_balance`.

## Reused code and versions

| piece | from | how |
|---|---|---|
| Lighter signing, orders, status | `Stat_Arb/statarb/exchange/lighter_client.py` @b7ef8d3 (the client Stat_Arb runs live) | subclassed (`add_market`, `snapshot`, `active_orders`, `order_raw`), hash-pinned |
| Polymarket client setup, FAK parsing | `rd3-repo/research/rd3_live/rd3_live.py` @5c81bb8 (R-D3 live taker run) | `make_client`, `parse_post`, `classify_api_error`, `resolve_fill`, `fill_within_order`, `cash`; hash-pinned |
| trigger, checks, books, sockets, log files | `lp_arb/paper.py` | subclassed |

`lighter-sdk` 1.1.2 @fd4ee25 is the same pin as Stat_Arb, xsmom and ladder.

`py_clob_client_v2` **1.2.2**, the newest release (2026-11-12). rd3 used 1.2.0 and Octopus live uses 1.1.0. The only
difference between 1.2.0 and 1.2.2 is the `exchange_v3` contract address: 1.2.0 has `0x9fE6…`, and 1.2.2 has
`0xe3333700…`, the V3 exchange Octopus sees on-chain. The CLOB's `/version` answers 2 today, so all three versions
sign against the v2 exchange now. 1.2.0 would sign v3 orders against the wrong contract once the CLOB moves to v3.

In live mode the probe refuses to start if any reused file's sha256 differs from the pinned one, or if the CLOB client
is not 1.2.2. It also logs the CLOB's order version at start (`probe_start.pm_clob_version`).

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
/home/ubuntu/lp-arb/probe-venv/bin/pip install -r /home/ubuntu/lp-arb/code/deploy/probe-requirements.txt
cd /home/ubuntu/lp-arb/code && /home/ubuntu/lp-arb/probe-venv/bin/python -m pytest lp_arb/tests -q -p no:cacheprovider --rootdir lp_arb/tests -c /dev/null
```

The unit runs this venv's Python, not `/usr/bin/python3`, because the system Python has neither SDK.

## Operator runbook

Before the first start, check these:

- `lp-arb-paper` is running; the probe does not need it, but the comparison does.
- `/home/ubuntu/lp-arb/probe/` holds no stray `.jsonl` (dry logs live in `probe-dry`).
- Both accounts are funded and hold **no** positions or open orders. The probe refuses to trade otherwise
  (`reconcile_fail`).

1. Install the unit:

   ```
   sudo cp /home/ubuntu/lp-arb/code/deploy/lp-arb-probe.service /etc/systemd/system/ && sudo systemctl daemon-reload
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
   - Per trigger: `trigger` → `check` ×3 → `skip` (reason) or `l_order` → `l_fill` → `pm_order`/`pm_fill` → `pair`.
     A `pm_order` with `status: rejected` that reads "version" or "invalid signature" means the CLOB client is out of
     date: stop.
   - `unhedged`, `halt` or `error` records: look at each one.
   - After each window (~6–8 min after its end): `settle` (paper), `probe_settle`, `lighter_settled`
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
```

## Record kinds

The paper trader's records keep their kinds and ids:

| kind | meaning |
|---|---|
| `start`, `window`, `trigger`, `check`, `health`, `error`, `stop` | as in the paper trader |
| `hedge` | the paper model's hedge, for comparison |
| `settle` | the paper's settle |

The probe adds these:

| kind | content |
|---|---|
| `probe_start` / `probe_stop` | limits, ledger state, CLOB version |
| `meta` | per window: Lighter min/decimals, PM min/tick, condition id |
| `skip` | trigger id + reason + sizing |
| `l_order` | send/ack/fill times (local), client order id, tx hash, status, filled, avg px, exchange timestamps; `purpose: unwind` for unwinds |
| `l_fill` | qty, px, model fee |
| `pm_order` | per hedge attempt: ask, limit, size, status, order id, times |
| `pm_fill` | shares, cost, px, fee |
| `fees` | live, ~5 s later: Polymarket trades' `fee_rate_bps` as charged |
| `pair` | hedged qty, both prices, fees, attempts, `pnl_usd` (= `realised_usd`), over-hedge |
| `unwind` / `unhedged` | unwind fill and loss; residual held |
| `balance` | every 60 s: free balances, collateral, Lighter positions, open costs, realised today, flags, skip counts |
| `probe_settle`, `lighter_settled`, `redeem_due`, `settle_unknown` | settlement records (see above) |
| `reconcile`, `reconcile_fail`, `halt`, `loss_cap` | start-up check and stop conditions |
