import json
import logging
import os
import tomllib
from datetime import date
from pathlib import Path

import typer
from filelock import FileLock

from .config import Settings
from .db import Database
from .media import digest

app=typer.Typer(no_args_is_help=True)


def settings(data: Path, backend: str="", interval: float=0):
    options={}
    config=Path("config.toml")
    if config.exists():
        options=tomllib.loads(config.read_text("utf-8")).get("analysis",{})
    options["root"]=data
    if backend:
        options["backend"]=backend
    if interval:
        options["interval"]=interval
    return Settings(**options)


@app.command()
def init(data: Path=Path("data")):
    s=settings(data)
    Database(s.root)
    typer.echo(f"Database: {s.root}")


@app.command("import")
def import_file(path: Path, reference: bool=False, title: str="", url: str="", data: Path=Path("data")):
    """Import a local image or video; original bytes are not copied."""
    if not path.is_file():
        raise typer.BadParameter("File does not exist")
    db=Database(settings(data).root)
    vid=db.add("local",digest(path),url,title or path.name,local_path=str(path.resolve()),reference=reference)
    typer.echo(f"Imported {vid}: {path.name}")


@app.command()
def add(url: str, title: str="", data: Path=Path("data")):
    """Queue a public YouTube, Dailymotion or Archive URL."""
    from urllib.parse import urlparse,parse_qs
    p=urlparse(url)
    host=(p.hostname or "").lower()
    if host in {"youtube.com","www.youtube.com","youtu.be"}:
        source="youtube"
        identifier=p.path.strip("/") if host=="youtu.be" else parse_qs(p.query).get("v",[""])[0]
    elif host in {"dailymotion.com","www.dailymotion.com"}:
        source="dailymotion"
        identifier=p.path.rsplit("/",1)[-1]
    elif host=="archive.org" and p.path.startswith("/details/"):
        source="archive"
        identifier=p.path.split("/")[2]
    else:
        raise typer.BadParameter("Only platform video/item URLs are accepted")
    if not identifier or p.scheme!="https":
        raise typer.BadParameter("Invalid HTTPS video URL")
    typer.echo(Database(settings(data).root).add(source,identifier,url,title or identifier))


@app.command()
def discover(provider: str, query: list[str]=typer.Option(None), start: str="2007-08-15", end: str="2012-12-31", pages: int=1, data: Path=Path("data"), budget: int=80):
    """Bounded official API discovery, with durable monthly search cursors."""
    from .discovery import discover as search, PRESET
    if pages<1:
        raise typer.BadParameter("pages must be positive")
    typer.echo(json.dumps(search(Database(settings(data).root),provider,query or PRESET,date.fromisoformat(start),date.fromisoformat(end),pages,budget)))


@app.command()
def run(data: Path=Path("data"), backend: str="", interval: float=0, limit: int=0, downloads: int=0, period: str="", source: str="", max_mb: int=0, max_duration: int=0, crops: str="", cached_only: bool=False):
    """Analyze queued videos. --period 2007-08-15 restricts to one campaign period.

    --max-mb/--max-duration give a fast first pass; larger videos become 'deferred'."""
    from .worker import run as work
    s=settings(data,backend,interval)
    if max_mb:
        s.max_download_mb=max_mb
    if max_duration:
        s.max_duration=max_duration
    if crops:
        s.crops={"true":True,"false":False}.get(crops.lower(),crops)
        if s.crops not in (True,False,"fine"):
            raise typer.BadParameter("crops must be true, false or fine")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",handlers=[logging.StreamHandler(),logging.FileHandler(s.root/"worker.log",encoding="utf-8")])
    period_id=None
    if period:
        found=Database(s.root).rows("SELECT id FROM periods WHERE start=?",(period,))
        if not found:
            raise typer.BadParameter("Unknown period start date")
        period_id=found[0]["id"]
    with FileLock(str(s.root/"worker.lock"),timeout=1):
        report=work(s,limit,downloads,period_id,source or None,cached_only)
    typer.echo(json.dumps(report))
    if report["failed"]:
        raise typer.Exit(1)


