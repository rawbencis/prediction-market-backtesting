import pytest

from prediction_market_extensions.backtesting.longshot import (
    BookState,
    PairObservation,
    TouchState,
    extract_market,
)


def market(**updates):
    return {
        "market_id": "m",
        "asset": "BTC",
        "duration_seconds": 1e-8,
        "start_ns": 0,
        "close_ns": 10,
        "outcomes": {"UP": "up-token", "DOWN": "down-token"},
        "settlement_winner": "DOWN",
        "settlement_ns": 11,
        **updates,
    }


def obs(ts, up=0.5, down=0.5, size=10, gap=False):
    return PairObservation(
        ts,
        BookState(ts, ((up, size),), ((up + 0.01, size),)),
        BookState(ts, ((down, size),), ((down + 0.01, size),)),
        gap,
    )


def path():
    return [obs(0), obs(1, 0.7, 0.2), obs(3, 0.2, 0.7), obs(10, 0.5, 0.5)]


def test_complete_labels_quantity_excursions_and_metadata():
    audit, events = extract_market(market(), path(), max_gap_ns=10)
    assert audit["eligible"] and audit["complete"] and audit["reversing"]
    assert audit["touch_count"] == 2
    assert audit["start_ns"] == 0 and audit["close_ns"] == 10
    first, second = events
    assert first["next_touch"] is True and first["time_to_next_touch_ns"] == 2
    assert first["time_to_next_touch"] == 2e-9
    assert second["next_touch"] is False and second["time_to_next_touch_ns"] is None
    assert second["time_between_touches_ns"] == 2
    assert first["cheap_side"] == "DOWN"
    assert first["cheap_ask_vwap"] == pytest.approx(0.21)
    assert first["entry_vwap"] == first["cheap_ask_vwap"]
    assert first["entry_spread"] == first["cheap_spread"]
    assert first["entry_depth"] == first["cheap_ask_depth"]
    assert first["remaining_fraction"] == 0.9
    assert first["cheap_spread"] == pytest.approx(0.01)
    assert first["cheap_ask_depth"] == 10
    assert first["executable_bid_mfe"] == pytest.approx(0.49)
    assert first["executable_bid_mae"] == pytest.approx(0.49)
    assert first["lifetime_remaining_ns"] == 9
    assert not first["excursion_incomplete"]
    assert first["executable_target"] is True
    assert first["target_first_ts_ns"] == 3
    assert first["time_to_target_ns"] == 2
    assert first["target_label_type"].endswith("not_a_fill")
    assert first["label_type"].startswith("descriptive")
    assert first["threshold_basis"] == "midpoint"
    assert first["crossing_from_midpoint"] == pytest.approx(0.505)
    assert first["crossing_to_midpoint"] == pytest.approx(0.705)
    assert first["crossing_overshoot"] == pytest.approx(0.005)
    assert first["crossing_was_jump"]
    assert first["settlement_winner"] == "DOWN"


def test_no_touch_and_nonreversing_markets_remain_in_denominator():
    audit, events = extract_market(market(), [obs(0), obs(10)])
    assert audit["eligible"] and audit["complete"] and audit["no_entry"]
    assert not events
    audit, events = extract_market(market(), [obs(0), obs(1, 0.7, 0.2), obs(10, 0.7, 0.2)])
    assert audit["eligible"] and not audit["reversing"]
    assert len(events) == 1 and events[0]["next_touch"] is False


@pytest.mark.parametrize(
    "data",
    [
        [obs(0), obs(1, 0.7, 0.2), obs(2, gap=True), obs(10, 0.2, 0.7)],
        [obs(0), obs(1, 0.7, 0.2)],
        [obs(0), obs(1, 0.7, 0.2), PairObservation(2, BookState(2), BookState(2)), obs(10)],
        [obs(0), obs(1, 0.7, 0.2), obs(2, 0.7, 0.7), obs(10)],
    ],
)
def test_censored_last_touch_unknown_not_false(data):
    audit, events = extract_market(market(), data, max_gap_ns=2)
    assert audit["censored"] and not audit["complete"]
    assert len(events) == 1 and events[0]["next_touch"] is None
    assert events[0]["excursion_incomplete"]
    assert events[0]["executable_bid_mfe"] is None


