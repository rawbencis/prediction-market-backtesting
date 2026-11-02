"""Statistical safeguards for chronological longshot research."""

import json
import math
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from prediction_market_extensions.backtesting.longshot.analysis import DAY_NS, analyze


def market(identifier, day=0, **changes):
    return {
        "market_id": identifier,
        "asset": "BTC",
        "duration_seconds": 300,
        "start_ns": day * DAY_NS,
        "close_ns": day * DAY_NS + 300_000_000_000,
        "settlement_winner": None,
        "eligible": True,
        "complete": True,
        "censored": False,
        **changes,
    }


def event(identifier, touch=1, next_touch=False, day=0, **changes):
    return {
        "market_id": identifier,
        "touch_number": touch,
        "ts_ns": day * DAY_NS + touch * 10_000_000_000,
        "cheap_side": "YES",
        "entry_vwap": 0.1,
        "entry_spread": 0.01,
        "entry_depth": 100,
        "quantity": 10,
        "remaining_fraction": 0.5,
        "next_touch": next_touch,
        "time_to_next_touch_ns": None,
        "time_between_touches_ns": None,
        **changes,
    }


def run(audits, events=(), trades=(), equity=(), **changes):
    return analyze(
        audits,
        list(events),
        trades,
        equity,
        **{
            "discovery_end_ns": 10 * DAY_NS,
            "validation_end_ns": 20 * DAY_NS,
            "final_end_ns": 30 * DAY_NS,
            "bootstrap_samples": 100,
            "min_days": 1,
            "min_markets": 1,
            **changes,
        },
    )


def test_whole_lifetime_chronological_partitions_and_strata():
    audits = [
        market("discovery"),
        market("validation", 10),
        market("final", 20),
        market("straddle", start_ns=10 * DAY_NS - 1, close_ns=10 * DAY_NS + 1),
        market("outside", 31),
        market("other_asset", asset="ETH"),
        market("other_duration", duration_seconds=900),
        market("ineligible", eligible=False),
    ]
    result = run(audits)
    assert [(g["partition"], g["asset"], g["duration_seconds"]) for g in result["groups"]] == [
        ("discovery", "BTC", 300),
        ("discovery", "BTC", 900),
        ("discovery", "ETH", 300),
        ("validation", "BTC", 300),
        ("final", "BTC", 300),
    ]
    assert result["exclusions"] == {
        "boundary_straddlers": 1,
        "outside_final_window": 1,
        "ineligible": 1,
    }
    assert all(not value["available"] for value in result["underlying_features"].values())
    json.dumps(result, allow_nan=False)


def test_risk_sets_keep_non_flips_censoring_and_missing_distinct():
    audits = [
        market("flip"),
        market("nonflip"),
        market("censored", complete=False, censored=True),
        market("missing"),
        market("zero"),
    ]
    rows = [
        event("flip", next_touch=True),
        event("flip", touch=2, next_touch=False),
        event("nonflip"),
        event("censored", next_touch=None),
        event("missing", next_touch=None),
    ]
    group = run(audits, rows)["groups"][0]
    p12 = group["transitions"]["p12"]
    assert p12["at_risk"] == 4
    assert p12["observed"] == 2
    assert p12["next_touch_count"] == 1
    assert p12["completed_non_flips"] == 1
    assert p12["censored"] == 1
    assert p12["missing_observations"] == 1
    assert p12["probability"]["estimate"] == 0.5
    assert group["transitions"]["p23"]["probability"]["estimate"] == 0
    assert group["zero_touch_markets"] == 1


