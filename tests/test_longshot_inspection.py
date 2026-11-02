from __future__ import annotations

import builtins
import json
import sqlite3

import pytest

from prediction_market_extensions.backtesting.longshot.inspection import (
    audit_index,
    audit_transactions,
    inspect_manifest,
    inspect_parquet,
    readonly_database,
    resolve_index_path,
)


def test_index_is_discovery_only_and_paths_are_validated(tmp_path):
    path = tmp_path / "index.db"
    data = tmp_path / "sample.parquet"
    data.write_bytes(b"not a parquet schema")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE windows(window_start_ts INTEGER, symbol TEXT, timeframe TEXT, "
            "side TEXT, data_source TEXT, file_path TEXT, file_size INTEGER, indexed_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO windows VALUES (1, 'BTC', '5m', 'UP', 'unknown', ?, 1, 'now')",
            [(data.name,), ("missing.parquet",)],
        )
    before = path.read_bytes()
    report = audit_index(path, sample_limit=2, allowed_roots=[tmp_path])
    assert report["sample_windows"][0]["resolved_path"] == str(data)
    assert not report["sample_windows"][0]["size_matches_index"]
    assert "path_error" in report["sample_windows"][1]
    assert not report["complete_coverage"]
    assert not report["execution_replay_supported"]
    assert path.read_bytes() == before
    assert len(audit_index(path, sample_limit=1, allowed_roots=[tmp_path])["sample_windows"]) == 1


def test_index_symlink_escape_and_missing_roots_fail(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.parquet"
    outside.touch()
    (root / "link.parquet").symlink_to(outside)
    index = tmp_path / "index.db"
    index.touch()
    with pytest.raises(ValueError, match="escapes"):
        resolve_index_path(str(root / "link.parquet"), index, [root])
    with pytest.raises(ValueError, match="allowed roots"):
        resolve_index_path(str(outside), index, [])


def test_readonly_store_rejects_writes_and_locks(tmp_path):
    path = tmp_path / "capture.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fills(price TEXT)")
    with pytest.raises(ValueError, match="readonly"):
        with readonly_database(path) as connection:
            connection.execute("INSERT INTO fills VALUES ('0.70')")
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(ValueError, match="locked"):
            audit_transactions(path)
        writer.rollback()
    assert not audit_transactions(path)["execution_replay_supported"]
    with pytest.raises(FileNotFoundError):
        audit_transactions(tmp_path / "absent.db")


def test_parquet_missing_dependency_is_explicit(tmp_path, monkeypatch):
    path = tmp_path / "btc-updown-5m-1781730900_UP.parquet"
    path.touch()
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "pyarrow.parquet":
            raise ImportError("missing")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ValueError, match="requires PyArrow"):
        inspect_parquet(path)


def test_parquet_schema_not_automatically_mapped(tmp_path):
    arrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "unknown_UP.parquet"
    parquet.write_table(arrow.table({"mystery": ["0.70", "0.71"]}), path)
    report = inspect_parquet(path, sample_limit=1)
    assert report["metadata_rows"] == 2
    assert report["sample_rows"] == [{"mystery": "0.70"}]
    assert report["supported_replay_mapping"] is None
    assert not report["execution_replay_supported"]


def test_parquet_nonfinite_samples_remain_explicit_json_diagnostics(tmp_path):
    arrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "unknown.parquet"
    parquet.write_table(
        arrow.table({"price": [float("nan"), float("inf")], "nested": [[float("-inf")], []]}),
        path,
    )
    report = inspect_parquet(path, sample_limit=2)
    json.dumps(report, allow_nan=False)
    assert report["sample_rows"][0]["price"] == {"nonfinite_float": "nan"}
    assert report["sample_rows"][0]["nested"] == [{"nonfinite_float": "-inf"}]
    assert report["sample_rows"][1]["price"] == {"nonfinite_float": "inf"}
    assert not report["execution_replay_supported"]


def test_manifest_counts_are_not_coverage(tmp_path):
    path = tmp_path / "manifest_btc.json"
    path.write_text(json.dumps([{"ts": 1, "slug": "x", "up_id": "u", "down_id": "d"}] * 2))
    report = inspect_manifest(path, sample_limit=1)
    assert report["manifest_entries"] == 2
    assert len(report["sample_entries"]) == 1
    assert not report["complete_coverage"]
    with pytest.raises(ValueError, match="Sample limit"):
        inspect_manifest(path, sample_limit=0)
