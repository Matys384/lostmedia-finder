import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .db import Database
from .media import digest


def stats(db):
    states={r["state"]:r["n"] for r in db.rows("SELECT state,COUNT(*) n FROM jobs GROUP BY state")}
    return {"discovered":db.rows("SELECT COUNT(*) n FROM videos WHERE is_reference=0")[0]["n"],
            "accessible":db.rows("SELECT COUNT(*) n FROM videos v WHERE is_reference=0 AND (local_path IS NOT NULL OR EXISTS(SELECT 1 FROM processing_metrics m WHERE m.video_id=v.id AND m.accessible=1))")[0]["n"],
            "references":db.rows("SELECT COUNT(*) n FROM videos WHERE is_reference=1")[0]["n"],
            "analyzed":states.get("done",0),"queued":states.get("queued",0),"running":states.get("running",0),
            "errors":states.get("failed",0),"ignored":states.get("ignored",0),"deferred":states.get("deferred",0),"restricted":states.get("restricted",0),
            "inaccessible":states.get("inaccessible",0),
            "matches":db.rows("SELECT COUNT(DISTINCT video_id) n FROM matches")[0]["n"],
            "jobs":states}


def platform_metrics(db,window=3600):
    """Per platform: analyzed per minute and mean seconds per stage (download, CPU decode, GPU)."""
    since=time.time()-window
    rows=db.rows("""SELECT v.source,COUNT(*) analyzed,AVG(m.download_seconds) download,AVG(m.decode_seconds) decode,
        AVG(m.gpu_seconds) gpu,AVG(m.match_seconds) matching,SUM(m.media_bytes) bytes FROM processing_metrics m JOIN videos v ON v.id=m.video_id
        WHERE m.completed>? AND m.download_seconds>0 GROUP BY v.source""",(since,))
    attempts={}
    if db.rows("SELECT name FROM sqlite_master WHERE name='download_attempts'"):
        for r in db.rows("SELECT source,outcome,COUNT(*) n FROM download_attempts WHERE finished>? GROUP BY source,outcome",(since,)):
            attempts.setdefault(r["source"],{})[r["outcome"]]=r["n"]
    for r in rows:
        r["per_minute"]=r["analyzed"]*60/window
        r["attempts"]=attempts.get(r["source"],{})
    return rows


def pacing(db):
    if not db.rows("SELECT name FROM sqlite_master WHERE name='tuning'"):
        return {}
    from .tuning import LADDERS
    out={}
    for r in db.rows("SELECT * FROM tuning"):
        ladder=LADDERS.get(r["source"])
        if ladder:
            pause=db.rows("SELECT next_at FROM rate_limits WHERE name=?",("cooldown:download:"+r["source"],))
            out[r["source"]]={"spacing":ladder[r["step"]],"stable":ladder[r["stable"]],"fastest_allowed":ladder[r["ceiling"]],
                              "throttles":r["throttles"],"paused_for":max(0,pause[0]["next_at"]-time.time()) if pause else 0,
                              "last_change":(json.loads(r["history"] or "[]") or [None])[-1]}
    return out


class DiscoveryRequest(BaseModel):
    provider: str = "archive"
    query: str = ""
    start: str = "2007-08-15"
    end: str = "2012-12-31"
    pages: int = Field(1,ge=1,le=100)


