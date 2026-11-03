from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import BookAction, BookType, RecordFlag

from prediction_market_extensions.backtesting._replay_specs import BookReplay
from prediction_market_extensions.backtesting.longshot import capture
from prediction_market_extensions.backtesting.longshot.capture import (
    CaptureError,
    audit_capture,
    load_capture_markets,
    load_capture_replay,
)

START = 1_782_864_000
SECOND = 1_000_000_000
SCHEMA = """
CREATE TABLE sessions (
 id INTEGER PRIMARY KEY, condition_id TEXT, start_at REAL, end_at REAL,
 started_at REAL, ended_at REAL, up_token_id TEXT, down_token_id TEXT,
 planned_start_at REAL, planned_end_at REAL, flags INTEGER,
 connected_at REAL, closed_at REAL, asset TEXT
);
CREATE TABLE raw_frames (
 id INTEGER PRIMARY KEY, session_id INTEGER, segment_id INTEGER, sequence INTEGER,
 received_at REAL, payload BLOB, flags INTEGER, monotonic_offset_seconds REAL,
 event_type TEXT, asset_id TEXT, exchange_timestamp_ms INTEGER, hash TEXT,
 payload_encoding TEXT, uncompressed_size INTEGER
);
CREATE TABLE book_events (
 frame_id INTEGER, event_index INTEGER, kind TEXT, asset_id TEXT, payload TEXT,
 flags INTEGER, exchange_timestamp_ms INTEGER, hash TEXT,
 PRIMARY KEY (frame_id, event_index)
);
CREATE TABLE book_levels (
 frame_id INTEGER, event_index INTEGER, level_index INTEGER, kind TEXT,
 asset_id TEXT, side TEXT, price TEXT, size TEXT, exchange_timestamp_ms INTEGER,
 hash TEXT, PRIMARY KEY (frame_id, event_index, level_index)
);
CREATE TABLE book_trades (
 frame_id INTEGER, event_index INTEGER, asset_id TEXT, side TEXT, price TEXT,
 size TEXT, exchange_timestamp_ms INTEGER, transaction_hash TEXT
);
CREATE TABLE segments (id INTEGER, session_id INTEGER);
CREATE TABLE coverage_intervals (
 session_id INTEGER, start_at REAL, end_at REAL, status TEXT
);
CREATE TABLE gaps (session_id INTEGER, start_at REAL, end_at REAL);
CREATE TABLE reconciliations (session_id INTEGER, payload BLOB);
CREATE TABLE checkpoint_diagnostics (session_id INTEGER, payload BLOB);
CREATE TABLE timestamp_inversions (session_id INTEGER, frame_id INTEGER);
CREATE TABLE archive_levels (
 session_id INTEGER, first_frame_id INTEGER, last_frame_id INTEGER,
 payload_encoding TEXT, payload BLOB
);
CREATE TABLE archive_trades (
 session_id INTEGER, first_sequence INTEGER, last_sequence INTEGER,
 payload_encoding TEXT, payload BLOB
);
CREATE TABLE raw_blocks (session_id INTEGER, payload_encoding TEXT, payload BLOB);
CREATE TABLE archive_levels_compact (
 session_id INTEGER, first_frame_id INTEGER, last_frame_id INTEGER,
 payload_encoding TEXT, payload BLOB,
 PRIMARY KEY (session_id, first_frame_id)
) WITHOUT ROWID;
CREATE TABLE heartbeat_diagnostics (session_id INTEGER, received_at REAL);
"""


@pytest.fixture
def database():
    # Keep test artifacts in the worktree, including when the runner's TMPDIR is external.
    directory = Path.cwd() / ".capture-test-artifacts" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    path = directory / "capture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO sessions (id,condition_id,start_at,end_at,started_at,ended_at,"
            "up_token_id,down_token_id,planned_start_at,planned_end_at,flags,asset) "
            "VALUES (1,'condition',?,?,?,?, '123','456',?,?,0,'BTC')",
            (START, START + 5, START - 50, START + 50, START, START + 5),
        )
    yield path
    shutil.rmtree(directory)
    root = directory.parent
    if root.exists() and not any(root.iterdir()):
        root.rmdir()


@pytest.fixture
def config(database):
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO coverage_intervals VALUES (1,?,?,'complete')", (START, START + 6)
        )
    return {
        "data": {
            "vendor": "sqlite_capture",
            "sources": [f"local:{database}"],
            "contract": {
                "evidence": "Synthetic recorder contract SCHEMA above; not production evidence",
                "timestamp_unit": "s",
                "start_column": "start_at",
                "end_column": "end_at",
                "arrival_order": "session_sequence",
                "sequence": "contiguous",
                "snapshot_kind": "snapshot",
                "delta_kind": "delta",
                "delta_size": "absolute",
                "zero_size": "delete",
                "buy_side": "bid",
                "trade_side": "aggressor",
                "price_precision": 2,
                "size_precision": 2,
                "coverage_intervals": {
                    "evidence": "Fixture recorder complete coverage declaration on arrival clock",
                    "start_column": "start_at",
                    "end_column": "end_at",
                    "status_column": "status",
                    "timestamp_unit": "s",
                },
            },
        },
    }


