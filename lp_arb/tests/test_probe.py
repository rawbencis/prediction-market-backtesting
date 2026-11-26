"""Probe tests: fakes only (no network, no SSM, no keys).  ../probe-venv/bin/python -m pytest lp_arb/tests -q"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lp_arb import paper as P
from lp_arb import probe as R

FIX = Path(__file__).parent / "fixtures" / "paper_2026-11-25.jsonl"
REPO = Path(__file__).resolve().parents[2]
FAKE_KEY = "0x" + "c0ffee" * 10 + "abcd"  # 64 hex, planted


# ---------------------------------------------------------------- helpers
def records(root, kind=None):
    out = []
    for f in sorted(Path(root).glob("*.jsonl")):
        for line in f.open():
            r = json.loads(line)
            if kind is None or r["kind"] == kind:
                out.append(r)
    return out


def set_book(book, bid, ask, bs=500.0, as_=500.0):
    book.clear()
    if bid:
        book.set(0, bid, bs)
    if ask:
        book.set(1, ask, as_)


class FakeL:
    def __init__(self, results=(), positions=None, orders=(), free=50.0, on_call=None):
        self.results, self.calls = list(results), []
        self.positions, self.orders, self.free, self.on_call = positions or {}, list(orders), free, on_call

    async def ioc(self, w, is_buy_up, price, size):
        self.calls.append(dict(is_buy_up=is_buy_up, price=price, size=size))
        if self.on_call:
            self.on_call()
        st, filled, px = self.results.pop(0)
        return dict(venue="lighter", status=st, filled=filled, avg_px=px, coid=len(self.calls), t_send=time.time(), t_ack=time.time(),
                    t_fill=time.time())

    async def balance(self):
        return dict(free=self.free, collateral=self.free, positions=self.positions)

    async def open_orders(self):
        return self.orders

    async def prepare(self, w):
        return dict(l_min=5.0)


class FakePM:
    def __init__(self, results=(), positions=(), orders=(), free=40.0):
        self.results, self.calls = list(results), []
        self.pos, self.orders, self.free = list(positions), list(orders), free

    async def fak_buy(self, token, price, size):
        self.calls.append(dict(token=token, price=price, size=size))
        st, sh, px = self.results.pop(0)
        return dict(venue="pm", status=st, shares=sh, cost=round(sh * px, 6), order_id=f"o{len(self.calls)}", t_send=time.time(),
                    t_ack=time.time())

    async def balance(self):
        return dict(free=self.free)

    async def open_orders(self):
        return self.orders

    async def positions(self):
        return self.pos

    async def prepare(self, w, cond, toks):
        return None

    async def fee_charged(self, oid):
        return []


def make_probe(tmp_path, lighter=None, pm=None, mode="live", red=None, stop_file=None):
    red = red or R.Redactor()
    pr = R.Probe(R.ProbeLog(tmp_path, red), mode, lighter=lighter or FakeL(), pm=pm or FakePM(),
                 stop_file=stop_file or (tmp_path / "STOP"))
    pr.free = {"lighter": 50.0, "pm": 40.0}
    pr.l_symbols_fn = lambda mids: {m: None for m in mids}  # no network in tests: unknown symbol (fail closed)
    return pr


def make_window(pr, e=1795606200, lbid=0.15, lask=0.19, pbid=0.25, pask=0.26):
    w = P.Window(e, 12572)
    w.up_token, w.down_token, w.slug, w.pm_rate = "UPTOK", "DOWNTOK", f"btc-updown-15m-{e}", 0.05
    set_book(w.lbook, lbid, lask)
    set_book(w.pbook, pbid, pask)
    pr.win[e], pr.by_mid[w.mid] = w, w
    pr.meta[e] = dict(symbol=R.l_symbol(e), l_min=5.0, pm_min_up=5.0, pm_min_down=5.0)
    return w


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- triggers identical to the paper trader
class FakeLoop:
    """Runs scheduled callbacks when the paper process actually ran them (a check record's t_local), else at now + d."""

    def __init__(self, ran_at=None):
        self.now, self.q, self.ran_at = 0.0, [], ran_at or {}

    def call_later(self, d, fn, *a):
        t = self.ran_at.get((a[1], a[5])) if fn.__name__ == "check" else None
        self.q.append((t or self.now + d, len(self.q), fn, a))


def test_replay_paper_log_fires_same_trigger_ids(tmp_path, monkeypatch):
    recs = [json.loads(line) for line in FIX.open()]
    wins = {r["e"]: r for r in recs if r["kind"] == "window"}
    trig = [r for r in recs if r["kind"] == "trigger"]
    checks = {(r["id"], r["at_ms"]): r for r in recs if r["kind"] == "check"}
    assert len(trig) > 100
    pr = make_probe(tmp_path, stop_file=tmp_path / "STOP")
    (tmp_path / "STOP").touch()  # kill switch on: the probe must still fire every trigger (and skip executing)
    loop = FakeLoop({k: c["t_local"] for k, c in checks.items()})
    monkeypatch.setattr(P.asyncio, "get_running_loop", lambda: loop)
    fired = []
    pr.log.hooks.append(lambda k, kw: fired.append(kw["id"]) if k == "trigger" else None)

    def drain(until):
        while loop.q:
            loop.q.sort()
            t, _, fn, a = loop.q[0]
            if t > until:
                return
            loop.q.pop(0)
            loop.now = t
            if fn.__name__ == "check":
                w, tid, side, lpx, t0, ms = a
                c = checks.get((tid, ms))
                if c is None or c["lighter_px"] is None:
                    w.lbook.clear()
                elif side == "a":
                    set_book(w.lbook, max(0.01, c["lighter_px"] - 0.05), c["lighter_px"], 100, c["lighter_size"])
                else:
                    set_book(w.lbook, c["lighter_px"], min(0.99, c["lighter_px"] + 0.05), c["lighter_size"], 100)
            fn(*a)

    for r in trig:
        ms = int(r["id"].rsplit("-", 1)[1])
        now = (ms + 0.5) / 1000
        drain(now)
        loop.now = now
        e = r["window"]
        if e not in pr.win:
            wr = wins[e]
            w = P.Window(e, wr["lighter_market"])
            w.up_token, w.down_token, w.slug = wr["up_token"], wr["down_token"], wr["slug"]
            pr.win[e] = w
            pr.meta[e] = dict(l_min=5.0, pm_min_up=5.0, pm_min_down=5.0)
        w = pr.win[e]
        w.pm_rate = r["pm_fee_rate"]
        lb, la, lbs, las = r["lighter_book"]
        pb, pa, pbs, pas = r["pm_book"]
        set_book(w.lbook, lb, la, lbs, las)
        set_book(w.pbook, pb, pa, pbs, pas)
        pr.evaluate(w, now, r["src"])
    drain(float("inf"))
    assert fired == [r["id"] for r in trig]
    # every displayed +500 ms check reached the probe's hook (here: skipped by the kill switch)
    disp = {k[0] for k, c in checks.items() if k[1] == 500 and c["displayed"]}
    skipped = {s["id"] for s in records(tmp_path, "skip")}
    assert skipped and skipped <= {r["id"] for r in trig}
    assert {s["reason"] for s in records(tmp_path, "skip")} <= {"kill_switch", "not_persistent", "stale_pm", "stale_lighter"}
    assert len(skipped) >= 0.9 * len(disp)


def test_probe_uses_paper_trigger_code_not_a_copy():
    assert R.Probe.evaluate is P.Paper.evaluate
    assert R.Probe.lquote_there is P.Paper.lquote_there
    assert R.Probe.discover is P.Paper.discover
    src = Path(R.__file__).read_text()
    assert "edge > TH" not in src and "def evaluate" not in src


# ---------------------------------------------------------------- sizing
@pytest.mark.parametrize("px,q", [(0.04, 25.0), (0.20, 5.0), (0.66, 5.0)])
def test_clip_size(px, q):
    assert R.clip_size(px, 5.0, 5.0) == q
    assert q * px >= R.PM_MIN_MARKETABLE_USD - 1e-9 or q == 5


def test_sizing_at_004_skips_on_leg_cost():
    # side a: PM Down at 0.04 means Lighter Up ~0.92 -> 25 x 0.92 = $23 > $10
    q, info, why = R.size_and_check("a", 0.92, 500, 0.04, 0.05, 5, 5, 0, 0, 50, 40)
    assert q is None and why == "lighter_leg_cost" and info["q"] == 25.0
    # side b: PM Up at 0.04, Lighter sells Up at 0.90 -> locks (1 - 0.90) x 25 = $2.5 + fee; PM 25 x 0.07 = $1.75
    q, info, why = R.size_and_check("b", 0.90, 500, 0.04, 0.05, 5, 5, 0, 0, 50, 40)
    assert why is None and q == 25.0 and info["lighter_cost"] == pytest.approx(25 * (0.10 + 0.01 * 0.9 * 0.1))
    assert info["pm_cost"] == pytest.approx(25 * R.pm_unit_cost(0.07, 0.05), abs=1e-4)


def test_sizing_at_020_and_066():
    q, info, why = R.size_and_check("a", 0.75, 500, 0.20, 0.05, 5, 5, 0, 0, 50, 40)
    assert (q, why) == (5.0, None)
    assert info["lighter_cost"] == pytest.approx(5 * (0.75 + 0.01 * 0.75 * 0.25), abs=1e-4)
    assert info["pm_cost"] == pytest.approx(5 * R.pm_unit_cost(0.23, 0.05), abs=1e-4)
    q, info, why = R.size_and_check("b", 0.71, 500, 0.66, 0.05, 5, 5, 0, 0, 50, 40)
    assert (q, why) == (5.0, None)
    assert info["lighter_cost"] == pytest.approx(5 * (0.29 + 0.01 * 0.71 * 0.29), abs=1e-4)  # a Lighter sell locks 1 - P


def test_sizing_skips():
    assert R.size_and_check("a", 0.5, 4.0, 0.4, 0.05, 5, 5, 0, 0, 50, 40)[2] == "lighter_size"
    assert R.size_and_check("a", 0.5, 500, 0.4, 0.05, 5, 5, 38.0, 0, 50, 40)[2] == "lighter_open_cap"
    assert R.size_and_check("a", 0.5, 500, 0.4, 0.05, 5, 5, 0, 28.5, 50, 40)[2] == "pm_open_cap"
    assert R.size_and_check("a", 0.5, 500, 0.4, 0.05, 5, 5, 0, 0, 2.0, 40)[2] == "lighter_balance"
    assert R.size_and_check("a", 0.5, 500, 0.4, 0.05, 5, 5, 0, 0, 50, 1.0)[2] == "pm_balance"
    assert R.size_and_check("a", 0.5, 500, 0.4, 0.05, 5, 5, 0, 0, None, 40)[2] == "lighter_balance"
    assert R.size_and_check("a", 0.5, 500, 0.4, 0.05, 7, 5, 0, 0, 50, 40)[0] == 7.0  # Lighter min binds


# ---------------------------------------------------------------- execution
def test_lighter_fill_then_hedge_success(tmp_path):
    L, PM = FakeL([("filled", 5.0, 0.19)]), FakePM([("filled", 5.0, 0.76)])
    pr = make_probe(tmp_path, L, PM)
    w = make_window(pr)
    run(pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert L.calls == [dict(is_buy_up=True, price=0.19, size=5.0)]
    assert PM.calls == [dict(token="DOWNTOK", price=0.78, size=5.0)]  # ask = 1 - 0.25, +3c
    pair = records(tmp_path, "pair")[0]
    exp = (1 - 0.19 - 0.76 - R.fl(0.19) - 0.05 * 0.76 * 0.24) * 5
    assert pair["pnl_usd"] == pytest.approx(exp, abs=1e-5) and pair["realised_usd"] == pair["pnl_usd"]
    assert not records(tmp_path, "unhedged") and pr.busy is None and pr.halted is None
    assert pr.ledger.open_cost("lighter") == pytest.approx(5 * R.lighter_unit_cost("a", 0.19))
    assert pr.ledger.expected() == ({w.mid: 5.0}, {"DOWNTOK": 5.0})


def test_lighter_no_fill_sends_no_hedge(tmp_path):
    L, PM = FakeL([("nofill", 0.0, None)]), FakePM()
    pr = make_probe(tmp_path, L, PM)
    pr.busy = "t1"
    run(pr.execute(make_window(pr), "t1", "b", 0.15, 5.0, "UPTOK", {}))
    assert PM.calls == [] and not records(tmp_path, "l_fill") and pr.busy is None


def test_lighter_partial_fill_hedges_the_fill(tmp_path):
    L, PM = FakeL([("filled", 7.0, 0.15)]), FakePM([("filled", 7.0, 0.27)])
    pr = make_probe(tmp_path, L, PM)
    run(pr.execute(make_window(pr), "t1", "b", 0.15, 10.0, "UPTOK", {}))
    assert L.calls[0]["is_buy_up"] is False and L.calls[0]["size"] == 10.0
    assert PM.calls == [dict(token="UPTOK", price=0.29, size=7.0)]
    assert records(tmp_path, "pair")[0]["qty"] == 7.0
    assert pr.ledger.expected()[0] == {12572: -7.0}  # sold Up = holds Down


def test_partial_fill_below_minimums_is_held_and_logged(tmp_path):
    L, PM = FakeL([("filled", 3.0, 0.19)]), FakePM()
    pr = make_probe(tmp_path, L, PM)
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert PM.calls == [] and len(L.calls) == 1  # 3 < PM min 5 and < Lighter min 5: neither hedge nor unwind possible
    u = records(tmp_path, "unhedged")[0]
    assert u["residual_held"] == 3.0 and u["why_residual"] == "below Lighter minimum"


def test_hedge_short_then_retry_completes(tmp_path):
    L = FakeL([("filled", 10.0, 0.19)])
    PM = FakePM([("filled", 4.0, 0.76), ("nofill", 0.0, 0), ("filled", 6.0, 0.77)])
    pr = make_probe(tmp_path, L, PM)
    w = make_window(pr)
    w.pbook.set(0, 0.25, 0)  # bid moved: fresh Down ask = 1 - 0.24 = 0.76
    w.pbook.set(0, 0.24, 300)
    run(pr.execute(w, "t1", "a", 0.19, 10.0, "DOWNTOK", {}))
    assert [c["size"] for c in PM.calls] == [10.0, 6.0, 6.0]
    assert all(c["price"] == 0.79 for c in PM.calls)
    assert records(tmp_path, "pair")[0]["qty"] == 10.0 and records(tmp_path, "pair")[0]["attempts"] == 3
    assert not records(tmp_path, "unhedged")


def test_hedge_fails_then_unwind(tmp_path):
    L = FakeL([("filled", 10.0, 0.19), ("filled", 10.0, 0.15)])
    PM = FakePM([("nofill", 0.0, 0)] * 4)
    pr = make_probe(tmp_path, L, PM)
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 10.0, "DOWNTOK", {}))
    assert len(PM.calls) == 1 + R.HEDGE_RETRIES
    assert L.calls[1] == dict(is_buy_up=False, price=0.10, size=10.0)  # sell Up back, bid 0.15 - 5c worst
    uw = records(tmp_path, "unwind")[0]
    assert uw["realised_usd"] == pytest.approx((0.15 - 0.19) * 10 - (R.fl(0.19) + R.fl(0.15)) * 10)
    u = records(tmp_path, "unhedged")[0]
    assert u["loss_usd"] == pytest.approx(-uw["realised_usd"]) and u["residual_held"] == 0
    assert pr.ledger.realised_today() == pytest.approx(uw["realised_usd"])
    assert pr.ledger.expected() == ({12572: 0.0}, {})
    assert pr.busy is None


