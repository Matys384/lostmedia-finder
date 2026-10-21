"""Complementary discovery from public exports and official upload playlists."""
import json
import os
import time
from datetime import date
from urllib.parse import urlparse,parse_qs

from .discovery import END,START,request


def event_table(db):
    db.execute("CREATE TABLE IF NOT EXISTS discovery_events(id INTEGER PRIMARY KEY,provider TEXT,method TEXT,query TEXT,start TEXT,end TEXT,returned INTEGER,new INTEGER,seconds REAL,error TEXT,created REAL)")


def record(db,provider,method,query,start,end,total,new,began,filtered=0):
    event_table(db)
    db.execute("INSERT INTO discovery_events(provider,method,query,start,end,returned,new,seconds,created) VALUES(?,?,?,?,?,?,?,?,?)",(provider,method,query,str(start),str(end),total,new,time.monotonic()-began,time.time()))
    db.execute("CREATE TABLE IF NOT EXISTS discovery_quality(id INTEGER PRIMARY KEY,provider TEXT,method TEXT,returned INTEGER,new INTEGER,duplicates INTEGER,filtered INTEGER,created REAL)")
    db.execute("INSERT INTO discovery_quality(provider,method,returned,new,duplicates,filtered,created) VALUES(?,?,?,?,?,?,?)",(provider,method,total,new,total-new-filtered,filtered,time.time()))


def import_catalog(db,path,start=START,end=END,allow_unknown=False):
    """Stream JSONL records {source,id,title,published,url}; never load entire catalog."""
    began=time.monotonic()
    total=new=outside=unknown=0
    for line_number,line in enumerate(open(path,encoding="utf-8-sig"),1):
        if not line.strip():
            continue
        item=json.loads(line)
        provider=item["source"]
        identifier=str(item["id"])
        published=item.get("published","")
        total+=1
        if not published:
            unknown+=1
            if not allow_unknown:
                continue
        elif not start<=date.fromisoformat(published[:10])<=end:
            outside+=1
            continue
        if provider=="youtube":
            url="https://www.youtube.com/watch?v="+identifier
        elif provider=="dailymotion":
            url="https://www.dailymotion.com/video/"+identifier
        elif provider=="archive":
            url="https://archive.org/details/"+identifier
        else:
            raise ValueError(f"Unknown source at line {line_number}")
        if not identifier or any(x in identifier for x in ('/','?','#','&','\\')):
            raise ValueError(f"Invalid identifier at line {line_number}")
        exists=db.rows("SELECT id FROM videos WHERE source=? AND external_id=? AND is_reference=0",(provider,identifier))
        db.add(provider,identifier,url,item.get("title",identifier),published or None)
        new+=not bool(exists)
    record(db,"catalog","jsonl",str(path),start,end,total,new,began,outside+(0 if allow_unknown else unknown))
    return {"rows":total,"new":new,"outside_period":outside,"unknown_date":unknown}


def playlist(db,playlist_id,start=START,end=END,pages=1,budget=80):
    key=os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ValueError("YOUTUBE_API_KEY required")
    state_key=f"youtube:playlist:{playlist_id}:{start}:{end}"
    new=total=filtered=0
    began=time.monotonic()
    for _ in range(pages):
        state=db.rows("SELECT * FROM searches WHERE key=?",(state_key,))
        if state and state[0]["done"]:
            break
        p={"key":key,"part":"snippet,contentDetails","playlistId":playlist_id,"maxResults":50}
        if state and state[0]["cursor"]:
            p["pageToken"]=state[0]["cursor"]
        data=request(db,"youtube","https://www.googleapis.com/youtube/v3/playlistItems",p,budget)
        for item in data.get("items",[]):
            total+=1
            published=item.get("contentDetails",{}).get("videoPublishedAt")
            identifier=item.get("contentDetails",{}).get("videoId")
            if not identifier or not published or not start<=date.fromisoformat(published[:10])<=end:
                filtered+=1
                continue
            exists=db.rows("SELECT id FROM videos WHERE source='youtube' AND external_id=? AND is_reference=0",(identifier,))
            db.add("youtube",identifier,"https://www.youtube.com/watch?v="+identifier,item["snippet"]["title"],published)
            new+=not bool(exists)
        cursor=data.get("nextPageToken")
        db.execute("INSERT OR REPLACE INTO searches VALUES(?,?,?,?)",(state_key,cursor,int(not cursor),time.time()))
        if not cursor:
            break
    record(db,"youtube","playlist",playlist_id,start,end,total,new,began,filtered)
    return {"returned":total,"new":new,"filtered":filtered,"duplicates":total-new-filtered}


def channel(db,channel_id,**kwargs):
    key=os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ValueError("YOUTUBE_API_KEY required")
    response=request(db,"youtube","https://www.googleapis.com/youtube/v3/channels",{"key":key,"id":channel_id,"part":"contentDetails"},kwargs.get("budget",80))
    if not response.get("items"):
        raise ValueError("Channel not found")
    uploads=response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    return playlist(db,uploads,**kwargs)