def event(
    database, frame_id, token="123", kind="snapshot", levels=None, *, offset=0, exchange=None
):
    if levels is None:
        levels = [("BUY", "0.40", "20.00"), ("SELL", "0.60", "20.00")]
    exchange = (START + offset) * 1000 if exchange is None else exchange
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO raw_frames "
            "(id,session_id,segment_id,sequence,received_at,flags,event_type) "
            "VALUES (?,1,1,?,?,0,'book')",
            (frame_id, frame_id, START + offset),
        )
        index = connection.execute(
            "SELECT COALESCE(MAX(event_index),-1)+1 FROM book_events WHERE frame_id=?",
            (frame_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO book_events (frame_id,event_index,kind,asset_id,flags,exchange_timestamp_ms)"
            " VALUES (?,?,?,?,0,?)",
            (frame_id, index, kind, token, exchange),
        )
        if kind == "trade":
            side, price, size = levels[0]
            connection.execute(
                "INSERT INTO book_trades VALUES (?,?,?,?,?,?,?,?)",
                (frame_id, index, token, side, price, size, exchange, f"tx-{frame_id}"),
            )
        else:
            for number, (side, price, size) in enumerate(levels):
                connection.execute(
                    "INSERT INTO book_levels VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (frame_id, index, number, kind, token, side, price, size, exchange),
                )


def paired(database):
    event(database, 1)
    event(database, 1, token="456")


def load(config):
    return asyncio.run(load_capture_markets(config))


def sql(database, query, parameters=()):
    with sqlite3.connect(database) as connection:
        connection.execute(query, parameters)


def test_audit_empty_ddl_and_compact_variant_is_bounded_read_only(database):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    audit = audit_capture(database, sample_limit=1)
    assert audit["read_only"]
    assert not audit["tables"]["book_events"]["populated"]
    assert not audit["reconstruction_supported"]
    assert audit["tables"]["archive_levels_compact"]["without_rowid"]
    assert "heartbeat_diagnostics" in audit["tables"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not database.with_name(database.name + "-journal").exists()


def test_audit_samples_not_counts_and_never_returns_payload(database):
    paired(database)
    sql(database, "UPDATE raw_frames SET payload=?", (b"secret-payload" * 1000,))
    audit = audit_capture(database, sample_limit=1)
    events = audit["tables"]["book_events"]
    assert events["populated"] and events["sample_truncated"]
    assert len(events["sample"]) == 1
    assert "payload" not in audit["tables"]["raw_frames"]["sample"][0]
    assert audit["tables"]["raw_frames"]["sample"][0]["payload_bytes"] == 14_000
    assert "COUNT" not in repr(audit)
    assert not audit["coverage_verified"]


@pytest.mark.parametrize("table", capture.ARCHIVE_TABLES)
def test_populated_archive_not_silently_concatenated(database, config, table):
    paired(database)
    if table == "raw_blocks":
        sql(database, f"INSERT INTO {table} VALUES (1,'zlib-framed-v1',X'789C')")
    elif table == "archive_trades":
        sql(database, f"INSERT INTO {table} VALUES (1,1,5,'zlib-framed-v1',X'789C')")
    else:
        sql(database, f"INSERT INTO {table} VALUES (1,1,5,'zlib-framed-v1',X'789C')")
    assert table in audit_capture(database)["populated_archives"]
    with pytest.raises(CaptureError, match="reconstruction/overlap/missing spans unsupported"):
        load(config)


@pytest.mark.parametrize("table", capture.REST_TABLES)
def test_populated_rest_history_is_not_silently_ignored(database, config, table):
    paired(database)
    sql(database, f"CREATE TABLE {table} (session_id INTEGER, received_at REAL, payload BLOB)")
    sql(database, f"INSERT INTO {table} VALUES (1,?,X'00')", (START + 2,))
    report = audit_capture(database)
    assert report["tables"][table]["populated"]
    assert table in report["populated_rest_tables"]
    assert not report["reconstruction_supported"]
    with pytest.raises(CaptureError, match=table + ": reconstruction"):
        load(config)


def test_archive_audit_reports_only_observed_overlap_and_noncontiguity(database):
    for left, right in ((1, 5), (4, 7), (10, 12)):
        sql(
            database,
            "INSERT INTO archive_levels VALUES (1,?,?,'zlib-framed-v1',X'789C')",
            (left, right),
        )
    audit = audit_capture(database)
    diagnostic = audit["archive_span_diagnostics"][0]
    assert diagnostic["overlap_observed"]
    assert diagnostic["noncontiguous_sample_ranges"]
    assert not diagnostic["full_span_verification"]


def test_snapshots_absolute_deltas_delete_and_integrated_trade(database, config):
    paired(database)
    event(database, 2, kind="delta", levels=[("BUY", "0.40", "7.25")], offset=1)
    event(database, 2, kind="trade", levels=[("SELL", "0.40", "1.00")], offset=1)
    event(database, 3, kind="delta", levels=[("BUY", "0.40", "0.00")], offset=2)
    markets, records, instruments = load(config)
    assert markets[0]["outcomes"] == {"UP": "123", "DOWN": "456"}
    assert markets[0]["start_ns"] == START * SECOND
    assert markets[0]["close_ns"] == (START + 5) * SECOND
    assert all(not item.info for item in instruments["condition"].values())
    up = records["condition"]["UP"]
    assert isinstance(up[0], OrderBookDeltas)
    assert isinstance(up[2], TradeTick)
    assert up[0].deltas[0].action == BookAction.CLEAR
    assert up[0].deltas[-1].flags & int(RecordFlag.F_LAST)
    assert up[1].deltas[0].action == BookAction.UPDATE
    assert up[3].deltas[0].action == BookAction.DELETE
    book = OrderBook(instruments["condition"]["UP"].id, BookType.L2_MBP)
    book.apply_deltas(up[0])
    book.apply_deltas(up[1])
    assert float(book.best_bid_size()) == 7.25
    book.apply_deltas(up[3])
    assert book.best_bid_price() is None


def test_empty_snapshot_event_clears_book_atomically(database, config):
    paired(database)
    event(database, 2, levels=[], offset=1)
    _, records, instruments = load(config)
    empty = records["condition"]["UP"][-1]
    assert len(empty.deltas) == 1
    assert empty.deltas[0].action == BookAction.CLEAR
    assert empty.deltas[0].flags & int(RecordFlag.F_SNAPSHOT)
    assert empty.deltas[0].flags & int(RecordFlag.F_LAST)
    book = OrderBook(instruments["condition"]["UP"].id, BookType.L2_MBP)
    for record in records["condition"]["UP"]:
        book.apply_deltas(record)
    assert book.best_bid_price() is None and book.best_ask_price() is None


def test_exchange_inversions_retained_not_sorted_and_arrival_is_causal(database, config):
    paired(database)
    event(database, 2, offset=2, exchange=(START + 1) * 1000)
    event(database, 3, offset=3, exchange=START * 1000)
    markets, records, _ = load(config)
    up = records["condition"]["UP"]
    assert [row.ts_init for row in up] == [
        START * SECOND,
        (START + 2) * SECOND,
        (START + 3) * SECOND,
    ]
    assert up[-1].ts_event == START * SECOND
    assert markets[0]["capture_diagnostics"]["exchange_timestamp_inversions"] == [
        {"frame_id": 3, "event_index": 0}
    ]


@pytest.mark.parametrize("kind", ["snapshot", "trade"])
def test_missing_exchange_timestamp_is_not_replaced_with_fresh_arrival(database, config, kind):
    paired(database)
    event(database, 2, kind=kind, offset=1)
    sql(database, "UPDATE book_events SET exchange_timestamp_ms=NULL WHERE frame_id=2")
    with pytest.raises(
        CaptureError, match="Missing exchange timestamp; source freshness is unknown"
    ):
        load(config)


@pytest.mark.parametrize("kind", ["gap", "divergent", "reconnect", "overflow"])
def test_invalid_coverage_resets_requires_new_snapshot(database, config, kind):
    paired(database)
    event(database, 2, kind="delta", offset=1)
    event(database, 3, kind="delta", offset=2)
    event(database, 4, offset=3)
    sql(
        database,
        "INSERT INTO coverage_intervals VALUES (1,?,?,?)",
        (START + 0.5, START + 1.5, kind),
    )
    config["data"]["contract"]["coverage_intervals"] = {
        "evidence": "Fixture half-open arrival-clock coverage intervals",
        "start_column": "start_at",
        "end_column": "end_at",
        "status_column": "status",
        "timestamp_unit": "s",
    }
    markets, records, _ = load(config)
    up = records["condition"]["UP"]
    assert not any(delta.action == BookAction.UPDATE for record in up for delta in record.deltas)
    assert up[-1].deltas[0].flags & int(RecordFlag.F_SNAPSHOT)
    assert markets[0]["capture_diagnostics"]["suppressed_events"] == 2


def test_gap_between_frames_and_segment_transition_reset_both_books(database, config):
    paired(database)
    event(database, 2, kind="delta", offset=2)
    event(database, 3, offset=3)
    sql(database, "UPDATE raw_frames SET segment_id=2 WHERE id=3")
    sql(database, "INSERT INTO gaps VALUES (1,?,?)", (START + 0.5, START + 1))
    config["data"]["contract"]["gaps"] = {
        "evidence": "Fixture half-open gap boundaries on arrival clock",
        "start_column": "start_at",
        "end_column": "end_at",
        "timestamp_unit": "s",
    }
    markets, records, _ = load(config)
    assert markets[0]["capture_diagnostics"]["reset_count"] == 2
    assert all(len(row.deltas) == 1 for row in records["condition"]["DOWN"][1:])
    assert markets[0]["capture_diagnostics"]["suppressed_events"] == 1


def test_sequence_discontinuity_suppresses_delta_until_snapshot(database, config):
    paired(database)
    event(database, 3, kind="delta", offset=1)
    event(database, 4, offset=2)
    markets, records, _ = load(config)
    assert markets[0]["capture_diagnostics"]["reset_count"] == 1
    assert markets[0]["capture_diagnostics"]["suppressed_events"] == 1
    assert not any(
        delta.action == BookAction.UPDATE
        for record in records["condition"]["UP"]
        for delta in record.deltas
    )


def test_session_sequence_not_frame_id_drives_replay_chronology(database, config):
    event(database, 20)
    event(database, 20, token="456")
    event(database, 10, offset=1)
    sql(database, "UPDATE raw_frames SET sequence=1 WHERE id=20")
    sql(database, "UPDATE raw_frames SET sequence=2 WHERE id=10")
    markets, records, _ = load(config)
    up = records["condition"]["UP"]
    assert [row.ts_init for row in up] == [START * SECOND, (START + 1) * SECOND]
    assert [row.deltas[0].sequence for row in up] == [1, 2]
    assert markets[0]["capture_diagnostics"]["frame_id_inversions"] == [
        {"frame_id": 10, "sequence": 2}
    ]


def test_segment_sequence_reset_fails_closed_instead_of_guessing_epoch(database, config):
    paired(database)
    event(database, 2, offset=1)
    sql(database, "UPDATE raw_frames SET segment_id=2, sequence=1 WHERE id=2")
    with pytest.raises(CaptureError, match="session sequence; resets unsupported"):
        load(config)


def test_disjoint_complete_coverage_does_not_bridge_unobserved_span(database, config):
    paired(database)
    event(database, 2, kind="delta", offset=3)
    event(database, 3, offset=4)
    sql(database, "DELETE FROM coverage_intervals")
    for left, right in ((START, START + 1), (START + 2, START + 6)):
        sql(database, "INSERT INTO coverage_intervals VALUES (1,?,?,'complete')", (left, right))
    config["data"]["contract"]["coverage_intervals"] = {
        "evidence": "Fixture half-open complete interval contract",
        "start_column": "start_at",
        "end_column": "end_at",
        "status_column": "status",
        "timestamp_unit": "s",
    }
    markets, records, _ = load(config)
    assert markets[0]["capture_diagnostics"]["reset_count"] == 1
    assert markets[0]["capture_diagnostics"]["suppressed_events"] == 1
    assert len(records["condition"]["UP"]) == 3


def test_outside_explicit_complete_coverage_never_establishes_snapshot_anchor(database, config):
    paired(database)
    event(database, 2, offset=1.5)
    event(database, 3, kind="delta", offset=2.5)
    event(database, 4, offset=3)
    sql(database, "DELETE FROM coverage_intervals")
    for left, right in ((START, START + 1), (START + 2, START + 4)):
        sql(database, "INSERT INTO coverage_intervals VALUES (1,?,?,'complete')", (left, right))
    config["data"]["contract"]["coverage_intervals"] = {
        "evidence": "Fixture explicit complete coverage on arrival clock",
        "start_column": "start_at",
        "end_column": "end_at",
        "status_column": "status",
        "timestamp_unit": "s",
    }
    markets, records, _ = load(config)
    assert markets[0]["capture_diagnostics"]["suppressed_events"] == 2
    assert not any(
        delta.action == BookAction.UPDATE
        for row in records["condition"]["UP"]
        for delta in row.deltas
    )
    assert records["condition"]["UP"][-1].deltas[0].flags & int(RecordFlag.F_SNAPSHOT)


def test_undeclared_populated_coverage_not_ignored(database, config):
    paired(database)
    sql(database, "INSERT INTO coverage_intervals VALUES (1,?,?,'complete')", (START, START + 6))
    del config["data"]["contract"]["coverage_intervals"]
    with pytest.raises(CaptureError, match="boundary contract"):
        load(config)


def test_missing_snapshot_anchor_suppresses_delta_and_trade(database, config):
    event(database, 1, kind="delta")
    event(database, 1, token="456")
    event(database, 2, kind="trade", levels=[("BUY", "0.40", "1.00")], offset=1)
    markets, records, _ = load(config)
    assert markets[0]["load_error"]
    assert records["condition"]["UP"] == []
    assert markets[0]["capture_diagnostics"]["suppressed_events"] == 2


def test_late_snapshot_after_suppressed_opening_delta_never_becomes_eligible(database, config):
    event(database, 1, kind="delta")
    event(database, 1, token="456")
    event(database, 2, offset=0.01)
    markets, records, _ = load(config)
    assert records["condition"]["UP"]
    assert markets[0]["capture_diagnostics"]["opening_snapshot_ns"] == {
        "UP": None,
        "DOWN": START * SECOND,
    }
    assert markets[0]["censored"] and not markets[0]["eligible"]
    assert "snapshot anchors at/before exact market open" in markets[0]["load_error"]


def test_capture_order_keys_align_snapshots_deltas_trades_and_resets_without_epsilon(
    database, config
):
    paired(database)
    event(database, 2, kind="delta", offset=1, levels=[("BUY", "0.40", "5.00")])
    event(database, 2, kind="trade", offset=1, levels=[("SELL", "0.40", "1.00")])
    event(database, 4, offset=2)
    event(database, 4, token="456", offset=2)
    markets, records, instruments = load(config)
    keys = markets[0]["capture_record_order"]
    assert keys == {
        "UP": [(1, 0), (2, 0), (2, 1), (4, -1), (4, 0)],
        "DOWN": [(1, 1), (4, -1), (4, 1)],
    }
    for side in ("UP", "DOWN"):
        assert len(keys[side]) == len(records["condition"][side])
        assert not instruments["condition"][side].info
    up = records["condition"]["UP"]
    assert isinstance(up[2], TradeTick)
    assert up[3].deltas[0].action == BookAction.CLEAR
    assert [record.ts_init for record in up] == [
        START * SECOND,
        (START + 1) * SECOND,
        (START + 1) * SECOND,
        (START + 2) * SECOND,
        (START + 2) * SECOND,
    ]


def test_causal_observations_use_arrival_and_keep_exchange_diagnostics(database, config):
    from prediction_market_extensions.backtesting.longshot.state import observations_from_records

    paired(database)
    event(
        database,
        2,
        levels=[("BUY", "0.70", "5.00"), ("SELL", "0.80", "5.00")],
        offset=3,
        exchange=(START + 1) * 1000,
    )
    _, records, _ = load(config)
    observations = observations_from_records(records["condition"])
    assert [row.ts_ns for row in observations] == [START * SECOND, (START + 3) * SECOND]
    assert observations[-1].up.midpoint == 0.75
    assert observations[-1].up.ts_ns == (START + 1) * SECOND


def test_planned_session_timing_variant_does_not_use_connection_times(database, config):
    paired(database)
    config["data"]["contract"].update(start_column="planned_start_at", end_column="planned_end_at")
    markets, _, _ = load(config)
    assert markets[0]["start_ns"] == START * SECOND
    assert markets[0]["close_ns"] == (START + 5) * SECOND


def test_alternate_started_ended_columns_require_explicit_market_timing_contract(database, config):
    paired(database)
    sql(database, "UPDATE sessions SET started_at=start_at,ended_at=end_at")
    sql(database, "ALTER TABLE sessions DROP COLUMN start_at")
    sql(database, "ALTER TABLE sessions DROP COLUMN end_at")
    with pytest.raises(CaptureError, match="Missing required sessions columns"):
        load(config)
    config["data"]["contract"].update(
        start_column="started_at",
        end_column="ended_at",
        evidence="This fixture variant uses started_at/ended_at for MARKET timing, not recording time",
    )
    markets, _, _ = load(config)
    assert markets[0]["start_ns"] == START * SECOND


@pytest.mark.parametrize("unit", ["ms", "iso"])
def test_observed_timestamp_encoding_variants_are_explicit(database, config, unit):
    paired(database)
    if unit == "ms":
        sql(database, "UPDATE sessions SET start_at=start_at*1000,end_at=end_at*1000")
        sql(database, "UPDATE raw_frames SET received_at=received_at*1000")
    else:
        sql(
            database,
            "UPDATE sessions SET start_at='2026-07-01T00:00:00Z', end_at='2026-07-01T00:00:05Z'",
        )
        sql(database, "UPDATE raw_frames SET received_at='2026-07-01T00:00:00Z'")
    config["data"]["contract"]["timestamp_unit"] = unit
    markets, records, _ = load(config)
    assert markets[0]["start_ns"] == START * SECOND
    assert records["condition"]["UP"][0].ts_init == START * SECOND


def test_duplicate_atomic_price_level_is_not_applied_in_arbitrary_order(database, config):
    paired(database)
    sql(
        database,
        "INSERT INTO book_levels VALUES (1,0,2,'snapshot','123','BUY','0.40','99.00',?,NULL)",
        (START * 1000,),
    )
    with pytest.raises(CaptureError, match="Duplicate price/side"):
        load(config)


def test_session_limit_and_duplicate_market_sessions_are_not_silently_merged(database, config):
    paired(database)
    sql(
        database,
        "INSERT INTO sessions (id,condition_id,start_at,end_at,up_token_id,down_token_id,asset,flags)"
        " VALUES (2,'condition',?,?,'123','456','BTC',0)",
        (START, START + 5),
    )
    config["data"]["session_limit"] = 1
    with pytest.raises(CaptureError, match="sessions exceeds"):
        load(config)
    config["data"]["session_limit"] = 2
    with pytest.raises(CaptureError, match="duplicate condition session"):
        load(config)


def test_explicit_session_ids_avoid_unselected_session_scan_and_require_exact_matches(
    database, config
):
    paired(database)
    sql(
        database,
        "INSERT INTO sessions (id,condition_id) VALUES (2,'unselected')",
    )
    config["data"].update(session_ids=[1], session_limit=1)
    markets, _, _ = load(config)
    assert [market["session_id"] for market in markets] == [1]
    config["data"]["session_ids"] = [99]
    with pytest.raises(CaptureError, match="sessions missing"):
        load(config)


def test_explicit_ids_use_per_primary_key_lookups_in_large_inventory(database, config, monkeypatch):
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO sessions (id,condition_id,flags) VALUES (?, ?, 0)",
            ((number, f"unselected-{number}") for number in range(2, 2002)),
        )
    paired(database)
    original_reader = capture._reader
    queries = []

    @contextmanager
    def traced_reader(path):
        with original_reader(path) as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(capture, "_reader", traced_reader)
    config["data"].update(session_ids=[1], session_limit=1)
    markets, _, _ = load(config)
    assert len(markets) == 1
    session_queries = [query for query in queries if " FROM sessions" in query]
    assert len(session_queries) == 1
    assert "WHERE id=1 LIMIT 2" in session_queries[0]
    orphan_queries = [query for query in queries if " AS child " in query]
    assert orphan_queries and all("WHERE child.frame_id=1" in query for query in orphan_queries)
    for selection in ([None], [], [1, 1]):
        config["data"]["session_ids"] = selection
        with pytest.raises(CaptureError, match="session_ids"):
            load(config)


def test_read_only_uri_reader_rejects_accidental_write(database):
    with pytest.raises(CaptureError, match="readonly"), capture._reader(database) as connection:
        connection.execute("DELETE FROM sessions")


def test_populated_reconciliation_never_retroactively_repairs(database, config):
    paired(database)
    sql(database, "INSERT INTO reconciliations VALUES (1,X'00')")
    assert not audit_capture(database)["reconstruction_supported"]
    with pytest.raises(CaptureError, match="reconciliations"):
        load(config)


def test_external_resolution_mapping_not_in_instrument_info(database, config):
    paired(database)
    config["data"]["resolutions"] = {
        "condition": {
            "winner": "DOWN",
            "settlement_ns": (START + 6) * SECOND,
            "verified": True,
            "evidence": "Synthetic fixture settlement outcome declaration",
        }
    }
    markets, _, instruments = load(config)
    assert markets[0]["settlement_winner"] == "DOWN"
    assert all(not item.info for item in instruments["condition"].values())
    replay = BookReplay(market_slug="condition", token_index=1, outcome="DOWN")
    loaded = asyncio.run(load_capture_replay(config, replay))
    assert loaded.realized_outcome == 1.0
    assert loaded.requested_window.start_ns == START * SECOND
    assert loaded.records and loaded.count == 1


@pytest.mark.parametrize(
    "resolution",
    [
        {"winner": "UP", "settlement_ns": (START + 6) * SECOND},
        {
            "winner": "UP",
            "settlement_ns": (START + 6) * SECOND,
            "verified": False,
            "evidence": "fixture",
        },
        {"winner": "UP", "settlement_ns": (START + 6) * SECOND, "verified": True, "evidence": ""},
    ],
)
def test_resolution_labels_require_verified_separate_evidence(database, config, resolution):
    paired(database)
    config["data"]["resolutions"] = {"condition": resolution}
    with pytest.raises(CaptureError, match="verified:true, evidence"):
        load(config)


def test_inventory_resolution_labels_alone_are_rejected(database, config):
    paired(database)
    config["markets"] = [{"market_id": "condition", "settlement_winner": "UP"}]
    with pytest.raises(CaptureError, match="verified separate resolution"):
        load(config)


@pytest.mark.parametrize("table", ["sessions", "raw_frames", "book_events"])
def test_unknown_validity_flags_fail_closed(database, config, table):
    paired(database)
    sql(database, f"UPDATE {table} SET flags=NULL")
    with pytest.raises(CaptureError, match="unknown recorder flags"):
        load(config)


@pytest.mark.parametrize("flags", [None, 1, "unknown"])
def test_populated_segment_invalidity_is_not_ignored(database, config, flags):
    paired(database)
    sql(database, "ALTER TABLE segments ADD COLUMN flags")
    sql(database, "INSERT INTO segments (id,session_id,flags) VALUES (1,1,?)", (flags,))
    with pytest.raises(CaptureError, match="recorder flags"):
        load(config)


def test_segment_status_requires_declared_complete_validity(database, config):
    paired(database)
    sql(database, "ALTER TABLE segments ADD COLUMN flags INTEGER")
    sql(database, "ALTER TABLE segments ADD COLUMN status TEXT")
    sql(database, "INSERT INTO segments (id,session_id,flags,status) VALUES (1,1,0,'divergent')")
    with pytest.raises(CaptureError, match="validity contract"):
        load(config)
    config["data"]["contract"]["segments"] = {
        "evidence": "Fixture segment status names; only complete establishes validity",
        "status_column": "status",
        "valid_value": "complete",
    }
    with pytest.raises(CaptureError, match="Invalid/unknown segment status"):
        load(config)
    sql(database, "UPDATE segments SET status='complete'")
    markets, _, _ = load(config)
    assert markets[0]["capture_diagnostics"]["coverage_verified"]


def test_missing_complete_coverage_censors_even_snapshot_anchored_market(database, config):
    paired(database)
    sql(database, "DELETE FROM coverage_intervals")
    markets, records, _ = load(config)
    assert records["condition"]["UP"]
    assert markets[0]["censored"] and not markets[0]["eligible"]
    assert "Missing verified complete coverage" in markets[0]["load_error"]


def test_replay_subwindow_is_not_fabricated(database, config):
    paired(database)
    replay = BookReplay(market_slug="condition", outcome="UP", start_time="2026-07-01T00:00:01Z")
    with pytest.raises(CaptureError, match="subwindow reconstruction"):
        asyncio.run(load_capture_replay(config, replay))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            "UPDATE raw_frames SET received_at=received_at-2 WHERE id=2",
            "Arrival timestamp inversion",
        ),
        (
            (
                "UPDATE book_events SET exchange_timestamp_ms=(received_at+1)*1000 "
                "FROM raw_frames WHERE frame_id=raw_frames.id"
            ),
            "ahead of arrival",
        ),
        ("UPDATE book_events SET asset_id='alien'", "contradicts session mapping"),
        ("UPDATE book_levels SET side='BID'", "side contradicts"),
        ("UPDATE raw_frames SET flags=1", "flags"),
        ("UPDATE sessions SET up_token_id=down_token_id", "distinct"),
        ("DELETE FROM book_events WHERE frame_id=2", "Orphaned"),
    ],
)
def test_fail_closed_invalid_capture(database, config, change, message):
    paired(database)
    event(database, 2, offset=1)
    sql(database, change)
    with pytest.raises(CaptureError, match=message):
        load(config)


