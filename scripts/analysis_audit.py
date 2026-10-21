"""Analyze bounded platform samples from the first chronological period."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.worker import run
from lostmedia.storage import usage

s=Settings(root=Path('data/chronological'),max_duration=600,max_download_mb=100)
db=Database(s.root)
pid=db.rows("SELECT id FROM periods WHERE start='2007-08-15'")[0]['id']
reports=[]
for source,count in [('dailymotion',10),('archive',20)]:
    begin=time.perf_counter()
    report=run(s,limit=count,downloads=2,period_id=pid,source=source)
    report.update(source=source,wall_seconds=time.perf_counter()-begin)
    reports.append(report)
    print(json.dumps(report),flush=True)
result={'period':['2007-08-15','2007-08-31'],'settings':{'max_duration_seconds':600,'max_download_mb':100,'interval':2,'crops':7,'precision':'float16'},
        'runs':reports,'storage':usage(s),
        'states':db.rows("SELECT v.source,j.state,COUNT(*) n FROM videos v JOIN jobs j ON j.video_id=v.id GROUP BY v.source,j.state"),
        'processing':db.rows("SELECT v.source,COUNT(*) n,SUM(m.seconds) measured_seconds,SUM(m.frames) frame_views,SUM(m.feature_bytes) feature_bytes FROM processing_metrics m JOIN videos v ON v.id=m.video_id GROUP BY v.source"),
        'errors':db.rows("SELECT v.source,v.external_id,j.state,j.error FROM videos v JOIN jobs j ON j.video_id=v.id WHERE j.state NOT IN ('queued','done')")}
Path('reports/analysis-audit.json').write_text(json.dumps(result,indent=2),'utf-8')
print(json.dumps(result,indent=2))
