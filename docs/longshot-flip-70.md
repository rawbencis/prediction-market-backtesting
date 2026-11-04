# 70-Cent Longshot Research

This is an observational crypto UP/DOWN research pipeline, not evidence of
profitability. It shares one settlement-free alternating-touch state machine
between dataset extraction and NautilusTrader strategies. Execution uses genuine
`OrderBookDeltas`, `L2_MBP`, and the existing framework's data-loading and execution
APIs, not a separate price-path simulator. Each of the six variants has its own
account and capital/position limits. Within each variant, all markets share that
account; variants are never combined into one funded portfolio.
Same-instrument `TradeTick` records integrated into a book replay use native
Nautilus book/trade matching. Touch signals consume only L2 deltas; standalone
trade-tick replay is unsupported.

## Commands

Run from the repository root after the installation in [Setup](setup.md).
The bundled synthetic configuration requires no network, credentials, or server
files. Keep research outputs outside the repository or under ignored `output/`.

```bash
uv run python scripts/longshot_flip_70.py audit --config docs/examples/longshot-synthetic.json --output output/longshot-primary
uv run python scripts/longshot_flip_70.py extract --config docs/examples/longshot-synthetic.json --output output/longshot-primary
uv run python scripts/longshot_flip_70.py replay --config docs/examples/longshot-synthetic.json --output output/longshot-primary
uv run python scripts/longshot_flip_70.py analyze --config docs/examples/longshot-synthetic.json --output output/longshot-primary
uv run python scripts/longshot_flip_70.py all --config docs/examples/longshot-synthetic.json --output output/longshot-all
uv run python scripts/longshot_flip_70.py sensitivity --config docs/examples/longshot-synthetic.json --output output/longshot-sensitivity
```

`audit` and `extract` both inventory every configured market and materialize the
descriptive event table. `replay` repeats extraction before running all six
independent variants. `all` additionally writes analysis. `analyze` checks the
manifest's configuration and CSV hashes before reading persisted datasets.
Re-extraction clears trade/equity tables so an earlier replay cannot be silently
mixed with a new extraction.

Timing instrumentation remains enabled by default; `BACKTEST_ENABLE_TIMING=0`
is the opt-out. Nautilus replay logging remains `INFO`. No per-market HTML is
generated.

## Configuration And Server Inputs

[Synthetic configuration](examples/longshot-synthetic.json) supplies a small
deterministic fixture, not real market results. Statistics with too few markets
or days are explicitly unavailable.

[Local-source template](examples/longshot-local.json) is an intentionally blocked
native capture example: replace its session, timing, recorder contract and
resolution placeholders only after verification. It does not claim access to
any server. `data.vendor: "sqlite_capture"` uses exactly one absolute `local:`
SQLite path and the existing Nautilus L2 research/execution pipeline.
PMXT and Telonex remain separate supported vendors; their `data.sources`
use the existing loader syntax, for example
`local:/your/pmxt/raws`, `archive:r2v2.pmxt.dev`, or `api:TELONEX_API_KEY`.
API credentials belong in the environment, never configuration or output
manifests. Local book sources may still require the existing vendor loader's
market-metadata lookup; they are not a promise of fully offline real-data loading.
PMXT/Telonex adapter failures remain visible in the audit.

For PMXT/Telonex, `markets` is an explicit universe; there is no automatic
discovery of unknown server directories. Each record requires:

- `market_id` (optionally `market_slug`), `asset`, `duration_seconds`;
- timezone-qualified `start`, `close` (or integer UTC `start_ns`, `close_ns`);
- `tokens.UP` and `tokens.DOWN`, each with `token_index` and `token_id`, verified
  against the vendor's actual outcome and instrument identity;
- `settlement_winner` (`UP`, `DOWN`, or null) and timezone-qualified `settlement`
  (or `settlement_ns`), for postprocessing only;
- historically applicable fee inputs for replay, described below.

`filters` supports `market_ids`, `assets`, `durations_seconds`, `start`, and
`end`. Date filters retain entire markets, rather than cropping a market into a
misleading complete history. Excluded markets remain in the audit.

`research` configures `threshold` (primary **0.70**), share `quantity`,
`max_gap_seconds`, and `max_stale_seconds`. Threshold changes belong in explicitly
named `sensitivity_trials`. `execution` configures `decision_latency_ms`,
`order_latency_ms`, `max_entry_price`, `initial_cash`, and `max_positions`.
Quantity, latency, or threshold trials are prespecified comparisons, not an
optimizer choosing a winner on the final test.

Before real-data execution, supply the market universe, actual trading-close
timestamps, token mapping, book coverage, resolution timestamps, and verified
historical fees. A current API fee rate is not proof of a historical rate.

## Native Server Source Audit

