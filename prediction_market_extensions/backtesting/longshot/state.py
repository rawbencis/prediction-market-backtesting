"""Settlement-free, observable-time state shared by research and execution."""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain
from math import isfinite
from typing import Literal

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookType, RecordFlag

Side = Literal["UP", "DOWN"]
DEFAULT_MAX_GAP_NS = 60_000_000_000
DEFAULT_MAX_STALE_NS = 60_000_000_000


@dataclass(frozen=True)
class BookState:
    ts_ns: int
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()

    def __post_init__(self):
        if not isinstance(self.ts_ns, int) or self.ts_ns < 0:
            raise ValueError("ts_ns must be a nonnegative integer")
        for name, reverse in (("bids", True), ("asks", False)):
            levels = tuple((float(p), float(q)) for p, q in getattr(self, name))
            object.__setattr__(self, name, tuple(sorted(levels, reverse=reverse)))

    @property
    def invalid_reasons(self) -> tuple[str, ...]:
        reasons = []
        if not self.bids or not self.asks:
            reasons.append("empty_book")
        for levels in (self.bids, self.asks):
            if any(not isfinite(p) or not 0 <= p <= 1 for p, _ in levels):
                reasons.append("invalid_price")
            if any(not isfinite(q) or q <= 0 for _, q in levels):
                reasons.append("invalid_size")
            if len({p for p, _ in levels}) != len(levels):
                reasons.append("duplicate_price")
        if self.bids and self.asks and self.bids[0][0] >= self.asks[0][0]:
            reasons.append("crossed_or_locked_book")
        return tuple(dict.fromkeys(reasons))

    @property
    def valid(self) -> bool:
        return not self.invalid_reasons

    def is_stale(self, ts_ns: int, max_stale_ns: int) -> bool:
        return ts_ns < self.ts_ns or ts_ns - self.ts_ns > max_stale_ns

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.valid else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.valid else None

    @property
    def spread(self) -> float | None:
        return self.asks[0][0] - self.bids[0][0] if self.valid else None

    @property
    def midpoint(self) -> float | None:
        return (self.asks[0][0] + self.bids[0][0]) / 2 if self.valid else None

    def depth(self, side: str) -> float:
        if side not in ("bid", "ask"):
            raise ValueError("side must be bid or ask")
        return sum(q for _, q in (self.bids if side == "bid" else self.asks))

    def depth_at_price(self, side: str, price: float) -> float:
        """Displayed quantity at or better than an executable price floor/cap."""
        if side not in ("bid", "ask") or not isfinite(price):
            raise ValueError("Expected bid/ask and a finite price")
        if not self.valid:
            return 0.0
        levels = self.bids if side == "bid" else self.asks
        return sum(q for p, q in levels if (p >= price if side == "bid" else p <= price))

    def vwap(self, side: str, quantity: float) -> float | None:
        if not isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        depth = self.depth(side)
        if not self.valid or depth < quantity:
            return None
        remaining, value = quantity, 0.0
        for price, size in self.bids if side == "bid" else self.asks:
            taken = min(size, remaining)
            value += price * taken
            remaining -= taken
            if remaining <= 0:
                break
        return value / quantity


@dataclass(frozen=True)
class PairObservation:
    ts_ns: int
    up: BookState
    down: BookState
    gap: bool = False

    def book(self, side: Side) -> BookState:
        if side not in ("UP", "DOWN"):
            raise ValueError("Unknown outcome")
        return self.up if side == "UP" else self.down


@dataclass(frozen=True)
class Touch:
    number: int
    side: Side
    initial_favorite: Side
    ts_ns: int

    @property
    def cheap_side(self) -> Side:
        return "DOWN" if self.side == "UP" else "UP"


