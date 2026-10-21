"""Bounded live integration audit. Hard stop at 2007-08-31, never advances."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.chronology import tick,initialize,progress


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--calls',type=int,default=30)
    args=parser.parse_args()
    s=Settings(root=Path('data/chronological'),max_duration=600,max_download_mb=100)
    db=Database(s.root)
    initialize(db)
    initial=db.rows('SELECT COUNT(*) n FROM videos WHERE is_reference=0')[0]['n']
    started=time.perf_counter()
    actual=0
    for _ in range(args.calls):
        first=db.rows("SELECT start FROM periods WHERE state!='exhausted' ORDER BY start LIMIT 1")
        if not first or first[0]['start']!='2007-08-15':
            break
        report=tick(s,discovery_pages=1,analysis_limit=0)
        actual+=report['discovery_calls']
        print(json.dumps(report),flush=True)
        if report['blocked'] or not report['discovery_calls']:
            break
    duration=time.perf_counter()-started
    final=db.rows('SELECT COUNT(*) n FROM videos WHERE is_reference=0')[0]['n']
    result={'period':['2007-08-15','2007-08-31'],'elapsed_seconds':duration,'successful_page_calls':actual,
            'new_unique_entries':final-initial,'unique_entries_per_second':(final-initial)/duration,
            'progress':progress(db)[:3],
            'discovery':db.rows("SELECT provider,method,COUNT(*) requests,SUM(returned) returned,SUM(new) new,SUM(returned-new) duplicate_or_filtered,SUM(seconds) seconds FROM discovery_events GROUP BY provider,method"),
            'frontier':db.rows("SELECT provider,start,end,pages,state,error FROM frontier WHERE period_id=(SELECT id FROM periods WHERE start='2007-08-15') ORDER BY provider,start,end")}
    Path('reports').mkdir(exist_ok=True)
    Path('reports/discovery-audit.json').write_text(json.dumps(result,indent=2),'utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
