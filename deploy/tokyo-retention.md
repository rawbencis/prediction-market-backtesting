# Tokyo box: what recorded data is needed until when, and a retention proposal

Written 2026-11-25 by the overnight session. Root disk 387 G, ≈ 84 G free after tonight's cleanup.
Nothing here has been applied except where marked "(done)": the recorders' own settings belong to their
owners, and deletions of anything not created tonight need the operator.

## 1. Who reads what

| reader | reads | dates needed | needed until | notes |
|---|---|---|---|---|
| **T1 batch shadow** (`lp-arb-shadow.timer`) | `~/shadow-rec` `pm` rows (BTC/ETH 15m), `~/lighter-data` `lb`/`lt`, `lighter_*.csv` | 2026-11-25 → the 14th counted day (≈ 12-08; later if days fail the coverage rule) | **evaluation runs each night for the previous day**, so the raw rows are needed only ~1 day. Keep 14 days + margin for re-runs/audit: **to ≈ 12-31** | `updown-rec` deletes `~/shadow-rec` days after 14 days, so a day can be re-run for 14 days only. The JSON per day in `~/lp-arb/shadow/` keeps the trigger list and per-window results permanently. |
| T1 holdout (registered, read 11-24; fee-corrected 11-24) | `~/shadow-rec` + `~/lighter-data` 11-11..23 | read done | optional: keep for audit / re-reads | **`updown-rec` will delete 11-10 on 11-25, 11-11 on 11-26, …, 11-23 on 12-08.** If the holdout should stay re-runnable, archive those days now (command below; ≈ 4 GB compressed). |
| T1 paper vs shadow | `~/lp-arb/paper/*.jsonl` | from 11-25 | 60 days (paper.py prunes) | ≤ 200 MB/day cap; real use ≈ 5–20 MB/day |
| One-touch MM | `~/onetouch-mm/research/onetouch_mm/data` | from the restart (11-25) | first read **≥ 2026-12-16** (≥ 3 weeks, ≥ 50 resolved levels); walk-forward wants all of it | ≈ 1.5–3 GB/day (§3). 45 days does not fit on this disk without freeing space. |
| T4 1h/4h recorder (spec only, not installed) | — | — | — | Dublin's H1 plan covers this; nothing on Tokyo. |
| Earlier studies on `~/stwf2-cache`, `~/binance_1s`, `~/btc1s` | June–July BTC 5m; Binance 1 s | — | keep (small relative to the disk, and not regenerable from Polymarket) | |

## 2. Proposed retention per directory

| directory | size now | growth | proposed retention | how |
|---|---|---|---|---|
| `~/shadow-rec` | 16 G (15 days) | 1.0–1.4 GB/day | 14 days raw (recorder default) **plus** a zstd archive of days the operator wants kept (holdout 11-11..23) | `updown-rec` prunes itself; archive with the command below |
| `~/lighter-data` | 0.7 G | ≈ 50–60 MB/day | keep all (≈ 20 GB/year) | no pruning in `lighter_rec.py` |
| `~/lp-arb/shadow` | < 10 MB | ≈ 1 MB/day | keep all | |
| `~/lp-arb/paper` | — | ≤ 200 MB/day, typically ≈ 5–20 MB | 60 days | `paper.py` deletes older files at the UTC day roll |
| `~/lp-arb/{shadow,paper,onetouch}-test` | small | — | delete after 11-25 review | `rm -rf ~/lp-arb/shadow-test ~/lp-arb/paper-test ~/lp-arb/onetouch-test` |
| `~/onetouch-mm/research/onetouch_mm/data` | 1.4 G (11-12/13) | ≈ 1.5–3 GB/day | `--keep-days 45` as asked, with a **30 GB free-space floor** that pauses recording (never deletes) | at 3 GB/day the floor is reached around day 18 unless ≈ 50 GB more is freed; options in §3 |
| `~/onetouch-mm/.../data` old 11-12/13 files | 1.4 G | — | keep until the new recording has ≥ 3 weeks, then delete (1 day, 3 truncated hours, 1 settlement) | operator |
| `~/poly-smart-flow/data/books.sqlite3` | 29 G | none (stopped 11-04) | compress to `.zst` (see the 11-25 report, T5), then the operator removes the original | |
| `~/stwf2-cache`, `~/binance_1s`, `~/btc1s` | 9.8 G | none | keep | |

## 3. One-touch disk arithmetic

Old recording (11-13, a weekday, full hours 12h–22h): 64–206 MB/h, mean ≈ 120 MB/h ≈ **2.9 GB/day**.
Tonight's 20-minute test: see the 11-25 report (T4) for the measured MB/hour.

- 21 days (the minimum before a read) ≈ 61 GB; 45 days ≈ 130 GB. Free now ≈ 84 GB.
- With the 30 GB floor, recording pauses (and logs `disk low … PAUSED`) at ≈ 54 GB used, i.e. after
  ≈ 18 days at 3 GB/day — just short of the 3-week minimum.
- Ways to make 45 days fit, any one is enough: (a) compress `books.sqlite3` and remove the original
  (frees ≈ 20+ GB, measured ratio in the report); (b) recompress finished hours from gzip to zstd -19
  (Dublin measured 1.5–1.7× on gzip'd JSON; ≈ 1 GB/day saved); (c) drop to `--keep-days 25`.

## 4. Archive command for the T1 holdout days (operator; optional)

```bash
mkdir -p ~/archive/shadow-rec && cd ~/shadow-rec
for d in 2026-11-11 2026-11-12 2026-11-13 2026-11-14 2026-11-15 2026-11-16 2026-11-17 \
         2026-11-18 2026-11-19 2026-11-20 2026-11-21 2026-11-22 2026-11-23; do
  [ -d "$d" ] && nice -n 19 tar -cf - "$d" | nice -n 19 zstd -T1 -10 -q -o ~/archive/shadow-rec/"$d".tar.zst
done
ls -la ~/archive/shadow-rec   # ≈ 0.3 GB per day (Dublin measured 27% of raw for the same row formats)
```
The `~/lighter-data` days are not pruned by anything, so they need no archive.
