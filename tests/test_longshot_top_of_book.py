from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from pyarrow import parquet

from prediction_market_extensions.backtesting.longshot.quote_cli import report, run, summary
from prediction_market_extensions.backtesting.longshot.top_of_book import (
    COLUMNS,
    Study,
    census,
    extract,
    index_file_path,
    midpoint,
    observations,
    quote_values,
    study_windows,
    utc_seconds,
)

START = 1781730900
OPEN = START * 1_000_000
SLUG = f"btc-updown-5m-{START}"
ROOT = Path(__file__).resolve().parents[1]


def row(side, offset, bid="0.39", ask="0.41", **overrides):
    result = {
        "timestamp_us": OPEN + offset,
        "local_timestamp_us": OPEN + offset + 100,
        "exchange": "POLYMARKET",
        "market_id": "market",
        "slug": SLUG,
        "asset_id": side.lower(),
        "outcome": side,
        "bid_price": bid,
        "ask_price": ask,
        "bid_size": "5",
        "ask_size": "7",
    }
    result.update(overrides)
    return result


def write_parquet(path, rows):
    schema = pa.schema(
        [(name, pa.int64() if name.endswith("timestamp_us") else pa.string()) for name in COLUMNS]
    )
    parquet.write_table(pa.Table.from_pylist(rows, schema=schema), path, row_group_size=2)


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    (data / "unified").mkdir(parents=True)
    (data / "quotes").mkdir()
    index = data / "unified" / "quotes_unified.db"
    with sqlite3.connect(index) as connection:
        connection.execute(
            "CREATE TABLE windows(window_start_ts INTEGER,symbol TEXT,timeframe TEXT,"
            "side TEXT,data_source TEXT,file_path TEXT,file_size INTEGER,indexed_at TEXT)"
        )
        connection.execute("CREATE INDEX schedule ON windows(window_start_ts,symbol,timeframe)")
    study = Study(data, (data,), index, START, START + 300, batch_size=2)

    def add(side, rows, source="test", size=None, name=None):
        path = data / "quotes" / (name or f"{SLUG}_{side}.parquet")
        write_parquet(path, rows)
        with sqlite3.connect(index) as connection:
            connection.execute(
                "INSERT INTO windows VALUES (?,?,?,?,?,?,?,?)",
                (
                    START,
                    "BTC",
                    "5m",
                    side,
                    source,
                    str(path.relative_to(data)),
                    path.stat().st_size if size is None else size,
                    "fixture",
                ),
            )
        return path

    return study, add


def mapping_file(study, *, verified=False, **overrides):
    entry = {"ts": START, "slug": SLUG, "up_id": "up", "down_id": "down"}
    if verified:
        entry.update(market_id="market", evidence="synthetic independently supplied identity")
    entry.update(overrides)
    path = study.data_root / ("verified.json" if verified else "manifest.json")
    path.write_text(json.dumps([entry]))
    return path


def window(study):
    return next(census(study))


def load_streams(study, add, up, down):
    streams = {}
    for side, rows in (("UP", up), ("DOWN", down)):
        path = add(side, rows)
        streams[side] = observations(path, side, window(study), study)[0]
    return streams


def basic_rows():
    times = [-100, 100_000, 200_000, 300_000, 400_000, 299_900_000]
    up = [
        row("UP", ts, *("0.69", "0.71") if i in (1, 4) else ("0.39", "0.41"))
        for i, ts in enumerate(times)
    ]
    down = [
        row("DOWN", ts, *("0.69", "0.71") if i == 3 else ("0.39", "0.41"))
        for i, ts in enumerate(times)
    ]
    return up, down


def tolerant(study):
    return replace(study, gap_us=60_000_000, pair_age_us=60_000_000)


def complete_rows():
    up, down = basic_rows()
    for ts in range(50_000_000, 299_000_000, 50_000_000):
        up.insert(-1, row("UP", ts))
        down.insert(-1, row("DOWN", ts))
    return up, down


