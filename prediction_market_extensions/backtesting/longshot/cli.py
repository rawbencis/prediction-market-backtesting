from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from prediction_market_extensions.backtesting.longshot.sources import (
    load_vendor_markets,
    read_configuration,
    record_digest,
    select_markets,
    timestamp_ns,
)

DATASET_NAMES = ("market_audit", "events", "trades", "equity", "market_equity")


def write_dataset(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, allow_nan=False, sort_keys=True)
                    for key, value in row.items()
                }
            )


def read_dataset(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="") as stream:
        return [
            {key: json.loads(value) if value else None for key, value in row.items()}
            for row in csv.DictReader(stream)
        ]


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def manifest(
    config: dict[str, Any], markets: Sequence[dict[str, Any]], output: Path, stage: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    checksums = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output.glob("*.csv")
    }
    write_json(
        output / "manifest.json",
        {
            "schema_version": 1,
            "stage": stage,
            "configuration": config,
            "seed": config.get("seed", 70),
            "commit": commit,
            "dirty_worktree": dirty,
            "source_coverage": {
                market["market_id"]: market.get("source_coverage", {}) for market in markets
            },
            "dataset_sha256": checksums,
            "synthetic": config["data"]["vendor"] == "synthetic",
            "warnings": [
                "Observational research, not causal proof or a profitability claim.",
                "Source coverage is loaded-record coverage, not proof of exchange completeness.",
                "Missing underlying inputs produce unavailable features, not zero-valued features.",
            ],
        },
    )


def load_inputs(config: dict[str, Any]):  # type: ignore[no-untyped-def]
    if config["data"]["vendor"] != "synthetic":
        return asyncio.run(load_vendor_markets(config))
    from prediction_market_extensions.backtesting.longshot.synthetic import synthetic_fixture

    markets, records, instruments = synthetic_fixture()
    selected, excluded = select_markets(markets, config.get("filters", {}))
    for market in selected:
        market["source_coverage"] = {
            side: {
                "record_count": len(rows),
                "sha256": record_digest(rows),
                "source": "bundled_synthetic",
            }
            for side, rows in records[market["market_id"]].items()
        }
    return selected + excluded, records, instruments


def extract_inputs(config: dict[str, Any], markets, records):  # type: ignore[no-untyped-def]
    from nautilus_trader.model.data import OrderBookDeltas

    from prediction_market_extensions.backtesting.longshot.dataset import extract_market
    from prediction_market_extensions.backtesting.longshot.features import (
        attach_underlying_features,
    )
    from prediction_market_extensions.backtesting.longshot.state import observations_from_records

    audits, events = [], []
    research = config.get("research", {})
    for market in markets:
        if market.get("exclusion_reasons"):
            audits.append(market)
            continue
        pair_records = records.get(market["market_id"], {})
        observations = observations_from_records(
            {
                side: [
                    record
                    for record in pair_records.get(side, [])
                    if isinstance(record, OrderBookDeltas)
                ]
                for side in ("UP", "DOWN")
            }
        )
        audit, market_events = extract_market(
            market,
            observations,
            threshold=research.get("threshold", 0.70),
            quantity=research.get("quantity", 5),
            max_gap_ns=int(research.get("max_gap_seconds", 30) * 1_000_000_000),
            max_stale_ns=int(research.get("max_stale_seconds", 5) * 1_000_000_000),
        )
        audit["source_coverage"] = market.get("source_coverage", {})
        if market.get("load_error"):
            audit.update(
                eligible=False, complete=False, censored=True, load_error=market["load_error"]
            )
            market_events = []
        audits.append(audit)
        events.extend(market_events)
    features = config.get("underlying", {})
    attach_underlying_features(
        events,
        markets,
        features.get("records_by_asset", {}),
        max_age_ns=int(features.get("max_age_seconds", 1) * 1_000_000_000),
        lookback_ns=int(features.get("lookback_seconds", 60) * 1_000_000_000),
    )
    return audits, events


