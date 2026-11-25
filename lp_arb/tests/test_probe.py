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
    assert {s["reason"] for s in records(tmp_path, "skip")} == {"kill_switch"}
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
    assert pr.ledger.expected() == ({R.l_symbol(w.e): 5.0}, {"DOWNTOK": 5.0})


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
    assert pr.ledger.expected()[0] == {R.l_symbol(1795606200): -7.0}  # sold Up = holds Down


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
    assert pr.ledger.expected() == ({R.l_symbol(1795606200): 0.0}, {})
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
    pr.on_displayed(w, "t1", "a", 0.60)
    assert records(tmp_path, "skip")[0]["reason"] == "pm_open_cap"


def test_dry_records_never_enter_the_live_ledger(tmp_path):
    pr = make_probe(tmp_path, mode="dry")
    pr.log("l_fill", id="d", window=1, side="a", mode="dry", symbol=R.l_symbol(1), qty=5.0, px=0.5)
    live = R.Ledger("live").replay(tmp_path)
    assert live.expected() == ({}, {}) and R.Ledger("dry").replay(tmp_path).expected()[0] == {R.l_symbol(1): 5.0}


# ---------------------------------------------------------------- restart / reconcile
def test_restart_with_foreign_position_refuses(tmp_path):
    L = FakeL(positions={"BTC-UPDOWN-15M-1795606200": 12.0})
    pr = make_probe(tmp_path, L, FakePM())
    run(pr.startup_reconcile())
    assert pr.reconcile_failed and records(tmp_path, "reconcile_fail")
    pr.on_displayed(make_window(pr), "t1", "a", 0.19)
    assert records(tmp_path, "skip")[0]["reason"] == "reconcile_fail"


def test_restart_with_foreign_pm_position_or_order_refuses(tmp_path):
    pr = make_probe(tmp_path, FakeL(), FakePM(positions=[dict(asset="999", size=3.0)]))
    run(pr.startup_reconcile())
    assert pr.reconcile_failed
    pr2 = make_probe(tmp_path / "b", FakeL(orders=[{"order_index": 1}]), FakePM())
    run(pr2.startup_reconcile())
    assert pr2.reconcile_failed


def test_restart_with_own_positions_passes(tmp_path):
    e = 1795606200
    first = make_probe(tmp_path)
    first.log("l_fill", id="i", window=e, side="b", mode="live", symbol=R.l_symbol(e), qty=5.0, px=0.15)
    first.log("pm_fill", id="i", window=e, side="b", mode="live", token="UPTOK", shares=5.0, cost=1.35, fee_usd=0.05)
    # a settled earlier window whose PM winner is still unredeemed is also ours
    first.log("pm_fill", id="j", window=e - 900, side="a", mode="live", token="OLDTOK", shares=5.0, cost=3.0, fee_usd=0.05)
    first.log("probe_settle", window=e - 900, mode="live", realised_usd=0.0)
    L = FakeL(positions={R.l_symbol(e): -5.0})
    PM = FakePM(positions=[dict(asset="UPTOK", size=5.0), dict(asset="OLDTOK", size=5.0, redeemable=True)])
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