The supplied evidence is **user-run read-only schema inspection**, not agent
server access. These paths occur only in configurable examples and commands,
never production defaults. Run in the repository environment from Setup; plain
server `python3` without project dependencies is not a replay environment.
Write reports to a separate output directory, not inside source stores.

```bash
uv run python scripts/longshot_flip_70.py source-audit --source-kind capture --source /home/ubuntu/books/smoke1.sqlite3 --sample-limit 20 --output /tmp/longshot-smoke1-audit
uv run python scripts/longshot_flip_70.py source-audit --source-kind capture --source /home/ubuntu/books/smoke2.sqlite3 --sample-limit 20 --output /tmp/longshot-smoke2-audit
uv run python scripts/longshot_flip_70.py source-audit --source-kind index --source /home/ubuntu/up_down_research/data/unified/quotes_unified.db --allowed-root /home/ubuntu/up_down_research/data --sample-limit 20 --output /tmp/longshot-index-audit
uv run python scripts/longshot_flip_70.py source-audit --source-kind parquet --source /home/ubuntu/up_down_research/data/quotes_threshold_5m/btc-updown-5m-1781730900_UP.parquet --sample-limit 5 --output /tmp/longshot-parquet-audit
uv run python scripts/longshot_flip_70.py source-audit --source-kind manifest --source /home/ubuntu/up_down_research/data/quotes_threshold_5m/manifest_btc.json --sample-limit 20 --output /tmp/longshot-manifest-audit
uv run python scripts/longshot_flip_70.py source-audit --source-kind transactions --source /home/ubuntu/db/octopus.db --output /tmp/longshot-fills-schema
```

`source-audit` writes `source_audit.json` without a market configuration or
partition boundaries. Samples are bounded (1–100); SQLite inspection is
read-only `mode=ro`, without `immutable`, and uses a query-work budget and lock
timeout. It does not copy databases, checkpoint, VACUUM, or count whole tables.
An unavailable file, lock, or exhausted work budget fails visibly. A bounded
sample is not exhaustive validation; rerun on explicitly chosen inputs after
reviewing schemas and indexes rather than increasing to a global scan.
This is important with the reported root disk at 94% utilization.

The two capture stores have active frames/events/levels/trades and quality
tables. The second schema also has `archive_levels_compact WITHOUT ROWID` and
heartbeat diagnostics. Presence of a table does not establish that it contains
records, reconstructs a complete history, or is safe to combine with active
storage. Compressed `raw_blocks` (`zlib-framed-v1`), archives, REST snapshots and
reconciliation reconstruction are not assumed interchangeable with active
events. Unsupported reconstruction must remain a replay blocker.

The unified database's supplied `windows` schema is **a file index only**.
Discovery resolves relative paths from the index directory, checks existence
and indexed sizes, and resolves symlinks against explicit `--allowed-root`
directories. Path errors stay visible in the sample report. Indexed windows
are not proof of coverage.

Parquet schemas remain unknown until inspected. Missing PyArrow produces an
explicit dependency message, not a fallback to PMXT. Inspection reports actual
schema, metadata row count and bounded sample rows, with **no supported replay
mapping** until column semantics and recorder evidence are supplied.
Nonfinite sample floats are tagged as `{"nonfinite_float": "nan"}` (or `inf`/
`-inf`), never normalized into valid prices.
Full-depth snapshot/delta replay must be distinguished from top-of-book/event-study data.
The threshold-selected filenames do not establish full L2 or an unbiased market
universe. No execution-realism or unbiased transition-probability claims are
permitted from such files. Manifest entries are not validated unique markets:
the user reported 17,348 BTC and 17,563 ETH entries, not complete-market counts.
`octopus.db` fills/state/wallet statistics are transactions, not historical quotes.

## Native Replay Prerequisites

The native adapter does not guess recorder semantics from column names.
Before enabling extraction/replay, obtain:

- recorder code or sample payload evidence establishing snapshot event
  boundaries (including empty snapshots), zero-size deletions, and BUY/SELL
  book-side and trade-aggressor meanings;
- explicit selected session IDs, populated representations and coverage/quality
  samples, including capture gaps, invalid segments and timestamp inversions;
- verified market open/close semantics distinct from planned times and actual
  collector connection times, plus session UP/DOWN token identity;
- an independent verified resolution mapping with winner and availability
  timestamp; the supplied session schema has **no settlement winner**;
- historically applicable fees and instrument tick/size precision.

