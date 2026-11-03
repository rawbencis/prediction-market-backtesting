"""Causal paired-L2 touch strategies; all orders are price-protected taker IOCs."""

from __future__ import annotations

from collections import deque
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import partial
from math import isfinite

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from prediction_market_extensions.backtesting.longshot.state import (
    DEFAULT_MAX_GAP_NS,
    DEFAULT_MAX_STALE_NS,
    BookState,
    PairedBookStream,
    TouchState,
)


class LongshotFlipConfig(StrategyConfig, frozen=True):
    instrument_ids: tuple[InstrumentId, ...]
    market_ids: tuple[str, ...]
    start_ns: tuple[int, ...]
    close_ns: tuple[int, ...]
    touch_number: int = 1
    target_exit: bool = False
    threshold: float = 0.70
    quantity: float = 5.0
    decision_latency_ms: float = 25.0
    order_latency_ms: float = 85.0
    max_entry_price: float = 0.35
    max_positions: int = 10
    initial_cash: float = 1000.0
    fee_rate: float = 0.02
    max_gap_ns: int = DEFAULT_MAX_GAP_NS
    max_stale_ns: int = DEFAULT_MAX_STALE_NS

    def __post_init__(self):
        count = len(self.market_ids)
        if (
            not count
            or len(set(self.market_ids)) != count
            or len(self.instrument_ids) != count * 2
            or len(set(self.instrument_ids)) != count * 2
            or len(self.start_ns) != count
            or len(self.close_ns) != count
        ):
            raise ValueError("Distinct outcome pairs and matching market windows are required")
        if self.touch_number not in (1, 2, 3):
            raise ValueError("touch_number must be 1, 2 or 3")
        if not 0.5 < self.threshold < 1 or not 0 < self.max_entry_price < 1:
            raise ValueError("Invalid threshold or entry probability price")
        if self.max_positions < 1:
            raise ValueError("max_positions must be positive")
        for name in ("quantity", "initial_cash"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("decision_latency_ms", "order_latency_ms", "fee_rate"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")


class LongshotFlipStrategy(Strategy):
    """One portfolio per variant, one entry attempt per market, no outcome metadata."""

    def __init__(self, config: LongshotFlipConfig, source_records=None):
        super().__init__(config)
        self.books = {}
        self.updated = {}
        self.pending_groups = set()
        self.group_ts = {}
        self.states = {}
        self.streams = {}
        self.pairs = {}
        self.by_instrument = {}
        self.attempted = set()
        self.pending_entries = set()
        self.pending_exits = set()
        self.selected = {}
        self.remaining = {}
        self.orders = {}
        self.fills = []
        self.signals = {}
        self.attempts = []
        self.cash = config.initial_cash
        self.reserved = {}
        self.source_records = (
            {key: deque(records) for key, records in source_records.items()}
            if source_records is not None
            else None
        )

    def on_start(self):
        for index, market_id in enumerate(self.config.market_ids):
            pair = self.config.instrument_ids[index * 2 : index * 2 + 2]
            self.pairs[market_id] = dict(zip(("UP", "DOWN"), pair, strict=True))
            self.streams[market_id] = PairedBookStream()
            self.states[market_id] = TouchState(
                threshold=self.config.threshold,
                start_ns=self.config.start_ns[index],
                close_ns=self.config.close_ns[index],
                max_gap_ns=self.config.max_gap_ns,
                max_stale_ns=self.config.max_stale_ns,
            )
            for instrument_id in pair:
                self.by_instrument[instrument_id] = market_id
                self.books[instrument_id] = OrderBook(instrument_id, BookType.L2_MBP)
                self.subscribe_order_book_deltas(instrument_id, book_type=BookType.L2_MBP)

    def on_order_book_deltas(self, deltas):
        instrument_id = deltas.instrument_id
        self.books[instrument_id].apply_deltas(deltas)
        timestamp = int(deltas.ts_init)
        source = deltas
        if self.source_records is not None:
            source = self.source_records[(instrument_id, timestamp)].popleft()
        self.updated[instrument_id] = int(source.ts_event)
        market_id = self.by_instrument[instrument_id]
        side = next(side for side, iid in self.pairs[market_id].items() if iid == instrument_id)
        previous = self.streams[market_id].update(side, source)
        self.group_ts[market_id] = timestamp
        if previous is not None:
            self.pending_groups.discard((market_id, previous.ts_ns))
            self._evaluate_observation(market_id, previous)
        group = (market_id, timestamp)
        if group not in self.pending_groups:
            self.pending_groups.add(group)
            # A nanosecond fence guarantees both outcomes and every batch at this
            # observable timestamp are applied before producing a signal.
            self.clock.set_time_alert_ns(
                f"group-{market_id}-{timestamp}",
                timestamp + 1,
                partial(self._observe, market_id, timestamp),
            )

    def _observe(self, market_id, timestamp, event):
        self.pending_groups.discard((market_id, timestamp))
        if self.group_ts.get(market_id) != timestamp:
            return
        observation = self.streams[market_id].flush()
        if observation is None:
            return
        self._evaluate_observation(market_id, observation)

    def _evaluate_observation(self, market_id, observation):
        touch = self.states[market_id].observe(observation)
        if touch is not None and touch.number == self.config.touch_number:
            self.signals[market_id] = {
                "signal_ts_ns": touch.ts_ns,
                "touch_number": touch.number,
                "initial_favorite": touch.initial_favorite,
                "side": touch.cheap_side,
            }
            if market_id not in self.attempted:
                self.attempted.add(market_id)
                self.selected[market_id] = touch.cheap_side
                self._delay(market_id, "entry", self._enter)
        if (
            self.config.target_exit
            and not self.states[market_id].censored
            and self.remaining.get(market_id, 0) > 0
        ):
            side = self.selected[market_id]
            book = observation.book(side)
            quantity = self.remaining[market_id]
            bid_vwap = book.vwap("bid", quantity)
            target_depth = sum(size for price, size in book.bids if price >= self.config.threshold)
            if (
                bid_vwap is not None
                and bid_vwap >= self.config.threshold
                and target_depth >= quantity
                and market_id not in self.pending_exits
            ):
                self.pending_exits.add(market_id)
                self._delay(market_id, "exit", self._exit)

    def _delay(self, market_id, action, callback):
        timestamp = self.clock.timestamp_ns()
        delay = max(1, round(self.config.decision_latency_ms * 1_000_000))
        self.clock.set_time_alert_ns(
            f"{action}-{market_id}-{timestamp}",
            timestamp + delay,
            partial(callback, market_id),
        )

    def _open(self, market_id):
        index = self.config.market_ids.index(market_id)
        return self.clock.timestamp_ns() < self.config.close_ns[index]

    def _can_arrive(self, market_id):
        index = self.config.market_ids.index(market_id)
        return (
            self.clock.timestamp_ns() + round(self.config.order_latency_ms * 1_000_000)
            < self.config.close_ns[index]
        )

    def _decision_books_valid(self, market_id):
        if self.states[market_id].censored:
            return False
        for instrument_id in self.pairs[market_id].values():
            book = self.books[instrument_id]
            snapshot = BookState(
                self.updated.get(instrument_id, 0),
                tuple((float(level.price), level.size()) for level in book.bids()),
                tuple((float(level.price), level.size()) for level in book.asks()),
            )
            if not snapshot.valid or snapshot.is_stale(
                self.clock.timestamp_ns(), self.config.max_stale_ns
            ):
                return False
        return True

    def _enter(self, market_id, event):
        side = self.selected[market_id]
        instrument_id = self.pairs[market_id][side]
        book = self.books[instrument_id]
        ask = book.best_ask_price()
        active = sum(
            1
            for key in self.config.market_ids
            if self.remaining.get(key, 0) > 0 or key in self.pending_entries
        )
        reservation = self.config.quantity * (
            self.config.max_entry_price + self.config.fee_rate / 4
        )
        reason = None
        if not self._can_arrive(market_id):
            reason = "closed"
        elif not self._decision_books_valid(market_id):
            reason = "invalid_or_stale_book"
        elif ask is None or float(ask) > self.config.max_entry_price:
            reason = "price_cap"
        elif active >= self.config.max_positions:
            reason = "position_limit"
        elif self.cash - sum(self.reserved.values()) < reservation:
            reason = "capital_limit"
        if reason:
            self.attempts.append({"market_id": market_id, "action": "buy", "status": reason})
            return
        self.pending_entries.add(market_id)
        self.reserved[market_id] = reservation
        self._submit(market_id, OrderSide.BUY, self.config.quantity, self.config.max_entry_price)

    def _exit(self, market_id, event):
        quantity = self.remaining.get(market_id, 0)
        if quantity <= 0 or not self._can_arrive(market_id):
            self.pending_exits.discard(market_id)
            return
        if not self._decision_books_valid(market_id):
            self.pending_exits.discard(market_id)
            self.attempts.append(
                {"market_id": market_id, "action": "sell", "status": "invalid_or_stale_book"}
            )
            return
        # Keep the target floor even when the bid disappeared during latency.
        # The exchange, not the decision-time quote, determines the actual fill.
        self._submit(market_id, OrderSide.SELL, quantity, self.config.threshold)

    def _submit(self, market_id, side, quantity, price):
        instrument_id = self.pairs[market_id][self.selected[market_id]]
        instrument = self.cache.instrument(instrument_id)
        tick = Decimal(str(instrument.price_increment))
        rounding = ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
        protected_price = (Decimal(str(price)) / tick).to_integral_value(rounding=rounding) * tick
        submitted_quantity = instrument.make_qty(quantity, round_down=True)
        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=side,
            quantity=submitted_quantity,
            price=instrument.make_price(float(protected_price)),
            time_in_force=TimeInForce.IOC,
        )
        self.orders[order.client_order_id] = (market_id, side)
        self.attempts.append(
            {
                "market_id": market_id,
                "action": "buy" if side == OrderSide.BUY else "sell",
                "status": "submitted",
                "submit_ts_ns": self.clock.timestamp_ns(),
                "client_order_id": str(order.client_order_id),
                "limit_price": float(order.price),
                "quantity": float(submitted_quantity),
            }
        )
        self.submit_order(order)
        latency_ns = round(self.config.order_latency_ms * 1_000_000)
        if latency_ns > 0:
            # Nautilus drains in-flight commands on clock/data events. Wake it
            # at arrival, before a later book update can affect this order.
            self.clock.set_time_alert_ns(
                f"arrival-{order.client_order_id}",
                self.clock.timestamp_ns() + latency_ns,
                self._arrival_clock,
            )

    def _arrival_clock(self, event):
        pass

    def on_order_filled(self, event):
        market_id, side = self.orders[event.client_order_id]
        quantity = float(event.last_qty)
        price = float(event.last_px)
        commission = float(event.commission.as_double())
        sign = 1 if side == OrderSide.BUY else -1
        self.remaining[market_id] = self.remaining.get(market_id, 0) + sign * quantity
        self.cash -= sign * quantity * price + commission
        self.fills.append(
            {
                "market_id": market_id,
                "action": "buy" if sign == 1 else "sell",
                "side": self.selected[market_id],
                "quantity": quantity,
                "price": price,
                "commission": commission,
                "ts_ns": int(event.ts_event),
                "liquidity": str(event.liquidity_side),
                "client_order_id": str(event.client_order_id),
            }
        )
        order = self.cache.order(event.client_order_id)
        if order is not None and order.is_closed:
            self._finished(event.client_order_id)

    def _finished(self, order_id):
        market_id, side = self.orders[order_id]
        if side == OrderSide.BUY:
            self.pending_entries.discard(market_id)
            self.reserved.pop(market_id, None)
        else:
            self.pending_exits.discard(market_id)

    def on_order_canceled(self, event):
        self._finished(event.client_order_id)

    def on_order_rejected(self, event):
        self._finished(event.client_order_id)

    def on_order_denied(self, event):
        self._finished(event.client_order_id)
