"""Offline CLI for the separate top-of-book study (no replay command)."""

from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
from collections import Counter, defaultdict
from io import StringIO
from pathlib import Path

from .analysis import _estimate, _partition
from .top_of_book import POLICY, WARNING, Study, digest, study_windows, utc_seconds

MAX_REPORT_BYTES = 256 * 1024**2
MAX_REPORT_ROWS = 10000


def read_config(path: Path) -> tuple[Study, dict]:
    if path.stat().st_size > 1024**2:
        raise ValueError("Configuration exceeds 1 MiB")
    config = json.loads(path.read_text())
    options = dict(config["study"])
    for key in ("data_root", "index", "manifest", "verified_identities"):
        if options.get(key) is not None:
            options[key] = Path(options[key])
    options["allowed_roots"] = tuple(Path(root) for root in options["allowed_roots"])
    options["start"], options["end"] = utc_seconds(options["start"]), utc_seconds(options["end"])
    return Study(**options), config


def summary(rows: list[dict], config: dict) -> dict:
    partitions = config["partitions"]
    ends = tuple(
        utc_seconds(partitions[key]) for key in ("discovery_end", "validation_end", "final_end")
    )
    start = utc_seconds(config["study"]["start"])
    end = utc_seconds(config["study"]["end"])
    if not ends[0] < ends[1] < ends[2] or not start < end <= ends[2]:
        raise ValueError("Prespecified increasing partition ends must cover the census range")
    options = config.get("analysis", {})
    samples, seed = options.get("bootstrap_samples", 0), options.get("seed", 70)
    min_days, min_markets = options.get("min_days", 3), options.get("min_markets", 20)
    for name, value, lower, upper in (
        ("bootstrap_samples", samples, 0, 10000),
        ("seed", seed, 0, 2**32 - 1),
        ("min_days", min_days, 2, 10000),
        ("min_markets", min_markets, 2, 10000),
    ):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"Invalid bounded analysis option: {name}")
    groups = defaultdict(list)
    exclusions = Counter()
    for row in rows:
        partition = _partition(row["start"], row["close"], ends)
        if partition is None:
            exclusions["crosses_partition_boundary"] += 1
            continue
        groups[(row["asset"], row["duration_seconds"], partition)].append(row)
    results = []
    rng = random.Random(seed)
    for (asset, duration, partition), group in sorted(groups.items()):
        item = {
            "asset": asset,
            "duration_seconds": duration,
            "partition": partition,
            "scheduled_windows": len(group),
            "eligible_open_windows": sum(row["eligible_open"] for row in group),
            "complete_windows": sum(row["complete"] for row in group),
            "censored_windows": sum(row["censored"] for row in group),
            "missing_or_invalid_windows": sum(bool(row["census_exclusions"]) for row in group),
            "census_exclusions": dict(
                Counter(reason for row in group for reason in row["census_exclusions"])
            ),
        }
        for transition in ("p12", "p23"):
            risk = [row for row in group if row["transitions"][transition]["at_risk"]]
            known = [row for row in risk if row["transitions"][transition]["success"] is not None]
            successes = sum(row["transitions"][transition]["success"] is True for row in known)
            days = sorted({row["start"] // 86400 for row in group})
            blocks = [
                (
                    sum(
                        row["transitions"][transition]["success"] is True
                        for row in known
                        if row["start"] // 86400 == day
                    ),
                    sum(row["start"] // 86400 == day for row in known),
                )
                for day in days
            ]
            estimate = _estimate(
                successes,
                len(known),
                blocks,
                markets=len(risk),
                min_days=min_days,
                min_markets=min_markets,
                samples=samples,
                rng=rng,
                informative_days=sum(denominator > 0 for _, denominator in blocks),
                outcome_complete=len(known) == len(risk),
            )
            item[transition] = {
                "eligible_at_risk": len(risk),
                "completed_at_risk": len(known),
                "observed_successes": successes,
                "completed_non_successes": len(known) - successes,
                "censored_unknown": len(risk) - len(known),
                "observed_complete_case_ratio": successes / len(known) if known else None,
                "guarded_estimate": estimate,
            }
        results.append(item)
    return {
        "groups": results,
        "scheduled_windows": len(rows),
        "partition_exclusions": dict(exclusions),
        "warning": WARNING,
        "interpretation": (
            "Observed-complete-case ratios can be selection biased. Guarded estimates require "
            "no unknown at-risk paths and sample-size guards. Missing/left-truncated windows "
            "cannot supply a known absence. Day blocks use scheduled UTC start days; final is "
            "not optimized or used for selection. Schedule is a prespecified assumed universe, "
            "not proof that every scheduled market was listed."
        ),
    }


def _compact(window: dict) -> dict:
    result = {
        key: window[key]
        for key in (
            "start",
            "close",
            "slug",
            "asset",
            "duration_seconds",
            "eligible_open",
            "complete",
            "censored",
            "exclusions",
            "transitions",
            "source_diagnostics",
            "source_choices",
            "selected_source",
        )
    } | {"census_exclusions": window["census_exclusions"]}
    result["source_diagnostics"] = {
        side: {key: value for key, value in diagnostic.items() if key != "raw_anomalies"}
        for side, diagnostic in window["source_diagnostics"].items()
    }
    return result


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, allow_nan=False, sort_keys=True) + "\n")


class BoundedOutputs:
    def __init__(self, limit: int) -> None:
        if type(limit) is not int or not 0 < limit <= MAX_REPORT_BYTES:
            raise ValueError("max_output_bytes must be in [1, 256 MiB]")
        self.limit, self.bytes = limit, 0

    def line(self, stream, payload: dict) -> None:
        line = json.dumps(payload, allow_nan=False, sort_keys=True) + "\n"
        self.text(stream, line)

    def text(self, stream, line: str) -> None:
        self.bytes += len(line.encode())
        if self.bytes > self.limit:
            raise ValueError("Output limit exceeded; partial output is not a completed study")
        stream.write(line)

    def json(self, path: Path, payload: object) -> None:
        line = json.dumps(payload, indent=2, allow_nan=False, sort_keys=True) + "\n"
        self.bytes += len(line.encode())
        if self.bytes > self.limit:
            raise ValueError("Output limit exceeded; partial output is not a completed study")
        path.write_text(line)


def run(stage: str, config_path: Path, output: Path) -> None:
    study, config = read_config(config_path)
    if "partitions" in config:
        summary([], config)  # Validate prespecification before reading any market.
    output = output.resolve()
    roots = [root.resolve() for root in study.allowed_roots]
    if any(output.is_relative_to(root) for root in roots):
        raise ValueError("Reports must be outside all read-only allowed roots")
    output.mkdir(parents=True, exist_ok=False)
    limits = BoundedOutputs(config.get("max_output_bytes", MAX_REPORT_BYTES))
    rows = []
    with (
        (output / "windows.jsonl").open("w") as windows,
        (output / "observations.jsonl").open("w") as observations,
        (output / "crossings.jsonl").open("w") as crossings,
        (output / "raw_anomalies.jsonl").open("w") as anomalies,
        (output / "touches.csv").open("w", newline="") as touches,
    ):
        fields = (
            "slug",
            "touch_number",
            "side",
            "timestamp_us",
            "remaining_us",
            "transition_us",
            "bid_price",
            "ask_price",
            "bid_size",
            "ask_size",
            "opposite_age_us",
            "cheap_bid_price",
            "cheap_ask_price",
            "cheap_bid_size",
            "cheap_ask_size",
        )
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        limits.text(touches, buffer.getvalue())
        buffer.seek(0)
        buffer.truncate(0)
        for window in study_windows(study):
            compact = _compact(window)
            rows.append(compact)
            limits.line(windows, compact)
            for diagnostic in window["source_diagnostics"].values():
                for anomaly in diagnostic["raw_anomalies"]:
                    limits.line(anomalies, {"window_slug": window["slug"], **anomaly})
            if stage == "extract":
                if config.get("emit_observations", False):
                    for side_rows in window["observations"].values():
                        for row in side_rows:
                            limits.line(observations, {"window_slug": window["slug"], **row})
                for crossing in window["diagnostic_crossings"]:
                    limits.line(crossings, {"slug": window["slug"], "kind": "midpoint", **crossing})
                for target in window["bid_targets"]:
                    limits.line(crossings, {"slug": window["slug"], "kind": "bid_target", **target})
                for touch in window["touches"]:
                    row, cheap = touch["row"], touch["opposite_row"]
                    record = {key: touch.get(key) for key in fields}
                    record["slug"] = window["slug"]
                    for key in ("bid_price", "ask_price", "bid_size", "ask_size"):
                        record[key], record[f"cheap_{key}"] = row[key], cheap[key]
                    writer.writerow(record)
                    limits.text(touches, buffer.getvalue())
                    buffer.seek(0)
                    buffer.truncate(0)
    limits.json(
        output / "audit.json",
        {
            "scheduled_windows": len(rows),
            "eligible_open_windows": sum(row["eligible_open"] for row in rows),
            "complete_windows": sum(row["complete"] for row in rows),
            "census_exclusions": dict(
                Counter(reason for row in rows for reason in row["census_exclusions"])
            ),
            "coverage_exclusions": dict(
                Counter(reason for row in rows for reason in row["exclusions"])
            ),
            "execution_replay_supported": False,
            "warning": WARNING,
        },
    )
    if "partitions" in config:
        limits.json(output / "report.json", summary(rows, config))
    root = Path(__file__).resolve().parents[3]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True))
    inputs = [study.index, config_path]
    inputs += [path for path in (study.manifest, study.verified_identities) if path is not None]
    limits.json(
        output / "manifest.json",
        {
            "stage": stage,
            "status": "completed",
            "configuration": config,
            "resolved_study": study.configuration(),
            "commit": commit,
            "dirty_worktree": dirty,
            "policy": POLICY,
            "warning": WARNING,
            "execution_replay_supported": False,
            "input_sha256": {str(path): digest(path) for path in inputs},
            "source_sha256": {
                row["slug"]: {
                    side: diagnostic["sha256"]
                    for side, diagnostic in row["source_diagnostics"].items()
                }
                for row in rows
            },
            "census_exclusions": {
                row["slug"]: row["census_exclusions"] for row in rows if row["census_exclusions"]
            },
            "output_sha256": {path.name: digest(path) for path in output.iterdir()},
        },
    )


