# PF1 Part A: Tokyo export

Read-only export of Lighter BTC 15m top-3 books (from `~/lighter-data`, written by
`lighter_rec.py`) and the live probe's trade-path records (`~/lp-arb/probe/*.jsonl`) for the
Dublin PF1 test. Data is not committed.

```bash
nice -n 15 python3 -I research/pf1_export/export_lighter.py books --since 2026-11-14 --until 2026-11-27
nice -n 15 python3 -I research/pf1_export/export_lighter.py probe
# 2026-12-05, holdout days:
nice -n 15 python3 -I research/pf1_export/export_lighter.py books --since 2026-11-28 --until 2026-12-04
```

Transfer, from the operator's laptop:

```bash
scp -3 tokyo:'~/export/lighter-btc15m/*' tokyo:~/export/probe-1126-1127.jsonl.gz dublin:~/lighter-import/
```
