"""Standalone quote-proxy research. Not a native L2 or maker-fill backtest."""
import argparse
import csv
import hashlib
import itertools
import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def candidates():
    return sorted(set((x, b, s, ttl) for x, b, s, ttl in itertools.product(
        (.60, .65, .70, .75, .80), (.20, .25, .30, .35, .40),
        (.50, .60, .70, .80), (30, 60, 300)) if b < x and b < s))


def valid(row):
    try:
        q = tuple(float(row[k]) for k in ('bid_price', 'ask_price', 'bid_size', 'ask_size'))
        b, a, bs, az = q
        if all(math.isfinite(v) for v in q) and 0 <= b < a <= 1 and min(bs, az) > 0:
            return q
    except (ValueError, TypeError):
        pass
    return None


def load(root, epoch):
    import pyarrow.parquet as pq
    events = {}
    slug = f'btc-updown-5m-{epoch}'
    for side in ('UP', 'DOWN'):
        path = root / f'{slug}_{side}.parquet'
        if path.stat().st_size > 512 * 1024**2:
            raise ValueError(f'oversize input: {path}')
        with pq.ParquetFile(path) as f:
            if f.metadata.num_rows > 2_000_000:
                raise ValueError(f'row limit: {path}')
            last = -1
            for batch in f.iter_batches(batch_size=4096, columns=[
                'timestamp_us', 'bid_price', 'ask_price', 'bid_size', 'ask_size'], use_threads=False):
                for r in batch.to_pylist():
                    ts = int(r['timestamp_us'])
                    if ts < last:
                        raise ValueError(f'unsorted timestamps: {path}')
                    last = ts
                    t = (ts - epoch * 1_000_000) / 1_000_000
                    if 0 <= t < 300:
                        update = events.setdefault(t, {})
                        if side in update:
                            raise ValueError(f'duplicate side/timestamp: {path}: {ts}')
                        update[side] = valid(r)
                        if len(events) > 500_000:
                            raise ValueError('event limit')
    if not events:
        raise ValueError(f'empty market: {slug}')
    return sorted(events.items())


def replay(events, grid, latency=.25, gap=5., age=2., size=1., winner=None):
    states = {c: dict(status='waiting', side='', trigger=None, entry=None, exit=None) for c in grid}
    book, seen, previous, last = {}, {}, None, None
    for t, update in events:
        interrupted = t > gap if last is None else t-last > gap
        last = t
        for side, q in update.items():
            book[side], seen[side] = q, t
        good = all(book.get(s) is not None and t-seen[s] <= age for s in ('UP', 'DOWN'))
        mids = {s: (book[s][0]+book[s][1])/2 for s in book} if good else None
        for c, r in states.items():
            x, b, s, ttl = c
            status = r['status']
            if status not in ('waiting', 'pending', 'entered'):
                continue
            if interrupted or (not good and t > age):
                r['status'] = 'censored'
                continue
            if not good:
                continue
            if status == 'waiting':
                crossed = [side for side in ('UP', 'DOWN') if previous and previous[side] < x <= mids[side]]
                if len(crossed) > 1:
                    r['status'] = 'censored'
                elif crossed and t <= 240:
                    r.update(status='pending', side=crossed[0], trigger=t)
                continue
            if status == 'pending' and t >= r['trigger']+ttl:
                r['status'] = 'expired'
                continue
            side = r['side']
            if side not in update:
                continue
            bid, ask, bs, az = book[side]
            if status == 'pending' and t >= r['trigger']+latency and ask < b and az >= size:
                r.update(status='entered', entry=t)
            elif status == 'entered' and t >= r['entry']+latency and bid > s and bs >= size:
                r.update(status='exited', exit=t)
        previous = mids
    for c, r in states.items():
        x, b, s, ttl = c
        status = r['status']
        if status in ('waiting', 'pending', 'entered') and (last is None or 300-last > gap):
            r['status'] = status = 'censored'
        if status == 'exited':
            lo = hi = size*(s-b)
        elif status == 'entered' and winner is not None:
            lo = hi = size*((winner == r['side'])-b)
            r['status'] = 'settled'
        elif status in ('entered', 'censored'):
            lo, hi = -size*b, size*(1-b)
        else:
            lo = hi = 0.
        r.update(lo=lo, hi=hi)
    return states


def score(rows):
    equity = peak = dd = 0.
    for r in rows:
        equity += r['lo']
        peak = max(peak, equity)
        dd = max(dd, peak-equity)
    return equity-.5*dd


