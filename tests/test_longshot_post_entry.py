from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from prediction_market_extensions.backtesting.longshot.post_entry import diagnose, run, windows
from prediction_market_extensions.backtesting.longshot.quote_cli import BoundedOutputs, read_config
from prediction_market_extensions.backtesting.longshot.top_of_book import census, digest
from tests.test_longshot_top_of_book import (
    OPEN,
    ROOT,
    START,
    mapping_file,
    row,
)

pytest_plugins = ("tests.test_longshot_top_of_book",)


def stream(side, states):
    return [row(side, ts, *("0.69", "0.71") if high else ("0.39", "0.41")) for ts, high in states]


def evaluate(study, up, down):
    return diagnose(next(census(study)), {"UP": up, "DOWN": down}, study)


def config_file(study, tmp_path, **options):
    config = study.configuration()
    for key in ("start", "end"):
        config[key] = datetime.fromtimestamp(getattr(study, key), UTC).isoformat()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"study": config, **options}))
    return path


def test_ordinary_success_without_preopen_and_no_complement_inference(store):
    study, add = store
    add("UP", stream("UP", [(0, False), (100_000, True), (200_000, True)]))
    add(
        "DOWN",
        [
            row("DOWN", 0, "0.50", "0.62", ask_size="12.345"),
            row("DOWN", 200_000, "0.69", "0.71"),
        ],
    )
    result, hashes = next(windows(study))
    assert result["status"] == "success"
    assert result["entry_us"] == OPEN + 100_000
    assert result["success_us"] == OPEN + 200_000
    assert result["opposite_ask"] == "0.62"
    assert result["opposite_ask_size"] == "12.345"
    assert result["opposite_age_us"] == 100_000
    assert result["identity"] == "unverified"
    assert len(hashes) == 2


def test_same_side_recrossing_does_not_replace_entry(store):
    study, _ = store
    result = evaluate(
        study,
        stream("UP", [(0, False), (1, True), (2, False), (3, True)]),
        stream("DOWN", [(0, False), (4, True)]),
    )
    assert result["entry_us"] == OPEN + 1
    assert result["entry_side"] == "UP"
    assert result["success_us"] == OPEN + 4


def test_first_observed_above_is_not_a_crossing_even_with_preopen(store):
    study, _ = store
    result = evaluate(
        study,
        stream("UP", [(-1, False), (0, True), (1, True)]),
        stream("DOWN", [(-1, False), (0, False), (1, False)]),
    )
    assert result["status"] == "no_entry"
    assert result["entry_us"] is None


@pytest.mark.parametrize("kind", ["invalid", "gap"])
def test_baseline_reset_before_entry_selects_later_observed_segment(store, kind):
    study, _ = store
    times = [0, 100_000, 200_000, 300_000, 400_000]
    if kind == "gap":
        times = [0, 2_000_000, 2_100_000, 2_200_000, 2_300_000]
    up = stream("UP", list(zip(times, [False, False, True, False, True])))
    down = stream("DOWN", [(ts, False) for ts in times])
    if kind == "invalid":
        up[1]["bid_price"] = None
    result = evaluate(study, up, down)
    expected = times[4] if kind == "invalid" else times[2]
    assert result["entry_us"] == OPEN + expected
    assert result["status"] == "unknown"
    assert result["anomaly_counts"]["pre_entry_baseline_resets"] >= 1


@pytest.mark.parametrize("kind", ["invalid", "gap", "stale"])
def test_break_after_entry_then_apparent_crossing_stays_unknown(store, kind):
    study, _ = store
    times = [0, 100_000, 200_000, 300_000, 400_000]
    if kind == "gap":
        times = [0, 100_000, 2_000_000, 2_100_000, 2_200_000]
    if kind == "stale":
        times = [0, 100_000, 1_100_001, 1_200_000, 1_300_000]
    up = stream("UP", [(ts, i > 0) for i, ts in enumerate(times)])
    down = stream("DOWN", [(ts, i == 4) for i, ts in enumerate(times)])
    if kind == "invalid":
        up[2]["ask_price"] = None
    if kind == "stale":
        down = [down[0], down[3], down[4]]
    result = evaluate(study, up, down)
    assert result["status"] == "unknown"
    assert result["break_us"] == OPEN + times[2]
    assert result["success_us"] is None
    assert result["entry_us"] == OPEN + 100_000


def test_late_invalidity_and_missing_terminal_do_not_erase_success(store):
    study, _ = store
    up = stream("UP", [(0, False), (1, True), (2, True), (3, True)])
    up[-1]["ask_price"] = None
    result = evaluate(study, up, stream("DOWN", [(0, False), (2, True)]))
    assert result["status"] == "success"
    assert result["anomaly_counts"]["invalid_paired_states"] == 1