class TouchState:
    """Count alternating descriptive midpoint crossings, never inferred ones."""

    def __init__(
        self,
        threshold: float = 0.70,
        start_ns: int = 0,
        close_ns: int = 2**63 - 1,
        max_gap_ns: int = DEFAULT_MAX_GAP_NS,
        max_stale_ns: int = DEFAULT_MAX_STALE_NS,
    ):
        if not isfinite(threshold) or not 0.5 < threshold < 1:
            raise ValueError("threshold must be between 0.5 and 1")
        if start_ns < 0 or close_ns <= start_ns or min(max_gap_ns, max_stale_ns) < 0:
            raise ValueError("invalid observation window or tolerances")
        self.threshold = threshold
        self.start_ns, self.close_ns = start_ns, close_ns
        self.max_gap_ns, self.max_stale_ns = max_gap_ns, max_stale_ns
        self.censored = False
        self.left_truncated = False
        self.reasons: list[str] = []
        self.initial_favorite: Side | None = None
        self.last_side: Side | None = None
        self.number = 0
        self.last_ts_ns: int | None = None
        self._above: dict[Side, bool] | None = None

    def _censor(self, reason: str):
        self.censored = True
        if reason not in self.reasons:
            self.reasons.append(reason)

    def observe(self, obs: PairObservation) -> Touch | None:
        if not self.start_ns <= obs.ts_ns < self.close_ns:
            return None
        if obs.gap:
            self._censor("explicit_gap")
        if self.last_ts_ns is not None:
            if obs.ts_ns <= self.last_ts_ns:
                self._censor("non_atomic_or_nonmonotonic_timestamp")
            elif obs.ts_ns - self.last_ts_ns > self.max_gap_ns:
                self._censor("observation_gap")
        elif obs.ts_ns - self.start_ns > self.max_gap_ns:
            self._censor("missing_start_coverage")
        self.last_ts_ns = obs.ts_ns
        for side in ("UP", "DOWN"):
            book = obs.book(side)
            for reason in book.invalid_reasons:
                self._censor(f"{side}:{reason}")
            if book.is_stale(obs.ts_ns, self.max_stale_ns):
                self._censor(f"{side}:stale_book")
        if self.censored:
            return None
        above = {side: obs.book(side).midpoint >= self.threshold for side in ("UP", "DOWN")}
        if all(above.values()):
            self._censor("ambiguous_simultaneous_favorites")
            return None
        if self._above is None:
            self._above = above
            if any(above.values()):
                self.left_truncated = True
                self.initial_favorite = next(side for side in above if above[side])
                self._censor("left_truncated")
            return None
        crossing = next((s for s in above if above[s] and not self._above[s]), None)
        self._above = above
        if crossing is None or crossing == self.last_side:
            return None
        self.number += 1
        self.last_side = crossing
        if self.initial_favorite is None:
            self.initial_favorite = crossing
        return Touch(self.number, crossing, self.initial_favorite, obs.ts_ns)


class PairedBookStream:
    """Buffer one ts_init group; update emits the previous complete atomic group."""

    def __init__(self):
        self._books: dict[Side, OrderBook] = {}
        self._times: dict[Side, int] = {}
        self._pending: list[tuple[Side, OrderBookDeltas]] = []
        self._ts: int | None = None

    def update(self, side: Side, record: OrderBookDeltas) -> PairObservation | None:
        if side not in ("UP", "DOWN"):
            raise ValueError("Unknown outcome")
        ts = int(record.ts_init)
        if self._ts is not None and ts < self._ts:
            raise ValueError("Records must be ordered by observable ts_init")
        result = self.flush() if self._ts is not None and ts > self._ts else None
        self._ts = ts
        self._pending.append((side, record))
        return result

    def flush(self) -> PairObservation | None:
        if not self._pending:
            return None
        ts = self._ts
        seen: set[Side] = set()
        gap = False
        for side, record in self._pending:
            gap |= side in seen
            gap |= not bool(record.deltas[-1].flags & RecordFlag.F_LAST)
            seen.add(side)
            book = self._books.get(side)
            if book is None:
                if any(b.instrument_id == record.instrument_id for b in self._books.values()):
                    raise ValueError("UP and DOWN must map to distinct instruments")
                book = OrderBook(record.instrument_id, BookType.L2_MBP)
                self._books[side] = book
            elif book.instrument_id != record.instrument_id:
                raise ValueError("Outcome mapped to multiple instruments")
            book.apply_deltas(record)
            self._times[side] = int(record.ts_event)
        snapshots = {}
        for side in ("UP", "DOWN"):
            book = self._books.get(side)
            snapshots[side] = BookState(
                self._times.get(side, ts),
                tuple((float(level.price), level.size()) for level in book.bids()) if book else (),
                tuple((float(level.price), level.size()) for level in book.asks()) if book else (),
            )
        self._pending.clear()
        self._ts = None
        return PairObservation(ts, snapshots["UP"], snapshots["DOWN"], gap)


def observations_from_records(
    records_by_side: dict[str, Sequence[OrderBookDeltas]],
) -> list[PairObservation]:
    if set(records_by_side) != {"UP", "DOWN"}:
        raise ValueError("An explicit UP/DOWN outcome mapping is required")
    stream = PairedBookStream()
    observations = []
    records = sorted(
        chain.from_iterable(
            ((side, record) for record in records_by_side[side]) for side in ("UP", "DOWN")
        ),
        key=lambda item: (item[1].ts_init, item[0]),
    )
    for side, record in records:
        obs = stream.update(side, record)
        if obs is not None:
            observations.append(obs)
    obs = stream.flush()
    if obs is not None:
        observations.append(obs)
    return observations