def test_hedge_retries_stop_after_window(tmp_path, monkeypatch):
    L = FakeL([("filled", 10.0, 0.19), ("filled", 10.0, 0.15)])
    PM = FakePM([("nofill", 0.0, 0)] * 4)
    pr = make_probe(tmp_path, L, PM)
    clock = [1000.0]
    monkeypatch.setattr(R.time, "time", lambda: clock[0])
    orig = PM.fak_buy

    async def slow(*a):
        clock[0] += 1.5
        return await orig(*a)

    PM.fak_buy = slow
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 10.0, "DOWNTOK", {}))
    assert len(PM.calls) == 2  # 2nd attempt at +1.5 s, a 3rd would start at +3.0 s > 2 s


def test_pm_ambiguous_halts_and_blocks(tmp_path):
    L, PM = FakeL([("filled", 5.0, 0.19)]), FakePM([("ambiguous", 0.0, 0)])
    pr = make_probe(tmp_path, L, PM)
    pr.busy = "t1"
    w = make_window(pr)
    run(pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert pr.halted == "pm_ambiguous" and pr.busy == "t1" and len(L.calls) == 1  # no blind unwind
    pr.on_displayed(w, "t2", "a", 0.19)
    assert records(tmp_path, "skip")[-1]["reason"] == "halted"


def test_lighter_ambiguous_halts(tmp_path):
    pr = make_probe(tmp_path, FakeL([("ambiguous", 0.0, None)]), FakePM())
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert pr.halted == "lighter_ambiguous"


def test_one_execution_at_a_time(tmp_path):
    pr = make_probe(tmp_path)
    w = make_window(pr)
    pr.busy = "t0"
    pr.on_displayed(w, "t1", "a", 0.19)
    assert records(tmp_path, "skip")[0]["reason"] == "busy"


def test_on_displayed_starts_execution(tmp_path):
    L, PM = FakeL([("filled", 5.0, 0.19)]), FakePM([("filled", 5.0, 0.76)])
    pr = make_probe(tmp_path, L, PM)

    async def go():
        w = make_window(pr)
        pr.persist.update(t1=True, t2=True)
        pr.on_displayed(w, "t1", "a", 0.19)
        assert pr.busy == "t1"
        pr.on_displayed(w, "t2", "b", 0.15)  # second trigger while the first is open
        await asyncio.gather(*list(pr.tasks))

    run(go())
    assert [s["reason"] for s in records(tmp_path, "skip")] == ["busy"]
    assert len(records(tmp_path, "pair")) == 1 and pr.busy is None


# ---------------------------------------------------------------- limits
def test_loss_cap_stops_new_pairs_until_next_utc_day(tmp_path):
    pr = make_probe(tmp_path)
    w = make_window(pr)
    pr.log("unwind", id="x", window=w.e, side="a", mode="live", qty=0.0, realised_usd=-10.5)  # qty 0: P&L only
    pr.ledger.realised[R.today()] = -10.5
    pr.on_displayed(w, "t1", "a", 0.19)
    assert records(tmp_path, "skip")[0]["reason"] == "loss_cap"
    assert records(tmp_path, "loss_cap")[0]["realised_today"] == -10.5
    # survives a restart: rebuilt from the log
    pr.log("pair", id="y", window=w.e, side="a", mode="live", realised_usd=-10.5)
    pr2 = make_probe(tmp_path)
    pr2.ledger.replay(tmp_path)
    assert pr2.ledger.realised_today() == pytest.approx(-10.5)
    assert pr2.gate() == "loss_cap"
    # a new UTC day clears it
    pr2.ledger.realised = type(pr2.ledger.realised)(float, {"2000-01-01": -50.0})
    assert pr2.gate() is None


def test_kill_switch_blocks_new_but_finishes_hedge(tmp_path):
    stop = tmp_path / "STOP"
    L = FakeL([("filled", 5.0, 0.19)], on_call=stop.touch)  # STOP appears while the Lighter leg is in flight
    PM = FakePM([("filled", 5.0, 0.76)])
    pr = make_probe(tmp_path, L, PM, stop_file=stop)
    w = make_window(pr)
    run(pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert len(PM.calls) == 1 and records(tmp_path, "pair")  # hedge finished
    pr.on_displayed(w, "t2", "a", 0.19)
    assert records(tmp_path, "skip")[0]["reason"] == "kill_switch"


def test_open_cost_cap_from_ledger(tmp_path):
    pr = make_probe(tmp_path)
    w = make_window(pr, lask=0.60, pbid=0.45, pask=0.46)
    for i in range(10):  # Lighter 10 x $3.0 = $30 (+$3 < $40); PM 10 x $2.92 = $29.2 (+ $2.96 > $30)
        pr.log("l_fill", id=f"f{i}", window=w.e, side="a", mode="live", symbol=R.l_symbol(w.e), qty=5.0, px=0.60)
        pr.log("pm_fill", id=f"f{i}", window=w.e, side="a", mode="live", token="DOWNTOK", shares=5.0, cost=2.85, fee_usd=0.07)
    assert pr.ledger.open_cost("pm") == pytest.approx(10 * 2.92)
    pr.persist["t1"] = True
    pr.on_displayed(w, "t1", "a", 0.60)
    assert records(tmp_path, "skip")[0]["reason"] == "pm_open_cap"


def test_dry_records_never_enter_the_live_ledger(tmp_path):
    pr = make_probe(tmp_path, mode="dry")
    pr.log("l_fill", id="d", window=1, side="a", mode="dry", symbol=R.l_symbol(1), market_id=7, qty=5.0, px=0.5)
    live = R.Ledger("live").replay(tmp_path)
    assert live.expected() == ({}, {}) and R.Ledger("dry").replay(tmp_path).expected()[0] == {7: 5.0}


# ---------------------------------------------------------------- restart / reconcile
def test_restart_with_foreign_position_refuses(tmp_path):
    L = FakeL(positions={12572: 12.0})
    pr = make_probe(tmp_path, L, FakePM())
    run(pr.startup_reconcile())
    assert pr.reconcile_failed and records(tmp_path, "reconcile_fail")
    pr.on_displayed(make_window(pr), "t1", "a", 0.19)
    assert records(tmp_path, "skip")[0]["reason"] == "reconcile_fail"


def test_restart_with_foreign_pm_position_or_order_refuses(tmp_path):
    pr = make_probe(tmp_path, FakeL(), FakePM(positions=[dict(token="999", size=3.0)]))
    run(pr.startup_reconcile())
    assert pr.reconcile_failed
    pr2 = make_probe(tmp_path / "b", FakeL(orders=[{"order_index": 1}]), FakePM())
    run(pr2.startup_reconcile())
    assert pr2.reconcile_failed


def test_restart_with_own_positions_passes(tmp_path):
    e = 1795606200
    first = make_probe(tmp_path)
    first.log("l_fill", id="i", window=e, side="b", mode="live", symbol=R.l_symbol(e), market_id=12572, qty=5.0, px=0.15)
    first.log("pm_fill", id="i", window=e, side="b", mode="live", token="UPTOK", shares=5.0, cost=1.35, fee_usd=0.05)
    # a settled earlier window whose PM winner is still unredeemed is also ours
    first.log("pm_fill", id="j", window=e - 900, side="a", mode="live", token="OLDTOK", shares=5.0, cost=3.0, fee_usd=0.05)
    first.log("probe_settle", window=e - 900, mode="live", realised_usd=0.0)
    L = FakeL(positions={12572: -5.0})
    PM = FakePM(positions=[dict(token="UPTOK", size=5.0), dict(token="OLDTOK", size=5.0, redeemable=True)])
    pr = make_probe(tmp_path, L, PM)
    pr.ledger.replay(tmp_path)
    run(pr.startup_reconcile())
    assert pr.reconcile_failed is None and records(tmp_path, "reconcile")[-1]["ok"] is True


def test_settlement_realises_and_logs_redeem_due(tmp_path):
    L, PM = FakeL([("filled", 5.0, 0.19)]), FakePM([("filled", 5.0, 0.76)])
    pr = make_probe(tmp_path, L, PM, mode="dry")
    w = make_window(pr)
    run(pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    locked = records(tmp_path, "pair")[0]["pnl_usd"]
    run(pr.probe_settle(w, {"pm_winner": "DOWN", "lighter_outcome": ["1", "settled"]}))
    s = records(tmp_path, "probe_settle")[0]
    assert s["pnl_usd"] == pytest.approx(locked, abs=1e-5) and abs(s["realised_usd"]) < 1e-5
    assert records(tmp_path, "redeem_due")[0]["winners"][0] == dict(token="DOWNTOK", shares=5.0, slug=w.slug)
    assert pr.ledger.expected() == ({}, {})


# ---------------------------------------------------------------- secrets
def test_dry_mode_never_touches_ssm(tmp_path):
    code = f"""
import sys, asyncio
sys.modules['boto3'] = None          # any `import boto3` now raises
import lp_arb.probe as R
def boom(*a, **k): raise AssertionError('SSM called in dry mode')
R.load_secrets = boom
R.build_live = boom
R.LighterLive = R.PMLive = boom
R.main(['--mode', 'dry', '--out', {str(tmp_path)!r}, '--minutes', '0.02'])
assert 'boto3' not in sys.modules or sys.modules['boto3'] is None
assert 'lighter' not in sys.modules and 'py_clob_client_v2' not in sys.modules
print('DRY_OK')
"""
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert "DRY_OK" in r.stdout, r.stderr[-2000:]
    starts = records(tmp_path, "probe_start")
    assert starts and starts[0]["mode"] == "dry"


def test_live_secrets_loaded_by_name_only():
    class FakeSSM:
        def get_parameters(self, Names, WithDecryption):
            assert WithDecryption and set(Names) == set(R.SSM.values())
            return {"Parameters": [{"Name": n, "Value": f"v{i}"} for i, n in enumerate(Names)], "InvalidParameters": []}

    sec = R.load_secrets(FakeSSM())
    assert set(sec) == set(R.SSM)

    class Missing(FakeSSM):
        def get_parameters(self, Names, WithDecryption):
            return {"Parameters": [], "InvalidParameters": [Names[0]]}

    with pytest.raises(RuntimeError, match="missing SSM parameters"):
        R.load_secrets(Missing())


def test_redaction_planted_key_never_reaches_logs(tmp_path, capsys):
    red = R.Redactor()
    red.add(FAKE_KEY)
    red.add("761897")  # account index
    red.add("2")  # signature type: too short to redact safely, deliberately ignored
    L = FakeL([("filled", 5.0, 0.19)])

    class LeakyPM(FakePM):
        async def fak_buy(self, token, price, size):
            raise RuntimeError(f"signer blew up with key {FAKE_KEY} / {FAKE_KEY[2:].upper()}")

    pr = make_probe(tmp_path, L, LeakyPM(), red=red)
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 5.0, "DOWNTOK", {}))  # crash path -> error + halt records
    pr.log("error", where="t", err=f"x{FAKE_KEY}", nested={"a": [FAKE_KEY[2:]], "signature": "0x" + "ab" * 65},
           acct=761897, s="account 761897 ts 17958977618971", blob="0x" + "1f" * 65)
    pr.err("t2", ValueError(FAKE_KEY))
    sys.stderr = R.ScrubStream(sys.__stderr__, red)
    try:
        print("stderr leak " + FAKE_KEY, file=sys.stderr)
    finally:
        sys.stderr = sys.__stderr__
    text = "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))
    h = FAKE_KEY[2:]
    assert h.lower() not in text.lower()
    assert "[REDACTED]" in text and "[SIG]" in text
    assert '"signature"' not in text
    assert "17958977618971" in text  # timestamps that contain the account index survive
    assert "761897" not in text.replace("17958977618971", "")  # the account index itself is gone (string and number)
    assert pr.halted == "execute_crash"


def test_redactor_scrub_stream(capsys):
    red = R.Redactor()
    red.add(FAKE_KEY)
    s = R.ScrubStream(sys.stdout, red)
    s.write(f"k={FAKE_KEY}\n")
    out = capsys.readouterr().out
    assert FAKE_KEY not in out and "[REDACTED]" in out


def test_live_refuses_when_reused_code_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "STATARB_DIR", tmp_path)
    bad = R.verify_pins()
    assert any("missing" in b for b in bad)
    monkeypatch.setattr(R, "load_secrets", lambda: pytest.fail("secrets loaded before the pin check"))
    with pytest.raises(SystemExit, match="reused code changed"):
        R.build_live(R.Redactor())