def test_seeded_day_bootstrap_preserves_zero_entry_denominator():
    audits = [market(str(day), day) for day in range(5)]
    trades = [
        {"market_id": "0", "variant": "hold", "filled": True, "net_pnl": 10, "deployed_capital": 5},
        {"market_id": "0", "variant": "hold", "filled": True, "net_pnl": 10, "deployed_capital": 5},
    ]
    rows = [event("0", next_touch=True), event("0", touch=2, next_touch=False)]
    first = run(audits, rows, trades)
    assert first == run(audits, rows, trades)
    metric = first["groups"][0]["variants"][0]
    assert metric["eligible_markets"] == 5
    assert metric["fill_rate"] == 0.2
    assert metric["net_pnl_per_eligible"]["estimate"] == 4
    assert metric["net_pnl_per_eligible"]["days"] == 5
    assert metric["net_pnl_per_eligible"]["ci95"][0] == 0
    assert metric["net_pnl_per_eligible"]["ci95"][1] > 4
    assert len(metric["per_market"]) == 5
    assert metric["net_deployed_return"]["estimate"] == 2
    # Most resamples have no deployed capital: reporting a conditional CI would mislead.
    assert metric["net_deployed_return"]["ci95"] is None


def test_insufficient_samples_are_null_not_zero():
    result = run(
        [market("a")],
        [event("a")],
        [{"market_id": "a", "variant": "hold", "filled": False}],
        min_days=5,
        min_markets=20,
    )
    group = result["groups"][0]
    assert group["transitions"]["p12"]["probability"]["estimate"] is None
    assert group["transitions"]["p12"]["probability"]["ci95"] is None
    variant = group["variants"][0]
    assert variant["fill_rate"] is None
    assert variant["net_pnl"] is None
    assert variant["net_pnl_per_trade"] is None
    assert variant["net_pnl_per_eligible"]["estimate"] is None
    assert variant["net_deployed_return"]["estimate"] is None
    assert variant["portfolio"]["max_drawdown"] is None


def test_portfolio_drawdown_sums_synchronous_curve_not_market_drawdowns():
    audits = [market("a"), market("b")]
    marks = []
    for name, values in (("a", (0, 10, 0)), ("b", (0, -10, 0))):
        for timestamp, value in zip((0, 10, 20), values, strict=True):
            marks.append(
                {
                    "market_id": name,
                    "variant": "hold",
                    "ts_ns": timestamp,
                    "equity": value,
                    "exposure": 5 if timestamp < 20 else 0,
                }
            )
    marks += [
        {"market_id": "a", "variant": "exit", "ts_ns": 0, "equity": 0, "exposure": 0},
        {"market_id": "a", "variant": "exit", "ts_ns": 10, "equity": -7, "exposure": 0},
    ]
    variants = {row["variant"]: row for row in run(audits, equity=marks)["groups"][0]["variants"]}
    assert variants["hold"]["portfolio"]["max_drawdown"] == 0
    assert variants["hold"]["portfolio"]["max_exposure"] == 10
    assert variants["exit"]["portfolio"]["max_drawdown"] == 7
    assert all(row["equity"] == 0 for row in variants["hold"]["portfolio"]["curve"])


def test_forward_fill_marks_when_market_timestamps_differ():
    marks = [
        {"market_id": "a", "variant": "hold", "ts_ns": 0, "equity": 5, "exposure": 1},
        {"market_id": "b", "variant": "hold", "ts_ns": 10, "equity": -3, "exposure": 2},
        {"market_id": "a", "variant": "hold", "ts_ns": 20, "equity": 7, "exposure": 0},
    ]
    portfolio = run([market("a"), market("b")], equity=marks)["groups"][0]["variants"][0][
        "portfolio"
    ]
    assert [row["equity"] for row in portfolio["curve"]] == [5, 2, 4]
    assert portfolio["max_drawdown"] == 3


def test_variants_have_independent_denominators_and_pnl():
    trades = [
        {"market_id": "a", "variant": "hold", "filled": True, "net_pnl": 3, "deployed_capital": 1},
        {"market_id": "a", "variant": "exit", "filled": True, "net_pnl": -2, "deployed_capital": 1},
        {"market_id": "b", "variant": "hold", "filled": False},
    ]
    variants = run([market("a"), market("b")], trades=trades)["groups"][0]["variants"]
    by_name = {row["variant"]: row for row in variants}
    assert by_name["hold"]["net_pnl_per_eligible"]["estimate"] == 1.5
    assert by_name["exit"]["net_pnl_per_eligible"]["estimate"] == -1
    assert by_name["hold"]["attempt_fill_rate"] == 0.5
    assert by_name["exit"]["attempt_fill_rate"] == 1


