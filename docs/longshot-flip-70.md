# 70-Cent Longshot Research

This is an observational crypto UP/DOWN research pipeline, not evidence of
profitability. It shares one settlement-free alternating-touch state machine
between dataset extraction and NautilusTrader strategies. Execution uses genuine
`OrderBookDeltas`, `L2_MBP`, and the existing framework's data-loading and execution
APIs, not a separate price-path simulator. Each of the six variants has its own
account and capital/position limits.

## Commands

Run from the repository root after the installation in [Setup](setup.md).
The bundled synthetic configuration requires no network, credentials, or server
files. Keep research outputs outside the repository or under ignored `output/`.

```bash
uv run python scripts/longshot_flip_70.py audit --config docs/examples/longshot-synthetic.json --output /tmp/longshot-primary
uv run python scripts/longshot_flip_70.py extract --config docs/examples/longshot-synthetic.json --output /tmp/longshot-primary
uv run python scripts/longshot_flip_70.py replay --config docs/examples/longshot-synthetic.json --output /tmp/longshot-primary
uv run python scripts/longshot_flip_70.py analyze --config docs/examples/longshot-synthetic.json --output /tmp/longshot-primary
uv run python scripts/longshot_flip_70.py all --config docs/examples/longshot-synthetic.json --output /tmp/longshot-all
uv run python scripts/longshot_flip_70.py sensitivity --config docs/examples/longshot-synthetic.json --output /tmp/longshot-sensitivity
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

[Local-source template](examples/longshot-local.json) contains placeholders:
replace its slug, dates, token identities, and local path with actual inventory.
It does not claim access to any server. `data.vendor` is `pmxt` or `telonex`;
`data.sources` uses the existing loader syntax, for example
`local:/your/pmxt/raws`, `archive:r2v2.pmxt.dev`, or `api:TELONEX_API_KEY`.
API credentials belong in the environment, never configuration or output
manifests. Local book sources may still require the existing vendor loader's
market-metadata lookup; they are not a promise of fully offline real-data loading.
PMXT/Telonex adapter failures remain visible in the audit.

`markets` is an explicit universe; there is no automatic discovery of unknown
server directories. Each record requires:

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
Empty, locked/crossed, invalid-price/size books and recording gaps cannot create
crossings. Interrupted histories are censored; the primary alternating count is
not restarted and relabeled as a fresh touch 1 after a gap.

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

Target variants sell using a delayed aggressive IOC limit at **0.70**, only
when observed bid depth supports the held quantity at the target.
Order latency is applied by Nautilus, after decision latency. A transient target
can disappear before arrival, causing partial or zero execution. Unsold shares
remain for settlement. There are no optimistic passive queue assumptions.
Partial fills, missed orders, commissions, and remaining quantities are retained
in the trade table.

Replay requires `fee_rate`, `fee_schedule: "polymarket_p*(1-p)"`,
`fee_verified: true`, a nonempty `fee_source`, and an applicability interval
`fee_valid_from_ns <= start_ns`, `fee_valid_to_ns >= close_ns`. The synthetic
fixture explicitly declares its known fee; this is not a real historical fee
assertion. Only the supported constant quote-currency schedule is replayable.
Unknown fee inputs and other schedules error rather than silently becoming zero.
The IOC fee model uses the existing commission calculation, explicitly treating
marketable IOC limits as takers.

## Output Schemas

Outputs are CSV plus JSON. **Every CSV cell is JSON-encoded**, including strings,
numbers, booleans, lists, and null; use the supplied `read_dataset` API or
`json.loads` per cell. Nanosecond timestamps are UTC integers, not floating-point
epoch seconds. Empty datasets have no rows. Nested columns remain auditable
without lossy flattening.

| File | Contents |
| --- | --- |
| `market_audit.csv` | One row per inventory market, asset/duration/window, mapping, coverage endpoints/counts, exclusions, invalid/gap/censor reasons, touch count, settlement availability, source coverage |
| `events.csv` | One row per observed alternating touch, side/initial favorite/cheap side, timestamp, configured quantity, midpoint label, ask VWAP/spread/depth, remaining lifetime, next-touch label/time, executable excursion diagnostics, postprocessing settlement and optional underlying features |
| `trades.csv` | One row per market per independent variant, including zero entries; attempts and actual fill records, quantity, fees, deployed capital, net PnL and unresolved remainder status |
| `equity.csv` | Combined account NAV/cash per variant and timestamp, with initial capital included exactly once; never sum independent variants |
| `market_equity.csv` | Zero-initial-capital per-market net mark-to-market attribution and current marked exposure, for synchronous asset/duration/partition aggregation; never add these contributions to account NAV again |
| `execution.json` | Variant identities and execution assumptions/disclosures |
| `analysis.json` | Partitioned transition denominators/censor counts, sample warnings, state buckets, independent-variant metrics, day-block confidence intervals, drawdown/exposure and unavailable features |
| `manifest.json` | Schema version, full configuration, seed, Git commit/dirty status when available, loaded source coverage and record hashes, CSV checksums, synthetic flag and warnings |

Executable MFE/MAE are quantity-specific observed bid-VWAP excursions relative
to the actual observed ask VWAP, not guaranteed future fills or trading signals.
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
