from dataclasses import FrozenInstanceError

import pytest
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from prediction_market_extensions.backtesting.longshot.state import (
    BookState,
    PairedBookStream,
    PairObservation,
    TouchState,
    observations_from_records,
)


def book(ts, bid, size=10):
    return BookState(ts, ((bid, size),), ((bid + 0.01, size),))


def obs(ts, up=0.5, down=0.5, gap=False):
    return PairObservation(ts, book(ts, up), book(ts, down), gap)


def record(side, ts, bid, event_ts=1):
    instrument = InstrumentId.from_str(f"{side}.POLYMARKET")
    deltas = [OrderBookDelta(instrument, BookAction.CLEAR, None, 0, 0, event_ts, ts)]
    for number, (order_side, price) in enumerate(
        ((OrderSide.BUY, bid), (OrderSide.SELL, bid + 0.01)), 1
    ):
        order = BookOrder(order_side, Price(price, 2), Quantity(10, 2), number)
        deltas.append(OrderBookDelta(instrument, BookAction.ADD, order, 128, 0, event_ts, ts))
    return OrderBookDeltas(instrument, deltas)


def test_immutable_book_depth_and_quantity_vwap():
    bids = [[0.4, 2], [0.5, 3]]
    state = BookState(1, bids, ((0.6, 2), (0.7, 3)))
    bids[0][0] = 0.9
    assert state.bids == ((0.5, 3), (0.4, 2))
    assert state.depth("bid") == 5
    assert state.vwap("bid", 5) == pytest.approx(0.46)
    assert state.vwap("ask", 5) == pytest.approx(0.66)
    assert state.vwap("ask", 6) is None
    assert state.spread == pytest.approx(0.1)
    assert state.midpoint == pytest.approx(0.55)
    assert not state.is_stale(3, 2)
    assert state.is_stale(4, 2)
    assert state.is_stale(0, 2)
    with pytest.raises(FrozenInstanceError):
        state.ts_ns = 2
    with pytest.raises(ValueError):
        state.vwap("ask", 0)
    with pytest.raises(ValueError):
        state.depth("UP")


@pytest.mark.parametrize(
    "bids,asks,reason",
    [
        ((), ((0.6, 1),), "empty_book"),
        (((0.5, 0),), ((0.6, 1),), "invalid_size"),
        (((float("nan"), 1),), ((0.6, 1),), "invalid_price"),
        (((0.5, 1),), ((1.1, 1),), "invalid_price"),
        (((0.6, 1),), ((0.6, 1),), "crossed_or_locked_book"),
        (((0.7, 1),), ((0.6, 1),), "crossed_or_locked_book"),
        (((0.5, 1), (0.5, 2)), ((0.6, 1),), "duplicate_price"),
    ],
)
def test_invalid_books(bids, asks, reason):
    state = BookState(1, bids, asks)
    assert not state.valid
    assert reason in state.invalid_reasons
    assert state.vwap("bid", 1) is None


def test_crossings_equality_repetition_and_alternation():
    state = TouchState(start_ns=0, close_ns=10)
    assert state.observe(obs(0)) is None
    first = state.observe(obs(1, 0.7, 0.2))
    assert (first.number, first.side, first.initial_favorite, first.cheap_side) == (
        1,
        "UP",
        "UP",
        "DOWN",
    )
    assert state.observe(obs(2, 0.8, 0.1)) is None
    assert state.observe(obs(3)) is None
    assert state.observe(obs(4, 0.7, 0.2)) is None
    second = state.observe(obs(5, 0.2, 0.7))
    assert (second.number, second.side, second.initial_favorite) == (2, "DOWN", "UP")
    third = state.observe(obs(6, 0.7, 0.2))
    assert third.number == 3
    assert state.observe(obs(11, 0.2, 0.7)) is None


@pytest.mark.parametrize("side", ["UP", "DOWN"])
def test_first_above_is_left_truncated_never_restarts_count(side):
    state = TouchState()
    assert state.observe(obs(0, *((0.7, 0.2) if side == "UP" else (0.2, 0.7)))) is None
    assert state.left_truncated and state.censored
    assert state.observe(obs(1)) is None
    assert state.observe(obs(2, 0.7, 0.2)) is None
    assert state.number == 0


@pytest.mark.parametrize(
    "bad,reason",
    [
        (obs(2, gap=True), "explicit_gap"),
        (obs(20), "observation_gap"),
        (PairObservation(2, BookState(2), book(2, 0.2)), "UP:empty_book"),
        (PairObservation(3, book(0, 0.5), book(3, 0.2)), "UP:stale_book"),
        (obs(2, 0.7, 0.7), "ambiguous_simultaneous_favorites"),
        (obs(0), "non_atomic_or_nonmonotonic_timestamp"),
    ],
)
def test_bad_observations_permanently_censor(bad, reason):
    state = TouchState(max_gap_ns=5, max_stale_ns=2)
    state.observe(obs(0))
    state.observe(bad)
    assert state.censored
    assert reason in state.reasons
    assert state.observe(obs(21, 0.7, 0.2)) is None
    assert state.number == 0