def test_segmented_history_overlap_and_sample_warnings():
    rows = [event("a", next_touch=True), event("a", touch=2, entry_vwap=0.25)]
    group = run([market("a")], rows)["groups"][0]
    vwap = [row for row in group["touch_history_comparisons"] if row["feature"] == "entry_vwap"]
    assert all(not row["sufficient"] for row in vwap)
    assert all("No observed" in row["overlap_warning"] for row in vwap)
    shared = next(
        row for row in group["touch_history_comparisons"] if row["feature"] == "entry_depth"
    )
    assert shared["sufficient"]
    assert "joint-state overlap" in shared["overlap_warning"]
    assert "not causal" in shared["interpretation"]


@pytest.mark.parametrize(
    "config",
    [
        {"discovery_end_ns": 20 * DAY_NS},
        {"validation_end_ns": math.inf},
        {"final_end_ns": math.nan},
        {"seed": 1.2},
        {"bootstrap_samples": 0},
        {"bootstrap_samples": True},
        {"min_days": 0},
        {"min_markets": -1},
    ],
)
def test_invalid_configuration_rejected(config):
    with pytest.raises((ValueError, TypeError)):
        run([], **config)


@pytest.mark.parametrize(
    "audits,rows",
    [
        ([market("a"), market("a")], []),
        ([market("a", close_ns=0)], []),
        ([market("a")], [event("missing")]),
        ([market("a")], [event("a"), event("a")]),
        ([market("a")], [event("a", entry_vwap=math.nan)]),
        ([market("a")], [event("a", next_touch="no")]),
        ([market("a")], [event("a", ts_ns=-1)]),
        ([market("a")], [event("a", ts_ns=300_000_000_000)]),
        ([market("a")], [event("a", touch=2), event("a", touch=1, ts_ns=30_000_000_000)]),
    ],
)
def test_invalid_observations_rejected(audits, rows):
    with pytest.raises(ValueError):
        run(audits, rows)


def test_empty_report_is_json_serializable():
    result = run([])
    assert result["groups"] == []
    json.dumps(result, allow_nan=False)


def test_zero_event_days_do_not_count_as_observed_transition_days():
    audits = [market(str(day), day) for day in range(5)]
    group = run(audits, [event("0")], min_days=5)["groups"][0]
    probability = group["transitions"]["p12"]["probability"]
    assert probability["days"] == 5
    assert probability["informative_days"] == 1
    assert probability["estimate"] is None


def test_touch_timing_and_executable_entry_buckets():
    rows = [
        event("a", next_touch=True, time_to_next_touch_ns=20),
        event("a", touch=2, time_between_touches_ns=20, entry_depth=1),
    ]
    group = run([market("a")], rows)["groups"][0]
    assert group["transitions"]["p12"]["time_to_next_touch_ns"]["median"] == 20
    assert group["transitions"]["p23"]["time_between_touches_ns"]["median"] == 20
    executable = [row for row in group["segments"] if row["feature"] == "executable_entry"]
    assert len(executable) == 2
    assert executable[0]["bucket"] != executable[1]["bucket"]


def test_incomplete_false_outcome_is_not_a_completed_non_flip():
    group = run([market("a", censored=True, complete=False)], [event("a", next_touch=False)])[
        "groups"
    ][0]
    statistic = group["transitions"]["p12"]
    assert statistic["observed"] == 0
    assert statistic["censored"] == 1
    assert statistic["probability"]["estimate"] is None