def test_census_relative_data_root_readonly_and_missing_windows(store):
    study, add = store
    up = add("UP", [row("UP", 1)])
    before = study.index.read_bytes()
    windows = list(census(replace(study, end=START + 900)))
    assert len(windows) == 3
    assert windows[0]["files"]["UP"]["path"] == str(up)
    assert "DOWN:missing" in windows[0]["exclusions"]
    assert windows[1]["exclusions"] == ["UP:missing", "DOWN:missing"]
    assert before == study.index.read_bytes()
    absent = replace(study, index=study.data_root / "absent.db")
    assert len(list(census(absent))) == 1
    assert any("index_unavailable" in reason for reason in next(census(absent))["exclusions"])
    assert not absent.index.exists()


def test_path_traversal_symlink_size_and_filename(store, tmp_path):
    study, add = store
    path = add("UP", [row("UP", 0)])
    for unsafe in ("../outside.parquet", str(path)):
        with pytest.raises(ValueError, match="relative"):
            index_file_path(unsafe, path.stat().st_size, study)
    with pytest.raises(ValueError, match="size"):
        index_file_path("quotes/" + path.name, 1, study)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"x")
    link = study.data_root / "escape.parquet"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        index_file_path(link.name, 1, study)
    add("DOWN", [row("DOWN", 0)], name="wrong_DOWN.parquet")
    assert any("Filename" in reason for reason in window(study)["exclusions"])
    with pytest.raises(ValueError, match="allowed_roots"):
        replace(study, allowed_roots=())


def test_duplicate_sources_are_never_silently_combined(store):
    study, add = store
    add("UP", [row("UP", 0)], source="a")
    add("DOWN", [row("DOWN", 0)], source="b")
    assert "ambiguous_sources" in window(study)["exclusions"]
    selected = window(replace(study, source="a"))
    assert set(selected["files"]) == {"UP"}
    assert "DOWN:missing" in selected["exclusions"]
    add("UP", [row("UP", 0)], source="a")
    assert "UP:duplicate" in window(replace(study, source="a"))["exclusions"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": "wrong"},
        {"asset_id": "down"},
        {"outcome": "DOWN"},
        {"exchange": "OTHER"},
        {"market_id": ""},
        {"timestamp_us": None},
    ],
)
def test_source_identity_and_timestamp_mismatch(store, overrides):
    study, add = store
    path = add("UP", [row("UP", 0, **overrides)])
    with pytest.raises(ValueError):
        observations(path, "UP", window(study), study, {"up_id": "up", "down_id": "down"})


def test_full_file_identity_including_premarket_and_cross_market(store):
    study, add = store
    add("UP", [row("UP", -10_000_000, asset_id="stale"), row("UP", 10)])
    add("DOWN", [row("DOWN", 10, market_id="other")])
    result = next(study_windows(study))
    assert any("Conflicting identities" in reason for reason in result["exclusions"])
    assert result["censored"]


def test_manifest_consistency_not_verification_and_conflicts(store):
    study, add = store
    for side in ("UP", "DOWN"):
        add(side, [row(side, -100), row(side, 10), row(side, 299_999_999)])
    manifest = mapping_file(study)
    result = next(study_windows(replace(study, manifest=manifest)))
    assert not result["eligible_open"]
    assert not result["independently_verified"]
    verified = mapping_file(study, verified=True, up_id="different")
    result = next(study_windows(replace(study, manifest=manifest, verified_identities=verified)))
    assert "manifest_verification_conflict" in result["exclusions"]
    manifest.write_text(
        json.dumps(
            [
                {"ts": START, "slug": SLUG, "up_id": "same", "down_id": "same"},
            ]
        )
    )
    with pytest.raises(ValueError, match="identity conflict"):
        next(study_windows(replace(study, manifest=manifest)))


