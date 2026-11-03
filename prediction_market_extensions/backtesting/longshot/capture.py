"""Read-only, bounded SQLite capture inspection and evidence-gated L2 replay.

Normalized recorder tables are not a documented exchange protocol. Replay therefore
requires a caller-supplied contract, including a reference to the recorder/version
which establishes its semantics. Compressed archives and reconciliation payloads
are inventoried, not silently spliced into the live stream.
"""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ARCHIVE_TABLES = (
    "archive_levels",
    "archive_levels_compact",
    "archive_trades",
    "raw_blocks",
)
REST_TABLES = ("rest_snapshots", "rest_levels")
DIAGNOSTIC_TABLES = (
    "segments",
    "coverage_intervals",
    "gaps",
    "reconciliations",
    "checkpoint_diagnostics",
    "timestamp_inversions",
    "heartbeat_diagnostics",
)
MAX_TABLES = 100
MAX_COLUMNS = 100
MAX_SAMPLE = 100


class CaptureError(ValueError):
    """The capture cannot establish a safe, causal replay."""


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def _reader(path: Path):
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise CaptureError(f"Capture is not a regular file: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    connection.row_factory = sqlite3.Row
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8_000_000)
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        # Bounds even unindexed scans, corrupt views and adversarial schemas.
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return int(steps > 20_000)

        connection.set_progress_handler(progress, 1000)
        yield connection
    except sqlite3.Error as error:
        raise CaptureError(f"Read-only capture access failed: {error}") from error
    finally:
        connection.close()


def _schema(connection):
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name LIMIT ?",
        (MAX_TABLES + 1,),
    ).fetchall()
    if len(rows) > MAX_TABLES:
        raise CaptureError("Schema exceeds bounded table limit")
    schema = {}
    for row in rows:
        columns = connection.execute(
            "SELECT name, type, pk FROM pragma_table_info(?) LIMIT ?",
            (row["name"], MAX_COLUMNS + 1),
        ).fetchall()
        if len(columns) > MAX_COLUMNS:
            raise CaptureError("Schema exceeds bounded column limit")
        schema[row["name"]] = {
            "columns": {column["name"]: column["type"] for column in columns},
            "primary_key": [
                column["name"]
                for column in sorted(columns, key=lambda column: column["pk"])
                if column["pk"]
            ],
            "without_rowid": "WITHOUT ROWID" in (row["sql"] or "").upper(),
        }
    return schema


def _require(schema, table, columns):
    if table not in schema or not set(columns) <= set(schema[table]["columns"]):
        raise CaptureError(f"Missing required {table} columns: {', '.join(columns)}")


def _bounded_rows(connection, query, parameters, limit, label):
    cursor = connection.execute(query + " LIMIT ?", (*parameters, limit + 1))
    for index, row in enumerate(cursor):
        if index == limit:
            raise CaptureError(f"{label} exceeds configured limit {limit}; replay not truncated")
        yield dict(row)


def _limit(value, name, maximum=1_000_000):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise CaptureError(f"{name} must be an integer in [1, {maximum}]")
    return value


