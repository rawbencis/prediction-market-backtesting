"""Chronological, market-clustered descriptive analysis of longshot replays.

Day blocks are assigned by market start (UTC); a sampled block retains every
observation belonging to its markets, including eligible markets with no entry.
All comparisons are observational, not estimates of a causal treatment effect.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from itertools import pairwise

DAY_NS = 86_400_000_000_000
PARTITIONS = ("discovery", "validation", "final")
STATE_BUCKETS = {
    "remaining_fraction": (0.25, 0.5, 0.75),
    "entry_vwap": (0.1, 0.2, 0.3),
    "entry_spread": (0.01, 0.03, 0.05),
    "entry_depth": (10.0, 100.0, 1000.0),
}
UNDERLYING_NAMES = ("distance_to_reference", "reference_volatility", "underlying_return")
UNDERLYING_COLUMNS = {
    "distance_to_reference": ("underlying_distance_to_reference", "fraction_of_reference"),
    "reference_volatility": ("underlying_volatility", "log_return_per_observation"),
    "underlying_return": ("underlying_recent_return", "simple_return"),
}
UNDERLYING_BUCKETS = {
    "underlying_distance_to_reference": (-0.02, -0.005, 0.0, 0.005, 0.02),
    "underlying_volatility": (0.001, 0.005, 0.02, 0.05),
    "underlying_recent_return": (-0.02, -0.005, 0.0, 0.005, 0.02),
}


def _underlying_summary(rows: list[dict]) -> dict:
    result = {}
    for name in UNDERLYING_NAMES:
        column, unit = UNDERLYING_COLUMNS[name]
        available = []
        unit_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            flat = row.get(column)
            if flat is not None:
                available.append(row)
                unit_counts[unit] += 1
        result[name] = {
            "available": bool(available),
            "available_observations": len(available),
            "unavailable_observations": len(rows) - len(available),
            "input_column": column,
            "unit_observation_counts": dict(unit_counts),
            "reason": None
            if available
            else "synchronized causal underlying features not supplied or unavailable",
        }
        if name == "distance_to_reference":
            result[name]["interpretation"] = (
                "fractional distance to explicitly supplied causal reference; "
                "no strike or reference is inferred"
            )
    return result


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be finite numeric data")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _estimate(
    numerator: float,
    denominator: float,
    blocks: list[tuple[float, float]],
    *,
    markets: int,
    min_days: int,
    min_markets: int,
    samples: int,
    rng: random.Random,
    informative_days: int | None = None,
    outcome_complete: bool = True,
) -> dict:
    if informative_days is None:
        informative_days = len(blocks)
    sufficient = (
        informative_days >= min_days
        and markets >= min_markets
        and denominator > 0
        and outcome_complete
    )
    result = {
        "estimate": _ratio(numerator, denominator) if sufficient else None,
        "ci95": None,
        "days": len(blocks),
        "informative_days": informative_days,
        "markets": markets,
        "sufficient": sufficient,
        "outcome_complete": outcome_complete,
        "bootstrap_valid_samples": 0,
        "bootstrap_samples": samples,
    }
    if not sufficient:
        return result
    draws = []
    for _ in range(samples):
        selected = rng.choices(blocks, k=len(blocks))
        value = _ratio(sum(row[0] for row in selected), sum(row[1] for row in selected))
        if value is not None:
            draws.append(value)
    result["bootstrap_valid_samples"] = len(draws)
    # Do not silently condition a sparse bootstrap on its few nonempty draws.
    if len(draws) >= max(2, math.ceil(samples * 0.95)):
        result["ci95"] = [_quantile(draws, 0.025), _quantile(draws, 0.975)]
    return result


def _partition(start: int, close: int, ends: tuple[int, int, int]) -> str | None:
    lower = None
    for name, upper in zip(PARTITIONS, ends, strict=True):
        if (lower is None or start >= lower) and close <= upper:
            return name
        lower = upper
    return None


def _bucket(value: object, edges: tuple[float, ...]) -> str:
    if value is None:
        return "unavailable"
    number = _number(value, "state feature")
    for index, edge in enumerate(edges):
        if number < edge:
            return f"[{edges[index - 1] if index else '-inf'},{edge})"
    return f"[{edges[-1]},inf)"


def _transition(
    rows: list[dict],
    days: list[int],
    market_days: dict[str, int],
    options: dict,
    rng: random.Random,
) -> dict:
    observed = [row for row in rows if row.get("next_touch") is not None]
    unknown = [row for row in rows if row.get("next_touch") is None]
    flips = sum(row["next_touch"] is True for row in observed)
    daily: dict[int, list[float]] = {day: [0.0, 0.0] for day in days}
    for row in observed:
        block = daily[market_days[row["market_id"]]]
        block[0] += row["next_touch"] is True
        block[1] += 1
    return {
        "at_risk": len(rows),
        "observed": len(observed),
        "next_touch_count": flips,
        "completed_non_flips": len(observed) - flips,
        "censored": sum(row["_censored"] for row in unknown),
        "missing_observations": sum(not row["_censored"] for row in unknown),
        "probability": _estimate(
            flips,
            len(observed),
            [tuple(daily[day]) for day in days],
            markets=len({row["market_id"] for row in observed}),
            informative_days=len({market_days[row["market_id"]] for row in observed}),
            rng=rng,
            **options,
        ),
        "time_to_next_touch_ns": _time_summary(
            [row for row in observed if row["next_touch"] is True],
            "time_to_next_touch_ns",
            market_days,
            options,
        ),
        "time_between_touches_ns": _time_summary(
            rows, "time_between_touches_ns", market_days, options
        ),
    }


def _time_summary(rows: list[dict], field: str, market_days: dict, options: dict) -> dict:
    known = [row for row in rows if row.get(field) is not None]
    enough = (
        len({row["market_id"] for row in known}) >= options["min_markets"]
        and len({market_days[row["market_id"]] for row in known}) >= options["min_days"]
    )
    values = [float(row[field]) for row in known]
    return {
        "observed": len(known),
        "missing": len(rows) - len(known),
        "median": _quantile(values, 0.5) if enough else None,
        "p25": _quantile(values, 0.25) if enough else None,
        "p75": _quantile(values, 0.75) if enough else None,
        "sufficient": enough,
    }


def _portfolio(
    rows: list[dict], markets: list[dict], required_markets: set[str] | None = None
) -> dict:
    missing_marks = any(row["equity"] is None or row["exposure"] is None for row in rows)
    if required_markets:
        missing_marks |= not required_markets.issubset({row["market_id"] for row in rows})
    if not rows or missing_marks:
        return {
            "available": False,
            "reason": "missing causal valuation marks" if missing_marks else "no equity supplied",
            "max_drawdown": None,
            "max_exposure": None,
            "time_weighted_exposure": None,
            "curve": [],
        }
    updates: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        updates[row["ts_ns"]].append(row)
    marks: dict[str, tuple[float, float]] = {}
    start = min(row["start_ns"] for row in markets)
    end = max(row["_analysis_end_ns"] for row in markets)
    peak = drawdown = max_exposure = integrated = 0.0
    previous_ts = start
    previous_exposure = 0.0
    curve = []
    for timestamp in sorted(updates):
        integrated += previous_exposure * (timestamp - previous_ts)
        for row in updates[timestamp]:
            marks[row["market_id"]] = (row["equity"], row["exposure"])
        pnl = sum(mark[0] for mark in marks.values())
        exposure = sum(mark[1] for mark in marks.values())
        peak = max(peak, pnl)
        drawdown = max(drawdown, peak - pnl)
        max_exposure = max(max_exposure, exposure)
        curve.append({"ts_ns": timestamp, "equity": pnl, "exposure": exposure})
        previous_ts, previous_exposure = timestamp, exposure
    integrated += previous_exposure * (end - previous_ts)
    return {
        "available": True,
        "max_drawdown": drawdown,
        "max_exposure": max_exposure,
        "time_weighted_exposure": _ratio(integrated, end - start),
        "curve": curve,
        "equity_convention": "sum of forward-filled per-market cumulative net MTM PnL",
    }


def _variant(
    name: str,
    markets: list[dict],
    rows: list[dict],
    equity: list[dict],
    options: dict,
    rng: random.Random,
) -> dict:
    eligible_ids = {row["market_id"] for row in markets}
    market_days = {row["market_id"]: row["start_ns"] // DAY_NS for row in markets}
    days = sorted(set(market_days.values()))
    filled = [row for row in rows if row["filled"]]
    unresolved = [row for row in filled if row["net_pnl"] is None]
    attempted = [row for row in rows if row.get("entry_attempted", True)]
    quantity_known = [
        row
        for row in filled
        if row.get("entry_quantity") is not None and row.get("requested_quantity") is not None
    ]
    filled_ids = {row["market_id"] for row in filled}
    daily = {day: [0.0, 0.0, 0.0] for day in days}
    for market in markets:
        daily[market_days[market["market_id"]]][1] += 1
    per_market = {
        market_id: {"filled_trades": 0, "net_pnl": 0.0, "deployed_capital": 0.0}
        for market_id in sorted(eligible_ids)
    }
    for row in filled:
        market = per_market[row["market_id"]]
        market["filled_trades"] += 1
        if row["net_pnl"] is None or market["net_pnl"] is None:
            market["net_pnl"] = None
        else:
            market["net_pnl"] += row["net_pnl"]
        market["deployed_capital"] += row["deployed_capital"]
        block = daily[market_days[row["market_id"]]]
        if row["net_pnl"] is not None:
            block[0] += row["net_pnl"]
        block[2] += row["deployed_capital"]
    pnl = sum(row["net_pnl"] for row in filled if row["net_pnl"] is not None)
    deployed = sum(row["deployed_capital"] for row in filled)
    sufficient = len(days) >= options["min_days"] and len(markets) >= options["min_markets"]
    filled_days = len({market_days[row["market_id"]] for row in filled})
    known_next = [row for row in filled if row.get("next_touch") is not None]
    return {
        "variant": name,
        "eligible_markets": len(markets),
        "attempt_count": len(attempted),
        "filled_trade_count": len(filled),
        "unresolved_pnl_count": len(unresolved),
        "partial_fill_count": (
            sum(row["entry_quantity"] < row["requested_quantity"] for row in quantity_known)
            if quantity_known
            else None
        ),
        "partial_fill_unknown_count": len(filled) - len(quantity_known),
        "filled_markets": len(filled_ids),
        "fill_rate": _ratio(len(filled_ids), len(markets)) if sufficient else None,
        "attempt_fill_rate": _ratio(len(filled), len(attempted)) if sufficient else None,
        "next_touch": {
            "observed": len(known_next),
            "missing_or_censored": len(filled) - len(known_next),
            "count": sum(row["next_touch"] is True for row in known_next),
            "rate": (
                _ratio(sum(row["next_touch"] is True for row in known_next), len(known_next))
                if sufficient
                and len({row["market_id"] for row in known_next}) >= options["min_markets"]
                and len({market_days[row["market_id"]] for row in known_next})
                >= options["min_days"]
                else None
            ),
        },
        "net_pnl": pnl if sufficient and not unresolved else None,
        "net_pnl_per_trade": (
            _ratio(pnl, len(filled))
            if sufficient
            and len(filled_ids) >= options["min_markets"]
            and filled_days >= options["min_days"]
            and not unresolved
            else None
        ),
        "net_pnl_per_eligible": _estimate(
            pnl,
            len(markets),
            [(daily[day][0], daily[day][1]) for day in days],
            markets=len(markets),
            outcome_complete=not unresolved,
            rng=rng,
            **options,
        ),
        "net_deployed_return": _estimate(
            pnl,
            deployed,
            [(daily[day][0], daily[day][2]) for day in days],
            markets=len(filled_ids),
            outcome_complete=not unresolved,
            informative_days=filled_days,
            rng=rng,
            **options,
        ),
        "deployed_capital": deployed,
        "per_market": [{"market_id": key, **value} for key, value in per_market.items()],
        "portfolio": _portfolio(equity, markets, filled_ids),
    }


def analyze(
    audits: list[dict],
    events: list[dict],
    trades: list[dict] = (),
    equity: list[dict] = (),
    *,
    discovery_end_ns: int,
    validation_end_ns: int,
    final_end_ns: int,
    seed: int = 70,
    bootstrap_samples: int = 1000,
    min_days: int = 5,
    min_markets: int = 20,
) -> dict:
    """Analyze prespecified chronological partitions without fitting on final data.

    Audits are flat market records with eligibility/completion/censor flags.
    Trades require ``market_id, variant, filled`` and, if filled,
    ``net_pnl, deployed_capital`` (null PnL means unresolved). Equity requires
    ``market_id, variant, ts_ns,
    equity, exposure``; equity is cumulative net market MTM PnL, not account NAV.
    Missing transition outcomes remain unknown, even for completed markets.
    Null equity marks invalidate portfolio estimates, never becoming zero marks.
    """
    ends = tuple(
        _integer(value, name)
        for name, value in (
            ("discovery_end_ns", discovery_end_ns),
            ("validation_end_ns", validation_end_ns),
            ("final_end_ns", final_end_ns),
        )
    )
    if not ends[0] < ends[1] < ends[2]:
        raise ValueError("partition boundaries must be strictly chronological")
    _integer(seed, "seed")
    options = {
        "samples": _integer(bootstrap_samples, "bootstrap_samples", minimum=2),
        "min_days": _integer(min_days, "min_days", minimum=1),
        "min_markets": _integer(min_markets, "min_markets", minimum=1),
    }
    rng = random.Random(seed)
    indexed: dict[str, dict] = {}
    exclusions = {"boundary_straddlers": 0, "outside_final_window": 0, "ineligible": 0}
    excluded_markets = []
    grouped: dict[tuple[str, str, float], list[dict]] = defaultdict(list)
    for source in audits:
        row = dict(source)
        for flag in ("eligible", "complete", "censored"):
            if not isinstance(row.get(flag), bool):
                raise TypeError(f"audit {flag} must be boolean")
        market_id = row.get("market_id")
        if not row["eligible"] and (not isinstance(market_id, str) or not market_id):
            exclusions["ineligible"] += 1
            excluded_markets.append({"market_id": None, "reason": "structurally_ineligible"})
            continue
        if not isinstance(market_id, str) or not market_id or market_id in indexed:
            raise ValueError("audits require unique nonempty string market_id")
        if not row["eligible"]:
            row["_partition"] = None
            indexed[market_id] = row
            exclusions["ineligible"] += 1
            excluded_markets.append({"market_id": market_id, "reason": "structurally_ineligible"})
            continue
        start = _integer(row["start_ns"], "start_ns")
        close = _integer(row["close_ns"], "close_ns")
        if close <= start:
            raise ValueError("market close_ns must exceed start_ns")
        settlement = row.get("settlement_ns")
        if settlement is not None:
            settlement = _integer(settlement, "settlement_ns", minimum=close)
        accounting_end = settlement if settlement is not None else close
        row["_analysis_end_ns"] = accounting_end
        duration = _number(row["duration_seconds"], "duration_seconds")
        if duration <= 0:
            raise ValueError("duration_seconds must be positive")
        if not isinstance(row["asset"], str) or not row["asset"]:
            raise ValueError("asset must be a nonempty string")
        partition = _partition(start, accounting_end, ends)
        row["_partition"] = partition
        indexed[market_id] = row
        if partition is None:
            crossed = [boundary for boundary in ends if start < boundary < accounting_end]
            if crossed:
                exclusions["boundary_straddlers"] += 1
                reason = "economic_lifetime_crosses_partition_boundary"
            else:
                exclusions["outside_final_window"] += 1
                reason = "outside_final_window"
            excluded_markets.append(
                {
                    "market_id": market_id,
                    "reason": reason,
                    "close_ns": close,
                    "economic_end_ns": accounting_end,
                    "crossed_boundaries_ns": crossed,
                }
            )
        else:
            grouped[(partition, row["asset"], duration)].append(row)
    accepted_events: dict[str, list[dict]] = defaultdict(list)
    event_keys = set()
    for source in events:
        row = dict(source)
        market = indexed.get(row.get("market_id"))
        if market is None:
            raise ValueError("event references unknown market")
        if not market["eligible"] or market["_partition"] is None:
            continue
        touch = _integer(row["touch_number"], "touch_number", minimum=1)
        key = (row["market_id"], touch)
        if key in event_keys:
            raise ValueError("duplicate touch_number for market")
        event_keys.add(key)
        timestamp = _integer(row["ts_ns"], "event ts_ns")
        if not market["start_ns"] <= timestamp <= market["close_ns"]:
            raise ValueError("event timestamp lies outside market lifetime")
        if row.get("next_touch") is not None and type(row["next_touch"]) is not bool:
            raise ValueError("next_touch must be true, false, or null")
        if row.get("next_touch") is False and (market["censored"] or not market["complete"]):
            row["next_touch"] = None
        for feature in (*STATE_BUCKETS, "quantity"):
            if row.get(feature) is not None:
                value = _number(row[feature], feature)
                if value < 0 or (feature == "remaining_fraction" and value > 1):
                    raise ValueError(f"invalid {feature}")
        for feature in ("time_to_next_touch_ns", "time_between_touches_ns"):
            if row.get(feature) is not None:
                _integer(row[feature], feature, minimum=0)
        supplied_underlying = [
            column for column, _unit in UNDERLYING_COLUMNS.values() if row.get(column) is not None
        ]
        if supplied_underlying:
            asof = _integer(row.get("underlying_ts_ns"), "underlying_ts_ns")
            if asof > timestamp:
                raise ValueError("underlying features cannot use future observations")
            for column in supplied_underlying:
                _number(row[column], column)
            if row.get("underlying_volatility") is not None and row["underlying_volatility"] < 0:
                raise ValueError("underlying volatility must be nonnegative")
        row["_censored"] = market["censored"]
        row["executable_entry"] = (
            float(
                row["quantity"] > 0
                and row["entry_depth"] >= row["quantity"]
                and row["entry_vwap"] > 0
            )
            if all(row.get(key) is not None for key in ("quantity", "entry_depth", "entry_vwap"))
            else None
        )
        accepted_events[row["market_id"]].append(row)
    for rows in accepted_events.values():
        ordered = sorted(rows, key=lambda row: row["touch_number"])
        if any(left["ts_ns"] >= right["ts_ns"] for left, right in pairwise(ordered)):
            raise ValueError("touch history must be strictly chronological")
    accepted_trades: dict[str, list[dict]] = defaultdict(list)
    accepted_equity: dict[str, list[dict]] = defaultdict(list)
    variants = set()
    for source in trades:
        row = dict(source)
        if row.get("market_id") not in indexed:
            raise ValueError("trade references unknown market")
        market = indexed[row["market_id"]]
        if not market["eligible"] or market["_partition"] is None:
            continue
        if not isinstance(row.get("variant"), str) or not row["variant"]:
            raise ValueError("trade variant must be nonempty string")
        variants.add(row["variant"])
        if type(row.get("filled")) is not bool:
            raise ValueError("trade filled must be boolean")
        if "entry_attempted" in row:
            if type(row["entry_attempted"]) is not bool:
                raise TypeError("entry_attempted must be boolean")
            if row["filled"] and not row["entry_attempted"]:
                raise ValueError("filled trade must have an entry attempt")
        for field in ("entry_quantity", "requested_quantity"):
            if row.get(field) is not None:
                value = _number(row[field], field)
                if value < 0 or (field == "requested_quantity" and value == 0):
                    raise ValueError(f"invalid {field}")
        if row["filled"] and row.get("entry_quantity") == 0:
            raise ValueError("filled entry_quantity must be positive")
        if row.get("next_touch") is not None and type(row["next_touch"]) is not bool:
            raise ValueError("trade next_touch must be true, false, or null")
        if row["filled"]:
            if row.get("net_pnl") is not None:
                row["net_pnl"] = _number(row["net_pnl"], "net_pnl")
            else:
                row["net_pnl"] = None
            row["deployed_capital"] = _number(row.get("deployed_capital"), "deployed_capital")
            if row["deployed_capital"] <= 0:
                raise ValueError("filled trade deployed_capital must be positive")
        accepted_trades[row["market_id"]].append(row)
    equity_keys = set()
    for source in equity:
        row = dict(source)
        market = indexed.get(row.get("market_id"))
        if market is None:
            raise ValueError("equity references unknown market")
        if not market["eligible"] or market["_partition"] is None:
            continue
        if not isinstance(row.get("variant"), str) or not row["variant"]:
            raise ValueError("equity variant must be nonempty string")
        variants.add(row["variant"])
        timestamp = _integer(row["ts_ns"], "equity ts_ns")
        if timestamp < market["start_ns"]:
            raise ValueError("equity timestamp lies outside market lifetime")
        if (
            timestamp > market["_analysis_end_ns"]
            and row.get("equity") is not None
            and row.get("exposure") is not None
        ):
            raise ValueError("equity timestamp lies outside market lifetime")
        key = (row["market_id"], row["variant"], timestamp)
        if key in equity_keys:
            raise ValueError("duplicate market/variant equity timestamp")
        equity_keys.add(key)
        for field in ("equity", "exposure"):
            row[field] = _number(row[field], field) if row.get(field) is not None else None
        if row["exposure"] is not None and row["exposure"] < 0:
            raise ValueError("exposure must be nonnegative")
        accepted_equity[row["market_id"]].append(row)
    groups = []
    for partition, asset, duration in sorted(
        grouped, key=lambda key: (PARTITIONS.index(key[0]), *key[1:])
    ):
        markets = sorted(grouped[(partition, asset, duration)], key=lambda row: row["market_id"])
        market_days = {row["market_id"]: row["start_ns"] // DAY_NS for row in markets}
        days = sorted(set(market_days.values()))
        rows = [event for market in markets for event in accepted_events[market["market_id"]]]
        transitions = {
            f"p{touch}{touch + 1}": _transition(
                [row for row in rows if row["touch_number"] == touch],
                days,
                market_days,
                options,
                rng,
            )
            for touch in (1, 2)
        }
        segments = []
        comparisons: dict[tuple[str, str], dict[int, list[dict]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for feature, edges in {
            **STATE_BUCKETS,
            "executable_entry": (0.5,),
            **UNDERLYING_BUCKETS,
        }.items():
            buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
            for row in rows:
                if feature in UNDERLYING_BUCKETS and row.get(feature) is None:
                    continue
                label = _bucket(row.get(feature), edges)
                buckets[(label, row["touch_number"])].append(row)
                comparisons[(feature, label)][row["touch_number"]].append(row)
            for (label, touch), observations in sorted(buckets.items()):
                segments.append(
                    {
                        "feature": feature,
                        "bucket": label,
                        "touch_number": touch,
                        "unit": next(
                            (
                                unit
                                for column, unit in UNDERLYING_COLUMNS.values()
                                if column == feature
                            ),
                            None,
                        ),
                        "transition": _transition(observations, days, market_days, options, rng),
                    }
                )
        history = []
        for (feature, label), histories in sorted(comparisons.items()):
            shared = len(histories) >= 2 and label != "unavailable"
            estimates = []
            for touch, observations in sorted(histories.items()):
                statistic = _transition(observations, days, market_days, options, rng)
                estimates.append({"touch_number": touch, **statistic})
            sufficient = shared and all(item["probability"]["sufficient"] for item in estimates)
            history.append(
                {
                    "feature": feature,
                    "bucket": label,
                    "sufficient": sufficient,
                    "overlap_warning": (
                        "Marginal bucket overlap does not establish joint-state overlap; "
                        "touch histories select different surviving markets."
                        if shared
                        else "No observed touch-history overlap in this bucket."
                    ),
                    "histories": estimates,
                    "interpretation": "observational, not causal; no final-partition optimization",
                }
            )
        group_trades = [row for market in markets for row in accepted_trades[market["market_id"]]]
        group_equity = [row for market in markets for row in accepted_equity[market["market_id"]]]
        groups.append(
            {
                "partition": partition,
                "asset": asset,
                "duration_seconds": duration,
                "eligible_markets": len(markets),
                "complete_markets": sum(row["complete"] for row in markets),
                "censored_markets": sum(row["censored"] for row in markets),
                "zero_touch_markets": sum(not accepted_events[row["market_id"]] for row in markets),
                "days": len(days),
                "transitions": transitions,
                "segments": segments,
                "touch_history_comparisons": history,
                "underlying_features": _underlying_summary(rows),
                "variants": [
                    _variant(
                        name,
                        markets,
                        [row for row in group_trades if row["variant"] == name],
                        [row for row in group_equity if row["variant"] == name],
                        options,
                        rng,
                    )
                    for name in sorted(variants)
                ],
            }
        )
    portfolios = []
    for partition in PARTITIONS:
        markets = [
            market for key, members in grouped.items() if key[0] == partition for market in members
        ]
        if not markets:
            continue
        rows = [row for market in markets for row in accepted_equity[market["market_id"]]]
        for variant in sorted(variants):
            portfolios.append(
                {
                    "partition": partition,
                    "variant": variant,
                    "eligible_markets": len(markets),
                    "portfolio": _portfolio(
                        [row for row in rows if row["variant"] == variant],
                        markets,
                        {
                            row["market_id"]
                            for market in markets
                            for row in accepted_trades[market["market_id"]]
                            if row["variant"] == variant and row["filled"]
                        },
                    ),
                }
            )
    return {
        "schema_version": 1,
        "partition_boundaries_ns": dict(zip(PARTITIONS, ends, strict=True)),
        "partition_rule": (
            "whole trading and accounting lifetime; start >= lower boundary and "
            "max(close, supplied observable settlement_ns) <= upper boundary"
        ),
        "bootstrap": {
            "method": "seeded UTC market-start-day block percentile bootstrap",
            "seed": seed,
            "samples": bootstrap_samples,
            "min_days": min_days,
            "min_markets": min_markets,
            "zero_entry_days_retained": True,
        },
        "exclusions": exclusions,
        "excluded_markets": excluded_markets,
        "groups": groups,
        "portfolios": portfolios,
        "underlying_features": _underlying_summary(
            [
                event
                for members in grouped.values()
                for market in members
                for event in accepted_events[market["market_id"]]
            ]
        ),
        "warnings": [
            "Observational comparisons are not causal effects.",
            "Buckets are prespecified; no parameters are optimized on final data.",
            "Estimates and CIs are null below configured minimum day/market counts.",
            "Missing next-touch observations are not classified as non-reversals.",
            "Non-flips on incomplete or censored markets remain unobserved.",
            "Unresolved filled-trade PnL invalidates PnL estimates without dropping denominators.",
            "Partial-fill classification requires both requested and filled quantities.",
            "Supplied settlement observation times extend partition accounting lifetimes.",
            "Exposure curves require synchronized causal net MTM marks; missing curves are unavailable.",
        ],
    }