@pytest.mark.parametrize("null_field", [None, "bid_price", "ask_price"])
def test_complete_non_success_requires_valid_terminal_coverage(store, null_field):
    study, _ = store
    up = stream("UP", [(ts * 1_000_000, ts == 1) for ts in range(300)])
    down = stream("DOWN", [(ts * 1_000_000, False) for ts in range(300)])
    if null_field:
        down[-1][null_field] = None
    result = evaluate(study, up, down)
    assert result["status"] == ("unknown" if null_field else "completed_non_success")
    assert result["entry_us"] == OPEN + 1_000_000


@pytest.mark.parametrize("after_entry", [False, True])
def test_simultaneous_opposing_crossings_are_ambiguous_not_ordered(store, after_entry):
    study, _ = store
    up = stream("UP", [(0, False), (1, after_entry), (2, False), (3, True)])
    down = stream("DOWN", [(0, False), (1, False), (2, False), (3, True)])
    result = evaluate(study, up, down)
    assert result["status"] == ("unknown" if after_entry else "ambiguous")
    assert result["break_us"] == OPEN + 3
    assert result["success_us"] is None
    assert result["anomaly_counts"]["simultaneous_opposing_crossings"] == 1


def test_terminal_ambiguity_does_not_count_later_breaks_as_pre_entry_resets(store):
    study, _ = store
    up = stream("UP", [(0, False), (1, True), (2, True)])
    up[-1]["ask_price"] = None
    result = evaluate(study, up, stream("DOWN", [(0, False), (1, True)]))
    assert result["status"] == "ambiguous"
    assert result["anomaly_counts"].get("pre_entry_baseline_resets", 0) == 0
    assert result["anomaly_counts"]["invalid_paired_states"] == 1


def test_opposite_already_above_is_not_new_upcrossing(store):
    study, _ = store
    result = evaluate(
        study,
        stream("UP", [(0, False), (1, True), (2, True)]),
        stream("DOWN", [(0, True), (1, True), (2, True)]),
    )
    assert result["entry_side"] == "UP"
    assert result["status"] == "unknown"
    assert result["success_us"] is None


def test_opposite_initially_above_must_drop_and_recross_after_entry(store):
    study, _ = store
    result = evaluate(
        study,
        stream("UP", [(0, False), (1, True), (2, True), (3, True)]),
        stream("DOWN", [(0, True), (1, True), (2, False), (3, True)]),
    )
    assert result["status"] == "success"
    assert result["success_us"] == OPEN + 3


def test_stale_pair_before_entry_and_future_quote_cannot_supply_baseline(store):
    study, _ = store
    up = stream("UP", [(0, False), (1_000_001, True)])
    down = stream("DOWN", [(0, False), (1_000_002, False)])
    result = evaluate(study, up, down)
    assert result["status"] == "no_entry"
    assert result["entry_us"] is None


def test_close_boundary_crossing_excluded_and_missing_terminal_unknown(store):
    study, _ = store
    result = evaluate(
        study,
        stream("UP", [(0, False), (1, True), (300_000_000, True)]),
        stream("DOWN", [(0, False), (300_000_000, True)]),
    )
    assert result["status"] == "unknown"
    assert result["success_us"] is None
    assert result["anomaly_counts"]["missing_valid_terminal_coverage"] == 1


def test_whole_terminal_row_and_exact_decimal_threshold(store):
    study, add = store
    add(
        "UP",
        [
            row("UP", 0),
            row("UP", 1, "0.699999999999999999999999999998", "0.70"),
            row("UP", 2, "0.69", "0.71"),
        ],
    )
    add(
        "DOWN",
        [row("DOWN", 0), row("DOWN", 3, "0.69", "0.71"), row("DOWN", 3, ask_price=None)],
    )
    result, _ = next(windows(study))
    assert result["entry_us"] == OPEN + 2
    assert result["status"] == "unknown"
    assert result["break_us"] == OPEN + 3
    assert result["anomaly_counts"]["raw_invalid_rows"] == 1


def test_missing_indexed_file_and_scheduled_windows_are_preserved(store):
    study, add = store
    add("UP", [row("UP", 0)]).unlink()
    add("DOWN", [row("DOWN", 0)])
    results = list(windows(replace(study, end=START + 600)))
    assert len(results) == 2
    assert all(result["status"] == "missing" for result, _ in results)
    assert "UP:" in results[0][0]["reason"]