def test_hedge_with_unknown_pm_book_first_then_retry(tmp_path):
    L, PM = FakeL([("filled", 5.0, 0.19)]), FakePM([("filled", 5.0, 0.76)])
    pr = make_probe(tmp_path, L, PM)
    w = make_window(pr)
    w.pbook.clear()
    orig = asyncio.sleep

    async def refill(d):  # the book comes back while the probe waits
        set_book(w.pbook, 0.25, 0.26)
        await orig(0)

    R.asyncio.sleep, saved = refill, R.asyncio.sleep
    try:
        run(pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    finally:
        R.asyncio.sleep = saved
    assert pr.halted is None and len(PM.calls) == 1 and records(tmp_path, "pair")
    assert records(tmp_path, "pm_order")[0]["status"] == "skipped"


def test_stale_window_settles_after_restart(tmp_path, monkeypatch):
    e = 1795606200
    first = make_probe(tmp_path)
    first.log("l_fill", id="i", window=e, side="a", mode="live", symbol=R.l_symbol(e), market_id=12572, qty=5.0, px=0.19)
    first.log("pm_fill", id="i", window=e, side="a", mode="live", token="DOWNTOK", shares=5.0, cost=3.8, fee_usd=0.05)
    first.log("pair", id="i", window=e, side="a", mode="live", realised_usd=0.1)
    pr = make_probe(tmp_path)
    pr.ledger.replay(tmp_path)
    assert pr.ledger.open_cost("pm") > 0

    def fake_outcome(ee, mid):
        assert (ee, mid) == (e, 12572)
        w = P.Window(ee, mid)
        w.up_token, w.down_token, w.slug = "UPTOK", "DOWNTOK", "s"
        return dict(pm_winner="DOWN", lighter_outcome=["1", "settled"]), w

    monkeypatch.setattr(R, "stale_outcome", fake_outcome)

    async def go():
        t = asyncio.get_running_loop().create_task(pr.settle_stale())
        await asyncio.sleep(0.05)
        pr.stop.set()
        await t

    run(go())
    assert pr.ledger.open_cost("pm") == 0 and pr.ledger.open_cost("lighter") == 0
    assert records(tmp_path, "probe_settle")[0]["window"] == e and records(tmp_path, "redeem_due")


def test_reconcile_size_mismatch_and_perp_scope(tmp_path):
    led = R.Ledger("live")
    led.apply("l_fill", dict(mode="live", window=1, side="a", symbol="S", market_id=5, qty=5.0, px=0.4, t_local=time.time()))
    assert R.reconcile(led, {5: 5.0}, [], [], [])[0] == []
    assert R.reconcile(led, {5: 9.0}, [], [], [])[0]  # more than the probe bought, in a probe market
    pr, wr = R.reconcile(led, {5: 5.0, "perp:BTC": 0.1}, [], [], [])  # a perp is another market: warning only
    assert pr == [] and "perp:BTC" in wr[0] and "not blocking" in wr[0]


def test_lighter_snapshot_reads_bo_positions():
    """/account bo_positions (2026-11-25 sample): lighter-sdk 1.1.8 models the field, 1.1.2 left it in
    additional_properties. The probe reads both, so an SDK upgrade cannot silently empty the reconcile."""
    import importlib.util
    import types

    if importlib.util.find_spec("lighter") is None:
        pytest.skip("lighter SDK not installed (probe-venv only)")
    from lighter.models.detailed_account import DetailedAccount

    raw = {"code": 200, "account_type": 0, "index": 1, "l1_address": "0x0", "cancel_all_time": 0, "total_order_count": 0,
           "total_isolated_order_count": 0, "pending_order_count": 0, "available_balance": "41.5", "status": 1, "collateral": "50.0",
           "account_index": 1, "name": "", "description": "", "can_invite": False, "referral_points_percentage": "0",
           "positions": [], "assets": [], "total_asset_value": "50", "cross_asset_value": "50", "pool_info": None, "shares": [],
           "bo_positions": [{"market_id": 12694, "entry_quote": "6.400000", "avg_entry_price": "0.64", "sign": -1, "size": "10.0000"},
                            {"market_id": 12695, "entry_quote": "2.0", "avg_entry_price": "0.40", "sign": 1, "size": "5.0000"}]}
    a = DetailedAccount.from_dict(raw)  # the installed SDK (1.1.8)
    assert a.bo_positions and not (a.additional_properties or {}).get("bo_positions")  # modelled now
    assert R.account_positions(a) == {12694: -10.0, 12695: 5.0}
    old = types.SimpleNamespace(positions=[], additional_properties={"bo_positions": raw["bo_positions"]})  # 1.1.2 shape
    assert R.account_positions(old) == {12694: -10.0, 12695: 5.0}
    perp = types.SimpleNamespace(position="0.1", sign=-1, symbol="BTC")
    assert R.account_positions(types.SimpleNamespace(positions=[perp], bo_positions=None)) == {"perp:BTC": -0.1}


# ---------------------------------------------------------------- +250 ms persistence
class StepLoop(FakeLoop):
    """FakeLoop that can also hold spawned executions (create_task) without running them."""

    def __init__(self):
        super().__init__()
        self.spawned = []

    def create_task(self, coro):
        self.spawned.append(coro)

        class T:
            def add_done_callback(self, fn):
                pass
        return T()

    def run_until(self, until, before=None):
        while self.q:
            self.q.sort(key=lambda z: z[0])
            t, _, fn, a = self.q[0]
            if t > until:
                return
            self.q.pop(0)
            self.now = t
            if before:
                before(t, fn)
            fn(*a)


def stamp(book, now, age_ms, lag_ms):
    """Book last updated age_ms before `now` (exchange time), received lag_ms after its exchange timestamp."""
    book.x = int(now * 1000 - age_ms)
    book.t = (book.x + lag_ms) / 1000


def fresh(w, now):
    stamp(w.pbook, now, 120, 110)  # PM: ~110 ms feed lag (Tokyo), just updated
    stamp(w.lbook, now, 5, 3)


def trigger_then(tmp_path, monkeypatch, at_250, at_0=fresh):
    """Side a trigger (edge 4.9c on make_window's books); `at_0(w, now)` stamps the books at the trigger, `at_250(w)`
    changes them just before the +250 ms check (default: both books updated again, fresh)."""
    pr = make_probe(tmp_path)
    w = make_window(pr)
    pr.win[w.e] = w
    loop = StepLoop()
    monkeypatch.setattr(P.asyncio, "get_running_loop", lambda: loop)
    pr.clock = lambda: loop.now
    t0 = w.e + 100.0
    loop.now = t0
    at_0(w, t0)
    pr.evaluate(w, t0, "lighter")
    trig = records(tmp_path, "trigger")
    assert len(trig) == 1 and trig[0]["side"] == "a" and trig[0]["edge"] > 0.04
    def before(t, fn):
        if fn.__name__ == "persist_check":
            fresh(w, t)
            at_250(w)
    loop.run_until(t0 + 2, before=before)
    for c in loop.spawned:
        c.close()
    return pr, w, trig[0]["id"], loop


def test_persistence_edge_gone_at_250ms_skips_not_persistent(tmp_path, monkeypatch):
    # PM Up bid drops 0.25 -> 0.24: the Down hedge costs 0.76, edge 1 - (0.19 + 0.76 + fees) = 3.9c < 4c.
    # The Lighter ask stays displayed, so the paper's +500 ms check still says "filled"; the probe must not trade.
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, lambda w: set_book(w.pbook, 0.24, 0.26))
    (p,) = records(tmp_path, "persist")
    assert p["id"] == tid and p["at_ms"] == 250 and p["ok"] is False and p["edge"] < 0.04
    (s,) = records(tmp_path, "skip")
    assert s["id"] == tid and s["reason"] == "not_persistent" and s["edge"] == p["edge"] and s["min_edge"] == 0.04
    assert [c["displayed"] for c in records(tmp_path, "check") if c["at_ms"] == 500] == [True]  # paper path unchanged
    assert not loop.spawned and pr.busy is None and not pr.lv.calls and tid not in pr.persist