def test_joint_portfolio_preserves_cross_asset_diversification():
    marks = [
        {"market_id": "a", "variant": "hold", "ts_ns": 0, "equity": 0, "exposure": 0},
        {"market_id": "b", "variant": "hold", "ts_ns": 0, "equity": 0, "exposure": 0},
        {"market_id": "a", "variant": "hold", "ts_ns": 10, "equity": -7, "exposure": 0},
        {"market_id": "b", "variant": "hold", "ts_ns": 10, "equity": 7, "exposure": 0},
    ]
    result = run([market("a"), market("b", asset="ETH")], equity=marks)
    assert len(result["groups"]) == 2
    assert result["portfolios"][0]["portfolio"]["max_drawdown"] == 0
    assert result["groups"][0]["variants"][0]["portfolio"]["max_drawdown"] == 7


def test_unresolved_pnl_and_missing_marks_never_become_zero():
    trades = [
        {
            "market_id": "a",
            "variant": "hold",
            "filled": True,
            "net_pnl": None,
            "deployed_capital": 5,
        },
        {"market_id": "b", "variant": "hold", "filled": True, "net_pnl": 7, "deployed_capital": 5},
    ]
    marks = [{"market_id": "a", "variant": "hold", "ts_ns": 0, "equity": None, "exposure": 5}]
    result = run([market("a"), market("b")], trades=trades, equity=marks)
    metric = result["groups"][0]["variants"][0]
    assert metric["filled_trade_count"] == 2
    assert metric["unresolved_pnl_count"] == 1
    assert metric["per_market"][0]["net_pnl"] is None
    assert metric["net_pnl"] is None
    assert metric["net_pnl_per_eligible"]["estimate"] is None
    assert metric["net_deployed_return"]["estimate"] is None
    assert metric["portfolio"]["available"] is False
    assert metric["portfolio"]["max_drawdown"] is None


def test_invalid_ineligible_audits_remain_counted_not_analysis_failures():
    audits = [
        market("a", eligible=False, start_ns=None, close_ns=None, duration_seconds=None),
        market(None, eligible=False, asset=None),
        market("valid"),
    ]
    result = run(audits)
    assert result["exclusions"]["ineligible"] == 2
    assert result["groups"][0]["eligible_markets"] == 1


def test_parent_cli_flat_underlying_features_report_actual_availability():
    rows = [
        event(
            "a",
            underlying_ts_ns=9_000_000_000,
            underlying_distance_to_reference=0.02,
            underlying_volatility=0.001,
            underlying_recent_return=0.03,
            underlying_unavailable_reason=None,
        ),
        event(
            "b",
            underlying_ts_ns=None,
            underlying_distance_to_reference=None,
            underlying_volatility=None,
            underlying_recent_return=None,
            underlying_unavailable_reason="synchronized underlying input unavailable",
        ),
    ]
    result = run([market("a"), market("b")], rows)
    for feature in result["underlying_features"].values():
        assert feature["available"]
        assert feature["available_observations"] == 1
        assert feature["unavailable_observations"] == 1
    volatility = result["underlying_features"]["reference_volatility"]
    assert volatility["unit_observation_counts"] == {"log_return_per_observation": 1}
    assert volatility["input_column"] == "underlying_volatility"
    segments = [
        row for row in result["groups"][0]["segments"] if row["feature"].startswith("underlying_")
    ]
    assert len(segments) == 3
    assert all(row["bucket"] != "unavailable" for row in segments)
    assert all(row["transition"]["at_risk"] == 1 for row in segments)
    assert all(row["unit"] for row in segments)


def test_flat_underlying_features_reject_future_asof_join():
    with pytest.raises(ValueError, match="future"):
        run(
            [market("a")],
            [event("a", underlying_ts_ns=20_000_000_000, underlying_recent_return=0.1)],
        )


def test_observable_settlement_extends_lifetime_without_partition_leakage():
    straddler = market(
        "straddler",
        start_ns=10 * DAY_NS - 2_000_000_000,
        close_ns=10 * DAY_NS - 1_000_000_000,
        settlement_ns=10 * DAY_NS + 1_000_000_000,
    )
    eligible = market("valid", settlement_ns=301_000_000_000)
    marks = [
        {
            "market_id": "valid",
            "variant": "hold",
            "ts_ns": 301_000_000_000,
            "equity": 3,
            "exposure": 0,
        }
    ]
    result = run([straddler, eligible], equity=marks)
    assert result["exclusions"]["boundary_straddlers"] == 1
    assert result["excluded_markets"][0] == {
        "market_id": "straddler",
        "reason": "economic_lifetime_crosses_partition_boundary",
        "close_ns": 10 * DAY_NS - 1_000_000_000,
        "economic_end_ns": 10 * DAY_NS + 1_000_000_000,
        "crossed_boundaries_ns": [10 * DAY_NS],
    }
    portfolio = result["groups"][0]["variants"][0]["portfolio"]
    assert portfolio["curve"][0]["equity"] == 3


