"""Budget enforcement and conservative cleanup of owned temporary media only."""
import shutil
import time
from pathlib import Path


class StorageFull(RuntimeError):
    pass


_scans={}


def usage(settings,max_age=0):
    """Walk the data root; max_age>0 reuses a recent walk (the hot path calls this per video)."""
    cached=_scans.get(settings.root)
    if max_age and cached and time.monotonic()-cached[0]<max_age:
        total=cached[1]
        disk=shutil.disk_usage(settings.root)
        return {"used_bytes":total,"budget_bytes":int(settings.disk_budget_gb*1024**3),
                "free_bytes":disk.free,"minimum_free_bytes":int(settings.min_free_gb*1024**3)}
    total=0
    for p in settings.root.rglob("*"):
        if p.is_file():
            try:
                total+=p.stat().st_size
            except FileNotFoundError:
                pass
    _scans[settings.root]=(time.monotonic(),total)
    disk=shutil.disk_usage(settings.root)
    return {"used_bytes":total,"budget_bytes":int(settings.disk_budget_gb*1024**3),
            "free_bytes":disk.free,"minimum_free_bytes":int(settings.min_free_gb*1024**3)}


def enforce(settings,reserve_bytes=0,max_age=0):
    state=usage(settings,max_age)
    if state["used_bytes"]+reserve_bytes>state["budget_bytes"] or state["free_bytes"]-reserve_bytes<state["minimum_free_bytes"]:
        raise StorageFull("Disk budget or free-space floor reached; processing paused, no evidence deleted")
    return state


def cleanup_video(settings,db,video_id,keep_thumbs=()):
    row=db.rows("SELECT local_path,is_reference FROM videos WHERE id=?",(video_id,))[0]
    if row["is_reference"]:
        return
    path=Path(row["local_path"]).resolve() if row["local_path"] else None
    owned=(settings.root/"media").resolve()
    if settings.delete_downloads and path and path.is_relative_to(owned) and path.is_file():
        path.unlink()
        # Record accessibility separately: absence of retained media is not inaccessibility.
        db.execute("UPDATE videos SET local_path=NULL WHERE id=?",(video_id,))
    folder=(settings.root/"media"/str(video_id)).resolve()
    if settings.delete_downloads and folder.parent==owned and folder.is_dir():
        # Also drops yt-dlp .part leftovers; only called once a job is terminal.
        shutil.rmtree(folder,ignore_errors=True)
    if not settings.keep_all_thumbnails:
        keep=set(keep_thumbs)
        folder=settings.root/"thumbs"/str(video_id)
        for thumb in folder.rglob("*.jpg") if folder.exists() else []:
            if str(thumb.relative_to(settings.root)).replace("\\","/") not in keep:
                thumb.unlink(missing_ok=True)
        for directory in sorted(folder.rglob("*"),reverse=True) if folder.exists() else []:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()


def cleanup_completed(settings,db):
    """Recover cleanup after a crash between committing results and unlinking media."""
    import json
    done={r["video_id"] for r in db.rows("SELECT video_id FROM jobs WHERE state='done'")}
    # Only videos that still own files; scanning every finished job does not scale.
    pending={r["id"] for r in db.rows("SELECT v.id FROM videos v JOIN jobs j ON j.video_id=v.id WHERE j.state='done' AND v.local_path IS NOT NULL")}
    for parent in ("thumbs","media"):
        if (settings.root/parent).exists():
            pending|={int(p.name) for p in (settings.root/parent).iterdir() if p.name.isdigit() and int(p.name) in done}
    for row in [{"video_id":v} for v in sorted(pending)]:
        keep=[]
        for match in db.rows("SELECT payload FROM matches WHERE video_id=?",(row["video_id"],)):
            keep.extend(p["candidate_thumb"] for p in json.loads(match["payload"])["pairs"])
        cleanup_video(settings,db,row["video_id"],keep)
