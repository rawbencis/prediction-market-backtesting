"""Independent Nautilus L2 portfolios, with fill-ledger settlement accounting.

Historical execution is deliberately unavailable without a verified, applicable
fee schedule. Current CLOB fees are not evidence of historical fees. Only the
explicit p*(1-p) quote-currency schedule is supported here, not share-withheld
fees, changing schedules, fee exponents, rebates or passive execution.
"""

from __future__ import annotations

import json
from decimal import Decimal
from math import isfinite
from pathlib import Path

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDC
from nautilus_trader.model.data import OrderBookDelta, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AccountType, BookType, LiquiditySide, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Money
from nautilus_trader.risk.config import RiskEngineConfig

from prediction_market_extensions import install_commission_patch
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission
from prediction_market_extensions.adapters.prediction_market.backtest_utils import (
    compute_binary_settlement_pnl,
)
from prediction_market_extensions.backtesting._backtest_runtime import add_engine_data_by_type
from prediction_market_extensions.backtesting._execution_config import StaticLatencyConfig
from prediction_market_extensions.backtesting.longshot.state import (
    DEFAULT_MAX_GAP_NS,
    DEFAULT_MAX_STALE_NS,
    observations_from_records,
)
from strategies.longshot_flip import LongshotFlipConfig, LongshotFlipStrategy

VARIANTS = tuple(
    f"touch{number}_{exit_mode}" for number in (1, 2, 3) for exit_mode in ("hold", "target")
)
_LOG_GUARD = None


class LongshotIOCFees(FeeModel):
    """IOC limits are taker orders, unlike the repository's passive limit model."""

    def get_commission(self, order, fill_qty, fill_px, instrument):
        fee = calculate_commission(
            quantity=Decimal(str(fill_qty)),
            price=Decimal(str(fill_px)),
            fee_rate=instrument.taker_fee,
            liquidity_side=LiquiditySide.TAKER,
        )
        return Money(Decimal(str(fee)), instrument.quote_currency)


def _observable_record(record):
    timestamp = int(record.ts_init)
    if isinstance(record, TradeTick):
        return TradeTick(
            record.instrument_id,
            record.price,
            record.size,
            record.aggressor_side,
            record.trade_id,
            timestamp,
            timestamp,
        )
    return OrderBookDeltas(
        record.instrument_id,
        [
            OrderBookDelta(
                delta.instrument_id,
                delta.action,
                delta.order,
                delta.flags,
                delta.sequence,
                timestamp,
                timestamp,
            )
            for delta in record.deltas
        ],
    )


def _fee_rate(market):
    rate = market.get("fee_rate")
    if rate is None or not isfinite(float(rate)) or float(rate) < 0:
        raise ValueError(f"{market['market_id']}: missing/invalid applicable fee_rate")
    if market.get("source") == "synthetic":
        if market.get("fixture_fee_known") is not True:
            raise ValueError("Synthetic replay requires an explicit known fixture fee")
    elif not (
        market.get("fee_verified") is True
        and market.get("fee_source")
        and int(market.get("fee_valid_from_ns", 2**63 - 1)) <= market["start_ns"]
        and int(market.get("fee_valid_to_ns", -1)) >= market["close_ns"]
    ):
        raise ValueError(
            f"{market['market_id']}: unverified historical fee inputs; "
            "current vendor/CLOB rates cannot establish an applicable historical schedule"
        )
    if market.get("fee_schedule") != "polymarket_p*(1-p)":
        raise ValueError("Unsupported historical fee schedule (requires polymarket_p*(1-p))")
    return float(rate)


