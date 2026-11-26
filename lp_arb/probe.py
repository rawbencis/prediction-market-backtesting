"""T1 live PROBE executor: Lighter x Polymarket BTC 15m arb, smallest clips, hard loss limits.

    python3 lp_arb/probe.py [--mode dry|live|check] [--out DIR] [--minutes N]

Triggers are the paper trader's (lp_arb/paper.py), not a copy: `Probe` subclasses `paper.Paper` and inherits
discovery, both sockets, `evaluate` (the frozen trigger), `lquote_there` and `check` (the +100/+300/+500 ms quote
checks and the cool-downs) unchanged. The probe only hooks the moment the paper's +500 ms check says the Lighter quote
is still displayed (the paper's own "Lighter fill"); from there it sizes a clip and, if every limit allows, executes:

  1. Lighter IOC limit at the trigger's Lighter price, size q (side a buys Up, side b sells Up = buys Down)
  2. on a confirmed Lighter fill of f shares: Polymarket FAK BUY of the complementary token, f shares,
     worst price min(0.99, best ask + 0.03)
  3. hedge short: retry at the fresh ask + 0.03, up to 3 more times within 2 s; still short: unwind the unhedged
     Lighter remainder with an IOC at the best available price and log `unhedged`
  4. one execution at a time: no new pair while a leg is unhedged

Persistence (before 1): +250 ms after each trigger the pair is re-priced from the current books with the trigger's own
after-fee formula; edge < 4c -> `skip` reason `not_persistent` (and a `persist` record for every trigger). A trigger
with no +250 ms verdict is skipped `persist_unchecked` (fail closed).
Staleness (same moments): at the trigger or at +250 ms, a PM book older than 300 ms by exchange timestamp or arriving
with > 300 ms lag -> `stale_pm`; Lighter, 100 ms -> `stale_lighter`. The ages and the reason are in every `persist`
record. STALE_GUARD = "log" (default) only logs them; "skip" also skips the trigger (checked before the edge).

Modes:
  dry  (default) real market data, simulated fills against the live books, NO credentials loaded, never calls SSM
  live loads 6 parameters from SSM (ap-northeast-1, instance role) at start, keeps them in memory only, redacts them
       from every log line, exception and stderr write; reconciles both accounts before trading
  check loads SSM and builds both clients exactly as live (pins, redaction, authentication), then prints balances,
       positions, open orders, Lighter account type/tier, whether the BTC 15m market is tradable, the Polymarket client
       version and USDC allowance, and what the live start-up reconcile would say. Places and cancels nothing; writes
       no log; exit 0 = ok, 1 = problems listed.

Records (OUT/YYYY-MM-DD.jsonl, UTC): the paper trader's kinds and ids unchanged (start, window, trigger, check,
hedge = the paper model's hedge, settle = the paper settle, health, error), plus: meta, skip, l_order, l_fill,
pm_order, pm_fill, unwind, unhedged, pair, fees, balance, probe_settle, lighter_settled, redeem_due, reconcile,
reconcile_fail, halt, loss_cap, persist. P&L-bearing records carry `realised_usd`; the ledger (open legs, open cost, today's
realised P&L) is rebuilt from these records at start, so limits survive a restart.

Facts behind the numbers (sources in docs/probe.md):
  Lighter binary: a BUY of Up locks P per share, a SELL (= Down) locks (1 - P) per share; fully collateralised, no
  leverage; settles to USDC automatically; reduceOnly must be 0; min_base_amount 5, size step 1e-4, price step 0.01.
  Polymarket: min_order_size from the book (5), $1 minimum for marketable orders, winners must be redeemed (no redeem
  implementation exists on this box: `redeem_due` is logged each window for the operator to claim in the UI).
"""

import argparse
import asyncio
import collections
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `lp_arb` importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lp_arb import paper as P  # noqa: E402  (the frozen trigger lives here; imported, not copied)
from lp_arb.paper import COOL_S, L_MATCH_MS, Log, Paper, fl  # noqa: E402

# ======================= hard risk limits (do not move to config) =======================
DAILY_LOSS_CAP_USD = 10.0  # realised loss per UTC day; reached -> open nothing until the next UTC day
MAX_OPEN_COST_PM_USD = 30.0  # unsettled Polymarket cost (price + fee)
MAX_OPEN_COST_LIGHTER_USD = 40.0  # unsettled Lighter locked collateral (+ fee)
MAX_LEG_COST_USD = 10.0  # either leg of one pair
MIN_CLIP = 5  # shares
PM_MIN_MARKETABLE_USD = 1.0  # Polymarket minimum for marketable orders
HEDGE_OVER_ASK = 0.03  # Polymarket FAK worst price = min(HEDGE_MAX_PX, ask + this)
HEDGE_MAX_PX = 0.99
HEDGE_RETRIES = 3  # after the first attempt
HEDGE_RETRY_WINDOW_S = 2.0
PERSIST_MS = 250  # persistence check: +250 ms after the trigger, re-price the pair from the current books
PERSIST_MIN_EDGE = 0.04  # edge after fees still < this at +250 ms -> skip `not_persistent` (brief 2026-11-26)
STALE_PM_MS = 300  # at the trigger or at +250 ms: PM book age (local now - exchange ts of its last update) or lag > this -> stale_pm
STALE_L_MS = 100  # same for the Lighter book -> stale_lighter
STALE_GUARD = "log"  # "log": compute and log the ages and the stale reason on every trigger, never skip for it (default)
#                      "skip": a stale trigger is skipped (`skip` reason stale_pm / stale_lighter)
assert STALE_GUARD in ("log", "skip"), STALE_GUARD
UNWIND_SLIP = 0.05  # unwind IOC worst price = best price -/+ this (fills at book prices, never worse than this)
L_FILL_TIMEOUT_S = 3.0  # Lighter IOC: wait this long for a terminal status, else the leg is ambiguous -> halt
L_POLL_S = 0.25  # status poll interval: <= 12 polls + 1 sendTx per order; standard tier = 60 requests / rolling minute
BALANCE_EVERY_S = 60
STOP_FILE = Path.home() / "lp-arb" / "probe" / "STOP"  # kill switch: open nothing new, finish a hedge in progress

# ======================= venue / environment =======================
OUT_LIVE = Path.home() / "lp-arb" / "probe"
OUT_DRY = Path.home() / "lp-arb" / "probe-dry"  # dry fills never share a ledger with live ones
SSM_REGION = "ap-northeast-1"
SSM = {
    "L_ACCOUNT_INDEX": "/lparb/probe/lighter/ACCOUNT_INDEX",
    "L_API_KEY_INDEX": "/lparb/probe/lighter/API_KEY_INDEX",
    "L_API_KEY_PRIVATE_KEY": "/lparb/probe/lighter/API_KEY_PRIVATE_KEY",
    "PM_PRIVATE_KEY": "/lparb/probe/polymarket/PRIVATE_KEY",
    "PM_FUNDER": "/lparb/probe/polymarket/FUNDER",
    "PM_SIGNATURE_TYPE": "/lparb/probe/polymarket/SIGNATURE_TYPE",
}
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com/positions?sizeThreshold=0&limit=500&user="
L_BASE = "https://mainnet.zklighter.elliot.ai"
# Reused venue code, imported in live mode only and pinned by content hash (a changed file -> refuse to trade).
STATARB_DIR = Path(os.environ.get("LPARB_STATARB_DIR", str(Path.home() / "Stat_Arb")))
RD3_FILE = Path(os.environ.get("LPARB_RD3_FILE", str(Path.home() / "rd3-repo/research/rd3_live/rd3_live.py")))
PINNED = {  # Stat_Arb @b7ef8d3, rd3-repo @5c81bb8
    "statarb/exchange/lighter_client.py": "07116e547990264ccc17f29ae667769ec2185de7376fecaae6d1cf6ea7379c9b",
    "statarb/exchange/base.py": "6128bfb3e3eb0ab633ef29d8f0e4d34c048f7181bdf34271095e704a711c432b",
    "statarb/exchange/__init__.py": "8f03d1e953edd11ee0db0e9d2cccea52373c7b8ac5d1f2b3a58f1e1f1e1d090f",
    "statarb/exchange/paper_client.py": "5be5aa85e448108ec7a40981d5232f274daf781ed3f809ce9d2c813cd56a327e",
    "statarb/config.py": "ca9151d8b2c79925a821235763afb887263a062c60b747533e90425479f1df7f",
    "statarb/secrets.py": "a662ec2f25c2855cf9a53825fd69ce21f46471f4581f1cd2edd7c2e51ccebaa3",
    "rd3_live.py": "9d004f7b36b9b2360e57dcdaa59d28d9ea8932606e5c0a5051e9b9575b20d5a5",
}
PM_CLIENT_VERSION = "1.2.2"  # py_clob_client_v2; 1.2.0's exchange_v3 address is stale (see docs/probe.md)
LIGHTER_SDK_COMMIT = "fd4ee2530f78940cbed3dd80131d0fa66f74ad2a"  # same pin as Stat_Arb / xsmom / ladder

