from copy import deepcopy

import pytest
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Quantity

import prediction_market_extensions.backtesting.longshot.replay as replay_module
from prediction_market_extensions.backtesting.longshot.replay import VARIANTS, replay_variants
from prediction_market_extensions.backtesting.longshot.synthetic import (
    SECOND,
    snapshot,
    synthetic_fixture,
)


@pytest.fixture(scope="module")
def replay():
    return replay_variants(*synthetic_fixture())


def row(result, market, variant):
    return next(
        trade
        for trade in result["trades"]
        if trade["market_id"] == market and trade["variant"] == variant
    )


def test_preopen_anchor_is_reported_at_market_open_not_before_it():
    markets, records, instruments = synthetic_fixture()
    market = markets[0]
    key = market["market_id"]
    anchor_ns = market["start_ns"] - SECOND
    for side in ("UP", "DOWN"):
        records[key][side].insert(0, snapshot(instruments[key][side], anchor_ns, 0.40, 0.42))
    result = replay_variants([market], records, instruments)
    points = [point for point in result["market_equity"] if point["market_id"] == key]
    assert points
    assert all(point["ts_ns"] >= market["start_ns"] for point in points)
    assert any(point["ts_ns"] == market["start_ns"] for point in points)


def test_native_causal_ingestion_keeps_trade_before_same_frame_book(monkeypatch):
    markets, records, instruments = synthetic_fixture()
    market = markets[0]
    key = market["market_id"]
    market.update(
        source="sqlite_capture",
        fee_verified=True,
        fee_source="synthetic-test-contract",
        fee_valid_from_ns=market["start_ns"],
        fee_valid_to_ns=market["close_ns"],
        capture_record_order={
            side: [[index, event] for index in range(len(records[key][side]))]
            for side, event in (("DOWN", 1), ("UP", 2))
        },
    )
    instrument = instruments[key]["DOWN"]
    timestamp = records[key]["DOWN"][1].ts_init
    tick = TradeTick(
        instrument.id,
        instrument.make_price(0.30),
        Quantity.from_int(1),
        AggressorSide.BUYER,
        TradeId("causal-fixture"),
        timestamp,
        timestamp,
    )
    records[key]["DOWN"].insert(1, tick)
    market["capture_record_order"]["DOWN"].insert(1, [1, 0])
    original_add = replay_module._add_causal_engine_data
    observed = []

    def check_order(engine, rows):
        observed.append(rows)
        assert rows[0].instrument_id == instrument.id
        same_frame = [record for record in rows if record.ts_init == timestamp]
        assert isinstance(same_frame[0], TradeTick)
        assert same_frame[1].instrument_id == instrument.id
        original_add(engine, rows)

    monkeypatch.setattr(replay_module, "_add_causal_engine_data", check_order)
    replay_variants([market], records, instruments)
    assert len(observed) == 6
    market["capture_record_order"]["DOWN"].pop()
    with pytest.raises(ValueError, match="aligned sequence/event"):
        replay_variants([market], records, instruments)


def test_causal_engine_batches_preserve_interleaved_types():
    class Engine:
        def __init__(self):
            self.rows = []
            self.batches = []

        def add_data(self, rows, *, sort):
            assert sort is False
            assert len({type(row) for row in rows}) == 1
            self.rows.extend(rows)
            self.batches.append(rows)

        def sort_data(self):
            self.rows.sort(key=lambda record: record.ts_init)

    markets, records, instruments = synthetic_fixture()
    key = markets[0]["market_id"]
    book = records[key]["UP"][0]
    instrument = instruments[key]["UP"]
    tick = TradeTick(
        instrument.id,
        instrument.make_price(0.30),
        Quantity.from_int(1),
        AggressorSide.BUYER,
        TradeId("interleaved"),
        book.ts_init,
        book.ts_init,
    )
    engine = Engine()
    replay_module._add_causal_engine_data(engine, [tick, book, tick])
    assert engine.rows == [tick, book, tick]
    assert len(engine.batches) == 3


