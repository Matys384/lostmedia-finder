"""Duration-targeted re-upload campaign.

Hypothesis: a viral 2007 video was re-uploaded unedited many times, so copies share the
original runtime (BSS: 5:12-5:13) within a few seconds. This campaign finds every listed
video in that runtime window over a date range, independently of the chronological
campaign, and gives those jobs priority in the analysis queue. Nothing is excluded:
other videos keep their place behind them.

Per platform:
- Dailymotion filters by duration server side (longer_than/shorter_than, minutes), so the
  window is enumerated without keywords; date windows split until under the API ceiling.
- YouTube only filters coarse buckets (videoDuration=medium is 4-20 min). Keyword searches
  per month (re-uploads usually keep the title), then videos.list reads exact durations.
- Archive: keyword searches; the runtime is checked when the item is fetched.
"""
import calendar
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from .discovery import request

CAMPAIGN = "bss-reupload"
TARGET = 312.5            # 5:12-5:13
TOLERANCE = 5.5           # -> 307..318 s
NEAR = (250, 330)         # slightly shortened copies (intro cut, sped up) come next
START, END = date(2007, 8, 15), date(2014, 12, 31)
YOUTUBE_TERMS = ["super mario 64 big star secret", "big star secret", "mario 64 secret", "super mario 64 secret",
                 "mario 64 screamer", "mario screamer", "scary mario", "mario 64 scary", "sm64 secret", "mario 64 glitch",
                 "super mario 64", "mario 64"]
ARCHIVE_TERMS = ["mario 64", "super mario", "big star secret", "screamer"]
PRIORITY = {"exact": 100, "near": 50, "keyword": 40}

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_frontier(id INTEGER PRIMARY KEY, campaign TEXT, provider TEXT, start TEXT, end TEXT,
  query TEXT, cursor TEXT, pages INTEGER DEFAULT 0, total INTEGER, state TEXT DEFAULT 'pending', error TEXT, updated REAL DEFAULT 0,
  UNIQUE(campaign, provider, start, end, query));
CREATE TABLE IF NOT EXISTS campaign_videos(campaign TEXT, video_id INTEGER, provider TEXT, duration REAL, tier TEXT,
  PRIMARY KEY(campaign, video_id));