# dry-mode simulation
DRY_BAL = {"lighter": 50.0, "pm": 40.0}
SIM_L_SEND_S, SIM_L_CONFIRM_S = 0.15, 0.25  # IOC reaches the book after SEND, fill confirmed CONFIRM later
SIM_PM_SEND_S, SIM_PM_ACK_S = 0.10, 0.05

DROP_KEYS = {"signature", "sig", "tx_info", "signed", "signed_order", "private_key", "api_secret", "passphrase",
             "api_passphrase", "secret", "l2_signature"}
SIG_RE = re.compile(r"0x[0-9a-fA-F]{130,}")  # 65-byte ECDSA signatures and longer signed blobs


def today(t=None):
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if t is None else t))


def l_symbol(e):
    return f"BTC-UPDOWN-15M-{e}"


# ======================= redaction =======================
class Redactor:
    """Replaces every registered secret (and its hex/case/0x variants) and any signature-length hex blob."""

    def __init__(self):
        self.pats = []

    def add(self, secret):
        s = str(secret or "").strip()
        if len(s) < 4:  # API_KEY_INDEX / SIGNATURE_TYPE are 1-3 digit selectors, not secrets; redacting "2" would wreck every number
            return
        vs = {s}
        h = s[2:] if s.lower().startswith("0x") else s
        if re.fullmatch(r"[0-9a-fA-F]{16,}", h):
            vs |= {h, "0x" + h}
        for v in vs:
            if len(v) >= 16:  # keys, addresses, API creds: anywhere, even glued to other text
                self.pats.append(re.compile(re.escape(v), re.I))
            else:  # short ids (account index): whole tokens only, so timestamps/ids that contain the digits survive
                self.pats.append(re.compile(r"(?<![0-9A-Za-z])" + re.escape(v) + r"(?![0-9A-Za-z])", re.I))
        self.pats.sort(key=lambda p: -len(p.pattern))  # 0x-variant before the bare hex

    def scrub(self, text):
        text = SIG_RE.sub("[SIG]", str(text))
        for p in self.pats:
            text = p.sub("[REDACTED]", text)
        return text

    def obj(self, o):
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items() if str(k).lower() not in DROP_KEYS}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        if o is None or isinstance(o, bool):
            return o
        if isinstance(o, (int, float)):  # an account index logged as a number is still redacted
            t = str(o)
            return o if self.scrub(t) == t else "[REDACTED]"
        return self.scrub(o)


class ScrubStream:
    """stdout/stderr wrapper: nothing reaches the journal unscrubbed (tracebacks, SDK prints, logging)."""

    def __init__(self, inner, red):
        self.inner, self.red = inner, red

    def write(self, s):
        return self.inner.write(self.red.scrub(s))

    def flush(self):
        self.inner.flush()

    def __getattr__(self, k):
        return getattr(self.inner, k)


class ProbeLog(Log):
    """paper.Log (UTC day files, size cap, retention) + redaction + hooks (ledger, settle capture)."""

    def __init__(self, root, red):
        super().__init__(root)
        self.red, self.hooks = red, []

    def __call__(self, kind, **kw):
        kw = self.red.obj(kw)
        for h in self.hooks:
            try:
                h(kind, kw)
            except Exception as ex:  # noqa: BLE001
                super().__call__("error", where="log_hook", err=self.red.scrub(repr(ex))[:300])
        super().__call__(kind, **kw)


# ======================= pure sizing / limits =======================
def clip_size(hedge_px, l_min=5.0, pm_min=5.0):
    """q = max(5, Lighter min, PM min, ceil($1 / hedge price)), whole shares (fits Lighter's 1e-4 and PM's 0.01 size
    steps, and keeps PM BUY amounts at <= 2 decimals)."""
    need = PM_MIN_MARKETABLE_USD / hedge_px if hedge_px > 0 else float("inf")
    return float(math.ceil(max(MIN_CLIP, l_min, pm_min, need) - 1e-9))


def lighter_unit_cost(side, lpx):
    """USDC locked per share on Lighter: BUY Up locks P, SELL Up (= buy Down) locks 1 - P; plus the taker fee."""
    return (lpx if side == "a" else 1 - lpx) + fl(lpx)


def pm_worst(ask):
    return round(min(HEDGE_MAX_PX, ask + HEDGE_OVER_ASK), 2)


def pm_unit_cost(px, rate):
    return px + rate * px * (1 - px)


def size_and_check(side, lpx, lsz, hedge_px, pm_rate, l_min, pm_min, open_l, open_pm, free_l, free_pm):
    """-> (q, info, None) or (None, info, skip_reason). Pure; tested."""
    q = clip_size(hedge_px, l_min, pm_min)
    l_cost = q * lighter_unit_cost(side, lpx)
    pm_cost = q * pm_unit_cost(pm_worst(hedge_px), pm_rate)
    info = dict(q=q, lighter_cost=round(l_cost, 4), pm_cost=round(pm_cost, 4), lighter_size=lsz, hedge_px=hedge_px)
    if lsz is None or q > lsz + 1e-9:
        return None, info, "lighter_size"
    if l_cost > MAX_LEG_COST_USD:
        return None, info, "lighter_leg_cost"
    if pm_cost > MAX_LEG_COST_USD:
        return None, info, "pm_leg_cost"
    if open_l + l_cost > MAX_OPEN_COST_LIGHTER_USD:
        return None, info, "lighter_open_cap"
    if open_pm + pm_cost > MAX_OPEN_COST_PM_USD:
        return None, info, "pm_open_cap"
    if free_l is None or free_l < l_cost:
        return None, info, "lighter_balance"
    if free_pm is None or free_pm < pm_cost:
        return None, info, "pm_balance"
    return q, info, None


# ======================= ledger (state = function of the probe's own log records) =======================
class Ledger:
    KINDS = ("l_fill", "pm_fill", "unwind", "pair", "unhedged", "probe_settle")

    def __init__(self, mode):
        self.mode = mode
        self.win = {}  # window -> {"l": {side: [qty, cost]}, "pm": {token: [shares, cost, side]}, "locked": usd}
        self.realised = collections.defaultdict(float)  # UTC day -> usd
        self.mids, self.tokens = set(), set()  # Lighter market ids / PM tokens this probe ever traded (reconcile)

    def _w(self, e):
        return self.win.setdefault(int(e), {"l": {}, "pm": {}, "locked": 0.0})

    def apply(self, kind, r):
        if kind not in self.KINDS or r.get("mode") != self.mode:
            return
        day = today(r.get("t_local"))
        if kind == "l_fill" and r.get("qty", 0) > 0:
            w = self._w(r["window"])
            leg = w["l"].setdefault(r["side"], [0.0, 0.0])
            leg[0] += r["qty"]
            leg[1] += r["qty"] * lighter_unit_cost(r["side"], r["px"])
            if r.get("market_id"):
                w["mid"] = int(r["market_id"])
                self.mids.add(int(r["market_id"]))
        elif kind == "pm_fill" and r.get("shares", 0) > 0:
            w = self._w(r["window"])
            leg = w["pm"].setdefault(r["token"], [0.0, 0.0, r["side"]])
            leg[0] += r["shares"]
            leg[1] += r["cost"] + r.get("fee_usd", 0.0)
            self.tokens.add(r["token"])
        elif kind == "unwind" and r.get("qty", 0) > 0:
            w = self._w(r["window"])
            leg = w["l"].get(r["side"])
            if leg and leg[0] > 0:
                frac = min(1.0, r["qty"] / leg[0])
                leg[1] -= leg[1] * frac
                leg[0] -= min(leg[0], r["qty"])
            self.realised[day] += r.get("realised_usd", 0.0)
        elif kind == "pair":
            self._w(r["window"])["locked"] += r.get("realised_usd", 0.0)
            self.realised[day] += r.get("realised_usd", 0.0)
        elif kind == "probe_settle":
            self.win.pop(int(r["window"]), None)
            self.realised[day] += r.get("realised_usd", 0.0)

    def replay(self, root):
        for f in sorted(Path(root).glob("????-??-??.jsonl")):
            for line in f.open():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                self.apply(r.get("kind"), r)
        return self

    def open_cost(self, venue):
        k = "l" if venue == "lighter" else "pm"
        return sum(leg[1] for w in self.win.values() for leg in w[k].values() if leg[0] > 1e-9)

    def realised_today(self):
        return self.realised[today()]

    def expected(self):
        """-> ({lighter market id: signed Up qty (+ Up, - Down)}, {pm token: shares}) for unsettled windows."""
        lpos, ppos = collections.defaultdict(float), collections.defaultdict(float)
        for e, w in self.win.items():
            for side, (q, _) in w["l"].items():
                lpos[w.get("mid", l_symbol(e))] += q if side == "a" else -q
            for tok, (s, _, _) in w["pm"].items():
                ppos[tok] += s
        return dict(lpos), dict(ppos)


