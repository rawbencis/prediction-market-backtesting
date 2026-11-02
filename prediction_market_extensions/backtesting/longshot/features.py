from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from itertools import pairwise
from statistics import pstdev
from typing import Any


def attach_underlying_features(
    events: Sequence[dict[str, Any]],
    markets: Sequence[dict[str, Any]],
    underlying: dict[str, list[dict[str, Any]]],
    *,
    max_age_ns: int = 1_000_000_000,
    lookback_ns: int = 60_000_000_000,
) -> None:
    """Backward-only as-of join; volatility is unannualized log-return dispersion."""
    if max_age_ns < 0 or lookback_ns <= 0:
        raise ValueError("Underlying max age must be nonnegative and lookback positive")
    inventory = {market["market_id"]: market for market in markets}
    prepared = {}
    for asset, records in underlying.items():
        timestamps, prices = [], []
        for record in records:
            timestamp, price = record["ts_ns"], float(record["price"])
            if (
                not isinstance(timestamp, int)
                or timestamp < 0
                or not math.isfinite(price)
                or price <= 0
                or (timestamps and timestamp <= timestamps[-1])
            ):
                raise ValueError(
                    "Underlying records require ordered unique timestamps and positive prices"
                )
            timestamps.append(timestamp)
            prices.append(price)
        prepared[asset] = timestamps, prices
    for event in events:
        event.update(
            underlying_distance_to_reference=None,
            underlying_volatility=None,
            underlying_recent_return=None,
            underlying_ts_ns=None,
            underlying_unavailable_reason="synchronized underlying input unavailable",
        )
        timestamps, prices = prepared.get(event["asset"], ([], []))
        end = bisect_right(timestamps, event["ts_ns"]) - 1
        if end < 0 or event["ts_ns"] - timestamps[end] > max_age_ns:
            continue
        event["underlying_ts_ns"] = timestamps[end]
        event["underlying_unavailable_reason"] = None
        market = inventory[event["market_id"]]
        reference, reference_ns = market.get("reference_price"), market.get("reference_ns")
        if (
            reference is not None
            and math.isfinite(float(reference))
            and float(reference) > 0
            and isinstance(reference_ns, int)
            and reference_ns <= event["ts_ns"]
        ):
            event["underlying_distance_to_reference"] = prices[end] / float(reference) - 1
        start = bisect_right(timestamps, event["ts_ns"] - lookback_ns) - 1
        if start >= 0 and event["ts_ns"] - lookback_ns - timestamps[start] <= max_age_ns:
            window = prices[start : end + 1]
            gaps = [
                right - left
                for left, right in zip(
                    timestamps[start:end], timestamps[start + 1 : end + 1], strict=True
                )
            ]
            if gaps and all(gap <= max_age_ns for gap in gaps):
                event["underlying_recent_return"] = prices[end] / prices[start] - 1
                returns = [math.log(right / left) for left, right in pairwise(window)]
                if len(returns) >= 2:
                    event["underlying_volatility"] = pstdev(returns)