"""


def tier(seconds):
    if seconds is None:
        return None
    if abs(seconds-TARGET) <= TOLERANCE:
        return "exact"
    if NEAR[0] <= seconds <= NEAR[1]:
        return "near"
    return None


def months(start, end):
    current = start
    while current <= end:
        last = date(current.year, current.month, calendar.monthrange(current.year, current.month)[1])
        yield current, min(last, end)
        current = last+timedelta(days=1)


def initialize(db, start=START, end=END):
    with db.connect() as c:
        c.executescript(SCHEMA)
        for a, b in months(start, end):
            c.execute("INSERT OR IGNORE INTO target_frontier(campaign,provider,start,end,query) VALUES(?,?,?,?,?)", (CAMPAIGN, "dailymotion", str(a), str(b), ""))
            for term in YOUTUBE_TERMS:
                c.execute("INSERT OR IGNORE INTO target_frontier(campaign,provider,start,end,query) VALUES(?,?,?,?,?)", (CAMPAIGN, "youtube", str(a), str(b), term))
        for year in range(start.year, end.year+1):
            a, b = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            for term in ARCHIVE_TERMS:
                c.execute("INSERT OR IGNORE INTO target_frontier(campaign,provider,start,end,query) VALUES(?,?,?,?,?)", (CAMPAIGN, "archive", str(a), str(b), term))


def keep(db, item, duration, label):
    """Queue a discovered video with its runtime and priority (never lowers an existing priority)."""
    vid = db.add(item["source"], item["external_id"], item["url"], item["title"], item.get("published"))
    priority = PRIORITY.get(label, 0)
    db.execute("UPDATE videos SET duration=COALESCE(?,duration) WHERE id=?", (duration, vid))
    db.execute("UPDATE jobs SET priority=MAX(priority,?) WHERE video_id=?", (priority, vid))
    db.execute("INSERT OR REPLACE INTO campaign_videos VALUES(?,?,?,?,?)", (CAMPAIGN, vid, item["source"], duration, label))
    return vid


def dailymotion(db, node):
    a, b = date.fromisoformat(node["start"]), date.fromisoformat(node["end"])
    after = calendar.timegm(a.timetuple())
    before = calendar.timegm((b+timedelta(days=1)).timetuple())-1
    page = int(node["cursor"] or 1)
    p = {"created_after": after, "created_before": before, "longer_than": (TARGET-TOLERANCE)/60, "shorter_than": (TARGET+TOLERANCE)/60,
         "limit": 100, "page": page, "fields": "id,title,url,created_time,duration", "sort": "recent"}
    data = request(db, "dailymotion", "https://api.dailymotion.com/videos", p)
    total = data.get("total")
    # The API serves at most 1000 results per query: split the window before paginating.
    if page == 1 and total and total > 1000 and a < b:
        split(db, node)
        return 0, 0, "split"
    new = 0
    for x in data["list"]:
        item = dict(source="dailymotion", external_id=x["id"], url=x["url"], title=x["title"],
                    published=datetime.fromtimestamp(x["created_time"], timezone.utc).isoformat())
        existed = db.rows("SELECT 1 FROM videos WHERE source='dailymotion' AND external_id=? AND is_reference=0", (x["id"],))
        keep(db, item, x.get("duration"), tier(x.get("duration")) or "near")
        new += not existed
    more = data.get("has_more") and page < 10
    state = "pending" if more else ("saturated" if data.get("has_more") else "exhausted")
    db.execute("UPDATE target_frontier SET cursor=?,pages=pages+1,total=?,state=?,error=NULL,updated=? WHERE id=?",
               (str(page+1) if more else None, total, state, time.time(), node["id"]))
    return len(data["list"]), new, state


def split(db, node):
    a, b = date.fromisoformat(node["start"]), date.fromisoformat(node["end"])
    step = 7 if (b-a).days >= 7 else 1
    with db.connect() as c:
        x = a
        while x <= b:
            y = min(b, x+timedelta(days=step-1))
            c.execute("INSERT OR IGNORE INTO target_frontier(campaign,provider,start,end,query) VALUES(?,?,?,?,?)", (CAMPAIGN, node["provider"], str(x), str(y), node["query"]))
            x = y+timedelta(days=1)
        c.execute("UPDATE target_frontier SET state='split',updated=? WHERE id=?", (time.time(), node["id"]))


ISO = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_seconds(value):
    m = ISO.fullmatch(value or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d*86400+h*3600+mi*60+s


def youtube_details(db, ids):
    """videos.list: exact runtime and channel for up to 50 ids per unit."""
    key = os.environ["YOUTUBE_API_KEY"]
    out = {}
    for i in range(0, len(ids), 50):
        data = request(db, "youtube", "https://www.googleapis.com/youtube/v3/videos",
                       {"key": key, "part": "contentDetails,snippet", "id": ",".join(ids[i:i+50]), "maxResults": 50})
        for x in data.get("items", []):
            out[x["id"]] = {"duration": iso_seconds(x["contentDetails"].get("duration")), "channel": x["snippet"].get("channelId"),
                            "title": x["snippet"]["title"], "published": x["snippet"]["publishedAt"]}
    return out


def youtube(db, node):
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("Set YOUTUBE_API_KEY")
    a, b = date.fromisoformat(node["start"]), date.fromisoformat(node["end"])
    p = {"key": key, "part": "snippet", "type": "video", "q": node["query"], "maxResults": 50, "videoDuration": "medium",
         "publishedAfter": a.isoformat()+"T00:00:00Z", "publishedBefore": (b+timedelta(days=1)).isoformat()+"T00:00:00Z"}
    if node["cursor"]:
        p["pageToken"] = node["cursor"]
    data = request(db, "youtube", "https://www.googleapis.com/youtube/v3/search", p)
    ids = [x["id"]["videoId"] for x in data.get("items", []) if "videoId" in x["id"]]
    details = youtube_details(db, ids) if ids else {}
    new = 0
    for vid_id, d in details.items():
        label = tier(d["duration"])
        existed = db.rows("SELECT 1 FROM videos WHERE source='youtube' AND external_id=? AND is_reference=0", (vid_id,))
        # Keyword hits outside the runtime window still enter the queue, behind the window.
        keep(db, dict(source="youtube", external_id=vid_id, url="https://www.youtube.com/watch?v="+vid_id,
                      title=d["title"], published=d["published"]), d["duration"], label or "keyword")
        db.execute("UPDATE videos SET channel=COALESCE(channel,?) WHERE source='youtube' AND external_id=?", (d["channel"], vid_id))
        new += not existed
    cursor = data.get("nextPageToken")
    state = "pending" if cursor and node["pages"]+1 < 10 else ("saturated" if cursor else "exhausted")
    db.execute("UPDATE target_frontier SET cursor=?,pages=pages+1,total=?,state=?,error=NULL,updated=? WHERE id=?",
               (cursor if state == "pending" else None, data.get("pageInfo", {}).get("totalResults"), state, time.time(), node["id"]))
    return len(ids), new, state


def archive(db, node):
    number = int(node["cursor"] or 1)
    escaped = node["query"].replace('"', '\\"')
    p = {"q": f'("{escaped}") AND mediatype:movies AND publicdate:[{node["start"]}T00:00:00Z TO {node["end"]}T23:59:59Z]',
         "fl[]": ["identifier", "title", "publicdate"], "sort[]": "identifier asc", "rows": 100, "page": number, "output": "json"}
    data = request(db, "archive", "https://archive.org/advancedsearch.php", p)
    response = data["response"]
    new = 0
    for x in response["docs"]:
        existed = db.rows("SELECT 1 FROM videos WHERE source='archive' AND external_id=? AND is_reference=0", (x["identifier"],))
        keep(db, dict(source="archive", external_id=x["identifier"], url="https://archive.org/details/"+x["identifier"],
                      title=x.get("title", x["identifier"]), published=str(x.get("publicdate", ""))), None, "keyword")
        new += not existed
    more = number*100 < response["numFound"]
    db.execute("UPDATE target_frontier SET cursor=?,pages=pages+1,total=?,state=?,error=NULL,updated=? WHERE id=?",
               (str(number+1) if more else None, response["numFound"], "pending" if more else "exhausted", time.time(), node["id"]))
    return len(response["docs"]), new, "pending" if more else "exhausted"


HANDLERS = {"dailymotion": dailymotion, "youtube": youtube, "archive": archive}


def step(db, calls=50, providers=("dailymotion", "youtube", "archive")):
    """Advance up to `calls` frontier windows, oldest first, rotating platforms."""
    initialize(db)
    log = []
    # Each platform gets its own budget per round: free Dailymotion pages must not starve YouTube.
    for provider in providers:
        done = 0
        nodes = db.rows("SELECT * FROM target_frontier WHERE campaign=? AND provider=? AND state IN ('pending','blocked') ORDER BY start,id LIMIT ?",
                        (CAMPAIGN, provider, calls))
        for node in nodes:
            if done >= calls:
                break
            began = time.monotonic()
            try:
                returned, new, state = HANDLERS[provider](db, node)
                log.append({"provider": provider, "start": node["start"], "query": node["query"], "returned": returned, "new": new,
                            "state": state, "seconds": round(time.monotonic()-began, 2)})
            except Exception as exc:
                db.execute("UPDATE target_frontier SET state='blocked',error=?,updated=? WHERE id=?", (str(exc)[:500], time.time(), node["id"]))
                log.append({"provider": provider, "start": node["start"], "error": str(exc)[:200]})
                # Quota, outage or refusal: leave this platform for the next round.
                break
            done += 1
    return log


def backfill_durations(db, limit=100000):
    """Runtime for already discovered videos so their priority reflects the target window."""
    updated = 0
    rows = db.rows("SELECT id,external_id FROM videos WHERE source='dailymotion' AND is_reference=0 AND duration IS NULL LIMIT ?", (limit,))
    for i in range(0, len(rows), 100):
        chunk = rows[i:i+100]
        data = request(db, "dailymotion", "https://api.dailymotion.com/videos",
                       {"ids": ",".join(r["external_id"] for r in chunk), "fields": "id,duration", "limit": 100})
        found = {x["id"]: x.get("duration") for x in data.get("list", [])}
        for r in chunk:
            seconds = found.get(r["external_id"])
            db.execute("UPDATE videos SET duration=? WHERE id=?", (seconds if seconds is not None else -1, r["id"]))
            label = tier(seconds)
            if label:
                db.execute("UPDATE jobs SET priority=MAX(priority,?) WHERE video_id=?", (PRIORITY[label], r["id"]))
            updated += 1
    rows = db.rows("SELECT id,external_id FROM videos WHERE source='youtube' AND is_reference=0 AND duration IS NULL LIMIT ?", (limit,))
    if rows and os.environ.get("YOUTUBE_API_KEY"):
        details = youtube_details(db, [r["external_id"] for r in rows])
        for r in rows:
            d = details.get(r["external_id"])
            db.execute("UPDATE videos SET duration=?,channel=COALESCE(channel,?) WHERE id=?",
                       (d["duration"] if d and d["duration"] is not None else -1, d and d["channel"], r["id"]))
            label = tier(d and d["duration"])
            if label:
                db.execute("UPDATE jobs SET priority=MAX(priority,?) WHERE video_id=?", (PRIORITY[label], r["id"]))
            updated += 1
    return updated


def progress(db):
    initialize(db)
    return {"windows": db.rows("SELECT provider,state,COUNT(*) windows,SUM(pages) pages,SUM(CASE WHEN state!='split' THEN total END) reported_total FROM target_frontier WHERE campaign=? GROUP BY provider,state", (CAMPAIGN,)),
            "videos": db.rows("""SELECT c.provider,c.tier,COUNT(*) videos,SUM(j.state='done') analyzed,SUM(j.state='queued') queued,
                SUM(j.state IN ('inaccessible','restricted')) unavailable FROM campaign_videos c JOIN jobs j ON j.video_id=c.video_id
                WHERE c.campaign=? GROUP BY c.provider,c.tier""", (CAMPAIGN,)),
            "by_year": db.rows("SELECT substr(v.published,1,4) year,c.provider,COUNT(*) n FROM campaign_videos c JOIN videos v ON v.id=c.video_id WHERE c.campaign=? AND c.tier='exact' GROUP BY year,c.provider ORDER BY year", (CAMPAIGN,))}