def reconcile(ledger, l_positions, l_orders, pm_positions, pm_orders, tol=1e-3):
    """Pure. l_positions {market id: signed qty} (binary `bo_positions`; perps as "perp:SYMBOL", always foreign);
    pm_positions [{asset, size, redeemable}]. -> (problems, warnings)."""
    exp_l, exp_pm = ledger.expected()
    problems, warns = [], []
    for sym, q in l_positions.items():
        if abs(q) <= tol:
            continue
        if sym not in ledger.mids:
            problems.append(f"lighter position {sym} {q:+} not created by the probe")
        elif abs(q - exp_l.get(sym, 0.0)) > tol:
            problems.append(f"lighter position {sym} {q:+} != probe ledger {exp_l.get(sym, 0.0):+}")
    for sym, q in exp_l.items():
        if abs(q) > tol and abs(l_positions.get(sym, 0.0)) <= tol:
            warns.append(f"probe ledger expects lighter {sym} {q:+} but the venue shows none (settled while down?)")
    if l_orders:
        problems.append(f"{len(l_orders)} open Lighter order(s); the probe only sends IOC")
    for p in pm_positions:
        if float(p.get("size") or 0) > tol and str(p.get("asset")) not in ledger.tokens:
            problems.append(f"polymarket position {str(p.get('asset'))[:12]}.. size {p.get('size')} not created by the probe")
    if pm_orders:
        problems.append(f"{len(pm_orders)} open Polymarket order(s); the probe only sends FAK")
    return problems, warns


# ======================= secrets / reused code (live only) =======================
def load_secrets(client=None):
    """SSM -> {logical name: value}. Values never leave memory; names only in errors."""
    if client is None:
        import boto3

        client = boto3.client("ssm", region_name=SSM_REGION)
    r = client.get_parameters(Names=list(SSM.values()), WithDecryption=True)
    if r.get("InvalidParameters"):
        raise RuntimeError(f"missing SSM parameters: {sorted(r['InvalidParameters'])}")
    by = {p["Name"]: p["Value"] for p in r["Parameters"]}
    return {k: by[n] for k, n in SSM.items()}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def verify_pins():
    bad = []
    for rel, want in PINNED.items():
        p = RD3_FILE if rel == "rd3_live.py" else STATARB_DIR / rel
        got = sha(p) if p.exists() else "missing"
        if got != want:
            bad.append(f"{p}: {got[:12]} != pinned {want[:12]}")
    from importlib.metadata import version

    v = version("py_clob_client_v2")
    if v != PM_CLIENT_VERSION:
        bad.append(f"py_clob_client_v2 {v} != {PM_CLIENT_VERSION}")
    return bad


