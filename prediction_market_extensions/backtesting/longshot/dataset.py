"""Auditable descriptive labels, not an execution or fill simulator."""

from collections import Counter
from collections.abc import Sequence
from math import isclose, isfinite

from .state import DEFAULT_MAX_GAP_NS, DEFAULT_MAX_STALE_NS, PairObservation, TouchState


def _covers_timestamp(obs: PairObservation, ts_ns: int, max_stale_ns: int) -> bool:
    return (
        not obs.gap
        and obs.up.valid
        and obs.down.valid
        and not obs.up.is_stale(ts_ns, max_stale_ns)
        and not obs.down.is_stale(ts_ns, max_stale_ns)
    )


def _observed_target(
    event: dict,
    future: Sequence[PairObservation],
    *,
    complete: bool,
    close_ns: int,
    max_gap_ns: int,
    max_stale_ns: int,
) -> dict:
    future = [obs for obs in future if obs.ts_ns < close_ns]
    evaluable = complete and bool(future)
    previous_ts = event["ts_ns"]
    first_ts = None
    for obs in future:
        valid = (
            not obs.gap
            and previous_ts < obs.ts_ns
            and obs.ts_ns - previous_ts <= max_gap_ns
            and obs.up.valid
            and obs.down.valid
            and not obs.up.is_stale(obs.ts_ns, max_stale_ns)
            and not obs.down.is_stale(obs.ts_ns, max_stale_ns)
            and not (
                obs.up.midpoint >= event["threshold"] and obs.down.midpoint >= event["threshold"]
            )
        )
        previous_ts = obs.ts_ns
        if not valid:
            evaluable = False
            break
        book = obs.book(event["cheap_side"])
        if book.depth_at_price("bid", event["threshold"]) >= event["quantity"]:
            first_ts = obs.ts_ns
            break
        if book.depth("bid") < event["quantity"]:
            evaluable = False
    return {
        "executable_target": True if first_ts is not None else (False if evaluable else None),
        "target_first_ts_ns": first_ts,
        "time_to_target_ns": first_ts - event["ts_ns"] if first_ts is not None else None,
        "target_label_type": "observable_quantity_bid_depth_at_threshold_not_a_fill",
        "target_price_floor": event["threshold"],
        "target_quantity": event["quantity"],
    }