The native template's null contract fields are deliberate blockers, not
defaults. `data.session_ids` selects exact sessions; `session_limit` and
`record_limit` cap loading rather than silently truncating replay. `markets`
uses session `condition_id` as `market_id` for fee/universe metadata.
Outcome identities come from `sessions.up_token_id`/`down_token_id`, not an
assumed token index. The contract selects exact start/end columns and timestamp
units separately from capture-connection timestamps.
Populated quality tables need explicit column/unit mappings with evidence.
If segments have a status column, `contract.segments` must identify it and
the supported `valid_value: "complete"` with recorder evidence; other status
semantics remain unsupported. Unknown/nonzero recorder flags fail closed.
Book/trade exchange timestamps are mandatory: receipt time is not a substitute
for unknown source freshness.
Native settlement labels belong in `data.resolutions[condition_id]`, requiring
`verified: true`, nonempty `evidence`, `winner: "UP"` or `"DOWN"`, and integer
post-close `settlement_ns`, not in unverified session or strategy metadata.
An empty resolution mapping leaves held positions unresolved; it does not
infer a winner from the last book.

Capture replay must preserve session sequence/event-index ordering and separate
received/exchange timestamps. Inversions are diagnostics, not a reason to sort
future exchange information into an earlier observable state. Invalid coverage
requires reset/quarantine and a fresh snapshot, not deltas applied to an old
book. Reconciliation observations cannot be applied before collector availability.
Both outcome snapshot anchors must be available at or before exact market open;
late initial snapshots cannot certify an unobserved opening prefix. Missing
complete-window evidence or any invalid/reset coverage permanently quarantines
that market for this pipeline, even if later snapshots reconstruct usable books.
`capture_record_order` retains sequence/event keys alongside the original
records, including same-receipt-time book/trade ordering through engine ingestion.
Active/archive/compact overlap or missing spans require explicit reconstruction
and precedence evidence; no blind concatenation or silent archive dropping.
Resolution labels remain in postprocessing inventory, never strategy-visible
instrument metadata. Outcomes are independent token books, not complements.

After filling the evidence-backed template:

```bash
uv run python scripts/longshot_flip_70.py audit --config docs/examples/longshot-local.json --output /tmp/longshot-native
uv run python scripts/longshot_flip_70.py extract --config docs/examples/longshot-local.json --output /tmp/longshot-native
uv run python scripts/longshot_flip_70.py replay --config docs/examples/longshot-local.json --output /tmp/longshot-native
uv run python scripts/longshot_flip_70.py analyze --config docs/examples/longshot-local.json --output /tmp/longshot-native
```

The unfilled template must fail closed. Synthetic fixtures validate adapter
behavior, not recorder behavior, statistical sufficiency of the smoke stores,
or real-server success. The server samples, population/coverage checks,
reconstruction semantics, Parquet schemas and resolution metadata are still
pending.

## Causal Touches And Observation Quality

Touch 1 is the first observed below-to-at/above-0.70 **midpoint** crossing by
either outcome. Touch 2 is the opposite outcome crossing; touch 3 is the return
to the initial favorite. Same-side recrossings do not advance the alternating
count. UP/DOWN are preserved, with `initial_favorite` and `cheap_side` making
normalization explicit.

Complete book events are applied atomically and grouped by observable
`ts_init`; deterministic ordering is not evidence of economic ordering within
a timestamp. Ambiguous simultaneous states are censored. An initially
above-threshold book is left-truncated, not a newly observed touch.
Both outcome books must be independently valid and sufficiently fresh.
Freshness uses source-event timestamps, not receipt timestamps; delayed data does
not become fresh merely by arriving.
Engine deltas are normalized to observable `ts_init` for both dispatch and book
matching; original input records retain their vendor timestamps for auditing.
Empty, locked/crossed, invalid-price/size books and recording gaps cannot create
crossings. Interrupted histories are censored; the primary alternating count is
not restarted and relabeled as a fresh touch 1 after a gap.
Censorship is a permanent strategy kill switch for that market: fresh later
snapshots do not restore entry or target decisions, and existing positions are
not optimistically flattened.

The midpoint touch label is **not** an executable price. Entry and target
diagnostics use the actual cheap-outcome ask and bid ladders at the configured
quantity. No executable opposite price is calculated as `1 - favorite_price`.
Unknown subsequent observation is null, never a completed non-reversal.
Settlement is joined only after extraction/execution; strategy instruments and
touch state contain no resolved outcomes.

## Execution And Fees

Variants are `touch1_hold`, `touch1_target`, `touch2_hold`, `touch2_target`,
`touch3_hold`, and `touch3_target`. Each permits one entry attempt per market.
They buy the opposite/cheap outcome at their selected observed touch. Entry
is a delayed aggressive, marketable IOC limit bounded by the price cap.
Actual ask-side arrival-book fills determine entry VWAP and deployed capital;
visible signal-time depth does not guarantee a fill.

