"""probe_vs_paper.py on synthetic logs (no network)."""

import json

import pytest

from lp_arb import probe_vs_paper as V

E = 1795700700
T0 = E + 60.0


def tid(side, t):
    return f"{E}-{side}-{int(t * 1000)}"


def trig(side, t, lpx, hpx):
    return dict(kind="trigger", t_local=t, id=tid(side, t), window=E, side=side, lighter_px=lpx, pm_px=hpx)


def build():
    """Probe: 4 triggers. A (side a) paired with slippage, exact id in paper; B (side b) paired, paper id 40 ms later and
    paper did not pair it; C skipped not_persistent; D Lighter filled 10, hedged 5, unwound 5 at a loss.
    Paper: A, B', C, an extra paper-only trigger, and one outside the probe's uptime."""
    L = "live"
    tA, tB, tC, tD = T0 + 10, T0 + 30, T0 + 50, T0 + 70
    A, B, C, D = tid("a", tA), tid("b", tB), tid("a", tC), tid("a", tD)
    probe = [
        dict(kind="probe_start", t_local=T0, mode=L),
        trig("a", tA, 0.40, 0.55), trig("b", tB, 0.61, 0.33), trig("a", tC, 0.30, 0.62), trig("a", tD, 0.20, 0.74),
        dict(kind="l_order", t_local=tA + 0.6, id=A, side="a", mode=L, status="filled", size=5.0, filled=5.0, avg_px=0.41),
        dict(kind="l_fill", t_local=tA + 0.6, id=A, side="a", mode=L, qty=5.0, px=0.41, fee_usd_model=0.0121),
        dict(kind="pm_order", t_local=tA + 0.7, id=A, side="a", mode=L, attempt=0, ask=0.56, status="filled"),
        dict(kind="pm_fill", t_local=tA + 0.7, id=A, side="a", mode=L, shares=5.0, cost=2.85, px=0.57, fee_usd=0.0613, fee_rate=0.05),
        dict(kind="pair", t_local=tA + 0.7, id=A, window=E, side="a", mode=L, qty=5.0, lighter_px=0.41, pm_px=0.57, attempts=1,
             pnl_usd=0.0766, realised_usd=0.0766, pnl_per_share=0.01532),
        dict(kind="fees", t_local=tA + 6, id=A, side="a", mode=L, venue="pm", trades=[dict(price=0.57, size=5.0, fee_rate_bps=500)]),
        dict(kind="l_fee", t_local=tA + 6, id=A, window=E, side="a", mode=L, purpose="entry", fee_model_usd=0.0121, fee_charged_usd=0.0123,
             applied=True, realised_usd=-0.0002, trades=[dict(fee_rate_raw=6000, usd_amount=2.05, fee_usd=0.0123)]),
        dict(kind="l_order", t_local=tB + 0.6, id=B, side="b", mode=L, status="filled", size=5.0, filled=5.0, avg_px=0.60),
        dict(kind="l_fill", t_local=tB + 0.6, id=B, side="b", mode=L, qty=5.0, px=0.60, fee_usd_model=0.012),
        dict(kind="pm_order", t_local=tB + 0.7, id=B, side="b", mode=L, attempt=0, ask=0.34, status="filled"),
        dict(kind="pm_fill", t_local=tB + 0.7, id=B, side="b", mode=L, shares=5.0, cost=1.75, px=0.35, fee_usd=0.0569, fee_rate=0.05),
        dict(kind="pair", t_local=tB + 0.7, id=B, window=E, side="b", mode=L, qty=5.0, lighter_px=0.60, pm_px=0.35, attempts=1,
             pnl_usd=0.1, realised_usd=0.1, pnl_per_share=0.02),
        dict(kind="skip", t_local=tC + 0.25, id=C, side="a", mode=L, reason="not_persistent"),
        dict(kind="skip", t_local=tC + 9, id="x", side="a", mode=L, reason="stale_pm"),
        dict(kind="l_order", t_local=tD + 0.6, id=D, side="a", mode=L, status="filled", size=10.0, filled=10.0, avg_px=0.20),
        dict(kind="l_fill", t_local=tD + 0.6, id=D, side="a", mode=L, qty=10.0, px=0.20, fee_usd_model=0.016),
        dict(kind="pm_order", t_local=tD + 0.7, id=D, side="a", mode=L, attempt=0, ask=0.75, status="filled"),
        dict(kind="pm_fill", t_local=tD + 0.7, id=D, side="a", mode=L, shares=5.0, cost=3.8, px=0.76, fee_usd=0.0912, fee_rate=0.05),
        dict(kind="pair", t_local=tD + 0.7, id=D, window=E, side="a", mode=L, qty=5.0, lighter_px=0.20, pm_px=0.76, attempts=4,
             pnl_usd=0.1, realised_usd=0.1, pnl_per_share=0.02),
        dict(kind="l_order", t_local=tD + 3, id=D, side="a", mode=L, purpose="unwind", status="filled", size=5.0, filled=5.0, avg_px=0.15),
        dict(kind="unwind", t_local=tD + 3, id=D, side="a", mode=L, qty=5.0, px=0.15, entry_px=0.20, realised_usd=-0.2627),
        dict(kind="unhedged", t_local=tD + 3, id=D, side="a", mode=L, qty=5.0, unwound=5.0, residual_held=0.0, loss_usd=0.2627),
        dict(kind="l_order", t_local=tD + 20, id="dry1", side="a", mode="dry", status="filled", size=5.0, filled=5.0),  # other mode
        dict(kind="probe_stop", t_local=T0 + 120, mode=L),
    ]
    paper = [
        trig("a", tA, 0.40, 0.55), dict(kind="check", t_local=tA + 0.5, id=A, at_ms=500, displayed=True),
        dict(kind="hedge", t_local=tA + 1, id=A, hedged=True, pm_px=0.56, qty=50.0, pnl_per_share=0.025),
        trig("b", tB + 0.04, 0.61, 0.33), dict(kind="check", t_local=tB + 0.54, id=tid("b", tB + 0.04), at_ms=500, displayed=True),
        dict(kind="hedge", t_local=tB + 1, id=tid("b", tB + 0.04), hedged=False, reason="hedge size < 5"),
        trig("a", tC, 0.30, 0.62), dict(kind="check", t_local=tC + 0.5, id=C, at_ms=500, displayed=False),
        trig("b", T0 + 90, 0.5, 0.4),  # paper-only, inside uptime
        trig("a", T0 + 500, 0.5, 0.4),  # outside uptime
    ]
    return probe, paper, (A, B, C, D)