def test_real_nautilus_types_and_all_six_independent_portfolios(replay):
    markets, records, instruments = synthetic_fixture()
    assert len({market["start_ns"] // (86_400 * SECOND) for market in markets}) == 3
    assert all(
        market["settlement_ns"] > market["close_ns"]
        for market in markets
        if market["settlement_ns"] is not None
    )
    assert all(market["underlying_available"] is False for market in markets)
    assert all(isinstance(i, BinaryOption) for pair in instruments.values() for i in pair.values())
    assert all(
        isinstance(r, OrderBookDeltas)
        for pair in records.values()
        for outcome in pair.values()
        for r in outcome
    )
    assert len(replay["trades"]) == len(markets) * 6
    assert set(replay["variants"]) == set(VARIANTS)
    for variant in VARIANTS:
        curve = [point for point in replay["equity"] if point["variant"] == variant]
        assert curve[0]["equity"] == 1000
        assert [p["ts_ns"] for p in curve] == sorted(p["ts_ns"] for p in curve)
        assert not row(replay, "no_touch", variant)["entered"]


def test_touch1_touch2_touch3_require_actual_alternation(replay):
    assert row(replay, "one_touch_loss", "touch1_hold")["entered"]
    assert not row(replay, "one_touch_loss", "touch2_hold")["entered"]
    assert row(replay, "one_flip", "touch2_hold")["entered"]
    assert not row(replay, "one_flip", "touch3_hold")["entered"]
    assert row(replay, "two_flips", "touch3_hold")["entered"]
    for variant in VARIANTS:
        for trade in [t for t in replay["trades"] if t["variant"] == variant]:
            assert (
                sum(a["action"] == "buy" and a["status"] == "submitted" for a in trade["attempts"])
                <= 1
            )


def test_entry_touch_uses_shared_descriptive_midpoint_not_best_bid():
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    timestamp = market["start_ns"] + SECOND
    records[key]["UP"][1] = snapshot(instruments[key]["UP"], timestamp, 0.69, 0.73)
    trade = row(replay_variants([market], records, instruments), key, "touch1_hold")
    assert trade["entered"]
    assert trade["signal_ts_ns"] == timestamp


def test_market_attribution_reconciles_without_duplicating_portfolio_capital(replay):
    for variant in VARIANTS:
        points = {}
        for point in replay["market_equity"]:
            if point["variant"] == variant:
                points.setdefault(point["ts_ns"], []).append(point)
        latest = {}
        for account in replay["equity"]:
            if account["variant"] != variant:
                continue
            for point in points.get(account["ts_ns"], []):
                latest[point["market_id"]] = point["equity"]
            assert account["equity"] == pytest.approx(1000 + sum(latest.values()))


def test_orders_fill_after_decision_and_exchange_latency_at_actual_books(replay):
    trade = row(replay, "one_touch_loss", "touch1_hold")
    buy = trade["fills"][0]
    attempt = trade["attempts"][0]
    assert attempt["submit_ts_ns"] - trade["signal_ts_ns"] == 25_000_001
    assert buy["ts_ns"] - attempt["submit_ts_ns"] >= 85_000_000
    assert buy["price"] == pytest.approx(0.28)
    assert attempt["limit_price"] == 0.35
    repriced = row(replay, "arrival_reprice", "touch1_hold")
    assert repriced["entry_attempted"]
    assert repriced["attempts"][0]["status"] == "submitted"
    assert repriced["entry_quantity"] == 0


def test_integrated_native_trade_ticks_are_accepted_without_entering_touch_state(monkeypatch):
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    baseline = row(replay_variants([market], records, instruments), key, "touch1_hold")
    for index, side in enumerate(("UP", "DOWN")):
        instrument = instruments[key][side]
        timestamp = market["start_ns"] + SECOND + 120_000_000 + index * 10_000_000
        records[key][side].append(
            TradeTick(
                instrument.id,
                instrument.make_price(0.74 if side == "UP" else 0.28),
                Quantity.from_int(1),
                AggressorSide.BUYER,
                TradeId(f"integrated-{side}"),
                timestamp - 10_000_000,
                timestamp,
            )
        )
        records[key][side].sort(key=lambda record: record.ts_init)
    original_add = replay_module.add_engine_data_by_type
    tick_batches = []

    def add_mixed_native_data(engine, engine_records):
        ticks = [record for record in engine_records if isinstance(record, TradeTick)]
        assert len(ticks) == 2
        assert all(tick.ts_event == tick.ts_init for tick in ticks)
        tick_batches.append(len(ticks))
        original_add(engine, engine_records)

    monkeypatch.setattr(replay_module, "add_engine_data_by_type", add_mixed_native_data)
    trade = row(replay_variants([market], records, instruments), key, "touch1_hold")
    assert len(tick_batches) == 6
    assert trade["signal_ts_ns"] == baseline["signal_ts_ns"]
    assert trade["entry_quantity"] == 5
    assert trade["entry_price"] == pytest.approx(0.28)
    assert trade["pnl"] == pytest.approx(baseline["pnl"])


def test_foreign_instrument_ticks_and_standalone_tick_replay_fail_closed():
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    instrument = instruments[key]["UP"]
    timestamp = market["start_ns"] + SECOND
    tick = TradeTick(
        instrument.id,
        instrument.make_price(0.74),
        Quantity.from_int(1),
        AggressorSide.BUYER,
        TradeId("wrong-side"),
        timestamp,
        timestamp,
    )
    records[key]["DOWN"].append(tick)
    with pytest.raises(ValueError, match="instrument-matched"):
        replay_variants([market], records, instruments)
    with pytest.raises(ValueError, match="standalone trade-tick replay"):
        replay_variants([market], {key: {"UP": [tick], "DOWN": []}}, instruments)


def test_taker_ioc_limit_fees_and_losing_settlement_are_not_hidden(replay):
    trade = row(replay, "one_touch_loss", "touch1_hold")
    assert trade["fees"] == pytest.approx(0.02016)
    assert trade["pnl"] == pytest.approx(-1.42016)
    target = row(replay, "one_flip", "touch1_target")
    assert target["exit_quantity"] == 5
    assert target["fees"] == pytest.approx(0.04032)
    assert target["pnl"] == pytest.approx(2.15968)
    assert all(fill["price"] >= 0.70 for fill in target["fills"] if fill["action"] == "sell")


def test_partial_entry_target_uses_actual_remaining_and_settles_remainder(replay):
    hold = row(replay, "partial", "touch1_hold")
    target = row(replay, "partial", "touch1_target")
    assert hold["entry_quantity"] == 2
    assert hold["requested_quantity"] == 5
    assert target["entry_quantity"] == 2
    assert target["requested_quantity"] == 5
    assert target["exit_quantity"] == 1
    assert target["remaining_quantity"] == 1
    sales = [a for a in target["attempts"] if a["action"] == "sell"]
    assert sales[0]["quantity"] == 2
    assert all(a["quantity"] <= 2 for a in sales)
    assert target["pnl"] == pytest.approx(target["cash_pnl"])


def test_transient_target_is_not_a_fictional_fill(replay):
    target = row(replay, "transient_target", "touch1_target")
    assert any(a["action"] == "sell" for a in target["attempts"])
    assert target["exit_quantity"] == 0
    assert target["remaining_quantity"] == 5
    assert target["pnl"] < 0


def test_target_requires_depth_for_actual_remaining_not_only_best_bid():
    markets, records, instruments = synthetic_fixture()
    market = markets[2]
    key = market["market_id"]
    for index, record in enumerate(records[key]["DOWN"]):
        if record.ts_init >= market["start_ns"] + 2 * SECOND:
            records[key]["DOWN"][index] = snapshot(
                instruments[key]["DOWN"], record.ts_init, 0.72, 0.74, bid_size=1
            )
    target = row(replay_variants([market], records, instruments), key, "touch1_target")
    assert target["entry_quantity"] == 5
    assert target["exit_quantity"] == 0
    assert not any(attempt["action"] == "sell" for attempt in target["attempts"])


def test_target_rejects_vwap_above_floor_if_some_required_levels_are_below_floor():
    markets, records, instruments = synthetic_fixture()
    market = markets[2]
    key = market["market_id"]
    instrument = instruments[key]["DOWN"]
    for index, record in enumerate(records[key]["DOWN"]):
        if record.ts_init < market["start_ns"] + 2 * SECOND:
            continue
        base = snapshot(instrument, record.ts_init, 0.80, 0.82, bid_size=3)
        below_floor = OrderBookDelta(
            instrument.id, BookAction.ADD,
            BookOrder(OrderSide.BUY, instrument.make_price(0.60), Quantity.from_int(2), 3),
            0, 0, record.ts_event, record.ts_init,
        )
        records[key]["DOWN"][index] = OrderBookDeltas(
            instrument.id, [*base.deltas[:-1], below_floor, base.deltas[-1]]
        )
    assert (3 * 0.80 + 2 * 0.60) / 5 > 0.70
    target = row(replay_variants([market], records, instruments), key, "touch1_target")
    assert target["entry_quantity"] == 5
    assert not any(attempt["action"] == "sell" for attempt in target["attempts"])


def test_valid_target_disappearing_before_decision_still_submits_floor_ioc():
    markets, records, instruments = synthetic_fixture()
    market = markets[2]
    key = market["market_id"]
    disappear = market["start_ns"] + 2 * SECOND + 10_000_000
    for index, record in enumerate(records[key]["DOWN"]):
        if record.ts_init >= disappear:
            records[key]["DOWN"][index] = snapshot(
                instruments[key]["DOWN"], record.ts_init, 0.60, 0.62
            )
    records[key]["DOWN"].append(snapshot(instruments[key]["DOWN"], disappear, 0.60, 0.62))
    records[key]["DOWN"].sort(key=lambda record: record.ts_init)
    target = row(replay_variants([market], records, instruments), key, "touch1_target")
    sales = [attempt for attempt in target["attempts"] if attempt["action"] == "sell"]
    assert sales and sales[0]["status"] == "submitted"
    assert sales[0]["limit_price"] == 0.70
    assert target["exit_quantity"] == 0


@pytest.mark.parametrize("invalidity", ["stale", "gap"])
def test_target_decision_suppressed_after_source_staleness_or_gap(invalidity):
    markets, records, instruments = synthetic_fixture()
    market = markets[2]
    key = market["market_id"]
    timestamp = market["start_ns"] + 2 * SECOND + 10_000_000
    for side, prices in (("UP", (0.26, 0.28)), ("DOWN", (0.72, 0.74))):
        record = snapshot(
            instruments[key][side],
            timestamp,
            *prices,
            ts_event=timestamp - 6 * SECOND if invalidity == "stale" else timestamp,
        )
        records[key][side].append(record)
        if invalidity == "gap" and side == "DOWN":
            records[key][side].append(record)
        records[key][side].sort(key=lambda record: record.ts_init)
    result = replay_variants([market], records, instruments, max_stale_ns=5 * SECOND)
    target = row(result, key, "touch1_target")
    assert target["censored"]
    assert target["exit_quantity"] == 0
    assert any(
        attempt["action"] == "sell" and attempt["status"] == "invalid_or_stale_book"
        for attempt in target["attempts"]
    )
    mark = next(
        point
        for point in result["market_equity"]
        if point["variant"] == "touch1_target" and point["ts_ns"] == timestamp
    )
    assert mark["equity"] is None and mark["exposure"] is None


def test_old_source_event_is_not_freshened_by_receipt_normalization():
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    timestamp = market["start_ns"] + SECOND
    records[key]["UP"][1] = snapshot(
        instruments[key]["UP"], timestamp, 0.72, 0.74, ts_event=timestamp - 6 * SECOND
    )
    trade = row(
        replay_variants([market], records, instruments, max_stale_ns=5 * SECOND),
        key,
        "touch1_hold",
    )
    assert not trade["entered"]
    assert "UP:stale_book" in trade["censor_reasons"]


@pytest.mark.parametrize("known_settlement", [False, True])
def test_idle_marks_expire_while_held_and_known_settlement_restores_cash(known_settlement):
    markets, records, instruments = synthetic_fixture()
    market = deepcopy(markets[1])
    key = market["market_id"]
    if known_settlement:
        market["settlement_ns"] = market["start_ns"] + 20 * SECOND
    else:
        market.update(
            close_ns=market["start_ns"] + 20 * SECOND,
            duration_seconds=20,
            settlement_winner=None,
            settlement_ns=None,
        )
    result = replay_variants([market], records, instruments, max_stale_ns=5 * SECOND)
    expiry = max(record.ts_event for rows in records[key].values() for record in rows) + 5 * SECOND + 1
    mark = next(
        point
        for point in result["market_equity"]
        if point["variant"] == "touch1_hold" and point["ts_ns"] == expiry
    )
    assert mark["equity"] is None and mark["exposure"] is None
    curve = [point for point in result["equity"] if point["variant"] == "touch1_hold"]
    assert next(point for point in curve if point["ts_ns"] == expiry)["equity"] is None
    trade = row(result, key, "touch1_hold")
    if known_settlement:
        assert curve[-1]["ts_ns"] == market["settlement_ns"]
        assert curve[-1]["equity"] == pytest.approx(1000 + trade["pnl"])
    else:
        assert curve[-1]["equity"] is None
        assert trade["pnl"] is None


def test_entry_cap_uses_actual_cheap_side_ask_not_favorite_complement():
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    for index, record in enumerate(records[key]["DOWN"]):
        if record.ts_init >= market["start_ns"] + SECOND:
            records[key]["DOWN"][index] = snapshot(
                instruments[key]["DOWN"], record.ts_init, 0.26, 0.45
            )
    result = replay_variants([market], records, instruments)
    trade = row(result, key, "touch1_hold")
    assert trade["entry_attempted"]
    assert trade["attempts"][0]["status"] == "price_cap"
    assert not trade["entered"]


def test_signals_use_observable_timestamp_not_earlier_exchange_event(monkeypatch):
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    timestamp = market["start_ns"] + SECOND
    for side, prices in (("UP", (0.72, 0.74)), ("DOWN", (0.26, 0.28))):
        records[key][side][1] = snapshot(
            instruments[key][side],
            timestamp,
            *prices,
            ts_event=market["start_ns"] + SECOND // 2,
        )
    original_add = replay_module.add_engine_data_by_type
    normalized_batches = []

    def add_on_observable_clock(engine, engine_records):
        assert all(
            delta.ts_event == delta.ts_init == record.ts_init
            for record in engine_records
            for delta in record.deltas
        )
        normalized_batches.append(len(engine_records))
        original_add(engine, engine_records)

    monkeypatch.setattr(replay_module, "add_engine_data_by_type", add_on_observable_clock)
    trade = row(replay_variants([market], records, instruments), key, "touch1_hold")
    assert trade["signal_ts_ns"] == timestamp
    assert trade["entry_ts_ns"] > timestamp
    assert len(normalized_batches) == 6
    assert records[key]["UP"][1].ts_event == market["start_ns"] + SECOND // 2


def test_missing_settlement_is_explicitly_unresolved(replay):
    trade = row(replay, "missing_settlement", "touch1_hold")
    assert trade["remaining_quantity"] == 5
    assert trade["settlement_status"] == "unresolved"
    assert trade["pnl"] is None


def test_shared_position_and_capital_limits_not_per_market():
    fixture = synthetic_fixture()
    result = replay_variants(*fixture, max_positions=1)
    entries = [t for t in result["trades"] if t["variant"] == "touch1_hold" and t["entered"]]
    assert len(entries) == 1
    assert (
        max(
            point["open_positions"]
            for point in result["equity"]
            if point["variant"] == "touch1_hold"
        )
        == 1
    )
    constrained = replay_variants(*fixture, initial_cash=1)
    assert not any(t["entered"] for t in constrained["trades"])
    assert any(a["status"] == "capital_limit" for t in constrained["trades"] for a in t["attempts"])


@pytest.mark.parametrize("known_settlement", [False, True])
def test_closed_held_position_keeps_portfolio_slot_even_after_offline_settlement(known_settlement):
    markets, records, instruments = synthetic_fixture()
    held = deepcopy(markets[1] if known_settlement else markets[7])
    later = deepcopy(markets[2])
    key = later["market_id"]
    offset = held["start_ns"] - later["start_ns"] + 6 * SECOND
    for name in ("start_ns", "close_ns", "settlement_ns"):
        later[name] += offset
    for side in ("UP", "DOWN"):
        records[key][side] = [
            snapshot(
                instruments[key][side],
                record.ts_init + offset,
                float(record.deltas[1].order.price),
                float(record.deltas[2].order.price),
            )
            for record in records[key][side]
        ]
    result = replay_variants([held, later], records, instruments, max_positions=1)
    assert row(result, held["market_id"], "touch1_hold")["entered"]
    assert row(result, held["market_id"], "touch1_hold")["settlement_status"] == (
        "settled" if known_settlement else "unresolved"
    )
    blocked = row(result, key, "touch1_hold")
    assert not blocked["entered"]
    assert blocked["attempts"][0]["status"] == "position_limit"


def test_settlement_changes_do_not_change_signals_orders_or_preclose_equity():
    markets, records, instruments = synthetic_fixture()
    markets = [markets[1]]
    first = replay_variants(markets, records, instruments)
    changed = deepcopy(markets)
    changed[0]["settlement_winner"] = "DOWN"
    payload = BinaryOption.to_dict(instruments[markets[0]["market_id"]]["DOWN"])
    payload["info"] = {"result": "DOWN", "winner": True, "nested": {"result": "DOWN"}}
    instruments[markets[0]["market_id"]]["DOWN"] = BinaryOption.from_dict(payload)
    second = replay_variants(changed, records, instruments)
    for a, b in zip(first["trades"], second["trades"], strict=True):
        assert a["fills"] == b["fills"]
        assert a["attempts"] == b["attempts"]
        assert a.get("signal_ts_ns") == b.get("signal_ts_ns")
    settlement = markets[0]["settlement_ns"]
    assert [p for p in first["equity"] if p["ts_ns"] < settlement] == [
        p for p in second["equity"] if p["ts_ns"] < settlement
    ]


def test_complete_observable_group_has_no_provisional_favorite():
    markets, records, instruments = synthetic_fixture()
    market = markets[1]
    key = market["market_id"]
    # The second outcome at the same timestamp makes the favorite ambiguous.
    # Neither arrival ordering nor one provisional book may trigger an entry.
    timestamp = market["start_ns"] + SECOND
    records[key]["DOWN"][1] = snapshot(instruments[key]["DOWN"], timestamp, 0.72, 0.74)
    first = replay_variants([market], records, instruments)
    swapped = {key: {"DOWN": records[key]["DOWN"], "UP": records[key]["UP"]}}
    second = replay_variants([market], swapped, instruments)
    assert not any(t["entered"] for t in first["trades"] + second["trades"])


def test_historical_fee_inputs_fail_closed_and_unsupported_schedule_rejected():
    markets, records, instruments = synthetic_fixture()
    market = deepcopy(markets[1])
    market["source"] = "telonex"
    with pytest.raises(ValueError, match="unverified historical"):
        replay_variants([market], records, instruments)
    market.update(
        fee_verified=True,
        fee_source="archived-market-fee-snapshot",
        fee_valid_from_ns=market["start_ns"],
        fee_valid_to_ns=market["close_ns"] - 1,
    )
    with pytest.raises(ValueError, match="unverified historical"):
        replay_variants([market], records, instruments)
    market["fee_valid_to_ns"] = market["close_ns"]
    market["fee_schedule"] = "different-exponent"
    with pytest.raises(ValueError, match="Unsupported historical fee"):
        replay_variants([market], records, instruments)
    market["fee_schedule"] = "polymarket_p*(1-p)"
    original_metadata = deepcopy(market)
    assert replay_variants([market], records, instruments)["trades"]
    assert market == original_metadata


def test_output_json_persists_portfolios_in_project_path():
    # No scratch-directory fixture: exercise the exact public serialization
    # contract without introducing files outside the project.
    import json
    from pathlib import Path

    output = Path("tests/.longshot-execution-artifacts")
    try:
        markets, records, instruments = synthetic_fixture()
        result = replay_variants([markets[0]], records, instruments, output_dir=output)
        assert json.loads((output / "replay.json").read_text()) == result
    finally:
        (output / "replay.json").unlink(missing_ok=True)
        if output.exists():
            output.rmdir()