def test_persistence_edge_kept_at_250ms_executes(tmp_path, monkeypatch):
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, lambda w: None)
    (p,) = records(tmp_path, "persist")
    assert p["ok"] is True and p["edge"] >= 0.04 and p["pm_book"][0] == 0.25
    assert not records(tmp_path, "skip")
    assert len(loop.spawned) == 1 and pr.busy == tid and pr.counts["exec"] == 1


def test_persistence_invalid_book_at_250ms_skips(tmp_path, monkeypatch):
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, lambda w: w.pbook.clear())
    assert records(tmp_path, "persist")[0]["edge"] is None
    assert [s["reason"] for s in records(tmp_path, "skip")] == ["not_persistent"] and not loop.spawned


def test_persistence_edge_uses_trigger_formula(tmp_path):
    pr = make_probe(tmp_path)
    for lb, la, pb, pa in ((0.15, 0.19, 0.25, 0.26), (0.61, 0.63, 0.52, 0.55), (0.14, 0.16, 0.08, 0.09)):
        w = make_window(pr, lbid=lb, lask=la, pbid=pb, pask=pa)
        ea = 1 - (la + (1 - pb) + P.fl(la) + 0.05 * (1 - pb) * pb)
        eb = 1 - ((1 - lb) + pa + P.fl(lb) + 0.05 * pa * (1 - pa))
        assert pr.pair_edge(w, "a")[0] == pytest.approx(ea, abs=1e-12)
        assert pr.pair_edge(w, "b")[0] == pytest.approx(eb, abs=1e-12)


def test_no_persistence_verdict_fails_closed(tmp_path):
    pr = make_probe(tmp_path)
    pr.on_displayed(make_window(pr), "t9", "a", 0.19)
    assert [s["reason"] for s in records(tmp_path, "skip")] == ["persist_unchecked"] and pr.busy is None


# ---------------------------------------------------------------- --mode check
EXV2 = "0xE111180000d2663C0091e4f400237545B87B996B"
PM_SPENDER = R.PMLive.spender  # kept: one test replaces R.PMLive


class CheckL(FakeL):
    def __init__(self, status="active", **kw):
        super().__init__(**kw)
        self.status, self.registered = status, []

    async def ioc(self, *a, **k):
        raise AssertionError("check mode placed a Lighter order")

    async def account_info(self):
        return dict(account_type=0, account_trading_mode=0, status=1, user_tier="std", user_tier_name="Standard")

    async def sign_check(self, mid, size=5, price=0.5, is_ask=False):
        self.signed = getattr(self, "signed", []) + [mid]
        return getattr(self, "sign_result", (True, None))

    async def market_info(self, mid):
        self.registered.append(mid)
        return dict(symbol="BTC-UPDOWN-15M-1795700700", l_min=5.0, l_size_dec=4, l_px_dec=2)


class CheckPM(FakePM):
    server_version = 2

    def __init__(self, allowance="115792089237316195423570985008687907853269984665640564039457584007913129639935", closed=False, **kw):
        super().__init__(**kw)
        self.al, self.closed = allowance, closed

    async def fak_buy(self, *a, **k):
        raise AssertionError("check mode placed a Polymarket order")

    async def allowance(self):
        return dict(balance="40000000", allowances={EXV2: self.al, "0xC5d563A36AE78145C45a50134d48A1215220f80a": "0"})

    async def closed_only(self):
        return dict(closed_only=self.closed)

    def spender(self, version, neg_risk=False):
        return PM_SPENDER(types_ns(), version, neg_risk)