def extract_market(
    market: dict,
    observations: Sequence[PairObservation],
    *,
    threshold: float = 0.70,
    quantity: float = 5,
    max_gap_ns: int = DEFAULT_MAX_GAP_NS,
    max_stale_ns: int = DEFAULT_MAX_STALE_NS,
) -> tuple[dict, list[dict]]:
    if not isfinite(quantity) or quantity <= 0:
        raise ValueError("quantity must be finite and positive")
    missing = [
        key
        for key in ("market_id", "asset", "duration_seconds", "start_ns", "close_ns", "outcomes")
        if market.get(key) is None
    ]
    outcomes = market.get("outcomes") or {}
    mapping_valid = (
        set(outcomes) == {"UP", "DOWN"}
        and all(isinstance(v, str) and v for v in outcomes.values())
        and outcomes.get("UP") != outcomes.get("DOWN")
    )
    winner, settlement_ns = market.get("settlement_winner"), market.get("settlement_ns")
    settlement_valid = (
        winner in ("UP", "DOWN")
        and isinstance(settlement_ns, int)
        and isinstance(market.get("close_ns"), int)
        and settlement_ns >= market["close_ns"]
    )
    exclusions = []
    if missing:
        exclusions.append("missing_metadata")
    if not mapping_valid:
        exclusions.append("invalid_outcome_mapping")
    window_valid = (
        isinstance(market.get("start_ns"), int)
        and isinstance(market.get("close_ns"), int)
        and 0 <= market["start_ns"] < market["close_ns"]
    )
    if not window_valid:
        exclusions.append("invalid_window")
    duration = market.get("duration_seconds")
    duration_valid = (
        window_valid
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and isfinite(duration)
        and duration > 0
        and isclose(
            duration,
            (market["close_ns"] - market["start_ns"]) / 1_000_000_000,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    )
    if not duration_valid:
        exclusions.append("invalid_duration")
    selected = [
        obs
        for obs in observations
        if window_valid and market["start_ns"] <= obs.ts_ns <= market["close_ns"]
    ]
    audit = {
        **market,
        "market_id": market.get("market_id"),
        "asset": market.get("asset"),
        "duration_seconds": market.get("duration_seconds"),
        "start_ns": market.get("start_ns"),
        "close_ns": market.get("close_ns"),
        "outcomes": outcomes,
        "mapping_valid": mapping_valid,
        "duration_valid": duration_valid,
        "missing_metadata": missing,
        "missing_settlement": not settlement_valid,
        "settlement_winner": winner if settlement_valid else None,
        "settlement_ns": settlement_ns if settlement_valid else None,
        "observation_count": len(selected),
        "input_observation_count": len(observations),
        "coverage_start_ns": selected[0].ts_ns if selected else None,
        "coverage_end_ns": selected[-1].ts_ns if selected else None,
        "explicit_gap_count": sum(obs.gap for obs in selected),
    }
    state = (
        TouchState(threshold, market["start_ns"], market["close_ns"], max_gap_ns, max_stale_ns)
        if window_valid
        else None
    )
    events = []
    invalid = Counter()
    gap_count = 0
    for index, obs in enumerate(selected):
        if index and obs.ts_ns - selected[index - 1].ts_ns > max_gap_ns:
            gap_count += 1
        for side in ("UP", "DOWN"):
            book = obs.book(side)
            invalid.update(f"{side}:{r}" for r in book.invalid_reasons)
            if book.is_stale(obs.ts_ns, max_stale_ns):
                invalid.update([f"{side}:stale_book"])
        touch = state.observe(obs)
        if touch is None or exclusions:
            continue
        cheap = obs.book(touch.cheap_side)
        ask = cheap.vwap("ask", quantity)
        events.append(
            {
                "market_id": market.get("market_id"),
                "asset": market.get("asset"),
                "duration_seconds": market.get("duration_seconds"),
                "touch_number": touch.number,
                "side": touch.side,
                "initial_favorite": touch.initial_favorite,
                "cheap_side": touch.cheap_side,
                "ts_ns": touch.ts_ns,
                "threshold": threshold,
                "crossing_from_midpoint": selected[index - 1].book(touch.side).midpoint,
                "crossing_to_midpoint": obs.book(touch.side).midpoint,
                "crossing_overshoot": obs.book(touch.side).midpoint - threshold,
                "crossing_was_jump": obs.book(touch.side).midpoint > threshold,
                "lifetime_remaining_ns": market["close_ns"] - touch.ts_ns,
                "remaining_fraction": (market["close_ns"] - touch.ts_ns)
                / (market["close_ns"] - market["start_ns"]),
                "time_between_touches_ns": touch.ts_ns - events[-1]["ts_ns"] if events else None,
                "quantity": quantity,
                "entry_vwap": ask,
                "entry_spread": cheap.spread,
                "entry_depth": cheap.depth("ask"),
                "cheap_ask_vwap": ask,
                "cheap_bid_vwap": cheap.vwap("bid", quantity),
                "cheap_spread": cheap.spread,
                "cheap_ask_depth": cheap.depth("ask"),
                "cheap_bid_depth": cheap.depth("bid"),
                "entry_depth_sufficient": ask is not None,
                "threshold_basis": "midpoint",
                "label_type": "descriptive_observed_alternating_midpoint_touch",
                "touch_label": "descriptive_midpoint_threshold_crossing",
                "excursion_basis": "future_quantity_bid_vwap_minus_entry_ask_vwap_per_unit",
                "executable_target": None,
                "settlement_winner": winner if settlement_valid else None,
                "_index": index,
            }
        )
    # observe() skips the close timestamp, so an exact-close row cannot certify
    # the preceding gap or the terminal book. Coverage is measured from the last
    # pre-close observation, and those books must still be valid through close
    # when no later observation replaces them.
    close_ns = market["close_ns"] if window_valid else None
    preclose = [obs for obs in selected if obs.ts_ns < close_ns] if window_valid else []
    closing = [obs for obs in selected if obs.ts_ns == close_ns] if window_valid else []
    end_gap_covered = bool(preclose) and close_ns - preclose[-1].ts_ns <= max_gap_ns
    terminal_rows = closing or preclose[-1:]
    terminal_valid = end_gap_covered and all(
        _covers_timestamp(obs, close_ns, max_stale_ns) for obs in terminal_rows
    )
    end_covered = end_gap_covered and terminal_valid
    complete = bool(state) and not state.censored and end_covered
    if not selected:
        exclusions.append("no_observations")
    if state and state.left_truncated:
        exclusions.append("left_truncated")
    audit.update(
        eligible=not exclusions,
        complete=complete,
        censored=not complete,
        left_truncated=bool(state and state.left_truncated),
        censor_reasons=(state.reasons if state else [])
        + ([] if end_gap_covered else ["missing_end_coverage"])
        + ([] if not end_gap_covered or terminal_valid else ["invalid_terminal_books"]),
        exclusion_flags=exclusions,
        invalid_reasons=dict(invalid),
        invalid_observation_count=sum(
            not obs.up.valid
            or not obs.down.valid
            or obs.up.is_stale(obs.ts_ns, max_stale_ns)
            or obs.down.is_stale(obs.ts_ns, max_stale_ns)
            for obs in selected
        ),
        observation_gap_count=gap_count,
        touch_count=len(events),
        reversing=any(event["touch_number"] >= 2 for event in events),
        no_entry=not events,
    )
    for index, event in enumerate(events):
        following = events[index + 1] if index + 1 < len(events) else None
        event["next_touch"] = True if following else (False if complete else None)
        event["time_to_next_touch_ns"] = following["ts_ns"] - event["ts_ns"] if following else None
        event["time_to_next_touch"] = (
            event["time_to_next_touch_ns"] / 1_000_000_000 if following else None
        )
        future = [
            obs for obs in selected[event.pop("_index") + 1 :] if obs.ts_ns < market["close_ns"]
        ]
        event.update(
            _observed_target(
                event,
                future,
                complete=complete,
                close_ns=market["close_ns"],
                max_gap_ns=max_gap_ns,
                max_stale_ns=max_stale_ns,
            )
        )
        bids = []
        path_complete = complete and bool(future) and event["entry_depth_sufficient"]
        previous_ts = event["ts_ns"]
        for obs in future:
            book = obs.book(event["cheap_side"])
            valid = (
                not obs.gap
                and obs.ts_ns > previous_ts
                and obs.ts_ns - previous_ts <= max_gap_ns
                and obs.up.valid
                and obs.down.valid
                and not obs.up.is_stale(obs.ts_ns, max_stale_ns)
                and not obs.down.is_stale(obs.ts_ns, max_stale_ns)
            )
            previous_ts = obs.ts_ns
            bid = book.vwap("bid", quantity) if valid else None
            if bid is None:
                path_complete = False
            else:
                bids.append(bid)
        event["excursion_incomplete"] = not path_complete
        event["observed_future_bid_vwap_max"] = max(bids) if bids else None
        event["observed_future_bid_vwap_min"] = min(bids) if bids else None
        event["executable_bid_mfe"] = max(bids) - event["cheap_ask_vwap"] if path_complete else None
        event["executable_bid_mae"] = min(bids) - event["cheap_ask_vwap"] if path_complete else None
    return audit, events
