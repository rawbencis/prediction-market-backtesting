"""Same loop as ~/onetouch-mm/research/onetouch_mm/replay.py, but a truncated .gz tail (crashed hour) ends that file instead of the run."""
import glob
import gzip
import json
import sys
import zlib
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/onetouch-mm/research/onetouch_mm")
from engine import Engine, handle_meta, handle_pm  # noqa: E402

eng = Engine(**dict(a.split("=") for a in sys.argv[2:]))
for fn in sorted(glob.glob(f"{sys.argv[1]}/rec_*.jsonl.gz")):
    n = 0
    try:
        with gzip.open(fn, "rt") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                n += 1
                t, s, d = r["t"], r["s"], r["d"]
                if s == "pm":
                    handle_pm(eng, t, d)
                elif s == "bn":
                    eng.on_spot(t, d[0], d[1])
                else:
                    handle_meta(eng, t, d)
    except (EOFError, zlib.error, OSError) as ex:
        print(f"truncated: {fn.rsplit('/', 1)[1]} after {n} rows ({type(ex).__name__})")
print("params", eng.p)
print(json.dumps(eng.summary(), indent=1))
print(f"{'day':10} {'n':>5} {'picked':>6} {'volume':>9} {'spread':>8} {'settle':>8} {'net':>8}")
for day, v in sorted(eng.daily().items()):
    ds = datetime.fromtimestamp(day * 86400, timezone.utc).strftime("%Y-%m-%d")
    print(f"{ds:10} {v['n']:5.0f} {v['picked']:6.0f} {v['vol']:9.0f} {v['spread']:8.2f} {v['settle']:8.2f} {v['spread'] + v['settle']:8.2f}")
