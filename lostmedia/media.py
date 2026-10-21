"""Bounded media acquisition and sequential, timestamped decoding."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import cv2
from PIL import Image


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=True)
    return json.loads(p.stdout)


def acquire(video, settings, db):
    if video.get("local_path") and Path(video["local_path"]).is_file():
        return Path(video["local_path"])
    if video["source"] in settings.thumbnail_only_sources:
        # Hard stop: these platforms' terms forbid downloading; see thumbnails.py.
        raise PermissionError(f"Video download disabled for {video['source']} (thumbnail-only platform)")
    from .storage import enforce
    enforce(settings,settings.max_download_mb*1024**2,max_age=30)
    if shutil.disk_usage(settings.root).free < settings.min_free_gb * 1024**3:
        raise RuntimeError("Insufficient disk space")
    from yt_dlp import YoutubeDL
    import time
    from .discovery import Throttled, reserve
    from .tuning import current_spacing, retry_after
    # Pace is adaptive per platform (see tuning.py); the spacing used is recorded with the outcome.
    video["_spacing"] = current_spacing(db, video["source"])
    reserve(db, "download:" + video["source"], video["_spacing"])
    video["_fetch_started"] = time.monotonic()
    folder = settings.root / "media" / str(video["id"])
    folder.mkdir(exist_ok=True)

    def limits(info, *, incomplete):
        if (info.get("duration") or 0) > settings.max_duration:
            return "Duration limit exceeded"
        if (info.get("filesize") or info.get("filesize_approx") or 0) > settings.max_download_mb * 1024**2:
            return "Size limit exceeded"

    def hook(status):
        if status.get("downloaded_bytes", 0) > settings.max_download_mb * 1024**2:
            raise RuntimeError("Download byte limit exceeded")
        if status.get("status")=="downloading" and shutil.disk_usage(settings.root).free<settings.min_free_gb*1024**3:
            raise RuntimeError("Free disk space floor reached during download")

    options = {"format": "bestvideo[height<=480]/best[height<=480]/bestvideo[height<=720]/best[height<=720]/best", "outtmpl": str(folder / "video.%(ext)s"),
               "noplaylist": True, "socket_timeout": 30, "retries": 2, "fragment_retries": 2,
               "concurrent_fragment_downloads": 1, "max_filesize": settings.max_download_mb * 1024**2,
               "match_filter": limits, "progress_hooks": [hook], "quiet": True, "noprogress": True,
               "overwrites": False, "continuedl": True, "sleep_interval_requests": 1}
    with YoutubeDL(options) as ydl:
        url = video["url"]
        if video["source"] == "archive" and "/details/" in url:
            from .discovery import archive_file
            url = archive_file(db, video, settings.max_download_mb, settings.max_duration)
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as exc:
            message = str(exc).lower()
            # Anti-bot pages and 429s mean "slow down": pause this platform, never work around it.
            if any(term in message for term in ("confirm you're not a bot", "confirm you’re not a bot", "http error 429", "too many requests", "rate-limit", "rate limit")):
                raise Throttled(f"{video['source']}: platform throttled downloads: {str(exc)[:200]}", retry_after(exc)) from exc
            raise
        if not info or info.get("_type") == "playlist":
            raise RuntimeError("No downloadable single video; import an individual file URL")
        path = Path(ydl.prepare_filename(info))
    if not path.is_file():
        raise RuntimeError("Download unavailable or exceeds configured limits")
    db.execute("UPDATE videos SET local_path=? WHERE id=?", (str(path.resolve()), video["id"]))
    return path


def frames(path, interval=2.0, max_duration=7200):
    path = Path(path)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        with Image.open(path) as im:
            yield 0.0, im.convert("RGB")
        return
    metadata = probe(path)
    duration = float(metadata.get("format", {}).get("duration", 0))
    if duration > max_duration:
        raise ValueError(f"Video duration {duration:.1f}s exceeds limit {max_duration}s")
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"Unable to open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        cap.release()
        raise ValueError("Missing frame rate")
    next_time, count = 0.0, 0
    try:
        while cap.grab():
            # POS_MSEC preserves the timeline of variable-frame-rate videos.
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if t <= 0 and count:
                t = count / fps
            count += 1
            if t > max_duration:
                raise ValueError("Maximum decoding duration reached")
            if t + 1e-4 < next_time:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                raise ValueError("Frame decoding failed")
            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            im.thumbnail((960, 960))
            yield t, im
            next_time = t + interval
    finally:
        cap.release()


def views(im, multi=True):
    yield "full", im
    if not multi:
        return
    w, h = im.size
    yield "flip", im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    for label, box in boxes(multi).items():
        yield label, im.crop(tuple(int(v*s) for v,s in zip(box,(w,h,w,h))))


def boxes(multi=True):
    """View label -> relative (left, top, right, bottom) window; 'full'/'flip' are the whole frame."""
    # Overlapping regions cover picture-in-picture and cropped reposts.
    out = {"center": (.15,.15,.85,.85), "tl": (0,0,.65,.65), "tr": (.35,0,1,.65), "bl": (0,.35,.65,1), "br": (.35,.35,1,1)}
    if multi == "fine":
        # Small picture-in-picture: corner windows at 45% and 30% of the frame.
        for k in (.45, .30):
            out.update({f"tl{k:.2f}": (0,0,k,k), f"tr{k:.2f}": (1-k,0,1,k), f"bl{k:.2f}": (0,1-k,k,1), f"br{k:.2f}": (1-k,1-k,1,1)})
    return out


def restore_thumbnails(path,video,encoder,settings,needed):
    """Rehydrate only evidence requested after a new-reference cache search."""
    folder=settings.root/"thumbs"/str(video["id"])/encoder.signature
    folder.mkdir(parents=True,exist_ok=True)
    wanted={int(Path(name).stem) for name in needed}
    if not wanted:
        return
    for i,(t,im) in enumerate(frames(path,settings.interval,settings.max_duration)):
        if i in wanted:
            im.thumbnail((384,216))
            im.save(folder/f"{i:06d}.jpg",quality=80)
        if i>=max(wanted):
            break

