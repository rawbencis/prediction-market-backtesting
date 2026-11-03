from __future__ import annotations

import json

import pytest

from prediction_market_extensions.backtesting.longshot.cli import (
    main,
    manifest,
    read_dataset,
    run_stage,
    write_dataset,
)


def test_read_dataset_accepts_large_capture_order_field(tmp_path) -> None:
    order = [[index, -1] for index in range(24_000)]
    path = tmp_path / "market_audit.csv"
    write_dataset(path, [{"capture_record_order": {"UP": order}}])
    assert path.stat().st_size > 131_072
    assert read_dataset(path)[0]["capture_record_order"]["UP"] == order


def test_non_analysis_stage_removes_stale_derived_json(tmp_path) -> None:
    for name in ("analysis.json", "execution.json"):
        (tmp_path / name).write_text("{}\n")
    run_stage({"data": {"vendor": "synthetic"}, "primary": True}, "extract", tmp_path)
    assert not (tmp_path / "analysis.json").exists()
    assert not (tmp_path / "execution.json").exists()
    assert (tmp_path / "market_audit.csv").exists()


def test_analyze_keeps_post_close_null_valuation(tmp_path) -> None:
    close = 300_000_000_000
    config = {
        "data": {"vendor": "synthetic"},
        "partitions": {
            "discovery_end": "1970-01-02T00:00:00Z",
            "validation_end": "1970-01-03T00:00:00Z",
            "final_end": "1970-01-04T00:00:00Z",
        },
        "bootstrap_samples": 10,
        "min_days": 1,
        "min_markets": 1,
    }
    audits = [
        {
            "market_id": "held",
            "asset": "BTC",
            "duration_seconds": 300,
            "start_ns": 0,
            "close_ns": close,
            "settlement_ns": None,
            "eligible": True,
            "complete": True,
            "censored": False,
        }
    ]
    write_dataset(tmp_path / "market_audit.csv", audits)
    write_dataset(tmp_path / "events.csv", [])
    write_dataset(tmp_path / "trades.csv", [])
    write_dataset(
        tmp_path / "equity.csv",
        [{"variant": "hold", "ts_ns": close, "equity": 1.0, "cash": 1.0, "open_positions": 1}],
    )
    write_dataset(
        tmp_path / "market_equity.csv",
        [
            {
                "market_id": "held",
                "variant": "hold",
                "ts_ns": close,
                "equity": 1.0,
                "exposure": 1.0,
            },
            {
                "market_id": "held",
                "variant": "hold",
                "ts_ns": close + 1,
                "equity": None,
                "exposure": None,
            },
        ],
    )
    manifest(config, audits, tmp_path, "replay")
    from prediction_market_extensions.backtesting.longshot.cli import analyze_output

    result = analyze_output(config, tmp_path)
    assert not result["portfolios"][0]["portfolio"]["available"]


def test_csv_preserves_types_and_nulls(tmp_path) -> None:
    rows = [{"market_id": "x", "next_touch": None, "eligible": False, "levels": [[0.7, 5]]}]
    path = tmp_path / "audit.csv"
    write_dataset(path, rows)
    assert read_dataset(path) == rows
    write_dataset(path, [])
    assert read_dataset(path) == []


def test_manifest_records_checksum_without_requiring_git(tmp_path) -> None:
    config = {"data": {"vendor": "synthetic"}, "seed": 70}
    write_dataset(tmp_path / "events.csv", [{"touch_number": 1}])
    manifest(
        config, [{"market_id": "x", "source_coverage": {"UP": {"count": 5}}}], tmp_path, "extract"
    )
    result = json.loads((tmp_path / "manifest.json").read_text())
    assert result["seed"] == 70
    assert result["synthetic"]
    assert len(result["dataset_sha256"]["events.csv"]) == 64
    assert result["source_coverage"]["x"]["UP"]["count"] == 5


def test_primary_threshold_cannot_be_selected_on_holdout(tmp_path) -> None:
    with pytest.raises(ValueError, match="Primary threshold"):
        run_stage({"research": {"threshold": 0.65}}, "all", tmp_path)


def test_source_audit_does_not_require_market_configuration(tmp_path):
    source = tmp_path / "manifest.json"
    source.write_text('[{"slug": "sample"}]')
    output = tmp_path / "audit"
    main(
        [
            "source-audit",
            "--source-kind",
            "manifest",
            "--source",
            str(source),
            "--output",
            str(output),
        ]
    )
    report = json.loads((output / "source_audit.json").read_text())
    assert report["manifest_entries"] == 1
    assert not report["execution_replay_supported"]


def test_source_audit_requires_explicit_source_kind(tmp_path):
    with pytest.raises(SystemExit):
        main(["source-audit", "--source", str(tmp_path), "--output", str(tmp_path)])
    with pytest.raises(SystemExit):
        main(["extract", "--output", str(tmp_path)])
