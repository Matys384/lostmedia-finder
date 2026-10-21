"""Strict chronological frontier, persisted before network/compute work.

An exhausted search method is not proof of complete platform coverage.
Monthly windows split to <=7 days, then daily when pagination is capped.
"""
import hashlib
import json
import os
import time
from datetime import date,timedelta

from .db import Database
from .discovery import END,START,page,windows
from .worker import run
from .storage import enforce

SCHEMA="""
CREATE TABLE IF NOT EXISTS periods(id INTEGER PRIMARY KEY,start TEXT UNIQUE,end TEXT,state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS frontier(id INTEGER PRIMARY KEY,period_id INTEGER REFERENCES periods(id),provider TEXT,start TEXT,end TEXT,query TEXT,cursor TEXT,pages INTEGER DEFAULT 0,state TEXT DEFAULT 'pending',error TEXT,updated REAL DEFAULT 0,UNIQUE(period_id,provider,start,end,query));
CREATE TABLE IF NOT EXISTS period_videos(period_id INTEGER,provider TEXT,video_id INTEGER,PRIMARY KEY(period_id,provider,video_id));
CREATE INDEX IF NOT EXISTS frontier_period ON frontier(period_id,state,start);
"""


def initialize(db,providers=("youtube","dailymotion","archive"),queries=("",),start=START,end=END):
    if start<START or end>END:
        raise ValueError("Chronological production campaign locked to 2007-08-15 through 2012-12-31")
    with db.connect() as c:
        c.executescript(SCHEMA)
        for a,b in windows(start,end):
            c.execute("INSERT OR IGNORE INTO periods(start,end) VALUES(?,?)",(str(a),str(b)))
            pid=c.execute("SELECT id FROM periods WHERE start=?",(str(a),)).fetchone()[0]
            for provider in providers:
                for query in queries:
                    c.execute("INSERT OR IGNORE INTO frontier(period_id,provider,start,end,query) VALUES(?,?,?,?,?)",(pid,provider,str(a),str(b),query))


def split_hours(db,node):
    """Dailymotion accepts second-level bounds: day -> 6x4h -> 4x1h before declaring saturation."""
    if "T" not in node["start"]:
        day=node["start"]
        parts=[(f"{day}T{h:02d}",f"{day}T{h+3:02d}") for h in range(0,24,4)]
    else:
        first,last=int(node["start"][11:13]),int(node["end"][11:13])
        if first==last:
            db.execute("UPDATE frontier SET state='saturated',error='Hourly pagination ceiling; residual coverage unknown' WHERE id=?",(node["id"],))
            return
        parts=[(f"{node['start'][:10]}T{h:02d}",)*2 for h in range(first,last+1)]
    with db.connect() as c:
        for a,b in parts:
            c.execute("INSERT OR IGNORE INTO frontier(period_id,provider,start,end,query) VALUES(?,?,?,?,?)",(node["period_id"],node["provider"],a,b,node["query"]))
        c.execute("UPDATE frontier SET state='split' WHERE id=?",(node["id"],))


def split(db,node):
    if node["provider"]=="dailymotion" and (node["start"]==node["end"] or "T" in node["start"]):
        return split_hours(db,node)
    start,end=date.fromisoformat(node["start"]),date.fromisoformat(node["end"])
    days=(end-start).days+1
    if days==1:
        db.execute("UPDATE frontier SET state='saturated',error='Daily pagination ceiling; residual coverage unknown' WHERE id=?",(node["id"],))
        return
    step=7 if days>7 else 1
    with db.connect() as c:
        a=start
        while a<=end:
            b=min(end,a+timedelta(days=step-1))
            c.execute("INSERT OR IGNORE INTO frontier(period_id,provider,start,end,query) VALUES(?,?,?,?,?)",(node["period_id"],node["provider"],str(a),str(b),node["query"]))
            a=b+timedelta(days=1)
        c.execute("UPDATE frontier SET state='split' WHERE id=?",(node["id"],))


