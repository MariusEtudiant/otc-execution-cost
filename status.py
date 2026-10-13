"""Quick health check of the overnight collection: coverage, gaps, errors, disk."""

import csv
import datetime as dt
import json
import os
import subprocess
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

status = json.load(open(os.path.join(DATA, "status.json"), encoding="utf-8"))
alive = str(status["pid"]) in subprocess.run(
    ["tasklist", "/FI", f"PID eq {status['pid']}"], capture_output=True, text=True).stdout
print(f"collector pid {status['pid']} alive={alive}  started {status['started_utc'][:16]}Z  "
      f"last {status['last_ts']}  free disk {status.get('free_gb')} GB")

rows = list(csv.DictReader(open(os.path.join(DATA, "metrics.csv"), encoding="utf-8")))
minutes = sorted({r["ts_utc"] for r in rows})
first, last = (dt.datetime.fromisoformat(m.replace("Z", "+00:00")) for m in (minutes[0], minutes[-1]))
expected = int((last - first).total_seconds() // 60) + 1
print(f"minutes {len(minutes)}/{expected} from {minutes[0]} to {minutes[-1]}  "
      f"({expected - len(minutes)} missing)")

per_book = Counter((r["venue"], r["asset"]) for r in rows)
for (venue, asset), n in sorted(per_book.items()):
    print(f"  {venue:9s} {asset}  {n:5d} snapshots  ({n / len(minutes):.1%})")

if status["last_errors"]:
    print("last errors:")
    for e in status["last_errors"][-5:]:
        print("  ", e[:160])