def types_ns():
    import types
    return types.SimpleNamespace(c=types.SimpleNamespace(chain_id=137))


def fake_http(status="active"):
    def http(url):
        if "events?" in url:
            return {"events": [{"order_books": [{"symbol": "BTC-UPDOWN-15M-1795700700", "market_id": 13001}]},
                               {"order_books": [{"symbol": "BTC-UPDOWN-15M-1795699800", "market_id": 12999}]},  # already ended
                               {"order_books": [{"symbol": "ETH-UPDOWN-15M-1795700700", "market_id": 13002}]}]}
        if "orderBooks?market_id=13001" in url:
            return {"order_books": [{"symbol": "BTC-UPDOWN-15M-1795700700", "market_id": 13001, "status": status,
                                     "market_type": "binary", "min_base_amount": "5.0000", "taker_fee": "0.0000"}]}
        raise AssertionError(url)
    return http


def test_pm_spender_follows_order_builder():
    assert R.PMLive.spender(types_ns(), 2) == ("exchange_v2", EXV2)
    assert R.PMLive.spender(types_ns(), 3)[0] == "exchange_v3"
    assert R.PMLive.spender(types_ns(), 2, neg_risk=True)[0] == "neg_risk_exchange_v2"


def test_check_mode_reads_everything_places_nothing(tmp_path):
    L, PM = CheckL(), CheckPM()
    res = run(R.account_check(L, PM, R.Ledger("live"), http=fake_http(), now=1795700700 + 100))
    assert res["ok"], res["problems"]
    t = res["lighter_btc15m"]
    assert (t["window"], t["market_id"], t["verdict"]) == (1795700700, 13001, "yes_by_api") and L.registered == [13001]
    assert all(t["checks"].values()) and "first live order" in t["note"]
    assert res["lighter"]["account"]["user_tier_name"] == "Standard" and res["lighter"]["balance"] == 50.0
    assert res["pm"]["allowance"] == dict(spender="exchange_v2", address=EXV2, allowance_usd=None, unlimited=True, ok=True)
    assert res["pm"]["balance"] == 40.0 and res["pm"]["closed_only"] is False
    assert res["pm_client"]["required"] == "1.2.2" and res["pm_client"]["clob_server_version"] == 2
    assert res["reconcile"]["ok"] and res["placed_orders"] == 0 == res["cancelled_orders"]
    assert not L.calls and not PM.calls


def test_check_mode_flags_every_problem(tmp_path):
    L = CheckL(positions={555: 5.0}, orders=[{"market_index": 555, "is_ask": True, "price": "0.5"}], free=3.0)
    PM = CheckPM(allowance="12000000", closed=True, orders=[{"market": "0xabc", "side": "BUY"}],
                 positions=[{"token": "999", "size": 7.0, "slug": "btc-updown-15m-1", "redeemable": False}])
    res = run(R.account_check(L, PM, R.Ledger("live"), http=fake_http(status="inactive"), now=1795700700 + 100))
    assert not res["ok"] and res["lighter_btc15m"]["verdict"] == "no"
    p = " | ".join(res["problems"])
    for want in ("market_active", "balance_for_one_clip", "allowance to exchange_v2 is $12.00", "closed-only", "reconcile:"):
        assert want in p, (want, p)
    assert res["pm"]["allowance"]["allowance_usd"] == 12.0 and res["reconcile"]["ok"] is False
    assert not L.calls and not PM.calls


def test_check_mode_survives_a_venue_error():
    class Broken(CheckPM):
        async def allowance(self):
            raise RuntimeError("503 from CLOB")

    res = run(R.account_check(CheckL(), Broken(), R.Ledger("live"), http=fake_http(), now=1795700700 + 100))
    assert not res["ok"] and any("pm allowance" in x for x in res["problems"]) and res["lighter"]["account"]


def test_check_mode_main_loads_like_live_and_never_prints_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # build_live wraps stdout/stderr; restore after the test
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    loaded = []

    def secrets(client=None):
        loaded.append(1)
        return {k: (FAKE_KEY if "KEY" in k else f"secret-{k.lower()}-value") for k in R.SSM}

    class LeakyL(CheckL):
        async def account_info(self):  # a venue echoing the key back must still not reach stdout
            return dict(account_type=0, name=f"acct {FAKE_KEY}", user_tier_name="Standard")

    built = {}
    monkeypatch.setattr(R, "verify_pins", lambda: [])
    monkeypatch.setattr(R, "load_secrets", secrets)
    monkeypatch.setattr(R, "LighterLive", lambda sec: built.setdefault("l", (LeakyL(), dict(sec)))[0])
    monkeypatch.setattr(R, "PMLive", lambda sec, red: built.setdefault("p", (CheckPM(), dict(sec)))[0])
    monkeypatch.setattr(R, "OUT_LIVE", tmp_path)
    monkeypatch.setattr(P, "http_json", fake_http())
    monkeypatch.setattr(R.time, "time", lambda: 1795700700 + 100.0)
    rc = R.main(["--mode", "check"])
    out = capsys.readouterr()
    assert loaded == [1] and built["l"][1]["L_API_KEY_PRIVATE_KEY"] == FAKE_KEY  # the same SSM load as live
    res = json.loads(out.out)
    assert rc == 0 and res["ok"] and res["lighter"]["account"]["name"].startswith("acct ")
    for s in (FAKE_KEY, FAKE_KEY[2:], "secret-pm_funder-value"):
        assert s not in out.out and s not in out.err
    assert not list(tmp_path.glob("*.jsonl"))  # check writes no log


def test_check_mode_refuses_when_reused_code_changed(monkeypatch):
    monkeypatch.setattr(R, "verify_pins", lambda: ["rd3_live.py: aaaa != pinned bbbb"])
    monkeypatch.setattr(R, "load_secrets", lambda client=None: (_ for _ in ()).throw(AssertionError("secrets read")))
    with pytest.raises(SystemExit, match="reused code changed"):
        R.main(["--mode", "check"])


# ---------------------------------------------------------------- stale books
@pytest.mark.parametrize("when,venue,age,lag,reason", [
    ("trigger", "pm", 450, 110, "stale_pm"),      # PM book not updated for 450 ms (exchange time)
    ("trigger", "pm", 320, 310, "stale_pm"),      # PM message arrived 310 ms after its exchange timestamp
    ("trigger", "lighter", 150, 3, "stale_lighter"),
    ("trigger", "lighter", 120, 110, "stale_lighter"),
    ("at_250", "pm", 400, 110, "stale_pm"),
    ("at_250", "lighter", 101, 3, "stale_lighter"),
])
@pytest.mark.parametrize("guard", ["log", "skip"])
def test_stale_book(tmp_path, monkeypatch, when, venue, age, lag, reason, guard):
    """Ages and the stale reason are always logged; only STALE_GUARD = "skip" turns them into a skip."""
    monkeypatch.setattr(R, "STALE_GUARD", guard)
    book = (lambda w: w.pbook) if venue == "pm" else (lambda w: w.lbook)

    def at_0(w, now):
        fresh(w, now)
        if when == "trigger":
            stamp(book(w), now, age, lag)

    holder = {}

    def at_250(w):
        if when == "at_250":
            stamp(book(w), holder["loop"].now, age, lag)

    orig = StepLoop.__init__

    def init(self):
        orig(self)
        holder["loop"] = self
    monkeypatch.setattr(StepLoop, "__init__", init)
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, at_250, at_0)
    (p,) = records(tmp_path, "persist")
    assert p["stale"] == reason and p["ok"] is True and p["stale_guard"] == guard  # edge still >= 4c
    a = p["ages"]["trigger" if when == "trigger" else "at_250"][venue]
    assert a["age_ms"] == pytest.approx(age, abs=1.5) and a["lag_ms"] == pytest.approx(lag, abs=1.5)
    if guard == "skip":
        (s,) = records(tmp_path, "skip")
        assert s["reason"] == reason and s["at"] == when and s["limits_ms"] == {"pm": 300, "lighter": 100}
        assert not loop.spawned and pr.busy is None
    else:  # log only: the trigger goes on to execute
        assert not records(tmp_path, "skip") and len(loop.spawned) == 1 and pr.busy == tid


def test_fresh_books_at_the_limits_pass_and_ages_logged(tmp_path, monkeypatch):
    def at_0(w, now):
        stamp(w.pbook, now, 300, 300)
        stamp(w.lbook, now, 100, 100)
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, lambda w: None, at_0)
    (p,) = records(tmp_path, "persist")
    assert p["stale"] is None and p["ok"] and set(p["ages"]) == {"trigger", "at_250"}
    assert p["ages"]["trigger"]["pm"]["age_ms"] == pytest.approx(300, abs=1.5)
    assert len(loop.spawned) == 1


def test_stale_guard_defaults_to_log():
    assert R.STALE_GUARD == "log"


def test_log_mode_keeps_the_persistence_filter(tmp_path, monkeypatch):
    def at_0(w, now):
        fresh(w, now)
        stamp(w.pbook, now, 900, 110)  # stale PM: logged, not enforced
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, lambda w: set_book(w.pbook, 0.24, 0.26), at_0)  # edge 3.9c at +250
    (p,) = records(tmp_path, "persist")
    assert p["stale"] == "stale_pm" and p["ok"] is False
    assert [s["reason"] for s in records(tmp_path, "skip")] == ["not_persistent"] and not loop.spawned


def test_book_without_exchange_timestamp_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "STALE_GUARD", "skip")

    def at_0(w, now):
        fresh(w, now)
        w.pbook.x = 0
    pr, w, tid, loop = trigger_then(tmp_path, monkeypatch, lambda w: None, at_0)
    assert records(tmp_path, "skip")[0]["reason"] == "stale_pm" and not loop.spawned