def audit_capture(path: Path, sample_limit: int = 20) -> dict[str, Any]:
    """Inspect bounded samples; no COUNT, decompression, checkpoint or DB writes.

    ``populated`` is an observed existence probe, not an inference from DDL.
    Samples (including archive spans) are incomplete and never certify coverage.
    Payloads are deliberately excluded and text values limited to 256 characters.
    """
    sample_limit = _limit(sample_limit, "sample_limit", MAX_SAMPLE)
    with _reader(Path(path)) as connection:
        schema = _schema(connection)
        tables = {}
        for table, details in schema.items():
            columns = details["columns"]
            expressions = []
            for column in columns:
                quoted = _identifier(column)
                if column in {"payload", "data", "blob", "compressed_data"} or (
                    "BLOB" in columns[column].upper()
                ):
                    expressions.append(f"length({quoted}) AS {_identifier(column + '_bytes')}")
                else:
                    expressions.append(f"substr(CAST({quoted} AS TEXT),1,256) AS {quoted}")
            rows = connection.execute(
                f"SELECT {', '.join(expressions)} FROM {_identifier(table)} LIMIT ?",
                (sample_limit + 1,),
            ).fetchall()
            tables[table] = {
                **details,
                "populated": bool(rows),
                "sample": [dict(row) for row in rows[:sample_limit]],
                "sample_truncated": len(rows) > sample_limit,
            }
        archives = [name for name in ARCHIVE_TABLES if tables.get(name, {}).get("populated")]
        rest_tables = [name for name in REST_TABLES if tables.get(name, {}).get("populated")]
        normalized = all(
            tables.get(name, {}).get("populated") for name in ("raw_frames", "book_events")
        )
        reconciliations = tables.get("reconciliations", {}).get("populated", False)
        span_diagnostics = []
        for name in archives:
            samples = tables[name]["sample"]
            for left_column, right_column in (
                ("first_frame_id", "last_frame_id"),
                ("start_frame_id", "end_frame_id"),
                ("first_sequence", "last_sequence"),
                ("start_sequence", "end_sequence"),
            ):
                if not {left_column, right_column} <= tables[name]["columns"].keys():
                    continue
                groups = {}
                for sample in samples:
                    try:
                        interval = (int(sample[left_column]), int(sample[right_column]))
                    except (TypeError, ValueError):
                        continue
                    groups.setdefault(sample.get("session_id"), []).append(interval)
                for session_id, intervals in groups.items():
                    ordered = sorted(intervals)
                    span_diagnostics.append(
                        {
                            "table": name,
                            "session_id": session_id,
                            "columns": [left_column, right_column],
                            "sampled_ranges": ordered,
                            "overlap_observed": any(
                                following[0] <= previous[1]
                                for previous, following in itertools.pairwise(ordered)
                            ),
                            "noncontiguous_sample_ranges": any(
                                following[0] > previous[1] + 1
                                for previous, following in itertools.pairwise(ordered)
                            ),
                            "full_span_verification": False,
                        }
                    )
        return {
            "source_kind": "sqlite_capture",
            "path": str(Path(path).expanduser().resolve()),
            "read_only": True,
            "sample_limit": sample_limit,
            "tables": tables,
            "populated_archives": archives,
            "populated_rest_tables": rest_tables,
            "archive_span_diagnostics": span_diagnostics,
            "normalized_events_observed": normalized,
            "reconstruction_supported": (
                not archives and not rest_tables and not reconciliations and normalized
            ),
            "replay_requires_explicit_contract": True,
            "coverage_verified": False,
            "limitations": [
                "Bounded samples are not counts or complete coverage evidence.",
                "Archive overlap/missing spans cannot be certified from bounded samples.",
                "zlib-framed-v1/raw-block/compact reconstruction is unsupported.",
                "REST snapshot/level availability and reconstruction are unsupported.",
                "Reconciliations cannot retroactively repair historical book state.",
                "Replay validates selected frame relationships, not unattributed global orphan rows.",
                "DDL presence, checkpoints and heartbeat diagnostics do not prove completeness.",
            ],
        }


def _contract(data):
    contract = data.get("contract")
    if not isinstance(contract, Mapping) or not isinstance(contract.get("evidence"), str):
        raise CaptureError("data.contract requires a nonempty evidence reference")
    if not contract["evidence"].strip():
        raise CaptureError("data.contract requires a nonempty evidence reference")
    exact = {
        "arrival_order": "session_sequence",
        "snapshot_kind": "snapshot",
        "delta_kind": "delta",
        "delta_size": "absolute",
        "zero_size": "delete",
        "buy_side": "bid",
    }
    for key, value in exact.items():
        if contract.get(key) != value:
            raise CaptureError(f"Unsupported or unverified contract.{key}; requires {value!r}")
    if contract.get("sequence") not in {"contiguous", "ordering_only"}:
        raise CaptureError("contract.sequence must be contiguous or ordering_only")
    if contract.get("timestamp_unit") not in {"s", "ms", "ns", "iso"}:
        raise CaptureError("contract.timestamp_unit must be s, ms, ns or iso")
    if contract.get("start_column") not in {"start_at", "started_at", "planned_start_at"}:
        raise CaptureError("contract.start_column must name exact market open metadata")
    if contract.get("end_column") not in {"end_at", "ended_at", "planned_end_at"}:
        raise CaptureError("contract.end_column must name exact market close metadata")
    for key in ("price_precision", "size_precision"):
        value = contract.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9:
            raise CaptureError(f"contract.{key} must be an explicit integer in [0, 9]")
    ignored = contract.get("ignored_event_types", [])
    if not isinstance(ignored, list) or any(
        not isinstance(kind, str) or not kind for kind in ignored
    ):
        raise CaptureError("contract.ignored_event_types must be an explicit list of event types")
    return dict(contract)


def _timestamp(value, unit):
    if value is None:
        raise CaptureError("Missing timestamp")
    if unit == "iso":
        from prediction_market_extensions.backtesting.longshot.sources import timestamp_ns

        return timestamp_ns(str(value))
    try:
        timestamp = Decimal(str(value)) * {"s": 1_000_000_000, "ms": 1_000_000, "ns": 1}[unit]
        if not timestamp.is_finite() or timestamp != timestamp.to_integral_value() or timestamp < 0:
            raise CaptureError("Invalid timestamp precision or sign")
        return int(timestamp)
    except (InvalidOperation, KeyError) as error:
        raise CaptureError("Invalid timestamp") from error


def _number(value, precision, *, price=False):
    if not isinstance(value, str):
        raise CaptureError("Recorder price/size must remain exact TEXT")
    try:
        result = Decimal(value)
        if (
            not result.is_finite()
            or result < 0
            or (price and result > 1)
            or result != result.quantize(Decimal(1).scaleb(-precision))
        ):
            raise CaptureError("Invalid or inexact recorder price/size")
        return result
    except InvalidOperation as error:
        raise CaptureError("Invalid recorder price/size") from error