def _engine(initial_cash, order_latency_ms):
    # Same engine/risk/account settings as PredictionMarketBacktest._build_engine.
    # Its passive-book fee factory cannot price taker IOC limits correctly.
    global _LOG_GUARD
    install_commission_patch()
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("LONGSHOT-001"),
            logging=LoggingConfig(log_level="INFO"),
            risk_engine=RiskEngineConfig(),
        )
    )
    engine.add_venue(
        venue=Venue("POLYMARKET"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=USDC,
        starting_balances=[Money(initial_cash, USDC)],
        fee_model=LongshotIOCFees(),
        book_type=BookType.L2_MBP,
        latency_model=StaticLatencyConfig(
            insert_latency_ms=order_latency_ms,
            update_latency_ms=order_latency_ms,
            cancel_latency_ms=order_latency_ms,
        ).build_latency_model(),
        liquidity_consumption=True,
        queue_position=False,
        bar_execution=False,
        trade_execution=True,
    )
    # Nautilus logging is process-global; retain its guard across independent
    # engine lifetimes (including repeated public replay calls).
    guard = engine.kernel.get_log_guard()
    if guard is not None:
        _LOG_GUARD = guard
    return engine


def _report(markets, observations, strategy, variant, initial_cash):
    fills = strategy.fills
    trades = []
    timeline = {}
    balances = {
        market["market_id"]: {"quantity": 0.0, "cash": 0.0, "side": None} for market in markets
    }
    marks = {}
    mark_source_times = {}
    for market in markets:
        key = market["market_id"]
        events = [fill for fill in fills if fill["market_id"] == key]
        if any(fill["ts_ns"] >= market["close_ns"] for fill in events):
            raise ValueError(
                f"{key}: fill at/after close; replay cadence cannot faithfully resolve order arrival"
            )
        buys = [fill for fill in events if fill["action"] == "buy"]
        sells = [fill for fill in events if fill["action"] == "sell"]
        remaining = sum(fill["quantity"] for fill in buys) - sum(fill["quantity"] for fill in sells)
        cash = sum(
            (1 if fill["action"] == "sell" else -1) * fill["quantity"] * fill["price"]
            - fill["commission"]
            for fill in events
        )
        side = strategy.selected.get(key)
        settled = (
            market.get("settlement_winner") in ("UP", "DOWN")
            and market.get("settlement_ns") is not None
            and int(market["settlement_ns"]) >= market["close_ns"]
        )
        payout = float(side == market.get("settlement_winner")) if settled else None
        # The helper's YES contract is the selected instrument, not the UP token.
        pnl = (
            compute_binary_settlement_pnl([{**fill, "side": "yes"} for fill in events], payout)
            if settled and events
            else cash
            if remaining == 0
            else None
        )
        trades.append(
            {
                "market_id": key,
                "variant": variant,
                **strategy.signals.get(key, {}),
                "entered": bool(buys),
                "entry_attempted": key in strategy.attempted,
                "censored": strategy.states[key].censored,
                "censor_reasons": list(strategy.states[key].reasons),
                "entry_quantity": sum(fill["quantity"] for fill in buys),
                "exit_quantity": sum(fill["quantity"] for fill in sells),
                "remaining_quantity": remaining,
                "entry_price": (
                    sum(fill["price"] * fill["quantity"] for fill in buys)
                    / sum(fill["quantity"] for fill in buys)
                    if buys
                    else None
                ),
                "entry_ts_ns": buys[0]["ts_ns"] if buys else None,
                "exit_ts_ns": sells[-1]["ts_ns"] if sells else None,
                "fees": sum(fill["commission"] for fill in events),
                "cash_pnl": cash,
                "settlement_winner": market.get("settlement_winner"),
                "settlement_ns": market.get("settlement_ns"),
                "settlement_status": "settled" if settled else "unresolved",
                "pnl": pnl,
                "fills": events,
                "attempts": [a for a in strategy.attempts if a["market_id"] == key],
            }
        )
        for observation in observations[key]:
            timeline.setdefault(observation.ts_ns, []).append(("mark", key, observation))
        for fill in events:
            timeline.setdefault(fill["ts_ns"], []).append(("fill", key, fill))
        if settled:
            timeline.setdefault(int(market["settlement_ns"]), []).append(("settle", key, payout))
    horizon = max(max(timeline), max(market["close_ns"] for market in markets))
    for key, market_observations in observations.items():
        for observation in market_observations:
            for side, book in (("UP", observation.up), ("DOWN", observation.down)):
                expiry = book.ts_ns + strategy.config.max_stale_ns + 1
                if book.valid and observation.ts_ns < expiry <= horizon:
                    timeline.setdefault(expiry, []).append(("expire", key, (side, book.ts_ns)))

    def current_mark(key, side, timestamp):
        source_ns = mark_source_times.get(key, {}).get(side)
        if source_ns is None or not 0 <= timestamp - source_ns <= strategy.config.max_stale_ns:
            return None
        return marks.get(key, {}).get(side)

    equity = []
    market_equity = []
    settled_markets = set()
    for timestamp, events in sorted(timeline.items()):
        changed = set()
        for action, key, value in events:
            balance = balances[key]
            if action == "mark" and key not in settled_markets:
                marks[key] = {
                    side: book.best_bid
                    if not value.gap
                    and not book.is_stale(value.ts_ns, strategy.config.max_stale_ns)
                    else None
                    for side, book in (("UP", value.up), ("DOWN", value.down))
                }
                mark_source_times[key] = {"UP": value.up.ts_ns, "DOWN": value.down.ts_ns}
                changed.add(key)
            elif action == "fill":
                sign = 1 if value["action"] == "buy" else -1
                balance["quantity"] += sign * value["quantity"]
                balance["cash"] -= sign * value["quantity"] * value["price"] + value["commission"]
                balance["side"] = value["side"]
                changed.add(key)
            elif action == "settle":
                balance["cash"] += balance["quantity"] * value
                balance["quantity"] = 0.0
                settled_markets.add(key)
                changed.add(key)
            elif action == "expire" and key not in settled_markets:
                side, source_ns = value
                if (
                    balance["quantity"] > 0
                    and balance["side"] == side
                    and mark_source_times.get(key, {}).get(side) == source_ns
                ):
                    changed.add(key)
        if not changed and all(action == "expire" for action, _, _ in events):
            continue
        for key in sorted(changed):
            balance = balances[key]
            mark = current_mark(key, balance["side"], timestamp)
            exposure = (
                balance["quantity"] * mark
                if balance["quantity"] > 0 and mark is not None
                else 0.0
                if balance["quantity"] == 0
                else None
            )
            market_equity.append(
                {
                    "market_id": key,
                    "variant": variant,
                    "ts_ns": timestamp,
                    "equity": balance["cash"] + exposure if exposure is not None else None,
                    "exposure": exposure,
                }
            )
        cash = initial_cash + sum(b["cash"] for b in balances.values())
        position_value = 0.0
        missing_mark = False
        for key, balance in balances.items():
            if balance["quantity"] <= 0:
                continue
            mark = current_mark(key, balance["side"], timestamp)
            if mark is None:
                missing_mark = True
            else:
                position_value += balance["quantity"] * mark
        equity.append(
            {
                "variant": variant,
                "ts_ns": timestamp,
                "cash": cash,
                "equity": None if missing_mark else cash + position_value,
                "open_positions": sum(b["quantity"] > 0 for b in balances.values()),
            }
        )
    return trades, equity, market_equity


def replay_variants(
    markets: list[dict],
    records_by_market: dict,
    instruments_by_market: dict,
    *,
    threshold=0.70,
    quantity=5,
    decision_latency_ms=25,
    order_latency_ms=85,
    max_entry_price=0.35,
    initial_cash=1000,
    max_positions=10,
    max_gap_ns=DEFAULT_MAX_GAP_NS,
    max_stale_ns=DEFAULT_MAX_STALE_NS,
    output_dir=None,
):
    """Run six independent shared-capital portfolios, never sum their equity.

    Inputs use market_id, start_ns, close_ns, source, explicit fee provenance,
    settlement_winner and settlement_ns. Resolution metadata is never placed in
    the engine instrument cache. Missing settlement remains unresolved (null PnL).
    Settlement payouts are postprocessed, not recycled into intrarun buying power.
    """
    for name, value in (
        ("quantity", quantity),
        ("initial_cash", initial_cash),
        ("threshold", threshold),
        ("max_entry_price", max_entry_price),
    ):
        if not isfinite(float(value)) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if threshold >= 1 or max_entry_price >= 1:
        raise ValueError("Probability prices must be below one")
    if not isinstance(max_positions, int) or max_positions < 1:
        raise ValueError("max_positions must be a positive integer")
    if any(not isinstance(value, int) or value < 0 for value in (max_gap_ns, max_stale_ns)):
        raise ValueError("Gap and staleness tolerances must be nonnegative integer nanoseconds")
    for value in (decision_latency_ms, order_latency_ms):
        if not isfinite(float(value)) or value < 0:
            raise ValueError("Latency must be finite and nonnegative")
    if not markets or len({m["market_id"] for m in markets}) != len(markets):
        raise ValueError("Unique nonempty markets are required")
    rates = {m["market_id"]: _fee_rate(m) for m in markets}
    records = []
    source_records = {}
    observations = {}
    clean_instruments = []
    for market in markets:
        key = market["market_id"]
        if market["start_ns"] >= market["close_ns"]:
            raise ValueError("Market start must precede close")
        pair_records = records_by_market[key]
        if set(pair_records) != {"UP", "DOWN"}:
            raise ValueError("Both executable outcome books are required")
        pair_books = {
            side: [record for record in rows if isinstance(record, OrderBookDeltas)]
            for side, rows in pair_records.items()
        }
        if not any(pair_books.values()):
            raise ValueError(
                "Book replay requires L2 deltas; standalone trade-tick replay is unsupported"
            )
        for side in ("UP", "DOWN"):
            instrument = instruments_by_market[key][side]
            if not isinstance(instrument, BinaryOption) or str(instrument.id.venue) != "POLYMARKET":
                raise ValueError("Polymarket BinaryOption instruments are required")
            payload = BinaryOption.to_dict(instrument)
            payload["info"] = {}
            payload["taker_fee"] = str(rates[key])
            clean_instruments.append(BinaryOption.from_dict(payload))
            for record in pair_records[side]:
                if (
                    not isinstance(record, OrderBookDeltas | TradeTick)
                    or record.instrument_id != instrument.id
                ):
                    raise ValueError(
                        "Replay requires instrument-matched OrderBookDeltas or integrated TradeTick"
                    )
                if record.ts_init < record.ts_event:
                    raise ValueError("Observable timestamps cannot precede exchange events")
                if isinstance(record, OrderBookDeltas):
                    source_records.setdefault(
                        (record.instrument_id, int(record.ts_init)), []
                    ).append(record)
                # Match and dispatch on one causal clock, even when vendor
                # exchange times precede availability. Raw inputs are unchanged
                # and remain the source for extraction and provenance hashes.
                records.append(_observable_record(record))
        observations[key] = list(observations_from_records(pair_books))
    if not records:
        raise ValueError("No replay data")
    result = {
        "trades": [],
        "equity": [],
        "market_equity": [],
        "variants": list(VARIANTS),
        "disclosures": [
            "Independent variants; each combines all markets with equal initial capital and limits.",
            "market_equity is zero-initial-capital PnL attribution, not independent funded accounts.",
            "IOC limits consume actual arrival-time L2 liquidity; no passive fills or complement quotes.",
            "Same-instrument integrated TradeTick records use native book/trade matching; signals remain L2-only.",
            "Target signals require remaining-quantity bid VWAP and sufficient depth at the sell floor.",
            "Freshness uses original source event times, not normalized receipt times.",
            "Engine book events use normalized observable ts_init; original source timestamps are retained in input records.",
            "Pending engine arrivals are processed on replay events; sparse cadence can increase effective latency.",
            "Settlement is postprocessed from actual fills and never visible before close.",
            "Settlement proceeds are not recycled into intrarun buying power (conservative constraint).",
            "Held positions retain execution position-limit slots after close and offline settlement until actually sold.",
            "Equity marks longs at observed bid; marks are not depth-sized liquidation guarantees.",
            "Only verified constant p*(1-p) quote-currency fees are supported; no share withholding.",
            "Final timestamp groups need a later replay event; timers never invent data beyond exhaustion.",
        ],
    }
    for number in (1, 2, 3):
        for target in (False, True):
            variant = f"touch{number}_{'target' if target else 'hold'}"
            engine = _engine(initial_cash, order_latency_ms)
            try:
                for instrument in clean_instruments:
                    engine.add_instrument(instrument)
                strategy = LongshotFlipStrategy(
                    LongshotFlipConfig(
                        instrument_ids=tuple(i.id for i in clean_instruments),
                        market_ids=tuple(m["market_id"] for m in markets),
                        start_ns=tuple(m["start_ns"] for m in markets),
                        close_ns=tuple(m["close_ns"] for m in markets),
                        touch_number=number,
                        target_exit=target,
                        threshold=threshold,
                        quantity=quantity,
                        decision_latency_ms=decision_latency_ms,
                        order_latency_ms=order_latency_ms,
                        max_entry_price=max_entry_price,
                        max_positions=max_positions,
                        initial_cash=initial_cash,
                        fee_rate=max(rates.values()),
                        max_gap_ns=max_gap_ns,
                        max_stale_ns=max_stale_ns,
                    ),
                    source_records=source_records,
                )
                engine.add_strategy(strategy)
                add_engine_data_by_type(engine, records)
                engine.run()
                trades, equity, market_equity = _report(
                    markets, observations, strategy, variant, initial_cash
                )
                result["trades"].extend(trades)
                result["equity"].extend(equity)
                result["market_equity"].extend(market_equity)
            finally:
                engine.dispose()
    if output_dir is not None:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "replay.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