def test_join_counts_and_skips():
    probe, paper, (A, B, C, D) = build()
    s = V.summarise(probe, paper, "live")
    t = s["triggers"]
    assert (t["probe"], t["paper"], t["both"], t["both_exact"], t["both_by_time"], t["probe_only"], t["paper_only"]) == (4, 4, 3, 2, 1, 1, 1)
    assert s["uptime"]["paper_triggers_outside"] == 1 and s["only_probe_ids"] == [D]
    assert s["skips"] == {"not_persistent": 1, "stale_pm": 1}


def test_lighter_fill_rate_vs_paper_d500():
    probe, paper, _ = build()
    lf = V.summarise(probe, paper, "live")["lighter_fill"]
    assert (lf["sent"], lf["filled"], lf["live_fill_rate"]) == (3, 3, 1.0)  # unwind and the dry order are not counted
    assert lf["paper_d500_same_triggers"] == pytest.approx(2 / 3, abs=1e-3)  # D has no paper trigger
    assert lf["live_vs_paper"] == {"filled/no_paper": 1, "filled/paper_d500": 2}
    assert lf["paper_d500_all"] == pytest.approx(2 / 3, abs=1e-3)


def test_per_pair_slippage_fees_and_paper_comparison():
    probe, paper, (A, B, C, D) = build()
    p = V.summarise(probe, paper, "live")["pairs"]
    rows = {r["id"]: r for r in p["rows"]}
    a, b = rows[A], rows[B]
    assert a["join"] == "exact" and a["live_cps"] == 1.53 and a["paper_cps"] == 2.5
    assert a["lighter_slip_c"] == 1.0  # bought at 0.41 vs trigger 0.40: 1c worse
    assert a["pm_slip_vs_trigger_c"] == 2.0 and a["pm_slip_vs_ask_c"] == 1.0
    assert a["fee_charged_pm_usd"] == pytest.approx(5 * 0.05 * 0.57 * 0.43, abs=1e-6) and a["fee_rate_bps_charged"] == [500]
    assert a["fee_charged_lighter_usd"] == 0.0123 and a["fee_lighter_rates_raw"] == [6000] and a["fee_model_lighter"] == 0.0121
    assert a["live_cps_charged_fee"] == pytest.approx(100 * (0.0766 - 0.0002) / 5, abs=0.01)
    assert b["join"] == "time" and b["paper_hedged"] is False and b["paper_cps"] is None and b["paper_reason"] == "hedge size < 5"
    assert b["lighter_slip_c"] == 1.0  # sold at 0.60 vs trigger 0.61: 1c worse
    assert p["n"] == 3 and p["with_paper_pair"] == 1 and p["paper_not_paired"] == 1
    assert p["live_cps_where_paper_paired"] == 1.53 and p["paper_cps_same"] == 2.5
    assert p["fees_charged"]["pm_pairs_with_record"] == 1 and p["fees_model_usd"]["pm"] == pytest.approx(0.0613 + 0.0569 + 0.0912)