@pytest.mark.parametrize(
    "key", ["evidence", "buy_side", "zero_size", "delta_size", "arrival_order"]
)
def test_no_invented_recorder_semantics(database, config, key):
    paired(database)
    del config["data"]["contract"][key]
    with pytest.raises(CaptureError, match="contract"):
        load(config)


def test_raw_only_unknown_normalization_is_rejected(database, config):
    sql(
        database,
        "INSERT INTO raw_frames (id,session_id,segment_id,sequence,received_at,flags) "
        "VALUES (1,1,1,1,?,0)",
        (START,),
    )
    with pytest.raises(CaptureError, match="without normalized events"):
        load(config)


def test_limits_fail_not_truncate_and_audit_limit_validated(database, config):
    paired(database)
    config["data"]["record_limit"] = 1
    with pytest.raises(CaptureError, match="limit"):
        load(config)
    for limit in (0, -1, True, 101):
        with pytest.raises(CaptureError, match="sample_limit"):
            audit_capture(database, limit)


def test_locked_missing_input_fail_without_copy_or_checkpoint(database, config):
    before = database.read_bytes()
    writer = sqlite3.connect(database)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(CaptureError, match="locked"):
            audit_capture(database)
        with pytest.raises(CaptureError, match="locked"):
            load(config)
    finally:
        writer.rollback()
        writer.close()
    assert database.read_bytes() == before
    missing = database.with_name("missing.sqlite")
    with pytest.raises(FileNotFoundError):
        audit_capture(missing)
    assert not missing.exists()