def create_app(settings):
    db=Database(settings.root)
    app=FastAPI(title="Lostmedia Finder",version="0.1.0")
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])
    token=secrets.token_urlsafe(32)
    processes={}

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method not in {"GET","HEAD","OPTIONS"} and request.headers.get("X-Lostmedia-Token")!=token:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail":"Invalid local session token"},status_code=403)
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; object-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/",response_class=HTMLResponse)
    def home():
        return (Path(__file__).parent/"static/index.html").read_text("utf-8")

    @app.get("/static/{name}")
    def static(name: str):
        if name not in {"app.js","style.css"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent/"static"/name)

    @app.get("/api/session")
    def session():
        return {"token":token}

    @app.get("/api/stats")
    def status():
        result=stats(db)
        result["processes"]={name:{"running":p.poll() is None,"exit_code":p.poll()} for name,p in processes.items()}
        rows=db.rows("SELECT COUNT(*) n FROM jobs WHERE state='done' AND updated>?",(time.time()-3600,))
        result["completed_last_hour"]=rows[0]["n"]
        from .storage import usage
        result["storage"]=usage(settings,max_age=30)
        # Stage averages over the last hour: shows whether network, decoding/GPU or matching limits throughput.
        result["platforms_last_hour"]=platform_metrics(db)
        result["pacing"]=pacing(db)
        result["stages_last_hour"]=db.rows("SELECT COUNT(*) videos,AVG(download_seconds) download,AVG(extract_seconds) extract,AVG(match_seconds) matching,AVG(media_bytes) media_bytes FROM processing_metrics WHERE completed>? AND extract_seconds IS NOT NULL",(time.time()-3600,))[0]
        sizes=db.rows("SELECT AVG(feature_bytes) average,AVG(seconds) seconds FROM processing_metrics WHERE completed IS NOT NULL")[0]
        result["average_feature_bytes"]=sizes["average"]
        result["estimated_remaining_known_queue_bytes"]=(sizes["average"] or 0)*result["queued"]
        try:
            p=subprocess.run(["nvidia-smi","--query-gpu=name,utilization.gpu,memory.used,memory.total","--format=csv,noheader,nounits"],capture_output=True,text=True,timeout=3)
            result["gpu"]=p.stdout.strip() or "unavailable"
        except (OSError,subprocess.TimeoutExpired):
            result["gpu"]="unavailable"
        return result

    @app.get("/api/videos")
    def videos(after: int=0, limit: int=50):
        return db.rows("SELECT v.id,v.title,v.url,v.is_reference,v.published,j.state,j.error FROM videos v LEFT JOIN jobs j ON v.id=j.video_id WHERE v.id>? ORDER BY v.id LIMIT ?",(after,max(1,min(limit,200))))

    @app.get("/api/matches")
    def matches(offset: int=0, limit: int=30):
        rows=db.rows("SELECT m.*,v.title,v.url,(v.local_path IS NOT NULL) has_media,r.title reference_title FROM matches m JOIN videos v ON v.id=m.video_id JOIN videos r ON r.id=m.reference_id ORDER BY m.score DESC LIMIT ? OFFSET ?",(max(1,min(limit,100)),max(0,offset)))
        for row in rows:
            row["payload"]=json.loads(row["payload"])
        return rows

    @app.get("/api/discovery")
    def discovery_stats():
        tables=db.rows("SELECT name FROM sqlite_master WHERE name='discovery_events'")
        if not tables:
            return []
        return db.rows("SELECT provider,method,COUNT(*) requests,SUM(returned) returned,SUM(new) new,SUM(seconds) seconds,SUM(error IS NOT NULL) errors FROM discovery_events GROUP BY provider,method")

    @app.get("/api/progress")
    def progress():
        from .chronology import progress as get_progress
        return get_progress(db)

    @app.get("/thumbs/{video}/{signature}/{name}")
    def thumbnail(video: int,signature: str,name: str):
        root=settings.root/"thumbs"
        target=(root/str(video)/signature/name).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix!=".jpg":
            raise HTTPException(404)
        return FileResponse(target)

    @app.get("/api/media/{video_id}")
    def media(video_id: int):
        rows=db.rows("SELECT local_path FROM videos WHERE id=?",(video_id,))
        if not rows or not rows[0]["local_path"] or not Path(rows[0]["local_path"]).is_file():
            raise HTTPException(404)
        return FileResponse(rows[0]["local_path"])

    @app.post("/api/upload")
    async def upload(file: UploadFile=File(...), reference: bool=Form(False)):
        ext=Path(file.filename or "").suffix.lower()
        if ext not in {".mp4",".mkv",".webm",".avi",".mov",".flv",".jpg",".png",".jpeg",".webp"}:
            raise HTTPException(400,"Unsupported file extension")
        path=settings.root/"uploads"/(secrets.token_hex(16)+ext)
        size=0
        try:
            with open(path,"wb") as out:
                while block:=await file.read(1024*1024):
                    size+=len(block)
                    if size>settings.max_download_mb*1024**2:
                        raise HTTPException(413,"File exceeds configured size limit")
                    out.write(block)
            sha=digest(path)
            existing=db.rows("SELECT id FROM videos WHERE source='local' AND external_id=? AND is_reference=?",(sha,int(reference)))
            if existing:
                path.unlink()
                return existing[0]
            return {"id":db.add("local",sha,"",file.filename or path.name,local_path=str(path),reference=reference)}
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def launch(name,args):
        if name in processes and processes[name].poll() is None:
            raise HTTPException(409,"Already running")
        with open(settings.root/(name+"-web.log"),"a",encoding="utf-8") as log:
            processes[name]=subprocess.Popen([sys.executable,"-m","lostmedia.cli",*args,"--data",str(settings.root)],stdout=log,stderr=log,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return {"pid":processes[name].pid}

    @app.post("/api/run")
    def start_worker():
        return launch("worker",["run","--backend",settings.backend,"--interval",str(settings.interval)])

    @app.post("/api/campaign")
    def start_campaign():
        (settings.root/"STOP").unlink(missing_ok=True)
        return launch("campaign",["campaign","--watch"])

    @app.post("/api/stop")
    def stop_campaign():
        (settings.root/"STOP").touch()
        return {"state":"stop requested at next durable batch checkpoint"}

    @app.post("/api/discover")
    def discover(request: DiscoveryRequest):
        from datetime import date
        if request.provider not in {"archive","youtube","dailymotion"}:
            raise HTTPException(400,"Unknown provider")
        try:
            if date.fromisoformat(request.start)>date.fromisoformat(request.end):
                raise ValueError()
        except ValueError:
            raise HTTPException(400,"Invalid dates")
        return launch("discovery",["discover",request.provider,"--query",request.query,"--start",request.start,"--end",request.end,"--pages",str(request.pages)])

    @app.post("/api/retry")
    def retry():
        return {"queued":db.execute("UPDATE jobs SET state='queued',error=NULL WHERE state='failed'")}

    return app