def test_left_truncated_empty_and_invalid_mapping_audits():
    audit, events = extract_market(market(), [obs(0, 0.7, 0.2), obs(10, 0.2, 0.7)])
    assert audit["left_truncated"] and not audit["eligible"] and events == []
    audit, events = extract_market(market(), [])
    assert audit["no_entry"] and "no_observations" in audit["exclusion_flags"]
    for outcomes in ({"YES": "up", "NO": "down"}, {"UP": "same", "DOWN": "same"}, {}):
        audit, events = extract_market(market(outcomes=outcomes), path())
        assert not audit["mapping_valid"] and not audit["eligible"] and not events
    audit, events = extract_market({"market_id": "missing"}, path())
    assert audit["missing_metadata"] and not audit["eligible"] and not events


def test_insufficient_entry_or_future_depth_is_not_executable_excursion():
    for data in (
        [obs(0), obs(1, 0.7, 0.2, size=1), obs(10)],
        [obs(0), obs(1, 0.7, 0.2), obs(5, size=1), obs(10)],
    ):
        audit, events = extract_market(market(), data)
        assert audit["complete"]
        assert events[0]["excursion_incomplete"]
        assert events[0]["executable_bid_mfe"] is None
        assert events[0]["executable_bid_mae"] is None


def test_missing_settlement_and_no_lookahead_in_state_or_touches():
    original_audit, original = extract_market(market(), path())
    for settlement in (
        {"settlement_winner": None, "settlement_ns": None},
        {"settlement_winner": "UP", "settlement_ns": 11},
        {"settlement_winner": "UP", "settlement_ns": 1},
    ):
        audit, events = extract_market(market(**settlement), path())
        assert audit["eligible"] == original_audit["eligible"]
        assert [
            {key: value for key, value in event.items() if key != "settlement_winner"}
            for event in events
        ] == [
            {key: value for key, value in event.items() if key != "settlement_winner"}
            for event in original
        ]
    state = TouchState()
    assert not any("settlement" in name for name in vars(state))
    assert not any("winner" in name for name in vars(state))


def test_outside_window_and_invalid_quantity():
    audit, events = extract_market(market(), [obs(0), obs(10), obs(11, 0.7, 0.2)])
    assert audit["input_observation_count"] == 3 and audit["observation_count"] == 2
    assert not events
    with pytest.raises(ValueError):
        extract_market(market(), path(), quantity=0)


def test_source_coverage_metadata_is_preserved_in_audit_only():
    source = {
        "source": "fixture",
        "source_coverage": {"hours_loaded": 2, "missing_hours": [3]},
        "source_gap_count": 1,
    }
    audit, events = extract_market(market(**source), path())
    assert all(audit[key] == value for key, value in source.items())
    assert all("source_coverage" not in event for event in events)


@pytest.mark.parametrize("duration", [10, 0, -1, float("nan"), None, "10", True])
def test_duration_must_match_market_lifetime(duration):
    audit, events = extract_market(market(duration_seconds=duration), path())
    assert not audit["eligible"] and not audit["duration_valid"]
    assert "invalid_duration" in audit["exclusion_flags"]
    assert not events


def test_quantity_target_requires_every_executable_price_at_or_above_floor():
    future_book = BookState(
        3,
        ((0.8, 3), (0.6, 3)),
        ((0.81, 10),),
    )
    assert future_book.vwap("bid", 5) > 0.7
    assert future_book.depth_at_price("bid", 0.7) == 3
    data = [obs(0), obs(1, 0.7, 0.2), PairObservation(3, obs(3, 0.2, 0.5).up, future_book), obs(10)]
    audit, events = extract_market(market(), data)
    assert audit["complete"]
    assert events[0]["next_touch"] is True
    assert events[0]["executable_target"] is False
    assert events[0]["target_first_ts_ns"] is None


