"""Bounded, read-only exchange-time quote diagnostics; never execution replay."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

SIDES = ("UP", "DOWN")
POLICY = "experimental terminal whole row in original file order per exchange timestamp"
WARNING = (
    "Exchange-time descriptive convention, not atomic venue events or executable arrival order. "
    "Gap/freshness tolerances are assumptions, not evidence of collector outages. "
    "No complement inference, fills, depth, PnL, settlement, fees or live-trading claims."
)
COLUMNS = (
    "timestamp_us",
    "local_timestamp_us",
    "exchange",
    "market_id",
    "slug",
    "asset_id",
    "outcome",
    "bid_price",
    "bid_size",
    "ask_price",
    "ask_size",
)


def utc_seconds(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Explicit UTC timestamps required")
    if parsed.microsecond:
        raise ValueError("Schedule boundaries must be integer seconds")
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86400 + delta.seconds


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@dataclass(frozen=True)
class Study:
    data_root: Path
    allowed_roots: tuple[Path, ...]
    index: Path
    start: int
    end: int
    symbol: str = "BTC"
    timeframe: str = "5m"
    source: str | None = None
    exchange: str = "POLYMARKET"
    max_windows: int = 10000
    max_file_bytes: int = 512 * 1024 * 1024
    max_rows: int = 2_000_000
    max_timestamps: int = 500_000
    max_anomalies: int = 10000
    max_decoded_bytes: int = 512 * 1024 * 1024
    batch_size: int = 4096
    gap_us: int = 1_000_000
    pair_age_us: int = 1_000_000
    anchor_age_us: int = 1_000_000
    terminal_age_us: int = 1_000_000
    threshold: str = "0.70"
    manifest: Path | None = None
    verified_identities: Path | None = None

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int:
            raise ValueError("Schedule boundaries must be integer seconds")
        if not self.data_root.is_absolute() or not self.index.is_absolute():
            raise ValueError("Explicit absolute data_root and index required")
        if not self.allowed_roots or any(not root.is_absolute() for root in self.allowed_roots):
            raise ValueError("Explicit absolute allowed_roots required")
        if self.symbol not in {"BTC", "ETH", "SOL", "XRP"}:
            raise ValueError("Unsupported explicit asset schedule")
        if not re.fullmatch(r"[1-9][0-9]*m", self.timeframe):
            raise ValueError("timeframe must be an integer-minute duration")
        if self.start >= self.end or self.start % self.duration or self.end % self.duration:
            raise ValueError("UTC range must be increasing and aligned to scheduled windows")
        bounds = {
            "max_windows": 10000,
            "max_file_bytes": 2 * 1024**3,
            "max_rows": 10_000_000,
            "max_timestamps": 1_000_000,
            "batch_size": 65536,
            "max_anomalies": 10000,
            "max_decoded_bytes": 2 * 1024**3,
            "gap_us": 60_000_000,
            "pair_age_us": 60_000_000,
            "anchor_age_us": 60_000_000,
            "terminal_age_us": 60_000_000,
        }
        for name, upper in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= upper:
                raise ValueError(f"{name} must be an integer in [1, {upper}]")
        if (self.end - self.start) // self.duration > self.max_windows:
            raise ValueError("Scheduled census exceeds max_windows; split the explicit range")
        threshold = Decimal(self.threshold)
        if not threshold.is_finite() or not 0 < threshold < 1:
            raise ValueError("threshold must be a finite decimal probability")

    @property
    def duration(self) -> int:
        return int(self.timeframe[:-1]) * 60

    def configuration(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))


def allowed_path(path: Path, study: Study) -> Path:
    resolved = path.resolve(strict=True)
    roots = [root.resolve(strict=True) for root in study.allowed_roots]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("Resolved path escapes allowed roots")
    if not resolved.is_file():
        raise ValueError("Expected a regular file")
    return resolved


def index_file_path(relative: str, size: int, study: Study) -> Path:
    if not isinstance(relative, str):
        # Census handles invalid index entries as validation exclusions.
        raise ValueError("Index file_path must be text")  # noqa: TRY004
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Index file_path must be relative to DATA ROOT without traversal")
    resolved = allowed_path(study.data_root / path, study)
    if not resolved.is_relative_to(study.data_root.resolve(strict=True)):
        raise ValueError("Resolved file escapes data_root")
    if type(size) is not int or not 0 < size <= study.max_file_bytes:
        raise ValueError("Invalid or over-limit indexed file size")
    if resolved.stat().st_size != size:
        raise ValueError("File size differs from index")
    return resolved


def _mapping(path: Path | None, study: Study, *, verified: bool = False) -> dict[int, dict]:
    if path is None:
        return {}
    path = allowed_path(path, study)
    if path.stat().st_size > 8 * 1024**2:
        raise ValueError("Identity manifest exceeds 8 MiB")
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        # Preserve the decoded-file validation error contract.
        raise ValueError("Identity manifest must be a list")  # noqa: TRY004
    result = {}
    for entry in entries:
        ts = entry["ts"]
        if type(ts) is not int:
            raise ValueError("Manifest ts must be integer seconds")
        slug = f"{study.symbol.lower()}-updown-{study.timeframe}-{ts}"
        if (
            entry["slug"] != slug
            or ts % study.duration
            or not isinstance(entry["up_id"], str)
            or not entry["up_id"]
            or not isinstance(entry["down_id"], str)
            or not entry["down_id"]
            or entry["up_id"] == entry["down_id"]
        ):
            raise ValueError("Manifest identity conflict")
        if verified and (not entry.get("market_id") or not entry.get("evidence")):
            raise ValueError("Verified identities require market_id and independent evidence")
        if ts in result:
            raise ValueError("Duplicate manifest window")
        result[ts] = entry
    return result


def census(study: Study) -> Iterator[dict]:
    """Schedule, not downloaded inventory, defines the denominator."""
    connection = None
    index_error = None
    try:
        index = allowed_path(study.index, study)
        connection = sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
    except (OSError, sqlite3.Error) as exc:
        index_error = f"index_unavailable:{exc}"
    try:
        for start in range(study.start, study.end, study.duration):
            rows = []
            query_error = index_error
            if connection is not None and not index_error:
                calls = 0

                def bounded_query() -> int:
                    nonlocal calls
                    calls += 1
                    return int(calls > 20000)

                connection.set_progress_handler(bounded_query, 1000)
                try:
                    rows = connection.execute(
                        "SELECT window_start_ts,symbol,timeframe,side,data_source,file_path,"
                        "file_size,indexed_at FROM windows "
                        "WHERE window_start_ts=? AND symbol=? AND timeframe=? LIMIT 101",
                        (start, study.symbol, study.timeframe),
                    ).fetchall()
                except sqlite3.Error as exc:
                    query_error = f"index_query_unavailable:{exc}"
            slug = f"{study.symbol.lower()}-updown-{study.timeframe}-{start}"
            item = {
                "start": start,
                "close": start + study.duration,
                "slug": slug,
                "asset": study.symbol,
                "duration_seconds": study.duration,
                "execution_replay_supported": False,
                "files": {},
                "exclusions": [],
                "source_choices": [dict(row) for row in rows],
            }
            sources = {row["data_source"] for row in rows}
            if any(not isinstance(source, str) or not source for source in sources):
                item["exclusions"].append("invalid_index_source")
            if query_error:
                item["exclusions"].append(query_error)
            selected = (
                study.source
                if study.source is not None
                else (next(iter(sources)) if len(sources) == 1 else None)
            )
            item["selected_source"] = selected
            if len(rows) > 100:
                item["exclusions"].append("too_many_source_choices")
            if len(sources) > 1 and study.source is None:
                item["exclusions"].append("ambiguous_sources")
            if any(row["side"] not in SIDES for row in rows):
                item["exclusions"].append("invalid_index_side")
            for side in SIDES:
                candidates = [
                    row for row in rows if row["side"] == side and row["data_source"] == selected
                ]
                if len(candidates) != 1:
                    item["exclusions"].append(
                        f"{side}:missing" if not candidates else f"{side}:duplicate"
                    )
                    continue
                row = candidates[0]
                try:
                    path = index_file_path(row["file_path"], row["file_size"], study)
                    if path.name != f"{slug}_{side}.parquet":
                        raise ValueError("Filename/slug/window/outcome conflict")
                    item["files"][side] = {
                        "path": str(path),
                        "file_size": row["file_size"],
                        "data_source": selected,
                        "indexed_at": row["indexed_at"],
                    }
                except (OSError, ValueError) as exc:
                    item["exclusions"].append(f"{side}:{exc}")
            yield item
    finally:
        if connection is not None:
            connection.close()


def quote_values(row: dict) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    try:
        if any(
            not isinstance(row[key], str) or len(row[key]) > 256
            for key in ("bid_price", "ask_price", "bid_size", "ask_size")
        ):
            return None
        bid, ask, bid_size, ask_size = (
            Decimal(row[key]) for key in ("bid_price", "ask_price", "bid_size", "ask_size")
        )
        if (
            not all(value.is_finite() for value in (bid, ask, bid_size, ask_size))
            or any(abs(value.as_tuple().exponent) > 256 for value in (bid, ask, bid_size, ask_size))
            or not 0 <= bid < ask <= 1
            or bid_size <= 0
            or ask_size <= 0
        ):
            return None
        return bid, ask, bid_size, ask_size
    except (TypeError, InvalidOperation, ValueError):
        return None


def midpoint(values: tuple[Decimal, Decimal, Decimal, Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 600  # Exact for the bounded decimal encodings above.
        return (values[0] + values[1]) / 2


def observations(
    path: Path, side: str, window: dict, study: Study, mapping: dict | None = None
) -> tuple[list[dict], dict]:
    from pyarrow import int64, parquet, string

    diagnostics = Counter()
    anomalies = []
    groups: dict[int, dict] = {}
    identity = None
    previous_exchange = previous_receipt = None
    previous_raw_mid = None
    threshold = Decimal(study.threshold)
    lower = window["start"] * 1_000_000 - study.anchor_age_us
    upper = window["close"] * 1_000_000
    before = path.stat()
    with parquet.ParquetFile(path) as file:
        expected = {
            name: int64() if name.endswith("timestamp_us") else string() for name in COLUMNS
        }
        schema = file.schema_arrow
        if len(schema) != len(COLUMNS) or any(
            name not in schema.names or schema.field(name).type != dtype
            for name, dtype in expected.items()
        ):
            raise ValueError("Unexpected top-of-book Parquet schema")
        if file.metadata.num_rows > study.max_rows:
            raise ValueError("Parquet row limit exceeded")
        if (
            sum(
                file.metadata.row_group(i).total_byte_size
                for i in range(file.metadata.num_row_groups)
            )
            > study.max_decoded_bytes
        ):
            raise ValueError("Parquet decoded byte limit exceeded")
        index = 0
        for batch in file.iter_batches(
            batch_size=study.batch_size, columns=list(COLUMNS), use_threads=False
        ):
            for row in batch.to_pylist():
                ts, receipt = row["timestamp_us"], row["local_timestamp_us"]
                if type(ts) is not int or type(receipt) is not int:
                    raise ValueError("Null/noninteger exchange or receipt timestamp")
                current = tuple(
                    row[name] for name in ("exchange", "market_id", "slug", "asset_id", "outcome")
                )
                if any(not isinstance(value, str) or not value for value in current):
                    raise ValueError("Missing source identity")
                if any(len(value) > 512 for value in current):
                    raise ValueError("Over-limit source identity")
                if (
                    row["exchange"] != study.exchange
                    or row["slug"] != window["slug"]
                    or row["outcome"].upper() != side
                ):
                    raise ValueError("Source exchange/slug/window/outcome identity mismatch")
                if identity is not None and identity != current:
                    raise ValueError("Conflicting identities within token/day file")
                identity = current
                if mapping and (
                    row["asset_id"] != mapping[f"{side.lower()}_id"]
                    or (mapping.get("market_id") and row["market_id"] != mapping["market_id"])
                ):
                    raise ValueError("Source token/market identity mismatch")
                diagnostics["file_rows"] += 1
                if diagnostics["file_rows"] > study.max_rows:
                    raise ValueError("Decoded Parquet row limit exceeded")
                diagnostics["exchange_inversions"] += (
                    previous_exchange is not None and ts < previous_exchange
                )
                diagnostics["receipt_inversions"] += (
                    previous_receipt is not None and receipt < previous_receipt
                )
                previous_exchange, previous_receipt = ts, receipt
                values = quote_values(row)
                if lower <= ts < upper and values is None:
                    anomalies.append(
                        {**row, "source_row_index": index, "source_path": str(path), "side": side}
                    )
                    if len(anomalies) > study.max_anomalies:
                        raise ValueError("Raw anomaly limit exceeded")
                if window["start"] * 1_000_000 <= ts < upper:
                    diagnostics["in_window_rows"] += 1
                    diagnostics["raw_invalid_rows"] += values is None
                    try:
                        bid, ask = Decimal(row["bid_price"]), Decimal(row["ask_price"])
                        if bid.is_finite() and ask.is_finite():
                            diagnostics["raw_locked_rows"] += bid == ask
                            diagnostics["raw_crossed_rows"] += bid > ask
                    except (TypeError, InvalidOperation):
                        pass
                    mid = None if values is None else midpoint(values)
                    diagnostics["raw_file_order_crossings"] += (
                        mid is not None
                        and previous_raw_mid is not None
                        and previous_raw_mid < threshold <= mid
                    )
                    previous_raw_mid = mid
                if lower <= ts < upper:
                    existing = groups.get(ts)
                    row.update(
                        source_row_index=index,
                        source_path=str(path),
                        side=side,
                        group_first_row_index=index
                        if existing is None
                        else existing["group_first_row_index"],
                        group_rows=1 if existing is None else existing["group_rows"] + 1,
                        group_receipt_min=receipt
                        if existing is None
                        else min(receipt, existing["group_receipt_min"]),
                        group_receipt_max=receipt
                        if existing is None
                        else max(receipt, existing["group_receipt_max"]),
                        group_invalid_rows=int(values is None)
                        if existing is None
                        else existing["group_invalid_rows"] + int(values is None),
                        valid=values is not None,
                    )
                    row["group_receipt_span_us"] = (
                        row["group_receipt_max"] - row["group_receipt_min"]
                    )
                    groups[ts] = row
                    if len(groups) > study.max_timestamps:
                        raise ValueError("Retained timestamp limit exceeded")
                index += 1
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during read")
    ordered = [groups[ts] for ts in sorted(groups)]
    in_window = [row for row in ordered if row["timestamp_us"] >= window["start"] * 1_000_000]
    diagnostics["terminal_timestamps"] = len(in_window)
    diagnostics["invalid_terminal_rows"] = sum(not row["valid"] for row in in_window)
    diagnostics["coalesced_rows"] = diagnostics["in_window_rows"] - len(in_window)
    diagnostics["resolved_invalid_groups"] = sum(
        row["valid"] and row["group_invalid_rows"] > 0 for row in in_window
    )
    gaps = [b["timestamp_us"] - a["timestamp_us"] for a, b in itertools.pairwise(ordered)]
    report = dict(diagnostics)
    report.update(
        identity=identity,
        sha256=digest(path),
        max_exchange_gap_us=max(gaps, default=None),
        first_in_window_us=in_window[0]["timestamp_us"] if in_window else None,
        last_in_window_us=in_window[-1]["timestamp_us"] if in_window else None,
        policy=POLICY,
        max_within_group_exchange_span_us=0,
        max_within_group_receipt_span_us=max(
            (row["group_receipt_span_us"] for row in ordered),
            default=None,
        ),
        raw_anomalies=anomalies,
    )
    final_stat = path.stat()
    if (before.st_size, before.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
        raise ValueError("Source changed during hashing")
    return ordered, report


def extract(
    window: dict, streams: dict[str, list[dict]], study: Study, *, independently_verified: bool
) -> dict:
    """Never restart a prefix interrupted by invalidity, gaps or ambiguous ordering."""
    start, close = window["start"] * 1_000_000, window["close"] * 1_000_000
    threshold = Decimal(study.threshold)
    latest = {}
    reasons = list(window["exclusions"])
    for side in SIDES:
        anchors = [row for row in streams.get(side, []) if row["timestamp_us"] < start]
        if anchors:
            latest[side] = anchors[-1]
        anchor = latest.get(side)
        if (
            not independently_verified
            or anchor is None
            or not anchor["valid"]
            or start - anchor["timestamp_us"] > study.anchor_age_us
        ):
            reasons.append(f"{side}:unverified_or_missing_backward_open_anchor")
        elif (values := quote_values(anchor)) and midpoint(values) >= threshold:
            reasons.append(f"{side}:left_truncated_above_threshold_open")
    prefix = not reasons
    initially_eligible = prefix
    touches, diagnostic_crossings, bid_targets, breaks = [], [], [], []
    events: dict[int, dict[str, dict]] = {}
    for side, rows in streams.items():
        for row in rows:
            if start <= row["timestamp_us"] < close:
                events.setdefault(row["timestamp_us"], {})[side] = row
    previous = dict(latest)
    previous_mid = {
        side: midpoint(values) if (values := quote_values(row)) else None
        for side, row in latest.items()
    }
    coalesced_crossings = Counter()
    for ts, updates in sorted(events.items()):
        crossing_sides = []
        target_sides = []
        for side, row in updates.items():
            old = previous.get(side)
            if not row["valid"] or (old is not None and ts - old["timestamp_us"] > study.gap_us):
                prefix = False
                breaks.append({"timestamp_us": ts, "side": side, "reason": "invalid_or_gap"})
            values = quote_values(row)
            old_values = None if old is None else quote_values(old)
            if (
                values
                and values[0] >= threshold
                and (old_values is None or old_values[0] < threshold)
            ):
                target_sides.append(side)
            mid = None if values is None else midpoint(values)
            prior_mid = previous_mid.get(side)
            if mid is not None and prior_mid is not None and prior_mid < threshold <= mid:
                crossing_sides.append(side)
                coalesced_crossings[side] += 1
            previous_mid[side] = mid
            previous[side] = row
        latest.update(updates)  # Both outcomes applied before any signal evaluation.
        paired = all(
            side in latest
            and latest[side]["valid"]
            and 0 <= ts - latest[side]["timestamp_us"] <= study.pair_age_us
            for side in SIDES
        )
        # Pairing tolerance must not outlive either outcome's coverage-gap limit.
        within_gap = all(
            side in latest
            and latest[side]["valid"]
            and 0 <= ts - latest[side]["timestamp_us"] <= study.gap_us
            for side in SIDES
        )
        if not paired:
            prefix = False
            breaks.append({"timestamp_us": ts, "reason": "stale_or_invalid_pair"})
        if not within_gap:
            if prefix:
                breaks.append({"timestamp_us": ts, "reason": "coverage_gap"})
            prefix = False
        ambiguous = len(crossing_sides) > 1
        if ambiguous:
            prefix = False
            breaks.append({"timestamp_us": ts, "reason": "simultaneous_opposing_crossings"})
        for side in target_sides:
            row = updates[side]
            bid_targets.append(
                {
                    "timestamp_us": ts,
                    "side": side,
                    "bid_price": row["bid_price"],
                    "row": row,
                    "quote_diagnostic_not_fill": True,
                    "source_row_index": row["source_row_index"],
                }
            )
        for side in crossing_sides:
            row = updates[side]
            opposite = latest.get("DOWN" if side == "UP" else "UP")
            age = None if opposite is None else ts - opposite["timestamp_us"]
            diagnostic = {
                "timestamp_us": ts,
                "offset_us": ts - start,
                "remaining_us": close - ts,
                "side": side,
                "ambiguous": ambiguous,
                "prefix_intact": prefix,
                "row": row,
                "opposite_row": opposite,
                "opposite_age_us": age,
                "cheap_side_quote_usable": bool(
                    opposite and opposite["valid"] and age <= study.pair_age_us
                ),
            }
            diagnostic_crossings.append(diagnostic)
            if prefix and (not touches or touches[-1]["side"] != side):
                touches.append(
                    {
                        **diagnostic,
                        "touch_number": len(touches) + 1,
                        "transition_us": None if not touches else ts - touches[-1]["timestamp_us"],
                    }
                )
    terminal_ok = all(
        side in latest
        and latest[side]["valid"]
        and 0 <= close - latest[side]["timestamp_us"] <= study.terminal_age_us
        for side in SIDES
    )
    if not terminal_ok:
        reasons.append("missing_terminal_coverage")
    if breaks:
        reasons.append("interrupted_history")
    complete = prefix and terminal_ok
    transitions = {}
    for stage in (1, 2):
        at_risk = len(touches) >= stage
        success = len(touches) > stage
        transitions[f"p{stage}{stage + 1}"] = {
            "at_risk": at_risk,
            "success": True if success else False if at_risk and complete else None,
        }
    return {
        **window,
        "eligible_open": initially_eligible,
        "complete": complete,
        "censored": not complete,
        "exclusions": sorted(set(reasons)),
        "touches": touches,
        "diagnostic_crossings": diagnostic_crossings,
        "bid_targets": bid_targets,
        "coverage_breaks": breaks,
        "transitions": transitions,
        "coalesced_crossings": dict(coalesced_crossings),
        "independently_verified": independently_verified,
        "warning": WARNING,
    }


def study_windows(study: Study) -> Iterator[dict]:
    manifests = _mapping(study.manifest, study)
    verified = _mapping(study.verified_identities, study, verified=True)
    for window in census(study):
        streams, diagnostics = {}, {}
        mapping = verified.get(window["start"]) or manifests.get(window["start"])
        if (
            window["start"] in manifests
            and window["start"] in verified
            and any(
                manifests[window["start"]][key] != verified[window["start"]][key]
                for key in ("slug", "up_id", "down_id")
            )
        ):
            window["exclusions"].append("manifest_verification_conflict")
        for side, source in window["files"].items():
            try:
                streams[side], diagnostics[side] = observations(
                    Path(source["path"]),
                    side,
                    window,
                    study,
                    mapping,
                )
            except (OSError, ValueError) as exc:
                window["exclusions"].append(f"{side}:{exc}")
                try:
                    source_hash = digest(Path(source["path"]))
                except OSError:
                    source_hash = "unavailable"
                diagnostics[side] = {
                    "read_error": str(exc),
                    "sha256": source_hash,
                    "raw_anomalies": [],
                }
        if set(streams) == set(SIDES):
            up, down = diagnostics["UP"]["identity"], diagnostics["DOWN"]["identity"]
            if up is None or down is None or up[1] != down[1] or up[3] == down[3]:
                window["exclusions"].append("cross_outcome_market_or_token_conflict")
        window["census_exclusions"] = list(window["exclusions"])
        result = extract(window, streams, study, independently_verified=window["start"] in verified)
        result["source_diagnostics"] = diagnostics
        result["observations"] = streams
        result["manifest_internal_consistency_is_not_independent_verification"] = True
        yield result
