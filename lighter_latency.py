import asyncio, json, time, socket, statistics as st, subprocess, sys
import websockets

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 180
MKTS = sys.argv[2].split(',') if len(sys.argv) > 2 else ['0', '1']
URL = 'wss://mainnet.zklighter.elliot.ai/stream?readonly=true'
HOSTS = [('lighter', 'mainnet.zklighter.elliot.ai'), ('polymarket (London)', 'clob.polymarket.com'),
         ('binance usdm (Tokyo)', 'fstream.binance.com'), ('hyperliquid (Tokyo)', 'api.hyperliquid.xyz')]

def q(x, p): return sorted(x)[int(p * (len(x) - 1))] if x else None

def tcp(host, n=20):
    ip = socket.gethostbyname(host)
    try: rdns = socket.gethostbyaddr(ip)[0]
    except Exception: rdns = '-'
    r = []
    for _ in range(n):
        s = socket.socket(); s.settimeout(3); t = time.perf_counter()
        try: s.connect((ip, 443)); r.append((time.perf_counter() - t) * 1e3)
        except Exception: pass
        finally: s.close()
        time.sleep(0.05)
    return ip, rdns, q(r, .5), min(r) if r else None

def to_ms(v):
    v = float(v)
    if v > 1e17: return v / 1e6
    if v > 1e14: return v / 1e3
    if v > 1e11: return v
    if v > 1e9: return v * 1e3
    return None

def walk(o, out, key=''):
    if isinstance(o, dict):
        for k, v in o.items(): walk(v, out, k)
    elif isinstance(o, list):
        for x in o[:50]: walk(x, out, key)
    elif 'time' in key.lower() and (isinstance(o, (int, float)) or (isinstance(o, str) and o.isdigit())):
        ms = to_ms(o)
        if ms: out.append((key, ms))

async def ws_probe():
    lag, rtt, first = {}, [], {}
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        for m in MKTS:
            for ch in ('trade', 'order_book'):
                await ws.send(json.dumps({'type': 'subscribe', 'channel': f'{ch}/{m}'}))
        async def pinger():
            while True:
                t = time.perf_counter(); w = await ws.ping(); await w
                rtt.append((time.perf_counter() - t) * 1e3); await asyncio.sleep(1)
        pt = asyncio.create_task(pinger())
        end = time.time() + DUR
        while time.time() < end:
            try: raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError: continue
            now = time.time() * 1e3
            msg = json.loads(raw)
            ch = str(msg.get('channel', msg.get('type', '?'))).split(':')[0].split('/')[0]
            first.setdefault(ch, raw[:400])
            ts = []; walk(msg, ts)
            for k, ms in ts:
                d = now - ms
                if -5e3 < d < 6e4: lag.setdefault((ch, k), []).append(d)
        pt.cancel()
    return lag, rtt, first

def main():
    try: print(subprocess.run(['chronyc', 'tracking'], capture_output=True, text=True).stdout)
    except Exception: print('chronyc not available: clock offset unknown')
    print(f"{'host':24}{'ip':17}{'tcp p50':>9}{'tcp min':>9}  rdns")
    for name, h in HOSTS:
        ip, rd, p50, mn = tcp(h)
        print(f"{name:24}{ip:17}{p50 or -1:9.1f}{mn or -1:9.1f}  {rd}")
    lag, rtt, first = asyncio.run(ws_probe())
    print(f"\nlighter ws ping rtt ms: n={len(rtt)} p10={q(rtt,.1)} p50={q(rtt,.5)} p90={q(rtt,.9)}")
    print('\nrecv - exchange ts (ms), by channel/field:')
    for (ch, k), v in sorted(lag.items()):
        print(f"  {ch:20}{k:22} n={len(v):6} min={min(v):8.1f} p10={q(v,.1):8.1f} p50={q(v,.5):8.1f} p90={q(v,.9):8.1f}")
    print('\nfirst raw message per channel:')
    for ch, r in first.items(): print(f"  {ch}: {r}")

main()
