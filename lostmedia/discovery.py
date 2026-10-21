"""Public API discovery with durable cursors and shared request budgets."""
import calendar
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

PRESET = ["Super Mario 64 Big Star Secret", "LotusMan17", "big star secret", "SM64 secret",
          "Super Mario 64", "video", "gameplay", "tutorial", "music", "funny", "compilation",
          "animation", "vlog", "gaming", "screamer", "reaction", "test"]


START=date(2007,8,15)
END=date(2012,12,31)


class Cooldown(RuntimeError):
    """A platform asked us to slow down; the job is requeued, never bypassed."""


class Throttled(Cooldown):
    """The platform refused this request for pacing reasons (429, anti-bot page)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def cooldown(db, name, seconds):
    db.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?)",("cooldown:"+name,time.time()+seconds))


def cooling_until(db, name):
    rows=db.rows("SELECT next_at FROM rate_limits WHERE name=?",("cooldown:"+name,))
    return rows[0]["next_at"] if rows and rows[0]["next_at"]>time.time() else 0


def reserve(db, name, spacing):
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        # Pauses are persisted state checked here, never a blocking sleep.
        pause=c.execute("SELECT next_at FROM rate_limits WHERE name=?",("cooldown:"+name,)).fetchone()
        now=time.time()
        if pause and pause[0]>now:
            raise Cooldown(f"{name}: cooling down for {pause[0]-now:.0f}s")
        row=c.execute("SELECT next_at FROM rate_limits WHERE name=?",(name,)).fetchone()
        target=max(now,row[0] if row else now)
        c.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?)",(name,target+spacing))
    time.sleep(max(0,target-time.time()))


# Official YouTube Data API costs (units). The project quota is 10,000 units per day and
# resets at midnight Pacific time; the local budget stays below it.
YOUTUBE_COSTS={"search":100,"videos":1,"playlistItems":1,"channels":1}
YOUTUBE_DAILY_UNITS=int(os.environ.get("YOUTUBE_DAILY_UNITS","9500"))


def youtube_day():
    # Fixed UTC-8: our day starts at or after Google's reset (PST/PDT), never before it.
    return (datetime.now(timezone.utc)-timedelta(hours=8)).date().isoformat()


def spend_youtube_units(db,url):
    cost=YOUTUBE_COSTS.get(url.rstrip("/").rsplit("/",1)[-1],100)
    day=youtube_day()
    with db.connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS quota_units(day TEXT,provider TEXT,units INTEGER NOT NULL,calls INTEGER NOT NULL,PRIMARY KEY(day,provider))")
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO quota_units VALUES(?,?,0,0)",(day,"youtube"))
        used=c.execute("SELECT units FROM quota_units WHERE day=? AND provider='youtube'",(day,)).fetchone()[0]
        if used+cost>YOUTUBE_DAILY_UNITS:
            raise RuntimeError(f"Local YouTube daily quota budget reached ({used}/{YOUTUBE_DAILY_UNITS} units); resumes after midnight Pacific")
        c.execute("UPDATE quota_units SET units=units+?,calls=calls+1 WHERE day=? AND provider='youtube'",(cost,day))
        utc=datetime.now(timezone.utc).date().isoformat()
        c.execute("INSERT OR IGNORE INTO quota VALUES(?,?,0)",(utc,"youtube"))
        c.execute("UPDATE quota SET calls=calls+1 WHERE day=? AND provider='youtube'",(utc,))


def request(db, provider, url, params, daily_limit=80):
    for attempt in range(4):
        reserve(db, provider, 1.1 if provider=="youtube" else 2.0)
        if provider=="youtube":
            spend_youtube_units(db,url)
        response=httpx.get(url,params=params,timeout=45,follow_redirects=True,headers={"User-Agent":"LostmediaFinder/0.1 (public research; bounded requests)"})
        if response.status_code in (429,500,502,503,504):
            delay=response.headers.get("Retry-After",str(2**attempt*3))
            try:
                delay=float(delay)
            except ValueError:
                from email.utils import parsedate_to_datetime
                delay=max(0,parsedate_to_datetime(delay).timestamp()-time.time())
            if delay>60:
                cooldown(db,provider,delay)
                raise Cooldown(f"{provider}: cooldown {delay}s; resume later")
            time.sleep(max(0,delay))
            continue
        if response.status_code in (401,403):
            reason=""
            try:
                reasons=response.json().get("error",{}).get("errors",[])
                reason=",".join(str(x.get("reason","")) for x in reasons)
            except (ValueError,AttributeError):
                pass
            raise RuntimeError(f"{provider}: access/quota denied (HTTP {response.status_code}, {reason}); no bypass attempted")
        if response.is_error:
            raise RuntimeError(f"{provider}: HTTP {response.status_code}")
        return response.json()
    raise RuntimeError(f"{provider}: transient failures after four attempts")


def windows(start, end, months=1):
    if start>end:
        raise ValueError("start must precede end")
    current=start
    while current<=end:
        year=current.year+(current.month-1+months)//12
        month=(current.month-1+months)%12+1
        next_date=date(year,month,1)
        yield current,min(end,next_date-timedelta(days=1))
        current=next_date


def moment(value):
    """'YYYY-MM-DD' -> date; 'YYYY-MM-DDTHH' (sub-day frontier window) -> UTC datetime."""
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc) if "T" in value else date.fromisoformat(value)
    return value


def span(start, end):
    """Inclusive window -> [after, before) UTC datetimes. Hour windows include their last hour."""
    a = start if isinstance(start, datetime) else datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    b = end+timedelta(hours=1) if isinstance(end, datetime) else datetime(end.year, end.month, end.day, tzinfo=timezone.utc)+timedelta(days=1)
    return a, b


def page(db, provider, query, start, end, cursor, budget):
    start, end = moment(start), moment(end)
    if isinstance(end, datetime):
        until=end.date()+timedelta(days=1)
    else:
        until=end+timedelta(days=1)
    if provider=="youtube":
        key=os.environ.get("YOUTUBE_API_KEY")
        if not key:
            raise ValueError("Set YOUTUBE_API_KEY for official YouTube discovery")
        p={"key":key,"part":"snippet","type":"video","q":query,"maxResults":50,
           "publishedAfter":start.isoformat()+"T00:00:00Z","publishedBefore":until.isoformat()+"T00:00:00Z"}
        if cursor:
            p["pageToken"]=cursor
        data=request(db,provider,"https://www.googleapis.com/youtube/v3/search",p,budget)
        observe(db,provider,str(start),str(end),query,data.get("pageInfo",{}).get("totalResults",0),"search.list (estimated total)")
        items=[dict(source=provider,external_id=x["id"]["videoId"],url="https://www.youtube.com/watch?v="+x["id"]["videoId"],
                    title=x["snippet"]["title"],published=x["snippet"]["publishedAt"]) for x in data.get("items",[]) if "videoId" in x["id"]]
        return items,data.get("nextPageToken")
    if provider=="dailymotion":
        after,before=span(start,end)
        p={"search":query,"created_after":int(after.timestamp()),
           "created_before":int(before.timestamp())-1,"limit":100,"page":int(cursor or 1),"fields":"id,title,url,created_time"}
        if not query:
            p.pop("search")
        data=request(db,provider,"https://api.dailymotion.com/videos",p)
        observe(db,provider,str(start),str(end),query,data.get("total",0),"videos (total may be capped)")
        return [dict(source=provider,external_id=x["id"],url=x["url"],title=x["title"],published=datetime.fromtimestamp(x["created_time"],timezone.utc).isoformat()) for x in data["list"]],str(p["page"]+1) if data.get("has_more") else None
    if provider=="archive":
        escaped=query.replace('\\','\\\\').replace('"','\\"')
        term=f'("{escaped}") AND ' if query else ""
        # Live scrape returned an identical cursor/page. Use verified advanced paging.
        number=int(cursor.split(":",1)[1]) if cursor and cursor.startswith("page:") else 1
        p={"q":f'{term}mediatype:movies AND publicdate:[{start.isoformat()}T00:00:00Z TO {end.isoformat()}T23:59:59Z]',
           "fl[]":["identifier","title","publicdate"],"sort[]":"identifier asc","rows":100,"page":number,"output":"json"}
        data=request(db,provider,"https://archive.org/advancedsearch.php",p)
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        response=data["response"]
        observe(db,provider,str(start),str(end),query,response["numFound"],"advancedsearch")
        next_cursor=f"page:{number+1}" if number*100<response["numFound"] else None
        return [dict(source=provider,external_id=x["identifier"],url="https://archive.org/details/"+x["identifier"],title=x.get("title",x["identifier"]),published=str(x.get("publicdate",""))) for x in response["docs"]],next_cursor
    raise ValueError("Unknown provider")


def observe(db,provider,start,end,query,total,endpoint):
    db.execute("CREATE TABLE IF NOT EXISTS observed_counts(provider TEXT,start TEXT,end TEXT,query TEXT,total INTEGER,endpoint TEXT,observed REAL,PRIMARY KEY(provider,start,end,query,endpoint))")
    db.execute("INSERT OR REPLACE INTO observed_counts VALUES(?,?,?,?,?,?,?)",(provider,start,end,query,total,endpoint,time.time()))


def discover(db, provider, queries, start=START, end=END, pages=1, budget=80):
    """pages is a total call cap, not a cap per query. Reruns resume cursors."""
    db.execute("CREATE TABLE IF NOT EXISTS discovery_events(id INTEGER PRIMARY KEY,provider TEXT,method TEXT,query TEXT,start TEXT,end TEXT,returned INTEGER,new INTEGER,seconds REAL,error TEXT,created REAL)")
    calls,new=0,0
    for start_w,end_w in windows(start,end):
        for query in queries:
            key=hashlib.sha256(json.dumps([provider,query,str(start_w),str(end_w)]).encode()).hexdigest()
            while calls<pages:
                state=db.rows("SELECT * FROM searches WHERE key=?",(key,))
                if state and state[0]["done"]:
                    break
                cursor=state[0]["cursor"] if state else None
                before=db.rows("SELECT COUNT(*) n FROM videos")[0]["n"]
                began=time.monotonic()
                try:
                    items,next_cursor=page(db,provider,query,start_w,end_w,cursor,budget)
                    for item in items:
                        db.add(**item)
                    after=db.rows("SELECT COUNT(*) n FROM videos")[0]["n"]
                    added=after-before
                    db.execute("INSERT OR REPLACE INTO searches VALUES(?,?,?,?)",(key,next_cursor,int(not next_cursor),time.time()))
                    db.execute("INSERT INTO discovery_events(provider,method,query,start,end,returned,new,seconds,created) VALUES(?,?,?,?,?,?,?,?,?)",(provider,"time-window",query,str(start_w),str(end_w),len(items),added,time.monotonic()-began,time.time()))
                    new+=added
                    calls+=1
                    if not next_cursor:
                        break
                except Exception as exc:
                    db.execute("INSERT INTO discovery_events(provider,method,query,start,end,returned,new,seconds,error,created) VALUES(?,?,?,?,?,?,?,?,?,?)",(provider,"time-window",query,str(start_w),str(end_w),0,0,time.monotonic()-began,str(exc),time.time()))
                    raise
            if calls>=pages:
                return {"calls":calls,"new":new}
    return {"calls":calls,"new":new}


VIDEO_EXTENSIONS=(".mp4",".ogv",".webm",".m4v",".mov",".avi",".mpg",".mpeg",".wmv",".flv",".mkv",".3gp")


def seconds(value):
    """Archive 'length' is seconds or H:MM:SS; None when absent or malformed."""
    try:
        parts=[float(x) for x in str(value).split(":")]
    except ValueError:
        return None
    total=0.
    for part in parts:
        total=total*60+part
    return total


def archive_file(db, video, max_mb, max_duration=None):
    data=request(db,"archive","https://archive.org/metadata/"+quote(video["external_id"],safe=""),{})
    videos=[f for f in data.get("files",[]) if f["name"].lower().endswith(VIDEO_EXTENSIONS) and int(f.get("size",0) or 0)>0]
    if not videos:
        raise ValueError("Archive item has no video file (unsupported item)")
    # Files flagged private are access-restricted by Archive; never requested.
    videos=[f for f in videos if str(f.get("private","")).lower()!="true"]
    if not videos:
        raise ValueError("Archive item video files are access-restricted")
    files=[f for f in videos if int(f["size"])<=max_mb*1024**2]
    if not files:
        raise ValueError("Archive item has no video within download size limit")
    # Known durations are checked before downloading, not after.
    if max_duration:
        short=[f for f in files if (seconds(f.get("length")) or 0)<=max_duration]
        if not short:
            raise ValueError("Archive item video duration limit exceeded before download")
        files=short
    # Smallest MP4 first: Archive derivatives (.ia.mp4, 512kb) are enough for matching
    # and originals can be 1 GB at a few hundred KB/s. One file per item for now.
    files.sort(key=lambda f:(not f["name"].lower().endswith(".mp4"),int(f["size"])))
    return "https://archive.org/download/"+quote(video["external_id"],safe="")+"/"+quote(files[0]["name"],safe="")
