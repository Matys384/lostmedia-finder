"""Discovery-only campaign ticks for the active period, until configured methods are exhausted.

Safe to run beside `lostmedia run` (SQLite WAL, no worker lock). Stops when no
pending/blocked window remains or when every remaining one is blocked (quota).
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lostmedia.chronology import split, tick  # noqa: E402
from lostmedia.cli import settings  # noqa: E402
from lostmedia.db import Database  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--data", default="data/chronological")
parser.add_argument("--calls", type=int, default=2000, help="total API page calls for this run")
parser.add_argument("--resplit-saturated", action="store_true", help="split previously saturated Dailymotion days into hours")
args = parser.parse_args()
s = settings(Path(args.data))
db = Database(s.root)
if args.resplit_saturated:
    for node in db.rows("SELECT * FROM frontier WHERE provider='dailymotion' AND state='saturated' AND start=end AND start NOT LIKE '%T%'"):
        split(db, node)
        print(json.dumps({"resplit": node["start"]}), flush=True)
done, began = 0, time.monotonic()
while done < args.calls:
    report = tick(s, discovery_pages=min(20, args.calls-done), analysis_limit=0)
    done += report.get("discovery_calls", 0)
    pending = db.rows("SELECT provider,state,COUNT(*) n FROM frontier WHERE period_id=(SELECT id FROM periods WHERE state='active' ORDER BY start LIMIT 1) AND state IN ('pending','blocked') GROUP BY provider,state")
    print(json.dumps({"calls": done, "seconds": round(time.monotonic()-began), "blocked": report.get("blocked"), "open_windows": pending}), flush=True)
    if not report.get("discovery_calls"):
        break
print(json.dumps({"finished": True, "calls": done, "videos": db.rows("SELECT provider,COUNT(*) n FROM period_videos GROUP BY provider")}), flush=True)