def test_unreadable_selected_file_records_unavailable_hash_and_continues(store, monkeypatch):
    study, add = store
    up = add("UP", [row("UP", 0)])
    down = add("DOWN", [row("DOWN", 0)])

    def fail_selected(path):
        if path == up:
            raise OSError("unreadable")
        return digest(path)

    monkeypatch.setattr(
        "prediction_market_extensions.backtesting.longshot.post_entry.digest", fail_selected
    )
    results = list(windows(replace(study, end=START + 600)))
    assert len(results) == 2
    invalid, hashes = results[0]
    assert invalid["status"] == "invalid"
    assert "UP:unreadable" in invalid["reason"]
    assert hashes[str(up)] == "unavailable"
    assert hashes[str(down)] == digest(down)
    assert results[1][0]["status"] == "missing"


def test_manifest_agreement_remains_unverified_and_identity_conflict_invalid(store):
    study, add = store
    add("UP", stream("UP", [(0, False), (1, True)]))
    add("DOWN", stream("DOWN", [(0, False), (2, True)]))
    manifest = mapping_file(study)
    result, _ = next(windows(replace(study, manifest=manifest)))
    assert result["status"] == "success"
    assert result["identity"] == "unverified"
    manifest.write_text(manifest.read_text().replace('"up"', '"other"'))
    result, _ = next(windows(replace(study, manifest=manifest)))
    assert result["status"] == "invalid"
    assert result["entry_us"] is None


def test_compact_outputs_hashes_and_direct_offline_cli(store, tmp_path):
    study, add = store
    for side in ("UP", "DOWN"):
        add(side, stream(side, [(0, False), (1, side == "UP"), (2, True)]))
    config = config_file(replace(study, end=START + 600), tmp_path)
    output = tmp_path / "diagnostic"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "longshot_post_entry.py"),
        "--config",
        str(config),
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert {path.name for path in output.iterdir()} == {
        "windows.csv",
        "summary.json",
        "manifest.json",
    }
    with (output / "windows.csv").open() as source:
        rows = list(csv.DictReader(source))
    assert [item["status"] for item in rows] == ["success", "missing"]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["scheduled_windows"] == 2
    assert summary["statuses"]["success"] == 1
    assert summary["statuses"]["completed_non_success"] == 0
    assert summary["statuses"]["unknown"] == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["input_sha256"][str(config)] == digest(config)
    assert len(manifest["source_sha256"][rows[0]["slug"]]) == 2
    for name, expected in manifest["output_sha256"].items():
        assert digest(output / name) == expected


@pytest.mark.parametrize("failure", ["output_limit", "interrupted", "manifest_interrupted"])
def test_incomplete_output_never_has_completion_manifest(store, tmp_path, monkeypatch, failure):
    study, _ = store
    output = tmp_path / "incomplete"
    config = config_file(
        study, tmp_path, **({"max_output_bytes": 1} if failure == "output_limit" else {})
    )
    if failure == "interrupted":

        def interrupted(_study):
            raise KeyboardInterrupt
            yield

        monkeypatch.setattr(
            "prediction_market_extensions.backtesting.longshot.post_entry.windows", interrupted
        )
    if failure == "manifest_interrupted":
        original = BoundedOutputs.json

        def interrupt_manifest(self, path, payload):
            original(self, path, payload)
            if path.name == "manifest.pending.json":
                raise KeyboardInterrupt

        monkeypatch.setattr(BoundedOutputs, "json", interrupt_manifest)
    with pytest.raises(ValueError if failure == "output_limit" else KeyboardInterrupt):
        run(config, output)
    assert not (output / "manifest.json").exists()


def test_fixed_server_example_is_only_twelve_windows_without_verified_identity():
    study, config = read_config(ROOT / "docs" / "longshot-post-entry-btc5m.json")
    assert (study.end - study.start) // study.duration == study.max_windows == 12
    assert study.start == int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())
    assert study.symbol == "BTC" and study.timeframe == "5m" and study.exchange == "polymarket"
    assert study.gap_us == study.pair_age_us == study.terminal_age_us == 1_000_000
    assert study.verified_identities is None
    assert config["max_output_bytes"] == 1024**2


@pytest.mark.parametrize("option", ["gap_us", "pair_age_us", "anchor_age_us", "terminal_age_us"])
def test_one_second_limits_cannot_be_widened(store, tmp_path, option):
    study, _ = store
    config = config_file(replace(study, **{option: 1_000_001}), tmp_path)
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="one second"):
        run(config, output)
    assert not output.exists()


def test_parser_and_output_guards_are_reused(store, tmp_path):
    study, add = store
    for side in ("UP", "DOWN"):
        add(side, [row(side, 0), row(side, 1)])
    result, _ = next(windows(replace(study, max_rows=1)))
    assert result["status"] == "invalid"
    assert "row limit" in result["reason"]
    result, _ = next(windows(replace(study, exchange="polymarket")))
    assert result["status"] == "invalid"
    with pytest.raises(ValueError, match="outside"):
        run(config_file(study, tmp_path), study.data_root / "output")