def _source(data):
    sources = data.get("sources", ())
    if len(sources) != 1 or not isinstance(sources[0], str) or not sources[0].startswith("local:"):
        raise CaptureError("Capture requires exactly one explicit local: SQLite source")
    path = Path(sources[0][len("local:") :])
    if not path.is_absolute():
        raise CaptureError("Capture local: source must be absolute")
    return path


def _invalid_intervals(connection, schema, session_id, contract, limit):
    invalid, complete = [], []
    for table in ("coverage_intervals", "gaps"):
        if table not in schema:
            continue
        columns = schema[table]["columns"]
        if "session_id" not in columns:
            if connection.execute(f"SELECT 1 FROM {_identifier(table)} LIMIT 1").fetchone():
                raise CaptureError(f"Populated {table} lacks session attribution")
            continue
        if not connection.execute(
            f"SELECT 1 FROM {_identifier(table)} WHERE session_id=? LIMIT 1", (session_id,)
        ).fetchone():
            continue
        mapping = contract.get(table)
        if (
            not isinstance(mapping, Mapping)
            or not isinstance(mapping.get("evidence"), str)
            or not mapping["evidence"].strip()
        ):
            raise CaptureError(f"Populated {table} requires an evidence-backed boundary contract")
        start, end = mapping.get("start_column"), mapping.get("end_column")
        _require(schema, table, [start or "", end or ""])
        status = mapping.get("status_column") if table == "coverage_intervals" else None
        if table == "coverage_intervals":
            _require(schema, table, [status or ""])
        unit = mapping.get("timestamp_unit")
        if unit not in {"s", "ms", "ns", "iso"}:
            raise CaptureError(f"{table} requires explicit timestamp_unit")
        selected_columns = [start, end] + ([status] if status else [])
        if "flags" in columns:
            selected_columns.append("flags")
        for row in _bounded_rows(
            connection,
            f"SELECT {', '.join(_identifier(column) for column in selected_columns)} "
            f"FROM {_identifier(table)} WHERE session_id=?",
            (session_id,),
            limit,
            table,
        ):
            _flags(row)
            left, right = _timestamp(row[start], unit), _timestamp(row[end], unit)
            if right < left:
                raise CaptureError("Inverted coverage/gap boundary")
            kind = row[status] if status else "gap"
            if kind not in {"complete", "gap", "divergent", "reconnect", "overflow"}:
                raise CaptureError(f"Unsupported coverage status: {kind}")
            (complete if kind == "complete" else invalid).append((left, right, kind))
    merged = []
    for left, right, kind in sorted(complete):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]), kind)
        else:
            merged.append((left, right, kind))
    return sorted(invalid), merged


def _projection(schema, table, required, optional=()):
    columns = [*required, *(name for name in optional if name in schema[table]["columns"])]
    return ", ".join(_identifier(name) for name in columns)


def _flags(row):
    # Unknown recorder flags may denote dropped/partial snapshots. Never erase them.
    if "flags" in row and row["flags"] not in (0, "0"):
        raise CaptureError("Nonzero/unknown recorder flags have unsupported semantics")


def _segments(connection, schema, session_id, contract, limit):
    if "segments" not in schema:
        return None
    columns = schema["segments"]["columns"]
    if not {"id", "session_id"} <= columns.keys():
        if connection.execute("SELECT 1 FROM segments LIMIT 1").fetchone():
            raise CaptureError("Populated segments lacks exact id/session attribution")
        return None
    if not connection.execute(
        "SELECT 1 FROM segments WHERE session_id=? LIMIT 1", (session_id,)
    ).fetchone():
        return None
    if "flags" not in columns:
        raise CaptureError("Populated segment validity requires explicit zero flags")
    mapping = contract.get("segments", {})
    status_column = mapping.get("status_column") if isinstance(mapping, Mapping) else None
    if "status" in columns and not status_column:
        raise CaptureError("Segment status requires an evidence-backed validity contract")
    if status_column and (
        status_column not in columns
        or not isinstance(mapping.get("evidence"), str)
        or not mapping["evidence"].strip()
        or mapping.get("valid_value") != "complete"
    ):
        raise CaptureError("Unsupported segment validity contract")
    projection = ["id", "flags"] + ([status_column] if status_column else [])
    ids = set()
    for row in _bounded_rows(
        connection,
        f"SELECT {', '.join(_identifier(column) for column in projection)} "
        "FROM segments WHERE session_id=?",
        (session_id,),
        min(limit, 10_000),
        "segments",
    ):
        _flags(row)
        if status_column and row[status_column] != mapping["valid_value"]:
            raise CaptureError("Invalid/unknown segment status; replay quarantined")
        if row["id"] in ids:
            raise CaptureError("Duplicate segment identity")
        ids.add(row["id"])
    return ids