def test_unhedged_and_unwind_costs():
    probe, paper, (A, B, C, D) = build()
    s = V.summarise(probe, paper, "live")
    u = s["unhedged"]
    assert u["n"] == 1 and u["loss_usd"] == 0.2627 and u["events"][0]["unwind_px"] == 0.15 and u["events"][0]["entry_px"] == 0.20
    assert s["unwinds"] == {"n": 1, "realised_usd": -0.2627}
    assert s["realised_usd"] == pytest.approx(0.0766 + 0.1 + 0.1 - 0.2627 - 0.0002, abs=1e-4)
    assert s["pairs"]["l_fee_true_up_usd"] == -0.0002


def test_uptime_handles_a_day_file_starting_mid_run():
    probe = [dict(kind="health", t_local=100.0), dict(kind="trigger", t_local=200.0), dict(kind="probe_stop", t_local=300.0),
             dict(kind="probe_start", t_local=1000.0), dict(kind="health", t_local=1600.0)]
    assert V.uptime(probe) == [(105.0, 295.0), (1005.0, 1595.0)]


def test_cli_writes_json(tmp_path, capsys):
    probe, paper, _ = build()
    day = "2026-11-27"
    for d, recs in (("probe", probe), ("paper", paper)):
        (tmp_path / d).mkdir()
        (tmp_path / d / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    out = tmp_path / "s.json"
    V.main(["--day", day, "--probe", str(tmp_path / "probe"), "--paper", str(tmp_path / "paper"), "--json", str(out)])
    assert json.loads(out.read_text())["pairs"]["n"] == 3 and "unhedged: 1" in capsys.readouterr().out


def test_simulated_paper_records_in_the_probe_log_are_excluded():
    probe, paper, (A, B, C, D) = build()
    fake = [dict(kind="paper_hedge", t_local=T0 + 11, id=A, sim=True, hedged=True, qty=50.0, pnl_per_share=0.5, pnl_usd=25.0),
            dict(kind="hedge", t_local=T0 + 12, id=A, hedged=True, qty=50.0, pnl_per_share=0.5, pnl_usd=25.0),  # legacy log
            dict(kind="paper_settle", t_local=T0 + 13, window=E, sim=True, paper_pnl_usd=25.0, realised_usd=99.0),
            dict(kind="pair", t_local=T0 + 14, id="simpair", window=E, side="a", mode="live", sim=True, qty=5.0, lighter_px=0.5,
                 pm_px=0.4, attempts=1, pnl_usd=9.0, realised_usd=9.0, pnl_per_share=1.8)]
    base = V.summarise(probe, paper, "live")
    s = V.summarise(probe + fake, paper, "live")
    assert s["sim_records_excluded"] == {"paper_hedge": 1, "hedge": 1, "paper_settle": 1, "pair": 1}
    assert s["pairs"]["n"] == base["pairs"]["n"] == 3 and s["realised_usd"] == base["realised_usd"]