def analyze_output(config: dict[str, Any], output: Path) -> dict[str, Any]:
    from prediction_market_extensions.backtesting.longshot.analysis import analyze

    previous = json.loads((output / "manifest.json").read_text())
    if previous["configuration"] != config:
        raise ValueError("Analysis configuration differs from the dataset manifest; re-extract")
    for name, expected in previous["dataset_sha256"].items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Dataset checksum mismatch: {name}")
    rows = {
        name: read_dataset(output / f"{name}.csv") if (output / f"{name}.csv").exists() else []
        for name in DATASET_NAMES
    }
    event_labels = {
        (event["market_id"], event["touch_number"]): event.get("next_touch")
        for event in rows["events"]
    }
    analysis_trades = [
        {
            **trade,
            "filled": trade["entered"],
            "net_pnl": trade["pnl"],
            "deployed_capital": sum(
                fill["quantity"] * fill["price"] + fill["commission"]
                for fill in trade["fills"]
                if fill["action"] == "buy"
            ),
            "next_touch": event_labels.get((trade["market_id"], trade.get("touch_number"))),
        }
        for trade in rows["trades"]
    ]
    accounting_ends = {
        market["market_id"]: max(market["close_ns"], market.get("settlement_ns") or 0)
        for market in rows["market_audit"]
        if isinstance(market.get("close_ns"), int)
    }
    if rows["equity"] and not rows["market_equity"]:
        raise ValueError("Market-attribution equity is missing; rerun replay before analysis")
    # The primary equity file is already one combined account per variant.
    # Grouped analysis instead consumes its zero-capital market attribution
    # sidecar, never duplicating initial account cash across markets.
    analysis_equity = [
        point
        for point in rows["market_equity"]
        if point["ts_ns"] <= accounting_ends[point["market_id"]]
    ]
    partitions = config["partitions"]
    return analyze(
        rows["market_audit"],
        rows["events"],
        analysis_trades,
        analysis_equity,
        discovery_end_ns=timestamp_ns(partitions["discovery_end"]),
        validation_end_ns=timestamp_ns(partitions["validation_end"]),
        final_end_ns=timestamp_ns(partitions["final_end"]),
        seed=config.get("seed", 70),
        bootstrap_samples=config.get("bootstrap_samples", 1000),
        min_days=config.get("min_days", 5),
        min_markets=config.get("min_markets", 20),
    )


def replay_inputs(config, markets, records, instruments):  # type: ignore[no-untyped-def]
    from prediction_market_extensions.backtesting.longshot.replay import replay_variants

    research, execution = config.get("research", {}), config.get("execution", {})
    return replay_variants(
        markets,
        records,
        instruments,
        threshold=research.get("threshold", 0.70),
        quantity=research.get("quantity", 5),
        decision_latency_ms=execution.get("decision_latency_ms", 25),
        order_latency_ms=execution.get("order_latency_ms", 85),
        max_entry_price=execution.get("max_entry_price", 0.35),
        initial_cash=execution.get("initial_cash", 1000),
        max_positions=execution.get("max_positions", 10),
        max_gap_ns=int(research.get("max_gap_seconds", 30) * 1_000_000_000),
        max_stale_ns=int(research.get("max_stale_seconds", 5) * 1_000_000_000),
    )


def run_stage(config: dict[str, Any], stage: str, output: Path) -> None:
    if config.get("primary", True) and config.get("research", {}).get("threshold", 0.70) != 0.70:
        raise ValueError("Primary threshold is 0.70; other thresholds belong in sensitivity_trials")
    output.mkdir(parents=True, exist_ok=True)
    if stage == "analyze":
        write_json(output / "analysis.json", analyze_output(config, output))
        return
    markets, records, instruments = load_inputs(config)
    audits, events = extract_inputs(config, markets, records)
    write_dataset(output / "market_audit.csv", audits)
    write_dataset(output / "events.csv", events)
    write_dataset(output / "trades.csv", [])
    write_dataset(output / "equity.csv", [])
    write_dataset(output / "market_equity.csv", [])
    if stage in {"replay", "all"}:
        eligible = {row["market_id"] for row in audits if row.get("eligible")}
        replay_markets = [market for market in markets if market["market_id"] in eligible]
        results = (
            replay_inputs(config, replay_markets, records, instruments)
            if replay_markets
            else {
                "trades": [],
                "equity": [],
                "market_equity": [],
                "variants": [],
                "status": "not_run_no_eligible_markets",
                "disclosures": ["No eligible markets; execution statistics are unavailable."],
            }
        )
        write_dataset(output / "trades.csv", results["trades"])
        write_dataset(output / "equity.csv", results["equity"])
        write_dataset(output / "market_equity.csv", results["market_equity"])
        write_json(
            output / "execution.json",
            {k: v for k, v in results.items() if k not in {"trades", "equity", "market_equity"}},
        )
    manifest(config, markets, output, stage)
    if stage == "all":
        write_json(output / "analysis.json", analyze_output(config, output))
    print(f"Longshot {stage}: {len(audits)} audited markets, {len(events)} touches -> {output}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Causal 70-cent L2 longshot research")
    parser.add_argument(
        "stage", choices=("audit", "extract", "analyze", "replay", "sensitivity", "all")
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    config = read_configuration(args.config)
    if args.stage != "sensitivity":
        run_stage(config, args.stage, args.output)
        return
    trials = config.get("sensitivity_trials", [])
    if not trials:
        raise ValueError("sensitivity_trials must explicitly name each prespecified trial")
    seen = set()
    for trial in trials:
        name = trial["name"]
        if not name or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in name
        ):
            raise ValueError("Trial names must be lowercase letters, digits, '-' or '_'")
        if name in seen:
            raise ValueError(f"Duplicate trial name: {name}")
        seen.add(name)
        trial_config = json.loads(json.dumps(config))
        trial_config["research"] = {**config.get("research", {}), **trial.get("research", {})}
        trial_config["execution"] = {**config.get("execution", {}), **trial.get("execution", {})}
        trial_config["trial_name"] = name
        trial_config["primary"] = False
        run_stage(trial_config, "all", args.output / name)