def _instrument(condition, token, side, start, close, contract):
    from nautilus_trader.model.currencies import USDC
    from nautilus_trader.model.enums import AssetClass
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import BinaryOption
    from nautilus_trader.model.objects import Price, Quantity

    symbol = Symbol(f"{condition}-{token}")
    price_increment = Decimal(1).scaleb(-contract["price_precision"])
    size_increment = Decimal(1).scaleb(-contract["size_precision"])
    return BinaryOption(
        instrument_id=InstrumentId(symbol, Venue("POLYMARKET")),
        raw_symbol=symbol,
        asset_class=AssetClass.ALTERNATIVE,
        currency=USDC,
        price_precision=contract["price_precision"],
        size_precision=contract["size_precision"],
        price_increment=Price.from_str(str(price_increment)),
        size_increment=Quantity.from_str(str(size_increment)),
        activation_ns=start,
        expiration_ns=close,
        ts_event=start,
        ts_init=start,
        outcome=side,
        info={},
    )


def _session_records(connection, schema, session, pair, contract, limit):
    from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
    from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide, RecordFlag
    from nautilus_trader.model.identifiers import TradeId

    _require(
        schema, "raw_frames", ["id", "session_id", "received_at", "sequence", "segment_id", "flags"]
    )
    _require(schema, "book_events", ["frame_id", "event_index", "kind", "asset_id", "flags"])
    _require(
        schema,
        "book_levels",
        ["frame_id", "event_index", "level_index", "kind", "asset_id", "side", "price", "size"],
    )
    for table in ("book_levels", "book_trades"):
        if table not in schema:
            continue
        _require(schema, table, ["frame_id", "event_index"])
    for table in (*ARCHIVE_TABLES, *REST_TABLES, "reconciliations"):
        if table not in schema:
            continue
        query = f"SELECT 1 FROM {_identifier(table)}"
        parameters = ()
        if "session_id" in schema[table]["columns"]:
            query += " WHERE session_id=?"
            parameters = (session["id"],)
        if connection.execute(query + " LIMIT 1", parameters).fetchone():
            raise CaptureError(
                f"Populated {table}: reconstruction/overlap/missing spans unsupported"
            )
    invalid, complete = _invalid_intervals(
        connection, schema, session["id"], contract, min(limit, 10_000)
    )
    segment_ids = _segments(connection, schema, session["id"], contract, limit)
    records = {"UP": [], "DOWN": []}
    record_order = {"UP": [], "DOWN": []}
    market_start = _timestamp(session[contract["start_column"]], contract["timestamp_unit"])
    mapping = {str(session[f"{side.lower()}_token_id"]): side for side in pair}
    valid = dict.fromkeys(pair, False)
    previous_arrival = previous_sequence = previous_segment = previous_frame_id = None
    previous_exchange = {}
    diagnostics = {
        "exchange_timestamp_inversions": [],
        "frame_id_inversions": [],
        "reset_count": 0,
        "suppressed_events": 0,
        "complete_intervals": complete,
        "invalid_intervals": invalid,
        "coverage_verified": False,
        "opening_snapshot_ns": dict.fromkeys(pair, None),
        "reconciliation_reconstruction": "unsupported",
        "unattributed_orphan_validation": "unsupported; selected frame relationships only",
    }
    total = 0
    frame_projection = _projection(
        schema,
        "raw_frames",
        ["id", "received_at", "sequence", "segment_id"],
        ["flags", "event_type"],
    )
    event_projection = _projection(
        schema,
        "book_events",
        ["frame_id", "event_index", "kind", "asset_id"],
        ["flags", "exchange_timestamp_ms"],
    )
    level_projection = _projection(
        schema,
        "book_levels",
        ["level_index", "kind", "asset_id", "side", "price", "size"],
        ["flags", "exchange_timestamp_ms"],
    )
    frame_query = (
        f"SELECT {frame_projection} FROM raw_frames WHERE session_id=? ORDER BY sequence,id"
    )
    for frame in _bounded_rows(connection, frame_query, (session["id"],), limit, "raw_frames"):
        _flags(frame)
        for table in ("book_levels", "book_trades"):
            if table not in schema:
                continue
            if connection.execute(
                f"SELECT 1 FROM {_identifier(table)} AS child "
                "LEFT JOIN book_events AS parent ON child.frame_id=parent.frame_id "
                "AND child.event_index=parent.event_index WHERE child.frame_id=? "
                "AND parent.frame_id IS NULL LIMIT 1",
                (frame["id"],),
            ).fetchone():
                raise CaptureError(f"Orphaned {table}: atomic event identity is missing")
        if segment_ids is not None and frame["segment_id"] not in segment_ids:
            raise CaptureError("Frame references unknown/unverified segment")
        if not isinstance(frame["id"], int) or frame["id"] < 0:
            raise CaptureError("Capture frames require nonnegative integer IDs")
        arrival = _timestamp(frame["received_at"], contract["timestamp_unit"])
        if previous_arrival is not None and arrival < previous_arrival:
            raise CaptureError("Arrival timestamp inversion in session_sequence chronology")
        sequence = frame["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise CaptureError("Invalid recorder sequence")
        if previous_sequence is not None and sequence <= previous_sequence:
            raise CaptureError("Nonunique/nonincreasing session sequence; resets unsupported")
        if (
            previous_frame_id is not None
            and frame["id"] <= previous_frame_id
            and len(diagnostics["frame_id_inversions"]) < MAX_SAMPLE
        ):
            diagnostics["frame_id_inversions"].append(
                {"frame_id": frame["id"], "sequence": sequence}
            )
        segment_changed = previous_arrival is not None and frame["segment_id"] != previous_segment
        sequence_gap = (
            previous_sequence is not None
            and contract["sequence"] == "contiguous"
            and sequence != previous_sequence + 1
        )
        crossed_gap = any(
            previous_arrival is not None and previous_arrival < right <= arrival
            for _, right, _ in invalid
        )
        inside_gap = any(left <= arrival < right for left, right, _ in invalid)
        outside_complete = bool(complete) and not any(
            left <= arrival < right for left, right, _ in complete
        )
        crossed_complete_gap = (
            bool(complete)
            and previous_arrival is not None
            and not any(left <= previous_arrival <= arrival < right for left, right, _ in complete)
        )
        inside_gap = inside_gap or outside_complete
        if segment_changed or sequence_gap or crossed_gap or inside_gap or crossed_complete_gap:
            diagnostics["reset_count"] += 1
            for side, instrument in pair.items():
                valid[side] = False
                records[side].append(
                    OrderBookDeltas(
                        instrument.id,
                        [
                            OrderBookDelta(
                                instrument.id,
                                BookAction.CLEAR,
                                None,
                                int(RecordFlag.F_LAST),
                                sequence,
                                arrival,
                                arrival,
                            )
                        ],
                    )
                )
                record_order[side].append((sequence, -1))
        previous_arrival, previous_sequence, previous_segment = (
            arrival,
            sequence,
            frame["segment_id"],
        )
        previous_frame_id = frame["id"]
        event_count = 0
        event_indices = set()
        for event in _bounded_rows(
            connection,
            f"SELECT {event_projection} FROM book_events WHERE frame_id=? ORDER BY event_index",
            (frame["id"],),
            limit,
            "book_events",
        ):
            event_count += 1
            _flags(event)
            event_index = event["event_index"]
            if not isinstance(event_index, int) or event_index < 0 or event_index in event_indices:
                raise CaptureError("Invalid/duplicate atomic event_index")
            event_indices.add(event_index)
            total += 1
            if total > limit:
                raise CaptureError("Normalized records exceed record_limit; replay not truncated")
            token = str(event["asset_id"])
            if token not in mapping:
                raise CaptureError(f"Event token {token} contradicts session mapping")
            side = mapping[token]
            instrument = pair[side]
            exchange_ms = event.get("exchange_timestamp_ms")
            if exchange_ms is None:
                raise CaptureError("Missing exchange timestamp; source freshness is unknown")
            event_ns = _timestamp(exchange_ms, "ms")
            if event_ns > arrival:
                raise CaptureError("Exchange timestamp is ahead of arrival; clock contract invalid")
            if (
                token in previous_exchange
                and event_ns < previous_exchange[token]
                and len(diagnostics["exchange_timestamp_inversions"]) < MAX_SAMPLE
            ):
                diagnostics["exchange_timestamp_inversions"].append(
                    {"frame_id": frame["id"], "event_index": event["event_index"]}
                )
            previous_exchange[token] = event_ns
            kind = event["kind"]
            if kind == "trade":
                _require(
                    schema,
                    "book_trades",
                    ["frame_id", "event_index", "asset_id", "side", "price", "size"],
                )
                trades = list(
                    _bounded_rows(
                        connection,
                        "SELECT "
                        + _projection(
                            schema,
                            "book_trades",
                            ["asset_id", "side", "price", "size"],
                            ["transaction_hash", "exchange_timestamp_ms", "flags"],
                        )
                        + " FROM book_trades WHERE frame_id=? AND event_index=?",
                        (frame["id"], event["event_index"]),
                        1,
                        "trade event",
                    )
                )
                if len(trades) != 1:
                    raise CaptureError("Trade event must have exactly one normalized trade")
                trade = trades[0]
                _flags(trade)
                if trade.get("exchange_timestamp_ms") not in (None, exchange_ms):
                    raise CaptureError("Trade timestamp contradicts atomic event")
                if str(trade["asset_id"]) != token or trade["side"] not in {"BUY", "SELL"}:
                    raise CaptureError("Trade token/side contradicts event")
                if contract.get("trade_side") != "aggressor":
                    raise CaptureError(
                        "Trades require evidence-backed contract.trade_side=aggressor"
                    )
                price = _number(trade["price"], contract["price_precision"], price=True)
                size = _number(trade["size"], contract["size_precision"])
                if size == 0:
                    raise CaptureError("Zero-size trade")
                if valid[side] and not inside_gap:
                    records[side].append(
                        TradeTick(
                            instrument.id,
                            instrument.make_price(price),
                            instrument.make_qty(size),
                            AggressorSide.BUYER if trade["side"] == "BUY" else AggressorSide.SELLER,
                            TradeId(
                                str(
                                    trade.get("transaction_hash")
                                    or f"{frame['id']}-{event['event_index']}"
                                )
                            ),
                            event_ns,
                            arrival,
                        )
                    )
                    record_order[side].append((sequence, event_index))
                else:
                    diagnostics["suppressed_events"] += 1
                continue
            if kind not in {"snapshot", "delta"}:
                raise CaptureError(f"Unsupported normalized event kind: {kind}")
            levels = list(
                _bounded_rows(
                    connection,
                    f"SELECT {level_projection} FROM book_levels WHERE frame_id=? AND event_index=? "
                    "ORDER BY level_index",
                    (frame["id"], event["event_index"]),
                    max(1, limit - total),
                    "book_levels",
                )
            )
            total += len(levels)
            if total > limit:
                raise CaptureError("Normalized levels exceed record_limit")
            deltas = []
            seen_levels = set()
            level_indices = set()
            if kind == "snapshot":
                deltas.append(
                    OrderBookDelta(
                        instrument.id,
                        BookAction.CLEAR,
                        None,
                        int(RecordFlag.F_SNAPSHOT),
                        sequence,
                        event_ns,
                        arrival,
                    )
                )
            for level in levels:
                _flags(level)
                level_index = level["level_index"]
                if (
                    not isinstance(level_index, int)
                    or level_index < 0
                    or level_index in level_indices
                ):
                    raise CaptureError("Invalid/duplicate atomic level_index")
                level_indices.add(level_index)
                if level.get("exchange_timestamp_ms") not in (None, exchange_ms):
                    raise CaptureError("Level timestamp contradicts atomic event")
                if (
                    level["kind"] != kind
                    or str(level["asset_id"]) != token
                    or level["side"] not in {"BUY", "SELL"}
                ):
                    raise CaptureError("Level kind/token/side contradicts event contract")
                price = _number(level["price"], contract["price_precision"], price=True)
                size = _number(level["size"], contract["size_precision"])
                key = (level["side"], price)
                if key in seen_levels:
                    raise CaptureError("Duplicate price/side in atomic event")
                seen_levels.add(key)
                if kind == "snapshot" and size == 0:
                    raise CaptureError("Zero-size snapshot level is not a valid resting order")
                action = BookAction.ADD if kind == "snapshot" else BookAction.UPDATE
                if size == 0:
                    action = BookAction.DELETE
                deltas.append(
                    OrderBookDelta(
                        instrument.id,
                        action,
                        BookOrder(
                            OrderSide.BUY if level["side"] == "BUY" else OrderSide.SELL,
                            instrument.make_price(price),
                            instrument.make_qty(size),
                            0,
                        ),
                        0,
                        sequence,
                        event_ns,
                        arrival,
                    )
                )
            if not deltas:
                raise CaptureError("Empty delta event has no reconstructible action")
            if inside_gap or (kind == "delta" and not valid[side]):
                diagnostics["suppressed_events"] += 1
                continue
            last = deltas[-1]
            deltas[-1] = OrderBookDelta(
                last.instrument_id,
                last.action,
                last.order,
                last.flags | int(RecordFlag.F_LAST),
                last.sequence,
                last.ts_event,
                last.ts_init,
            )
            records[side].append(OrderBookDeltas(instrument.id, deltas))
            record_order[side].append((sequence, event_index))
            if kind == "snapshot":
                valid[side] = True
                if arrival <= market_start:
                    diagnostics["opening_snapshot_ns"][side] = arrival
        if not event_count and frame.get("event_type") not in contract.get(
            "ignored_event_types", []
        ):
            raise CaptureError(
                "Frame without normalized events requires explicit ignored_event_types"
            )
    diagnostics["normalized_rows_read"] = total
    return records, diagnostics, record_order


def _load_capture_markets(config):
    from prediction_market_extensions.backtesting.longshot.sources import (
        record_digest,
        select_markets,
    )

    data = config["data"]
    contract = _contract(data)
    path = _source(data)
    session_limit = _limit(data.get("session_limit", 100), "session_limit", 10_000)
    record_limit = _limit(data.get("record_limit", 100_000), "record_limit")
    session_ids = data.get("session_ids")
    if session_ids is not None and (
        not isinstance(session_ids, list)
        or not session_ids
        or len(session_ids) > session_limit
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in session_ids
        )
        or len(set(session_ids)) != len(session_ids)
    ):
        raise CaptureError(
            "session_ids must be a nonempty unique integer list within session_limit"
        )
    inventory = {str(market["market_id"]): dict(market) for market in config.get("markets", [])}
    if len(inventory) != len(config.get("markets", [])):
        raise CaptureError("Duplicate supplied market inventory")
    resolutions = data.get("resolutions", {})
    markets, records, instruments = [], {}, {}
    remaining_records = record_limit
    with _reader(path) as connection:
        schema = _schema(connection)
        _require(
            schema,
            "sessions",
            [
                "id",
                "condition_id",
                contract["start_column"],
                contract["end_column"],
                "up_token_id",
                "down_token_id",
                "asset",
                "flags",
            ],
        )
        session_projection = _projection(
            schema,
            "sessions",
            [
                "id",
                "condition_id",
                contract["start_column"],
                contract["end_column"],
                "up_token_id",
                "down_token_id",
                "asset",
            ],
            ["flags"],
        )
        if session_ids is not None:
            if schema["sessions"]["primary_key"] != ["id"]:
                raise CaptureError("Explicit session_ids require sessions.id as the primary key")
            sessions = []
            for session_id in sorted(session_ids):
                rows = list(
                    _bounded_rows(
                        connection,
                        f"SELECT {session_projection} FROM sessions WHERE id=?",
                        (session_id,),
                        1,
                        "session primary-key lookup",
                    )
                )
                if len(rows) != 1:
                    raise CaptureError(
                        "Requested capture sessions missing; no implicit substitution"
                    )
                sessions.extend(rows)
        else:
            sessions = list(
                _bounded_rows(
                    connection,
                    f"SELECT {session_projection} FROM sessions ORDER BY id",
                    (),
                    session_limit,
                    "sessions",
                )
            )
        if not sessions or (session_ids is not None and len(sessions) != len(session_ids)):
            raise CaptureError(
                "Requested capture sessions missing; no implicit session substitution"
            )
        seen = set()
        for session in sessions:
            _flags(session)
            condition = str(session["condition_id"] or "")
            if not condition or condition in seen:
                raise CaptureError(
                    "Missing/duplicate condition session; merging sessions unsupported"
                )
            seen.add(condition)
            start = _timestamp(session[contract["start_column"]], contract["timestamp_unit"])
            close = _timestamp(session[contract["end_column"]], contract["timestamp_unit"])
            if close <= start:
                raise CaptureError("Market close must follow exact session market start")
            tokens = {
                side: str(session[f"{side.lower()}_token_id"] or "") for side in ("UP", "DOWN")
            }
            if not all(tokens.values()) or tokens["UP"] == tokens["DOWN"]:
                raise CaptureError("Session must supply distinct UP/DOWN token identities")
            market = inventory.get(condition, {})
            for key, value in {"start_ns": start, "close_ns": close}.items():
                if key in market and market[key] != value:
                    raise CaptureError(f"Supplied {key} contradicts sessions")
            if market.get("tokens") and any(
                str(market["tokens"].get(side, {}).get("token_id")) != token
                for side, token in tokens.items()
            ):
                raise CaptureError("Supplied token mapping contradicts sessions")
            market.update(
                market_id=condition,
                market_slug=market.get("market_slug", condition),
                asset=session["asset"],
                start_ns=start,
                close_ns=close,
                duration_seconds=(close - start) / 1_000_000_000,
                source="sqlite_capture",
                outcomes=tokens,
                session_id=session["id"],
                underlying_available=False,
                underlying_source=None,
            )
            resolution = resolutions.get(condition)
            if resolution is None and (
                market.get("settlement_winner") is not None
                or market.get("settlement_ns") is not None
            ):
                raise CaptureError(
                    "Inventory settlement labels require a verified separate resolution"
                )
            if resolution is not None:
                if (
                    not isinstance(resolution, Mapping)
                    or resolution.get("verified") is not True
                    or not isinstance(resolution.get("evidence"), str)
                    or not resolution["evidence"].strip()
                    or resolution.get("winner") not in {"UP", "DOWN"}
                    or isinstance(resolution.get("settlement_ns"), bool)
                    or not isinstance(resolution.get("settlement_ns"), int)
                    or resolution["settlement_ns"] < close
                ):
                    raise CaptureError(
                        "Resolution requires verified:true, evidence, winner and post-close settlement_ns"
                    )
                if (
                    market.get("settlement_winner") is not None
                    and market["settlement_winner"] != resolution["winner"]
                ):
                    raise CaptureError("External resolution contradicts market inventory")
                if market.get("settlement_ns") not in (None, resolution["settlement_ns"]):
                    raise CaptureError("External resolution timestamp contradicts market inventory")
                market["settlement_winner"] = resolution["winner"]
                market["settlement_ns"] = resolution["settlement_ns"]
            selected, excluded = select_markets([market], config.get("filters", {}))
            if excluded:
                markets.extend(excluded)
                continue
            market = selected[0]
            pair = {
                side: _instrument(condition, token, side, start, close, contract)
                for side, token in tokens.items()
            }
            session_records, diagnostics, record_order = _session_records(
                connection, schema, session, pair, contract, remaining_records
            )
            remaining_records -= max(
                diagnostics["normalized_rows_read"],
                sum(len(side_records) for side_records in session_records.values()),
            )
            if remaining_records < 0:
                raise CaptureError("Total replay records exceed record_limit")
            market["capture_diagnostics"] = diagnostics
            market["capture_record_order"] = record_order
            covering_evidence = any(
                left <= start and right >= close
                for left, right, _ in diagnostics["complete_intervals"]
            )
            invalid_coverage = any(
                left < close and right >= start
                for left, right, _ in diagnostics["invalid_intervals"]
            )
            diagnostics["coverage_verified"] = (
                covering_evidence
                and not invalid_coverage
                and not diagnostics["reset_count"]
                and all(value is not None for value in diagnostics["opening_snapshot_ns"].values())
            )
            market["source_coverage"] = {
                side: {
                    "record_count": len(side_records),
                    "book_event_count": sum(
                        type(row).__name__ == "OrderBookDeltas" for row in side_records
                    ),
                    "sha256": record_digest(side_records),
                    "coverage_verified": diagnostics["coverage_verified"],
                    "requested_window": {"start_ns": start, "end_ns": close},
                    "loaded_window": (
                        {"start_ns": side_records[0].ts_init, "end_ns": side_records[-1].ts_init}
                        if side_records
                        else None
                    ),
                }
                for side, side_records in session_records.items()
            }
            errors = []
            if not covering_evidence:
                errors.append("Missing verified complete coverage spanning exact market window")
            if invalid_coverage or diagnostics["reset_count"]:
                errors.append("Capture reset/invalid coverage: market permanently quarantined")
            if not all(session_records.values()):
                errors.append("Both outcomes require observable snapshot-anchored records")
            if any(value is None for value in diagnostics["opening_snapshot_ns"].values()):
                errors.append("Both outcomes require snapshot anchors at/before exact market open")
            if errors:
                market.update(
                    load_error="; ".join(errors), eligible=False, complete=False, censored=True
                )
            markets.extend(selected)
            records[condition] = session_records
            instruments[condition] = pair
    return markets, records, instruments


