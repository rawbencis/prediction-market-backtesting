from copy import deepcopy

import pytest
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.instruments import BinaryOption

import prediction_market_extensions.backtesting.longshot.replay as replay_module
from prediction_market_extensions.backtesting.longshot.replay import VARIANTS, replay_variants
from prediction_market_extensions.backtesting.longshot.synthetic import (
    SECOND,
    snapshot,
)
from prediction_market_extensions.backtesting.longshot.synthetic import (
    synthetic_inputs as synthetic_fixture,
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
    assert target["entry_quantity"] == 2
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
    assert len(entries) == 3
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


def test_unresolved_position_keeps_portfolio_slot_after_market_close():
    markets, records, instruments = synthetic_fixture()
    unresolved, later = deepcopy(markets[7]), deepcopy(markets[1])
    key = later["market_id"]
    offset = unresolved["start_ns"] - later["start_ns"] + 6 * SECOND
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
    result = replay_variants([unresolved, later], records, instruments, max_positions=1)
    assert row(result, unresolved["market_id"], "touch1_hold")["entered"]
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
    assert replay_variants([market], records, instruments)["trades"]


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