# ---------------------------------------------------------------- reconcile scope (11-26 operator check)
def test_reconcile_scope_btc15m_blocks_other_markets_warn():
    led = R.Ledger("live")
    led.apply("pm_fill", dict(mode="live", window=1, side="a", token="OURS", shares=5.0, cost=2.0, t_local=time.time()))
    pm = [dict(token="OURS", size=5.0, slug="btc-updown-15m-1"),
          dict(token="T1", size=3.0, slug="btc-updown-15m-1795700700", outcome="Up"),  # foreign BTC 15m -> refuse
          dict(token="T2", size=40.0, slug="will-it-rain-in-tokyo", outcome="Yes"),  # other market -> warn
          dict(token="T3", size=2.0, slug="eth-updown-15m-1795700700"),  # ETH 15m is another market -> warn
          dict(token="T4", size=1.0, slug=""),  # unknown market -> refuse (fail closed)
          dict(token="T5", size=0.0, slug="btc-updown-15m-1")]  # zero size ignored
    lpos = {77: 5.0, 78: -5.0, 79: 1.0}
    syms = {77: "BTC-UPDOWN-15M-1795700700", 78: "ETH-UPDOWN-15M-1795700700", 79: None}
    probs, warns = R.reconcile(led, lpos, [], pm, [], l_symbols=syms)
    assert len(probs) == 4 and len(warns) == 3
    joined = " | ".join(probs)
    for x in ("btc-updown-15m-1795700700", "T4", "77", "79"):
        assert x in joined
    assert all("not blocking" in w for w in warns)
    assert any("will-it-rain" in w for w in warns) and any("ETH-UPDOWN" in w for w in warns)


def test_startup_reconcile_other_market_positions_do_not_block(tmp_path):
    pr = make_probe(tmp_path, FakeL(positions={91: 2.0}), FakePM(positions=[dict(token="X", size=9.0, slug="us-election-2028")]))
    pr.l_symbols_fn = lambda mids: {m: "ETH-UPDOWN-15M-1" for m in mids}
    run(pr.startup_reconcile())
    rec = records(tmp_path, "reconcile")[-1]
    assert pr.reconcile_failed is None and rec["ok"] is True and len(rec["warnings"]) == 2


# ---------------------------------------------------------------- Data API v2 positions
def v2_pages(pages, reject_zero=False):
    calls = []

    def get(url):
        import urllib.error
        import urllib.parse
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        calls.append((url.split("?")[0], q))
        if reject_zero and q.get("filter_amount") == "0":
            import io
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(
                b'{"error":"filter_amount must be > 0","code":"invalid_request","parameter":"filter_amount"}'))
        if q.get("user") == "0xprotocol":
            import io
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(
                b'{"error":"user is a protocol contract address","code":"invalid_request","parameter":"user"}'))
        i = 0 if "cursor" not in q else int(q["cursor"][1:])
        return pages[i]
    return get, calls


def row(tok, size, slug="btc-updown-15m-1", redeemable=False):
    return dict(proxy_wallet="0xfunder", token_id=tok, condition_id="0xc", current_size=size, slug=slug, title="t", outcome="Up",
                status="OPEN", current_value=size * 0.5, redeemable=redeemable)


def test_pm_positions_v2_paginates_with_user_anchor_and_keeps_dust():
    pages = [dict(data=[row("a", 5.0), row("b", 0.04)], pagination=dict(has_more=True, next_cursor="c1")),
             dict(data=[row("c", 1.0, redeemable=True)], pagination=dict(has_more=True, next_cursor="c2")),
             dict(data=[], pagination=dict(has_more=False, next_cursor=None))]
    get, calls = v2_pages(pages)
    rows, notes = R.pm_positions_v2("0xfunder", get)
    assert [r["token"] for r in rows] == ["a", "b", "c"] and rows[1]["size"] == 0.04 and rows[2]["redeemable"] is True
    assert rows[0] == dict(token="a", condition="0xc", size=5.0, slug="btc-updown-15m-1", title="t", outcome="Up", redeemable=False,
                           value=2.5, status="OPEN")
    assert all(u == "https://data-api.polymarket.com/v2/positions" for u, _ in calls) and len(calls) == 3
    assert all(q["user"] == "0xfunder" and q["filter_type"] == "TOKENS" and q["filter_amount"] == "0" for _, q in calls)
    assert "cursor" not in calls[0][1] and calls[1][1]["cursor"] == "c1" and calls[2][1]["cursor"] == "c2"
    assert notes == []


def test_pm_positions_v2_falls_back_without_filter_and_says_so():
    get, calls = v2_pages([dict(data=[row("a", 5.0)], pagination=dict(has_more=False, next_cursor=None))], reject_zero=True)
    rows, notes = R.pm_positions_v2("0xfunder", get)
    assert len(rows) == 1 and "filter_amount" not in calls[-1][1] and "dust" in notes[0]


def test_pm_positions_v2_other_400_is_reported_not_retried():
    get, calls = v2_pages([])
    with pytest.raises(RuntimeError, match="HTTP 400: user is a protocol contract address"):
        R.pm_positions_v2("0xprotocol", get)
    assert len(calls) == 1


def test_pm_positions_v2_rejects_broken_pagination():
    get, _ = v2_pages([dict(data=[row("a", 1.0)], pagination=dict(has_more=True, next_cursor=None))])
    with pytest.raises(RuntimeError, match="next_cursor"):
        R.pm_positions_v2("0xfunder", get)
    loop_pages = [dict(data=[], pagination=dict(has_more=True, next_cursor="c0"))]
    get, _ = v2_pages(loop_pages)
    with pytest.raises(RuntimeError, match="next_cursor"):
        R.pm_positions_v2("0xfunder", get)


def test_redeem_sweep_uses_v2_rows(tmp_path):
    PM = FakePM(positions=[dict(token="W", size=5.0, value=5.0, redeemable=True, slug="btc-updown-15m-1", title="t"),
                           dict(token="L", size=5.0, value=0.0, redeemable=True, slug="btc-updown-15m-1")])
    pr = make_probe(tmp_path, pm=PM)
    run(pr.redeem_sweep(make_window(pr)))
    (r,) = records(tmp_path, "redeem_due")
    assert [w["token"] for w in r["winners"]] == ["W"] and r["usd"] == 5.0


# ---------------------------------------------------------------- Polymarket API creds: derive first
class FakeClob:
    def __init__(self, derive_ok=True):
        self.calls, self.derive_ok = [], derive_ok

    def derive_api_key(self, nonce=None):
        self.calls.append("derive")
        if not self.derive_ok:
            raise RuntimeError("400 no key")
        return types_ns_creds("k-derived")

    def create_api_key(self, nonce=None):
        self.calls.append("create")
        return types_ns_creds("k-created")

    def create_or_derive_api_key(self, nonce=None):  # the library's order
        self.calls.append("library")
        return self.create_api_key(nonce)


def types_ns_creds(k):
    import types
    return types.SimpleNamespace(api_key=k, api_secret="s", api_passphrase="p")


def test_derive_first_derives_and_never_creates_when_a_key_exists():
    c = FakeClob()
    with R.derive_first(FakeClob):
        cr = c.create_or_derive_api_key()
    assert cr.api_key == "k-derived" and c.calls == ["derive"] and c._probe_creds_source == "derived"
    assert FakeClob.create_or_derive_api_key.__name__ == "create_or_derive_api_key"  # restored


def test_derive_first_creates_only_if_derive_fails():
    c = FakeClob(derive_ok=False)
    with R.derive_first(FakeClob):
        cr = c.create_or_derive_api_key()
    assert cr.api_key == "k-created" and c.calls == ["derive", "create"] and c._probe_creds_source == "created"


def test_derive_first_wraps_the_real_client_class():
    from py_clob_client_v2 import ClobClient
    orig = ClobClient.create_or_derive_api_key
    with R.derive_first(ClobClient):
        assert ClobClient.create_or_derive_api_key is not orig
    assert ClobClient.create_or_derive_api_key is orig


# ---------------------------------------------------------------- sessions closed on exit
def test_main_closes_venues_on_exit_and_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    closed = []

    class C(CheckL):
        def close(self):
            closed.append("l")

    class D(CheckPM):
        def close(self):
            closed.append("p")

    monkeypatch.setattr(R, "build_live", lambda red: (C(), D()))
    monkeypatch.setattr(R, "OUT_LIVE", tmp_path)
    monkeypatch.setattr(P, "http_json", fake_http())
    monkeypatch.setattr(R.time, "time", lambda: 1795700700 + 100.0)
    R.main(["--mode", "check"])
    assert closed == ["l", "p"]

    async def boom(*a, **k):
        raise RuntimeError("venue down")
    monkeypatch.setattr(R, "account_check", boom)
    with pytest.raises(RuntimeError):
        R.main(["--mode", "check"])
    assert closed == ["l", "p", "l", "p"]


def test_build_live_closes_lighter_if_polymarket_setup_fails(monkeypatch):
    closed = []

    class L:
        def close(self):
            closed.append("l")

    def pm_fails(sec, red):
        raise RuntimeError("clob down")
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(R, "verify_pins", lambda: [])
    monkeypatch.setattr(R, "load_secrets", lambda client=None: {k: "x" * 20 for k in R.SSM})
    monkeypatch.setattr(R, "LighterLive", lambda sec: L())
    monkeypatch.setattr(R, "PMLive", pm_fails)
    with pytest.raises(RuntimeError, match="clob down"):
        R.build_live(R.Redactor())
    assert closed == ["l"]