Target variants require bid VWAP at least **0.70** for the actually filled
remaining quantity, plus sufficient bid depth at or above that sell floor.
They then submit a delayed aggressive IOC sell; depth disappearing before arrival
may produce partial or zero execution. Prespecified threshold trials use their
configured threshold, with the sell floor rounded upward to a valid tick.
Order latency is applied by Nautilus, after decision latency. A transient target
can disappear before arrival, causing partial or zero execution. Unsold shares
remain for settlement. There are no optimistic passive queue assumptions.
Pending arrivals are processed on replay events, so sparse cadence can increase
effective latency. Fills at or after trading close raise rather than being
silently accepted.
At decision time, censored, invalid, gapped, or stale paired books suppress the
exit. A vanished target price in otherwise valid books still submits the floor
IOC, allowing the engine to record a genuine miss rather than a fictional fill.
Partial fills, missed orders, commissions, and remaining quantities are retained
in the trade table.

Replay requires `fee_rate`, `fee_schedule: "polymarket_p*(1-p)"`,
`fee_verified: true`, a nonempty `fee_source`, and an applicability interval
`fee_valid_from_ns <= start_ns`, `fee_valid_to_ns >= close_ns`. The synthetic
fixture explicitly declares its known fee; this is not a real historical fee
assertion. Only the supported constant quote-currency schedule is replayable.
Unknown fee inputs and other schedules error rather than silently becoming zero.
Synthetic exemption requires `source: "synthetic"` and `fixture_fee_known: true`,
plus the explicit supported rate and schedule. The IOC fee model uses the existing
commission calculation, explicitly treating
marketable IOC limits as takers.

## Output Schemas

Outputs are CSV plus JSON. **Every CSV cell is JSON-encoded**, including strings,
numbers, booleans, lists, and null; use the supplied `read_dataset` API or
`json.loads` per cell. `read_dataset` raises the CSV field limit to the file
size so generated ordering metadata, including large `capture_record_order`
cells, can be reread. Nanosecond timestamps are UTC integers, not floating-point
epoch seconds. Empty datasets have no rows. Nested columns remain auditable
without lossy flattening.

| File | Contents |
| --- | --- |
| `market_audit.csv` | One row per inventory market, asset/duration/window, mapping, coverage endpoints/counts, exclusions, invalid/gap/censor reasons, touch count, settlement availability, source coverage |
| `events.csv` | One row per observed alternating touch, side/initial favorite/cheap side, timestamp, configured quantity, midpoint label, ask VWAP/spread/depth, remaining lifetime, next-touch label/time, executable excursion diagnostics, postprocessing settlement and optional underlying features |
| `trades.csv` | One row per replay-eligible market per independent variant, including zero entries; `entered`, entry/exit/remaining and lot-rounded requested quantities, fees, `cash_pnl`, `pnl`, settlement status, attempts and actual fill records; excluded markets remain in the audit |
| `equity.csv` | Combined account NAV/cash per variant and timestamp, with initial capital included exactly once; never sum independent variants |
| `market_equity.csv` | Zero-initial-capital per-market net mark-to-market attribution and current marked exposure, for synchronous asset/duration/partition aggregation; never add these contributions to account NAV again |
| `execution.json` | Variant identities and execution assumptions/disclosures |
| `analysis.json` | Partitioned transition denominators/censor counts, sample warnings, state buckets, independent-variant metrics, day-block confidence intervals, drawdown/exposure and unavailable features |
| `manifest.json` | Schema version, full configuration, seed, Git commit/dirty status when available, loaded source coverage and record hashes, CSV checksums, synthetic flag and warnings |

Trade columns include `market_id`, `variant`, `entered`, `entry_attempted`,
`entry_quantity`, `requested_quantity`, `exit_quantity`, `remaining_quantity`, `entry_price`,
`entry_ts_ns`, `exit_ts_ns`, `fees`, `pnl` (null for unresolved holdings),
`settlement_status`, `fills`, and `attempts`. Fill records include `action`,
`side`, `quantity`, `price`, `commission`, `ts_ns`, and `client_order_id`.
Analysis derives deployed capital from actual buy notional plus commissions,
and joins `next_touch` by market and selected touch number.
Account rows use `variant`, `ts_ns`, `cash`, `equity`, and `open_positions`.
Attribution rows use `market_id`, `variant`, `ts_ns`, `equity`, and `exposure`;
their equity is cumulative net market PnL, not account NAV.

The analysis boundary maps `entered` to `filled` and `pnl` to `net_pnl`, and
calculates deployed capital from actual buy-fill notional plus buy commissions.
An open remainder without valid settlement has null `pnl`, not zero profit.
Reporting expires bid marks at the source-age limit. Expiry points propagate
unavailable equity/exposure while held; they do not fabricate book events or
fills. A known settlement can restore the final cash valuation. Post-close
null marks are retained so a missing valuation invalidates portfolio statistics
instead of being dropped and forward-filled.

Executable MFE/MAE are quantity-specific pre-close bid-VWAP excursions relative
to the actual observed ask VWAP, not guaranteed future fills or trading signals.
A price first observed at close is not executable, and a close-only suffix is
unavailable.
Incomplete future paths/depth are flagged and complete excursion statistics
remain unavailable. Successful reversals, non-reversals, no-touch markets, and
censored paths all remain visible in their appropriate denominators.