def test_missing_start_coverage_and_parameter_validation():
    state = TouchState(start_ns=10, max_gap_ns=2)
    state.observe(obs(13))
    assert "missing_start_coverage" in state.reasons
    for kwargs in ({"threshold": 0.5}, {"close_ns": 0}, {"max_gap_ns": -1}):
        with pytest.raises(ValueError):
            TouchState(**kwargs)


def test_record_pairing_observable_time_atomicity_and_stream_equivalence():
    records = {
        "UP": [record("UP", 2, 0.7, event_ts=0), record("UP", 1, 0.5)],
        "DOWN": [record("DOWN", 1, 0.5), record("DOWN", 2, 0.2)],
    }
    paired = observations_from_records(records)
    assert [o.ts_ns for o in paired] == [1, 2]
    assert all(o.up.valid and o.down.valid and not o.gap for o in paired)
    assert paired[1].up.best_bid == 0.7
    assert paired[1].down.best_bid == 0.2
    assert paired[1].up.ts_ns == 0
    assert paired[1].down.ts_ns == 1
    stream = PairedBookStream()
    assert stream.update("DOWN", records["DOWN"][0]) is None
    assert stream.update("UP", records["UP"][1]) is None
    assert stream.update("DOWN", records["DOWN"][1]) == paired[0]
    assert stream.update("UP", records["UP"][0]) is None
    assert stream.flush() == paired[1]
    assert stream.flush() is None


def test_duplicate_same_outcome_same_timestamp_and_mapping():
    records = {
        "UP": [record("UP", 1, 0.5), record("UP", 1, 0.7)],
        "DOWN": [record("DOWN", 1, 0.2)],
    }
    paired = observations_from_records(records)
    assert len(paired) == 1 and paired[0].gap
    with pytest.raises(ValueError):
        observations_from_records({"YES": []})
    with pytest.raises(ValueError):
        observations_from_records({"UP": [record("UP", 1, 0.5)], "DOWN": [record("UP", 1, 0.5)]})
    stream = PairedBookStream()
    stream.update("UP", record("UP", 2, 0.5))
    with pytest.raises(ValueError):
        stream.update("DOWN", record("DOWN", 1, 0.5))


def test_empty_records_and_delayed_exchange_event_ages():
    assert observations_from_records({"UP": [], "DOWN": []}) == []
    paired = observations_from_records(
        {
            "UP": [record("UP", 1, 0.5, event_ts=1)],
            "DOWN": [record("DOWN", 1, 0.5, event_ts=1), record("DOWN", 5, 0.4, event_ts=5)],
        }
    )
    assert paired[-1].ts_ns == 5
    assert paired[-1].up.ts_ns == 1 and paired[-1].down.ts_ns == 5
    state = TouchState(max_stale_ns=3)
    state.observe(paired[0])
    state.observe(paired[1])
    assert "UP:stale_book" in state.reasons
    delayed = observations_from_records(
        {"UP": [record("UP", 10, 0.5, event_ts=1)], "DOWN": [record("DOWN", 10, 0.5, event_ts=10)]}
    )
    state = TouchState(max_stale_ns=3)
    state.observe(delayed[0])
    assert "UP:stale_book" in state.reasons


def test_midpoint_crossing_is_descriptive_not_executable_bid_target():
    state = TouchState()
    state.observe(obs(0))
    observation = PairObservation(
        1,
        BookState(1, ((0.69, 10),), ((0.71, 10),)),
        book(1, 0.2),
    )
    touch = state.observe(observation)
    assert observation.up.best_bid < state.threshold
    assert observation.up.midpoint == state.threshold
    assert touch is not None and touch.side == "UP"


@pytest.mark.parametrize("prior_touches", [1, 2])
def test_exact_close_crossing_is_not_a_touch(prior_touches):
    state = TouchState(start_ns=0, close_ns=10)
    state.observe(obs(0))
    assert state.observe(obs(1, 0.7, 0.2)).number == 1
    if prior_touches == 2:
        assert state.observe(obs(3, 0.2, 0.7)).number == 2
        assert state.observe(obs(10, 0.7, 0.2)) is None
    else:
        assert state.observe(obs(10, 0.2, 0.7)) is None
    assert state.number == prior_touches
    assert state.last_side == ("UP" if prior_touches == 1 else "DOWN")