def test_lighter_sdk_api_client_close_leaves_no_unclosed_session():
    """The mechanism BinaryLighterClient.close relies on: the SDK ApiClient built on a loop thread (as Stat_Arb does)
    closes its aiohttp session; no 'Unclosed client session' at garbage collection. No network: no request is made."""
    import gc
    import threading
    import warnings

    import lighter

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    async def build():
        a = lighter.ApiClient(configuration=lighter.Configuration(host="https://example.invalid"))
        lighter.OrderApi(a)
        return a

    a = asyncio.run_coroutine_threadsafe(build(), loop).result(5)
    asyncio.run_coroutine_threadsafe(a.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        del a
        gc.collect()
    assert not [w for w in ws if "Unclosed" in str(w.message)]


# ---------------------------------------------------------------- Lighter fee: charged on the fill, not the model
def ltrade(coid, rate, usd, we_bid=True, maker_ask=True, size=5.0, price=0.4):
    return dict(trade_id=1, price=str(price), size=str(size), usd_amount=str(usd), is_maker_ask=maker_ask,
                bid_client_id=coid if we_bid else 99, ask_client_id=99 if we_bid else coid, taker_fee=rate, maker_fee=None)


def test_lighter_fee_charged_reads_rate_times_notional():
    usd, rows = R.lighter_fee_charged([ltrade(7, 4800, 2.0), ltrade(7, 4800, 1.0), ltrade(8, 9999, 9.0)], 7)
    assert usd == pytest.approx(4800 * 3.0 / 1e6) and len(rows) == 2 and rows[0]["role"] == "taker"
    assert R.lighter_fee_charged([ltrade(8, 1, 1.0)], 7) == (None, [])
    # we sold (ask) and the maker was the bid -> we took; a resting maker fill would use maker_fee
    usd, rows = R.lighter_fee_charged([ltrade(7, 3000, 2.0, we_bid=False, maker_ask=False)], 7)
    assert usd == pytest.approx(0.006) and rows[0]["role"] == "taker"


class FeeL(FakeL):
    def __init__(self, trades, **kw):
        super().__init__(**kw)
        self.trades, self.fee_calls = trades, 0

    async def fee_charged(self, mid, coid):
        self.fee_calls += 1
        return self.trades


def test_fee_true_up_books_charged_minus_model_and_logs_both(tmp_path, monkeypatch):
    async def no_sleep(s):
        return None
    monkeypatch.setattr(R.asyncio, "sleep", no_sleep)
    L = FeeL([ltrade(1, 4800, 0.95)], results=[("filled", 5.0, 0.19)])  # 5 sh @0.19 -> notional 0.95
    PM = FakePM([("filled", 5.0, 0.76)])
    pr = make_probe(tmp_path, L, PM)
    w = make_window(pr)

    async def go():
        await pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {})
        await asyncio.gather(*list(pr.tasks))
    run(go())
    (pair,) = records(tmp_path, "pair")
    (f,) = records(tmp_path, "l_fee")
    model = P.fl(0.19) * 5
    charged = 4800 * 0.95 / 1e6
    assert f["fee_model_usd"] == pytest.approx(model) and f["fee_charged_usd"] == pytest.approx(charged) and f["applied"] is True
    assert f["realised_usd"] == pytest.approx(model - charged, abs=1e-8) and f["purpose"] == "entry"
    # the day's realised P&L = the pair at the model fee + the true-up = the pair at the charged fee
    led = R.Ledger("live").replay(tmp_path)
    assert led.realised_today() == pytest.approx(pair["realised_usd"] + model - charged, abs=1e-6)


def test_fee_true_up_nets_out_at_settlement(tmp_path):
    """Pair at the model fee, true-up, settlement: total realised = payout - all costs with the CHARGED Lighter fee."""
    e = 1795606200
    pr = make_probe(tmp_path)
    w = make_window(pr, e=e)
    model, charged = P.fl(0.19) * 5, 0.0040  # the ledger books the model fee in the leg cost (lighter_unit_cost)
    pr.log("l_fill", id="t", window=e, side="a", mode="live", symbol=R.l_symbol(e), market_id=12572, qty=5.0, px=0.19,
           fee_usd_model=model)
    pr.log("pm_fill", id="t", window=e, side="a", mode="live", token="DOWNTOK", shares=5.0, cost=3.8, fee_usd=0.0912)
    locked = (1 - 0.19 - 0.76) * 5 - model - 0.0912
    pr.log("pair", id="t", window=e, side="a", mode="live", qty=5.0, realised_usd=locked)
    pr.log("l_fee", id="t", window=e, side="a", mode="live", realised_usd=model - charged)
    run(pr.probe_settle(w, dict(pm_winner="UP", lighter_outcome=["2", "x"])))
    led = R.Ledger("live").replay(tmp_path)
    total_cost = 5 * 0.19 + charged + 3.8 + 0.0912
    assert led.realised_today() == pytest.approx(5.0 - total_cost, abs=1e-6)
    s = records(tmp_path, "probe_settle")[0]
    assert s["realised_usd"] == pytest.approx(0.0, abs=1e-6)


def test_fee_true_up_not_found_or_implausible_keeps_model(tmp_path, monkeypatch):
    async def no_sleep(s):
        return None
    monkeypatch.setattr(R.asyncio, "sleep", no_sleep)
    pr = make_probe(tmp_path, FeeL([]))
    base = dict(id="t", window=1, side="a", mode="live")
    run(pr.lighter_fee_true_up(base, 5, 1, 0.01, "entry"))
    pr2 = make_probe(tmp_path / "b", FeeL([ltrade(1, 4_800_000, 1.0)]))  # 4.8 USD on a 1 USD fill: unit wrong
    run(pr2.lighter_fee_true_up(base, 5, 1, 0.01, "entry"))
    a, = records(tmp_path, "l_fee")
    b, = records(tmp_path / "b", "l_fee")
    assert a["fee_charged_usd"] is None and a["realised_usd"] == 0.0 and pr.lv.fee_calls == 3
    assert b["applied"] is False and b["realised_usd"] == 0.0 and b["fee_charged_usd"] == pytest.approx(4.8)


def test_dry_mode_never_looks_up_lighter_fees(tmp_path):
    L = FeeL([], results=[("filled", 5.0, 0.19)])
    pr = make_probe(tmp_path, L, FakePM([("filled", 5.0, 0.76)]), mode="dry")
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert L.fee_calls == 0 and not records(tmp_path, "l_fee")


# ---------------------------------------------------------------- 11-26 live: "MarketIndex is not valid"
SIGN_PROOF = r"""
import json, sys
sys.path.insert(0, {repo!r})
from lighter.signer_client import SignerClient, get_signer, create_api_key, decode_and_free
from lp_arb import probe as R
priv, pub, err = create_api_key()                      # throwaway keypair, generated locally, never registered
assert err is None and priv and pub
s = SignerClient.__new__(SignerClient)                 # signing only: no ApiClient, no nonce manager, no network
s.signer, s.account_index = get_signer(), 100
assert decode_and_free(s.signer.CreateClient(b"http://127.0.0.1:1", priv.encode(), 304, 4, 100)) is None
out = {{}}
def sign(mid, **over):
    a = R.ioc_order_args(SignerClient, mid, 7, 50000, 45, False)
    a.update(over)
    _, tx, h, e = s.sign_create_order(**a, nonce=R.SIGN_CHECK_NONCE, api_key_index=4)
    return dict(ok=e is None and bool(tx), err=e)
for mid in (12572, 12808, 40000, 2 ** 40, 254, 255, 2 ** 48):
    out[str(mid)] = sign(mid)
out["12808_28day_expiry"] = sign(12808, order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY)  # Stat_Arb's IOC args
# the probe's own sign_check path (BinaryLighterClient.sign_check) on the real signer
cls = R.binary_client_class()
c = cls.__new__(cls)
c._signer = s
c._config = type("C", (), dict(api_key_index=4))()
c.add_market = lambda mid: cls.B.Market(symbol="BTC-UPDOWN-15M-1", market_id=mid, price_decimals=2, size_decimals=4,
                                        min_base_amount=5.0, min_quote_amount=0.0)
out["sign_check_12808"] = c.sign_check(12808, 5.0, 0.5)
print("RESULT" + json.dumps(out))
"""


def test_signer_accepts_binary_market_ids_offline_with_throwaway_key():
    """Item 3: lighter-sdk 1.1.8's signer, a throwaway key, no network (API URL is a closed local port; signing never
    contacts it). Binary-option ids sign; the nil market 255 and > 2^48-1 do not; Stat_Arb's 28-day expiry on an IOC
    is rejected (the second defect behind the first)."""
    import importlib.util
    if importlib.util.find_spec("lighter") is None:
        pytest.skip("lighter SDK not installed (probe-venv only)")
    r = subprocess.run([sys.executable, "-c", SIGN_PROOF.format(repo=str(REPO))], capture_output=True, text=True, timeout=60)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line, r.stderr[-2000:]
    out = json.loads(line[6:])
    for mid in ("12572", "12808", "40000", str(2 ** 40), "254"):
        assert out[mid] == {"ok": True, "err": None}, (mid, out[mid])
    assert out["255"]["err"] == "MarketIndex is not valid" and out[str(2 ** 48)]["err"] == "MarketIndex is not valid"
    assert out["12808_28day_expiry"] == {"ok": False, "err": "OrderExpiry is invalid"}
    assert out["sign_check_12808"] == [True, None]


def test_pinned_lighter_sdk_is_the_binary_capable_one():
    v, commit = R.lighter_sdk_installed()
    if v is None:
        pytest.skip("lighter SDK not installed (probe-venv only)")
    assert (v, commit) == (R.LIGHTER_SDK_VERSION, R.LIGHTER_SDK_COMMIT) == ("1.1.8", "56b3537cd311117a5a665d74db4c2cce7da155de")


def test_live_refuses_an_older_lighter_sdk(monkeypatch):
    monkeypatch.setattr(R, "lighter_sdk_installed", lambda: ("1.1.2", "fd4ee2530f78940cbed3dd80131d0fa66f74ad2a"))
    bad = R.verify_pins()
    assert any("lighter-sdk 1.1.2" in b and "MarketIndex is not valid" in b for b in bad)