## Analysis And Validation

Mandatory `partitions.discovery_end`, `validation_end`, and `final_end` are
strictly increasing timezone-qualified boundaries. Markets are assigned by
their **whole economic lifetime**, through the later of trading close and a
known settlement timestamp; straddling markets are excluded and reported.
Delayed settlement is not moved backward to close or allowed to leak training
labels across a partition boundary.
There is no automatic final-test optimization.

Assets, durations, and partitions remain separate. Prespecified current-state
buckets use fraction of lifetime remaining, entry VWAP, spread, and depth.
Touch-history comparisons include overlap and sample-size warnings and are
observational, not causal proof. p12/p23 denominators distinguish complete
at-risk paths from censoring; incomplete observation is not a no-flip outcome.

Seeded UTC market-start-day bootstrap blocks retain all records for each market,
including zero-entry eligible markets. Confidence intervals for transition
probabilities and net returns require configured minimum days and markets.
Insufficient samples produce null/unavailable estimates, not spurious zeros.
Variant metrics include trade and eligible-market counts, fill rate, next-touch
probability, net PnL/trade, net PnL/eligible market, deployed return, portfolio
drawdown, and exposure. Portfolio drawdown is computed on a synchronous sum of
market equity, not a sum of individual maximum drawdowns.

Optional `underlying.records_by_asset` accepts ordered unique
`{"ts_ns": ..., "price": ...}` records. `max_age_seconds` and `lookback_seconds`
bound the backward-only as-of join. A known `reference_price` and
`reference_ns <= event time` enable fractional distance to reference.
Recent return uses the backward window, and volatility is unannualized
population dispersion of consecutive log returns (at least two returns).
Missing references, insufficient history, stale input, and recording gaps leave
individual features null. Nothing is reconstructed from settlement.

## Limitations And Verification

The synthetic integration is plumbing evidence only. No real-market performance,
server execution, or profitability is asserted. Historical completeness cannot
be proved from a vendor file alone; configurable gap/stale tolerances are study
assumptions. Final timestamp groups require a subsequent clock-driving replay
event; no future data is fabricated to force execution.