def choose(history, grid, train, validation, minimum, topk):
    def rank(c, days):
        return statistics.median(score(history[d][c]) for d in days)
    eligible = [c for c in grid if all(sum(r['entry'] is not None for d in days
        for r in history[d][c]) >= minimum for days in (train, validation))]
    top = sorted(eligible, key=lambda c: (-rank(c, train), c))[:topk]
    top.sort(key=lambda c: (-rank(c, validation), -rank(c, train), c))
    return top[0] if top and rank(top[0], validation) > 0 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--start', type=date.fromisoformat)
    ap.add_argument('--end', type=date.fromisoformat, help='exclusive UTC date')
    ap.add_argument('--train-days', type=int, default=10)
    ap.add_argument('--validation-days', type=int, default=4)
    ap.add_argument('--holdout-days', type=int, default=7)
    ap.add_argument('--min-entries', type=int, default=20)
    ap.add_argument('--top-k', type=int, default=20)
    ap.add_argument('--settlements', type=Path, help='CSV: slug,winner,available_at; timezone-aware ISO timestamp')
    a = ap.parse_args()
    if min(a.train_days, a.validation_days, a.holdout_days, a.min_entries, a.top_k) < 1:
        ap.error('all counts must be positive')
    epochs = sorted(int(f.stem.split('_')[0].rsplit('-', 1)[1]) for f in a.data.glob('btc-updown-5m-*_UP.parquet'))
    if not epochs:
        ap.error('no UP Parquet files in data directory')
    first = datetime.fromtimestamp(epochs[0], timezone.utc).date()
    last = datetime.fromtimestamp(epochs[-1]+300, timezone.utc)
    start = a.start or (first if epochs[0] % 86400 == 0 else first+timedelta(days=1))
    end = a.end or last.date()
    days = [start+timedelta(days=i) for i in range((end-start).days)]
    window = a.train_days+a.validation_days
    if len(days) <= window+a.holdout_days:
        ap.error('insufficient complete days: need training + validation + development OOS + holdout')
    holdout = days[-a.holdout_days]
    # Fail before starting if any scheduled market is missing.
    for d in days:
        epoch = int(datetime.combine(d, datetime.min.time(), timezone.utc).timestamp())
        for ts in range(epoch, epoch+86400, 300):
            for side in ('UP', 'DOWN'):
                if not (a.data/f'btc-updown-5m-{ts}_{side}.parquet').is_file():
                    ap.error(f'missing scheduled input: btc-updown-5m-{ts}_{side}.parquet')
    settlements = {}
    if a.settlements:
        with a.settlements.open() as f:
            for r in csv.DictReader(f):
                available = datetime.fromisoformat(r['available_at'])
                if available.tzinfo is None or r['winner'] not in ('UP', 'DOWN') or r['slug'] in settlements:
                    raise ValueError('invalid settlement or duplicate slug')
                settlements[r['slug']] = (r['winner'], available.timestamp())
    a.out.mkdir(parents=True, exist_ok=False)
    grid = candidates()
    manifest = {k: str(v) for k, v in vars(a).items()}
    manifest.update(execution='QUOTE_PROXY_NOT_MAKER_FILLS', start=str(start), end=str(end),
        holdout_start=str(holdout), source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        candidates=len(grid), latency_s=.25, max_gap_s=5, max_age_s=2, size=1, maker_fees=0)
    (a.out/'manifest.json').write_text(json.dumps(manifest, indent=2))
    history, frozen, daily = {}, None, []
    with (a.out/'ledger.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['day','slug','params','status','side','trigger','entry','exit','lo','hi'])
        w.writeheader()
        for i, d in enumerate(days):
            if d <= holdout and i >= window:
                past = days[i-window:i]
                selected = choose(history, grid, past[:a.train_days], past[a.train_days:], a.min_entries, a.top_k)
                if d == holdout:
                    frozen = selected
            elif d > holdout:
                selected = frozen
            else:
                selected = None
            active = grid if d < holdout else ([selected] if selected else [])
            bucket = {c: [] for c in active}
            epoch = int(datetime.combine(d, datetime.min.time(), timezone.utc).timestamp())
            for ts in range(epoch, epoch+86400, 300):
                slug = f'btc-updown-5m-{ts}'
                winner, available = settlements.get(slug, (None, math.inf))
                result = replay(load(a.data, ts), active, winner=winner if available <= epoch+86400 else None) if active else {}
                for c, r in result.items():
                    bucket[c].append(r)
                    w.writerow(dict(day=str(d), slug=slug, params=json.dumps(c), **r))
            if i >= window:
                rows = bucket.get(selected, [])
                daily.append(dict(day=str(d), phase='holdout' if d >= holdout else 'development_oos',
                    params=json.dumps(selected), lo=sum(r['lo'] for r in rows), hi=sum(r['hi'] for r in rows),
                    entries=sum(r['entry'] is not None for r in rows), censored=sum(r['status']=='censored' for r in rows)))
            if d < holdout:
                history[d] = bucket
                if i >= window:
                    del history[days[i-window]]
            print(d, 'cash' if selected is None else selected, flush=True)
    with (a.out/'daily_oos.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(daily[0]))
        w.writeheader()
        w.writerows(daily)
    summary = {phase: dict(days=len(rows), lo=sum(r['lo'] for r in rows), hi=sum(r['hi'] for r in rows))
        for phase in ('development_oos','holdout') for rows in [[r for r in daily if r['phase']==phase]]}
    (a.out/'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