@app.command()
def retry(data: Path=Path("data"), all_done: bool=False, deferred: bool=False):
    """Retry failed jobs; --deferred requeues over-limit videos; --all-done rematches after adding references."""
    db=Database(settings(data).root)
    states=["'failed'"]+(["'done'"] if all_done else [])+(["'deferred'","'ignored'"] if deferred else [])
    states="("+",".join(states)+")"
    typer.echo(db.execute(f"UPDATE jobs SET state='queued',error=NULL WHERE state IN {states}"))


@app.command()
def status(data: Path=Path("data")):
    from .web import stats
    typer.echo(json.dumps(stats(Database(settings(data).root)),indent=2))


@app.command("import-catalog")
def catalog(path: Path,start: str="2007-08-15",end: str="2012-12-31",allow_unknown: bool=False,data: Path=Path("data")):
    from .catalogs import import_catalog
    typer.echo(json.dumps(import_catalog(Database(settings(data).root),path,date.fromisoformat(start),date.fromisoformat(end),allow_unknown)))


@app.command("explore")
def explore(identifier: str,channel: bool=False,pages: int=1,start: str="2007-08-15",end: str="2012-12-31",data: Path=Path("data")):
    from .catalogs import channel as by_channel,playlist
    search=by_channel if channel else playlist
    typer.echo(json.dumps(search(Database(settings(data).root),identifier,start=date.fromisoformat(start),end=date.fromisoformat(end),pages=pages)))


@app.command("build-index")
def build_index(signature: str,shard_size: int=100000,data: Path=Path("data")):
    from .indexing import build
    s=settings(data)
    typer.echo(json.dumps(build(Database(s.root),s,signature,shard_size)))


@app.command("search-index")
def search_index(feature_file: Path,signature: str,k: int=10,data: Path=Path("data")):
    from .indexing import search
    from .features import load
    typer.echo(json.dumps(search(settings(data),signature,load(feature_file)["vectors"],k)))


@app.command()
def campaign(data: Path=Path("data"),watch: bool=False,pages: int=3,analysis_limit: int=10):
    """Chronological 2007-08-15..2012-12-31. --watch loops until finished or stopped."""
    import time
    from .chronology import tick
    s=settings(data)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",handlers=[logging.StreamHandler(),logging.FileHandler(s.root/"campaign.log",encoding="utf-8")])
    with FileLock(str(s.root/"worker.lock"),timeout=1):
        while True:
            if (s.root/"STOP").exists():
                typer.echo("Stopped at durable checkpoint (STOP file)")
                break
            try:
                report=tick(s,discovery_pages=pages,analysis_limit=analysis_limit)
                typer.echo(json.dumps(report))
                if not watch or report.get("state") in {"finished","audit_boundary"}:
                    break
            except Exception:
                logging.exception("Campaign paused; state retained")
                if not watch:
                    raise
            # Responsive bounded wait; STOP is checked every second.
            for _ in range(30):
                if (s.root/"STOP").exists():
                    break
                time.sleep(1)


@app.command("target-discover")
def target_discover(calls: int=50, backfill: bool=False, data: Path=Path("data")):
    """Duration-targeted re-upload discovery (BSS runtime 5:12 +-5 s), 2007-08-15..2014-12-31."""
    from .targeted import backfill_durations, step
    db=Database(settings(data).root)
    if backfill:
        typer.echo(json.dumps({"durations_backfilled":backfill_durations(db)}))
    for line in step(db,calls):
        typer.echo(json.dumps(line))


@app.command("target-progress")
def target_progress(data: Path=Path("data")):
    from .targeted import progress
    typer.echo(json.dumps(progress(Database(settings(data).root)),indent=2))


@app.command("campaign-progress")
def campaign_progress(data: Path=Path("data")):
    from .chronology import progress
    typer.echo(json.dumps(progress(Database(settings(data).root)),indent=2))


@app.command()
def serve(data: Path=Path("data"), port: int=8765):
    import uvicorn
    from .web import create_app
    uvicorn.run(create_app(settings(data)),host="127.0.0.1",port=port)


if __name__=="__main__":
    app()