def test_capture_loading_offloads_sqlite_from_event_loop(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def blocking_loader(config):
        started.set()
        assert release.wait(5)
        return [], {}, {}

    monkeypatch.setattr(capture, "_load_capture_markets", blocking_loader)

    async def run():
        task = asyncio.create_task(load_capture_markets({}))
        await asyncio.to_thread(started.wait, 5)
        assert not task.done()
        release.set()
        return await task

    assert asyncio.run(run()) == ([], {}, {})


def test_native_sqlite_cli_all_network_free_end_to_end(database, config):
    from prediction_market_extensions.backtesting.longshot.cli import read_dataset

    for frame, offset in enumerate((0, 1, 1.15, 2, 2.15, 3, 4, 5, 5.5), start=1):
        up, down = ("0.40", "0.60"), ("0.40", "0.60")
        if offset >= 1:
            up, down = ("0.72", "0.74"), ("0.26", "0.28")
        if offset >= 2:
            up, down = ("0.26", "0.28"), ("0.72", "0.74")
        for token, (bid, ask) in (("123", up), ("456", down)):
            event(
                database,
                frame,
                token=token,
                offset=offset,
                levels=[("BUY", bid, "20.00"), ("SELL", ask, "20.00")],
            )
    event(database, 6, kind="trade", offset=3, levels=[("SELL", "0.26", "1.00")])
    config.update(
        partitions={
            "discovery_end": "2026-07-02T00:00:00Z",
            "validation_end": "2026-07-03T00:00:00Z",
            "final_end": "2026-07-04T00:00:00Z",
        },
        bootstrap_samples=10,
        markets=[
            {
                "market_id": "condition",
                "fee_rate": 0.02,
                "fee_schedule": "polymarket_p*(1-p)",
                "fee_verified": True,
                "fee_source": "Synthetic fixture fee contract, not a historical CLOB fee claim",
                "fee_valid_from_ns": START * SECOND,
                "fee_valid_to_ns": (START + 5) * SECOND,
            }
        ],
    )
    config["data"]["resolutions"] = {
        "condition": {
            "winner": "DOWN",
            "settlement_ns": (START + 6) * SECOND,
            "verified": True,
            "evidence": "Synthetic fixture settlement outcome declaration",
        }
    }
    config_path = database.parent / "config.json"
    config_path.write_text(json.dumps(config))
    output = database.parent / "cli-output"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/longshot_flip_70.py",
            "all",
            "--config",
            str(config_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 audited markets, 2 touches" in result.stdout
    audit = read_dataset(output / "market_audit.csv")
    assert audit[0]["complete"] and audit[0]["eligible"]
    execution = json.loads((output / "execution.json").read_text())
    assert execution.get("status") != "not_run_no_eligible_markets"
    assert len(execution["variants"]) == 6
    assert any(trade["entered"] for trade in read_dataset(output / "trades.csv"))
    assert (output / "analysis.json").exists()


@pytest.mark.parametrize("problem", ["missing_coverage", "same_arrival_reset", "complete_span_gap"])
def test_native_cli_all_never_executes_unverified_or_reset_market(database, config, problem):
    from prediction_market_extensions.backtesting.longshot.cli import read_dataset

    paired(database)
    for frame, offset in ((2, 2), (3, 5.5)):
        event(database, frame, offset=offset)
        event(database, frame, token="456", offset=offset)
    if problem == "missing_coverage":
        sql(database, "DELETE FROM coverage_intervals")
    elif problem == "same_arrival_reset":
        sql(database, "INSERT INTO gaps VALUES (1,?,?)", (START + 0.5, START + 1))
        config["data"]["contract"]["gaps"] = {
            "evidence": "Fixture known missing interval before fresh snapshots",
            "start_column": "start_at",
            "end_column": "end_at",
            "timestamp_unit": "s",
        }
    else:
        sql(database, "DELETE FROM coverage_intervals")
        for left, right in ((START, START + 1), (START + 1.5, START + 6)):
            sql(database, "INSERT INTO coverage_intervals VALUES (1,?,?,'complete')", (left, right))
    markets, records, _ = load(config)
    assert markets[0]["load_error"]
    assert markets[0]["censored"] and not markets[0]["eligible"]
    if problem != "missing_coverage":
        up = records["condition"]["UP"]
        at_reset = [record for record in up if record.ts_init == (START + 2) * SECOND]
        assert len(at_reset) == 2
        assert at_reset[0].deltas[0].action == BookAction.CLEAR
        assert at_reset[1].deltas[0].flags & int(RecordFlag.F_SNAPSHOT)
    config.update(
        partitions={
            "discovery_end": "2026-07-02T00:00:00Z",
            "validation_end": "2026-07-03T00:00:00Z",
            "final_end": "2026-07-04T00:00:00Z",
        },
        bootstrap_samples=10,
    )
    config_path = database.parent / "quarantine-config.json"
    config_path.write_text(json.dumps(config))
    output = database.parent / "quarantine-output"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/longshot_flip_70.py",
            "all",
            "--config",
            str(config_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    execution = json.loads((output / "execution.json").read_text())
    assert execution["status"] == "not_run_no_eligible_markets"
    assert read_dataset(output / "trades.csv") == []
    audit = read_dataset(output / "market_audit.csv")[0]
    assert audit["censored"] and not audit["complete"] and not audit["eligible"]
