"""Summarise order_probe results from its CSV and/or printed dict lines in logs, optionally by UTC hour."""
import ast, csv, sys, time
import numpy as np

COLS = ("post_rtt", "cxl_rtt", "send_to_ex_visible", "send_to_rx_visible", "cxl_to_ex_gone", "cxl_to_rx_gone")


def rows_from(paths):
    out = {}
    for p in paths:
        if p.endswith(".csv"):
            for r in csv.DictReader(open(p)):
                out[int(r["t_send"])] = r
        else:
            for line in open(p):
                if line.startswith("{'i'"):
                    r = ast.literal_eval(line.strip())
                    out[int(r["t_send"])] = r
    return [out[k] for k in sorted(out)]


def pcts(v):
    v = np.array([float(x) for x in v if x not in ("", None)])
    if not len(v):
        return "n=0"
    q = np.percentile(v, [10, 50, 90, 99])
    return f"n={len(v):3d} p10={q[0]:7.1f} p50={q[1]:7.1f} p90={q[2]:7.1f} p99={q[3]:7.1f}"


def main(paths, by_hour=False):
    rows = rows_from(paths)
    fills = sum(float(r["matched"] or 0) > 0 for r in rows)
    notc = sum(str(r["status"]).upper() not in ("CANCELED", "CANCELLED") for r in rows)
    print(f"cycles={len(rows)} fills={fills} status_not_CANCELED={notc}")
    groups = {"all": rows}
    if by_hour:
        for r in rows:
            groups.setdefault(time.strftime("%H", time.gmtime(int(r["t_send"]) / 1000)), []).append(r)
    for g, rr in groups.items():
        print(f"-- {g}")
        for c in COLS:
            print(f"  {c:20s} {pcts([r[c] for r in rr])}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if x != "--by-hour"]
    main(a, "--by-hour" in sys.argv)