def test_whole_terminal_row_nulls_transient_anomalies_and_batch_boundary(store):
    study, add = store
    rows = [
        row("UP", -1),
        row("UP", 10, "0.8", "0.7"),
        row("UP", 10, "0.7", "0.7"),
        row("UP", 10, "0.69", "0.71"),
        row("UP", 20),
        row("UP", 20, bid_price=None, ask_size=None),
        row("UP", 30),
        row("UP", 300_000_000),
    ]
    path = add("UP", rows)
    terminal, diagnostics = observations(path, "UP", window(study), study)
    selected = {item["timestamp_us"] - OPEN: item for item in terminal}
    assert selected[10]["source_row_index"] == 3
    assert selected[10]["group_first_row_index"] == 1
    assert selected[10]["group_rows"] == 3
    assert selected[10]["valid"]
    assert selected[20]["bid_price"] is None and selected[20]["ask_size"] is None
    assert not selected[20]["valid"]
    assert 300_000_000 not in selected
    assert diagnostics["raw_crossed_rows"] == diagnostics["raw_locked_rows"] == 1
    assert diagnostics["invalid_terminal_rows"] == 1
    assert diagnostics["resolved_invalid_groups"] == 1
    assert len(diagnostics["raw_anomalies"]) == 3


def test_exchange_and_receipt_inversions_keep_original_row_indices(store):
    study, add = store
    path = add(
        "UP",
        [
            row("UP", 20, local_timestamp_us=OPEN + 100),
            row("UP", 10, local_timestamp_us=OPEN + 50),
            row("UP", 20, local_timestamp_us=OPEN + 60),
        ],
    )
    terminal, diagnostics = observations(path, "UP", window(study), study)
    assert diagnostics["exchange_inversions"] == diagnostics["receipt_inversions"] == 1
    assert [item["source_row_index"] for item in terminal] == [1, 2]
    assert terminal[-1]["group_receipt_span_us"] == 40
    assert diagnostics["max_within_group_receipt_span_us"] == 40
    assert terminal[-1]["local_timestamp_us"] == OPEN + 60


def test_multiple_reversals_decimal_thresholds_and_quote_targets(store):
    study, add = store
    study = tolerant(study)
    streams = load_streams(study, add, *complete_rows())
    result = extract(window(study), streams, study, independently_verified=True)
    assert result["complete"]
    assert [touch["side"] for touch in result["touches"]] == ["UP", "DOWN", "UP"]
    assert [touch["touch_number"] for touch in result["touches"]] == [1, 2, 3]
    assert result["transitions"]["p12"]["success"] is True
    assert result["transitions"]["p23"]["success"] is True
    assert not result["bid_targets"]  # Midpoint 0.70 with bid 0.69 is not a bid target.
    assert result["touches"][0]["opposite_age_us"] == 0
    assert result["touches"][1]["transition_us"] == 200_000
    assert result["touches"][0]["opposite_row"]["bid_size"] == "5"
    assert result["touches"][0]["remaining_us"] == 299_900_000


def test_repeated_same_side_does_not_advance_and_complete_no_reversal(store):
    study, add = store
    study = tolerant(study)
    up, down = complete_rows()
    down = [row("DOWN", item["timestamp_us"] - OPEN) for item in down]
    streams = load_streams(study, add, up, down)
    result = extract(window(study), streams, study, independently_verified=True)
    assert result["complete"]
    assert len(result["touches"]) == 1
    assert len(result["diagnostic_crossings"]) == 2
    assert result["transitions"]["p12"]["success"] is False
    assert result["transitions"]["p23"]["success"] is None


@pytest.mark.parametrize("mode", ["late", "no_anchor", "invalid", "gap", "stale", "terminal"])
def test_incomplete_absence_never_false_and_no_restarted_numbering(store, mode):
    study, add = store
    study = tolerant(study)
    up, down = complete_rows()
    if mode == "late":
        up = up[1:]
    elif mode == "no_anchor":
        up[0]["timestamp_us"] = OPEN
    elif mode == "invalid":
        up[2]["bid_price"] = None
    elif mode == "gap":
        study = replace(study, gap_us=50_000)
    elif mode == "stale":
        down = [item for item in down if item["timestamp_us"] < OPEN]
        study = replace(study, pair_age_us=50_000)
    elif mode == "terminal":
        down.pop()
    streams = load_streams(study, add, up, down)
    result = extract(window(study), streams, study, independently_verified=True)
    assert result["censored"]
    if mode in {"invalid", "gap", "stale"}:
        assert len(result["touches"]) <= 1
    if not result["transitions"]["p12"]["success"]:
        assert result["transitions"]["p12"]["success"] is None
    if mode in {"late", "no_anchor"}:
        assert not result["touches"]
        assert result["diagnostic_crossings"]


