"""Run wallet_dissect.py (copied unchanged from branch research/wallet-dissect) with a global throttle:
>= 0.55 s between any two HTTP requests (<= 1.8 req/s), covering pagination, retries and the Gamma fallback loop.
usage: wd_throttled.py <wallet_dissect args...>"""
import runpy, sys, time, urllib.request
from pathlib import Path
_orig, _last = urllib.request.urlopen, [0.0]
def urlopen(*a, **k):
    w = 0.55 - (time.monotonic() - _last[0])
    if w > 0:
        time.sleep(w)
    _last[0] = time.monotonic()
    return _orig(*a, **k)
urllib.request.urlopen = urlopen
here = Path(__file__).parent
sys.path.insert(0, str(here))
sys.argv = [str(here / "wallet_dissect.py")] + sys.argv[1:]
runpy.run_path(str(here / "wallet_dissect.py"), run_name="__main__")
