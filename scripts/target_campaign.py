"""Unattended duration-targeted discovery (lostmedia/targeted.py) until every window is exhausted.

Rounds of frontier calls; when only quota-blocked or unreachable windows remain, waits and retries.
Run beside the analysis worker. Stop with data/chronological/STOP.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lostmedia.cli import settings  # noqa: E402
from lostmedia.db import Database  # noqa: E402
from lostmedia.targeted import CAMPAIGN, backfill_durations, step  # noqa: E402

root = settings(Path(sys.argv[1] if len(sys.argv) > 1 else "data/chronological")).root
db = Database(root)
print(json.dumps({"durations_backfilled": backfill_durations(db)}), flush=True)
while not (root/"STOP").exists():
    log = step(db, 100)
    for line in log:
        print(json.dumps(line), flush=True)
    open_windows = db.rows("SELECT provider,state,COUNT(*) n FROM target_frontier WHERE campaign=? AND state IN ('pending','blocked') GROUP BY provider,state", (CAMPAIGN,))
    print(json.dumps({"at": time.strftime("%H:%M:%S"), "open": open_windows}), flush=True)
    if not open_windows:
        break
    if not any("error" not in line for line in log):
        # Everything left is quota-blocked or unreachable: back off before the next round.
        for _ in range(1800):
            if (root/"STOP").exists():
                break
            time.sleep(1)
