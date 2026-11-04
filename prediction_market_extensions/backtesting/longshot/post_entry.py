"""Bounded offline observed-start quote diagnostic, separate from numbered touches."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from io import StringIO
from pathlib import Path

from .quote_cli import BoundedOutputs, read_config
from .top_of_book import (
    POLICY,
    SIDES,
    WARNING,
    Study,
    _mapping,
    allowed_path,
    census,
    digest,
    midpoint,
    observations,
    quote_values,
)

INTERPRETATION = (
    "Observed-start estimand: first qualifying observed segment, not first economic market touch. "
    "No preopen anchor required; initial above-threshold states are not crossings. "
    "Manifest agreement is internal consistency, not independent identity evidence. "
    "Unknown follow-up is not failure; no probability-versus-ask edge or profitability estimate."
)
STATUSES = (
    "success",
    "completed_non_success",
    "unknown",
    "no_entry",
    "ambiguous",
    "missing",
    "invalid",
)
FIELDS = (
    "start",
    "close",
    "slug",
    "status",
    "reason",
    "identity",
    "entry_side",
    "entry_us",
    "opposite_ask",
    "opposite_ask_size",
    "opposite_age_us",
    "success_us",
    "break_us",
    "anomaly_counts",
)


def diagnose(window: dict, streams: dict[str, list[dict]], study: Study) -> dict:
    """Evaluate one entry only; never restart interrupted post-entry follow-up."""
    result = dict.fromkeys(FIELDS)
    result.update(
        start=window["start"],
        close=window["close"],
        slug=window["slug"],
        identity="unverified",
        status="no_entry",
        reason="no_qualifying_observed_crossing",
    )
    counts = Counter()
    events: dict[int, dict] = {}
    start, close = window["start"] * 1_000_000, window["close"] * 1_000_000
    for side in SIDES:
        for row in streams.get(side, []):
            if start <= row["timestamp_us"] < close:
                events.setdefault(row["timestamp_us"], {})[side] = row
    latest, baseline = {}, None
    threshold = Decimal(study.threshold)
    for ts, updates in sorted(events.items()):
        gap = any(
            side in latest and ts - latest[side]["timestamp_us"] > study.gap_us for side in SIDES
        )
        latest.update(updates)
        values = {side: quote_values(row) for side, row in latest.items()}
        invalid = any(value is None for value in values.values())
        stale = any(
            ts - row["timestamp_us"] > min(study.pair_age_us, study.gap_us)
            for row in latest.values()
        )
        paired = len(values) == 2 and not invalid and not stale
        counts["invalid_paired_states"] += int(invalid)
        counts["excessive_gap_states"] += int(gap)
        counts["stale_paired_states"] += int(stale)
        broken = not paired or gap
        if broken:
            baseline = None
            if result["status"] == "unknown" and result["break_us"] is None:
                result.update(reason="invalid_gap_or_stale_pair", break_us=ts)
            elif result["status"] == "no_entry":
                counts["pre_entry_baseline_resets"] += 1
        if not paired:
            continue
        mids = {side: midpoint(values[side]) for side in SIDES}
        crossings = (
            [side for side in SIDES if baseline[side] < threshold <= mids[side]]
            if baseline is not None
            else []
        )
        baseline = mids
        if len(crossings) == 2:
            counts["simultaneous_opposing_crossings"] += 1
            if result["entry_us"] is None and result["status"] == "no_entry":
                result.update(
                    status="ambiguous", reason="simultaneous_opposing_crossings", break_us=ts
                )
            elif result["status"] == "unknown" and result["break_us"] is None:
                result.update(reason="simultaneous_opposing_crossings", break_us=ts)
            continue
        if result["status"] == "no_entry" and crossings:
            side = crossings[0]
            opposite = "DOWN" if side == "UP" else "UP"
            row = latest[opposite]
            result.update(
                status="unknown",
                reason="missing_valid_terminal_coverage",
                entry_side=side,
                entry_us=ts,
                opposite_ask=row["ask_price"],
                opposite_ask_size=row["ask_size"],
                opposite_age_us=ts - row["timestamp_us"],
            )
        elif (
            result["status"] == "unknown"
            and result["break_us"] is None
            and crossings
            and crossings[0] != result["entry_side"]
        ):
            result.update(status="success", reason="opposite_midpoint_upcrossing", success_us=ts)
    terminal_ok = all(
        side in latest
        and quote_values(latest[side]) is not None
        and close - latest[side]["timestamp_us"]
        <= min(study.terminal_age_us, study.gap_us, study.pair_age_us)
        for side in SIDES
    )
    if result["status"] == "unknown" and result["break_us"] is None and terminal_ok:
        result.update(status="completed_non_success", reason="uninterrupted_terminal_coverage")
    counts["missing_valid_terminal_coverage"] += int(not terminal_ok)
    result["anomaly_counts"] = dict(counts)
    return result


def windows(study: Study):
    manifests = _mapping(study.manifest, study)
    verified = _mapping(study.verified_identities, study, verified=True)
    for window in census(study):
        streams, hashes, counts = {}, {}, Counter()
        ts = window["start"]
        mapping = verified.get(ts) or manifests.get(ts)
        if (
            ts in manifests
            and ts in verified
            and any(manifests[ts][key] != verified[ts][key] for key in ("slug", "up_id", "down_id"))
        ):
            window["exclusions"].append("manifest_verification_conflict")
        identities = {}
        for side, source in window["files"].items():
            path = Path(source["path"])
            try:
                hashes[str(path)] = digest(path)
                streams[side], report = observations(path, side, window, study, mapping)
                if hashes[str(path)] != report["sha256"]:
                    raise ValueError("Source changed during diagnostic")
                identities[side] = report["identity"]
                counts.update(
                    {
                        key: value
                        for key, value in report.items()
                        if type(value) is int and key.endswith(("_rows", "_inversions"))
                    }
                )
            except (OSError, ValueError) as exc:
                window["exclusions"].append(f"{side}:{exc}")
                hashes.setdefault(str(path), "unavailable")
        if len(identities) == 2:
            up, down = identities["UP"], identities["DOWN"]
            if up is None or down is None or up[1] != down[1] or up[3] == down[3]:
                window["exclusions"].append("cross_outcome_market_or_token_conflict")
        if window["exclusions"]:
            result = dict.fromkeys(FIELDS)
            result.update(
                start=window["start"],
                close=window["close"],
                slug=window["slug"],
                status="missing" if len(window["files"]) < 2 else "invalid",
                reason=";".join(window["exclusions"]),
                identity="unverified",
                anomaly_counts={},
            )
        else:
            result = diagnose(window, streams, study)
            if ts in verified:
                result["identity"] = "independent_evidence_supplied"
        counts.update(result["anomaly_counts"])
        result["anomaly_counts"] = dict(counts)
        yield result, hashes


def run(config_path: Path, output: Path) -> None:
    study, config = read_config(config_path)
    config_hash = digest(config_path)
    if (
        study.gap_us > 1_000_000
        or study.pair_age_us > 1_000_000
        or study.anchor_age_us > 1_000_000
        or study.terminal_age_us > 1_000_000
        or Decimal(study.threshold) != Decimal("0.70")
    ):
        raise ValueError("Diagnostic requires threshold 0.70 and tolerances at most one second")
    output = output.resolve()
    if any(output.is_relative_to(root.resolve()) for root in study.allowed_roots):
        raise ValueError("Reports must be outside all read-only allowed roots")
    inputs = [study.index] + [
        path for path in (study.manifest, study.verified_identities) if path is not None
    ]
    input_hashes = {str(config_path): config_hash}
    for path in inputs:
        try:
            if path != study.index:
                _mapping(path, study, verified=path == study.verified_identities)
            input_hashes[str(path)] = digest(allowed_path(path, study))
        except FileNotFoundError:
            if path != study.index:
                raise
            input_hashes[str(path)] = "unavailable"
    output.mkdir(parents=True, exist_ok=False)
    limits = BoundedOutputs(config.get("max_output_bytes", 16 * 1024**2))
    statuses, anomalies, sources = Counter(), Counter(), {}
    with (output / "windows.csv").open("w", newline="") as stream:
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=FIELDS)
        writer.writeheader()
        limits.text(stream, buffer.getvalue())
        for row, hashes in windows(study):
            statuses[row["status"]] += 1
            anomalies.update(row["anomaly_counts"])
            sources[row["slug"]] = hashes
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(
                {**row, "anomaly_counts": json.dumps(row["anomaly_counts"], sort_keys=True)}
            )
            limits.text(stream, buffer.getvalue())
    limits.json(
        output / "summary.json",
        {
            "scheduled_windows": sum(statuses.values()),
            "statuses": {key: statuses[key] for key in STATUSES},
            "anomaly_counts": dict(anomalies),
            "interpretation": INTERPRETATION,
            "warning": WARNING,
            "execution_replay_supported": False,
        },
    )
    for path, expected in input_hashes.items():
        if (digest(Path(path)) if Path(path).exists() else "unavailable") != expected:
            raise ValueError("Input changed during diagnostic; output is incomplete")
    pending = output / "manifest.pending.json"
    limits.json(
        pending,
        {
            "status": "completed",
            "diagnostic": "observed_start_post_entry",
            "configuration": study.configuration(),
            "input_sha256": input_hashes,
            "source_sha256": sources,
            "output_sha256": {
                name: digest(output / name) for name in ("windows.csv", "summary.json")
            },
            "implementation_sha256": digest(Path(__file__)),
            "policy": POLICY,
            "interpretation": INTERPRETATION,
            "warning": WARNING,
        },
    )
    pending.replace(output / "manifest.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, epilog=INTERPRETATION)
    parser.add_argument("--config", type=Path, required=True, help="Existing explicit study JSON")
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory outside input roots"
    )
    args = parser.parse_args()
    run(args.config, args.output)


if __name__ == "__main__":
    main()
