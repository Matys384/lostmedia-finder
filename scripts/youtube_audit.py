"""Complement monthly no-keyword search with daily partitions and broad topics."""
import json
import sys
import time
from datetime import date,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lostmedia.config import Settings
from lostmedia.db import Database
from lostmedia.discovery import page
from lostmedia.catalogs import record

s=Settings(root=Path('data/chronological'));db=Database(s.root)
pid=db.rows("SELECT id FROM periods WHERE start='2007-08-15'")[0]['id']
tasks=[(date(2007,8,15)+timedelta(days=i),date(2007,8,15)+timedelta(days=i),'','daily-partition') for i in range(17)]
tasks += [(date(2007,8,15),date(2007,8,31),q,'broad-topic') for q in ['music','funny','tutorial','gameplay','vlog','news','animation','sports','concert','dance','interview','documentary']]
began=time.perf_counter();calls=0;total_new=0
for a,b,q,method in tasks:
    db.execute('INSERT OR IGNORE INTO frontier(period_id,provider,start,end,query) VALUES(?,?,?,?,?)',(pid,'youtube',str(a),str(b),q))
    node=db.rows('SELECT * FROM frontier WHERE period_id=? AND provider=? AND start=? AND end=? AND query=?',(pid,'youtube',str(a),str(b),q))[0]
    if node['pages']>0:
        continue
    started=time.monotonic()
    try:
        items,cursor=page(db,'youtube',q,a,b,None,80)
    except Exception as exc:
        db.execute("UPDATE frontier SET state='blocked',error=? WHERE id=?",(str(exc),node['id']))
        print(str(exc),flush=True);break
    new=filtered=0
    for item in items:
        if not str(a)<=item['published'][:10]<=str(b):
            filtered+=1
            continue
        previous=db.rows("SELECT id FROM videos WHERE source='youtube' AND external_id=? AND is_reference=0",(item['external_id'],))
        vid=db.add(**item);new+=not bool(previous)
        db.execute('INSERT OR IGNORE INTO period_videos VALUES(?,?,?)',(pid,'youtube',vid))
    db.execute('UPDATE frontier SET cursor=?,pages=1,state=?,error=NULL,updated=? WHERE id=?',(cursor,'pending' if cursor else 'exhausted',time.time(),node['id']))
    record(db,'youtube',method,q,a,b,len(items),new,started,filtered)
    calls+=1;total_new+=new
    print(json.dumps({'method':method,'start':str(a),'query':q,'returned':len(items),'new':new}),flush=True)
report={'calls':calls,'new':total_new,'seconds':time.perf_counter()-began,'methods':db.rows("SELECT method,COUNT(*) requests,SUM(returned) returned,SUM(new) new,SUM(returned-new) duplicate_or_filtered FROM discovery_events WHERE provider='youtube' GROUP BY method")}
Path('reports/youtube-discovery-integration.json').write_text(json.dumps(report,indent=2),'utf-8')
print(json.dumps(report,indent=2))