def import_rd3():
    spec = importlib.util.spec_from_file_location("rd3_live", RD3_FILE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ======================= venues =======================
class LighterLive:
    """Stat_Arb's LighterClient (lighter-sdk @fd4ee25) + the binary-market bits it lacks."""

    def __init__(self, sec):
        sys.path.insert(0, str(STATARB_DIR))
        from statarb.config import LighterConfig
        from statarb.exchange import base as B
        from statarb.exchange.lighter_client import LighterClient

        self.B = B

        class BinaryLighterClient(LighterClient):
            def _refresh_markets(self):  # the default orderBooks listing has no binary markets: keep added ones
                keep = {s: m for s, m in getattr(self, "_markets", {}).items() if "UPDOWN" in s}
                super()._refresh_markets()
                with self._lock:
                    self._markets.update(keep)

            def add_market(self, market_id):
                resp = self._with_retries(f"order_books({market_id})", lambda: self._order_api.order_books(market_id=market_id))
                for ob in resp.order_books:
                    m = B.Market(symbol=ob.symbol, market_id=ob.market_id, price_decimals=ob.supported_price_decimals,
                                 size_decimals=ob.supported_size_decimals, min_base_amount=float(ob.min_base_amount),
                                 min_quote_amount=float(ob.min_quote_amount))
                    with self._lock:
                        self._markets[ob.symbol] = m
                    return m

            def _auth(self):
                return self._signer.create_auth_token_with_expiry(api_key_index=self._config.api_key_index)[0]

            def snapshot(self):
                """Binary positions are NOT in `positions`: /account returns them as `bo_positions`
                [{market_id, sign (1 Up, -1 Down), size, avg_entry_price, entry_quote}], which SDK 1.1.2 does not model
                (kept in additional_properties). -> positions {market_id: signed Up qty, "perp:SYM": signed qty}."""
                a = self._with_retries("account", lambda: self._account_api.account(by="index", value=str(self._config.account_index))).accounts[0]
                pos = {}
                for p in (a.additional_properties or {}).get("bo_positions") or ():
                    q = float(p.get("size") or 0)
                    if q:
                        pos[int(p["market_id"])] = q if int(p.get("sign") or 1) == 1 else -q
                for p in a.positions or ():
                    q = float(p.position)
                    if q:
                        pos[f"perp:{p.symbol}"] = q if int(p.sign) == 1 else -q
                return dict(available=float(a.available_balance), collateral=float(a.collateral), positions=pos)

            def account_detail(self):
                """--mode check: account type/tier/status as the API reports them (raw codes; nothing interpreted)."""
                a = self._with_retries("account", lambda: self._account_api.account(by="index", value=str(self._config.account_index))).accounts[0]
                out = dict(account_type=a.account_type, account_trading_mode=a.account_trading_mode, status=a.status,
                           available=float(a.available_balance), collateral=float(a.collateral),
                           pending_order_count=a.pending_order_count, total_order_count=a.total_order_count)
                try:
                    lim = self._with_retries("account_limits", lambda: self._account_api.account_limits(
                        account_index=self._config.account_index, authorization=self._auth()))
                    out.update(user_tier=lim.user_tier, user_tier_name=lim.user_tier_name, taker_fee_tick=lim.current_taker_fee_tick,
                               maker_fee_tick=lim.current_maker_fee_tick)
                except Exception as ex:  # noqa: BLE001
                    out["limits_error"] = repr(ex)[:200]
                return out

            def active_orders(self):
                r = self._with_retries("account_active_orders(all)", lambda: self._order_api.account_active_orders(
                    account_index=self._config.account_index, authorization=self._auth()))
                return [o.to_dict() for o in (r.orders or ())]

            def order_raw(self, coid):
                r = self._with_retries("account_orders", lambda: self._order_api.account_orders(
                    account_index=self._config.account_index, authorization=self._auth(), client_order_indexes=str(coid)))
                for o in getattr(r, "orders", None) or ():
                    if int(o.client_order_index) == int(coid):
                        return o.to_dict()
                return None

        cfg = LighterConfig(base_url=L_BASE, api_key_private_key=sec["L_API_KEY_PRIVATE_KEY"], account_index=int(sec["L_ACCOUNT_INDEX"]),
                            api_key_index=int(sec["L_API_KEY_INDEX"]), request_timeout_s=5.0, max_retries=1, retry_backoff_s=0.1)
        self.c = BinaryLighterClient(cfg)

    async def prepare(self, w):
        m = await asyncio.to_thread(self.c.add_market, w.mid)
        return dict(l_min=m.min_base_amount, l_size_dec=m.size_decimals, l_px_dec=m.price_decimals) if m else None

    async def ioc(self, w, is_buy_up, price, size):
        B = self.B
        sym = l_symbol(w.e)
        req = B.OrderRequest(symbol=sym, side=B.OrderSide.BUY if is_buy_up else B.OrderSide.SELL, size=size,
                             order_type=B.OrderType.IOC, limit_price=price)
        out = dict(t_send=time.time(), venue="lighter", symbol=sym, is_buy_up=is_buy_up, limit=price, size=size)
        try:
            res = await asyncio.to_thread(self.c.place_order, req)
        except Exception as ex:  # noqa: BLE001  ExchangeError after retries: the order may or may not exist
            return dict(out, status="ambiguous", filled=0.0, reason=repr(ex)[:300], t_ack=time.time())
        out.update(t_ack=time.time(), coid=res.client_order_id, tx=res.exchange_order_id)
        if res.status is B.OrderStatus.REJECTED:
            return dict(out, status="rejected", filled=0.0, reason=res.reason)
        end = time.time() + L_FILL_TIMEOUT_S
        while time.time() < end:  # IOC: poll the authoritative status (not-found right after send = still pending)
            try:
                st = await asyncio.to_thread(self.c._query_order_status, sym, res.client_order_id)
            except Exception as ex:  # noqa: BLE001
                st, out["poll_err"] = None, repr(ex)[:200]
            if st is not None and st.status in (B.OrderStatus.FILLED, B.OrderStatus.CANCELED):
                raw = None
                if st.filled_size > 0:
                    try:
                        raw = await asyncio.to_thread(self.c.order_raw, res.client_order_id)
                    except Exception:  # noqa: BLE001
                        raw = None
                return dict(out, status="filled" if st.filled_size > 0 else "nofill", filled=st.filled_size,
                            avg_px=st.avg_fill_price or None, order_index=st.exchange_order_id, t_fill=time.time(),
                            exch={k: (raw or {}).get(k) for k in ("timestamp", "transaction_time", "updated_at", "created_at", "status",
                                                                     "filled_base_amount", "filled_quote_amount")})
            await asyncio.sleep(L_POLL_S)
        return dict(out, status="ambiguous", filled=0.0, reason=f"no terminal status within {L_FILL_TIMEOUT_S}s")

    async def fee_charged(self, market_id, coid):
        """Off the hot path: the order's trades from the public recentTrades (taker_fee, price, size, exchange time)."""
        d = await asyncio.to_thread(P.http_json, P.L_REST + f"recentTrades?market_id={market_id}&limit=100")
        return [dict(price=float(t["price"]), size=float(t["size"]), taker_fee=t.get("taker_fee"), usd=t.get("usd_amount"),
                     timestamp=t.get("timestamp"), tx_hash=t.get("tx_hash"), trade_id=t.get("trade_id"))
                for t in d.get("trades") or () if str(coid) in (str(t.get("ask_client_id")), str(t.get("bid_client_id")))]

    async def balance(self):
        s = await asyncio.to_thread(self.c.snapshot)
        return dict(free=s["available"], collateral=s["collateral"], positions=s["positions"])

    async def open_orders(self):
        return await asyncio.to_thread(self.c.active_orders)

    async def account_info(self):
        return await asyncio.to_thread(self.c.account_detail)

    async def market_info(self, mid):
        """The signer client registers the market exactly as live `prepare` does (Stat_Arb's market cache)."""
        m = await asyncio.to_thread(self.c.add_market, mid)
        return dict(symbol=m.symbol, l_min=m.min_base_amount, l_size_dec=m.size_decimals, l_px_dec=m.price_decimals) if m else None


class PMLive:
    """rd3_live's client set-up (HTTP/2 keep-alive, derived API creds, version resolve) and its response parsing."""

    def __init__(self, sec, red):
        self.rd3 = import_rd3()
        self.c = self.rd3.make_client({"POLY_PRIVATE_KEY": sec["PM_PRIVATE_KEY"], "POLY_SIGNATURE_TYPE": sec["PM_SIGNATURE_TYPE"],
                                       "POLY_FUNDER_ADDRESS": sec["PM_FUNDER"]})
        cr = self.c.creds
        for v in (cr.api_key, cr.api_secret, cr.api_passphrase):
            red.add(v)
        self.funder = (sec["PM_FUNDER"] or "").strip() or self.c.get_address()  # positions live at the funder (proxy) or the EOA
        self.server_version = self.c.get_version()

    async def prepare(self, w, cond, toks):
        def warm():
            for t in toks:
                self.c.get_tick_size(t)
                self.c.get_neg_risk(t)
            self.c.get_clob_market_info(cond)

        await asyncio.to_thread(warm)

    async def fak_buy(self, token, price, size):
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2.exceptions import PolyApiException

        rd3 = self.rd3
        out = dict(venue="pm", token=token, limit=price, size=size, t_sign=time.time())

        def go():
            signed = self.c.create_order(OrderArgs(token_id=token, price=price, size=size, side="BUY"))
            out["t_send"] = time.time()
            return self.c.post_order(signed, OrderType.FAK)

        try:
            resp = await asyncio.to_thread(go)
        except PolyApiException as e:
            msg = getattr(e, "error_msg", None) or str(e)
            kind = rd3.classify_api_error(e.status_code, msg)
            return dict(out, t_ack=time.time(), status="nofill" if kind == "nofill" else kind, shares=0.0, cost=0.0,
                        code=e.status_code, reason=str(msg)[:300])
        except Exception as ex:  # noqa: BLE001
            return dict(out, t_ack=time.time(), status="ambiguous", shares=0.0, cost=0.0, reason=repr(ex)[:300])
        out["t_ack"] = time.time()
        kind, oid, sh, cost = rd3.parse_post(resp, size, price)
        out.update(order_id=oid, resp_status=(resp or {}).get("status") if isinstance(resp, dict) else None)
        if kind == "pending":
            if not oid:
                return dict(out, status="ambiguous", shares=0.0, cost=0.0, reason="no order id")
            r = await asyncio.to_thread(rd3.resolve_fill, self.c, oid, 3.0)
            if r is None:
                return dict(out, status="ambiguous", shares=0.0, cost=0.0, reason="fill unresolved in 3 s")
            sh, cost, est = r
            out["cost_est"] = est
            kind = "filled" if sh > 0 else "nofill"
        if kind == "rejected":
            return dict(out, status="rejected", shares=0.0, cost=0.0, reason=str(resp)[:300])
        if sh > 0 and not rd3.fill_within_order(sh, cost, size, price):
            return dict(out, status="ambiguous", shares=sh, cost=cost, reason="fill outside order bounds")
        return dict(out, status="filled" if sh > 0 else "nofill", shares=sh, cost=cost, t_fill=time.time())

    async def fee_charged(self, oid):
        """Best effort, off the hot path: fee_rate_bps on the order's trades."""
        from py_clob_client_v2.clob_types import TradeParams

        def go():
            o = self.c.get_order(oid) or {}
            rows = []
            for tid in o.get("associate_trades") or []:
                for t in self.c.get_trades(TradeParams(id=tid), only_first_page=True):
                    rows.append(dict(price=float(t["price"]), size=float(t["size"]), fee_rate_bps=float(t.get("fee_rate_bps") or 0),
                                     match_time=t.get("match_time")))
            return rows

        return await asyncio.to_thread(go)

    async def balance(self):
        return dict(free=await asyncio.to_thread(self.rd3.cash, self.c))

    async def allowance(self):
        """Raw COLLATERAL balance/allowances (L2-authenticated). The probe only BUYs, so only collateral is spent."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        return await asyncio.to_thread(self.c.get_balance_allowance, BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))

    async def closed_only(self):
        return await asyncio.to_thread(self.c.get_closed_only_mode)

    def spender(self, version, neg_risk=False):
        """The exchange contract a BUY is signed for (py_clob_client_v2 order_builder: v3 -> exchange_v3, v2 -> exchange_v2)."""
        from py_clob_client_v2.config import get_contract_config

        cc = get_contract_config(self.c.chain_id if hasattr(self.c, "chain_id") else 137)
        if int(version) == 3:
            return "exchange_v3", cc.exchange_v3
        return ("neg_risk_exchange_v2", cc.neg_risk_exchange_v2) if neg_risk else ("exchange_v2", cc.exchange_v2)

    async def open_orders(self):
        return await asyncio.to_thread(self.c.get_open_orders)

    async def positions(self):
        def go():
            req = urllib.request.Request(DATA_API + self.funder, headers={"User-Agent": "lp-arb-probe"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)

        return await asyncio.to_thread(go)


class LighterSim:
    """Dry mode: the IOC meets the live Lighter book SIM_L_SEND_S after sending; walks levels at or better than the limit."""

    def __init__(self, probe):
        self.p, self.n = probe, 0

    async def prepare(self, w):
        d = await asyncio.to_thread(P.http_json, P.L_REST + f"orderBooks?market_id={w.mid}")
        ob = d["order_books"][0]
        return dict(l_min=float(ob["min_base_amount"]), l_size_dec=ob["supported_size_decimals"], l_px_dec=ob["supported_price_decimals"])

    async def ioc(self, w, is_buy_up, price, size):
        self.n += 1
        out = dict(t_send=time.time(), venue="lighter", symbol=l_symbol(w.e), is_buy_up=is_buy_up, limit=price, size=size,
                   coid=f"sim-{self.n}")
        await asyncio.sleep(SIM_L_SEND_S)
        out["t_ack"] = time.time()
        book = w.lbook.a if is_buy_up else w.lbook.b
        lv = sorted((p, s) for p, s in book.items() if (p <= price + 1e-9 if is_buy_up else p >= price - 1e-9))
        if not is_buy_up:
            lv.reverse()
        filled = cost = 0.0
        for p, s in lv:
            t = min(s, size - filled)
            filled, cost = filled + t, cost + t * p
            if filled >= size - 1e-9:
                break
        await asyncio.sleep(SIM_L_CONFIRM_S)
        return dict(out, status="filled" if filled > 0 else "nofill", filled=round(filled, 4),
                    avg_px=round(cost / filled, 6) if filled else None, t_fill=time.time())

    async def balance(self):
        return dict(free=self.p.sim_bal["lighter"])


class PMSim:
    """Dry mode: the FAK meets the live PM book SIM_PM_SEND_S after sending. Up asks for Up; 1 - Up bids for Down."""

    def __init__(self, probe):
        self.p, self.n = probe, 0

    async def prepare(self, w, cond, toks):
        return None

    async def fak_buy(self, token, price, size):
        self.n += 1
        w = self.p.window_of_token(token)
        out = dict(venue="pm", token=token, limit=price, size=size, t_sign=time.time(), order_id=f"sim-{self.n}")
        await asyncio.sleep(SIM_PM_SEND_S)
        out["t_send"] = out["t_sign"]
        if w is None or not w.pbook.valid():
            return dict(out, t_ack=time.time(), status="nofill", shares=0.0, cost=0.0, reason="book unknown")
        if token == w.up_token:
            lv = sorted((p, s) for p, s in w.pbook.a.items() if p <= price + 1e-9)
        else:
            lv = sorted((round(1 - p, 4), s) for p, s in w.pbook.b.items() if 1 - p <= price + 1e-9)
        sh = cost = 0.0
        for p, s in lv:
            t = min(s, size - sh)
            sh, cost = sh + t, cost + t * p
            if sh >= size - 1e-9:
                break
        await asyncio.sleep(SIM_PM_ACK_S)
        return dict(out, t_ack=time.time(), status="filled" if sh > 0 else "nofill", shares=round(sh, 4), cost=round(cost, 6),
                    t_fill=time.time())

    async def balance(self):
        return dict(free=self.p.sim_bal["pm"])


# ======================= the probe =======================
class Probe(Paper):
    def __init__(self, log, mode="dry", stop_at=None, lighter=None, pm=None, stop_file=STOP_FILE):
        super().__init__(log, stop_at)
        self.mode, self.stop_file = mode, stop_file
        self.ledger = Ledger(mode)
        self.lv = lighter if lighter is not None else (LighterSim(self) if mode == "dry" else None)
        self.pv = pm if pm is not None else (PMSim(self) if mode == "dry" else None)
        self.sim_bal = dict(DRY_BAL)
        self.free = {"lighter": None, "pm": None}  # last balance snapshot minus local spend since
        self.busy = None  # trigger id of the execution in progress (at most one)
        self.halted = None  # reason; set on any ambiguous leg -> nothing new until restart + review
        self.reconcile_failed = None
        self.meta = {}  # window epoch -> dict
        self.settled = {}  # window epoch -> the paper settle record
        self.tasks = set()
        self.counts = collections.Counter()
        self.persist = {}  # trigger id -> True / False from the +250 ms check (popped at +500 ms)
        self.ages = {}  # trigger id -> book ages at the trigger (consumed at +250 ms)
        self.clock = time.time  # tests replace it with the fake loop's clock
        if isinstance(log, ProbeLog):
            log.hooks.append(self.ledger.apply)
            log.hooks.append(self._capture)
            log.hooks.append(self._on_trigger)

    def _capture(self, kind, kw):
        if kind == "start":
            kw["mode"] = f"probe-{self.mode}"
        elif kind == "settle":
            self.settled[kw["window"]] = kw

    def _on_trigger(self, kind, kw):
        """Each paper `trigger` record (logged inside the unchanged paper.evaluate) schedules the +250 ms check."""
        if kind == "trigger":
            w = self.win.get(kw["window"])
            if w is not None:
                self.ages[kw["id"]] = self.book_ages(w)
                while len(self.ages) > 2000:
                    self.ages.pop(next(iter(self.ages)))
                asyncio.get_running_loop().call_later(PERSIST_MS / 1000, self.persist_check, w, kw["id"], kw["side"])

    def window_of_token(self, tok):
        for w in self.win.values():
            if tok in (w.up_token, w.down_token):
                return w
        return None

    def spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    # ---------- per window: venue metadata ----------
    async def pm_meta_and_socket(self, w):
        self.spawn(self.prepare_window(w))
        await super().pm_meta_and_socket(w)

    async def prepare_window(self, w):
        for _ in range(120):
            if w.up_token or self.stop.is_set():
                break
            await self.sleep(0.5)
        if not w.up_token:
            return
        for attempt in range(5):
            try:
                m = dict(symbol=l_symbol(w.e))
                m.update(await self.lv.prepare(w) or {})
                for name, tok in (("up", w.up_token), ("down", w.down_token)):
                    b = await asyncio.to_thread(P.http_json, f"{CLOB}/book?token_id={tok}")
                    m[f"pm_min_{name}"] = float(b.get("min_order_size") or 5)
                    m[f"pm_tick_{name}"] = float(b.get("tick_size") or 0.01)
                    m["cond"], m["neg_risk"] = b.get("market"), b.get("neg_risk")
                await self.pv.prepare(w, m["cond"], (w.up_token, w.down_token))
                self.meta[w.e] = m
                self.log("meta", window=w.e, **m)
                return
            except Exception as ex:  # noqa: BLE001
                self.err("prepare_window", ex)
                await self.sleep(3 + 3 * attempt)

    # ---------- the hook: paper's +500 ms "quote still displayed" ----------
    def check(self, w, tid, side, lpx, t0, ms):
        before = w.nxt[side]
        super().check(w, tid, side, lpx, t0, ms)  # logs `check`, applies the paper cool-down, schedules the paper hedge
        if ms == 500 and w.nxt[side] != before and w.nxt[side] == t0 + L_MATCH_MS / 1000 + COOL_S:
            try:
                self.on_displayed(w, tid, side, lpx)
            except Exception as ex:  # noqa: BLE001
                self.err("on_displayed", ex)

    # ---------- +250 ms persistence ----------
    def pair_edge(self, w, side):
        """1 - pair cost after fees from the current best quotes, the trigger's own formula (paper.evaluate):
        a: buy Up on Lighter at the ask + buy Down on PM at 1 - Up bid;  b: sell Up on Lighter at the bid + buy Up on PM."""
        if not (w.lbook.valid() and w.pbook.valid()):
            return None, {}
        lb, la, lbs, las = w.lbook.best()
        pb, pa, pbs, pas = w.pbook.best()
        if side == "a":
            cost = la + (1 - pb) + fl(la) + self.fpm(w, 1 - pb)
        else:
            cost = (1 - lb) + pa + fl(lb) + self.fpm(w, pa)
        return 1 - cost, dict(lighter_book=[lb, la, lbs, las], pm_book=[pb, pa, pbs, pas])

    def book_ages(self, w):
        """Per venue: age_ms = local now - exchange timestamp of the book's last update; lag_ms = local receive time of
        that update - its exchange timestamp; since_recv_ms = local now - that receive time. None if no timestamp yet."""
        now = self.clock()

        def one(b):
            if not b.x:
                return dict(age_ms=None, lag_ms=None, since_recv_ms=None)
            return dict(age_ms=round(now * 1000 - b.x, 1), lag_ms=round(b.t * 1000 - b.x, 1), since_recv_ms=round((now - b.t) * 1000, 1))

        return dict(pm=one(w.pbook), lighter=one(w.lbook))

    @staticmethod
    def stale_reason(ages):
        """'stale_pm' / 'stale_lighter' / None for one ages snapshot; a venue with no exchange timestamp is stale."""
        for venue, lim, why in (("pm", STALE_PM_MS, "stale_pm"), ("lighter", STALE_L_MS, "stale_lighter")):
            a = ages[venue]
            if a["age_ms"] is None or a["age_ms"] > lim or a["lag_ms"] > lim:
                return why
        return None

    def persist_check(self, w, tid, side):
        try:
            edge, books = self.pair_edge(w, side)
            ok = edge is not None and round(edge, 9) >= PERSIST_MIN_EDGE
            a0, a250 = self.ages.pop(tid, None), self.book_ages(w)
            stale = (self.stale_reason(a0) if a0 else "stale_pm") or self.stale_reason(a250)
            enforce = STALE_GUARD == "skip"
            self.persist[tid] = ok and not (stale and enforce)
            while len(self.persist) > 2000:  # triggers whose +500 check never ran (window end, stop)
                self.persist.pop(next(iter(self.persist)))
            e = None if edge is None else round(edge, 5)
            self.log("persist", id=tid, window=w.e, side=side, at_ms=PERSIST_MS, edge=e, ok=ok, stale=stale,
                     stale_guard=STALE_GUARD, ages=dict(trigger=a0, at_250=a250), **books)
            if stale and enforce:
                at = "trigger" if a0 is None or self.stale_reason(a0) else "at_250"
                self.skip(tid, w, side, stale, at=at, ages=dict(trigger=a0, at_250=a250),
                          limits_ms=dict(pm=STALE_PM_MS, lighter=STALE_L_MS))
            elif not ok:
                self.skip(tid, w, side, "not_persistent", at_ms=PERSIST_MS, edge=e, min_edge=PERSIST_MIN_EDGE)
        except Exception as ex:  # noqa: BLE001
            self.persist[tid] = False
            self.err("persist_check", ex)

    def skip(self, tid, w, side, reason, **kw):
        self.counts[reason] += 1
        self.log("skip", id=tid, window=w.e, side=side, reason=reason, mode=self.mode, **kw)

    def gate(self):
        if self.halted:
            return "halted"
        if self.reconcile_failed:
            return "reconcile_fail"
        if self.stop_file.exists():
            return "kill_switch"
        if self.ledger.realised_today() <= -DAILY_LOSS_CAP_USD:
            return "loss_cap"
        if self.busy:
            return "busy"
        return None

    def hedge_quote(self, w, side):
        """(token, best ask) of the complementary Polymarket token: side a buys Down (ask = 1 - Up bid), b buys Up."""
        if not w.pbook.valid():
            return None, None
        pb, pa, _, _ = w.pbook.best()
        return (w.down_token, round(1 - pb, 4)) if side == "a" else (w.up_token, pa)

    def on_displayed(self, w, tid, side, lpx):
        persistent = self.persist.pop(tid, None)
        if persistent is False:
            return  # already skipped (`stale_*` or `not_persistent`) at +250 ms
        g = self.gate()
        if g:
            if g == "loss_cap" and self.counts["loss_cap"] == 0:
                self.log("loss_cap", realised_today=round(self.ledger.realised_today(), 4), cap=DAILY_LOSS_CAP_USD)
            return self.skip(tid, w, side, g)
        if persistent is None:  # fail closed: no +250 ms verdict for this trigger
            return self.skip(tid, w, side, "persist_unchecked")
        m = self.meta.get(w.e)
        if not m:
            return self.skip(tid, w, side, "meta_not_ready")
        ok, lp, ls = self.lquote_there(w, side, lpx)
        tok, ask = self.hedge_quote(w, side)
        if not ok or ask is None:
            return self.skip(tid, w, side, "quote_gone" if not ok else "pm_book_unknown")
        pm_min = m["pm_min_down" if side == "a" else "pm_min_up"]
        q, info, why = size_and_check(side, lpx, ls, ask, self.fpm_rate(w), m.get("l_min", 5.0), pm_min,
                                      self.ledger.open_cost("lighter"), self.ledger.open_cost("pm"), self.free["lighter"], self.free["pm"])
        if why:
            return self.skip(tid, w, side, why, **info)
        self.busy = tid
        self.counts["exec"] += 1
        self.spawn(self.execute(w, tid, side, lpx, q, tok, info))

    def fpm_rate(self, w):
        return w.pm_rate if w.pm_rate is not None else 0.05

    # ---------- execution ----------
    def halt(self, reason, **kw):
        self.halted = reason
        kw.setdefault("mode", self.mode)
        self.log("halt", reason=reason, **kw)

    async def execute(self, w, tid, side, lpx, q, tok, info):
        try:
            await self._execute(w, tid, side, lpx, q, tok, info)
        except Exception as ex:  # noqa: BLE001  an exception mid-pair: legs unknown -> stop opening
            self.err("execute", ex, traceback.format_exc()[-800:])
            self.halt("execute_crash", id=tid)
        finally:
            if not self.halted:
                self.busy = None

    async def _execute(self, w, tid, side, lpx, q, tok, info):
        base = dict(id=tid, window=w.e, side=side, mode=self.mode)
        r = await self.lv.ioc(w, side == "a", lpx, q)
        self.log("l_order", **base, **{k: v for k, v in r.items() if k != "venue"}, sizing=info)
        if r["status"] == "ambiguous":
            return self.halt("lighter_ambiguous", **base, coid=r.get("coid"))
        f = float(r.get("filled") or 0.0)
        if f <= 0:
            return
        lfill = float(r.get("avg_px") or lpx)
        lfee = fl(lfill) * f
        self.spend("lighter", f * lighter_unit_cost(side, lfill))
        self.log("l_fill", **base, symbol=l_symbol(w.e), market_id=w.mid, qty=f, px=lfill, fee_usd_model=round(lfee, 6), coid=r.get("coid"),
                 t_send=r.get("t_send"), t_ack=r.get("t_ack"), t_fill=r.get("t_fill"), exch=r.get("exch"))
        if self.mode == "live" and r.get("coid"):
            self.spawn(self.log_lighter_fees(base, w.mid, r["coid"]))
        hedged, pm_cost, pm_fee, attempts, ambiguous = await self.hedge_pm(w, tid, side, tok, f, base)
        if ambiguous:
            return self.halt("pm_ambiguous", **base, hedged=hedged)
        pq = min(hedged, f)
        if pq > 0:
            hp = pm_cost / hedged
            locked = (1 - lfill - hp if side == "a" else lfill - hp) * pq - fl(lfill) * pq - pm_fee * pq / hedged
            self.log("pair", **base, qty=pq, lighter_px=lfill, pm_px=round(hp, 6), lighter_fee_usd=round(fl(lfill) * pq, 6),
                     pm_fee_usd=round(pm_fee * pq / hedged, 6), attempts=attempts, pnl_usd=round(locked, 6), realised_usd=round(locked, 6),
                     pnl_per_share=round(locked / pq, 6), over_hedge=round(max(0.0, hedged - f), 4))
        rem = round(f - hedged, 4)
        if rem > 1e-9:
            await self.unwind(w, tid, side, lfill, rem, base)

    def spend(self, venue, usd):
        if self.mode == "dry":
            self.sim_bal[venue] -= usd
        if self.free[venue] is not None:
            self.free[venue] -= usd

    async def hedge_pm(self, w, tid, side, tok, f, base):
        hedged = cost = fee = 0.0
        m = self.meta[w.e]
        pm_min = m["pm_min_down" if side == "a" else "pm_min_up"]
        rate = self.fpm_rate(w)
        t_first = time.time()
        for attempt in range(1 + HEDGE_RETRIES):
            if attempt and time.time() - t_first > HEDGE_RETRY_WINDOW_S:
                break
            _, ask = self.hedge_quote(w, side)
            need = math.floor(f - hedged + 1e-9)  # whole shares: PM BUY amounts stay at <= 2 decimals
            if ask is None:
                self.log("pm_order", **base, attempt=attempt, status="skipped", reason="pm book unknown")
                await asyncio.sleep(0.1)
                continue
            px = pm_worst(ask)
            if need < pm_min or need * px < PM_MIN_MARKETABLE_USD:
                self.log("pm_order", **base, attempt=attempt, status="skipped", reason="remainder below PM minimum", need=need, limit=px)
                break
            r = await self.pv.fak_buy(tok, px, float(need))
            self.log("pm_order", **base, attempt=attempt, ask=ask, **{k: v for k, v in r.items() if k != "venue"})
            if r["status"] == "ambiguous":
                return hedged, cost, fee, attempt + 1, True
            if r["status"] == "filled" and r["shares"] > 0:
                sh, c = float(r["shares"]), float(r["cost"])
                fu = sh * rate * (c / sh) * (1 - c / sh)
                hedged, cost, fee = hedged + sh, cost + c, fee + fu
                self.spend("pm", c + fu)
                self.log("pm_fill", **base, attempt=attempt, token=tok, shares=sh, cost=round(c, 6), px=round(c / sh, 6),
                         fee_usd=round(fu, 6), fee_rate=rate, order_id=r.get("order_id"), t_send=r.get("t_send"), t_ack=r.get("t_ack"))
                if self.mode == "live" and r.get("order_id"):
                    self.spawn(self.log_fees(base, r["order_id"]))
            if hedged >= f - 1e-6 or math.floor(f - hedged + 1e-9) < 1:
                return hedged, cost, fee, attempt + 1, False
        return hedged, cost, fee, attempt + 1, False

    async def log_lighter_fees(self, base, mid, coid):
        await asyncio.sleep(5)
        try:
            self.log("fees", **base, venue="lighter", coid=coid, trades=await self.lv.fee_charged(mid, coid))
        except Exception as ex:  # noqa: BLE001
            self.err("fees", ex)

    async def log_fees(self, base, oid):
        await asyncio.sleep(5)
        try:
            self.log("fees", **base, venue="pm", order_id=oid, trades=await self.pv.fee_charged(oid))
        except Exception as ex:  # noqa: BLE001
            self.err("fees", ex)

    async def unwind(self, w, tid, side, lfill, rem, base):
        m = self.meta[w.e]
        lmin = m.get("l_min", 5.0)
        done = 0.0
        realised = 0.0
        if rem >= lmin and w.lbook.valid():
            lb, la, _, _ = w.lbook.best()
            # side a holds Up -> sell Up; side b holds Down (short Up) -> buy Up back
            px = round(max(0.01, lb - UNWIND_SLIP), 2) if side == "a" else round(min(0.99, la + UNWIND_SLIP), 2)
            r = await self.lv.ioc(w, side == "b", px, rem)
            self.log("l_order", **base, purpose="unwind", **{k: v for k, v in r.items() if k != "venue"})
            if r["status"] == "ambiguous":
                return self.halt("lighter_unwind_ambiguous", **base, coid=r.get("coid"))
            done = float(r.get("filled") or 0.0)
            if done > 0:
                upx = float(r.get("avg_px") or px)
                realised = ((upx - lfill) if side == "a" else (lfill - upx)) * done - (fl(lfill) + fl(upx)) * done
                # proceeds: side a sells Up at upx; side b buys Up back at upx = sells Down at 1 - upx (collateral released)
                self.spend("lighter", -(((upx if side == "a" else 1 - upx) - fl(upx)) * done))
                self.log("unwind", **base, qty=done, px=upx, entry_px=lfill, realised_usd=round(realised, 6), coid=r.get("coid"))
        residual = round(rem - done, 4)
        self.log("unhedged", **base, qty=rem, unwound=done, residual_held=residual, loss_usd=round(-realised, 6),
                 note="residual is held to settlement" if residual > 0 else None,
                 why_residual=("below Lighter minimum" if rem < lmin else "unwind short") if residual > 0 else None)

    # ---------- settlement ----------
    async def settle(self, w):
        await super().settle(w)  # the paper's settle: waits for both outcomes, logs `settle`
        rec = self.settled.pop(w.e, None) or {}
        try:
            await self.probe_settle(w, rec)
        except Exception as ex:  # noqa: BLE001
            self.err("probe_settle", ex)

    async def probe_settle(self, w, rec):
        legs = self.ledger.win.get(w.e)
        if legs:
            pm_w = rec.get("pm_winner")
            lo = (rec.get("lighter_outcome") or [None])[0]
            l_up = {"2": True, "1": False}.get(str(lo)) if lo is not None else None
            pm_up = {"UP": True, "DOWN": False}.get(pm_w)
            if l_up is None or pm_up is None:
                self.log("settle_unknown", window=w.e, mode=self.mode, pm_winner=pm_w, lighter_outcome=rec.get("lighter_outcome"),
                         note="legs stay open in the ledger (their cost stays reserved); check both venues by hand")
            else:
                pay = cost = 0.0
                redeem = []
                for s, (q, c) in legs["l"].items():
                    pay += q * (1.0 if (s == "a") == l_up else 0.0)
                    cost += c
                for tok, (sh, c, s) in legs["pm"].items():
                    won = (tok == w.up_token) == pm_up
                    pay += sh * (1.0 if won else 0.0)
                    cost += c
                    if won and sh > 0:
                        redeem.append(dict(token=tok, shares=sh, slug=w.slug))
                actual = pay - cost
                settle_realised = actual - legs["locked"]
                self.log("probe_settle", window=w.e, mode=self.mode, pm_winner=pm_w, lighter_up=l_up, venues_agree=l_up == pm_up,
                         payout_usd=round(pay, 6), cost_usd=round(cost, 6), pnl_usd=round(actual, 6), locked_usd=round(legs["locked"], 6),
                         realised_usd=round(settle_realised, 6))
                lpay = sum(q * (1.0 if (s == "a") == l_up else 0.0) for s, (q, _) in legs["l"].items())
                if self.mode == "dry":
                    self.sim_bal["lighter"] += lpay
                if redeem:
                    self.log("redeem_due", window=w.e, mode=self.mode, winners=redeem, usd=round(sum(r["shares"] for r in redeem), 4),
                             note="Polymarket winners are not auto-redeemed: claim in the Polymarket UI")
                if self.mode == "live":
                    self.spawn(self.confirm_lighter_settled(w, lpay))
        if self.mode == "live":
            self.spawn(self.redeem_sweep(w))

    async def confirm_lighter_settled(self, w, lpay):
        sym = l_symbol(w.e)
        for _ in range(20):
            try:
                b = await self.lv.balance()
                if abs(b["positions"].get(w.mid, 0.0)) < 1e-9:
                    return self.log("lighter_settled", window=w.e, symbol=sym, position_gone=True, expected_payout=round(lpay, 6),
                                    free=b["free"], collateral=b["collateral"])
            except Exception as ex:  # noqa: BLE001
                self.err("lighter_settled", ex)
            await self.sleep(30)
        self.log("lighter_settled", window=w.e, symbol=sym, position_gone=False, expected_payout=round(lpay, 6),
                 note="position still on the account 10 min after the paper settle")

    async def redeem_sweep(self, w):
        """Every window: all redeemable winners still on the Polymarket account (claim them in the UI)."""
        try:
            pos = await self.pv.positions()
            due = [dict(token=str(p.get("asset")), shares=p.get("size"), value=p.get("currentValue"), title=p.get("title"))
                   for p in pos if p.get("redeemable") and float(p.get("currentValue") or 0) > 0]
            if due:
                self.log("redeem_due", window=w.e, mode=self.mode, sweep=True, winners=due, usd=round(sum(float(d["value"] or 0) for d in due), 4),
                         note="claim in the Polymarket UI")
        except Exception as ex:  # noqa: BLE001
            self.err("redeem_sweep", ex)

    async def settle_stale(self):
        """Ledger windows that ended while the probe was down (or whose paper settle gave up): settle from Gamma and
        Lighter's REST outcome, every 5 min, so their open cost does not stay reserved forever."""
        while not self.stop.is_set():
            for e in [e for e in list(self.ledger.win) if e + 900 + 600 < time.time() and e not in self.win]:
                try:
                    rec, w = await asyncio.to_thread(stale_outcome, e, self.ledger.win[e].get("mid"))
                    if rec:
                        self.log("stale_settle", window=e, mode=self.mode, **rec)
                        await self.probe_settle(w, rec)
                except Exception as ex:  # noqa: BLE001
                    self.err("settle_stale", ex)
            await self.sleep(300)

    # ---------- balances ----------
    async def balances(self):
        while not self.stop.is_set():
            try:
                lb, pb = await self.lv.balance(), await self.pv.balance()
                if not self.busy:  # a snapshot mid-pair would double-count the pair's spend
                    self.free = {"lighter": lb["free"], "pm": pb["free"]}
                self.log("balance", mode=self.mode, lighter=lb.get("free"), lighter_collateral=lb.get("collateral"), pm=pb.get("free"),
                         lighter_positions=lb.get("positions"),
                         open_cost_lighter=round(self.ledger.open_cost("lighter"), 4), open_cost_pm=round(self.ledger.open_cost("pm"), 4),
                         realised_today=round(self.ledger.realised_today(), 4), busy=self.busy, halted=self.halted,
                         reconcile_failed=bool(self.reconcile_failed), kill_switch=self.stop_file.exists(), skips=dict(self.counts))
            except Exception as ex:  # noqa: BLE001
                self.err("balances", ex)
            await self.sleep(BALANCE_EVERY_S)

    # ---------- start / stop ----------
    async def startup_reconcile(self):
        lb = await self.lv.balance()
        lo = await self.lv.open_orders()
        pp = await self.pv.positions()
        po = await self.pv.open_orders()
        problems, warns = reconcile(self.ledger, lb["positions"], lo, pp, po)
        self.log("reconcile", ok=not problems, problems=problems, warnings=warns, lighter_positions=lb["positions"],
                 pm_positions=len(pp), expected=self.ledger.expected())
        if problems:
            self.reconcile_failed = problems
            self.log("reconcile_fail", problems=problems, note="refusing to trade; fix the accounts by hand, then restart")

    async def run(self):
        self.ledger.replay(self.log.root)
        self.log("probe_start", mode=self.mode, realised_today=round(self.ledger.realised_today(), 4),
                 open_cost_lighter=round(self.ledger.open_cost("lighter"), 4), open_cost_pm=round(self.ledger.open_cost("pm"), 4),
                 limits=dict(daily_loss_cap=DAILY_LOSS_CAP_USD, open_pm=MAX_OPEN_COST_PM_USD, open_lighter=MAX_OPEN_COST_LIGHTER_USD,
                             leg=MAX_LEG_COST_USD), stop_file=str(self.stop_file),
                 pm_clob_version=getattr(self.pv, "server_version", None))
        if self.mode == "live":
            try:
                await self.startup_reconcile()
            except Exception as ex:  # noqa: BLE001
                self.err("reconcile", ex)
                self.reconcile_failed = [f"reconcile could not run: {ex!r}"[:300]]
                self.log("reconcile_fail", problems=self.reconcile_failed)
        else:
            self.free = dict(self.sim_bal)
        bt = asyncio.get_running_loop().create_task(self.balances())
        st = asyncio.get_running_loop().create_task(self.settle_stale())
        await super().run()
        for _ in range(150):  # finish a hedge in progress (<= 15 s)
            if not self.busy or self.halted:
                break
            await asyncio.sleep(0.1)
        bt.cancel()
        st.cancel()
        self.log("probe_stop", busy=self.busy, halted=self.halted, counts=dict(self.counts))


def stale_outcome(e, mid):
    """-> (paper-style settle record, minimal window) once both venues have an outcome, else (None, None)."""
    slug = f"btc-updown-15m-{e}"
    ms = P.http_json(P.GAMMA + f"closed=true&slug={slug}")
    if not ms or mid is None:
        return None, None
    m = ms[0]
    names = [n.upper() for n in json.loads(m["outcomes"])]
    pr = [float(v) for v in json.loads(m["outcomePrices"])]
    if max(pr) < 0.99:
        return None, None
    ob = P.http_json(P.L_REST + f"orderBooks?market_id={mid}")["order_books"][0]
    if str(ob.get("outcome")) not in ("1", "2"):
        return None, None
    w = P.Window(e, mid)
    toks = json.loads(m["clobTokenIds"])
    w.up_token, w.down_token, w.slug = toks[names.index("UP")], toks[names.index("DOWN")], slug
    return dict(pm_winner=names[pr.index(max(pr))], lighter_outcome=[str(ob["outcome"]), str(ob.get("status"))]), w


# ======================= --mode check (operator, before go-live) =======================
def current_btc15m(http=None, now=None):
    """(window epoch, Lighter market id) of the BTC 15m window in progress, else the next one (Lighter events listing,
    the request paper.discover makes)."""
    http = http or P.http_json
    now = time.time() if now is None else now
    d = http(P.L_REST + f"events?active=true&sort=start_at&order=asc&limit=40&tag={P.TAG}")
    evs = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
    best = None
    for ev in evs:
        try:
            ob = ev["order_books"][0]
            m = re.match(r"BTC-UPDOWN-15M-(\d+)$", ob["symbol"], re.I)
            e, mid = int(m.group(1)), int(ob["market_id"])
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        if e + 900 > now and (best is None or e < best[0]):
            best = (e, mid)
    return best


def _brief_lighter_order(o):
    return {k: o.get(k) for k in ("market_index", "is_ask", "price", "remaining_base_amount", "initial_base_amount", "type",
                                  "time_in_force", "status", "client_order_index")}


def _brief_pm_order(o):
    return {k: o.get(k) for k in ("market", "asset_id", "side", "price", "original_size", "size_matched", "order_type", "status")}


def _brief_pm_position(p):
    return {k: p.get(k) for k in ("slug", "title", "outcome", "size", "avgPrice", "curPrice", "redeemable", "conditionId")}


async def account_check(lv, pv, ledger, http=None, now=None):
    """Everything the operator needs before go-live, read-only: authenticates (the client constructors), reads balances,
    positions, open orders, account type, market tradability, client versions and allowance. Places and cancels nothing
    (lv/pv order methods are never called). -> dict with `ok` and `problems`."""
    from importlib.metadata import version

    http = http or P.http_json
    out, problems, notes = {}, [], []

    async def part(name, coro):
        try:
            return await coro
        except Exception as ex:  # noqa: BLE001
            problems.append(f"{name}: {ex!r}"[:300])
            return None

    # ---- Lighter
    acct = await part("lighter account", lv.account_info())
    bal = await part("lighter balance", lv.balance())
    lo = await part("lighter open orders", lv.open_orders())
    out["lighter"] = dict(account=acct, balance=None if bal is None else bal.get("free"),
                          collateral=None if bal is None else bal.get("collateral"),
                          positions=None if bal is None else bal.get("positions"),
                          open_orders=None if lo is None else [_brief_lighter_order(o) for o in lo])
    cur = None
    try:
        cur = current_btc15m(http, now)
    except Exception as ex:  # noqa: BLE001
        problems.append(f"lighter events: {ex!r}"[:300])
    trad = dict(window=None, verdict="no")
    if cur:
        e, mid = cur
        trad.update(window=e, market_id=mid, symbol=l_symbol(e))
        ob = None
        try:
            ob = (http(P.L_REST + f"orderBooks?market_id={mid}").get("order_books") or [None])[0]
        except Exception as ex:  # noqa: BLE001
            problems.append(f"lighter orderBooks: {ex!r}"[:300])
        if ob:
            trad.update(status=ob.get("status"), market_type=ob.get("market_type"), min_base_amount=ob.get("min_base_amount"),
                        taker_fee=ob.get("taker_fee"), is_taker_fee_enabled=ob.get("is_taker_fee_enabled"))
        mi = await part("lighter market registration", lv.market_info(mid))
        trad["signer_market"] = mi
        checks = dict(market_active=bool(ob) and str(ob.get("status")).lower() == "active", signer_registered=bool(mi),
                      account_read=acct is not None, auth_read=lo is not None,
                      balance_for_one_clip=bal is not None and (bal.get("free") or 0) >= MAX_LEG_COST_USD)
        trad["checks"] = checks
        trad["verdict"] = "yes_by_api" if all(checks.values()) else "no"
        for k, v in checks.items():
            if not v:
                problems.append(f"lighter btc15m not tradable: {k}")
    else:
        problems.append("lighter: no active BTC 15m window found")
    trad["note"] = ("Lighter exposes no per-account permission for binary markets; 'yes_by_api' means every API-side check "
                    "passed. Only the first live order proves the account may trade it (a rejection reason is logged).")
    out["lighter_btc15m"] = trad

    # ---- Polymarket
    inst = None
    try:
        inst = version("py_clob_client_v2")
    except Exception:  # noqa: BLE001
        pass
    sv = getattr(pv, "server_version", None)
    out["pm_client"] = dict(py_clob_client_v2=inst, required=PM_CLIENT_VERSION, clob_server_version=sv)
    if inst != PM_CLIENT_VERSION:
        problems.append(f"py_clob_client_v2 {inst} != {PM_CLIENT_VERSION}")
    pbal = await part("pm balance", pv.balance())
    al = await part("pm allowance", pv.allowance())
    co = await part("pm closed-only", pv.closed_only())
    po = await part("pm open orders", pv.open_orders())
    pp = await part("pm positions", pv.positions())
    alw = dict((al or {}).get("allowances") or {})
    if al and "allowance" in al and not alw:
        alw = {"(single)": al["allowance"]}
    need = None
    if sv is not None:
        name, addr = pv.spender(sv)
        have = next((v for k, v in alw.items() if str(k).lower() == str(addr).lower()), None)
        if have is None and "(single)" in alw:
            have = alw["(single)"]
        try:
            have_usd = float(have) / 1e6 if have is not None else 0.0
        except ValueError:
            have_usd = 0.0
        need = dict(spender=name, address=addr, allowance_usd=None if have_usd > 1e12 else round(have_usd, 2),
                    unlimited=have_usd > 1e12, ok=have_usd >= MAX_OPEN_COST_PM_USD)
        if not need["ok"]:
            problems.append(f"pm allowance to {name} is ${have_usd:.2f} < ${MAX_OPEN_COST_PM_USD:.0f} (approve USDC for that exchange)")
    closed = co.get("closed_only") if isinstance(co, dict) else co
    if closed:
        problems.append("pm account is in closed-only mode")
    out["pm"] = dict(balance=None if pbal is None else pbal.get("free"), allowance=need,
                     allowances_raw={k: ("unlimited" if _big(v) else v) for k, v in alw.items()}, closed_only=closed,
                     open_orders=None if po is None else [_brief_pm_order(o) for o in po],
                     positions=None if pp is None else [_brief_pm_position(p) for p in pp])

    # ---- would a live start refuse?
    if bal is not None and lo is not None and pp is not None and po is not None:
        rp, rw = reconcile(ledger, bal.get("positions") or {}, lo, pp, po)
        out["reconcile"] = dict(ok=not rp, problems=rp, warnings=rw, expected=ledger.expected())
        problems += [f"reconcile: {x}" for x in rp]
        if rw:
            notes += rw
    out["limits"] = dict(daily_loss_cap=DAILY_LOSS_CAP_USD, open_pm=MAX_OPEN_COST_PM_USD, open_lighter=MAX_OPEN_COST_LIGHTER_USD,
                         leg=MAX_LEG_COST_USD, persist_min_edge=PERSIST_MIN_EDGE)
    out.update(ok=not problems, problems=problems, notes=notes, placed_orders=0, cancelled_orders=0)
    return out


def _big(v):
    try:
        return float(v) > 1e18
    except (TypeError, ValueError):
        return False


def build_live(red):
    bad = verify_pins()
    if bad:
        raise SystemExit("refusing live mode, reused code changed: " + "; ".join(bad))
    sec = load_secrets()
    for v in sec.values():
        red.add(v)
    sys.stdout, sys.stderr = ScrubStream(sys.stdout, red), ScrubStream(sys.stderr, red)
    try:
        lv = LighterLive(sec)
        pv = PMLive(sec, red)
    finally:
        sec.clear()
    return lv, pv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=("dry", "live", "check"), default="dry",
                    help="check: load SSM like live, authenticate, print accounts/markets/allowance; places nothing")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--minutes", type=float, default=None)
    a = ap.parse_args(argv)
    out = a.out or (OUT_LIVE if a.mode == "live" else OUT_DRY)
    red = Redactor()

    def hook(t, v, tb):
        sys.__stderr__.write(red.scrub("".join(traceback.format_exception(t, v, tb))))

    sys.excepthook = hook
    lv = pv = None
    if a.mode == "check":
        lv, pv = build_live(red)
        ledger = Ledger("live")
        ledger.replay(OUT_LIVE)
        res = asyncio.run(account_check(lv, pv, ledger))
        print(json.dumps(red.obj(res), indent=1, default=str))
        return 0 if res["ok"] else 1
    if a.mode == "live":
        lv, pv = build_live(red)
    probe = Probe(ProbeLog(out, red), a.mode, time.time() + a.minutes * 60 if a.minutes else None, lighter=lv, pm=pv)
    asyncio.run(probe.run())


if __name__ == "__main__":
    sys.exit(main())
