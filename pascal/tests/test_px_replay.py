"""Synthetic checks for px_replay.py (run before any recorded day is read).

Run: ~/shadow-venv/bin/python -m pytest -q pascal/tests/test_px_replay.py
"""

import gzip
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import px_replay as px  # noqa: E402


@pytest.fixture(scope="module")
def res(tmp_path_factory):
    return px.synth(tmp_path_factory.mktemp("px"))


def tot(rs):
    return sum(r["pnl"] for r in rs)


def test_flat_books_no_trades(res):
    assert all(len(v) == 0 for v in res["flat"].values())


def test_planted_3c_gap_5s_profits(res):
    for order, rs in res["pm5s"].items():
        assert len(rs) == 1, order
        r = rs[0]
        assert r["pairs"] == r["shares"] and r["unwound"] == 0
        assert abs(r["pnl"] / r["shares"] - 0.03) < 1e-5, (order, r)  # 3¢ after fees on every share


def test_planted_gap_100ms_on_pm_does_not_profit(res):
    for order, rs in res["pm100ms"].items():
        assert tot(rs) <= 1e-9, (order, rs)
    assert tot(res["pm100ms"]["pascal_first"]) < 0  # Pascal leg filled, PM hedge gone -> unwind loss


def test_pascal_side_100ms_gap_is_reachable_pascal_first(res):
    # Pascal is 2 ms away: a 100 ms Pascal-side gap fills Pascal first; PM-first arrives too late.
    assert tot(res["pas100ms"]["pascal_first"]) > 0
    assert tot(res["pas100ms"]["pm_first"]) < 0


def test_px_b_fill_rule(tmp_path):
    day, t0 = px._synth_day(tmp_path, "flat")
    f = tmp_path / "rec" / day / "01.jsonl.gz"
    lines = [json.loads(ln) for ln in gzip.open(f, "rt")]
    sym = "SYN_EVT.A"
    # PM mid 0.50, h = 1¢ -> our bid 0.49 = best bid (joining): a sell taker at 0.49 does not fill us,
    # one at 0.48 (through our price) does, at our 0.49.
    tA, tB = t0 + 600e3 + 1, t0 + 1200e3 + 1
    for i, (t, p) in enumerate(((tA, 0.49), (tB, 0.48))):
        lines.append({"k": "pt", "r": t + 7, "id": str(i), "s": sym, "p": p, "sz": 30, "ts": t, "side": "ASK",
                      "mk": "m", "tk": "t"})
    lines.sort(key=lambda o: o["r"])
    with gzip.open(f, "wt") as fh:
        for o in lines:
            fh.write(json.dumps(o) + "\n")
    px.build_cache(day, rec=tmp_path / "rec", cache=tmp_path / "cache")
    meta = json.loads((tmp_path / "cache" / day / "meta.json").read_text())
    info = dict(ert=t0 + 5 * 86400e3, sports=False, start=0, event="SYN_EVT", is_mece=False, cond="c",
                p_rate=0.02, rebate=0.25)
    mk = px.Mkt(sym, tmp_path / "cache" / day, meta, {sym: info}, {"c": 0.0})
    fills = px.run_px_b(mk, 0.01, 430.0)
    assert len(fills) == 1
    fl = fills[0]
    assert fl["t"] == tB and abs(fl["q"] - 0.49) < 1e-9 and not fl["improving"]
    # hedge: buy PM short at 1 - 0.49 = 0.51 (PM fee 0) -> pair costs exactly $1: zero P&L, rebate positive
    assert abs(fl["pnl"]) < 1e-9 and fl["rebate"] > 0