def report(input_dir: Path, output: Path) -> None:
    manifest_path = input_dir / "manifest.json"
    if manifest_path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Manifest exceeds report limit")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") != "completed":
        raise ValueError("Cannot report an incomplete extraction")
    windows = input_dir / "windows.jsonl"
    if windows.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("Report input exceeds byte limit")
    if digest(windows) != manifest["output_sha256"]["windows.jsonl"]:
        raise ValueError("Report input hash mismatch")
    rows = []
    with windows.open() as stream:
        for line in stream:
            rows.append(json.loads(line))
            if len(rows) > MAX_REPORT_ROWS:
                raise ValueError("Report row limit exceeded")
    if (
        len(rows)
        != (
            utc_seconds(manifest["configuration"]["study"]["end"])
            - utc_seconds(manifest["configuration"]["study"]["start"])
        )
        // int(manifest["resolved_study"]["timeframe"][:-1])
        // 60
    ):
        raise ValueError("Report census row count mismatch")
    if output.exists() or any(
        output.resolve().is_relative_to(Path(root).resolve())
        for root in manifest["resolved_study"]["allowed_roots"]
    ):
        raise ValueError("Report output must be new and outside the source data root")
    _write_json(output, summary(rows, manifest["configuration"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("audit", "extract"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", required=True, type=Path)
        sub.add_argument("--output", required=True, type=Path)
    sub = commands.add_parser("report")
    sub.add_argument("--input", required=True, type=Path)
    sub.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "report":
            report(args.input, args.output)
        else:
            run(args.command, args.config, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Study rejected: {exc}\n")


if __name__ == "__main__":
    main()
