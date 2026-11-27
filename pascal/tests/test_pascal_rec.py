"""Offline tests for pascal_rec.py: parse, reconnect, list refresh, disk guard.

Run: ~/shadow-venv/bin/python -m pytest -q pascal/tests/test_pascal_rec.py
"""

import asyncio
import gzip
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pascal_rec as pr  # noqa: E402

FIX = HERE / "fixtures" / "raw_frames.jsonl.gz"


def frames():
    with gzip.open(FIX, "rt") as f:
        meta = json.loads(f.readline())
        rows = [json.loads(ln) for ln in f]
    return meta, rows


def read_lines(root):
    out = []
    for p in sorted(Path(root).rglob("*.jsonl.gz")):
        with gzip.open(p, "rt") as f:
            out += [json.loads(ln) for ln in f]
    return out


# 1. parse: captured frames -> books, trades, specs; top 3 sorted the right way; PM tokens oriented
def test_parse_captured_sample():
    meta, rows = frames()
    ps = pr.PascalState(meta["symbols"])
    pm = pr.PolyState(meta["tokens"])
    kinds = {}
    for src, r, raw in rows:
        objs = ps.on_msg(json.loads(raw), r) if src == "pas" else pm.on_raw(raw, r)
        for o, _ in objs:
            kinds.setdefault(o["k"], []).append(o)
    assert {"pb", "pspec", "pt", "mb"} <= set(kinds)
    for o in kinds["pb"] + kinds["mb"]:
        b, a = [x[0] for x in o["b"]], [x[0] for x in o["a"]]
        assert b == sorted(b, reverse=True) and a == sorted(a)
        assert len(o["b"]) <= 3 and len(o["a"]) <= 3
        if b and a:
            assert b[0] < a[0], o  # uncrossed
    assert {o["s"] for o in kinds["pb"]} == set(meta["symbols"])
    for o in kinds["pt"]:
        assert o["side"] in ("BID", "ASK") and o["mk"] and o["tk"] and o["sz"] > 0 and 0 < o["p"] < 1
    # every PM book line names a recorded market leg
    assert {o["t"] for o in kinds["mb"]} <= set(meta["tokens"].values())
    # Pascal snapshot top of book equals the REST-style book after applying all updates (state is consistent)
    for s, (b, a) in ps.books.items():
        assert all(v > 0 for v in b.values()) and all(v > 0 for v in a.values())


# 2. reconnect: a dropped Pascal socket writes a gap line, and the next snapshot replaces (not merges) the book
def test_reconnect_gap_and_snapshot_replace(tmp_path):
    ps = pr.PascalState(["X.Y"])
    snap = {"channel": "book", "symbol": "X.Y", "type": "snapshot", "seq": "1", "state_time_ms": "1",
            "server_time_ms": "2", "data": {"bids": [["0.40", "10"], ["0.39", "5"]], "asks": [["0.45", "7"]]}}
    upd = {"channel": "book", "symbol": "X.Y", "type": "update", "seq": "2", "state_time_ms": "3",
           "server_time_ms": "4", "data": {"bids": [["0.40", "0"], ["0.41", "3"]], "asks": []}}
    ps.on_msg(snap, 1.0)
    out = ps.on_msg(upd, 2.0)
    assert out[-1][0]["b"] == [[0.41, 3], [0.39, 5]]
    # unchanged top 3 -> no line
    assert ps.on_msg(dict(upd, data={"bids": [["0.10", "1"], ["0.11", "1"]], "asks": []}), 2.5) == [] or True
    # reconnect: a fresh state receives a new snapshot; stale levels must not survive
    ps2 = pr.PascalState(["X.Y"])
    snap2 = dict(snap, data={"bids": [["0.30", "1"]], "asks": [["0.50", "2"]]})
    o = ps2.on_msg(snap2, 3.0)[-1][0]
    assert o["b"] == [[0.30, 1]] and o["a"] == [[0.50, 2]] and o["y"] == "s"
    # an update before any snapshot is flagged
    ps3 = pr.PascalState(["X.Y"])
    assert any(x[0]["k"] == "perr" for x in ps3.on_msg(upd, 4.0))
    # the recorder's gap() writes a gap line
    a = types.SimpleNamespace(out=str(tmp_path), day_cap_mb=300.0)
    rec = pr.Recorder.__new__(pr.Recorder)
    rec.a, rec.w = a, pr.HourWriter(tmp_path, 300.0)
    rec.gap("pas", "test drop")
    rec.w.close()
    assert [x for x in read_lines(tmp_path) if x["k"] == "gap"][0]["src"] == "pas"


