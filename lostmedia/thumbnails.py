"""Thumbnail-only analysis for platforms whose terms forbid downloading videos (YouTube).

Only the thumbnail image that the official API returns for a video is fetched; the video
itself is never requested. The thumbnail is embedded like a single frame and scored against
every reference frame. A thumbnail cannot prove an identity: scores rank candidates for a
person to check on the platform's own watch page.
"""
import os
import time
from pathlib import Path

import httpx
import numpy as np

from .discovery import request, reserve

SIZES = ("high", "medium", "standard", "default")  # 480x360 first: present for 2007 uploads too


def api_thumbnail(db, video):
    """Thumbnail URL from the official API (videos.list, 1 quota unit)."""
    if video["source"] != "youtube":
        raise ValueError(f"No thumbnail API for {video['source']}")
    data = request(db, "youtube", "https://www.googleapis.com/youtube/v3/videos",
                   {"key": os.environ["YOUTUBE_API_KEY"], "part": "snippet", "id": video["external_id"]})
    items = data.get("items", [])
    if not items:
        raise RuntimeError("Video unavailable: not returned by the YouTube API")
    thumbs = items[0]["snippet"].get("thumbnails", {})
    url = next((thumbs[s]["url"] for s in SIZES if s in thumbs), None)
    if not url:
        raise RuntimeError("Video unavailable: no thumbnail in API response")
    db.execute("UPDATE videos SET thumb_url=? WHERE id=?", (url, video["id"]))
    return url


def pick(thumbnails):
    """Best URL from an API `thumbnails` object."""
    return next((thumbnails[s]["url"] for s in SIZES if s in (thumbnails or {})), None)


def fetch(db, video, settings):
    url = video.get("thumb_url")
    if url == "":
        # Batch lookup (targeted.backfill_thumbnails) found no such video in the API.
        raise RuntimeError("Video unavailable: not returned by the YouTube API")
    url = url or api_thumbnail(db, video)
    reserve(db, "thumb:"+video["source"], 0.5)
    began = time.monotonic()
    response = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "LostmediaFinder/0.1 (thumbnail check)"})
    if response.status_code == 404:
        raise RuntimeError("HTTP Error 404: thumbnail not found (video likely removed)")
    response.raise_for_status()
    folder = settings.root/"thumbs"/str(video["id"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/"thumbnail.jpg"
    path.write_bytes(response.content)
    video["_fetch_started"] = began
    return path


def relative(settings, video_id):
    return f"thumbs/{video_id}/thumbnail.jpg"


def score(candidate, reference):
    """Best cosine and pHash distance between any thumbnail view and any reference frame view."""
    cv = np.asarray(candidate["vectors"], dtype=np.float32)
    rv = np.asarray(reference["vectors"], dtype=np.float32)
    sims = cv @ rv.T
    ci, ri = np.unravel_index(int(np.argmax(sims)), sims.shape)
    distances = (candidate["hashes"][:, None, :] != reference["hashes"][None, :, :]).sum(-1)
    hi = np.unravel_index(int(np.argmin(distances)), distances.shape)
    best = float(sims[ci, ri])
    pair = {"candidate_time": 0.0, "reference_time": float(reference["times"][ri]), "similarity": best, "cosine": best,
            "phash_distance": int(distances[hi]), "candidate_thumb": str(candidate["thumbs"][ci]),
            "reference_thumb": str(reference["thumbs"][ri]), "crop": str(candidate["labels"][ci]),
            "reference_crop": str(reference["labels"][ri])}
    return {"score": best, "status": "thumbnail", "temporal_frames": 0, "speed": None, "offset": None,
            "start": 0.0, "end": 0.0, "phash_distance": int(distances[hi]), "pairs": [pair]}


def phash_hex(candidate):
    """64-bit pHash of the whole thumbnail, for grouping identical re-uploads."""
    bits = np.asarray(candidate["hashes"][0], dtype=np.uint8)
    return np.packbits(bits).tobytes().hex()