Settlement proceeds are not recycled into intrarun buying power. Bid-marked
equity is not a quantity-guaranteed liquidation value. Unsupported fee schedules,
share-withheld commissions, hidden liquidity, and venue-specific matching
behavior require additional verified integration before claims of realism.
The pipeline does not optimize thresholds or combine six independent PnLs
into an imaginary portfolio.
Execution position-limit slots remain occupied by every held or pending entry,
including closed markets and positions settled only in offline reporting. Slots
are released only by actual sells; this conservative restriction can prevent
later entries even when reporting has credited a settlement payout.

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/test_longshot*.py -q
uv run pytest tests/ -q
uv run mkdocs build --strict
uv run python scripts/generate_codebase_uml.py
```

See [Project Status](project-status.md) for the implementation PR and recorded
verification limitations.

## Separate Top-Of-Book Parquet Study

This **descriptive, offline quote study** is separate from the L2 research and
execution pipeline above. Run
`python -m prediction_market_extensions.backtesting.longshot.quote_cli` with
`audit`, `extract`, or `report`; there is deliberately no replay command.
`top_of_book` is not an execution vendor. Real `BookReplay` APIs are unchanged.
No synthetic depth, complement prices, PnL, settlement labels, fees, guaranteed
fills, or live-trading conclusions are produced.

The supplied server evidence describes `windows(window_start_ts INTEGER,
symbol,timeframe,side,data_source,file_path,file_size,indexed_at)` in
`/home/ubuntu/up_down_research/data/unified/quotes_unified.db`. Relative paths
resolve against **DATA ROOT**, not the index parent. Explicit absolute
`data_root`, `index`, and `allowed_roots` are required; traversal, symlink
escape, filename mismatch, and indexed-size mismatch are rejected. SQLite is
opened read-only, with bounded query work, no rebuild or downloader invocation.
Missing index/query results remain scheduled audit rows. Files with duplicate
sources require an explicit `study.source`; duplicate rows within that source
are excluded. UP from one source and DOWN from another are never spliced.

The assumed market schedule is every duration-aligned start in `[start,end)`,
specified in UTC, for one explicit asset/duration per invocation. This defines
an audit denominator independently of downloads, manifests, or index counts.
It is **not independent evidence that every scheduled market was listed**.
Missing, one-sided, invalid, ambiguous, and partition-straddling windows remain
visible. There is no threshold-selected universe or final-test optimization.

Parquet must contain integer `timestamp_us`/`local_timestamp_us` and string
`exchange,market_id,slug,asset_id,outcome,bid_price,bid_size,ask_price,ask_size`.
The expected filename is `<asset>-updown-<duration>-<start>_<UP|DOWN>.parquet`.
Every row, including premarket and post-close rows in a token/day file, must
agree on full identity; the two tokens must differ and share a market ID.
Economic observations are clipped to `[open,close)`, with only the configured
backward preopen anchor interval retained. No close-boundary quote is a touch.
Exchange spelling is an explicit, case-sensitive `study.exchange` setting
(`row["exchange"] != study.exchange`). The code default remains `POLYMARKET`.
The server examples below use `polymarket` only because user inspection of these
quote files observed that lowercase spelling. Do not change the default to fit
one sample, and do not treat a case mismatch as a reason to relax identity,
gap, anchor, or resource guards.

Optional `study.manifest` names a JSON list of `ts,slug,up_id,down_id`.
Duplicate windows, contradictory tokens/slugs, and conflicts with verified
identities are rejected. **Internally consistent manifests are not independent
verification.** Optional `study.verified_identities` uses the same list fields
plus `market_id` and `evidence`: a user attestation of genuinely independent
identity evidence, not an automatically certified label. Do not copy an
unverified manifest into this file and call it verified.
Without that evidence, numbered paths remain ineligible/censored, while
diagnostic crossings are still emitted.

The user-reviewed downloader requests each `asset_id` separately and writes
response bytes, with no local complementary-price transform. However, one old
batch downloader has a stale `token_ids[key]` assignment: filenames alone are
not reliable identity evidence. This implementation does not execute or change
downloaders, access credentials, or retrieve server data. Rotate the exposed
Telonex credential; never put any key into a config or report.

### Observation And Censoring Convention

The experimental **terminal whole-row per exchange timestamp in original file
order** policy selects the last row, including its nulls. It does not select the
last non-null value separately for each column. Same-time transient locked or
crossed rows can resolve to a valid terminal row; invalid terminal rows remain
coverage breaks. No invalid/null quote is forward-filled.
Integer microseconds and exact decimal threshold comparisons are retained.
Decimal strings are bounded to 256 characters/exponent magnitude 256; oversized
or unsupported encodings are invalid, not rounded into valid observations.

Terminal rows retain source path/row index, first group row index, group count,
exchange and receipt times, receipt min/max/span, and invalid-group counts.
Raw invalid/locked/crossed rows retain their original values and indices in
`raw_anomalies.jsonl`. Diagnostics report exchange/receipt inversions, raw
file-order crossing counts, coalesced counts, gaps, and boundary coverage.
Raw/coalesced sensitivity describes different observation conventions; neither
recovers certified ground truth. Receipt inversions are not repaired.
Exchange-time grouping/sorting is not a proven atomic venue event or executable
arrival ordering.

`gap_us`, `pair_age_us`, `anchor_age_us`, and `terminal_age_us` are explicit
positive microsecond tolerances (defaults 1,000,000). They are assumptions, not
proof of collector outages or complete histories. An opening anchor must be
valid, independently identity-verified, strictly preopen, and within the anchor
age. An above-threshold opening anchor is left-truncated. The first post-open
quote is never proof of a complete prefix. Invalid rows, excess gaps, stale
pairs, and simultaneous opposing crossings permanently censor numbering:
later recoveries do not restart it. Pre-break observed successes remain known;
unfinished interrupted paths remain unknown, not false.
A numbered touch requires both outcomes to be valid and within `gap_us` of the
decision timestamp, independently of `pair_age_us`. A wider pairing tolerance
cannot keep a non-updating outcome eligible after its coverage-gap limit; that
gap censors numbering immediately instead of waiting for the stale outcome to
update.

Both outcomes at each common exchange timestamp are applied before evaluating
signals. Pairing uses only backward quotes with explicit maximum age; no
opposite quote is inferred from a complement or a future observation.
Touches alternate: crossing from below to midpoint >=0.70 on the first side,
then the opposite side, then the initial side. Same-side recrossings do not
advance numbering. Diagnostic crossings preserve ambiguity and censored history.
Touches retain actual opposite-side bid/ask/sizes, age, remaining lifetime, and
transition microseconds. This opposite token is labeled the cheap-side quote,
not inferred to have any guaranteed price or liquidity.
Separately observed bid>=0.70 episode starts are quote diagnostics, **not fills**.

### Outputs And Bounded Resources

Processing is sequential (one market, two bounded streams; no worker pool).
Defaults: 4,096-row Parquet batches with Arrow threading disabled, 512 MiB
compressed and metadata-decoded bytes per file, 2 million rows per file,
500,000 retained timestamps per outcome, 10,000 raw anomalies per file,
10,000 scheduled windows, 256 MiB total streaming output. Configurable limits
have hard ceilings; split ranges rather than bypassing guards. Metadata bounds
are not an OS memory sandbox for hostile Parquet. No large-store copies or
unbounded background jobs are created. Reports must be outside allowed roots
and use a new destination. Output-limit failures leave partial files **without
a completed manifest**; these cannot be reported as completed studies.

`audit.json` and `windows.jsonl` retain census and coverage exclusions;
`touches.csv` contains numbered quote touches; `crossings.jsonl` contains
diagnostic midpoint crossings and bid-target episode starts.
`raw_anomalies.jsonl` preserves invalid raw rows. Optional
`emit_observations: true` writes every retained terminal row to
`observations.jsonl`; keep it false for batch runs to avoid large output stores.
`audit` validates/analyses files but omits event and terminal-row exports.
`manifest.json` records config, resolved roots, commit/dirty state, source and
input/output SHA256 hashes, policy, and census exclusions.
A missing index is recorded as input hash `unavailable` rather than aborting
the supported census. If a selected file cannot be hashed after a read error,
that source hash is `unavailable` and later scheduled windows still continue.

With prespecified `partitions.discovery_end,validation_end,final_end`,
`report.json` separates asset/duration/partition, scheduled/eligible/complete/
missing windows, p12/p23 eligible at-risk paths, completed at-risk outcomes,
observed successes, completed non-successes, and censored unknowns.
Known success before a later interruption counts as observed; known absence
requires continuous acceptable coverage through close.
Complete-case ratios can be selection-biased. Guarded estimates require no
unknown at-risk paths, `analysis.min_days` (default 3), and `min_markets`
(default 20). Optional seeded UTC day-block bootstrap (`bootstrap_samples`,
default 0; `seed`, default 70) reuses existing sufficiency/quantile primitives.
All market lifetimes must fit their partition; boundaries are validated before
reading data. A separately run `report` verifies the extraction's window hash
and uses its recorded configuration, not new favorable test boundaries.

### User-Reported Single-Market Diagnostic

These are **user-supplied observations, not agent server access, fixture truth,
complete-market certification, executable fills, PnL, or profitability**.

Earlier inspection, before the offline CLI was run, reported
`btc-updown-5m-1781730900` (2026-06-17 21:15–21:20 UTC): both files internally
matched manifests and the same market ID; first in-window quotes at open+0.014
seconds and final quotes at close−0.012 seconds; transient crossed/locked raw
rows resolved within timestamp groups. Those boundary offsets were not restated
by the later CLI closeout and still do not prove a verified preopen anchor.

User-run server verification at commit
`967a06213bcbdd9f4265092af6660aec675b3c4a` (terminal output supplied by the user):
the full suite was 865 passed, 1 skipped, and 407 warnings. One-market offline
`audit` and `extract` completed only after `study.exchange` was changed from the
default `POLYMARKET` to the observed lowercase `polymarket`. The original failed
output was preserved and a fresh destination was used. Both files loaded with no
census exclusions. UP had 38,871 in-window rows and 35,787 terminal timestamps;
DOWN had 38,869 and 35,785. Both had zero invalid terminal rows and a maximum
exchange gap of 354 ms. Receipt inversions were UP 5 and DOWN 7. Fourteen
diagnostic midpoint crossings were exported. The first alternating sequence
reproduced UP +31.782 s, DOWN +126.871 s, and UP +286.255 s, each with bid 0.69
and ask 0.71. Every midpoint crossing had `prefix_intact=false`. The window
remains censored and ineligible, with UP and DOWN
`unverified_or_missing_backward_open_anchor` exclusions. p12 and p23 are not at
risk and success remains null. This does not certify independent token
identities, opening anchors, complete history, fills, or profitability.
The June chronological batch was not run.

### Exact Offline Server Reproduction

Run in an existing repository checkout with its Python dependencies installed
(including a patched PyArrow, >=23.0.1). No command below downloads market data.
The examples set `exchange` to the inspected lowercase `polymarket`; the code
default remains `POLYMARKET`, and identity comparison stays case-sensitive.
Replace `source` only when inspection shows more than one source, and record
that choice before extracting. Optional manifest/verified-identity paths must be
under allowed roots. Do not invent `verified_identities` or widen gap/anchor
tolerances to make this window eligible.
The single-market `audit`/`extract` below is the command shape the user ran at
`967a06213bcbdd9f4265092af6660aec675b3c4a`. This agent did not access the server
or rerun it. The fixed June batch is an example only and was **not** run.

```bash
cd /absolute/path/to/prediction-market-backtesting
cat > /tmp/longshot-single.json <<'JSON'
{
  "study": {
    "data_root": "/home/ubuntu/up_down_research/data",
    "allowed_roots": ["/home/ubuntu/up_down_research/data"],
    "index": "/home/ubuntu/up_down_research/data/unified/quotes_unified.db",
    "start": "2026-06-17T21:15:00Z",
    "end": "2026-06-17T21:20:00Z",
    "symbol": "BTC", "timeframe": "5m", "exchange": "polymarket",
    "source": null, "gap_us": 1000000, "pair_age_us": 1000000,
    "anchor_age_us": 1000000, "terminal_age_us": 1000000
  },
  "emit_observations": true,
  "max_output_bytes": 268435456
}
JSON
uv run python -m prediction_market_extensions.backtesting.longshot.quote_cli audit --config /tmp/longshot-single.json --output /tmp/longshot-single-audit
uv run python -m prediction_market_extensions.backtesting.longshot.quote_cli extract --config /tmp/longshot-single.json --output /tmp/longshot-single-extract
cat /tmp/longshot-single-extract/audit.json
```

Then run this **fixed chronological BTC5m example** (June 1–10 discovery,
June 11–20 validation, June 21–30 final). These dates are an example
prespecification, not selected for performance or known coverage; establish your
own schedule before analysis. Do not alter final dates after seeing results.
No touch probabilities are certified merely because the index contains files.

```bash
cat > /tmp/longshot-btc5m-june.json <<'JSON'
{
  "study": {
    "data_root": "/home/ubuntu/up_down_research/data",
    "allowed_roots": ["/home/ubuntu/up_down_research/data"],
    "index": "/home/ubuntu/up_down_research/data/unified/quotes_unified.db",
    "start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z",
    "symbol": "BTC", "timeframe": "5m", "exchange": "polymarket",
    "source": null, "gap_us": 1000000, "pair_age_us": 1000000,
    "anchor_age_us": 1000000, "terminal_age_us": 1000000
  },
  "emit_observations": false,
  "max_output_bytes": 268435456,
  "partitions": {
    "discovery_end": "2026-06-11T00:00:00Z",
    "validation_end": "2026-06-21T00:00:00Z",
    "final_end": "2026-07-01T00:00:00Z"
  },
  "analysis": {"bootstrap_samples": 1000, "seed": 70, "min_days": 3, "min_markets": 20}
}
JSON
uv run python -m prediction_market_extensions.backtesting.longshot.quote_cli audit --config /tmp/longshot-btc5m-june.json --output /tmp/longshot-btc5m-june-audit
uv run python -m prediction_market_extensions.backtesting.longshot.quote_cli extract --config /tmp/longshot-btc5m-june.json --output /tmp/longshot-btc5m-june-extract
uv run python -m prediction_market_extensions.backtesting.longshot.quote_cli report --input /tmp/longshot-btc5m-june-extract --output /tmp/longshot-btc5m-june-report.json
```

If a resource guard rejects a range, keep that failure visible, use smaller
prespecified ranges with the same partition boundaries, and do not count partial
output as complete coverage. Each report describes its own explicit census.
Offline fixture verification:

```bash
uv run ruff check prediction_market_extensions/backtesting/longshot/top_of_book.py prediction_market_extensions/backtesting/longshot/quote_cli.py tests/test_longshot_top_of_book.py
uv run ruff format --check prediction_market_extensions/backtesting/longshot/top_of_book.py prediction_market_extensions/backtesting/longshot/quote_cli.py tests/test_longshot_top_of_book.py
uv run pytest tests/test_longshot_top_of_book.py -q
```

### Review-Fix Verification

The four review fixes from `5fa35e3` were rechecked against the code at the
user-run revision `967a06213bcbdd9f4265092af6660aec675b3c4a`. This documentation
closeout does not change those guards, so the server results above remain
results for that revision.

- Coverage gaps are independent of pairing. `extract` accepts a numbered touch
  only while `prefix` is intact, and it clears `prefix` when either outcome is
  outside `gap_us`, even if `pair_age_us` is wider
  (`top_of_book.py`, `within_gap`).
  `test_coverage_gap_blocks_numbered_touch_before_stale_update` uses
  `gap_us=1000` and `pair_age_us=1000000`: the later opposite crossing stays
  diagnostic, p12 remains at risk with null success, and the window is censored.
- A missing index is an explicit unavailable hash. `quote_cli.py` writes
  `input_sha256` `unavailable` when `study.index` does not exist, without
  opening it. `test_missing_index_hash_is_explicit` completes `audit` and keeps
  the `index_unavailable` census exclusion.
- A non-text index path is an excluded window. `index_file_path` raises
  `ValueError` before constructing `Path`. Census catches that error.
  `test_null_index_path_remains_an_excluded_window` retains the scheduled window
  and the valid opposite file.
- Source hashing after a read error is best-effort. The `OSError` handler stores
  the read error and `sha256: unavailable`, then later windows still yield.
  `test_unreadable_source_hash_does_not_abort_later_windows` covers a disappeared
  file across two scheduled windows.

The review threads were rechecked and left unresolved for the reviewer. This
closeout has no permission to resolve them.