def test_simultaneous_opposing_crossings_do_not_invent_order(store):
    study, add = store
    up = [row("UP", -1), row("UP", 10, "0.69", "0.71")]
    down = [row("DOWN", -1), row("DOWN", 10, "0.69", "0.71")]
    streams = load_streams(study, add, up, down)
    result = extract(window(study), streams, study, independently_verified=True)
    assert not result["touches"]
    assert len(result["diagnostic_crossings"]) == 2
    assert all(item["ambiguous"] for item in result["diagnostic_crossings"])


def test_raw_coalesced_sensitivity_is_diagnostic_only(store):
    study, add = store
    path = add(
        "UP",
        [
            row("UP", -1),
            row("UP", 1),
            row("UP", 10, "0.69", "0.71"),
            row("UP", 10),
            row("UP", 20, "0.69", "0.71"),
            row("UP", 20),
        ],
    )
    terminal, diagnostics = observations(path, "UP", window(study), study)
    result = extract(window(study), {"UP": terminal}, study, independently_verified=False)
    assert diagnostics["raw_file_order_crossings"] == 2
    assert result["coalesced_crossings"].get("UP", 0) == 0
    assert result["censored"]
    assert "not atomic" in result["warning"]


def test_threshold_precision_and_independently_observed_bid_episode(store):
    study, add = store
    below = row("UP", 1, "0.69", "0.7099999999999999999999999999999999999999")
    assert midpoint(quote_values(below)) < Decimal("0.70")
    up = [row("UP", -1), below, row("UP", 10, "0.70", "0.72"), row("UP", 20, "0.70", "0.72")]
    down = [row("DOWN", -1), row("DOWN", 1), row("DOWN", 10), row("DOWN", 20)]
    streams = load_streams(study, add, up, down)
    result = extract(window(study), streams, study, independently_verified=True)
    assert len(result["touches"]) == 1
    assert result["touches"][0]["timestamp_us"] == OPEN + 10
    assert len(result["bid_targets"]) == 1
    assert result["bid_targets"][0]["row"]["bid_price"] == "0.70"


def test_open_above_threshold_is_left_truncated(store):
    study, add = store
    streams = load_streams(
        study,
        add,
        [row("UP", -1, "0.69", "0.71"), row("UP", 10)],
        [row("DOWN", -1), row("DOWN", 10)],
    )
    result = extract(window(study), streams, study, independently_verified=True)
    assert not result["eligible_open"]
    assert "UP:left_truncated_above_threshold_open" in result["exclusions"]


def test_interrupted_risk_is_unknown_but_prior_success_stays_known(store):
    study, add = store
    study = tolerant(study)
    up, down = complete_rows()
    up[4]["bid_price"] = None  # Interrupt after touch 2, before a potential touch 3.
    streams = load_streams(study, add, up, down)
    result = extract(window(study), streams, study, independently_verified=True)
    assert [touch["side"] for touch in result["touches"]] == ["UP", "DOWN"]
    assert result["transitions"]["p12"]["success"] is True
    assert result["transitions"]["p23"]["at_risk"]
    assert result["transitions"]["p23"]["success"] is None
    assert result["censored"]


def test_invalid_preopen_anchor_retains_raw_diagnostic(store):
    study, add = store
    path = add("UP", [row("UP", -10, "0.7", "0.7"), row("UP", 1)])
    terminal, diagnostic = observations(path, "UP", window(study), study)
    assert diagnostic["raw_anomalies"][0]["timestamp_us"] == OPEN - 10
    assert diagnostic["raw_anomalies"][0]["source_row_index"] == 0
    result = extract(window(study), {"UP": terminal}, study, independently_verified=True)
    assert not result["eligible_open"]


