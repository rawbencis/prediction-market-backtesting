"""maker_strict: dsim.maker with one change (generated from dsim.maker source 2026-11-16 by gen in overnight notes):
a print THROUGH our price fills at most (print size - queue ahead) instead of our whole remaining order.
Reason: our simulated order is absent from the recorded book, so a small taker print that traded at a worse level
would only have taken its own size from us. Everything else is dsim's maker verbatim."""
import numpy as np

from dsim import REBATE_SHARE, fee, state  # noqa: F401


def maker_strict(m, t_arr, tok, price, size, t_cxl, model="pess"):
    """Post-only BUY `tok` at price (1e-4) arriving t_arr, cancel arriving t_cxl (not held).
    -> dict(status, fills=[...]) ; status 'rejected' if it would cross on arrival."""
    s, q = (0, price) if tok == 0 else (1, 10000 - price)
    better = (lambda a, c: a > c) if s == 0 else (lambda a, c: a < c)
    B, P = m["book"], m["pr"]
    i = state(m, t_arr, strict=False)
    if i < 0 or (B["b"][i] == 0 and B["k"][i] == 0):
        return dict(status="nobook", fills=[])
    best, opp = (int(B["b"][i]), int(B["k"][i])) if s == 0 else (int(B["k"][i]), int(B["b"][i]))
    if opp and (q >= opp if s == 0 else q <= opp):
        return dict(status="rejected", fills=[])
    if best == 0 or better(q, best):
        ahead, disp = 0.0, 0.0
    elif q == best:
        disp = float(B["bs"][i] if s == 0 else B["ks"][i])
        ahead = disp
    else:
        ahead, disp = None, None  # behind the top: unknown until our level becomes top
    if model == "front":
        ahead = 0.0
    left, fills, since = size, [], 0.0

    def fill(x, n, ah):
        nonlocal left
        n = min(left, n)
        if n > 1e-9:
            left -= n
            fills.append(dict(x=int(x), tok=tok, px=price, size=n, fee=0.0, rebate=REBATE_SHARE * fee(price, n), maker=1,
                              ahead=float(ah if ah is not None else -1)))

    jb = i + 1
    jp = int(np.searchsorted(P["x"], t_arr, "right"))
    nb, npr = len(B["x"]), len(P["x"])
    while left > 1e-9:
        tb = B["x"][jb] if jb < nb and B["x"][jb] < t_cxl else None  # book rows strictly before the cancel
        tp = P["x"][jp] if jp < npr and P["x"][jp] <= t_cxl else None  # prints up to and incl. the cancel ms
        if tb is None and tp is None:
            break
        if tp is not None and (tb is None or tp <= tb):  # print first on ties: the book row is the post-trade state
            j = jp
            jp += 1
            if bool(P["hb"][j]) != (s == 0):
                continue
            px, n = int(P["up"][j]), float(P["sz"][j])
            if better(q, px):  # through our price. STRICT: our order is a phantom (absent from history), so the taker's
                # print size n is all there was to trade: it fills at most n minus what is ahead of us (dsim fills all of `left`)
                a0 = ahead if ahead is not None else 0.0
                d = min(a0, n)
                if ahead is not None:
                    ahead -= d
                fill(P["x"][j], n - d, a0)
            elif px == q:
                since += n
                if ahead is None:
                    ahead = 0.0 if model == "front" else (disp if disp is not None else None)
                if ahead is None:
                    continue
                a0 = ahead
                d = min(ahead, n)
                ahead -= d
                fill(P["x"][j], n - d, a0)
            continue
        j = jb
        jb += 1
        if B["b"][j] == 0 and B["k"][j] == 0:  # dropped connection: book unknown, keep state
            disp, since = None, 0.0
            continue
        nbest, nopp = (int(B["b"][j]), int(B["k"][j])) if s == 0 else (int(B["k"][j]), int(B["b"][j]))
        nsz = float(B["bs"][j] if s == 0 else B["ks"][j])
        if nopp and (q >= nopp if s == 0 else q <= nopp):  # a resting order crossed our synthetic order: it would have hit us
            oz = float(B["ks"][j] if s == 0 else B["bs"][j])
            fill(B["x"][j], oz - (ahead or 0.0), ahead)
        if nbest == 0 or better(q, nbest):  # our level emptied (or we are alone at the best)
            ahead, disp = 0.0, 0.0
        elif nbest == q:
            if model == "front":
                ahead = 0.0
            elif ahead is None:
                ahead = nsz
            elif disp is not None and model == "prop":
                cxl = disp - nsz - since
                D = disp - since
                if cxl > 0 and D > 0:
                    ahead = max(0.0, ahead - cxl * ahead / D)
            ahead = min(ahead, nsz)
            disp = nsz
        else:
            disp = None  # a better level exists; our level not visible
        since = 0.0
    return dict(status="ok", fills=fills)
