from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.adapters.polymarket.common.symbol import get_polymarket_token_id

from prediction_market_extensions.adapters.prediction_market.replay import ReplayLoadRequest
from prediction_market_extensions.backtesting._market_data_config import MarketDataConfig
from prediction_market_extensions.backtesting._replay_specs import BookReplay
from prediction_market_extensions.backtesting.data_sources.registry import resolve_replay_adapter


def timestamp_ns(value: str) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {value!r}")
    return int(timestamp.tz_convert("UTC").value)


def read_configuration(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise TypeError("Configuration must be a JSON object")
    boundaries = config.get("partitions", {})
    times = [
        timestamp_ns(boundaries[key]) for key in ("discovery_end", "validation_end", "final_end")
    ]
    if not times[0] < times[1] < times[2]:
        raise ValueError("Chronological partition boundaries must be strictly increasing")
    data = config.get("data", {})
    if data.get("vendor") not in {"synthetic", "pmxt", "telonex", "sqlite_capture"}:
        raise ValueError("data.vendor must be synthetic, pmxt, telonex or sqlite_capture")
    sources = data.get("sources", [])
    if data["vendor"] != "synthetic" and not sources:
        raise ValueError("Explicit vendor sources are required; no server path is assumed")
    for source in sources:
        if not isinstance(source, str) or not source.startswith(("local:", "archive:", "api:")):
            raise ValueError("Sources must use the existing local:, archive: or api: syntax")
        if "@" in source or "?" in source or "#" in source:
            raise ValueError("Do not put credentials or signed URLs in configuration")
        if source.startswith("api:") and source != "api:TELONEX_API_KEY":
            raise ValueError("Use api:TELONEX_API_KEY, never a literal API credential")
    if data["vendor"] == "sqlite_capture":
        if len(sources) != 1 or not sources[0].startswith("local:"):
            raise ValueError("SQLite capture requires exactly one local: source")
        if not Path(sources[0][len("local:") :]).is_absolute():
            raise ValueError("SQLite capture source must be an explicit absolute path")
    for market in config.get("markets", []):
        for field in ("start", "close", "settlement"):
            if market.get(field):
                market[f"{field}_ns"] = timestamp_ns(market[field])
    return config


def select_markets(
    markets: list[dict[str, Any]], filters: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected, excluded = [], []
    seen: set[str] = set()
    start = timestamp_ns(filters["start"]) if filters.get("start") else None
    end = timestamp_ns(filters["end"]) if filters.get("end") else None
    if start is not None and end is not None and start >= end:
        raise ValueError("Filter start must precede end")
    for original in markets:
        market = dict(original)
        market_id = str(market["market_id"])
        if market_id in seen:
            raise ValueError(f"Duplicate market_id: {market_id}")
        seen.add(market_id)
        reasons = []
        if filters.get("market_ids") and market_id not in filters["market_ids"]:
            reasons.append("market_universe")
        if filters.get("assets") and market.get("asset") not in filters["assets"]:
            reasons.append("asset_filter")
        if (
            filters.get("durations_seconds")
            and market.get("duration_seconds") not in filters["durations_seconds"]
        ):
            reasons.append("duration_filter")
        if start is not None and int(market.get("start_ns") or 0) < start:
            reasons.append("date_filter")
        if end is not None and int(market.get("close_ns") or end + 1) > end:
            reasons.append("date_filter")
        if reasons:
            market.update(eligible=False, complete=False, censored=True, exclusion_reasons=reasons)
            excluded.append(market)
        else:
            selected.append(market)
    return selected, excluded


def record_digest(records: list[Any] | tuple[Any, ...]) -> str:
    digest = hashlib.sha256()
    for record in records:
        payload = type(record).to_dict(record)
        digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


async def load_vendor_markets(
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, list[Any]]], dict[str, dict[str, Any]]]:
    data = config["data"]
    if data["vendor"] == "sqlite_capture":
        from prediction_market_extensions.backtesting.longshot.capture import load_capture_markets

        return await load_capture_markets(config)
    selection = MarketDataConfig(
        platform="polymarket",
        data_type="book",
        vendor=data["vendor"],
        sources=tuple(data["sources"]),
    )
    adapter = resolve_replay_adapter(
        platform=selection.platform, data_type=selection.data_type, vendor=selection.vendor
    )
    markets, excluded = select_markets(list(config.get("markets", [])), config.get("filters", {}))
    records_by_market: dict[str, dict[str, list[Any]]] = {}
    instruments_by_market: dict[str, dict[str, Any]] = {}
    with adapter.configure_sources(sources=selection.sources):
        for market in markets:
            market_id = market["market_id"]
            records_by_market[market_id] = {}
            instruments_by_market[market_id] = {}
            market["source_coverage"] = {}
            mapping = market.get("tokens", {})
            if set(mapping) != {"UP", "DOWN"} or any(
                not isinstance(token, dict) or "token_id" not in token or "token_index" not in token
                for token in mapping.values()
            ):
                market["load_error"] = "explicit UP/DOWN token_id and token_index mapping required"
                continue
            market["outcomes"] = {side: str(token["token_id"]) for side, token in mapping.items()}
            if not market.get("start_ns") or not market.get("close_ns"):
                market["load_error"] = "missing open/close metadata; no implicit date range"
                continue
            for side in ("UP", "DOWN"):
                token = mapping[side]
                replay = BookReplay(
                    market_slug=market.get("market_slug", market_id),
                    token_index=int(token["token_index"]),
                    outcome=side,
                    start_time=pd.Timestamp(int(market["start_ns"]), unit="ns", tz="UTC"),
                    end_time=pd.Timestamp(int(market["close_ns"]), unit="ns", tz="UTC"),
                )
                loaded = await adapter.load_replay(replay, request=ReplayLoadRequest())
                if loaded is None:
                    market["load_error"] = f"{side}: vendor loader returned no usable replay"
                    continue
                if str(loaded.outcome).upper() != side:
                    market["load_error"] = f"{side}: vendor outcome mapping mismatch"
                    continue
                instrument_token = get_polymarket_token_id(loaded.instrument.id)
                if instrument_token != str(token["token_id"]):
                    market["load_error"] = f"{side}: vendor token identity mismatch"
                    continue
                records = list(loaded.records)
                records_by_market[market_id][side] = records
                instruments_by_market[market_id][side] = loaded.instrument
                market["source_coverage"][side] = {
                    "requested_window": asdict(loaded.requested_window),
                    "loaded_window": asdict(loaded.loaded_window) if loaded.loaded_window else None,
                    "book_event_count": loaded.count,
                    "record_count": len(records),
                    "sha256": record_digest(records),
                }
                # Settlement labels stay in this postprocessing inventory, never strategy config.
                if loaded.realized_outcome == 1.0:
                    known_winner = market.get("settlement_winner")
                    if known_winner is not None and known_winner != side:
                        market["load_error"] = "settlement mapping contradicts supplied inventory"
                    else:
                        market["settlement_winner"] = side
    return markets + excluded, records_by_market, instruments_by_market