async def load_capture_markets(
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, list[Any]]], dict[str, dict[str, Any]]]:
    """Return the same inventory/records/instruments tuple as load_vendor_markets.

    Each DB reader lives in its worker thread, so bounded SQLite work cannot block
    an API handler's event loop. Labels are returned only in the inventory.
    """
    return await asyncio.to_thread(_load_capture_markets, config)


async def load_capture_replay(config: Mapping[str, Any], replay, *, request=None):
    """Expose a normalized capture as the repository's existing LoadedReplay."""
    from prediction_market_extensions.adapters.prediction_market.replay import (
        LoadedReplay,
        ReplayCoverageStats,
        ReplayWindow,
    )
    from prediction_market_extensions.backtesting._replay_specs import BookReplay

    if not isinstance(replay, BookReplay) or replay.outcome not in {"UP", "DOWN"}:
        raise CaptureError("Capture replay requires BookReplay with explicit UP/DOWN outcome")
    if replay.lookback_hours is not None:
        raise CaptureError("Capture lookback/subwindow reconstruction is unsupported")
    markets, records, instruments = await load_capture_markets(config)
    matches = [market for market in markets if market["market_slug"] == replay.market_slug]
    if len(matches) != 1 or matches[0].get("load_error"):
        raise CaptureError("Replay market missing, ambiguous or lacks paired snapshot coverage")
    market = matches[0]
    side = replay.outcome
    side_records = tuple(records[market["market_id"]][side])
    if replay.start_time is not None or replay.end_time is not None:
        from prediction_market_extensions.backtesting.longshot.sources import timestamp_ns

        if (
            replay.start_time is not None
            and timestamp_ns(str(replay.start_time)) != market["start_ns"]
        ) or (
            replay.end_time is not None and timestamp_ns(str(replay.end_time)) != market["close_ns"]
        ):
            raise CaptureError("Capture replay subwindow reconstruction is unsupported")
    count = market["source_coverage"][side]["book_event_count"]
    if request is not None and (request.min_record_count > count or request.min_price_range > 0):
        raise CaptureError("Capture cannot satisfy requested minimum coverage")
    winner = market.get("settlement_winner")
    return LoadedReplay(
        replay=replay,
        instrument=instruments[market["market_id"]][side],
        records=side_records,
        outcome=side,
        realized_outcome=None if winner is None else float(winner == side),
        metadata={"capture_diagnostics": market["capture_diagnostics"]},
        requested_window=ReplayWindow(market["start_ns"], market["close_ns"]),
        loaded_window=ReplayWindow(side_records[0].ts_init, side_records[-1].ts_init),
        coverage_stats=ReplayCoverageStats(
            count=count,
            count_key="book_event_count",
            market_key="condition_id",
            market_id=market["market_id"],
        ),
        instrument_ids=(instruments[market["market_id"]][side].id,),
    )