def tick(settings,discovery_pages=1,analysis_limit=10,page_cap=10):
    db=Database(settings.root)
    initialize(db)
    enforce(settings)
    periods=db.rows("SELECT * FROM periods WHERE state!='exhausted' ORDER BY start LIMIT 1")
    if not periods:
        return {"state":"finished","meaning":"Available configured methods exhausted; coverage is not exhaustive"}
    period=periods[0]
    if period["start"]>settings.campaign_through:
        return {"state":"audit_boundary","through":settings.campaign_through,"meaning":"Later periods disabled pending validation"}
    db.execute("UPDATE periods SET state='active' WHERE id=?",(period["id"],))
    # Re-enable previously unavailable YouTube discovery when credentials appear.
    if os.environ.get("YOUTUBE_API_KEY"):
        db.execute("UPDATE frontier SET state='pending',error=NULL WHERE period_id=? AND provider='youtube' AND state='unavailable'",(period["id"],))
    calls=0
    blocked=[]
    nodes=db.rows("SELECT * FROM frontier WHERE period_id=? AND state IN ('pending','blocked') ORDER BY start,updated,provider",(period["id"],))
    for node in nodes:
        if node["provider"]=="youtube" and not os.environ.get("YOUTUBE_API_KEY"):
            db.execute("UPDATE frontier SET state='unavailable',error='Missing YOUTUBE_API_KEY' WHERE id=?",(node["id"],))
            continue
        if calls>=discovery_pages:
            break
        try:
            began=time.monotonic()
            items,cursor=page(db,node["provider"],node["query"],node["start"],node["end"],node["cursor"],80)
            new=filtered=0
            for item in items:
                # Never queue an out-of-period result if a platform ignores its filters.
                published=item.get("published")
                if not published or not node["start"][:10]<=published[:10]<=node["end"][:10]:
                    filtered+=1
                    continue
                previous=db.rows("SELECT id FROM videos WHERE source=? AND external_id=? AND is_reference=0",(item["source"],item["external_id"]))
                vid=db.add(**item)
                new+=not bool(previous)
                db.execute("INSERT OR IGNORE INTO period_videos VALUES(?,?,?)",(period["id"],node["provider"],vid))
            from .catalogs import record
            record(db,node["provider"],"chronological",node["query"],node["start"],node["end"],len(items),new,began,filtered)
            count=node["pages"]+1
            state="pending" if cursor else "exhausted"
            db.execute("UPDATE frontier SET pages=?,cursor=?,state=?,error=NULL,updated=? WHERE id=?",(count,cursor,state,time.time(),node["id"]))
            # Dailymotion commonly caps search to 1000 items even when has_more=false.
            capped=node["provider"] in {"youtube","dailymotion"} and count>=page_cap and (cursor or len(items)>=(100 if node["provider"]=="dailymotion" else 50))
            if node["provider"]=="archive" and cursor and cursor.startswith("page:") and int(cursor.split(":")[1])>100:
                capped=True
            if capped:
                split(db,node)
            calls+=1
        except Exception as exc:
            db.execute("UPDATE frontier SET state='blocked',error=?,updated=? WHERE id=?",(str(exc)[:1000],time.time(),node["id"]))
            blocked.append({"provider":node["provider"],"error":str(exc)})
    # Bounded retries, never silently reprocess completed videos.
    db.execute("UPDATE jobs SET state='queued' WHERE state='failed' AND attempts<3 AND updated<? AND video_id IN (SELECT video_id FROM period_videos WHERE period_id=?)",(time.time()-30,period["id"]))
    result=None
    if analysis_limit:
        result=run(settings,limit=analysis_limit,period_id=period["id"])
    pending=db.rows("SELECT COUNT(*) n FROM frontier WHERE period_id=? AND state IN ('pending','blocked')",(period["id"],))[0]["n"]
    jobs=db.rows("SELECT COUNT(*) n FROM jobs j JOIN period_videos pv ON pv.video_id=j.video_id WHERE pv.period_id=? AND (j.state IN ('queued','running') OR (j.state='failed' AND attempts<3))",(period["id"],))[0]["n"]
    if not pending and not jobs:
        db.execute("UPDATE periods SET state='exhausted' WHERE id=?",(period["id"],))
    return {"period":period["start"]+" / "+period["end"],"discovery_calls":calls,"pending_methods":pending,"pending_jobs":jobs,"blocked":blocked,"analysis":result}


def progress(db):
    if not db.rows("SELECT name FROM sqlite_master WHERE name='periods'"):
        return []
    return db.rows("""SELECT p.start,p.end,p.state,f.provider,
      (SELECT COUNT(*) FROM period_videos v WHERE v.period_id=p.id AND v.provider=f.provider) discovered,
      (SELECT COUNT(*) FROM period_videos v JOIN jobs j ON j.video_id=v.video_id WHERE v.period_id=p.id AND v.provider=f.provider AND j.state='done') analyzed,
      (SELECT COUNT(*) FROM period_videos v JOIN jobs j ON j.video_id=v.video_id WHERE v.period_id=p.id AND v.provider=f.provider AND j.state='inaccessible') inaccessible,
      (SELECT COUNT(*) FROM period_videos v JOIN jobs j ON j.video_id=v.video_id WHERE v.period_id=p.id AND v.provider=f.provider AND j.state NOT IN ('done','inaccessible')) unprocessed,
      SUM(f.state='saturated') saturated_windows,SUM(f.state='blocked') blocked_windows,SUM(f.state='unavailable') unavailable_methods,
      SUM(f.state='exhausted') exhausted_windows,COUNT(*) windows,MAX(f.error) note
      FROM periods p JOIN frontier f ON f.period_id=p.id GROUP BY p.id,f.provider ORDER BY p.start,f.provider""")
