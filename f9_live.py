"""F9 'live' latency scenario (measured on dublin 2 with Polymarket's private order feed, 2026-11-17/18).

live   : book seen +9 ms; post on book +55 if the BTC best bid and ask were unchanged over the previous 1 s, else +190
         (median; p10 106, p90 559); cancel of a resting order lands +90 after the signal; taker hold 150.
         Cancels need the ack: a cancel lands no earlier than order-on-book + 90 (ackc, applied by the callers).
live_q : as live, but each slow post draws its latency from the measured quantiles (piecewise linear between
         p0 80 (assumed), p10 106, p50 190, p90 559, p100 1200 (assumed)); deterministic per (market, time).
Queue: pess (an existing level at our price is ahead of us by its displayed size; live: 98% of orders had one).
Importing this module registers both scenarios in pmlat.SC and patches pmlat.olat for live_q. Read cap: PMLAT_MAXDAY.
"""
import numpy as np

import pmlat

pmlat.SC["live"] = dict(book=9.0, order=190.0, order_fast=55.0, stable_ms=1000.0, cancel=90.0, hold=150.0)
pmlat.SC["live_q"] = dict(book=9.0, order=190.0, order_fast=55.0, stable_ms=1000.0, cancel=90.0, hold=150.0, sample=1)
QP, QV = np.array([0.0, 0.1, 0.5, 0.9, 1.0]), np.array([80.0, 106.0, 190.0, 559.0, 1200.0])

_olat = pmlat.olat


def olat(m, t, sc):
    v = _olat(m, t, sc)
    if sc.get("sample") and v == sc["order"]:
        u = np.random.default_rng((int(m["key"][2]) * 1_000_003 + int(t)) % 2**32).random()
        return float(np.interp(u, QP, QV))
    return v


pmlat.olat = olat
