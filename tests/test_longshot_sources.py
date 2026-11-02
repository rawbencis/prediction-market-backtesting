from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from prediction_market_extensions.adapters.prediction_market.replay import ReplayWindow
from prediction_market_extensions.backtesting.longshot import sources as source_module
from prediction_market_extensions.backtesting.longshot.sources import (
    load_vendor_markets,
    read_configuration,
    select_markets,
    timestamp_ns,
)


def test_timezone_and_duplicate_inventory() -> None:
    with pytest.raises(ValueError, match="timezone"):
        timestamp_ns("2026-01-01")
    assert timestamp_ns("2026-01-01T01:00:00+01:00") == timestamp_ns("2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="Duplicate"):
        select_markets([{"market_id": "x"}, {"market_id": "x"}], {})


def test_filters_preserve_excluded_markets() -> None:
    markets = [
        {
            "market_id": "btc",
            "asset": "BTC",
            "duration_seconds": 300,
            "start_ns": 10,
            "close_ns": 20,
        },
        {
            "market_id": "eth",
            "asset": "ETH",
            "duration_seconds": 900,
            "start_ns": 10,
            "close_ns": 20,
        },
    ]
    selected, excluded = select_markets(markets, {"assets": ["BTC"], "durations_seconds": [300]})
    assert [market["market_id"] for market in selected] == ["btc"]
    assert excluded[0]["exclusion_reasons"] == ["asset_filter", "duration_filter"]
    assert not excluded[0]["eligible"]
    assert "eligible" not in markets[1]


def test_configuration_requires_explicit_sources_and_partitions(tmp_path) -> None:
    config = {
        "data": {"vendor": "pmxt", "sources": []},
        "partitions": {
            "discovery_end": "2026-01-02T00:00:00Z",
            "validation_end": "2026-01-03T00:00:00Z",
            "final_end": "2026-01-04T00:00:00Z",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Explicit vendor sources"):
        read_configuration(path)
    config["data"]["sources"] = ["local:/data/pmxt"]
    path.write_text(json.dumps(config))
    assert read_configuration(path) == config
    config["data"]["sources"] = ["api:literal"]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="never a literal"):
        read_configuration(path)
    config["data"] = {"vendor": "synthetic"}
    config["partitions"]["validation_end"] = config["partitions"]["discovery_end"]
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="strictly increasing"):
        read_configuration(path)


def test_vendor_reuses_book_replay_adapter_and_preserves_load_failures(monkeypatch) -> None:
    requests = []

    class Adapter:
        def configure_sources(self, *, sources):
            assert sources == ("local:/configured/raws",)
            return nullcontext()

        async def load_replay(self, replay, *, request):
            requests.append(replay)
            if replay.token_index == 1:
                return None
            return SimpleNamespace(
                outcome="UP",
                instrument=SimpleNamespace(id=InstrumentId.from_str("condition-token0.POLYMARKET")),
                records=(),
                count=0,
                requested_window=ReplayWindow(start_ns=1, end_ns=300_000_000_001),
                loaded_window=None,
                realized_outcome=1.0,
            )

    monkeypatch.setattr(source_module, "resolve_replay_adapter", lambda **kwargs: Adapter())
    market = {
        "market_id": "m",
        "asset": "BTC",
        "duration_seconds": 300,
        "start_ns": 1,
        "close_ns": 300_000_000_001,
        "tokens": {
            "UP": {"token_index": 0, "token_id": "token0"},
            "DOWN": {"token_index": 1, "token_id": "token1"},
        },
    }
    markets, records, instruments = asyncio.run(
        load_vendor_markets(
            {"data": {"vendor": "pmxt", "sources": ["local:/configured/raws"]}, "markets": [market]}
        )
    )
    assert len(requests) == 2
    assert requests[0].start_time.value == 1
    assert requests[1].end_time.value == market["close_ns"]
    assert markets[0]["load_error"].startswith("DOWN")
    assert markets[0]["outcomes"] == {"UP": "token0", "DOWN": "token1"}
    assert markets[0]["settlement_winner"] == "UP"
    assert set(records["m"]) == {"UP"}
    assert set(instruments["m"]) == {"UP"}


def test_capture_configuration_requires_one_absolute_local_source(tmp_path):
    config = {
        "data": {"vendor": "sqlite_capture", "sources": ["local:/configured/capture.sqlite3"]},
        "partitions": {
            "discovery_end": "2026-01-02T00:00:00Z",
            "validation_end": "2026-01-03T00:00:00Z",
            "final_end": "2026-01-04T00:00:00Z",
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert read_configuration(path)["data"]["vendor"] == "sqlite_capture"
    for sources, message in [
        (["local:relative.sqlite3"], "absolute path"),
        (["archive:r2v2.pmxt.dev"], "exactly one local"),
        (["local:/one.sqlite3", "local:/two.sqlite3"], "exactly one local"),
    ]:
        config["data"]["sources"] = sources
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match=message):
            read_configuration(path)