class RecSigner:
    """Signer stand-in with the SDK's constants; records create_order / sign_create_order kwargs."""
    ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET = 0, 1
    ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL, ORDER_TIME_IN_FORCE_GOOD_TILL_TIME, ORDER_TIME_IN_FORCE_POST_ONLY = 0, 1, 2
    DEFAULT_IOC_EXPIRY, DEFAULT_28_DAY_ORDER_EXPIRY = 0, -1

    def __init__(self, err=None):
        self.calls, self.err = [], err

    def create_order(self, **kw):
        self.calls.append(("create_order", kw))
        return (None, None, self.err) if self.err else (object(), "0xtxhash", None)

    def sign_create_order(self, **kw):
        self.calls.append(("sign_create_order", kw))
        return (None, None, None, self.err) if self.err else (14, "{signed}", "h", None)


def binary_client(signer):
    import threading
    cls = R.binary_client_class()
    B = cls.B
    c = cls.__new__(cls)
    mk = B.Market(symbol="BTC-UPDOWN-15M-1795700700", market_id=12808, price_decimals=2, size_decimals=4, min_base_amount=5.0,
                  min_quote_amount=0.0)
    c._signer, c._lock, c._submitted = signer, threading.Lock(), {}
    c._config = type("C", (), dict(api_key_index=4))()
    c.get_markets = lambda: {mk.symbol: mk}
    c.add_market = lambda mid: mk
    c._with_retries = lambda desc, f: f()
    c._id_gen = type("G", (), dict(next=lambda self: 4242))()
    return c, B


def statarb_available():
    return (R.STATARB_DIR / "statarb" / "exchange" / "lighter_client.py").exists() and __import__("importlib").util.find_spec("lighter")


@pytest.mark.skipif(not statarb_available(), reason="Stat_Arb + lighter SDK not on this machine")
def test_ioc_is_sent_with_nil_expiry_on_the_binary_market():
    sig = RecSigner()
    c, B = binary_client(sig)
    res = c.place_order(B.OrderRequest(symbol="BTC-UPDOWN-15M-1795700700", side=B.OrderSide.SELL, size=5.0, order_type=B.OrderType.IOC,
                                       limit_price=0.45))
    (name, kw), = sig.calls
    assert name == "create_order" and kw == dict(market_index=12808, client_order_index=4242, base_amount=50000, price=45, is_ask=True,
                                                 order_type=0, time_in_force=0, reduce_only=False, trigger_price=0, order_expiry=0)
    assert res.status is B.OrderStatus.NEW and res.exchange_order_id == "0xtxhash"
    # the sign-only check signs exactly the same fields (plus an explicit nonce: no nonce fetch), never create_order
    sig2 = RecSigner()
    c2, _ = binary_client(sig2)
    assert c2.sign_check(12808, 5.0, 0.45, is_ask=True) == (True, None)
    (name2, kw2), = sig2.calls
    assert name2 == "sign_create_order" and kw2.pop("nonce") == R.SIGN_CHECK_NONCE and kw2.pop("api_key_index") == 4
    assert {k: v for k, v in kw2.items() if k != "client_order_index"} == {k: v for k, v in kw.items() if k != "client_order_index"}


@pytest.mark.skipif(not statarb_available(), reason="Stat_Arb + lighter SDK not on this machine")
def test_signer_rejection_is_marked_local():
    import types
    c, B = binary_client(RecSigner(err="MarketIndex is not valid"))
    lv = R.LighterLive.__new__(R.LighterLive)
    lv.c, lv.B = c, B
    w = types.SimpleNamespace(e=1795700700, mid=12808)
    r = run(lv.ioc(w, False, 0.45, 5.0))
    assert r["status"] == "rejected" and r["local"] is True and r["reason"] == "MarketIndex is not valid" and r.get("tx") in (None, "None")
    c2, _ = binary_client(RecSigner(err="21120: not enough margin"))  # a venue rejection: not deterministic-local
    lv.c = c2
    assert run(lv.ioc(w, False, 0.45, 5.0))["local"] is False


@pytest.mark.parametrize("reason,local", [
    ("MarketIndex is not valid", True), ("OrderExpiry is invalid", True), ("BaseAmount should not be less than 1", True),
    ("size 2.0 below market minimum base amount 5.0", True), ("ClientOrderIndex should be unique within the group", True),
    ("21120: not enough margin", False), ("HTTP 429 too many requests", False), (None, False), ("", False)])
def test_local_reject_classifier(reason, local):
    assert R.is_local_reject(reason) is local


# ---------------------------------------------------------------- item 4: deterministic local rejection -> halt once
def test_lighter_local_reject_halts_after_the_first_trigger(tmp_path):
    class Rej(FakeL):
        async def ioc(self, w, is_buy_up, price, size):
            self.calls.append(dict(price=price))
            return dict(venue="lighter", status="rejected", local=True, filled=0.0, reason="MarketIndex is not valid", coid=1,
                        t_send=time.time(), t_ack=time.time())
    L = Rej()
    pr = make_probe(tmp_path, L, FakePM())
    w = make_window(pr)
    run(pr.execute(w, "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert pr.halted == "lighter_local_reject"
    (h,) = records(tmp_path, "halt")
    assert h["reason"] == "lighter_local_reject" and h["id"] == "t1" and h["error"] == "MarketIndex is not valid"
    pr.persist.update(t2=True, t3=True)
    pr.on_displayed(w, "t2", "a", 0.19)
    pr.on_displayed(w, "t3", "a", 0.19)
    assert len(L.calls) == 1 and [s["reason"] for s in records(tmp_path, "skip")] == ["halted", "halted"]


def test_venue_rejection_does_not_halt(tmp_path):
    class Rej(FakeL):
        async def ioc(self, w, is_buy_up, price, size):
            self.calls.append(1)
            return dict(venue="lighter", status="rejected", local=False, filled=0.0, reason="21120: not enough margin", coid=1)
    pr = make_probe(tmp_path, Rej(), FakePM())
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert pr.halted is None and not records(tmp_path, "halt")


def test_pm_local_reject_unwinds_then_halts_without_retrying(tmp_path):
    class LocalPM(FakePM):
        async def fak_buy(self, token, price, size):
            self.calls.append(dict(price=price))
            return dict(venue="pm", status="rejected", local=True, shares=0.0, cost=0.0, reason="ValueError('invalid tick size')")
    L = FakeL([("filled", 5.0, 0.19), ("filled", 5.0, 0.17)])
    PM = LocalPM()
    pr = make_probe(tmp_path, L, PM)
    run(pr.execute(make_window(pr), "t1", "a", 0.19, 5.0, "DOWNTOK", {}))
    assert len(PM.calls) == 1  # no retries
    assert records(tmp_path, "unwind")[0]["qty"] == 5.0 and records(tmp_path, "unhedged")
    assert pr.halted == "pm_local_reject" and records(tmp_path, "halt")[0]["reason"] == "pm_local_reject"


def test_window_sign_check_failure_halts_before_any_trade(tmp_path, monkeypatch):
    class SignFail(FakeL):
        async def sign_check(self, mid, size=5, price=0.5, is_ask=False):
            return False, "MarketIndex is not valid"
    pr = make_probe(tmp_path, SignFail(), FakePM())
    w = make_window(pr)
    monkeypatch.setattr(P, "http_json", lambda url: {"min_order_size": 5, "tick_size": 0.01, "market": "0xc", "neg_risk": False})
    run(pr.prepare_window(w))
    assert pr.halted == "lighter_sign_reject"
    h = records(tmp_path, "halt")[0]
    assert h["market_id"] == w.mid and "MarketIndex" in h["error"] and records(tmp_path, "meta")[0]["sign_check"] == "MarketIndex is not valid"
    pr.persist["t1"] = True
    pr.on_displayed(w, "t1", "a", 0.19)
    assert not pr.lv.calls and records(tmp_path, "skip")[0]["reason"] == "halted"


def test_window_sign_check_ok_is_logged(tmp_path, monkeypatch):
    class SignOk(FakeL):
        async def sign_check(self, mid, size=5, price=0.5, is_ask=False):
            return True, None
    pr = make_probe(tmp_path, SignOk(), FakePM())
    monkeypatch.setattr(P, "http_json", lambda url: {"min_order_size": 5, "tick_size": 0.01, "market": "0xc", "neg_risk": False})
    run(pr.prepare_window(make_window(pr)))
    assert pr.halted is None and records(tmp_path, "meta")[0]["sign_check"] == "ok"


def test_check_mode_sign_only_step(tmp_path):
    L = CheckL()
    res = run(R.account_check(L, CheckPM(), R.Ledger("live"), http=fake_http(), now=1795700700 + 100))
    sc = res["lighter_btc15m"]["sign_check"]
    assert sc["ok"] is True and sc["submitted"] is False and L.signed == [13001] and res["lighter_btc15m"]["checks"]["signs_ioc"]
    L2 = CheckL()
    L2.sign_result = (False, "MarketIndex is not valid")
    res2 = run(R.account_check(L2, CheckPM(), R.Ledger("live"), http=fake_http(), now=1795700700 + 100))
    assert not res2["ok"] and res2["lighter_btc15m"]["sign_check"]["error"] == "MarketIndex is not valid"
    assert "lighter btc15m not tradable: signs_ioc" in res2["problems"] and not L2.calls


# ---------------------------------------------------------------- item 5: paper simulation labelled in the probe log
def test_paper_records_are_labelled_simulated(tmp_path):
    pr = make_probe(tmp_path)
    pr.log("hedge", id="t", window=1, side="a", hedged=True, pm_px=0.5, qty=50.0, pnl_per_share=0.02)
    pr.log("settle", window=1, pm_winner="UP", paper_pnl_usd=1.0)
    assert not records(tmp_path, "hedge") and not records(tmp_path, "settle")
    (h,) = records(tmp_path, "paper_hedge")
    (s,) = records(tmp_path, "paper_settle")
    assert h["sim"] is True and s["sim"] is True and pr.settled[1]["paper_pnl_usd"] == 1.0  # still drives probe_settle
    led = R.Ledger("live").replay(tmp_path)
    assert led.realised_today() == 0.0  # simulated P&L never enters the probe's ledger
