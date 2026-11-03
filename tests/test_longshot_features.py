from __future__ import annotations

import pytest

from prediction_market_extensions.backtesting.longshot.features import attach_underlying_features


def test_underlying_join_is_causal_and_reports_unavailable() -> None:
    markets = [{"market_id": "x", "reference_price": 100, "reference_ns": 0}]
    event = {"market_id": "x", "asset": "BTC", "ts_ns": 2}
    records = [{"ts_ns": 0, "price": 100}, {"ts_ns": 1, "price": 110}, {"ts_ns": 2, "price": 121}]
    attach_underlying_features([event], markets, {"BTC": records}, max_age_ns=1, lookback_ns=2)
    assert event["underlying_recent_return"] == pytest.approx(0.21)
    assert event["underlying_distance_to_reference"] == pytest.approx(0.21)
    before = dict(event)
    attach_underlying_features(
        [event], markets, {"BTC": records + [{"ts_ns": 3, "price": 1}]}, max_age_ns=1, lookback_ns=2
    )
    assert event == before
    stale = {"market_id": "x", "asset": "BTC", "ts_ns": 10}
    attach_underlying_features([stale], markets, {"BTC": records}, max_age_ns=1)
    assert stale["underlying_recent_return"] is None
    assert stale["underlying_unavailable_reason"]


def test_underlying_does_not_bridge_gaps_or_future_reference() -> None:
    markets = [{"market_id": "x", "reference_price": 100, "reference_ns": 100}]
    event = {"market_id": "x", "asset": "BTC", "ts_ns": 10}
    attach_underlying_features(
        [event],
        markets,
        {"BTC": [{"ts_ns": 0, "price": 100}, {"ts_ns": 10, "price": 120}]},
        max_age_ns=1,
        lookback_ns=10,
    )
    assert event["underlying_recent_return"] is None
    assert event["underlying_distance_to_reference"] is None
    with pytest.raises(ValueError, match="ordered"):
        attach_underlying_features([event], markets, {"BTC": [{"ts_ns": 0, "price": 0}]})
