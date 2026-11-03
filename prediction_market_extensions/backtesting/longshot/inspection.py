from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def bounded_limit(value: int) -> int:
    if not 1 <= value <= 100:
        raise ValueError("Sample limit must be between 1 and 100")
    return value


@contextmanager
def readonly_database(path: Path):
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not a SQLite file: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    # Bound VM work as well as returned rows; LIMIT alone cannot bound a scan.
    budget = 0

    def interrupt() -> int:
        nonlocal budget
        budget += 1
        return int(budget > 2000)

    connection.set_progress_handler(interrupt, 1000)
    try:
        yield connection
    except sqlite3.Error as exc:
        raise ValueError(f"Read-only SQLite inspection failed for {path}: {exc}") from exc
    finally:
        connection.close()


def schema_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='table' LIMIT 101"
    ).fetchall()
    if len(tables) > 100:
        raise ValueError("Schema exceeds bounded inspection limit of 100 tables")
    result = {}
    for table in tables:
        quoted = '"' + table["name"].replace('"', '""') + '"'
        columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        populated = connection.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone()
        result[table["name"]] = {
            "columns": [dict(column) for column in columns],
            "ddl": table["sql"],
            "populated": populated is not None,
        }
    return result


def resolve_index_path(value: str, index: Path, allowed_roots: list[Path]) -> Path:
    if not allowed_roots:
        raise ValueError("Explicit allowed roots are required to validate indexed file paths")
    path = Path(value)
    if not path.is_absolute():
        path = index.resolve(strict=True).parent / path
    resolved = path.resolve(strict=True)
    roots = [root.resolve(strict=True) for root in allowed_roots]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError(f"Indexed path escapes allowed roots: {value}")
    if not resolved.is_file():
        raise ValueError(f"Indexed path is not a file: {value}")
    return resolved


def audit_index(path: Path, *, sample_limit: int = 20, allowed_roots: list[Path]) -> dict[str, Any]:
    bounded_limit(sample_limit)
    with readonly_database(path) as connection:
        schemas = schema_inventory(connection)
        required = {
            "window_start_ts",
            "symbol",
            "timeframe",
            "side",
            "data_source",
            "file_path",
            "file_size",
            "indexed_at",
        }
        columns = {column["name"] for column in schemas.get("windows", {}).get("columns", [])}
        if not required <= columns:
            raise ValueError("Unsupported file index: required windows columns are missing")
        rows = connection.execute("SELECT * FROM windows LIMIT ?", (sample_limit,)).fetchall()
        samples = []
        for row in rows:
            sample = dict(row)
            try:
                resolved = resolve_index_path(str(row["file_path"]), path, allowed_roots)
                sample.update(
                    resolved_path=str(resolved),
                    actual_file_size=resolved.stat().st_size,
                    size_matches_index=resolved.stat().st_size == row["file_size"],
                )
            except (OSError, ValueError) as exc:
                sample["path_error"] = str(exc)
            samples.append(sample)
    return {
        "source_kind": "file_index",
        "schemas": schemas,
        "sample_windows": samples,
        "sample_limit": sample_limit,
        "complete_coverage": False,
        "execution_replay_supported": False,
        "warnings": [
            "Unordered bounded discovery sample, not a market count or coverage check.",
            "Indexed windows do not prove files exist, contain full L2, or cover entire windows.",
        ],
    }


def _sample_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": repr(value)}
    if isinstance(value, dict):
        return {key: _sample_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sample_value(item) for item in value]
    return value


def inspect_parquet(path: Path, *, sample_limit: int = 5) -> dict[str, Any]:
    bounded_limit(sample_limit)
    path = path.resolve(strict=True)
    try:
        from pyarrow import parquet
    except ImportError as exc:
        raise ValueError(
            "Parquet inspection requires PyArrow in the inspection environment; "
            "server python3 has not established the Parquet schema. No PMXT mapping is assumed."
        ) from exc
    with parquet.ParquetFile(path) as file:
        schema = file.schema_arrow
        if len(schema) > 128:
            raise ValueError("Parquet schema exceeds bounded limit of 128 columns")
        batch = next(file.iter_batches(batch_size=sample_limit), None)
        samples = [] if batch is None else _sample_value(batch.to_pylist())
        return {
            "source_kind": "parquet_unknown",
            "path": str(path),
            "schema": str(schema),
            "metadata_rows": file.metadata.num_rows,
            "metadata_row_groups": file.metadata.num_row_groups,
            "sample_rows": samples,
            "sample_limit": sample_limit,
            "supported_replay_mapping": None,
            "execution_replay_supported": False,
            "required_evidence": [
                "Column meanings, units and timestamps; outcome identity and observation cadence.",
                "Snapshot/delta boundaries and deletion semantics for any full-depth mapping.",
                "Selection process: threshold-selected samples cannot establish unbiased transitions.",
            ],
            "warnings": [
                "Filename and column names alone do not establish full-depth L2.",
                "Top-of-book/event-study data cannot support full-depth execution realism.",
            ],
        }


def inspect_manifest(path: Path, *, sample_limit: int = 20) -> dict[str, Any]:
    bounded_limit(sample_limit)
    with path.open("rb") as stream:
        payload = stream.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("Manifest exceeds bounded 8 MiB inspection limit")
    entries = json.loads(payload)
    if not isinstance(entries, list):
        # Invalid decoded file structure is reported as a validation error.
        raise ValueError("Expected a manifest list")  # noqa: TRY004
    return {
        "source_kind": "market_manifest",
        "manifest_entries": len(entries),
        "sample_entries": entries[:sample_limit],
        "complete_coverage": False,
        "execution_replay_supported": False,
        "warnings": ["Entry count is not a validated unique or complete market count."],
    }


def audit_transactions(path: Path) -> dict[str, Any]:
    with readonly_database(path) as connection:
        schemas = schema_inventory(connection)
    return {
        "source_kind": "transactions_not_l2",
        "schemas": schemas,
        "execution_replay_supported": False,
        "warnings": ["Wallet fills are not historical quotes or an L2 order book source."],
    }
