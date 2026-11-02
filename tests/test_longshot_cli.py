from __future__ import annotations

import json

import pytest

from prediction_market_extensions.backtesting.longshot.cli import (
    manifest,
    read_dataset,
    run_stage,
    write_dataset,
)


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