def test_unresolved_post_close_null_mark_preserves_unavailable_portfolio():
    marks = [
        {
            "market_id": "a",
            "variant": "hold",
            "ts_ns": 400_000_000_000,
            "equity": None,
            "exposure": 5,
        }
    ]
    result = run([market("a")], equity=marks)
    assert not result["portfolios"][0]["portfolio"]["available"]


def test_no_signal_rows_not_attempts_and_partial_unresolved_fill_stays_counted():
    rows = [
        {"market_id": "a", "variant": "hold", "filled": False, "entry_attempted": False},
        {
            "market_id": "b",
            "variant": "hold",
            "filled": True,
            "entry_attempted": True,
            "entry_quantity": 2,
            "requested_quantity": 5,
            "net_pnl": None,
            "deployed_capital": 0.6,
        },
        {"market_id": "c", "variant": "hold", "filled": False, "entry_attempted": True},
    ]
    metric = run([market("a"), market("b"), market("c")], trades=rows)["groups"][0]["variants"][0]
    assert metric["attempt_count"] == 2
    assert metric["filled_trade_count"] == 1
    assert metric["attempt_fill_rate"] == 0.5
    assert metric["partial_fill_count"] == 1
    assert metric["unresolved_pnl_count"] == 1
    assert metric["net_pnl_per_eligible"]["markets"] == 3
    assert metric["net_pnl_per_eligible"]["estimate"] is None


def test_actual_synthetic_cli_all_and_analyze_pipeline():
    from prediction_market_extensions.backtesting.longshot.cli import main, read_dataset

    root = Path(__file__).resolve().parents[1]
    output = root / f".longshot-pipeline-test-{uuid4().hex}"
    assert not output.exists()
    options = [
        "--config",
        str(root / "docs/examples/longshot-synthetic.json"),
        "--output",
        str(output),
    ]
    try:
        main(["all", *options])
        first_analysis = json.loads((output / "analysis.json").read_text())
        main(["analyze", *options])
        analysis = json.loads((output / "analysis.json").read_text())
        assert analysis == first_analysis
        audits = read_dataset(output / "market_audit.csv")
        events = read_dataset(output / "events.csv")
        trades = read_dataset(output / "trades.csv")
        attributed_equity = read_dataset(output / "market_equity.csv")
        assert len(audits) == 8
        assert events
        assert len(trades) == 48
        assert len({(row["market_id"], row["variant"]) for row in trades}) == 48
        assert attributed_equity and all("market_id" in row for row in attributed_equity)
        assert {group["partition"] for group in analysis["groups"]} == {
            "discovery",
            "validation",
            "final",
        }
        variants = [variant for group in analysis["groups"] for variant in group["variants"]]
        assert len({variant["variant"] for variant in variants}) == 6
        assert sum(variant["filled_trade_count"] for variant in variants) > 0
        assert sum(variant["unresolved_pnl_count"] for variant in variants) > 0
        assert all(variant["net_pnl_per_eligible"]["estimate"] is None for variant in variants)
        assert all(
            transition["probability"]["estimate"] is None
            for group in analysis["groups"]
            for transition in group["transitions"].values()
        )
        assert analysis["exclusions"]["outside_final_window"] == 0
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["synthetic"] and manifest["dataset_sha256"]
        assert "market_equity.csv" in manifest["dataset_sha256"]
        json.dumps(analysis, allow_nan=False)
    finally:
        if output.exists():
            shutil.rmtree(output)