def test_target_insufficient_depth_or_no_preclose_future_is_unknown():
    for data in (
        [obs(0), obs(1, 0.7, 0.2), obs(3, 0.2, 0.7, size=1), obs(10)],
        [obs(0), obs(1, 0.7, 0.2), obs(10, 0.2, 0.7)],
    ):
        _, events = extract_market(market(), data)
        assert events[0]["executable_target"] is None
        assert events[0]["target_first_ts_ns"] is None


def test_target_censoring_and_positive_target_before_later_gap():
    before_gap = [obs(0), obs(1, 0.7, 0.2), obs(3, gap=True), obs(5, 0.2, 0.7), obs(10)]
    audit, events = extract_market(market(), before_gap)
    assert audit["censored"] and events[0]["executable_target"] is None
    after_target = [obs(0), obs(1, 0.7, 0.2), obs(3, 0.2, 0.7), obs(5, gap=True), obs(10)]
    audit, events = extract_market(market(), after_target)
    assert audit["censored"] and events[0]["executable_target"] is True
    assert events[0]["target_first_ts_ns"] == 3


def test_target_noncomplementary_books_and_missing_settlement():
    data = [obs(0), obs(1, 0.7, 0.6), obs(3, 0.1, 0.75), obs(10)]
    audit, events = extract_market(market(settlement_winner=None, settlement_ns=None), data)
    assert audit["eligible"] and audit["missing_settlement"]
    assert events[0]["entry_vwap"] == pytest.approx(0.61)
    assert events[0]["executable_target"] is True
    assert events[0]["target_price_floor"] == 0.7


def test_exact_close_cannot_hide_a_gap_or_invalid_terminal_book():
    audit, events = extract_market(
        market(),
        [obs(0), obs(1, 0.7, 0.2), obs(10, 0.2, 0.7)],
        max_gap_ns=5,
    )
    assert audit["censored"] and not audit["complete"]
    assert "missing_end_coverage" in audit["censor_reasons"]
    assert events[0]["next_touch"] is None

    invalid_close = [
        obs(0),
        obs(1, 0.7, 0.2),
        PairObservation(10, BookState(10), BookState(10)),
    ]
    audit, events = extract_market(market(), invalid_close, max_gap_ns=20)
    assert not audit["complete"]
    assert "invalid_terminal_books" in audit["censor_reasons"]
    assert events[0]["next_touch"] is None


def test_preclose_book_expiring_before_close_is_not_a_completed_non_reversal():
    audit, events = extract_market(
        market(),
        [obs(0), obs(1, 0.7, 0.2)],
        max_gap_ns=20,
        max_stale_ns=5,
    )
    assert not audit["complete"]
    assert "invalid_terminal_books" in audit["censor_reasons"]
    assert events[0]["next_touch"] is None
    fresh, fresh_events = extract_market(
        market(),
        [obs(0), obs(1, 0.7, 0.2)],
        max_gap_ns=20,
        max_stale_ns=20,
    )
    assert fresh["complete"]
    assert fresh_events[0]["next_touch"] is False


def test_close_only_suffix_is_not_an_executable_excursion():
    audit, events = extract_market(market(), [obs(0), obs(1, 0.7, 0.2), obs(10, 0.9, 0.1)])
    assert audit["complete"] and events[0]["next_touch"] is False
    assert events[0]["excursion_incomplete"]
    assert events[0]["executable_bid_mfe"] is None
    assert events[0]["executable_bid_mae"] is None
    assert events[0]["observed_future_bid_vwap_max"] is None


def test_exact_close_reversal_counts_as_coverage_not_a_next_touch():
    audit, events = extract_market(
        market(),
        [obs(0), obs(1, 0.7, 0.2), obs(10, 0.2, 0.7)],
    )
    assert audit["complete"]
    assert audit["coverage_end_ns"] == 10
    assert audit["touch_count"] == 1 and not audit["reversing"]
    assert len(events) == 1 and events[0]["next_touch"] is False
    assert events[0]["executable_target"] is None
