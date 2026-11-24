#!/bin/bash
cd /home/ubuntu/lighter-flips/overnight
for d in "$@"; do
  avail=$(free -m | awk '/Mem:/{print $7}'); cap=1500M; [ "$avail" -lt 3000 ] && cap=1000M
  systemd-run --user --scope -q -p MemoryMax=$cap -p CPUQuota=80% nice -n 19 python3 t2_legs.py day --day $d
done
echo T2DONE