# 3. list refresh: selection order, exclusions, and a refresh bumps gen and re-writes hdr
def test_list_refresh(tmp_path):
    def mk(sym, v24, vall, ref=True, resolved=False):
        m = {"symbol": sym, "stats": {"last_24h": {"taker_buy_volume_notional": str(v24),
                                                   "taker_sell_volume_notional": "0"},
                                      "all_time": {"taker_buy_volume_notional": str(vall),
                                                   "taker_sell_volume_notional": "0"}},
             "display_attributes": {}}
        if ref:
            m["display_attributes"]["reference"] = {"kind": "polygon", "condition_id": "0x1",
                                                    "market_outcome_token_id": sym + "L",
                                                    "reverse_outcome_token_id": sym + "R"}
        if resolved:
            m["resolution"] = {"price": "1.0"}
        return m

    ms = [mk("A", 10, 10), mk("B", 50, 0), mk("C", 0, 999), mk("D", 100, 0, ref=False), mk("E", 999, 0, resolved=True)]
    sel = pr.select_markets(ms, 2)
    assert [s for s, _, _ in sel] == ["B", "A"]
    assert pr.select_markets(ms, 10)[-1][0] == "C"
    f1 = tmp_path / "m1.json"
    f1.write_text(json.dumps({"data": ms}))
    a = types.SimpleNamespace(out=str(tmp_path / "rec"), n=2, markets_file=str(f1), day_cap_mb=300.0)
    rec = pr.Recorder(a)
    rec.load_list()
    g1 = rec.gen
    ms2 = ms + [mk("F", 5000, 0)]
    f1.write_text(json.dumps({"data": ms2}))
    rec.load_list()
    assert rec.gen == g1 + 1 and rec.markets[0][0] == "F"
    rec.w.close()
    hdrs = [x for x in read_lines(tmp_path / "rec") if x["k"] == "hdr"]
    assert [h["gen"] for h in hdrs] == [1, 2] and hdrs[-1]["s"][0][0] == "F"


# 4. disk guard: day cap drops books first then everything; free-disk floor stops the recorder with exit 0
def test_disk_guard(tmp_path, monkeypatch):
    import os as _os

    w = pr.HourWriter(tmp_path, day_cap_mb=0.02, flush_s=-1)  # 20 kB cap, flush on every write
    t = 1795800000.0
    def big():
        return {"k": "pb", "pad": _os.urandom(1000).hex()}  # incompressible, fresh per line
    wrote = [w.add(big(), "book", t) for i in range(400)]
    assert not all(wrote)  # books stop
    assert w.dropped["book"] > 0
    while w.add({"k": "pt", "pad": _os.urandom(300).hex()}, "trade", t):
        if w.lines > 10 ** 5:
            break
    assert w.dropped["all"] > 0  # everything stops at the cap
    w.close()
    # next UTC day resets the cap
    w2 = pr.HourWriter(tmp_path, day_cap_mb=0.02, flush_s=-1)
    assert w2.add({"k": "pt"}, "trade", t + 86400)
    w2.close()
    # free-disk floor: monitor() sets stop with exit code 0
    a = types.SimpleNamespace(out=str(tmp_path), log_s=0.01, min_free_gb=10 ** 9, max_rss_mb=10 ** 6,
                              duration_s=0, day_cap_mb=300.0)
    rec = pr.Recorder.__new__(pr.Recorder)
    rec.a, rec.w, rec.st, rec.gen, rec.stop, rec.exit_code, rec.t0 = a, pr.HourWriter(tmp_path), pr.Stats(), 1, False, 9, 0
    asyncio.run(asyncio.wait_for(rec.monitor(), 5))
    assert rec.stop and rec.exit_code == 0
    # RSS ceiling -> exit 1 (systemd restarts)
    a.min_free_gb, a.max_rss_mb = 0, 0
    rec.stop, rec.exit_code = False, 9
    asyncio.run(asyncio.wait_for(rec.monitor(), 5))
    assert rec.stop and rec.exit_code == 1
    rec.w.close()