def test_bounded_read_limits_and_utc(store):
    study, add = store
    path = add("UP", [row("UP", 1), row("UP", 2)])
    with pytest.raises(ValueError, match="row limit"):
        observations(path, "UP", window(study), replace(study, max_rows=1))
    with pytest.raises(ValueError, match="timestamp limit"):
        observations(path, "UP", window(study), replace(study, max_timestamps=1))
    assert utc_seconds("2026-06-17T21:15:00Z") == START
    with pytest.raises(ValueError, match="UTC"):
        utc_seconds("2026-06-17T21:15:00")
    with pytest.raises(ValueError, match="max_windows"):
        replace(study, end=START + 600, max_windows=1)


def config_for(study, **options):
    return {
        "study": {
            **study.configuration(),
            "start": "2026-06-17T21:15:00Z",
            "end": "2026-06-17T21:30:00Z",
            **options,
        },
        "partitions": {
            "discovery_end": "2026-06-17T21:20:00Z",
            "validation_end": "2026-06-17T21:25:00Z",
            "final_end": "2026-06-17T21:30:00Z",
        },
    }


def test_offline_cli_audit_extract_report_and_hashes(store, tmp_path):
    study, add = store
    study = tolerant(study)
    for side, rows in zip(("UP", "DOWN"), complete_rows(), strict=True):
        add(side, rows)
    study = replace(study, verified_identities=mapping_file(study, verified=True))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_for(study)))
    original = study.index.read_bytes()
    output = tmp_path / "extract"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "prediction_market_extensions.backtesting.longshot.quote_cli",
            "extract",
            "--config",
            str(config),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
    )
    assert study.index.read_bytes() == original
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert not manifest["execution_replay_supported"]
    assert len(manifest["source_sha256"][SLUG]) == 2
    assert len((output / "touches.csv").read_text().splitlines()) == 4
    report(output, tmp_path / "report.json")
    audit_output = tmp_path / "audit"
    run("audit", config, audit_output)
    audit = json.loads((audit_output / "audit.json").read_text())
    assert audit["scheduled_windows"] == 3
    assert audit["complete_windows"] == 1
    assert (audit_output / "observations.jsonl").stat().st_size == 0
    with (output / "windows.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        report(output, tmp_path / "tampered.json")


def test_output_bounds_and_source_root_protection(store, tmp_path):
    study, add = store
    add("UP", [row("UP", 1)])
    config = tmp_path / "config.json"
    configuration = config_for(study)
    configuration["max_output_bytes"] = 10
    config.write_text(json.dumps(configuration))
    with pytest.raises(ValueError, match="Output limit"):
        run("extract", config, tmp_path / "partial")
    assert not (tmp_path / "partial" / "manifest.json").exists()
    with pytest.raises(ValueError, match="read-only"):
        run("audit", config, study.data_root / "output")


def test_summary_unknown_denominators_and_day_block_guards(store):
    study, _ = store
    config = config_for(study)
    config["analysis"] = {"bootstrap_samples": 100, "seed": 7, "min_days": 2, "min_markets": 2}
    base = {
        "asset": "BTC",
        "duration_seconds": 300,
        "eligible_open": True,
        "complete": False,
        "censored": True,
        "census_exclusions": [],
        "transitions": {
            "p12": {"at_risk": True, "success": None},
            "p23": {"at_risk": False, "success": None},
        },
    }
    rows = [{**base, "start": START, "close": START + 300}]
    result = summary(rows, config)["groups"][0]
    assert result["p12"]["eligible_at_risk"] == 1
    assert result["p12"]["completed_at_risk"] == 0
    assert result["p12"]["censored_unknown"] == 1
    assert result["p12"]["observed_complete_case_ratio"] is None
    assert result["p12"]["guarded_estimate"]["estimate"] is None
    assert summary(rows, config) == summary(rows, config)


def test_quote_vendor_cannot_be_used_as_execution_replay(tmp_path):
    from prediction_market_extensions.backtesting.longshot.sources import read_configuration

    path = tmp_path / "execution.json"
    path.write_text(
        json.dumps(
            {
                "partitions": config_for(
                    Study(tmp_path, (tmp_path,), tmp_path / "index", START, START + 300)
                )["partitions"],
                "data": {"vendor": "top_of_book", "sources": ["local:/unused"]},
            }
        )
    )
    with pytest.raises(ValueError, match="data.vendor"):
        read_configuration(path)
